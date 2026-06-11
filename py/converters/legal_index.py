"""
legal_index.py — the Legal/LLC analog of the Tax Line Index and Medical Value
Index. Tax forms and financial statements are tabular (rows × columns); legal and
LLC documents are mostly free text — but that text still needs to be AUDITABLE and
SEARCHABLE. This builds a structured index of the auditable elements of a legal
document, each traceable to a source page:

  - Party            (named parties / signatories)
  - Date             (effective, execution, and referenced dates)
  - Amount           (dollar amounts, with their context)
  - Defined Term     (the quoted terms a contract defines — its backbone)
  - Section          (article / section / exhibit headings — the structure map)
  - Case No.         (case / docket numbers, for litigation)

Every value is read VERBATIM from the page it is attributed to, so the independent
validator can confirm traceability (value present on its cited Section 3 page).
Like the tax and medical indexes, it SELF-WITHHOLDS — on an image-only document
with no extractable text, or one with no structured elements, it sets
`legal_index_reliable = False` and the Gold Master presents Section 3 (the verbatim
text) as the authoritative record rather than an empty or guessed index.
"""
import re
from converters import content_detector as cd

# Split Section-3-style markdown back into {page_num: text} via its page anchors.
_ANCHOR_RE = re.compile(r'<a id="[^"]*page-(\d+)[^"]*"></a>')

# "Term" means … / "Term" shall mean … — straight and curly quotes.
_DEFINED_TERM_RE = re.compile(
    r'[“"]([A-Z][^”"]{2,45})[”"]\s*(?:\([^)]{0,40}\)\s*)?'
    r'(?:shall\s+mean|means\b|shall\s+have\s+the\s+meaning|shall\s+refer\s+to)',
)
# Case caption "X v. Y" and recital "by and between X" — conservative, verbatim.
_CAPTION_RE = re.compile(r'\b([A-Z][A-Za-z.,&\'\- ]{2,45}?)\s+v\.?\s+([A-Z][A-Za-z.,&\'\- ]{2,45})')
_RECITAL_RE = re.compile(r'\bby\s+and\s+(?:between|among)\s+([A-Z][A-Za-z.,&\'\- ]{3,45})')

# Numbered references — the fingerprint of a contract clause, statute, or rule:
# "Section 9.3(a)", "Article XI", "ARTICLE IV", "§ 202", "Rule 341", "Schedule 12",
# "Exhibit B", "Paragraph 4". The IDENTIFIER must be captured exactly so an AI can
# cite it and a human can confirm the words against the PDF at that page.
_REFERENCE_RE = re.compile(
    r'\b(Section|Sec\.|Article|Art\.|Paragraph|Para\.|Clause|Schedule|Exhibit|'
    r'Appendix|Addendum|Rule|Articles|Sections)\s+'
    r'(\d+(?:\.\d+)*(?:\([a-z0-9]{1,4}\))*|[IVXLCDM]{1,7}\b|[A-Z]\b)'
)
_SECTION_SYMBOL_RE = re.compile(r'(§+\s*\d+(?:\.\d+)*(?:\([a-z0-9]{1,4}\))*[A-Za-z\-]*)')
# Statute / case citations — "26 U.S.C. § 199A", "735 ILCS 5/2-1301",
# "183 Ill. 2d 290 (1998)", "No. 11-2070 (7th Cir. 2012)".
_CITATION_RE = re.compile(
    r'\b\d{1,4}\s+U\.?S\.?C\.?\s*§*\s*\d+[A-Za-z0-9\-]*'
    r'|\b\d{1,4}\s+[A-Z]{2,6}\s+\d+(?:/\d+)?(?:-\d+)?'
    r'|\b\d{1,4}\s+[A-Z][a-z]+\.?\s*(?:\d?d|App\.|Supp\.)?\s*\d+\s*\(\d{4}\)'
    r'|\bNo\.\s*\d{1,4}-\d{1,5}\s*\([^)]{2,30}\d{4}\)'
)

# Index budget. 800 is ample for a normal contract or filing, but it is a global
# cap applied in page order — so on a multi-thousand-page production it was spent
# entirely on the first few hundred pages, leaving the rest of the document with no
# navigable index at all. Scale the budget with page count (so coverage spans the
# whole document) but bound it so the index — which all lands in Part 01 of a split
# file — cannot itself blow the per-part size budget.
_MAX_ENTRIES = 800                  # floor: small docs are unchanged
_ENTRIES_PER_PAGE = 3               # additional budget granted per source page
_MAX_ENTRIES_CEILING = 4000         # hard ceiling so Part 01 stays within budget


