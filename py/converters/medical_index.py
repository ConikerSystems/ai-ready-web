"""
Medical Value Index — the medical analog of pdf_converter's Tax Line Index.

Goal (audit-ready, 0 wrong values): extract EVERY clinical test/result from a
lab or diagnostic report — analyte values, vitals, qualitative findings — each
with a page anchor so it is traceable to its source, and surface abnormal flags.

Design principle (same as tax): generalize by STRUCTURE, never branch per lab
vendor. A lab report — Quest/Access, LabCorp, Epic/WakeMed, Genova, Mosaic — all
reduce to one logical model:

    panel · test · result · units · reference-range · flag

The fields are recognized two ways, both vendor-agnostic:
  (1) label-anchored  — literal markers introduce fields:
        value : "Your Value" | "Result(s)" | "Current Result"
        range : "Standard Range" | "Reference Range" | "Reference Interval"
  (2) positional      — a packed token run "TEST [ref#] VALUE [FLAG] UNITS RANGE"
        parsed by token TYPE (numeric / range-pattern / unit / flag), not by a
        fixed column order (LabCorp and Access pack fields in different orders).

Plus a qualitative model for microbiology / genomics / descriptive components:
        test · result   where result ∈ {Not Detected, Detected, Positive,
        Negative, Reactive, a genotype, a color, "See Text", …}.

A row becomes an entry ONLY when a test name pairs with a recognizable result.
Prose rows (radiology narrative, clinical notes, reference books) yield nothing —
so the index self-withholds on documents that have no discrete values, exactly
like the tax index withholds on layouts it cannot parse. The independent
validator (converters/validator.py) re-confirms every emitted value against the
source page; anything it cannot confirm is flagged, never presented as fact.
"""
import re

# ── Units vocabulary (broad, but a closed set so prose numbers aren't mistaken
# for measurements). Lowercased compare; original casing preserved on output. ──
_UNITS = [
    "mg/dL", "mg/dl", "g/dL", "g/dl", "mcg/dL", "µg/dL", "ng/dL", "pg/mL",
    "ng/mL", "ng/L", "µg/L", "mcg/L", "mg/L", "g/L", "mmol/L", "µmol/L",
    "umol/L", "nmol/L", "pmol/L", "mEq/L", "IU/L", "IU/mL", "mIU/L", "mIU/mL",
    "U/L", "U/mL", "kU/L", "ratio", "%", "fL", "fl", "pg", "g/dL",
    "10^3/µL", "10*3/uL", "10^6/µL", "10*6/uL", "x10E3/uL", "x10E6/uL",
    "K/uL", "M/uL", "cells/µL", "cells/uL", "/uL", "/µL", "WBC/uL", "RBC/uL",
    "mm/hr", "mmHg", "bpm", "mOsm/kg", "mOsm/L", "copies/mL", "IU", "mcg", "mg",
    "mL", "ml", "L", "cm", "mm", "kg", "sec", "seconds", "titer", "index",
    "mcg/mL", "ug/L", "ug/dL", "ug/mL", "uIU/mL", "uU/mL", "mU/L",
]
# longest-first so "mg/dL" matches before "mg"
_UNITS_SORTED = sorted(set(_UNITS), key=len, reverse=True)
_UNIT_RE = re.compile(
    r'^(?:' + "|".join(re.escape(u) for u in _UNITS_SORTED) + r')$', re.IGNORECASE)

# A reference range: 65 - 100  |  0-30  |  <5.7  |  >100  |  <=1.2  |  3.5-5.2
#   |  = or >6.5  |  0.0-1.2  |  None detected  |  Negative
_RANGE_RE = re.compile(
    r'^(?:'
    r'[<>]=?\s*[\d.,]+'                       # <5.7  >100  <=1.2
    r'|=?\s*or\s*[<>]=?\s*[\d.,]+'            # = or >6.5
    r'|[\d.,]+\s*[-–]\s*[\d.,]+'              # 65 - 100   3.5-5.2
    r'|[\d.,]+\s*to\s*[\d.,]+'                # 65 to 100
    r')$', re.IGNORECASE)

