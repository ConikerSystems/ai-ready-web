import fitz  # PyMuPDF
import re
import shutil
import hashlib
import subprocess
import tempfile
import os
from datetime import date
from converters import content_detector


# OCR render resolution. 300 DPI grayscale + a direct Tesseract LSTM pass measured
# consistently >= PyMuPDF's get_textpage_ocr wrapper across scanned legal documents
# (cleaner headers, URLs, and paragraph structure).
_OCR_DPI = 300

# Optional high-accuracy LSTM model (tessdata_best/eng, ~15MB) bundled under the
# project's tessdata/. When present it roughly HALVES serif-font character
# confusions (e.g. "r"→"t") vs the system "fast" model, at ~2× the time. Fetched
# via scripts/fetch_ocr_model.sh; absent → we use the system model (graceful).
_BEST_TESSDATA = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tessdata")


def _best_tessdata_dir() -> str:
    """Path to the bundled high-accuracy tessdata dir, or "" if its model is absent."""
    return _BEST_TESSDATA if os.path.exists(
        os.path.join(_BEST_TESSDATA, "eng.traineddata")) else ""


def _ocr_available() -> bool:
    """Return True if Tesseract OCR is installed and accessible."""
    return shutil.which("tesseract") is not None


def _clean_ocr_text(text: str, keep_all: bool = False) -> str:
    """Drop decorative scan artifacts that carry no information, conservatively.
    A line is removed ONLY if it has no alphanumeric character at all (e.g. an OCR'd
    rule line "~~~~~" or "———"); any line with a letter or digit is kept verbatim, so
    real content is never lost. Runs of blank lines collapse to one.

    keep_all=True keeps symbol-only lines too (used by the verbatim PDF→DOCX export,
    where a signature underscore line or "* * *" separator is meaningful)."""
    out, blank = [], False
    for line in text.splitlines():
        if not line.strip():
            if not blank:
                out.append("")
            blank = True
            continue
        blank = False
        if not keep_all and not re.search(r"[A-Za-z0-9]", line):   # pure symbols → artifact
            continue
        out.append(line.rstrip())
    return "\n".join(out).strip()


# Verify-workflow thresholds (Tesseract per-word confidence, 0–100).
#   _OCR_FLAG_CONF      : a word at/below this is listed under "verify these".
#   _OCR_PAGE_REVIEW_CONF: a page whose MEAN word confidence is below this is
#                          marked "review recommended" (a degraded-scan signal).
#   _OCR_WORD_REVIEW_CONF: a SUBSTANTIVE word (real, load-bearing) at/below this
#                          ALSO marks its page for review — even when the page mean
#                          looks fine. This catches the dangerous case where the
#                          page averages well but one critical term is degraded
#                          (e.g. "arbitration" read at 0% on a 89%-mean order page).
#                          Page mean alone hid those pages from the reader's eye.
# NOTE — what confidence can and cannot do: it reliably flags illegible/garbled
# reads, but it does NOT catch a CONFIDENT mis-read of one real word as another
# (e.g. a clean "are" recognized as "ate" can score 95). Those slips require
# reading the text against the original; this workflow narrows where to look, it
# does not certify the rest. Documented for the user in the page output.
_OCR_FLAG_CONF = 60
_OCR_PAGE_REVIEW_CONF = 88
_OCR_WORD_REVIEW_CONF = 50
_OCR_FLAG_MAX = 30      # cap the per-page verify list so a bad scan doesn't flood it


def _is_substantive_ocr_word(word: str) -> bool:
    """A 'real word' whose misreading matters — alphabetic and long enough to carry
    meaning, so junk OCR tokens ('oo', 'KH', 'Vv.', 'i}') don't escalate a page.
    Punctuation/digits are stripped before measuring length."""
    return len(re.sub(r"[^A-Za-z]", "", word)) >= 4


def _ocr_page_needs_review(mean_conf, flagged) -> bool:
    """Flag a page for human review when its MEAN word confidence is low, OR it
    contains a substantive (load-bearing) word misread at/below the word-review bar
    even though the page mean looks fine. `flagged` is the page's [(word, conf)]
    list (already ≤ _OCR_FLAG_CONF)."""
    if mean_conf is not None and mean_conf < _OCR_PAGE_REVIEW_CONF:
        return True
    return any(c <= _OCR_WORD_REVIEW_CONF and _is_substantive_ocr_word(w)
               for w, c in flagged)


def _run_tesseract(page: fitz.Page, dpi: int, args: list) -> str:
    """Render the page to a grayscale PNG and run Tesseract, returning its stdout
    (or the contents of <base>.<ext> when args write a sidecar file). "" on failure."""
    img = base = None
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img = f.name
        pix.save(img)
        cmd = ["tesseract", img, "-", "--oem", "1", "--psm", "3", "-l", "eng"]
        best = _best_tessdata_dir()
        if best:
            cmd += ["--tessdata-dir", best]
        cmd += args
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        return proc.stdout or ""
    except Exception:
        return ""
    finally:
        if img and os.path.exists(img):
            try:
                os.unlink(img)
            except OSError:
                pass


def _ocr_page_data(page: fitz.Page, dpi: int, keep_all: bool = False):
    """OCR a page via Tesseract TSV (one pass) → (text, mean_conf, flagged).
    Body text is reconstructed from the same word boxes that carry the confidence,
    so the two are always consistent. `mean_conf` is the mean per-word confidence
    (0–100, or None if no words); `flagged` is an ordered, de-duplicated list of
    (word, conf) at/below _OCR_FLAG_CONF. Returns (None, None, []) on failure."""
    img = base = None
    try:
        pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img = f.name
        pix.save(img)
        base = tempfile.mktemp()
        cmd = ["tesseract", img, base, "--oem", "1", "--psm", "3", "-l", "eng"]
        best = _best_tessdata_dir()
        if best:
            cmd += ["--tessdata-dir", best]
        cmd += ["-c", "tessedit_create_tsv=1"]
        subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        rows = [r.split("\t") for r in open(base + ".tsv", encoding="utf-8").read().splitlines()[1:]]
    except Exception:
        return None, None, []
    finally:
        for p in (img, (base + ".tsv") if base else None):
            if p and os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    by_line, confs, flagged, seen = {}, [], [], set()
    for r in rows:
        if len(r) < 12 or r[0] != "5" or not r[11].strip():
            continue                              # level 5 = a word with text
        word = r[11]
        try:
            conf = float(r[10])
        except ValueError:
            continue
        by_line.setdefault((int(r[2]), int(r[3]), int(r[4])), []).append(word)
        confs.append(conf)
        if conf <= _OCR_FLAG_CONF and word not in seen and re.search(r"[A-Za-z0-9]", word):
            seen.add(word)
            flagged.append((word, round(conf)))

    out, prev_par = [], None
    for (blk, par, ln) in sorted(by_line):
        if prev_par is not None and (blk, par) != prev_par:
            out.append("")
        out.append(" ".join(by_line[(blk, par, ln)]))
        prev_par = (blk, par)
    text = _clean_ocr_text("\n".join(out), keep_all=keep_all)
    mean_conf = sum(confs) / len(confs) if confs else None
    return text, mean_conf, flagged


def _ocr_page(page: fitz.Page, page_num: int, file_slug: str) -> str:
    """
    Extract text from an image-only page via Tesseract OCR with a verify aid:
    the heading carries the page's mean OCR confidence, low-confidence pages are
    marked for review, and uncertain words are listed (so they can be checked
    against the original before the Markdown is turned into a document). Body text
    is kept clean. Falls back to a plain Tesseract pass, then PyMuPDF's OCR.
    """
    anchor_id = f"{file_slug}-page-{page_num:03d}" if file_slug else f"page-{page_num:03d}"
    text, mean_conf, flagged = _ocr_page_data(page, _OCR_DPI)

    if not text:                                  # fallbacks (no confidence data)
        text = _clean_ocr_text(_run_tesseract(page, _OCR_DPI, []))
        mean_conf, flagged = None, []
    if not text:
        try:
            tp = page.get_textpage_ocr(flags=0, language="eng", dpi=_OCR_DPI)
            text = _clean_ocr_text(page.get_text(textpage=tp))
        except Exception:
            text = ""
    if not text:
        return (f'\n<a id="{anchor_id}"></a>\n\n> *Page {page_num}: No extractable '
                f'text (image-only or scanned page — OCR unavailable)*\n')

    conf_str = f" · confidence {round(mean_conf)}%" if mean_conf is not None else ""
    mean_low = (mean_conf is not None and mean_conf < _OCR_PAGE_REVIEW_CONF)
    review = _ocr_page_needs_review(mean_conf, flagged)
    heading = f"### Page {page_num} (OCR — {_OCR_DPI} DPI{conf_str})"
    if review:
        # Reason-aware: a low page mean reads differently than a clean-looking page
        # whose one critical word was misread — the latter is the easy-to-miss case.
        heading += ("  ⚠️ low confidence — review recommended" if mean_low
                    else "  ⚠️ key word(s) misread — review recommended")

    block = f'\n<a id="{anchor_id}"></a>\n\n{heading}\n\n{text}\n'
    if flagged:
        # Surface substantive (real-word) misreads FIRST, lowest-confidence first, so
        # the load-bearing words are never pushed past the cap by junk tokens.
        ordered = sorted(flagged, key=lambda wc: (not _is_substantive_ocr_word(wc[0]), wc[1]))
        shown = ordered[:_OCR_FLAG_MAX]
        words = " · ".join(f'`{w}` ({c}%)' for w, c in shown)
        more = f" … +{len(ordered) - len(shown)} more" if len(ordered) > len(shown) else ""
        block += (f'\n> ⚠️ **Verify against original — low-confidence OCR words:** '
                  f'{words}{more}\n')
    return block