def _entry_budget(n_pages: int) -> int:
    return max(_MAX_ENTRIES, min(_MAX_ENTRIES_CEILING, n_pages * _ENTRIES_PER_PAGE))


_MAX_PER_KIND_PER_PAGE = {"Date": 6, "Amount": 12, "Party": 8, "Defined Term": 30,
                          "Section": 20, "Case No.": 4, "Reference": 30, "Citation": 12}


def _page_map(combined_md: str) -> dict:
    pages: dict[int, str] = {}
    parts = _ANCHOR_RE.split(combined_md)
    for i in range(1, len(parts) - 1, 2):
        try:
            pages[int(parts[i])] = parts[i + 1]
        except ValueError:
            pass
    return pages


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().strip(",.;:").strip()


def _page_entries(text: str, page: int, anchor: str) -> list:
    out = []

    def add(kind, value, detail=""):
        v = _clean(value)
        if len(v) < 2:
            return
        out.append({"kind": kind, "detail": _clean(detail), "value": v,
                    "page": page, "anchor": anchor})

    # Defined terms — the contract's backbone, very high navigation value.
    for m in list(_DEFINED_TERM_RE.finditer(text))[:_MAX_PER_KIND_PER_PAGE["Defined Term"]]:
        add("Defined Term", m.group(1))

    # Section / article / exhibit headings — the structure map.
    for h in cd.extract_section_headings(text, _MAX_PER_KIND_PER_PAGE["Section"]):
        add("Section", h)

    # Numbered references (the clause/statute/rule fingerprint) — capture the exact
    # identifier ("Section 9.3(a)") plus the words that introduce it, so it is
    # precisely citable and verifiable against the PDF at this page.
    for m in list(_REFERENCE_RE.finditer(text))[:_MAX_PER_KIND_PER_PAGE["Reference"]]:
        ident = f"{m.group(1)} {m.group(2)}"
        tail = _clean(text[m.end():m.end() + 60]).split(".")[0]
        add("Reference", ident, tail[:55])
    for m in list(_SECTION_SYMBOL_RE.finditer(text))[:_MAX_PER_KIND_PER_PAGE["Reference"]]:
        add("Reference", m.group(1))
    # Statute / case citations.
    for m in list(_CITATION_RE.finditer(text))[:_MAX_PER_KIND_PER_PAGE["Citation"]]:
        add("Citation", m.group(0))

    # Per-region TABLES — account for tabular content alongside prose. Each
    # markdown table already rendered on this page becomes a traceable entry: the
    # first column header (verbatim) as the value, the full column list + size as
    # the detail. So an AI can find "page N has a Date/Description/Amount table".
    for headers, nrows, ncols in _page_tables(text):
        add("Table", headers[0], f"{nrows}×{ncols} table — columns: " + " | ".join(headers[:8]))

    # Parties — label-anchored roles, plus case captions and recital parties.
    for role, name in cd.extract_parties(text):
        add("Party", name, role)
    cap = _CAPTION_RE.search(text)
    if cap:
        add("Party", cap.group(1), "Caption (v.)")
        add("Party", cap.group(2), "Caption (v.)")
    for m in list(_RECITAL_RE.finditer(text))[:4]:
        add("Party", m.group(1), "by and between")

    # Dates and dollar amounts — verbatim, with context.
    for d in cd.extract_dates(text, _MAX_PER_KIND_PER_PAGE["Date"]):
        add("Date", d)
    for ctx, amt in cd.extract_dollar_context(text, _MAX_PER_KIND_PER_PAGE["Amount"]):
        add("Amount", amt, ctx)

    # Case / docket numbers.
    for cn in cd.extract_case_numbers(text):
        add("Case No.", cn)
    return out


# A markdown table row already rendered into Section 3 by the converter's
# layout-aware table detection: "| cell | cell | … |". The separator row
# "|---|---|" is skipped. Indexing tables FROM the rendered markdown (rather than
# re-detecting on raw geometry) means every Table entry is verbatim-traceable to
# the page and costs only string parsing — no per-page geometry pass.
_PIPE_SEP_RE = re.compile(r'^\|[\s:|*-]+\|?\s*$')


