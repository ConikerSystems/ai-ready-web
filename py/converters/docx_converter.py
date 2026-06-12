from docx import Document
import re
from converters import content_detector
from converters import integrity

_HEADING_STYLES = {"heading 1", "heading 2", "heading 3", "heading 4", "title"}
_LEVEL_MAP = {"heading 1": "#", "heading 2": "##", "heading 3": "###", "heading 4": "####", "title": "#"}

# Shared content type map (same as pdf_converter)
_CONTENT_TYPE_MAP = {
    "financial": "Financial Statement",
    "medical":   "Medical Document",
    "legal":     "Legal Document",
    "real_estate": "Real Estate Document",
    "insurance": "Insurance Document",
}

# Filename keywords for type fallback (same logic as pdf_converter)
_FNAME_KEYWORDS = {
    "Insurance Document": ['insurance', 'ho6', 'ho-6', 'policy', 'umbrella',
                           'homeowner', 'renters', 'neptune', 'eagles',
                           'dec official', 'declaration', 'coverage', 'premium'],
    "Medical Document":   ['medical', 'lab', 'labs', 'doctor', 'hospital',
                           'clinic', 'radiology', 'pathology', 'health'],
    "Legal Document":     ['contract', 'agreement', 'deposition', 'complaint',
                           'petition', 'motion', 'brief', 'transcript',
                           'affidavit', 'judgment', 'order', 'subpoena'],
    "Financial Statement":['statement', 'invoice', 'balance', 'financial',
                           'budget', 'revenue', 'expense', 'payroll'],
}

# Medical section headers in Word documents (plain text, not Word heading styles)
_MEDICAL_SECTION_RE = re.compile(
    r'^(?:HISTORY|FINDINGS?|IMPRESSION|ASSESSMENT(?:\s+AND\s+PLAN)?|PLAN|CONCLUSION|'
    r'CYTOLOGIC\s+DIAGNOSIS|PATHOLOGIC\s+DIAGNOSIS|FINAL\s+DIAGNOSIS|CLINICAL\s+HISTORY|'
    r'INDICATION|TECHNIQUE|COMPARISON|PROCEDURE|RESULT|SUMMARY|RECOMMENDATION|'
    r'CC|HPI|ROS|PMH(?:x)?|PSH|FH|SH|SOC(?:\s+HX)?|'
    r'MEDICATIONS?|ALLERGIES?|VITALS?|EXAM(?:INATION)?|CHIEF\s+COMPLAINT|'
    r'REVIEW\s+OF\s+SYSTEMS|PAST\s+(?:MEDICAL\s+)?HISTORY)'
    r'[\s:\.]+',
    re.IGNORECASE | re.MULTILINE,
)

def _table_to_md(table) -> str:
    rows = []
    for i, row in enumerate(table.rows):
        cells = [cell.text.strip().replace("|", "\\|") for cell in row.cells]
        rows.append("| " + " | ".join(cells) + " |")
        if i == 0:
            rows.append("|" + "|".join(["---"] * len(cells)) + "|")
    return "\n".join(rows)


def _detect_content_type(full_text: str, filename: str) -> str:
    """Detect content type from text, with filename fallback."""
    detected = content_detector.detect_content_type(full_text)
    ct = _CONTENT_TYPE_MAP.get(detected, "Word Document")
    if ct == "Word Document":
        fname_lower = filename.lower()
        for content_type, keywords in _FNAME_KEYWORDS.items():
            if any(k in fname_lower for k in keywords):
                return content_type
    return ct


def convert(file_path: str, filename: str, process_date: str, file_slug: str = "") -> tuple[str, dict]:
    doc = Document(file_path)

    title = doc.core_properties.title or filename
    author = doc.core_properties.author or ""

    lines = []
    full_text_parts = []
    para_count = 0

    for element in doc.element.body:
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        if tag == "p":
            from docx.text.paragraph import Paragraph
            para = Paragraph(element, doc)
            style_name = para.style.name.lower() if para.style else ""
            text = para.text.strip()
            if not text:
                continue
            full_text_parts.append(text)
            if style_name in _HEADING_STYLES:
                prefix = _LEVEL_MAP.get(style_name, "##")
                lines.append(f"\n{prefix} {text}\n")
            else:
                lines.append(text)
                lines.append("")
            para_count += 1
        elif tag == "tbl":
            from docx.table import Table
            tbl = Table(element, doc)
            lines.append(_table_to_md(tbl))
            lines.append("")

    full_text = "\n".join(full_text_parts)
    content_type = _detect_content_type(full_text, filename)

    # Detect tax forms
    form, form_name = "", ""
    if re.search(r'\b(form\s*1040)\b', full_text, re.IGNORECASE):
        form, form_name = "1040", "U.S. Individual Income Tax Return"
        content_type = "Tax Form"
    elif re.search(r'\b(form\s*w[-\s]?2)\b', full_text, re.IGNORECASE):
        form, form_name = "W-2", "Wage and Tax Statement"
        content_type = "Tax Form"

    # Detect case numbers
    case_matches = re.findall(
        r'(?:case\s*(?:no|number|#)\.?\s*[:\-]?\s*)([A-Z0-9\-]{4,20})\b',
        full_text[:3000], re.IGNORECASE
    )

    # Extract year
    years = re.findall(r'\b(20\d{2}|19\d{2})\b', full_text[:2000])
    tax_year = years[0] if years else ""

    combined = "\n".join(lines)

    # Promote medical section headers in plain text (not Word heading style)
    if content_type == "Medical Document":
        combined = _MEDICAL_SECTION_RE.sub(
            lambda m: f"\n#### {m.group(0).strip().rstrip(':.')}\n", combined
        )

    meta = {
        "potential_titles": [title] if title != filename else [],
        "form": form,
        "form_name": form_name,
        "tax_year": tax_year if content_type == "Tax Form" else "",
        "case_numbers": list(set(case_matches))[:3],
        "page_count": para_count,
        "author": author,
        "content_type": content_type,
        "scanned_ratio": 0,
        "ocr_pages": 0,
        "ocr_available": False,
        "integrity": integrity.check_numbers(full_text, combined),
    }

    return combined, meta
