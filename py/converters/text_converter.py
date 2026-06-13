"""Plain-text / Markdown passthrough converter.

Folder ingest hits real `.txt` and `.md` files (e.g. `26.01.28 hearing.txt`,
`Case_Citations_Reference_Guide.md`). Rather than report them as "unsupported"
and drop them — which would violate AI Ready's never-skip guarantee — we include
them verbatim. The text is already AI-readable; we only wrap it in the same
Section-3 shape (one page anchor) the rest of the pipeline expects so it threads
through `_assemble_gold_master`, masking, and large-file splitting unchanged.

Matches the converter contract: convert(file_path, filename, process_date,
file_slug="") -> (section3_md, meta).
"""

from converters import content_detector
from converters import integrity

_CONTENT_TYPE_MAP = {
    "financial": "Financial Statement",
    "medical": "Medical Document",
    "legal": "Legal Document",
    "real_estate": "Real Estate Document",
    "insurance": "Insurance Document",
}


def _read_text(file_path: str) -> str:
    """Read text tolerantly — these are user files of unknown encoding."""
    for enc in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            with open(file_path, "r", encoding=enc) as f:
                return f.read()
        except (UnicodeDecodeError, UnicodeError):
            continue
    # Last resort: never fail to include a file over an encoding quirk.
    with open(file_path, "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def convert(file_path: str, filename: str, process_date: str, file_slug: str = "") -> tuple[str, dict]:
    text = _read_text(file_path)

    detected = content_detector.detect_content_type(text)
    is_md = filename.lower().endswith(".md")
    content_type = _CONTENT_TYPE_MAP.get(detected, "Markdown Document" if is_md else "Text Document")

    anchor_id = f"{file_slug}-page-001" if file_slug else "page-001"
    # A .md file already carries its own headings/tables — keep it verbatim. A
    # .txt is plain prose; emit it verbatim too (no reflowing — fidelity first).
    body = text if text.strip() else "_(empty file)_"
    section3 = f"\n<a id=\"{anchor_id}\"></a>\n\n### Page 1\n\n{body}\n"

    meta = {
        "potential_titles": [],
        "form": "",
        "form_name": "",
        "tax_year": "",
        "case_numbers": [],
        "page_count": 1,
        "author": "",
        "content_type": content_type,
        "scanned_ratio": 0,
        "ocr_pages": 0,
        "ocr_available": False,
        "integrity": integrity.check_numbers(text, section3),
    }

    # Document Index — the universal audit fingerprint (parties, dates, amounts,
    # defined terms, numbered references, section headings, case numbers → page
    # anchor). It operates on the extracted text, so a .txt/.md gets the same
    # index a PDF does. (Bates and tax/medical grid indexes are PDF-structure
    # only and don't apply here.) Mirrors pdf_converter's call; self-withholds on
    # empty/low-signal text via legal_index_reliable.
    from converters import legal_index
    try:
        legal_index.build_legal_index(section3, meta, file_slug)
    except Exception:
        meta.setdefault("legal_index", [])
        meta["legal_index_reliable"] = False
        meta.setdefault("legal_index_unreliable_reason", "Document index build failed.")

    return section3, meta