def _page_tables(text: str):
    """Yield (header_cells, n_rows, n_cols) for each markdown pipe-table block of
    >=2 data rows and >=2 columns on the page."""
    block = []
    for ln in text.split("\n"):
        s = ln.strip()
        if s.startswith("|") and s.count("|") >= 3:
            if _PIPE_SEP_RE.match(s):
                continue            # skip the |---|---| separator, keep the block open
            block.append(s)
        else:
            t = _summarize_table(block)
            if t:
                yield t
            block = []
    t = _summarize_table(block)
    if t:
        yield t


def _summarize_table(block):
    if len(block) < 2:
        return None
    cells = [c.strip() for c in block[0].strip().strip("|").split("|")]
    headers = [re.sub(r"\s+", " ", c).strip("* ") for c in cells if c.strip()]
    if len(headers) < 2:
        return None
    return headers, len(block), len(cells)


def build_legal_index(combined_md: str, meta: dict, file_slug: str) -> None:
    """Populate meta['legal_index'] + meta['legal_index_reliable'] (+ reason).
    Reads each page's text from the assembled Section-3 markdown so OCR pages are
    covered too. Dedups by (kind, value), keeping the FIRST page a value appears
    on (the most useful anchor for navigation)."""
    pages = _page_map(combined_md)
    total_text = sum(len(t) for t in pages.values())
    max_entries = _entry_budget(len(pages))

    entries: list = []
    by_key: dict = {}

    def _alpha(s):
        return sum(c.isalpha() for c in s or "")

    for pg in sorted(pages):
        anchor = f"{file_slug}-page-{pg:03d}" if file_slug else f"page-{pg:03d}"
        for e in _page_entries(pages[pg], pg, anchor):
            # Tables are deduped per page (a "Date" column recurs across pages and
            # each page's table is its own region); all other kinds dedupe globally.
            key = ((e["kind"], e["value"].lower(), pg) if e["kind"] == "Table"
                   else (e["kind"], e["value"].lower()))
            prev = by_key.get(key)
            if prev is not None:
                # A numbered reference appears in both the table of contents and the
                # provision body. Prefer the occurrence with the richer heading text
                # (the body), so its page anchor points where the clause actually is.
                if e["kind"] == "Reference" and _alpha(e["detail"]) > _alpha(prev["detail"]):
                    prev.update(e)
                continue
            by_key[key] = e
            entries.append(e)
            if len(entries) >= max_entries:
                break
        if len(entries) >= max_entries:
            break

    # Bates numbering — one navigable entry tying the file's Bates series to its
    # pages. Each page's individual Bates No. is annotated in Section 3 (after the
    # page anchor) so "go to Bates <N>" resolves to the exact page.
    bates = meta.get("bates")
    if bates and bates.get("start"):
        p2b = bates.get("page_to_bates", {})
        start_pg = next((pg for pg, lbl in p2b.items() if lbl == bates["start"]),
                        (min(p2b) if p2b else 1))
        anchor = f"{file_slug}-page-{start_pg:03d}" if file_slug else f"page-{start_pg:03d}"
        term = bates.get("label_term", "Bates No.")
        entries.insert(0, {
            "kind": bates.get("kind", "Bates"), "value": bates["start"],
            "detail": (f"{term} range {bates['start']}–{bates['end']} · {bates['count']} "
                       f"pages · 1 per page. Each page's {term} follows its page anchor "
                       f"(navigate by {term})."),
            "page": start_pg, "anchor": anchor,
        })

    meta["legal_index"] = entries
    if not pages or total_text < 200:
        meta["legal_index_reliable"] = False
        meta["legal_index_unreliable_reason"] = (
            "This document has little or no extractable text (likely image-only with "
            "no OCR). No structured index can be built; rely on Section 3 and the "
            "original document."
        )
    elif not entries:
        meta["legal_index_reliable"] = False
        meta["legal_index_unreliable_reason"] = (
            "No structured legal elements (parties, dates, amounts, defined terms, "
            "section headings) could be extracted with confidence from this document."
        )
    else:
        meta["legal_index_reliable"] = True
        meta["legal_index_unreliable_reason"] = ""