# A numeric result value: 118  0.86  14,500  <1  188.6  None Detected handled separately
_VALUE_RE = re.compile(r'^[<>]?=?\s*[\d][\d.,]*$')

# Abnormal flags adjacent to a value. CH=critical high, CL=critical low.
_FLAG_TOKENS = {
    "h": "High", "l": "Low", "high": "High", "low": "Low",
    "ch": "Critical High", "cl": "Critical Low", "c": "Critical",
    "critical": "Critical", "panic": "Critical", "a": "Abnormal",
    "abn": "Abnormal", "abnormal": "Abnormal", "*": "Abnormal",
    "ll": "Critical Low", "hh": "Critical High",
}
_FLAG_RE = re.compile(
    r'^(CH|CL|HH|LL|H|L|C|A|High|Low|Critical|Panic|Abn(?:ormal)?|'
    r'Above|Below|Elevated|Decreased)$', re.IGNORECASE)

# Recognized qualitative results (closed set keeps prose out of the index)
_QUAL_RESULT_RE = re.compile(
    r'^(Not\s+Detected|Detected|None\s+Detected|Negative|Positive|Reactive|'
    r'Non[- ]?Reactive|Nonreactive|Normal|Abnormal|Present|Absent|Indeterminate|'
    r'Equivocal|Reference|See\s+(?:Text|Note|Report|Below)|Pending|'
    r'Clear|Cloudy|Turbid|Hazy|Bloody|Yellow|Straw|Colorless|Amber|Red|'
    r'Few|Moderate|Many|Rare|Trace|Positive\s+\w+|Negative\s+\w+)$',
    re.IGNORECASE)

# Genomic genotype result (Genova): "positive", "+/-", "-/-", "C677T", "Heterozygous"
_GENOTYPE_RE = re.compile(
    r'^(Homozygous|Heterozygous|Wild[- ]?Type|Normal|positive|negative|'
    r'[+\-]\s*/\s*[+\-]|[+\-]{1,2})$', re.IGNORECASE)

# Field-label markers
_VALUE_LABEL_RE = re.compile(
    r'^(Your\s+Value|Results?|Current\s+Result(?:\s+and\s+Flag)?|Value|Observed|'
    r'In\s+Range)$',
    re.IGNORECASE)
# Quest-style split value columns: a normal value sits under 'In Range', an
# abnormal one under 'Out Of Range' (the column itself signals the abnormality).
_VALUE_OOR_LABEL_RE = re.compile(r'^Out\s+Of\s+Range$', re.IGNORECASE)
_RANGE_LABEL_RE = re.compile(
    r'^(Standard\s+Range|Reference\s+Range|Reference\s+Interval|Ref\s+Range|'
    r'Normal\s+Range|Range|Expected)$', re.IGNORECASE)

# Panel / section group headers within a lab report (all-caps or title sections)
_PANEL_RE = re.compile(
    r'^(?:'
    r'[A-Z][-A-Z0-9 ,/&()]{3,55}'            # GENERAL CHEMISTRY, COMPLETE BLOOD COUNT
    r'|[A-Z][a-z]+(?:\s+[A-Za-z]+){0,4}\s+(?:Panel|Tests?|Needs|Profile|Studies)'
    r')$')

# Header rows we never want as an entry
_HEADER_NOISE_RE = re.compile(
    r'\b(Test\s+Name|Component|Reference\s+(?:Range|Interval)|Standard\s+Range|'
    r'Your\s+Value|Current\s+Result|Previous\s+Result|Units?)\b', re.IGNORECASE)

# Lines that are clearly not tests (addresses, phones, dates, page furniture)
_NONTEST_RE = re.compile(
    r'^\s*$|^\d[\d\s/:.\-]+$|'
    r'\b(?:Page|Phone|Fax|DOB|MRN|Acc#|Chart#|Client|Patient|Ordered?|Collected?|'
    r'Received?|Reported?|Resulted?|Director|CLIA|Address|Final\s+Report|'
    r'Copyright|©|www\.|http|Specimen|Account\s+Number|Date\s+(?:Collected|Received|Reported))\b',
    re.IGNORECASE)


