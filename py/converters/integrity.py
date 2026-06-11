"""
Source-data integrity check.

A core promise of AI Ready: the numeric values in the source document must appear
unchanged in the extracted Markdown. This module verifies that every distinct
number in the ORIGINAL source text is present in the extracted Markdown (compared
BEFORE masking — masking intentionally removes some values and is reported
separately). Anything that can't be confirmed is surfaced to the user, exactly
like mask/replacement notices, so a high-integrity guarantee is auditable.

This catches extraction faults — dropped table cells, mangled columns, OCR
mis-reads — that would otherwise silently change the data an AI later relies on.
"""
import re

# A numeric value: an integer/decimal, optionally with thousands separators.
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")

# A date value — verified as a whole token so "12/11/2024" can't silently become
# "12/11/2042". Covers M/D/Y, M-D-Y, and ISO Y-M-D forms.
_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b|\b\d{4}-\d{2}-\d{2}\b")


def _digits(tok: str) -> str:
    return re.sub(r"\D", "", tok)


def check_numbers(source_text: str, extracted_md: str, sample: int = 25) -> dict:
    """
    Compare distinct numeric tokens in `source_text` to `extracted_md`.

    Returns a dict:
      total      — distinct source numbers checked
      preserved  — how many were found in the extraction
      missing    — list (up to `sample`) of source numbers not confirmed present
      pct        — preservation percentage
      ok         — True when nothing is missing
    Tokens with fewer than 2 digits are ignored (page numbers, list bullets, etc).
    """
    md_norm = re.sub(r"\s+", " ", extracted_md)
    # Comma-stripped copy so "1,234,567" in the source still matches "1234567"
    # in the extraction (or vice-versa) — i.e. tolerate thousands-separator
    # reformatting without tolerating an actual change of digits.
    md_nocommas = md_norm.replace(",", "")

    seen, ordered = set(), []
    # Check whole date tokens first (so the slash/dash form is preserved), then
    # individual numbers.
    for tok in _DATE_RE.findall(source_text):
        if tok and tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    for tok in _NUM_RE.findall(source_text):
        if len(_digits(tok)) < 2:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        ordered.append(tok)

    missing = []
    for tok in ordered:
        if tok in md_norm or tok.replace(",", "") in md_nocommas:
            continue
        missing.append(tok)

    total = len(ordered)
    preserved = total - len(missing)
    return {
        "total": total,
        "preserved": preserved,
        "missing": missing[:sample],
        "missing_count": len(missing),
        "pct": round(100.0 * preserved / total, 2) if total else 100.0,
        "ok": not missing,
    }
