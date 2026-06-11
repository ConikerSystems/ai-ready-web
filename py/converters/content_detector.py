"""Shared document content-type detection and entity extraction."""
import re

_TYPE_TERMS: dict[str, list[str]] = {
    "medical": [
        "diagnosis", "icd-10", "icd-9", "prescription", "medication", "dosage",
        "patient", "physician", "clinical", "treatment", "procedure",
        "symptom", "discharge", "admission", "outpatient", "inpatient",
        "provider", "clinic", "hospital", "pharmacy", "laboratory",
        "radiology", "pathology", "referral", "encounter", "medical record",
        "chief complaint", "date of service", "vital signs", "chronic", "acute",
        # Lab result patterns
        "reference range", "reference interval", "specimen", "collection date",
        "result", "lab result", "lab report", "test result",
        "CBC", "WBC", "RBC", "HGB", "HCT", "MCV", "MCH", "MCHC", "RDW",
        "platelet", "hemoglobin", "hematocrit", "neutrophil", "lymphocyte",
        "glucose", "creatinine", "BUN", "eGFR", "sodium", "potassium",
        "cholesterol", "triglyceride", "HDL", "LDL", "TSH", "T3", "T4",
        "urinalysis", "urine", "blood panel", "metabolic panel", "lipid panel",
        # Radiology / imaging
        "impression", "findings", "ct scan", "mri", "ultrasound", "x-ray",
        "radiograph", "imaging", "scan", "contrast", "mass", "lesion", "nodule",
        # Clinical note sections
        "HPI", "chief complaint", "review of systems", "assessment and plan",
        "physical examination", "past medical history", "family history",
        "social history", "allergies", "current medications",
        # Genomic / specialty labs
        "genotype", "allele", "variant", "mutation", "genomic", "DNA",
        "amino acid", "marker", "biomarker", "panel", "assay",
    ],
    "legal": [
        "plaintiff", "defendant", "petitioner", "respondent",
        "whereas", "herein", "hereby", "hereunder",
        "agreement", "contract", "covenant", "indemnif",
        "jurisdiction", "governing law", "arbitration",
        "attorney", "counsel", "docket", "judgment", "motion",
        "complaint", "affidavit", "deposition", "settlement",
        "executed", "notary", "grantor", "grantee",
        "lien", "encumbrance", "easement",
    ],
    "financial": [
        "balance sheet", "income statement", "statement of operations",
        "cash flow", "revenue", "net income", "gross profit",
        "ebitda", "fiscal year", "quarterly",
        "total assets", "total liabilities", "stockholders equity",
        "accounts receivable", "accounts payable",
        "depreciation", "amortization", "working capital",
        "audit", "gaap", "ifrs", "earnings per share",
        "shareholder", "dividend", "capital expenditure",
    ],
    "real_estate": [
        "parcel", "assessor parcel", "apn",
        "legal description", "square feet", "sq ft",
        "acreage", "zoning", "assessed value", "appraised value",
        "deed of trust", "escrow", "closing",
        "comparable", "market value", "real property",
        "lot size", "property tax", "title insurance",
    ],
    "insurance": [
        "policy number", "insured", "premium", "deductible",
        "coverage", "claim", "claimant", "adjuster",
        "endorsement", "rider", "exclusion",
        "underwriter", "beneficiary", "policyholder", "insurer",
        "declaration page", "explanation of benefits",
    ],
}

_THRESHOLD = 3

_DATE_RE = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|'
    r'October|November|December)\s+\d{1,2},?\s+\d{4}'
    r'|\b\d{1,2}/\d{1,2}/(?:\d{2}|\d{4})'
    r'|\b\d{4}-\d{2}-\d{2}\b',
    re.IGNORECASE,
)

_ICD10_RE = re.compile(r'\b([A-Z]\d{2}(?:\.\d{1,4})?)\b')
_ICD10_VALID_PREFIXES = set("ABCDEFGHIJKLMNOPQRSTVWXYZ")

_CASE_NO_RE = re.compile(
    r'(?:case\s*(?:no|number|#)\.?\s*[:\-]?\s*|docket\s*(?:no|#)?\s*[:\-]?\s*)'
    r'([A-Z0-9\-:]{4,25})',
    re.IGNORECASE,
)

_PARTY_PATTERNS = [
    (r'(?:plaintiff|petitioner)\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,60})', "Plaintiff"),
    (r'(?:defendant|respondent)\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,60})', "Defendant"),
    (r'grantor\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,60})', "Grantor"),
    (r'grantee\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,60})', "Grantee"),
    (r'(?:patient|patient\s+name)\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,40})', "Patient"),
    (r'(?:provider|physician|doctor|dr\.)\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,50})', "Provider"),
    (r'employer\s*[:\-]\s*([A-Z][A-Za-z ,\.&]{2,60})', "Employer"),
    (r'(?:insured|policyholder)\s*[:\-]\s*([A-Z][A-Za-z ,\.]{2,60})', "Insured"),
]


def detect_content_type(text: str) -> str:
    """Score text against domain term lists; return best match or 'generic'."""
    text_lower = text.lower()
    scores: dict[str, int] = {
        ctype: sum(1 for t in terms if t in text_lower)
        for ctype, terms in _TYPE_TERMS.items()
    }
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] >= _THRESHOLD else "generic"


def extract_dates(text: str, max_results: int = 20) -> list[str]:
    seen: dict[str, None] = {}
    for m in _DATE_RE.finditer(text):
        seen[m.group()] = None
        if len(seen) >= max_results:
            break
    return list(seen)


def extract_icd_codes(text: str) -> list[str]:
    seen: dict[str, None] = {}
    for m in _ICD10_RE.finditer(text):
        code = m.group(1)
        if code[0] in _ICD10_VALID_PREFIXES and len(code) >= 3:
            seen[code] = None
    return list(seen)[:20]


def extract_case_numbers(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1) for m in _CASE_NO_RE.finditer(text)))[:5]


def extract_parties(text: str) -> list[tuple[str, str]]:
    """Return list of (role, name) pairs found in text."""
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for pattern, role in _PARTY_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
            name = m.group(1).strip().rstrip(",.")
            if name not in seen and len(name) > 3:
                results.append((role, name))
                seen.add(name)
    return results[:10]


def extract_dollar_context(text: str, max_results: int = 30) -> list[tuple[str, str]]:
    """Return [(context_label, amount_string), ...] by document position."""
    results: list[tuple[str, str]] = []
    for m in re.finditer(r'\$[\d,]+(?:\.\d{2})?|\b\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b', text):
        start = max(0, m.start() - 70)
        ctx = text[start:m.start()].strip().split("\n")[-1].strip()
        if ctx and not ctx.isspace():
            results.append((ctx[-55:], m.group()))
        if len(results) >= max_results:
            break
    return results


def extract_section_headings(text: str, max_results: int = 30) -> list[str]:
    """Extract numbered sections, article headers, and all-caps headings."""
    patterns = [
        r'^\s*(?:\d+\.)+\s+([A-Z][A-Za-z &\-]{3,60})',
        r'^\s*(?:ARTICLE|SECTION|PART)\s+[IVXivx\d]+[:\.\s]+(.{4,60})',
        r'^([A-Z][A-Z\s&\-]{5,50})$',
    ]
    headings: list[str] = []
    seen: set[str] = set()
    for pat in patterns:
        for m in re.finditer(pat, text, re.MULTILINE):
            h = (m.group(1) if m.lastindex else m.group()).strip()
            if h not in seen:
                headings.append(h)
                seen.add(h)
    return headings[:max_results]