def _page_needs_ocr(page: fitz.Page) -> bool:
    """True if a page should be OCR'd rather than read from its text layer.

    A page is OCR'd when it has effectively no extractable text (a pure scan) OR
    when its text is negligible AND a raster image covers most of the page — the
    "scanned page with a stray text layer" case (a Bates stamp or page-number
    overlay) that the old `any text block` test wrongly treated as digital,
    silently dropping the scanned body."""
    text = page.get_text().strip()
    if len(text) < 20:
        return True
    if len(text) < 200:                       # sparse text — could be a scan + stamp
        try:
            page_area = abs(page.rect.width * page.rect.height) or 1.0
            img_area = 0.0
            for info in page.get_image_info():
                bbox = info.get("bbox")
                if bbox:
                    img_area += abs((bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
            if img_area / page_area > 0.5:    # a large image dominates the page
                return True
        except Exception:
            pass
    return False


def extract_page_text_verbatim(page: fitz.Page, dpi: int = _OCR_DPI,
                               ocr_enabled: bool = True) -> str:
    """Faithful per-page text for the PDF→DOCX export: the digital text layer in
    reading order, or OCR for scanned pages. No tax/gold-master reformatting and
    no dropping of symbol-only lines (keep_all)."""
    if _page_needs_ocr(page) and ocr_enabled:
        text, _conf, _flag = _ocr_page_data(page, dpi, keep_all=True)
        if text:
            return text
    return page.get_text("text", sort=True).rstrip()


# Known structured form patterns for smart line detection
_FORM_PATTERNS = [
    # 1040-X before 1040 so amended returns are identified correctly
    (re.compile(r"\b(form\s*1040[-\s]?x)\b", re.IGNORECASE), "1040-X", "Amended U.S. Individual Income Tax Return"),
    (re.compile(r"\b(form\s*1040)\b", re.IGNORECASE), "1040", "U.S. Individual Income Tax Return"),
    (re.compile(r"\b(form\s*1099[-\s]\w+)\b", re.IGNORECASE), "1099", "Tax Information Return"),
    (re.compile(r"\b(form\s*w[-\s]?2)\b", re.IGNORECASE), "W-2", "Wage and Tax Statement"),
    (re.compile(r"\b(form\s*w[-\s]?9)\b", re.IGNORECASE), "W-9", "Request for Taxpayer ID"),
    (re.compile(r"\b(form\s*941)\b", re.IGNORECASE), "941", "Employer's Quarterly Federal Tax Return"),
    # Schedule K-1 — federal and state partnership/S-corp income
    (re.compile(r"\b(schedule\s*k[-\s]?1)\b", re.IGNORECASE), "K-1", "Partner's Share of Income, Deductions, Credits"),
    # State K-1 variants (CA 565, MN KPI, WV EK-1, etc.)
    (re.compile(r"\b(k[-\s]?1\s*\(\s*\d{3,4}\s*\))\b", re.IGNORECASE), "K-1", "State Schedule K-1"),
    # Form 5713 — International Boycott Report
    (re.compile(r"\b(form\s*5713)\b", re.IGNORECASE), "5713", "International Boycott Report"),
    # PTE / pass-through entity tax forms
    (re.compile(r"\b(ptet|pte[-\s]cr|pass.through entity)\b", re.IGNORECASE), "PTE", "Pass-Through Entity Tax"),
]

# Only match line labels followed by a clean numeric value (dollar amount).
# Avoids capturing form cross-references like "line 3b" → "4a" or instructions.
# Only match line labels followed by a meaningful numeric value (100+, or a decimal like 0.xx).
# Excludes single/double-digit checkbox numbers and form cross-references.
_LINE_RE = re.compile(
    r"(?:^|\n)\s*(line\s+(\d+[a-z]?))\s*[:\.\-]?\s*([-\$\(]?(?:[\d,]{3,}|\d+\.\d+)[\)]?)\s*$",
    re.IGNORECASE | re.MULTILINE
)
_SECTION_RE = re.compile(
    r"(?:^|\n)\s*(part\s+[IVXivx]+|section\s+\d+[a-z]?|schedule\s+[a-z]"
    r"|line\s+\d+[a-z]?,\s*[A-Z][^,\n]{3,40}"    # K-1: "Line 1, Ordinary Income"
    r"|distributive\s+share\s+items"               # K-1 column header
    r")\s*[:\-]?\s*(.{0,80}?)(?=\n|$)",
    re.IGNORECASE | re.MULTILINE
)
_CASE_NO_RE = re.compile(r"\b(?:case\s*(?:no|number|#)\.?\s*[:\-]?\s*)([A-Z0-9\-]{4,20})\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")

# Document-type detectors
_FORM_1040_RETURN_RE = re.compile(r'U\.S\.\s+Individual\s+Income\s+Tax\s+Return', re.IGNORECASE)
_TAX_PERIOD_RE = re.compile(r'Tax Period\s+Requested[:\s]+(\d{2})-(\d{2})-(\d{4})', re.IGNORECASE)
_TRANSCRIPT_DATE_RE = re.compile(r'(?:Request|Response)\s+Date[:\s]+(\d{2}-\d{2}-\d{4})', re.IGNORECASE)

# Noise filtering — dot leaders, IRS boilerplate, and medical boilerplate
_DOT_LEADER_RE = re.compile(r'^[.\s\-_·•…]{3,}$')
_NOISE_FRAGS_RE = re.compile(
    r'For Paperwork Reduction Act|'
    r'\bCat\.\s*No\.\s*\d+|'
    r'www\.irs\.gov[/\w]*|'
    r'\bOMB No\.\s*\d{4}-\d{4}|'
    # Medical boilerplate
    r'continued on (?:next|following) page|'
    r'this (?:report|result|document) (?:has been |is )?electronically (?:signed|verified)|'
    r'for (?:clinical|medical|diagnostic) use only|'
    r'this (?:report|result) (?:is )?not valid without|'
    r'please (?:note|see) (?:the )?(?:following |)(?:important |)(?:information|note|disclaimer)|'
    r'results?\s+reported\s+(?:in|as)\s+(?:mg|mmol|mcg|ug|ng|pg|mEq|IU|U|g|L|dL|mL)/|'
    r'reference\s+(?:intervals?|ranges?)\s+(?:are|were)\s+(?:established|determined|based)|'
    r'specimen\s+(?:stability|integrity|handling|collection)\s+(?:information|notes?|instructions?)',
    re.IGNORECASE,
)

# Medical section headers for clinical notes and reports
_MEDICAL_SECTION_RE = re.compile(
    r'^(?P<label>'
    r'HISTORY|FINDINGS?|IMPRESSION|ASSESSMENT(?:\s+AND\s+PLAN)?|PLAN|CONCLUSION|'
    r'CYTOLOGIC\s+DIAGNOSIS|PATHOLOGIC\s+DIAGNOSIS|FINAL\s+DIAGNOSIS|CLINICAL\s+DIAGNOSIS|'
    r'SPECIMEN|GROSS\s+DESCRIPTION|MICROSCOPIC\s+DESCRIPTION|CLINICAL\s+HISTORY|'
    r'INDICATION|TECHNIQUE|COMPARISON|PROCEDURE|RESULT|SUMMARY|RECOMMENDATION|'
    r'CC|HPI|ROS|PMH(?:x)?|PSH|FH|SH|SOC(?:\s+HX)?|OB(?:\s+HIST)?|'
    r'MEDICATIONS?|ALLERGIES?|VITALS?|EXAM(?:INATION)?|PHYSICAL\s+EXAM(?:INATION)?|'
    r'CHIEF\s+COMPLAINT|REVIEW\s+OF\s+SYSTEMS|PAST\s+(?:MEDICAL\s+)?HISTORY|'
    r'SOCIAL\s+HISTORY|FAMILY\s+HISTORY|SURGICAL\s+HISTORY'
    r')[\s:\.]+',
    re.IGNORECASE | re.MULTILINE,
)


# Medical lab value patterns
# Matches: "Test Name Your Value X.XX units Standard Range Y-Z units [Flag]"
# Also:    "Test Name In Range X.XX  Reference Range Y-Z"
_LAB_INLINE_RE = re.compile(
    r'^(?P<test>.+?)\s+'
    r'(?:Your\s+Value|Result|In\s+Range|Out\s+Of\s+Range)\s+'
    r'(?P<value>[\d.,<>]+\s*(?:mg/dL|mmol/L|IU/L|ng/dL|pg/mL|mIU/L|g/dL|%|U/L|µg/dL|mcg/dL|'
    r'cells/µL|10\^3/µL|10\^6/µL|mEq/L|fl|fmol|nmol|pmol|µmol|umol|ng/L|ng/mL|µg/L|mcg/L|'
    r'mOsm/kg|mOsm/L|copies/mL|ratio|titer|index|bpm|mmHg|cm|mm)?)\s*'
    r'(?P<flag>H\b|L\b|High\b|Low\b|Abnormal\b|Abn\b|Critical\b|Panic\b)?\s*'
    r'(?:Standard\s+Range|Reference\s+Range|Ref\s+Range|Range)\s+'
    r'(?P<range>.+?)$',
    re.IGNORECASE,
)

# Flags that indicate an abnormal lab result — preserve these prominently
_LAB_FLAG_RE = re.compile(
    r'\b(H\b|L\b|High\b|Low\b|Abnormal\b|Abn\b|Above\b|Below\b|Critical\b|Panic\b|Out\s+of\s+Range|Elevated\b|Decreased\b)\b',
    re.IGNORECASE,
)

# Grouped test section headers within a lab report
# e.g. "COMPREHENSIVE METABOLIC PANEL", "CBC WITH DIFFERENTIAL", "LIPID PANEL"
_LAB_GROUP_RE = re.compile(
    r'^(?:'
    r'COMPREHENSIVE\s+METABOLIC\s+PANEL|BASIC\s+METABOLIC\s+PANEL|'
    r'CBC(?:\s+WITH\s+(?:DIFFERENTIAL|DIFF))?|COMPLETE\s+BLOOD\s+(?:COUNT|PANEL)|'
    r'LIPID\s+PANEL|THYROID|LIVER\s+(?:FUNCTION|PANEL)|RENAL\s+PANEL|'
    r'URINALYSIS|URINE\s+PANEL|HEMOGLOBIN\s+A1C|HBA1C|'
    r'COAGULATION|IRON\s+STUDIES|VITAMIN|MINERAL|ELECTROLYTES|'
    r'HORMONE|TUMOR\s+MARKER|INFLAMMATORY|CARDIAC|'
    r'[A-Z][A-Z\s/,]{4,50}'
    r')$',
    re.MULTILINE,
)


def _post_process_medical(md: str) -> str:
    """
    Post-process markdown from medical documents to improve lab result integrity.

    Handles three patterns generically:
    1. Inline format: "Test Your Value X.XX units Standard Range Y-Z [Flag]"
    2. Packed 3-col table: | Test Name Your Value X.XX units | Standard Range | Y-Z |
       where "Your Value" / "In Range" / "Out Of Range" is embedded in column 0
    3. Redundant/inconsistent table headers from original PDF (Component | Your Value |
       Standard Range | Flag) replaced with clean canonical header
    4. Lab section group headers promoted to #### subheadings
    """
    # Canonical lab table header — used once per section, replaces originals
    _LAB_TABLE_HEADER = "| Test | Result | Reference Range | Flag |"
    _LAB_TABLE_SEP    = "|---|---|---|---|"

    # Detect embedded-label table row: | Test Your Value X.XX | Standard Range | Y-Z |
    _PACKED_ROW_RE = re.compile(
        r'^\|\s*(.+?)\s+(?:Your\s+Value|In\s+Range|Out\s+Of\s+Range|Result)\s+'
        r'([\d.,<>\s]+(?:mg/dL|mmol/L|IU/L|ng/dL|pg/mL|mIU/L|g/dL|%|U/L|'
        r'µg/dL|mcg/dL|fl|ng/mL|µg/L|mcg/L|ng/L|nmol|pmol|umol|'
        r'mOsm/kg|mOsm/L|copies/mL|ratio|titer|index|bpm|mmHg|cm|mm)?)'
        r'\s*(H\b|L\b|High|Low|Abnormal|Abn|Critical|Panic|Above|Below)?\s*'
        r'\|\s*(?:Standard\s+)?(?:Range|Reference\s+Range|Ref\s+Range)?\s*'
        r'\|\s*(.+?)\s*\|',
        re.IGNORECASE,
    )

    # Detect original PDF table headers we want to replace
    _ORIG_HEADER_RE = re.compile(
        r'^\|\s*(?:Component|Test(?:\s+Name)?|RESULT)\s*\|'
        r'\s*(?:Your\s+Value|In\s+Range|Current\s+Result|Result)\s*\|'
        r'\s*(?:Standard\s+Range|Reference\s+(?:Range|Interval)|Ref\s+Range)\s*\|',
        re.IGNORECASE,
    )

    # Two-pass: collect lab rows into blocks, emit each block as one clean table
    # Pass 1: tag each line
    tagged = []  # (tag, content)  tag = 'lab', 'orig_header', 'sep', 'group', 'blank', 'prose'
    for line in md.splitlines():
        stripped = line.strip()
        if not stripped:
            tagged.append(('blank', line))
        elif _ORIG_HEADER_RE.match(stripped):
            tagged.append(('orig_header', stripped))
        elif stripped in ("| --- | --- | --- | --- |", "|---|---|---|---|",
                          "| --- | --- | --- |", "|---|---|---|"):
            tagged.append(('sep', stripped))
        elif _LAB_INLINE_RE.match(stripped):
            m = _LAB_INLINE_RE.match(stripped)
            test = m.group("test").strip().rstrip(":")
            value = m.group("value").strip()
            flag = (m.group("flag") or "").strip()
            ref_range = m.group("range").strip()
            tagged.append(('lab', f"| {test} | {value} | {ref_range} | {flag or '—'} |"))
        elif _PACKED_ROW_RE.match(stripped):
            m = _PACKED_ROW_RE.match(stripped)
            test = m.group(1).strip().rstrip(":")
            value = m.group(2).strip()
            flag = (m.group(3) or "").strip()
            ref_range = m.group(4).strip()
            tagged.append(('lab', f"| {test} | {value} | {ref_range} | {flag or '—'} |"))
        elif _LAB_GROUP_RE.match(stripped) and len(stripped) > 4 and stripped == stripped.upper():
            tagged.append(('group', stripped))
        else:
            tagged.append(('prose', line))

    # Pass 2: emit, collapsing lab rows into single tables
    output_lines = []
    i = 0
    while i < len(tagged):
        tag, content = tagged[i]

        if tag in ('orig_header', 'sep'):
            i += 1
            continue

        if tag == 'group':
            output_lines.append(f"\n#### {content}\n")
            i += 1
            continue

        if tag == 'lab':
            # Emit one header, then collect all consecutive lab rows
            # (skip blanks between them, stop at non-lab/non-blank content)
            output_lines.append(_LAB_TABLE_HEADER)
            output_lines.append(_LAB_TABLE_SEP)
            while i < len(tagged):
                t, c = tagged[i]
                if t == 'lab':
                    output_lines.append(c)
                    i += 1
                elif t == 'blank':
                    # Peek ahead — if next non-blank is also a lab row, keep going
                    j = i + 1
                    while j < len(tagged) and tagged[j][0] == 'blank':
                        j += 1
                    if j < len(tagged) and tagged[j][0] == 'lab':
                        i += 1  # skip the blank, continue collecting
                    else:
                        break   # real gap — end this table
                elif t in ('orig_header', 'sep'):
                    i += 1  # skip redundant headers within a section
                else:
                    break
            output_lines.append("")
            continue

        if tag == 'blank':
            output_lines.append("")
            i += 1
            continue

        output_lines.append(content)
        i += 1

    return "\n".join(output_lines)


def _clean_line(line: str) -> str:
    """Return cleaned line, or empty string if the line is pure noise."""
    s = line.strip()
    if not s:
        return ""
    if _DOT_LEADER_RE.match(s):
        return ""
    # High dot density (form fill-in leaders like "Name . . . . . . . .")
    non_space = s.replace(" ", "")
    if len(non_space) > 6 and non_space.count(".") / len(non_space) > 0.6:
        return ""
    if _NOISE_FRAGS_RE.search(s):
        return ""
    return s


def _detect_form(full_text: str) -> tuple[str, str]:
    for pattern, form_id, form_name in _FORM_PATTERNS:
        if pattern.search(full_text):
            return form_id, form_name
    return "", ""


# A form designation describes the WHOLE document only when the form signal is
# representative across its pages — otherwise a single stray "Form 1099-X" buried
# in a large mixed production (e.g. a multi-thousand-page Bates discovery set)
# would mislabel the entire document as one tax return AND trigger tax-grid
# geometry over every page. Small documents keep the historical whole-doc
# behavior exactly (cheap and almost always correct); large documents must earn
# the Tax Form label by having tax-form signals on a meaningful share of pages.
_FORM_WHOLE_DOC_MAX_PAGES = 150
_FORM_REPRESENTATIVE_RATIO = 0.25
_CONTENT_REPRESENTATIVE_RATIO = 0.30


def _form_classifies_whole_doc(page_texts: list[str]) -> bool:
    """True if a detected tax-form signal legitimately describes the whole document
    (small doc, or tax-form pages are a meaningful share of a large one)."""
    n = len(page_texts)
    if n <= _FORM_WHOLE_DOC_MAX_PAGES:
        return True
    need = n * _FORM_REPRESENTATIVE_RATIO
    tax_pages = 0
    for t in page_texts:
        if _detect_form(t)[0]:
            tax_pages += 1
            if tax_pages >= need:
                return True
    return False


def _dominant_content_type(page_texts: list[str]) -> str:
    """Domain content-type for the WHOLE document. On a small document this is the
    historical keyword scan over the full text. On a large one, keyword *presence*
    across thousands of pages is meaningless (nearly every domain's terms appear
    somewhere), so a single domain present on a handful of pages would mislabel a
    mixed production — and trigger that domain's expensive specialized index over
    the whole doc. Instead classify by the domain that is REPRESENTATIVE per page,
    requiring a meaningful share; otherwise 'generic' (→ universal Document Index)."""
    n = len(page_texts)
    if n <= _FORM_WHOLE_DOC_MAX_PAGES:
        return content_detector.detect_content_type("\n".join(page_texts))
    need = n * _CONTENT_REPRESENTATIVE_RATIO
    votes: dict[str, int] = {}
    for t in page_texts:
        d = content_detector.detect_content_type(t)
        if d != "generic":
            votes[d] = votes.get(d, 0) + 1
    if not votes:
        return "generic"
    top = max(votes, key=lambda k: votes[k])
    return top if votes[top] >= need else "generic"


def _detect_title(doc: fitz.Document, full_text: str) -> dict:
    meta = doc.metadata or {}
    candidates = []

    if meta.get("title"):
        candidates.append(meta["title"].strip())

    first_page_text = doc[0].get_text() if len(doc) > 0 else ""
    for line in first_page_text.splitlines():
        stripped = line.strip()
        if stripped and len(stripped) > 4:
            candidates.append(stripped)
            break

    # Scan full document for form detection — cover pages and multi-section
    # returns (e.g. TurboTax printouts) have forms starting on later pages
    form_id, form_name = _detect_form(full_text)
    case_matches = _CASE_NO_RE.findall(full_text[:3000])

    # Tax year: for IRS transcripts, extract from "Tax Period Requested" to avoid
    # confusing the request date (e.g. 2026) with the actual tax period (e.g. 2024).
    tax_year = ""
    tax_period = ""
    transcript_date = ""
    period_m = _TAX_PERIOD_RE.search(full_text[:5000])
    if period_m:
        tax_period = period_m.group(3)
        tax_year = period_m.group(3)
        date_m = _TRANSCRIPT_DATE_RE.search(full_text[:1500])
        if date_m:
            transcript_date = date_m.group(1)
    else:
        # Strongest tax-year signal: the form's own dated header/footer, e.g.
        # "Form 1040 (2024)" or "FOR THE YEAR ENDING December 31, 2024". Preparer
        # packets (H&R Block, TurboTax) prepend cover sheets, invoices, and a
        # "2025 Income Tax Estimator/Planner" whose years would otherwise win a
        # raw frequency count — so trust the explicit return-year markers first.
        # Match only the actual return form header — 1040, 1040-SR, 1040-NR — and
        # NOT "Form 1040-ES (YYYY)", the estimated-tax voucher for the FOLLOWING
        # year. A CPA packet for a 2023 return bundles 2024 1040-ES vouchers, and
        # the old "[A-Z\- ]*" wildcard matched "Form 1040-ES (2024)" first, mis-
        # dating the whole return as 2024.
        explicit_year = (
            re.search(r'Form\s+1040(?:-?(?:SR|NR))?\s*\((20\d{2})\)', full_text)
            or re.search(r'FOR\s+THE\s+YEAR\s+(?:ENDING|BEGINNING)\b[^\d]{0,40}?(20\d{2})',
                         full_text, re.IGNORECASE)
            or re.search(r'for\s+the\s+year\s+Jan\w*\.?\s*1\b[^\n]{0,60}?(20\d{2})',
                         full_text, re.IGNORECASE)
        )
        all_years_20 = re.findall(r'\b(20\d{2})\b', full_text[:10000])
        all_years_19 = re.findall(r'\b(19\d{2})\b', full_text[:2000])
        if explicit_year:
            tax_year = explicit_year.group(1)
        elif all_years_20:
            # Pick the most common 20xx year (avoids picking processing/filing year)
            from collections import Counter
            year_counts = Counter(all_years_20)
            # Exclude the current year (2026) as it's likely a processing date
            current_year = str(__import__('datetime').datetime.now().year)
            candidates = [(y, c) for y, c in year_counts.most_common() if y != current_year]
            tax_year = candidates[0][0] if candidates else (all_years_20[0] if all_years_20 else "")
        elif all_years_19:
            tax_year = all_years_19[0]

    return {
        "potential_titles": candidates[:3],
        "form": form_id,
        "form_name": form_name,
        "tax_year": tax_year,
        "tax_period": tax_period,
        "transcript_date": transcript_date,
        "case_numbers": list(set(case_matches))[:3],
        "author": meta.get("author", "").strip(),
        "creator": meta.get("creator", "").strip(),
        "page_count": len(doc),
    }


def _page_to_markdown(page: fitz.Page, page_num: int, form_id: str,
                      file_slug: str = "", content_type: str = "") -> str:
    """Convert a page to Markdown using coordinate-aware block extraction with AcroForm fallback."""
    anchor_id = f"{file_slug}-page-{page_num:03d}" if file_slug else f"page-{page_num:03d}"
    anchor = f'<a id="{anchor_id}"></a>'

    # AcroForm path: fillable PDF fields take priority when present
    widgets = list(page.widgets())
    if widgets:
        lines = [f"\n{anchor}\n\n### Page {page_num} (AcroForm fields)\n"]
        rows = [(
            (w.field_name or "").strip(),
            ("Yes" if w.field_value is True else ("No" if w.field_value is False else str(w.field_value or "").strip()))
        ) for w in widgets if (w.field_name or "").strip() or str(w.field_value or "").strip()]
        if rows:
            lines += ["\n| Field | Value |", "|---|---|"]
            for fname, fval in rows:
                lines.append(f"| {fname} | {fval.replace('|', chr(92) + '|')} |")
            lines.append("")
        # The field table alone drops the page's printed/overlay text layer —
        # e-sign stamps, DocuSign timestamps, printed form labels. Append it
        # verbatim below the fields so nothing on the page is lost (additive only).
        page_text = "\n".join(
            cl for raw in page.get_text("text", sort=True).splitlines()
            for cl in (_clean_line(raw),) if cl
        ).strip()
        if page_text:
            lines.append(page_text)
            lines.append("")
        return "\n".join(lines)

    raw_blocks = page.get_text("blocks")
    # Each block: (x0, y0, x1, y1, text, block_no, block_type); type 0 = text
    text_blocks = [
        (b[0], b[1], b[2], b[3], b[4])
        for b in raw_blocks
        if len(b) >= 7 and b[6] == 0 and b[4].strip()
    ]

    if not text_blocks:
        return f"\n{anchor}\n\n> *Page {page_num}: No extractable text (image-only or scanned page)*\n"

    lines = [f"\n{anchor}\n\n### Page {page_num}\n"]

    # Build cleaned full text for regex-based structured extraction
    full_text = "\n".join(
        cl
        for _, _, _, _, btext in sorted(text_blocks, key=lambda b: (b[1], b[0]))
        for raw in btext.splitlines()
        for cl in (_clean_line(raw),)
        if cl
    )

    if not full_text.strip():
        return f"\n{anchor}\n\n> *Page {page_num}: No extractable text after filtering*\n"

    # Structured forms: prefer line/section table output when regex matches succeed
    if form_id:
        # Emit schedule/part section headers (Schedule D, Part I, etc.)
        section_matches = _SECTION_RE.findall(full_text)
        if section_matches:
            for sec_label, sec_title in section_matches:
                lines.append(f"\n#### {sec_label.strip()}{': ' + sec_title.strip() if sec_title.strip() else ''}\n")
        # Emit a clean line-value table for any lines with numeric values
        line_matches = _LINE_RE.findall(full_text)
        if line_matches:
            lines.append("\n| Line | Value |")
            lines.append("|------|-------|")
            for line_label, _, value in line_matches:
                cv = _clean_line(value.strip()).replace("|", "\\|")
                if cv:
                    lines.append(f"| {line_label.strip()} | {cv} |")
            lines.append("")
        # Always fall through to coordinate-aware extraction so dollar values
        # in right-aligned blocks (the common IRS/TurboTax layout) are also captured.

    # Medical documents: promote clinical section headers (FINDINGS:, HPI:, etc.)
    # to markdown subheadings so AI can navigate clinical note structure
    if content_type == "Medical Document":
        processed = _MEDICAL_SECTION_RE.sub(
            lambda m: f"\n#### {m.group('label').strip()}\n", full_text
        )
        if processed != full_text:
            # Re-emit with promoted headers
            lines.append(processed.strip())
            lines.append("")
            return "\n".join(lines)

    # Coordinate-aware extraction: group side-by-side blocks into visual rows.
    # Y_BAND is adaptive: use the median block height so tightly-spaced table cells
    # (common in financial statements where each cell is its own PDF block) still group.
    text_blocks.sort(key=lambda b: b[1])  # top to bottom
    block_heights = [max(b[3] - b[1], 1) for b in text_blocks]
    block_heights.sort()
    median_h = block_heights[len(block_heights) // 2]
    Y_BAND = max(15, min(int(median_h * 1.2), 40))
    rows: list[list[tuple]] = []
    current_row: list[tuple] = [text_blocks[0]]
    row_cy = (text_blocks[0][1] + text_blocks[0][3]) / 2

    for block in text_blocks[1:]:
        block_cy = (block[1] + block[3]) / 2
        if abs(block_cy - row_cy) <= Y_BAND:
            current_row.append(block)
        else:
            rows.append(sorted(current_row, key=lambda b: b[0]))
            current_row = [block]
            row_cy = block_cy
    rows.append(sorted(current_row, key=lambda b: b[0]))

    def _block_lines(btext):
        return [cl for raw in btext.splitlines() for cl in (_clean_line(raw),) if cl]

    def _is_numeric(s):
        return bool(re.match(r'^-?[\$\(]?[\d,]+\.?\d*[\)]?%?$', s.strip()))

    def _is_header_row(parts):
        if not parts:
            return False
        non_numeric = sum(1 for p in parts if not _is_numeric(p))
        return non_numeric >= len(parts) * 0.6

    def _escape_cell(s):
        return s.replace("|", "\\|")

    def _emit_table_row(parts, header_emitted_ref):
        row_md = "| " + " | ".join(_escape_cell(p) for p in parts) + " |"
        sep = "|" + "|".join(" --- " for _ in parts) + "|"
        if not header_emitted_ref[0] and _is_header_row(parts):
            lines.append(row_md)
            lines.append(sep)
            header_emitted_ref[0] = True
        else:
            if not header_emitted_ref[0]:
                lines.append(sep)
                header_emitted_ref[0] = True
            lines.append(row_md)

    # ── Pass 1: build a list of "logical rows" ─────────────────────────────────
    # Each logical row is a list of cell strings.
    # Two patterns are handled:
    #   A) Multiple PDF blocks on the same Y-band → cells = one per block
    #   B) Single PDF block with N newline-separated lines → cells = those lines
    #      (common in Coinbase, some Fidelity/Schwab exports)
    logical_rows = []
    for row in rows:
        if len(row) >= 2:
            # Pattern A: multi-block row
            parts = []
            for x0, y0, x1, y1, btext in row:
                seg = " ".join(_block_lines(btext))
                if seg:
                    parts.append(seg)
            logical_rows.append(("multi", parts))
        else:
            # Single block — check if it contains multiple tab-like lines (Pattern B)
            _, _, _, _, btext = row[0]
            cell_lines = _block_lines(btext)
            if len(cell_lines) >= 3:
                logical_rows.append(("packed", cell_lines))
            else:
                logical_rows.append(("prose", cell_lines))

    # ── Pass 2: detect table spans ─────────────────────────────────────────────
    # A span of consecutive "multi" or "packed" rows with consistent cell counts
    # is rendered as a markdown table. Prose rows break the table.
    from collections import Counter

    # Find dominant cell count for packed rows
    packed_counts = [len(p) for t, p in logical_rows if t == "packed" and len(p) >= 3]
    dominant_packed = Counter(packed_counts).most_common(1)[0][0] if packed_counts else 0

    multi_counts = [len(p) for t, p in logical_rows if t == "multi" and len(p) >= 3]
    dominant_multi = Counter(multi_counts).most_common(1)[0][0] if multi_counts else 0

    in_table = False
    header_emitted_ref = [False]

    for kind, parts in logical_rows:
        if not parts:
            if in_table:
                lines.append("")
                in_table = False
                header_emitted_ref = [False]
            continue

        is_table_row = (
            (kind == "packed" and dominant_packed >= 3 and len(parts) == dominant_packed) or
            (kind == "multi"  and dominant_multi  >= 3 and len(parts) == dominant_multi)  or
            (kind == "multi"  and len(parts) >= 3)
        )

        if is_table_row:
            if not in_table:
                in_table = True
                header_emitted_ref = [False]
            _emit_table_row(parts, header_emitted_ref)

        elif kind == "multi" and len(parts) == 2:
            if in_table:
                lines.append("")
                in_table = False
                header_emitted_ref = [False]
            last = parts[-1]
            last_num = re.sub(r"[\$,\(\)\s]", "", last)
            if re.match(r'^-?\d+\.?\d*$', last_num) and len(last) <= 20:
                lines.append(f"**{parts[0]}**: {last}")
            else:
                lines.append("  ".join(parts))
            lines.append("")

        else:
            if in_table:
                lines.append("")
                in_table = False
                header_emitted_ref = [False]
            for cl in parts:
                lines.append(cl)
            lines.append("")

    if in_table:
        lines.append("")

    return "\n".join(lines)


def _dedup_section3_pages(combined: str) -> tuple[str, int]:
    """Collapse byte-identical page copies in the Section 3 markdown.

    Tax returns ship the same forms 2-3× (filing + records copies); each copy is
    identical page text apart from its page number, so Section 3 carries that bulk
    several times. This keeps the FIRST occurrence in full and replaces each later
    identical page's BODY with a one-line pointer to it — preserving every page
    anchor (so no index link or navigation breaks) and losing no information (the
    pointer says where the identical content lives). Returns (md, pages_collapsed).

    Information-preserving and safe for any document: only byte-identical page
    bodies are collapsed, so a document without duplicate pages is unchanged.
    """
    parts = re.split(r'(<a id="[^"]*page-\d+[^"]*"></a>)', combined)
    if len(parts) < 4:
        return combined, 0
    out, seen, collapsed = [parts[0]], {}, 0
    for k in range(1, len(parts), 2):
        anchor = parts[k]
        body = parts[k + 1] if k + 1 < len(parts) else ''
        m = re.search(r'id="([^"]*page-(\d+)[^"]*)"', anchor)
        aid, pn = m.group(1), int(m.group(2))
        # Canonical body for comparison: drop the per-page "### Page N" header line.
        canon = re.sub(r'###\s*Page\s+\d+[^\n]*', '', body)
        canon = re.sub(r'\s+', ' ', canon).strip()
        h = hashlib.md5(canon.encode()).hexdigest()
        if canon and h in seen:
            first_pn, first_aid = seen[h]
            hdr_m = re.search(r'###\s*Page\s+\d+[^\n]*', body)
            hdr = hdr_m.group(0) if hdr_m else f'### Page {pn}'
            out.append(anchor)
            out.append(f"\n\n{hdr}\n\n_↩ Identical to [Page {first_pn}]"
                       f"(#{first_aid}) — duplicate copy removed to reduce size. "
                       f"See p.{first_pn} for the full content._\n")
            collapsed += 1
        else:
            if canon:
                seen[h] = (pn, aid)
            out.append(anchor)
            out.append(body)
    return ''.join(out), collapsed


def convert(file_path: str, filename: str, process_date: str, file_slug: str = "",
            progress_cb=None, control: dict = None, stage_cb=None) -> tuple[str, dict]:
    def _stage(label: str):
        # Coarse post-page-loop progress so a large document never looks frozen
        # while the index/integrity/assembly passes run (they carry no page counter).
        if stage_cb:
            try:
                stage_cb(label)
            except Exception:
                pass

    doc = fitz.open(file_path)
    page_texts = [page.get_text() for page in doc]
    full_text = "\n".join(page_texts)

    meta = _detect_title(doc, full_text)
    if meta["form"] and _form_classifies_whole_doc(page_texts):
        meta["content_type"] = "Tax Form"
    else:
        # Either no form signal, or a form signal that does not represent the whole
        # document (a stray tax form inside a large mixed production). In the latter
        # case drop the form id so downstream tax rendering / tax-grid geometry does
        # not run over a non-tax document; the universal Document Index handles it.
        meta["form"] = ""
        meta["form_name"] = ""
        detected = _dominant_content_type(page_texts)
        content_type_map = {
            "financial": "Financial Statement",
            "medical": "Medical Document",
            "legal": "Legal Document",
            "real_estate": "Real Estate Document",
            "insurance": "Insurance Document",
        }
        meta["content_type"] = content_type_map.get(detected, "PDF Document")

        # Filename fallback: tax forms often score as "Financial Statement" because
        # they have lots of dollar amounts. Check filename before accepting that label.
        if meta["content_type"] in ("PDF Document", "Financial Statement"):
            fname_lower = filename.lower()
            # LLC / operating-agreement / exhibit docs — check FIRST so an LLC series
            # agreement named after an entity ("EXHIBIT B - Eagles") isn't captured
            # by an entity-name collision in the insurance cue list below.
            if any(k in fname_lower for k in ['operating agreement', 'company agreement',
                                               'series agreement', 'llc agreement',
                                               'articles of organization', 'exhibit a',
                                               'exhibit b', 'partnership agreement']):
                meta["content_type"] = "Legal Document"
            # Tax forms — check first (highest priority for this app)
            elif any(k in fname_lower for k in ['1040', '1099', 'w-2', 'k-1', 'k1',
                                               'sch_k', 'schedule_k', 'tax_return', 'tax return',
                                               'tax year', 'tax period', 'irs', 'amended',
                                               '1040x', '5713', 'ptet', 'pte-cr', '592-b',
                                               '3804', 'sch_vk', 'sch_ek', 'sch_kp',
                                               'deferred_income', 'section_199a', 'refunds',
                                               'payments and refund', 'state_and_city',
                                               'state_pte', 'actions-signed', 'actions_signed']):
                meta["content_type"] = "Tax Form"
                if not meta.get("form"):
                    meta["form"] = "Tax Document"
                    meta["form_name"] = "Tax Form or Schedule"
            elif any(k in fname_lower for k in ['insurance', 'ho6', 'ho-6', 'policy', 'umbrella',
                                               'homeowner', 'renters', 'neptune', 'eagles',
                                               'dec official', 'declaration', 'coverage',
                                               'premium', 'flood', 'dwelling']):
                meta["content_type"] = "Insurance Document"
            elif any(k in fname_lower for k in ['medical', 'lab', 'labs', 'doctor', 'hospital',
                                                 'clinic', 'radiology', 'pathology']):
                meta["content_type"] = "Medical Document"
            elif any(k in fname_lower for k in ['contract', 'agreement', 'deposition', 'complaint',
                                                 'petition', 'motion', 'brief', 'transcript',
                                                 'affidavit', 'judgment', 'order', 'subpoena']):
                meta["content_type"] = "Legal Document"

            # Content-signal legal upgrade: a generic PDF with a case/docket number,
            # an "X v. Y" caption, or several court/procedural terms IS a legal
            # document even if its filename and term-density didn't trip the legal
            # classifier — so filings, notices, court rules, and case law all get the
            # audit-ready Legal Index. "Any legal file" is audit-ready.
            if meta["content_type"] == "PDF Document":
                _sig = full_text
                _score = 0
                if content_detector.extract_case_numbers(_sig):
                    _score += 2
                if re.search(r"\b[A-Z][A-Za-z.&' ]{2,40}\s+v\.\s+[A-Z]", _sig):
                    _score += 2
                _sl = _sig.lower()
                _score += sum(1 for t in (
                    'plaintiff', 'defendant', 'petitioner', 'respondent', 'appellant',
                    'appellee', 'whereas', 'hereby', 'docket', 'affidavit', 'notice of',
                    'certificate of service', 'circuit court', 'supreme court', 'appellate',
                    'jurisdiction', 'in witness whereof', 'undersigned', 'ill. app',
                    'notary public', 'litigation', 'counsel for') if t in _sl)
                if _score >= 3:
                    meta["content_type"] = "Legal Document"

    # Track scanned page ratio for audit visibility
    image_only_pages = sum(
        1 for p in doc
        if not any(b[6] == 0 and b[4].strip()
                   for b in p.get_text("blocks") if len(b) >= 7)
    )
    meta["image_only_pages"] = image_only_pages
    meta["scanned_ratio"] = round(image_only_pages / max(len(doc), 1), 2)

    form_id = meta["form"]
    content_type = meta["content_type"]
    ocr_enabled = _ocr_available()
    total_pages = len(doc)

    # Pages that should be OCR'd: pure scans AND scanned pages carrying only a
    # stray text layer (Bates stamp / page-number overlay). _page_needs_ocr keeps
    # genuinely-digital pages (e.g. tax forms, text-rich legal PDFs) on the text path.
    image_only_indices = [i for i, p in enumerate(doc) if _page_needs_ocr(p)]
    ocr_page_count = len(image_only_indices) if ocr_enabled else 0
    ocr_done = 0

    pages_md = []
    raw_source_pages = []  # raw text of digital pages, for source-data integrity
    for i, page in enumerate(doc, start=1):
        # Check skip/stop signal from job controller
        if control and (control.get("skip") or control.get("stop")):
            break

        is_image_only = (i - 1) in image_only_indices

        if is_image_only and ocr_enabled:
            ocr_done += 1
            if progress_cb:
                progress_cb(i, total_pages, ocr_done, ocr_page_count, True)
            pages_md.append(_ocr_page(page, i, file_slug))
        else:
            # Integrity baseline = source text after the SAME line-level noise
            # filtering the extractor applies (drops form boilerplate like
            # "OMB No. 1545-0074"), so the check flags only real data loss.
            raw_source_pages.append("\n".join(
                cl for raw in page.get_text().splitlines()
                for cl in (_clean_line(raw),) if cl
            ))
            pages_md.append(_page_to_markdown(page, i, form_id, file_slug, content_type))
            # Advance the page counter on digital pages too (throttled), so the bar
            # moves steadily through the whole document instead of only jumping on
            # the handful of scanned pages.
            if progress_cb and (i % 20 == 0 or i == total_pages):
                progress_cb(i, total_pages, ocr_done, ocr_page_count, False)

    doc.close()

    meta["ocr_pages"] = ocr_done
    meta["ocr_available"] = ocr_enabled

    combined = "\n".join(pages_md)

    # For fully-scanned documents where pre-OCR text was empty, re-run content
    # detection on the OCR output — OCR may have revealed the document type
    if meta["content_type"] == "PDF Document" and meta.get("ocr_pages", 0) > 0:
        ocr_text = re.sub(r'<a id="[^"]+"></a>|### Page \d+[^\n]*', '', combined)
        re_detected = content_detector.detect_content_type(ocr_text)
        if re_detected != "generic":
            new_type = {
                "financial": "Financial Statement",
                "medical": "Medical Document",
                "legal": "Legal Document",
                "real_estate": "Real Estate Document",
                "insurance": "Insurance Document",
            }.get(re_detected)
            if new_type:
                meta["content_type"] = new_type
                content_type = new_type

    if content_type == "Medical Document":
        combined = _post_process_medical(combined)
        # Medical Value Index — the analog of the Tax Line Index. Index every
        # discrete clinical value (test/analyte/finding) with a page anchor, by
        # structure (not vendor). Withholds rather than guess on layouts it can't
        # parse confidently (narrative/graphical reports, reference books).
        from converters import medical_index
        try:
            doc_ref = fitz.open(file_path)
            medical_index.build_medical_value_index(doc_ref, meta, file_slug)
            doc_ref.close()
        except Exception:
            meta.setdefault("medical_value_index", [])
        medical_index.assess_medical_index_reliability(meta)

    # Document Index — built for EVERY document, not gated on a domain guess. It
    # indexes the auditable elements present in any text (parties, dates, amounts,
    # defined terms, numbered references/clauses/statutes/citations, section &
    # exhibit headings, case numbers), each traceable to a source page. This is the
    # universal audit fingerprint: format, not document-type, decides what is
    # extracted. Tabular forms (tax/medical) additionally get their row/column
    # index below; everything else relies on this one. Self-withholds on image-only
    # documents. (Module name `legal_index` is historical — it serves all types.)
    # Bates numbering — detect the per-page discovery stamps (e.g. "GRANT THORNTON
    # 0192"…"5333") and annotate each page so an AI can navigate to a Bates number
    # and a human can confirm it. Each file detects its OWN series (the Bates range
    # is tied to this file). Done before the index so it can carry a Bates entry.
    _stage("Detecting page labels")
    try:
        from converters import bates
        _binfo = bates.detect_bates(combined)
        if _binfo:
            combined = bates.annotate(combined, file_slug, _binfo)
            meta["bates"] = {
                "prefix": _binfo["prefix"], "start": _binfo["start_label"],
                "end": _binfo["end_label"], "count": _binfo["count"],
                "kind": _binfo["kind"], "label_term": _binfo["label_term"],
                "page_to_bates": _binfo["page_to_bates"],
            }
    except Exception:
        pass

    _stage("Building document index")
    from converters import legal_index
    try:
        legal_index.build_legal_index(combined, meta, file_slug)
    except Exception:
        meta.setdefault("legal_index", [])
        meta["legal_index_reliable"] = False
        meta.setdefault("legal_index_unreliable_reason", "Document index build failed.")

    # OCR confidence — the integrity signal for SCANNED pages, where numeric token
    # integrity (below) cannot apply (there is no digital text layer to compare).
    # Aggregated from the per-page Tesseract mean confidence already shown in each
    # OCR page heading, so the document carries a verifiable scanned-content quality
    # score and a count of pages that fell below the review threshold.
    _ocr_confs = [int(c) for c in re.findall(r'OCR — \d+ DPI · confidence (\d+)%', combined)]
    if _ocr_confs:
        # low_pages = pages MARKED for review, which now includes pages with a high
        # mean but a load-bearing word misread (the "review recommended" tag), not
        # just pages below the mean threshold — so the summary count matches the tags.
        meta["ocr_confidence"] = {
            "pages": len(_ocr_confs),
            "mean": round(sum(_ocr_confs) / len(_ocr_confs)),
            "min": min(_ocr_confs),
            "low_pages": combined.count("review recommended"),
        }

    # Source-data integrity: confirm every number in the digital source text
    # survived into the extracted Markdown (checked before masking). Surfaced to
    # the user so any extraction fault that changed a value is never silent.
    _stage("Verifying source integrity")
    from converters import integrity
    meta["integrity"] = integrity.check_numbers("\n".join(raw_source_pages), combined)

    # Build tax line index for Tax Form documents
    # Pass the fitz doc so the builder can do page-level form detection
    if meta["content_type"] == "Tax Form":
        _stage("Indexing tax lines")
        try:
            doc_ref = fitz.open(file_path)
            index = _build_tax_line_index(combined, meta, file_slug, doc_ref)
            # Grid-layout schedules (Schedule E now; D / 8949 in later phases) —
            # one (line × column) cell per value, appended to the main index.
            try:
                from converters import grid_forms
                index += grid_forms.extract(doc_ref, meta, file_slug)
                # Form 8949 per-transaction detail → appendix appended after Section 3
                # (masked downstream with the rest of the gold master).
                appendix = grid_forms.render_8949_appendix(meta.get("form_8949_pages", []))
                if appendix:
                    combined += "\n\n" + appendix
            except Exception:
                pass
            meta["tax_line_index"] = index
            doc_ref.close()
        except Exception:
            meta["tax_line_index"] = _build_tax_line_index(combined, meta, file_slug, None)
        # Safety net: AI Ready's value geometry is tuned to specific software
        # layouts. On an unsupported layout (e.g. an H&R Block preparer packet) the
        # line-to-value matching grabs wrong numbers that still LOOK like real tax
        # data. Sanity-check the extracted 1040 figures against form arithmetic;
        # if they don't reconcile, flag the index UNRELIABLE so the gold master
        # suppresses the figures instead of presenting confident wrong values.
        _assess_tax_index_reliability(meta)

    # Collapse byte-identical page copies (filing + records copies of tax forms) to
    # roughly halve Section 3 size. Runs AFTER the index is built (so extraction sees
    # the full text) and preserves every page anchor, so no index link breaks.
    _stage("Finalizing extraction")
    combined, meta["deduped_pages"] = _dedup_section3_pages(combined)

    return combined, meta


# Known tax line descriptions keyed by (form_hint, line_number)
# Official IRS line number descriptions — public record, not personal data.
# Values always come from the document; only descriptions are hardcoded here.
# Keyed as (form_hint, line_number) where form_hint matches meta["form"].lower()
_IRS_LINE_DESCRIPTIONS = {
    # ── Form 1040 / 1040-SR ──────────────────────────────────────────────────
    ("1040", "1a"):  "Total wages from Form(s) W-2",
    ("1040", "1b"):  "Household employee wages not reported on W-2",
    ("1040", "1c"):  "Tip income not reported on line 1a",
    ("1040", "1d"):  "Medicaid waiver payments",
    ("1040", "1e"):  "Taxable dependent care benefits from Form 2441",
    ("1040", "1f"):  "Employer-provided adoption benefits from Form 8839",
    ("1040", "1g"):  "Wages from Form 8919",
    ("1040", "1h"):  "Other earned income",
    ("1040", "1z"):  "Add lines 1a through 1h — total wages",
    ("1040", "2a"):  "Tax-exempt interest",
    ("1040", "2b"):  "Taxable interest",
    ("1040", "3a"):  "Qualified dividends",
    ("1040", "3b"):  "Ordinary dividends",
    ("1040", "4a"):  "IRA distributions",
    ("1040", "4b"):  "IRA distributions — taxable amount",
    ("1040", "5a"):  "Pensions and annuities",
    ("1040", "5b"):  "Pensions and annuities — taxable amount",
    ("1040", "6a"):  "Social security benefits",
    ("1040", "6b"):  "Social security benefits — taxable amount",
    ("1040", "7"):   "Capital gain or (loss)",
    ("1040", "7a"):  "Capital gain or (loss)",   # 2025 Form 1040 labels the value box 7a
    ("1040", "8"):   "Additional income from Schedule 1, Part I",
    ("1040", "9"):   "Total income",
    ("1040", "10"):  "Adjustments to income from Schedule 1, Part II",
    ("1040", "11"):  "Adjusted Gross Income (AGI)",
    ("1040", "11a"): "Adjusted Gross Income (AGI)",
    ("1040", "12"):  "Standard deduction or itemized deductions (Schedule A)",
    ("1040", "12a"): "Standard deduction or itemized deductions",
    ("1040", "12e"): "Standard deduction or itemized deductions",
    ("1040", "13"):  "Qualified business income deduction (Form 8995 or 8995-A)",
    ("1040", "13a"): "Qualified business income deduction",
    ("1040", "14"):  "Add lines 12 and 13",
    ("1040", "15"):  "Taxable income",
    ("1040", "16"):  "Tax",
    ("1040", "17"):  "Alternative Minimum Tax (AMT) from Form 6251",
    ("1040", "18"):  "Add lines 16 and 17",
    ("1040", "19"):  "Child tax credit or credit for other dependents",
    ("1040", "20"):  "Amount from Schedule 3, line 8",
    ("1040", "21"):  "Add lines 19 and 20",
    ("1040", "22"):  "Subtract line 21 from line 18",
    ("1040", "23"):  "Other taxes including self-employment tax (Schedule 2)",
    ("1040", "24"):  "Total tax",
    ("1040", "25a"): "Federal income tax withheld from Form(s) W-2",
    ("1040", "25b"): "Federal income tax withheld from Form(s) 1099",
    ("1040", "25c"): "Other federal income tax withheld",
    ("1040", "25d"): "Total federal income tax withheld",
    ("1040", "26"):  "Estimated tax payments and amount applied from prior year",
    ("1040", "27"):  "Earned income credit (EIC)",
    ("1040", "27a"): "Earned income credit (EIC)",
    ("1040", "28"):  "Additional child tax credit from Schedule 8812",
    ("1040", "29"):  "American opportunity credit from Form 8863",
    ("1040", "31"):  "Amount from Schedule 3, line 15",
    ("1040", "32"):  "Total other payments and refundable credits",
    ("1040", "33"):  "Total payments",
    ("1040", "34"):  "Amount overpaid",
    ("1040", "35a"): "Amount refunded to you",
    ("1040", "36"):  "Amount applied to next year's estimated tax",
    ("1040", "37"):  "Amount you owe",
    ("1040", "38"):  "Estimated tax penalty",
    # ── Form 1040-X (Amended Return) ─────────────────────────────────────────
    ("1040-x", "1"):   "Adjusted gross income",
    ("1040-x", "2"):   "Itemized deductions or standard deduction",
    ("1040-x", "3"):   "Subtract line 2 from line 1",
    ("1040-x", "4"):   "Exemptions (pre-2018) or qualified business income deduction",
    ("1040-x", "5"):   "Taxable income",
    ("1040-x", "6"):   "Tax",
    ("1040-x", "7"):   "Credits",
    ("1040-x", "8"):   "Subtract line 7 from line 6",
    ("1040-x", "9"):   "Other taxes",
    ("1040-x", "10"):  "Total tax",
    ("1040-x", "11"):  "Federal income tax withheld and excess Social Security tax",
    ("1040-x", "12"):  "Estimated tax payments and refundable credits",
    ("1040-x", "13"):  "Total payments",
    ("1040-x", "14"):  "Overpayment",
    ("1040-x", "15"):  "Amount paid with original return plus additional tax paid",
    ("1040-x", "16"):  "Total",
    ("1040-x", "17"):  "Overpayment",
    ("1040-x", "18"):  "Amount you owe",
    # ── Schedule K-1 (Form 1065 — Partnership) ───────────────────────────────
    ("k-1", "1"):   "Ordinary business income (loss)",
    ("k-1", "2"):   "Net rental real estate income (loss)",
    ("k-1", "3"):   "Other net rental income (loss)",
    ("k-1", "4"):   "Total guaranteed payments",
    ("k-1", "4a"):  "Guaranteed payments for services",
    ("k-1", "4b"):  "Guaranteed payments for capital",
    ("k-1", "4c"):  "Total guaranteed payments",
    ("k-1", "5"):   "Interest income",
    ("k-1", "6a"):  "Ordinary dividends",
    ("k-1", "6b"):  "Qualified dividends",
    ("k-1", "7"):   "Royalties",
    ("k-1", "8"):   "Net short-term capital gain (loss)",
    ("k-1", "9a"):  "Net long-term capital gain (loss)",
    ("k-1", "9b"):  "Collectibles (28%) gain (loss)",
    ("k-1", "9c"):  "Unrecaptured Section 1250 gain",
    ("k-1", "10"):  "Net Section 1231 gain (loss)",
    ("k-1", "11"):  "Other income (loss)",
    ("k-1", "12"):  "Section 179 deduction",
    ("k-1", "13"):  "Other deductions",
    ("k-1", "13r"): "Retirement plan contributions",
    ("k-1", "14"):  "Self-employment earnings (loss)",
    ("k-1", "14a"): "Self-employment income — net earnings",
    ("k-1", "14b"): "Self-employment income — gross farming/fishing",
    ("k-1", "14c"): "Self-employment income — gross non-farm",
    ("k-1", "15"):  "Credits",
    ("k-1", "17"):  "Alternative minimum tax (AMT) items",
    ("k-1", "18"):  "Tax-exempt income and nondeductible expenses",
    ("k-1", "19"):  "Distributions",
    ("k-1", "20"):  "Other information",
    ("k-1", "21"):  "Foreign taxes paid or accrued",
    # ── Form 1099-NEC ────────────────────────────────────────────────────────
    ("1099", "1"):  "Nonemployee compensation",
    ("1099", "2"):  "Payer made direct sales of $5,000 or more",
    ("1099", "4"):  "Federal income tax withheld",
    # ── Form 1099-MISC ───────────────────────────────────────────────────────
    ("1099-misc", "1"):  "Rents",
    ("1099-misc", "2"):  "Royalties",
    ("1099-misc", "3"):  "Other income",
    ("1099-misc", "4"):  "Federal income tax withheld",
    ("1099-misc", "6"):  "Medical and health care payments",
    ("1099-misc", "10"): "Gross proceeds paid to an attorney",
    # ── Form W-2 ─────────────────────────────────────────────────────────────
    ("w-2", "1"):   "Wages, tips, other compensation",
    ("w-2", "2"):   "Federal income tax withheld",
    ("w-2", "3"):   "Social security wages",
    ("w-2", "4"):   "Social security tax withheld",
    ("w-2", "5"):   "Medicare wages and tips",
    ("w-2", "6"):   "Medicare tax withheld",
    ("w-2", "12"):  "Deferred compensation and benefits (coded)",
    ("w-2", "16"):  "State wages, tips, etc.",
    ("w-2", "17"):  "State income tax",
    # ── Schedule 1 (Additional Income and Adjustments) ───────────────────────
    ("schedule 1", "1"):  "Taxable refunds of state/local income taxes",
    ("schedule 1", "3"):  "Business income (loss) from Schedule C",
    ("schedule 1", "4"):  "Other gains (losses) from Form 4797",
    ("schedule 1", "5"):  "Rental real estate / royalties / S-corps / partnerships",
    ("schedule 1", "7"):  "Unemployment compensation",
    ("schedule 1", "8z"): "Other income — nonemployee compensation (1099-NEC)",
    ("schedule 1", "9"):  "Total other income",
    ("schedule 1", "10"): "Total additional income",
    # ── Schedule D (Capital Gains and Losses) ────────────────────────────────
    ("schedule d", "1a"): "Short-term totals from 1099-B (basis reported to IRS)",
    ("schedule d", "1b"): "Short-term from Form 8949 — Box A",
    ("schedule d", "7"):  "Net short-term capital gain or (loss)",
    ("schedule d", "15"): "Net long-term capital gain or (loss)",
    ("schedule d", "16"): "Combined net capital gain or (loss)",
}


def _is_bare_year(value_raw: str) -> bool:
    """True only for a BARE 4-digit year (1900-2099) — no thousands comma and no
    decimal point. A stray "2025" is a year reference and should be skipped;
    "2,085." / "2,013." are real dollar amounts that merely look year-like and must
    NOT be dropped. This is the fix for line 19 ($2,085) being discarded as a year."""
    v = value_raw.strip()
    if ',' in v or '.' in v:
        return False
    return bool(re.match(r'^(19|20)\d{2}$', v.lstrip('-$(').rstrip(')')))


def _lookup_irs_desc(form_hint: str, line_num: str) -> str:
    """Look up official IRS line description. Checks specific form first."""
    fh = form_hint.lower()
    ln = line_num.lower().strip()
    # Direct match on the specific form wins
    desc = _IRS_LINE_DESCRIPTIONS.get((fh, ln), "")
    if desc:
        return desc
    # Fallback: check related forms in order of specificity
    # Do NOT cross-contaminate (e.g. K-1 line 4 ≠ 1040-X line 4)
    fallback_order = {
        "1040": ["1040-sr"],
        "1040-x": ["1040"],
        "k-1": [],           # K-1 lines are unique — no fallback
        "1099": ["1099-misc", "1099-nec"],
        "1099-misc": ["1099"],
        "1099-nec": ["1099"],
        "w-2": [],
    }
    for alias in fallback_order.get(fh, ["1040"]):
        desc = _IRS_LINE_DESCRIPTIONS.get((alias, ln), "")
        if desc:
            return desc
    return ""


def _assess_tax_index_reliability(meta: dict) -> None:
    """Sanity-check extracted 1040 line values against form arithmetic. Sets
    meta['tax_index_reliable'] (bool) and meta['tax_index_unreliable_reason'].

    Rationale: the value extractor assumes a label-left / value-right geometry
    that holds for some tax software but not others. Applied to a layout it
    wasn't built for, it returns wrong-but-plausible numbers. Form arithmetic is
    layout-independent ground truth — if the figures don't add up, the
    extraction is untrustworthy no matter how confident each value looks.
    """
    meta["tax_index_reliable"] = True
    meta["tax_index_unreliable_reason"] = ""
    if meta.get("form", "") != "1040":
        return  # identities below are 1040-specific; other forms not yet checked

    def _strict_num(s: str):
        s = (s or "").strip()
        if not s:
            return None
        neg = s.startswith("(") and s.endswith(")")
        s = re.sub(r"[^\d.]", "", s)
        if not s or s == ".":
            return None
        try:
            v = float(s)
            return -v if neg else v
        except ValueError:
            return None

    vals = {}
    for e in meta.get("tax_line_index", []) or []:
        if e.get("column"):
            continue  # single-column 1040 lines only (grid cells handled elsewhere)
        ln = (e.get("line", "") or "").replace("line", "").strip().lower()
        v = _strict_num(e.get("value", ""))
        if ln and v is not None:
            vals[ln] = v

    # Layout-independent Form 1040 identities (the family the validator uses,
    # plus AGI = total income − adjustments).
    identities = [
        ("18", ["16", "17"], "+"),
        ("21", ["19", "20"], "+"),
        ("24", ["22", "23"], "+"),
        ("11", ["9", "10"], "-"),
    ]
    testable = passed = 0
    for target, operands, op in identities:
        if target not in vals or any(o not in vals for o in operands):
            continue
        testable += 1
        computed = (sum(vals[o] for o in operands) if op == "+"
                    else vals[operands[0]] - sum(vals[o] for o in operands[1:]))
        if abs(computed - vals[target]) < 1.0:
            passed += 1

    # Need ≥2 testable identities to judge; unreliable if most fail.
    if testable >= 2 and passed / testable < 0.6:
        meta["tax_index_reliable"] = False
        meta["tax_index_unreliable_reason"] = (
            f"Only {passed} of {testable} Form 1040 arithmetic checks reconciled — "
            f"the extracted line values do not add up, indicating this PDF's layout "
            f"is not one AI Ready can reliably parse (e.g. an H&R Block preparer "
            f"packet). Figures have been withheld to avoid presenting wrong values."
        )


def _build_tax_line_index(combined_md: str, meta: dict, file_slug: str,
                          doc=None) -> list[dict]:
    """
    High-integrity tax line index builder.

    Two-tier approach:
    TIER 1 (always included): Lines with known IRS descriptions from
    _IRS_LINE_DESCRIPTIONS. These are the authoritative answer to
    'what is line N on this form?' — no noise possible.

    TIER 2 (conditionally included): Lines detected in the text without
    a known IRS description. Requires the line to be from a page identified
    as a primary form page (not a worksheet, instruction, or state form page)
    AND the value must be a substantial dollar amount.

    Page-level form detection: when a fitz Document is available, each page
    is classified by reading its header/footer. Only primary form pages
    contribute to Tier 2 extraction.
    """
    tax_year = meta.get("tax_year") or meta.get("tax_period") or ""
    form_id = meta.get("form", "")
    entries = []
    seen_lines = set()

    # ── Page classification ──────────────────────────────────────────────────
    # Identify which pages in the document are primary form pages vs
    # worksheets, state forms, instructions, cover sheets
    _PRIMARY_FORM_RE = re.compile(
        r'Form\s+(1040(?:-X|-SR|-NR)?|1099-\w+|W-2|'
        r'Schedule\s+[A-F]|Sch\.\s*[A-F]|K-1\s*\(\d+\)|'
        r'8949|6251|2441|8995|5713)\s*(?:\(Rev\.?|20\d{2})?',
        re.IGNORECASE
    )
    _SKIP_PAGE_RE = re.compile(
        r'worksheet|filing\s+instructions|'
        r'cover\s+sheet|client\s+information|'
        r'payment\s+voucher|1040-ES|turbotax',
        re.IGNORECASE
    )

    # "Schedule X (Form 1040)" — allow intervening words. H&R Block prints the
    # schedule name and "(Form 1040)" separated by an OMB number and subtitle,
    # e.g. "SCHEDULE SE  OMB No. 1545-0074  Self-Employment Tax  (Form 1040)".
    _SCHEDULE_ATTACHMENT_RE = re.compile(
        r'Schedule\s+[A-Z1-9]+\b[\s\S]{0,80}?\(Form\s+(?:1040|1065|1120)\)',
        re.IGNORECASE
    )

    # A page headed by "SCHEDULE X" is an attachment, not the main 1040 — the
    # 1040 main pages open with "U.S. Individual Income Tax Return" / "Form 1040",
    # never "SCHEDULE". Checked against the page's first ~200 chars.
    _SCHEDULE_HEADER_RE = re.compile(
        r'\bSCHEDULE\s+(?:[A-Z]{1,3}|\d{1,2})\b', re.IGNORECASE)

    # "Attachment Sequence No." — allow words in between ("Attachment / Internal
    # Revenue Service / Sequence No. 17" in the H&R Block layout).
    _ATTACHMENT_SEQ_RE = re.compile(
        r'Attachment\b[\s\S]{0,60}?Sequence\s+No', re.IGNORECASE)

    # OMB No. 1545-0074 is unique to Form 1040 (not its schedules).
    # Form 1040 main pages also have "Form 1040" within their text (may need wider scan).
    _FORM1040_OMB_RE = re.compile(r'1545-0074', re.IGNORECASE)

    def _is_primary_form_page(page_text: str, form_id: str) -> bool:
        """
        True if this page is the main form (not a schedule attachment or other form).
        For Form 1040: detect via OMB 1545-0074 (unique to Form 1040 vs schedules)
          OR via "Form 1040 (YEAR)" appearing explicitly.
        Schedule attachment pages always have "Attachment Sequence No." — excluded.
        For other forms: use regex matching.
        """
        if _SKIP_PAGE_RE.search(page_text):
            return False
        if _SCHEDULE_ATTACHMENT_RE.search(page_text):
            return False
        if form_id == "1040":
            # Precise identification: the two main 1040 pages carry titles that NO
            # schedule or attached form does — page 1 "U.S. Individual Income Tax
            # Return", page 2 the "Form 1040 (YEAR)" footer. OMB 1545-0074 alone is
            # NOT sufficient: Schedule SE, Form 8959, 8995, etc. all share it, and
            # in a preparer packet they reuse 1040 line numbers (4a, 9, 10, …),
            # contaminating the index. Title-matching excludes them cleanly.
            has_1040_p1 = bool(re.search(
                r'U\.?\s*S\.?\s+Individual\s+Income\s+Tax\s+Return', page_text, re.IGNORECASE))
            has_1040_p2 = bool(re.search(r'Form\s+1040\s*\(20\d{2}\)', page_text))
            if not (has_1040_p1 or has_1040_p2):
                return False
            # Even with a 1040 title, a page that also declares itself a schedule
            # or carries an attachment-sequence number is an attachment, not main.
            if _SCHEDULE_HEADER_RE.search(page_text[:200]):
                return False
            if _ATTACHMENT_SEQ_RE.search(page_text):
                return False
            return True
        if form_id in ("1040-X", "1040-x"):
            # 1040-X (amended) — page must contain "1040-X" (may appear in URL/footer)
            # and be a Treasury form. Use wider 1200-char scan for the 1040-X search.
            has_attachment_seq = bool(_ATTACHMENT_SEQ_RE.search(page_text))
            if has_attachment_seq:
                return False
            # Expand search for 1040-X since it may appear after first 800 chars
            page_text_wide = page_text  # already passed in; caller may pass more
            has_1040x = bool(re.search(r'1040-X', page_text_wide, re.IGNORECASE))
            has_treasury = bool(re.search(r'Department\s+of\s+the\s+Treasury', page_text_wide))
            return has_1040x and has_treasury
        if form_id.upper() == "K-1":
            return bool(re.search(r'Schedule\s+K-1\b|K-1\s*\(', page_text, re.IGNORECASE))
        return bool(_PRIMARY_FORM_RE.search(page_text))

    primary_page_nums: set[int] = set()
    # Use wider scan for 1040-X — its form number may appear later in page text
    _pg_scan_len = 1200 if form_id in ("1040-X", "1040-x") else 800
    if doc is not None:
        for i, page in enumerate(doc, start=1):
            page_text = page.get_text()[:_pg_scan_len]
            if _is_primary_form_page(page_text, form_id):
                primary_page_nums.add(i)
    all_primary = (doc is None or len(primary_page_nums) == 0)

    def _get_anchor(pos: int) -> tuple[str, int]:
        """Return (markdown link, page_num) for the nearest anchor before pos."""
        before = combined_md[:pos]
        anchors = list(re.finditer(r'<a id="([^"]+)"></a>', before))
        if anchors:
            aid = anchors[-1].group(1)
            pg = re.search(r'page-(\d+)', aid)
            if pg:
                pn = int(pg.group(1))
                return f"[p.{pn}](#{aid})", pn
        return "", 0

    def _clean_val(s: str) -> float:
        return float(s.replace(',', '').replace('$', '')
                      .replace('(', '-').replace(')', '').strip())

    _FORM_NUMBERS = {
        '1040', '1041', '1065', '1099', '1116', '1120', '1125',
        '2106', '2439', '2441', '3800', '4136', '4562', '4797',
        '4972', '5329', '6251', '8082', '8582', '8606', '8801',
        '8812', '8814', '8824', '8863', '8880', '8888', '8949',
        '8960', '8962', '8986', '8990', '8991', '8992', '8993',
        '8994', '8995', '9465',
    }
    _NOISE_DESC = re.compile(
        r'see\s+instructions|attach\s|enter\s+here|if\s+any|check\s+here|'
        r'do\s+not\s+include|from\s+line\s+\d|go\s+to\s+line|'
        r'exp\s+\d|put\s+[a-z]+\$|call\s+[a-z]+\$|'
        r'~~~+|}}+|\{\{+|\.{4,}|^\s*-{3,}',
        re.IGNORECASE
    )

    def _add(line_num: str, desc: str, value_raw: str, pos: int,
             tier: int = 2, min_val: float = 10.0, tier1_min: float = 100.0):
        """
        tier=1: known IRS line — include if value is from a primary form page.
        tier=2: unknown line — only include if on a primary form page
                AND value is substantial AND description is clean.
        Both tiers require the value to originate from a primary form page
        when primary pages are known (all_primary=False).

        tier1_min: minimum |value| for a tier-1 (known) line. Defaults to $100 for
        the text/markdown patterns (which can mis-read small line-number cross-
        references like "...from line 18" as a value). The coordinate matcher
        (Pattern G), which is column-aligned and reliable, passes tier1_min=1 so
        legitimately small whole-dollar amounts (e.g. line 20 = $28) are captured.
        """
        ln = line_num.strip().lower()
        if not ln or ln in seen_lines:
            return

        # Restrict to primary form pages for ALL tiers when pages are known
        if not all_primary:
            _, page_num = _get_anchor(pos)
            if page_num not in primary_page_nums:
                return

        # Validate value
        try:
            val = _clean_val(value_raw)
        except ValueError:
            return
        stripped = value_raw.replace(',', '').replace('.', '').strip()

        # Form number and year checks apply to ALL tiers
        if _is_bare_year(value_raw):
            return
        if stripped.rstrip(').,') in _FORM_NUMBERS:
            return
        if re.match(r'^0\d+$', stripped):
            return

        if tier == 1:
            # Tier 1: known IRS line. Floor is tier1_min (default $100 for noisy
            # text patterns; the coordinate matcher passes $1 to capture small
            # whole-dollar amounts). $0/blank lines fall out at the low floor too.
            if abs(val) < tier1_min:
                return
        else:
            # Tier 2: unknown line — requires page context + clean description.
            if abs(val) < min_val:
                return
            if _is_bare_year(value_raw):
                return
            if stripped in _FORM_NUMBERS:
                return
            if re.match(r'^0\d+$', stripped):
                return
            # Must be on a primary form page (page-level filtering for unknown lines)
            _, page_num = _get_anchor(pos)
            if not all_primary and page_num not in primary_page_nums:
                return
            # Description must not be noise
            if _NOISE_DESC.search(desc) or len(desc.strip()) < 4:
                return

        # Validate line number range (IRS forms: 1–120)
        try:
            n = int(re.sub(r'[a-z]', '', ln))
            if n < 1 or n > 120:
                return
        except ValueError:
            pass

        irs_desc = _lookup_irs_desc(form_id, ln)
        clean_desc = irs_desc or desc.strip().rstrip('.,;')
        if not irs_desc and _NOISE_DESC.search(clean_desc):
            clean_desc = ""

        seen_lines.add(ln)
        anchor, _ = _get_anchor(pos)
        entries.append({
            "line": f"line {ln}",
            "description": clean_desc,
            "value": value_raw.strip(),
            "page": anchor,
            "tax_year": tax_year,
            "form": form_id,
            "tier": tier,
        })

    # Build reverse lookup: IRS description → (form, line_num)
    # Used for description-based matching (e.g. TurboTax summary tables)
    _DESC_TO_LINE: dict[str, tuple[str, str]] = {}
    for (fh, ln), desc in _IRS_LINE_DESCRIPTIONS.items():
        key = desc.lower().strip()
        if fh == form_id.lower() or form_id == "":
            _DESC_TO_LINE[key] = (fh, ln)
    # Also index partial matches for common descriptions
    _KNOWN_DESC_PATTERNS = [
        # ── Form 1040 income lines ──────────────────────────────────────────
        (re.compile(r'total\s+wages?\b(?!\s+and\s+tips\s+reported)', re.IGNORECASE), ("1040", "1z")),
        (re.compile(r'wages?,?\s+tips?,?\s+other\s+compensation', re.IGNORECASE), ("1040", "1a")),
        (re.compile(r'(?:taxable\s+)?interest\s+income\b', re.IGNORECASE), ("1040", "2b")),
        (re.compile(r'tax.exempt\s+interest', re.IGNORECASE), ("1040", "2a")),
        (re.compile(r'ordinary\s+dividends\b', re.IGNORECASE), ("1040", "3b")),
        (re.compile(r'qualified\s+dividends\b', re.IGNORECASE), ("1040", "3a")),
        (re.compile(r'(?:ira|pension|annuity)\s+(?:distributions?|taxable)', re.IGNORECASE), ("1040", "4b")),
        (re.compile(r'pensions?\s+and\s+annuities', re.IGNORECASE), ("1040", "5b")),
        (re.compile(r'social\s+security\s+(?:benefits?|taxable)', re.IGNORECASE), ("1040", "6b")),
        (re.compile(r'capital\s+gain\s+or\s+\(?loss\)?', re.IGNORECASE), ("1040", "7")),
        (re.compile(r'(?:additional|other)\s+income\b', re.IGNORECASE), ("1040", "8")),
        (re.compile(r'total\s+income\b', re.IGNORECASE), ("1040", "9")),
        (re.compile(r'adjustments?\s+to\s+income', re.IGNORECASE), ("1040", "10")),
        (re.compile(r'adjusted\s+gross\s+income', re.IGNORECASE), ("1040", "11")),
        # ── Form 1040 deductions/tax lines ──────────────────────────────────
        (re.compile(r'standard\s+(?:deduction|or\s+itemized)', re.IGNORECASE), ("1040", "12")),
        (re.compile(r'qualified\s+business\s+income\s+deduction', re.IGNORECASE), ("1040", "13")),
        (re.compile(r'taxable\s+income\b', re.IGNORECASE), ("1040", "15")),
        (re.compile(r'\btax\b(?!\s+withheld)(?!\s+payments)', re.IGNORECASE), ("1040", "16")),
        (re.compile(r'alternative\s+minimum\s+tax', re.IGNORECASE), ("1040", "17")),
        (re.compile(r'child\s+(?:tax\s+)?credit', re.IGNORECASE), ("1040", "19")),
        (re.compile(r'total\s+tax\b', re.IGNORECASE), ("1040", "24")),
        # ── Form 1040 payments/refund lines ─────────────────────────────────
        (re.compile(r'(?:w.2\s+)?federal\s+(?:income\s+)?tax\s+withheld\s+from\s+(?:form|w)', re.IGNORECASE), ("1040", "25a")),
        (re.compile(r'1099\s+federal\s+(?:income\s+)?tax\s+withheld', re.IGNORECASE), ("1040", "25b")),
        (re.compile(r'total\s+(?:federal\s+)?(?:income\s+)?tax\s+withheld', re.IGNORECASE), ("1040", "25d")),
        (re.compile(r'estimated\s+tax\s+payments', re.IGNORECASE), ("1040", "26")),
        (re.compile(r'earned\s+income\s+credit', re.IGNORECASE), ("1040", "27")),
        (re.compile(r'additional\s+child\s+tax\s+credit', re.IGNORECASE), ("1040", "28")),
        (re.compile(r'total\s+(?:other\s+)?payments', re.IGNORECASE), ("1040", "33")),
        (re.compile(r'amount\s+(?:you\s+)?(?:refunded|refund)', re.IGNORECASE), ("1040", "35a")),
        (re.compile(r'amount\s+applied\s+to\s+next\s+year', re.IGNORECASE), ("1040", "36")),
        (re.compile(r'amount\s+(?:you\s+)?owe\b', re.IGNORECASE), ("1040", "37")),
        # ── Schedule K-1 (partnership) ───────────────────────────────────────
        (re.compile(r'ordinary\s+(?:business\s+)?income\s*(?:\(loss\))?', re.IGNORECASE), ("k-1", "1")),
        (re.compile(r'net\s+rental\s+real\s+estate', re.IGNORECASE), ("k-1", "2")),
        (re.compile(r'guaranteed\s+payments?\s+for\s+services', re.IGNORECASE), ("k-1", "4a")),
        (re.compile(r'guaranteed\s+payments?\s+for\s+capital', re.IGNORECASE), ("k-1", "4b")),
        (re.compile(r'total\s+guaranteed\s+payments', re.IGNORECASE), ("k-1", "4c")),
        (re.compile(r'(?:k-1\s+)?interest\s+income\b', re.IGNORECASE), ("k-1", "5")),
        (re.compile(r'royalt(?:y|ies)\b', re.IGNORECASE), ("k-1", "7")),
        (re.compile(r'net\s+short.term\s+capital\s+gain', re.IGNORECASE), ("k-1", "8")),
        (re.compile(r'net\s+long.term\s+capital\s+gain', re.IGNORECASE), ("k-1", "9a")),
        (re.compile(r'(?:net\s+)?section\s+1231\s+gain', re.IGNORECASE), ("k-1", "10")),
        (re.compile(r'section\s+179\s+deduction', re.IGNORECASE), ("k-1", "12")),
        (re.compile(r'retirement\s+plan\s+contributions?', re.IGNORECASE), ("k-1", "13r")),
        (re.compile(r'self.?employment\s+(?:income|earnings)', re.IGNORECASE), ("k-1", "14")),
        (re.compile(r'distributions\b', re.IGNORECASE), ("k-1", "19")),
        # ── Form 1099 ────────────────────────────────────────────────────────
        (re.compile(r'non.?employee\s+compensation', re.IGNORECASE), ("1099", "1")),
        (re.compile(r'federal\s+income\s+tax\s+withheld.*1099', re.IGNORECASE), ("1099", "4")),
        # ── Form W-2 ─────────────────────────────────────────────────────────
        (re.compile(r'wages,?\s+tips,?\s+other\s+compensation\s+\(box\s+1\)', re.IGNORECASE), ("w-2", "1")),
        (re.compile(r'social\s+security\s+wages', re.IGNORECASE), ("w-2", "3")),
        (re.compile(r'medicare\s+wages', re.IGNORECASE), ("w-2", "5")),
    ]

    # ── Tier 1: Known IRS lines — scan entire document ───────────────────────
    # These are authoritative. If we find line 11 = $X on a 1040, that IS the AGI.
    all_irs_lines = {ln for (_, ln) in _IRS_LINE_DESCRIPTIONS if
                     _lookup_irs_desc(form_id, ln)}

    # Pattern A: | Line N | value |
    for m in re.finditer(
        r'^\|\s*(line\s+(\d+[a-z]?))\s*\|\s*([-\$\(]?[\d,]+\.?\d*[\)]?)\s*\|',
        combined_md, re.IGNORECASE | re.MULTILINE
    ):
        ln = m.group(2).strip().lower()
        tier = 1 if ln in all_irs_lines else 2
        _add(ln, m.group(1), m.group(3), m.start(), tier=tier)

    # Pattern B: #### Line N, Description: value  (K-1 section headers)
    for m in re.finditer(
        r'^####\s+line\s+(\d+[a-z]?),?\s*([^:\n]{3,60}):\s*([-\$\(]?[\d,]+\.?\d*[\)]?)\s*$',
        combined_md, re.IGNORECASE | re.MULTILINE
    ):
        ln = m.group(1).strip().lower()
        tier = 1 if ln in all_irs_lines else 2
        _add(ln, m.group(2), m.group(3), m.start(), tier=tier)

    # Pattern C: **N description**: value  (coordinate-aware bold pairs)
    for m in re.finditer(
        r'\*\*(\d+[a-z]?)\s+([^*\n]{3,60}?)\*\*:\s*([-\$\(]?[\d,]+\.?\d*)',
        combined_md, re.IGNORECASE
    ):
        ln = m.group(1).strip().lower()
        tier = 1 if ln in all_irs_lines else 2
        _add(ln, m.group(2), m.group(3), m.start(), tier=tier,
             min_val=100.0 if tier == 2 else 0.0)

    # Pattern D: AcroForm | field_with_line_ref | value |
    for m in re.finditer(
        r'^\|\s*([^|]{2,80}?)\s*\|\s*([-\$\(]?[\d,]+\.?\d*[\)]?)\s*\|',
        combined_md, re.MULTILINE
    ):
        field = m.group(1).strip()
        val = m.group(2).strip()
        # Require "line N" at the START of the field — prevents matching cross-references
        # like "New York AGI from line 32" where 32 belongs to a different form.
        ln_m = re.match(r'(?:line|ln)\s*_?\s*(\d+[a-z]?)', field.lstrip(), re.IGNORECASE)
        if ln_m:
            ln = ln_m.group(1).strip().lower()
            tier = 1 if ln in all_irs_lines else 2
            _add(ln, field, val, m.start(), tier=tier)

    # ── Pattern E: Multi-column tax form table rows (primary pages only) ─────
    # Form 1040 pages extract as: | 20 | Description text | 20 | 690. |
    # The line number appears as a bare integer in the first cell,
    # value in the last cell. BOTH are literally in the source.
    # Restricted to primary form pages to prevent cross-form contamination.
    for m in re.finditer(
        r'^\|\s*(\d{1,3}[a-z]?)\s*\|([^|]{3,120})\|.*?\|\s*([-\$\(]?(?:\d{1,3}(?:,\d{3})+|\d{2,8}\.)\.?\d*[\)]?)\s*\|',
        combined_md, re.IGNORECASE | re.MULTILINE
    ):
        ln = m.group(1).strip().lower()
        desc_raw = m.group(2).strip().rstrip('~}. ')
        value_raw = m.group(3).strip()
        if ln in seen_lines:
            continue
        try:
            n = int(re.sub(r'[a-z]', '', ln))
            if n < 1 or n > 120:
                continue
        except ValueError:
            continue
        # Only accept from primary form pages — prevents state/worksheet contamination
        _, page_num = _get_anchor(m.start())
        if not all_primary and page_num not in primary_page_nums:
            continue
        irs_desc = _lookup_irs_desc(form_id, ln)
        display_desc = irs_desc if irs_desc else re.sub(r'[~}]{2,}.*$', '', desc_raw).strip()
        # Use tier=1 when we have a known IRS line description — bypasses noise filter
        entry_tier = 1 if irs_desc else 2
        _add(ln, display_desc, value_raw, m.start(), tier=entry_tier, min_val=100.0)

    # ── Pattern G: Coordinate-based extraction from primary form pages ───────
    # Uses PyMuPDF word-level bounding boxes to match line number labels with
    # their adjacent dollar values by spatial proximity.
    #
    # IRS Form 1040 (TurboTax PDF) layout observed from coordinate analysis:
    #   - Main line numbers appear at two X positions per row:
    #       LEFT MARGIN  (~x 85–115): the primary label
    #       RIGHT COLUMN (~x 460–490): the repeated label next to value box
    #   - Dollar values appear at x > 490 (right value column)
    #       OR at x ~280–350 (middle column for "a" sub-line amounts)
    #   - The value Y position is ~4–8 points ABOVE the label Y position
    #     (value box renders slightly above the text label)
    #   - Matching strategy: for each right-column label, find the closest
    #     dollar value within ±10 Y points and to the right of that label.
    #
    # This approach works for any IRS form with a two-column layout
    # (label | value) — not limited to Form 1040.
    _VAL_RE = re.compile(
        r'^[-\$\(]?\d{1,3}(?:,\d{3})*\.?\d*[\)]?$'
    )

    # Pre-compute markdown anchor positions for each page. Store the position just
    # PAST the full `<a id="..."></a>` element so that _get_anchor() — which
    # resolves a position to its page by finding the nearest COMPLETE anchor
    # before it — lands inside this page. Matching the whole element (incl.
    # `</a>`) and using .end() is length-independent: the previous code stored
    # .start() and the caller added a fixed +50, which fell SHORT of the closing
    # tag once real file slugs (e.g. "src01-tax-period-2023-irs-and-page-026",
    # ~51 chars) made the anchor longer than 50 chars — silently suppressing all
    # coordinate (Pattern G) extraction in production while short test slugs ('x')
    # masked it.
    _anchor_positions: dict[int, int] = {}
    for _a in re.finditer(r'<a id="[^"]*page-(\d+)[^"]*"></a>', combined_md):
        _pg = int(_a.group(1))
        if _pg not in _anchor_positions:
            _anchor_positions[_pg] = _a.end()

    if doc is not None:
        for page_num in sorted(primary_page_nums)[:30]:
            try:
                page = doc[page_num - 1]
            except IndexError:
                continue
            anchor_id = f"{file_slug}-page-{page_num:03d}" if file_slug else f"page-{page_num:03d}"
            anchor_link = f"[p.{page_num}](#{anchor_id})"
            # Position just AFTER this page's complete anchor element, so
            # _get_anchor() resolves base_pos back to this page (see note above).
            anchor_pos = _anchor_positions.get(page_num, 0)

            try:
                words = page.get_text("words")  # (x0,y0,x1,y1,word,block,line,word_no)
            except Exception:
                continue

            pw = page.rect.width

            # Collect candidate line labels and dollar values
            labels = []   # (xc, yc, text)
            values = []   # (xc, yc, text, float_val)

            for w in words:
                x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
                xc = (x0 + x1) / 2
                yc = (y0 + y1) / 2
                word = word.strip().rstrip('.')

                # Line number labels: accepted from two zones:
                #   LEFT MARGIN  (xc < 120): primary label column — any integer 1–120
                #   RIGHT COLUMN (xc > 460): sub-line labels with letter suffix only
                #     (e.g. "2b", "3a", "25d") — pure integers here could be
                #     spurious references inside description text.
                is_left_margin = xc < 120
                is_right_sublabel = xc > 460 and re.search(r'[a-z]', word, re.IGNORECASE)
                if (is_left_margin or is_right_sublabel) and re.match(r'^\d{1,3}[a-z]?$', word, re.IGNORECASE):
                    n_str = re.sub(r'[a-z]', '', word, flags=re.IGNORECASE)
                    try:
                        n = int(n_str)
                        if 1 <= n <= 120:
                            labels.append((xc, yc, word.lower()))
                    except ValueError:
                        pass

                # Dollar value: number with commas or decimal point
                # Skip values in the page header zone (top 5% of page height).
                # IRS form pages carry summary values in the header that would
                # be incorrectly matched to the first line's label. H&R Block page 2
                # starts its line grid high (line 16's value at ~6% height), so an
                # 8% cut dropped it — 5% clears the title row without losing line 16.
                if yc < page.rect.height * 0.05:
                    continue
                raw = word + ('.' if w[4].strip().endswith('.') else '')
                # Treat a number as a currency VALUE only if it is formatted like
                # money on the form: it has a thousands comma OR ends with a decimal
                # point (e.g. "23,334." or "28."). Bare integers ("9", "20") are
                # line-number labels, not values — this guard keeps them out once the
                # small-amount floor is lowered below.
                looks_money = (',' in raw) or w[4].strip().endswith('.')
                if _VAL_RE.match(raw) and looks_money:
                    try:
                        fval = float(
                            raw.replace(',', '').replace('$', '')
                               .replace('(', '-').replace(')', '').strip()
                        )
                        # Capture every whole-dollar amount (>= $1); skip cents-only.
                        if abs(fval) >= 1:
                            # Normalize: strip leading $ sign for clean display
                            raw_clean = raw.lstrip('$')
                            # Carry the right edge (x1) for column-alignment filtering.
                            values.append((xc, yc, raw_clean, fval, x1))
                    except ValueError:
                        pass

            # ── Keep only values that sit in a real value COLUMN ─────────────
            # Form value boxes are right-aligned into columns (the main right
            # column, and a middle column for "a"-amounts like 2a/3a/4a). Digits
            # embedded in description text — e.g. line cross-references like
            # "Add lines 1z, 2b, ... and 8." — are NOT column-aligned. Keep a value
            # only if it is in the far-right main column OR its right edge aligns
            # with >=2 other values (i.e. it forms a genuine column). This is what
            # stops a stray "8." or "9." in a sentence from being read as a value.
            if values:
                _edge_counts: dict[int, int] = {}
                for _v in values:
                    _b = round(_v[4] / 4)
                    _edge_counts[_b] = _edge_counts.get(_b, 0) + 1
                # Keep a value if it's in the far-right MAIN column (x% > 85), or it
                # forms a right-aligned column (>=2 share an edge) sitting in the
                # value region (x% > 45). The x%>45 floor rejects left-margin /
                # sidebar numbers (e.g. the std-deduction amounts, or stray "18."
                # "24." cross-refs) that happen to share a right edge.
                values = [
                    _v for _v in values
                    if (_v[0] / pw) > 0.85
                    or (_edge_counts[round(_v[4] / 4)] >= 2 and (_v[0] / pw) > 0.45)
                ]

            # Column-aware matching: a value box's line is identified by the nearest
            # KNOWN label to its LEFT on the same row. Horizontal nearest-left (not
            # vertical distance alone) is what keeps two-value rows correct. On
            # Form 1040, line 2 prints on ONE row as:
            #     "2a Tax-exempt interest [23,334.]   b Taxable interest [61,886.]"
            # so 2a must bind the middle value (23,334) and 2b the right value
            # (61,886). Y-only matching let 2a grab the right column's 61,886 and
            # dropped 23,334 entirely — the bug this replaces.
            base_pos = anchor_pos
            Y_TOL = 10   # points; value box renders on the same row as its label

            # Step 1: each value -> its owner label = nearest known label to the LEFT
            # on the same row.
            value_owner: dict[int, int] = {}
            for vi, (vx, vy, vtext, vfloat, _vx1) in enumerate(values):
                stripped = vtext.replace(',', '').replace('.', '').strip()
                if stripped in _FORM_NUMBERS or _is_bare_year(vtext):
                    continue
                best_li, best_key = None, None
                for li, (lx, ly, ln) in enumerate(labels):
                    if lx >= vx:                 # label must sit to the LEFT of the value
                        continue
                    if abs(vy - ly) > Y_TOL:      # ...and on the same row
                        continue
                    if not _lookup_irs_desc(form_id, ln):
                        continue
                    # NOTE: do NOT skip labels already in seen_lines here. A value
                    # must bind to its TRUE nearest label; if an earlier pattern
                    # already captured that line, _add() dedups and drops this
                    # redundant value. Skipping seen labels during selection made
                    # the value leak onto an adjacent unclaimed label (e.g. line 18
                    # captured by the markdown pattern, then line 18's value rebinding
                    # to the empty line 17 above it) — a phantom duplicate.
                    dx = vx - lx
                    # Owner selection key: PRIMARY = which ROW the value sits in
                    # (vertical proximity, banded); SECONDARY = nearest label to the
                    # LEFT within that row (horizontal). Row-first is the correct
                    # order because every column on the form repeats labels, so only
                    # the row uniquely identifies a line:
                    #  - Tight 12pt layouts (CPA software): a value sits ~2pt above
                    #    its own label, putting the row-ABOVE label ~9.6pt away —
                    #    inside Y_TOL. Vertical-primary picks the value's true row
                    #    instead of leaking one row up.
                    #  - Right-column sublabels (25d, 35a at x~472) sit horizontally
                    #    CLOSE to the value column, so a horizontal-primary key would
                    #    wrongly bind line 26's value to the 25d label one row up.
                    #    Vertical-primary keeps it on line 26.
                    #  - Two-value rows (2a [mid] + 2b [right] on one line) tie on
                    #    the vertical band, so horizontal nearest-left still splits
                    #    them correctly.
                    # Band = 5pt: groups a real row (own-label dy<=~3, co-labels
                    # dy~0) into one band while separating adjacent 12pt rows.
                    key = (round(abs(vy - ly) / 5.0), dx)
                    if best_key is None or key < best_key:
                        best_key, best_li = key, li
                if best_li is not None:
                    value_owner[vi] = best_li

            # Step 2: each label keeps only its single nearest value to the right; a
            # value displaced by a closer one for the same label is left unassigned.
            label_best: dict[int, tuple[int, float]] = {}
            for vi, li in value_owner.items():
                dx = values[vi][0] - labels[li][0]
                if li not in label_best or dx < label_best[li][1]:
                    label_best[li] = (vi, dx)

            for li, (vi, _dx) in label_best.items():
                ln = labels[li][2]
                val_raw = values[vi][2]
                _add(ln, _lookup_irs_desc(form_id, ln), val_raw, base_pos,
                     tier=1, tier1_min=1)

    # Sort: Tier 1 (known IRS lines) first, then by line number
    def _sort_key(e):
        n = re.search(r'(\d+)', e.get("line", ""))
        return (e.get("tier", 2), int(n.group(1)) if n else 999)
    entries.sort(key=_sort_key)

    # Remove tier from output (internal use only)
    for e in entries:
        e.pop("tier", None)

    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Structured Summary layer
# ─────────────────────────────────────────────────────────────────────────────

_IRS_TRANSCRIPT_RE = re.compile(r"Wage and Income Transcript", re.IGNORECASE)

_FORM_SPLIT_RE = re.compile(
    r"(?="
    r"Form W-2 Wage and Tax Statement"
    r"|1099-B Proceeds from Broker"
    r"|1099-R Distributions from Pensions"
    r"|5498 Individual Retirement"
    r"|1099-NEC Non.?employee Compensation"
    r"|1099-DIV Dividends and Distributions"
    r"|1099-INT Interest Income"
    r")",
    re.IGNORECASE,
)

_SUMMARY_DISCLAIMER = (
    "> **IMPORTANT — DERIVED CONTENT:** This structured summary is computed by AI Ready "
    "using pattern matching and arithmetic on the extracted source text. "
    "It is **not** a substitute for official IRS transcripts, brokerage 1099s, "
    "filed tax returns, or any other official document. "
    "Verify all totals and figures against the original source documents before use."
)


def _parse_kv(text: str) -> dict:
    result: dict[str, str] = {}
    lines = [l.strip() for l in text.splitlines()]
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.endswith(":") and 3 <= len(line) <= 120:
            key = line[:-1].strip()
            j = i + 1
            while j < len(lines) and not lines[j]:
                j += 1
            if j < len(lines) and not lines[j].endswith(":") and lines[j]:
                result[key] = lines[j]
                i = j + 1
                continue
        i += 1
    return result


def _dollars(val: str) -> float:
    if not val:
        return 0.0
    try:
        return float(re.sub(r"[^\d.-]", "", val.replace(",", "")) or "0")
    except Exception:
        return 0.0


def _fmt(val: float) -> str:
    if val < 0:
        return f"$({abs(val):,.2f})"
    return f"${val:,.2f}"


def _kv_fuzzy(kv: dict, *fragments: str) -> str:
    for frag in fragments:
        # Exact key match wins over substring match (prevents "Proceeds reported to IRS"
        # from shadowing "Proceeds" in IRS transcript 1099-B blocks)
        if frag in kv:
            return kv[frag]
        for k, v in kv.items():
            if frag.lower() in k.lower():
                return v
    return ""


def _first_after(block: str, header_frag: str) -> str:
    lines = [l.strip() for l in block.splitlines()]
    found = False
    for line in lines:
        if header_frag.lower() in line.lower():
            found = True
            continue
        if found and line and not line.endswith(":"):
            return line
    return ""


def _split_irs_blocks(full_text: str) -> dict[str, list[str]]:
    raw = _FORM_SPLIT_RE.split(full_text)
    classified: dict[str, list[str]] = {}
    for block in raw:
        if not block.strip():
            continue
        bup = block.upper()
        if "FORM W-2" in bup or "W-2 WAGE" in bup:
            classified.setdefault("W-2", []).append(block)
        elif "1099-B" in bup and "BROKER" in bup:
            classified.setdefault("1099-B", []).append(block)
        elif "1099-R" in bup and ("DISTRIBUTION" in bup or "PENSION" in bup):
            classified.setdefault("1099-R", []).append(block)
        elif "5498" in bup and "RETIREMENT" in bup:
            classified.setdefault("5498", []).append(block)
        elif "1099-NEC" in bup:
            classified.setdefault("1099-NEC", []).append(block)
        elif "1099-DIV" in bup:
            classified.setdefault("1099-DIV", []).append(block)
        elif "1099-INT" in bup:
            classified.setdefault("1099-INT", []).append(block)
    return classified


def _parse_w2(block: str) -> dict:
    kv = _parse_kv(block)
    lines = [l.strip() for l in block.splitlines()]
    ein = _kv_fuzzy(kv, "Employer Identification")
    employer_name = ""
    if ein:
        idx = next((i for i, l in enumerate(lines) if l == ein), -1)
        if idx >= 0:
            for l in lines[idx + 1:]:
                if l and not l.endswith(":") and l != ein:
                    employer_name = l
                    break
    return {
        "employer": employer_name,
        "ein": ein,
        "wages": _dollars(_kv_fuzzy(kv, "Wages, Tips")),
        "federal_tax": _dollars(_kv_fuzzy(kv, "Federal Income Tax Withheld")),
        "ss_wages": _dollars(_kv_fuzzy(kv, "Social Security Wages")),
        "ss_tax": _dollars(_kv_fuzzy(kv, "Social Security Tax Withheld")),
        "medicare_wages": _dollars(_kv_fuzzy(kv, "Medicare Wages")),
        "medicare_tax": _dollars(_kv_fuzzy(kv, "Medicare Tax Withheld")),
        "deferred": _dollars(_kv_fuzzy(kv, "Deferred Compensation")),
        "roth": _dollars(_kv_fuzzy(kv, '"AA"', "Roth Contributions")),
        "health": _dollars(_kv_fuzzy(kv, '"DD"', "Health Coverage")),
        "retirement": _kv_fuzzy(kv, "Retirement Plan Indicator"),
    }


_PAGE_MARKER_RE = re.compile(r'^page\s+\d+/\d+$', re.IGNORECASE)


def _clean_payer(name: str) -> str:
    """Filter out page markers and other non-payer artifacts from extracted payer names."""
    if not name:
        return name
    if _PAGE_MARKER_RE.match(name.strip()):
        return ""
    if re.match(r'^\d+/\d+$', name.strip()):
        return ""
    return name


def _parse_1099b(block: str) -> dict:
    kv = _parse_kv(block)
    payer = _clean_payer(_kv_fuzzy(kv, "Payer", "Broker", "Issuer") or _first_after(block, "1099-B"))
    account = _kv_fuzzy(kv, "Account", "Recipient")
    date_sold = _kv_fuzzy(kv, "Date of Sale", "Date Sold", "Sale or Exchange")
    date_acq = _kv_fuzzy(kv, "Date Acquired", "Acquisition")
    description = _kv_fuzzy(kv, "Description")
    proceeds = _dollars(_kv_fuzzy(kv, "Proceeds"))
    cost = _dollars(_kv_fuzzy(kv, "Cost or Other Basis", "Cost or Basis", "Basis"))
    wash = _dollars(_kv_fuzzy(kv, "Wash Sale"))
    term = "Unknown"
    check = _kv_fuzzy(kv, "Check Box", "Applicable", "Holding")
    bup = (check + block).upper()
    if "SHORT" in bup:
        term = "Short-term"
    elif "LONG" in bup:
        term = "Long-term"
    return {
        "payer": payer,
        "account": account,
        "date_sold": date_sold,
        "date_acq": date_acq,
        "description": description,
        "proceeds": proceeds,
        "cost": cost,
        "wash": wash,
        "gain_loss": proceeds - cost + wash,
        "term": term,
    }


def _parse_1099r(block: str) -> dict:
    kv = _parse_kv(block)
    payer = _clean_payer(_kv_fuzzy(kv, "Payer", "Issuer") or _first_after(block, "1099-R"))
    account = _kv_fuzzy(kv, "Account", "Recipient")
    codes = _kv_fuzzy(kv, "Distribution Code", "Box 7")
    return {
        "payer": payer,
        "account": account,
        "codes": codes,
        "gross": _dollars(_kv_fuzzy(kv, "Gross Distribution")),
        "taxable": _dollars(_kv_fuzzy(kv, "Taxable Amount")),
        "withheld": _dollars(_kv_fuzzy(kv, "Federal Income Tax Withheld", "Tax Withheld")),
        "contrib": _dollars(_kv_fuzzy(kv, "Employee Contributions")),
    }


def _parse_simple(block: str) -> dict:
    kv = _parse_kv(block)
    payer = _kv_fuzzy(kv, "Payer", "Issuer", "Trustee") or _first_after(block, "1099")
    account = _kv_fuzzy(kv, "Account", "Recipient")
    return {"payer": payer, "account": account, **kv}


