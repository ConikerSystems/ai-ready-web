"""
AI Ready Mobile — web-only orchestration glue.

Everything under py/converters/ and py/masking/ is a VERBATIM copy from the
private AI_Ready repo (source of truth — fix there first, then re-copy).
This module is the only web-specific Python: it routes a file to the right
converter, applies masking + the user's custom replacements (same engine and
semantics as the desktop app, including the cross-file name sweep), and zips
the batch. `_term_to_regex` / `_apply_custom_variables` are copied from
ai_ready app.py — keep in sync.

Mobile scope (agreed): PDF, DOCX, PPTX, TXT, MD only. No Excel/CSV, no OCR,
no audio, no folder ingest, no condense.
"""
import os
import re
import zipfile

from converters import pdf_converter, text_converter
from masking.masker import mask_text, sweep_names

SUPPORTED = {".pdf", ".docx", ".pptx", ".txt", ".md"}


def _converter_for(ext: str):
    # docx/pptx import lazily: python-docx / python-pptx finish installing in
    # the background after boot, and PDFs must work immediately regardless.
    if ext == ".pdf":
        return pdf_converter
    if ext == ".docx":
        from converters import docx_converter
        return docx_converter
    if ext == ".pptx":
        from converters import pptx_converter
        return pptx_converter
    return text_converter


def make_slug(filename: str) -> str:
    base = filename.rsplit(".", 1)[0]
    return re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_") or "document"


# ── copied from ai_ready app.py (keep in sync) ──────────────────────────────

def _term_to_regex(term: str) -> str:
    tokens = re.split(r"\s+", term.strip())
    return r"\s+".join(re.escape(t) for t in tokens if t)


def _apply_custom_variables(text: str, variables: list) -> tuple:
    count = 0
    for var in variables:
        current = (var.get("current") or var.get("find") or "").strip()
        masked = var.get("masked", "")        # blank = delete, intentionally
        if not current:
            continue
        terms = [current] + [a for a in var.get("aliases", []) if a and a.strip()]
        terms = sorted(set(terms), key=len, reverse=True)
        for term in terms:
            pat = _term_to_regex(term)
            n = len(re.findall(pat, text, re.IGNORECASE))
            if n:
                text = re.sub(pat, masked, text, flags=re.IGNORECASE)
                count += n
    return text, count

# ─────────────────────────────────────────────────────────────────────────────


def process_file(path: str, filename: str, process_date: str,
                 mask_mode: str, variables: list,
                 batch_names: set, progress_cb=None, stage_cb=None) -> tuple:
    """Convert one file → (output_name, markdown). Raises ValueError on
    unsupported type. `batch_names` accumulates discovered personal names
    across the batch for the final cross-file sweep (desktop parity)."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in SUPPORTED:
        raise ValueError(f"unsupported file type: {ext}")
    conv = _converter_for(ext)
    slug = make_slug(filename)
    if ext == ".pdf":
        md, meta = conv.convert(path, filename, process_date, slug,
                                progress_cb=progress_cb, stage_cb=stage_cb)
    else:
        md, meta = conv.convert(path, filename, process_date, slug)

    if mask_mode in ("full", "soft"):
        md, _stats = mask_text(md, mask_mode, collect=batch_names)
    if variables:
        md, _n = _apply_custom_variables(md, variables)
    return slug + ".md", md


def finish_batch(outputs: dict, mask_mode: str, batch_names: set) -> dict:
    """Desktop-parity final pass: a name found in one document is masked in
    every document of the run."""
    if mask_mode in ("full", "soft") and batch_names:
        for name in list(outputs):
            outputs[name], _ = sweep_names(outputs[name], batch_names, mask_mode)
    return outputs


def build_zip(outputs: dict, zip_path: str) -> str:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, md in outputs.items():
            z.writestr(name, md)
    return zip_path