# ──────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ──────────────────────────────────────────────────────────────────────────
def _page_rows(page):
    """Return visual rows: [[(x0, x1, text), …], …] clustered by y, x-sorted.

    Lines come from get_text('dict') so each carries its own bbox; spans within
    a line are joined. Rows are clustered when their y-centres fall within ~0.6×
    the median line height (tight enough to keep separate table rows apart, loose
    enough to join columns that the PDF emits at slightly staggered y, as Access
    does)."""
    lines = []
    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if not txt:
                continue
            x0, y0, x1, y1 = ln["bbox"]
            lines.append((x0, x1, (y0 + y1) / 2, max(y1 - y0, 1), txt))
    if not lines:
        return []
    heights = sorted(h for *_, h, _ in lines)
    med_h = heights[len(heights) // 2]
    tol = max(med_h * 0.6, 3)
    lines.sort(key=lambda L: (L[2], L[0]))
    rows, cur, cy = [], [], None
    for x0, x1, yc, h, txt in lines:
        if cy is None or abs(yc - cy) <= tol:
            cur.append((x0, x1, txt))
            cy = yc if cy is None else (cy + yc) / 2
        else:
            rows.append(sorted(cur, key=lambda f: f[0]))
            cur, cy = [(x0, x1, txt)], yc
    if cur:
        rows.append(sorted(cur, key=lambda f: f[0]))
    return rows


def _clean(s):
    return re.sub(r'\s+', ' ', s).strip().strip(":").strip()


def _split_tokens(text):
    """Split a packed fragment into whitespace tokens, but keep a range like
    '65 - 100' or '0.0-1.2' or '= or >6.5' as ONE token."""
    text = _clean(text)
    # protect spaced ranges/operators
    text = re.sub(r'(\d[\d.,]*)\s*([-–])\s*(\d[\d.,]*)', r'\1\2\3', text)
    text = re.sub(r'=\s*or\s*([<>]=?)\s*([\d.,]+)', r'=or\1\2', text, flags=re.I)
    text = re.sub(r'([<>]=?)\s+([\d.,]+)', r'\1\2', text)
    return text.split()


def _classify(tok):
    t = tok.strip()
    low = t.lower().replace("=or", "= or ")
    if _UNIT_RE.match(t):
        return "unit"
    if _RANGE_RE.match(t.replace("–", "-")) or _RANGE_RE.match(low):
        return "range"
    if _FLAG_RE.match(t):
        return "flag"
    if _VALUE_RE.match(t.replace(",", "")):
        return "value"
    return "word"


# ──────────────────────────────────────────────────────────────────────────
# Header-driven column model
# ──────────────────────────────────────────────────────────────────────────
# Generic left-column header words — distinguish a true header row from a
# WakeMed/Epic data row that merely repeats the 'Your Value'/'Standard Range'
# column labels next to a real test name.
_GENERIC_LEFT_HDR = re.compile(
    r'^(Component|Test(?:\s+Name)?|Analyte|Parameter|Name|Results?|Observation|'
    r'Measurement|Marker|Organism|Bacteria|Protozoa|Yeast|Parasites?|Microbiology|'
    r'Biomarker|Item)$', re.IGNORECASE)


def _header_columns(fragments):
    """If this row is a column header, return [(x_center, role), …] else None.

    A header has ≥1 non-leftmost fragment naming a value/range/result/units/flag
    column. The leftmost fragment is the test-name column whatever it is called
    ('Test Name', 'Component', 'Protozoa', 'Organism', …)."""
    if len(fragments) < 2:
        return None
    cols, found_field = [], False
    for i, (x0, x1, txt) in enumerate(fragments):
        xc = (x0 + x1) / 2
        t = _clean(txt)
        if i > 0 and re.search(r'\bPrevious\s+Result\b', t, re.I):
            cols.append((xc, "ignore")); found_field = True; continue
        if _RANGE_LABEL_RE.match(t):
            cols.append((xc, "range")); found_field = True
        elif _VALUE_OOR_LABEL_RE.match(t):
            cols.append((xc, "value_abn")); found_field = True
        elif _VALUE_LABEL_RE.match(t):
            cols.append((xc, "value")); found_field = True
        elif re.match(r'^Units?$', t, re.I):
            cols.append((xc, "unit")); found_field = True
        elif re.match(r'^Flags?$', t, re.I):
            cols.append((xc, "flag")); found_field = True
        elif i == 0:
            cols.append((xc, "test"))
        else:
            cols.append((xc, "ignore"))
    # need a test column on the left and at least one value/range field
    if not found_field or cols[0][1] != "test":
        return None
    if not any(r in ("value", "value_abn", "range") for _, r in cols):
        return None
    return cols


def _assign(fragments, cols):
    """Assign each fragment to its nearest column by x-centre → {role: text}."""
    bucket = {}
    for x0, x1, txt in fragments:
        xc = (x0 + x1) / 2
        role = min(cols, key=lambda c: abs(c[0] - xc))[1]
        if role == "ignore":
            continue
        bucket.setdefault(role, []).append(_clean(txt))
    return {r: " ".join(v).strip() for r, v in bucket.items()}


# Address / zip / phone signatures that masquerade as ALL-CAPS panels
_ADDRESS_RE = re.compile(
    r'\b[A-Z]{2}\s+\d{5}(?:-\d{4})?\b'          # FL 33458-3101
    r'|\b\d{3,5}\s+[A-Z].*\b(?:WAY|ST|AVE|AVENUE|ROAD|RD|BLVD|DRIVE|DR|LANE|LN)\b'
    r'|\(\d{3}\)\s*\d', re.IGNORECASE)


def _is_panel(fragments):
    """A lone full-width header like 'GENERAL CHEMISTRY' / 'Lipid Panel'.
    Tolerates a single trailing stray fragment (e.g. a page-count digit)."""
    text_frags = [f for f in fragments if _clean(f[2])]
    if not text_frags:
        return None
    t = _clean(text_frags[0][2])
    # ignore a lone trailing bare number / short artifact
    if len(text_frags) > 2:
        return None
    if len(text_frags) == 2 and not re.fullmatch(r'[\d.,*_ ]{1,6}', _clean(text_frags[1][2])):
        return None
    if _HEADER_NOISE_RE.search(t) or _NONTEST_RE.search(t) or _ADDRESS_RE.search(t):
        return None
    first = t.split()[0] if t.split() else ""
    if _PANEL_RE.match(t) and 3 < len(t) <= 60 and _classify(first) not in ("value", "range"):
        return re.sub(r'\s*\([Cc]ontinued\)\s*$', '', t).strip()
    return None


def _parse_value_cell(text):
    """Split a value-column cell into (value, units, flag).

    Accepts a result ONLY when it is a number (optionally with units/flag) or a
    recognized qualitative result from the closed set — never arbitrary prose.
    Handles '118 H', '100 WBC/uL', '14,500 RBC/uL', '<1', 'Not Detected'."""
    text = _clean(text).strip("*_ ")
    if not text or _VALUE_LABEL_RE.match(text):
        return "", "", ""
    if _QUAL_RESULT_RE.match(text) or _GENOTYPE_RE.match(text):
        return text, "", ""
    toks = _split_tokens(text)
    value, units, flag = "", "", ""
    for tk in toks:
        c = _classify(tk)
        if c == "value" and not value:
            value = tk
        elif c == "unit" and not units:
            units = tk
        elif c == "flag" and not flag:
            flag = _FLAG_TOKENS.get(tk.lower(), tk)
        elif c == "range" and not value:
            value = tk  # e.g. '<1' result expressed as an inequality
    # short tokenless qualitative (e.g. 'N/A', a single descriptive word) — accept
    # only when it is at most two clean words and not a sentence
    if not value and 0 < len(toks) <= 2 and not re.search(r'[.;:]', text) \
            and re.fullmatch(r'[A-Za-z/][A-Za-z/ ]{0,18}', text):
        return text, "", ""
    return value, units, flag


def _parse_range_cell(text):
    """Split a reference-range cell into (range, units). Handles '8 - 82 U/L',
    '0.0-1.2 ug/L', '<5.7 %', 'Negative', or a bare units string (no range)."""
    text = _clean(text)
    if not text:
        return "", ""
    if _QUAL_RESULT_RE.match(text):
        return text, ""
    toks = _split_tokens(text)
    rng, units = "", ""
    for tk in toks:
        c = _classify(tk)
        if c == "range" and not rng:
            rng = tk.replace("–", "-")
        elif c == "unit" and not units:
            units = tk
        elif c == "value" and not rng:
            rng = tk  # single-sided like a bare threshold
    return rng, units


def _norm_flag(text):
    t = _clean(text)
    return _FLAG_TOKENS.get(t.lower(), t if _FLAG_RE.match(t) else "")


def _extract_page(page, page_num, anchor):
    """Header-driven extraction for one page. Returns (entries, had_header)."""
    rows = _page_rows(page)
    cols = None
    panel = ""
    entries = []
    cur = None  # entry awaiting a possible split value/range row (WakeMed)

    def flush():
        nonlocal cur
        if cur and (cur.get("result") or cur.get("ref_range")):
            entries.append(cur)
        cur = None

    stub_fresh = False  # cur is a WakeMed label stub created on the previous row
    for frags in rows:
        # panel / section header?
        p = _is_panel(frags)
        if p:
            flush(); panel = p; stub_fresh = False; continue
        # column header? (only when none set yet, or it's a true repeat header
        # whose leftmost cell is a generic column label — NOT a WakeMed data row
        # that merely echoes the 'Your Value'/'Standard Range' labels)
        hdr = _header_columns(frags)
        if hdr and (cols is None or _GENERIC_LEFT_HDR.match(_clean(frags[0][2]))):
            flush(); cols = hdr; stub_fresh = False; continue
        if not cols:
            continue
        cell = _assign(frags, cols)
        test = _clean(cell.get("test", ""))
        # strip a trailing footnote ref like 'A, 01' / '01'
        test = re.sub(r'\s*(?:[A-Z],\s*)?\d{1,2}\s*$', '', test).strip() if re.search(r'\d{1,2}\s*$', test) and len(test.split()) > 1 else test
        val_cell = cell.get("value", "")
        abn_cell = cell.get("value_abn", "")
        rng = _clean(cell.get("range", ""))
        unit_cell = _clean(cell.get("unit", ""))
        flag_cell = _norm_flag(cell.get("flag", ""))

        value, units, vflag = _parse_value_cell(val_cell or abn_cell)
        units = units or unit_cell
        # a value found in the 'Out Of Range' column is abnormal by position
        flag = flag_cell or vflag or ("Abnormal" if (abn_cell and not val_cell and value) else "")
        # separate a glued 'range + units' cell ('8 - 82 U/L'); a cell that is
        # only units (WakeMed qualitative) yields no range
        rng, rng_units = _parse_range_cell(rng)
        units = units or rng_units

        # an ALL-CAPS multi-word panel name (esp. a '(Continued)' header) that
        # leaked next to a stray bare number is a section header, not a test row
        core = re.sub(r'\s*\([Cc]ontinued\)\s*$', '', test).strip()
        looks_panel = bool(core) and _PANEL_RE.match(core) and len(core.split()) >= 2 \
            and core.upper() == core
        stray_only = (not rng) and (not units) and (not value or re.fullmatch(r'\d{1,3}', value or ""))
        if looks_panel and (re.search(r'\(continued\)', test, re.I) or stray_only):
            flush(); panel = re.sub(r'\s*\([Cc]ontinued\)\s*$', '', test).strip()
            stub_fresh = False; continue

        has_test = bool(test) and not _HEADER_NOISE_RE.search(test) and not _NONTEST_RE.search(test)
        has_result = bool(value) or bool(rng)

        if has_test:
            flush()
            if not has_result:
                # WakeMed label sub-row: 'Lipase | Your Value | Standard Range'
                cur = {"panel": panel, "test": test, "result": "", "units": "",
                       "ref_range": "", "flag": "", "page": page_num, "anchor": anchor}
                stub_fresh = True
            else:
                cur = {"panel": panel, "test": test, "result": value,
                       "units": units, "ref_range": rng, "flag": flag,
                       "page": page_num, "anchor": anchor}
                flush(); stub_fresh = False
        elif stub_fresh and cur is not None and has_result:
            # value/range row immediately below a label stub → fill the stub
            cur["result"] = cur["result"] or value
            cur["units"] = cur["units"] or units
            cur["ref_range"] = cur["ref_range"] or rng
            cur["flag"] = cur["flag"] or flag
            flush(); stub_fresh = False
        else:
            # any other row (prose, blank) ends a dangling unfilled stub
            flush(); stub_fresh = False
    flush()
    return entries, (cols is not None)


def _is_indexable(e):
    """An audit-grade entry must carry units, a reference range, or a recognized
    qualitative/genotype result. A bare number with neither units nor range is
    not independently verifiable as normal/abnormal — and is exactly the shape
    of pseudo-table noise in reference books — so it is not indexed (the value
    still appears verbatim in the Section 3 text)."""
    res = (e.get("result") or "").strip()
    if not res:
        return False  # an index entry with no result value is not auditable
    if e.get("units") or e.get("ref_range"):
        return True
    return bool(_QUAL_RESULT_RE.match(res) or _GENOTYPE_RE.match(res))


def build_medical_value_index(doc, meta, file_slug):
    """Build meta['medical_value_index'] — every test/result with a page anchor."""
    entries = []
    pages_with_tables = 0
    for i, page in enumerate(doc, 1):
        anchor = f"{file_slug}-page-{i:03d}" if file_slug else f"page-{i:03d}"
        page_entries, had_header = _extract_page(page, i, anchor)
        if had_header:
            pages_with_tables += 1
        entries.extend(e for e in page_entries if _is_indexable(e))
    # de-dup identical (test,result,page) — packed blocks can re-emit
    seen, deduped = set(), []
    for e in entries:
        k = (e["page"], e["test"].lower(), e["result"].lower(), e["ref_range"])
        if k in seen:
            continue
        seen.add(k); deduped.append(e)
    meta["medical_value_index"] = deduped
    meta["medical_table_pages"] = pages_with_tables
    return deduped


def assess_medical_index_reliability(meta):
    """Mirror of tax index reliability: withhold rather than present doubtful
    values. The medical index is structural (each row independently confirmable
    against its page), so the gate is conservative — we mark UNRELIABLE only when
    the index is empty or so sparse relative to table pages that the layout was
    likely not understood. Per-value correctness is enforced by the validator."""
    meta.setdefault("medical_index_reliable", True)
    meta.setdefault("medical_index_unreliable_reason", "")
    idx = meta.get("medical_value_index", []) or []
    if not idx:
        meta["medical_index_reliable"] = False
        meta["medical_index_unreliable_reason"] = (
            "No discrete clinical values with a recognizable result/range "
            "structure were found — this document reads as narrative or "
            "reference text, so no value index is presented (the verbatim "
            "text in Section 3 is the record).")
        return
    # sanity: a real lab row needs a result OR a range
    bad = sum(1 for e in idx if not e.get("result") and not e.get("ref_range"))
    if bad and bad / len(idx) > 0.5:
        meta["medical_index_reliable"] = False
        meta["medical_index_unreliable_reason"] = (
            f"{bad} of {len(idx)} detected rows lack a confident value — the "
            f"layout was not parsed reliably; values withheld to avoid error.")
