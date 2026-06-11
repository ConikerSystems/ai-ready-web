"""
bates.py — detect and index sequential per-page reference labels: Bates numbers on
discovery productions AND court-record page labels.

Lawyers cite a page by its stamp, and the stamp format varies:
  - Bates production:   "GRANT THORNTON 0192" … "GRANT THORNTON 5333"
  - Common-law record:  "C 1" … "C 174"   (the "C-pages" of a court record)
  - Variants:           "C - 1", "R. 12", "SA 5", "ABC-0001234"

So an AI must be able to "go to C 51" or "go to Bates GRANT THORNTON 0512", and a
human must be able to confirm it against the PDF. This module:
  - detects the dominant label SERIES (a consistent prefix whose number INCREMENTS
    across the majority of pages — a constant repeated footer is rejected);
  - classifies it (Bates vs court record) for correct wording;
  - annotates each page with its label right after the page anchor (the stamp also
    stays verbatim in the page footer);
  - returns a summary the Document Index and Section 1 use, tied to THIS file (each
    file detects its own series; different files carry different schemes/ranges).
"""
import re

# A footer reference label: an uppercase-leading prefix (1+ chars, optionally
# several words), a space/dash/underscore separator (optional when the number is a
# 4+ digit run glued to the prefix, e.g. "ABC0001234"), then the number — at the
# END of a footer line (the label is often glued onto other footer text, e.g.
# "ATJ 503.11 Page 3 of 3 (08/25)  C 51"), anchored to `$`, starting the line or
# after 2+ spaces. The number width is NOT fixed: Bates runs are commonly 4, 5, 6,
# 7+ digits. Matches "C 4", "C - 1", "R. 12", "GRANT THORNTON 0192",
# "GRANT THORNTON 000192", "ABC-0001234", "DEF 00012345".
_LABEL_LINE_RE = re.compile(
    r'(?:^|\s{2,})'
    r'([A-Z][A-Za-z&.]*(?:[ _\-]+[A-Za-z&.]+)*?)'      # prefix (letters/words)
    r'(?:[ _\-]+(\d{1,12})|(\d{4,12}))\s*$')           # sep+number, or glued 4+ digits


def _page_map(combined_md: str) -> dict:
    pages = {}
    parts = re.split(r'<a id="[^"]*page-(\d+)[^"]*"></a>', combined_md)
    for i in range(1, len(parts) - 1, 2):
        try:
            pages[int(parts[i])] = parts[i + 1]
        except ValueError:
            pass
    return pages


def _classify(prefix: str):
    """(kind, label_term) for the Document Index / page marker wording."""
    if " " in prefix or len(prefix) >= 6:
        return "Bates", "Bates No."          # "GRANT THORNTON 0192"
    return "Page label", "Record page"        # "C 4", "R. 12"


def detect_bates(combined_md: str) -> dict | None:
    """Return the dominant page-label series, or None. Shape: {prefix, kind,
    label_term, page_to_bates:{pg:label}, start_label, end_label, start_num,
    end_num, count}."""
    pages = _page_map(combined_md)
    if not pages:
        return None
    per_page = {}
    prefix_count: dict = {}
    for pg, text in pages.items():
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        for ln in reversed(lines[-4:]):           # the label sits near the page foot
            m = _LABEL_LINE_RE.search(ln)
            if m:
                prefix = re.sub(r'\s+', ' ', m.group(1)).strip(" _-")
                numstr = m.group(2) or m.group(3)
                if (not prefix or len(prefix) > 30 or not numstr
                        or prefix.lower() in ("page", "no", "line", "figure", "vol", "volume", "of")):
                    continue
                label = re.sub(r'\s+', ' ', m.group(0)).strip()   # exact verbatim label
                per_page[pg] = (prefix, int(numstr), label)
                prefix_count[prefix] = prefix_count.get(prefix, 0) + 1
                break
    if not prefix_count:
        return None
    prefix = max(prefix_count, key=prefix_count.get)
    series = {pg: v for pg, v in per_page.items() if v[0] == prefix}
    # Majority of pages, and the numbers must INCREMENT across the document (a
    # constant repeated footer spans 0 and is rejected — not a real label series).
    if len(series) < max(2, 0.5 * len(pages)):
        return None
    nums = [v[1] for v in series.values()]
    start_num, end_num = min(nums), max(nums)
    if (end_num - start_num) < 0.5 * len(series):
        return None
    kind, label_term = _classify(prefix)
    lo_pg = min(series, key=lambda p: series[p][1])
    hi_pg = max(series, key=lambda p: series[p][1])
    return {
        "prefix": prefix, "kind": kind, "label_term": label_term,
        "page_to_bates": {pg: v[2] for pg, v in series.items()},
        "start_label": series[lo_pg][2], "end_label": series[hi_pg][2],
        "start_num": start_num, "end_num": end_num, "count": len(series),
    }


def annotate(combined_md: str, file_slug: str, info: dict) -> str:
    """Insert a prominent, citable label marker right after each page's anchor."""
    p2b = info["page_to_bates"]
    term = info.get("label_term", "Bates No.")

    def _ins(m):
        b = p2b.get(int(m.group(1)))
        return m.group(0) + (f"\n\n> **{term}:** {b}" if b else "")

    pat = r'<a id="' + re.escape(file_slug) + r'-page-(\d+)[^"]*"></a>'
    return re.sub(pat, _ins, combined_md)
