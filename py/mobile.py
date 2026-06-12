"""
AI Ready Mobile — web-only orchestration.

Mirrors the desktop `_process_job` flat-run path step for step so mobile
output is format-identical to the desktop app: per-file Gold Masters wrapped
in START/END FILE markers, a combined document with Source Registry +
Working Notes + append marker, part-splitting at the user's Output Size,
batch-wide name sweep. The format machinery itself is `assembly.py` — a
verbatim extract of app.py's functions (see its header).

`py/converters/` and `py/masking/` are VERBATIM copies from the private
AI_Ready repo — fix there first, then re-copy.

Mobile scope (agreed): PDF, DOCX, PPTX, TXT, MD only. No Excel/CSV, no OCR,
no audio, no folder ingest, no condense, no preserve-original toggle.
"""
import os
import re
import zipfile

import assembly
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


def run_batch(input_dir: str, filenames: list, mask_mode: str, variables: list,
              part_target: int, timestamp: str, gen_time: str, process_date: str,
              app_version: str, file_start_cb=None, progress_cb=None) -> list:
    """Process a flat batch exactly like the desktop worker. Writes all outputs
    into OUTPUTS_DIR and returns [{name, size, kind}] (combined parts first).
    `file_start_cb(i, n, filename)` fires as each source file begins."""
    assembly.VERSION = app_version
    out_dir = assembly.OUTPUTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, old))

    # Desktop parity: sources ordered NEWEST-FIRST (date-in-name heuristic).
    filenames = sorted(filenames, key=assembly._doc_order_key)

    pt = part_target or assembly._PART_TARGET
    registry, sections, large_files, results = [], [], [], []
    batch_names: set = set()

    for idx, filename in enumerate(filenames):
        if file_start_cb:
            file_start_cb(idx, len(filenames), filename)
        source_idx = idx + 1
        ext = os.path.splitext(filename)[1].lower()
        if ext not in SUPPORTED:
            continue
        path = os.path.join(input_dir, filename)
        sha = assembly._compute_sha256(path)
        slug = assembly._make_slug(filename, source_idx)
        conv = _converter_for(ext)
        if ext == ".pdf":
            section3, meta = conv.convert(path, filename, process_date, slug,
                                          progress_cb=progress_cb)
        else:
            section3, meta = conv.convert(path, filename, process_date, slug)

        # Desktop-parity (v1.3.26): assembled WITHOUT the global preamble —
        # markers let the per-file output re-insert the full rules/notice while
        # combined outputs carry them once per part.
        gold = assembly._assemble_gold_master(
            filename, meta, sha, mask_mode, process_date, section3, file_slug=slug,
            include_preamble=False)
        del section3

        masked, _stats = mask_text(gold, mask_mode, collect=batch_names)
        del gold
        if variables:
            masked, _n, _vs = assembly._apply_custom_variables(masked, variables)

        src_id = f"SRC-{source_idx:03d}"
        breadcrumb = filename
        stem = os.path.splitext(os.path.basename(filename))[0]
        out_name = stem + ".md"
        page_count = meta.get("page_count", "?")
        per_file_md = (masked
                       .replace(assembly._MARK_RULES, assembly._global_rules(mask_mode), 1)
                       .replace(assembly._MARK_NOTICE, assembly._LEGAL_DISCLAIMER, 1))
        front_matter = assembly._front_matter("ai-ready/gold-master", [
            ("source", filename),
            ("sha256", sha),
            ("pages", page_count),
            ("mask", assembly._mask_label(mask_mode)),
            ("generated", process_date),
            ("tool", f"AI Ready {app_version}"),
        ])
        with open(os.path.join(out_dir, out_name), "w", encoding="utf-8") as f:
            f.write(front_matter)
            f.write(f"# ===== START FILE: {breadcrumb} =====\n\n")
            f.write(per_file_md)
            f.write(f"\n\n# ===== END FILE: {breadcrumb} =====\n")

        entry = {"src_id": src_id, "filename": filename, "folder_path": breadcrumb,
                 "page_count": page_count, "sha256": sha, "file_slug": slug}
        if len(per_file_md.encode("utf-8")) > pt:
            large_files.append({**entry, "out_name": out_name})
        else:
            section_md = (masked
                          .replace(assembly._MARK_RULES, assembly._RULES_POINTER, 1)
                          .replace("\n\n" + assembly._MARK_NOTICE + "\n\n", "\n\n", 1)
                          .replace(assembly._MARK_NOTICE, "", 1))
            sections.append(f"\n\n---\n\n# ===== START FILE: {breadcrumb} ({src_id}) =====\n\n"
                            + section_md
                            + f"\n\n# ===== END FILE: {breadcrumb} ({src_id}) =====\n")
            registry.append(entry)
        results.append(out_name)
        del masked, per_file_md
        import gc; gc.collect()

    # Batch-wide name sweep over every individual .md (desktop parity).
    if batch_names and mask_mode != "none":
        for out_name in results:
            fp = os.path.join(out_dir, out_name)
            with open(fp, "r", encoding="utf-8") as rf:
                text = rf.read()
            text, hits = sweep_names(text, batch_names, mask_mode)
            if hits:
                with open(fp, "w", encoding="utf-8") as wf:
                    wf.write(text)

    outputs = []   # [{name, size, kind}]

    # Large files: split each post-sweep single-file .md into ordered parts.
    for lf in large_files:
        fp = os.path.join(out_dir, lf["out_name"])
        with open(fp, "r", encoding="utf-8") as rf:
            lf_md = rf.read()
        parts = assembly._split_gold_master(
            lf_md, lf["filename"], lf["file_slug"], lf["sha256"], lf["page_count"],
            timestamp, gen_time, mask_mode, part_target=pt)
        for p in parts:
            outputs.append({"name": p["name"], "size": p["size"], "kind": "large-part"})

    # Combined document (one per flat run, split at Output Size boundaries).
    if registry:
        raw = "".join(sections)
        if batch_names and mask_mode != "none":
            raw, _swept = sweep_names(raw, batch_names, mask_mode)
        _primary, parts = assembly._emit_combined_group(
            f"AI_Ready_{timestamp}", "Combined Document", registry, raw,
            timestamp, gen_time, mask_mode, len(registry), part_target=pt)
        for p in parts:
            outputs.append({"name": p["name"], "size": p["size"], "kind": "combined"})

    for out_name in results:
        fp = os.path.join(out_dir, out_name)
        outputs.append({"name": out_name, "size": os.path.getsize(fp), "kind": "file"})

    # Combined first, then large parts, then per-file outputs.
    order = {"combined": 0, "large-part": 1, "file": 2}
    outputs.sort(key=lambda o: order.get(o["kind"], 9))
    return outputs


def build_zip(zip_name: str) -> str:
    """Zip everything in OUTPUTS_DIR (the whole run package)."""
    out_dir = assembly.OUTPUTS_DIR
    zip_path = os.path.join("/tmp", zip_name)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(out_dir)):
            z.write(os.path.join(out_dir, name), name)
    return zip_path
