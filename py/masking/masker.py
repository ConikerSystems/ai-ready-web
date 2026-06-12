import re
from dataclasses import dataclass, field
from typing import Literal

MaskMode = Literal["full", "soft", "none"]


def _luhn_ok(s: str) -> bool:
    """True if the digit string passes the Luhn checksum and is a plausible
    payment-card length (13-19 digits). Used to tell real card numbers from
    incidental grids of 4-digit numbers in financial/tax tables."""
    digits = re.sub(r"\D", "", s)
    if not (13 <= len(digits) <= 19):
        return False
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _valid_ssn(area: str, group: str, serial: str) -> bool:
    """True if (area, group, serial) is a structurally valid SSN. Used to gate the
    ambiguous space-separated form so it only fires on real SSNs."""
    try:
        a, g, s = int(area), int(group), int(serial)
    except ValueError:
        return False
    if a == 0 or a == 666 or a >= 900:
        return False
    if g == 0 or s == 0:
        return False
    return True


def _ssn_context(m) -> bool:
    """True only when an SSN cue ('ssn' / 'social security' / 'social') sits just
    before or after a bare 9-digit run, so routing numbers, EINs, and other
    9-digit IDs aren't masked as Social Security numbers. The dashed SSN form is
    unambiguous and skips this gate."""
    s = m.string
    before = s[max(0, m.start() - 40):m.start()].lower()
    after = s[m.end():m.end() + 20].lower()
    return bool(re.search(r"\b(?:ssn|social security|social)\b", before + " " + after))


@dataclass
class MaskStat:
    label: str
    count: int = 0
    full_pattern: str = ""
    soft_pattern: str = ""


# Regex patterns: (name, compiled_regex, full_replacement, soft_replacement)
_PATTERNS = [
    (
        "Social Security Number",
        # Boundaries are digit-only lookarounds, not \b: \b fails when extraction
        # glues adjacent text onto the SSN with no space ("390-90-6555Page2"),
        # letting a real SSN leak. (?<!\d)/(?!\d) still refuse to match inside a
        # longer digit run but catch the SSN when a letter abuts it.
        re.compile(r"(?<!\d)(\d{3})-(\d{2})-(\d{4})(?!\d)"),
        lambda m: "XXX-XX-XXXX",
        lambda m: f"XXX-XX-{m.group(3)}",
    ),
    (
        # Space-separated SSN as printed in IRS form SSN boxes: "390 90 6555".
        # Gated by SSN validity (area not 000/666/900-999, group/serial not 00/0000)
        # so it doesn't swallow three unrelated numbers in a financial table.
        "Social Security Number (spaced)",
        re.compile(r"\b(\d{3})\s(\d{2})\s(\d{4})\b"),
        lambda m: "XXX XX XXXX",
        lambda m: f"XXX XX {m.group(3)}",
        lambda m: _valid_ssn(m.group(1), m.group(2), m.group(3)),
    ),
    (
        # A bare 9-digit run is only treated as an SSN when an SSN cue sits nearby
        # (see _ssn_context). Without that gate it would also swallow 9-digit
        # routing numbers, EINs, and other IDs. The dashed form above is
        # unambiguous and needs no such gate.
        "Social Security Number (no dashes)",
        re.compile(r"\b(\d{3})(\d{2})(\d{4})\b"),
        lambda m: "XXXXXXXXX",
        lambda m: f"XXXXX{m.group(3)}",
        lambda m: _ssn_context(m),
    ),
    (
        # Payment-card numbers written as 4-4-4-4 (or 4-4-4) groups. Validated
        # with the Luhn checksum so grids of 4-digit numbers in tax/financial
        # tables (e.g. a K-1 row "1732 7295 3510 1543") are NOT mistaken for a
        # card and destroyed. Real issued cards always pass Luhn.
        "Card Number",
        re.compile(r"\b(\d{4})[- ](\d{4})[- ](\d{4})(?:[- ](\d{4}))?\b"),
        lambda m: "XXXX-XXXX-XXXX" if not m.group(4) else "XXXX-XXXX-XXXX-XXXX",
        lambda m: f"XXXX-XXXX-XXXX-{m.group(4)}" if m.group(4) else f"XXXX-XXXX-{m.group(3)}",
        lambda m: _luhn_ok(m.group(0)),
    ),
    (
        # Account numbers anchored to an explicit "Account/Acct" label so we
        # don't touch unlabeled figures. Catches "Account 910120649451",
        # "Account No: 1234-5678", and 1040 direct-deposit account numbers.
        "Account Number",
        re.compile(
            r"\b(?:Account|Acct)\.?\s*(?:Number|No\.?|#)?\s*[:#]?\s*(\d[\d\-]{5,18}\d)\b",
            re.IGNORECASE,
        ),
        lambda m: m.group(0).replace(m.group(1), "X" * len(m.group(1).replace("-", ""))),
        lambda m: m.group(0).replace(m.group(1), "XXXX" + re.sub(r"\D", "", m.group(1))[-4:]),
    ),
    (
        "Date of Birth",
        re.compile(
            r"\b(?:DOB|Date\s+of\s+Birth|Birth\s+(?:Date|Day)|Born(?:\s+on)?|Birthday)"
            r"[:\s]+(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})\b",
            re.IGNORECASE,
        ),
        lambda m: re.sub(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', '[DOB MASKED]', m.group(0)),
        lambda m: re.sub(r'\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}', '[DOB MASKED]', m.group(0)),
    ),
    (
        "Date of Birth (ISO)",
        re.compile(
            r"\b(?:DOB|Date\s+of\s+Birth|Birth\s+(?:Date|Day)|Born(?:\s+on)?|Birthday)"
            r"[:\s]+(\d{4})[/\-](\d{2})[/\-](\d{2})\b",
            re.IGNORECASE,
        ),
        lambda m: re.sub(r'\d{4}[/\-]\d{2}[/\-]\d{2}', '[DOB MASKED]', m.group(0)),
        lambda m: re.sub(r'\d{4}[/\-]\d{2}[/\-]\d{2}', '[DOB MASKED]', m.group(0)),
    ),
    (
        "Date of Birth (compact)",
        re.compile(
            r"\b(?:DOB|Date\s+of\s+Birth|Birth\s+(?:Date|Day)|Born(?:\s+on)?|Birthday)"
            r"[:\s]+(\d{8})\b",
            re.IGNORECASE,
        ),
        lambda m: re.sub(r'\d{8}', '[DOB MASKED]', m.group(0)),
        lambda m: re.sub(r'\d{8}', '[DOB MASKED]', m.group(0)),
    ),
    (
        "Medical Record Number",
        re.compile(r"\b(?:MRN|Medical Record(?:\s+No\.?|\s+Number)?)[:\s#]+([A-Z0-9]{4,12})\b", re.IGNORECASE),
        lambda m: m.group(0).replace(m.group(1), "X" * len(m.group(1))),
        lambda m: m.group(0).replace(m.group(1), "XXXX" + m.group(1)[-4:]),
    ),
    (
        # Handles "Routing Number 101205681", "ABA 101205681", and the IRS
        # direct-deposit wording "Routing Transit Number: 101205681".
        "Routing Number",
        re.compile(
            r"\b(?:Routing(?:\s+Transit)?(?:\s+(?:Number|No\.?|#))?|ABA)"
            r"[:\s#]*(\d{9})\b",
            re.IGNORECASE,
        ),
        lambda m: m.group(0).replace(m.group(1), "XXXXXXXXX"),
        lambda m: m.group(0).replace(m.group(1), "XXXXX" + m.group(1)[-4:]),
    ),
    (
        # Bank account numbers: a long contiguous digit run (12-17 digits). SSNs
        # are 9, routing 9, payment cards 13-16 but those are caught above with
        # their own (often grouped) patterns; a bare 12-17 digit block is almost
        # always a bank/deposit account number. Tax dollar amounts never reach 12
        # contiguous digits, so this won't touch financial values.
        "Bank Account Number",
        re.compile(r"\b(\d{12,17})\b"),
        lambda m: "X" * len(m.group(1)),
        lambda m: "X" * (len(m.group(1)) - 4) + m.group(1)[-4:],
    ),
    (
        "Personal Name",
        re.compile(
            # Require a colon after the label so "my name Chad" doesn't fire.
            # Matches "Name: John Smith", "Patient Name: Jane Doe" etc.
            # but NOT "my name is" or "in the name of" in running prose.
            # The colon and the name must be on the SAME line ([ \t] not \s) so a
            # bare label like "Patient name:" doesn't swallow the NEXT line's
            # field label (e.g. "Scan date:") as if it were a person's name.
            r'\b(?:(?:Your\s+)?(?:First\s+and\s+(?:Middle\s+)?|Full\s+)?Name'
            r'|Last\s+Name|Taxpayer(?:\s+Name)?|Patient(?:\s+Name)?'
            r'|Client(?:\s+Name)?|Employee(?:\s+Name)?|Insured(?:\s+Name)?'
            r'|Beneficiary(?:\s+Name)?|Account\s+Holder)'
            r'[ \t]*:[ \t]*([A-Z][a-zA-Z\'\-]+(?:[ \t]+[A-Z]\.?)?[ \t]+[A-Z][a-zA-Z\'\-]+)\b',
            re.IGNORECASE,
        ),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: _soft_name(m),
    ),
    (
        # Email address. Universal PII — never document-type specific. Soft mode
        # keeps the domain (useful for "who is this from") but drops the local part.
        "Email Address",
        re.compile(r"\b([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"),
        lambda m: "[EMAIL MASKED]",
        lambda m: f"[email]@{m.group(2)}",
    ),
    (
        # US phone numbers. Restricted to parenthesized area codes "(203) 658-8456"
        # or dash/dot-separated "203-658-8456" / "203.658.8456" forms — deliberately
        # NOT the all-space "203 658 8456" form, so columns of figures in tax and
        # financial tables are never mistaken for phone numbers. A leading +1 is
        # allowed. The (?<![\d$]) / (?!\d) boundaries refuse to fire inside a longer
        # number or a dollar amount.
        "Phone Number",
        re.compile(
            r"(?<![\d$])(?:\+?1[-.\s])?"
            r"(?:\(\d{3}\)\s?\d{3}[-.\s]\d{4}|\d{3}[-.]\d{3}[-.]\d{4})(?!\d)"
        ),
        lambda m: "[PHONE MASKED]",
        lambda m: "[PHONE MASKED]",
    ),
    (
        # Street address: a house number, up to four capitalized words, and a street
        # suffix (St, Ave, Drive, Park, …), with an optional unit (Suite/Apt/#). The
        # capitalized-word requirement keeps it from spanning ordinary lowercase
        # prose; the suffix list keeps it anchored to real addresses.
        "Street Address",
        re.compile(
            r"\b\d{1,6}\s+(?:[A-Z][A-Za-z.'\-]+\s+){0,4}"
            r"(?:Street|St|Avenue|Ave|Boulevard|Blvd|Road|Rd|Drive|Dr|Lane|Ln|"
            r"Court|Ct|Place|Pl|Way|Terrace|Ter|Circle|Cir|Highway|Hwy|Parkway|Pkwy|"
            r"Square|Sq|Trail|Trl|Park|Plaza|Row)\b\.?"
            r"(?:,?\s*(?:#|Suite|Ste|Unit|Apt|Floor|Fl|Rm|Room|No)\.?\s*[\w\-]+)?"
        ),
        lambda m: "[ADDRESS MASKED]",
        lambda m: "[ADDRESS MASKED]",
    ),
    # ── Legal personal names ────────────────────────────────────────────────
    # Names in legal documents live in prose and signature/appearance blocks, not
    # labeled "Name:" form fields, so the labeled pattern above never catches them.
    # Each of these is anchored to an unambiguous legal marker (Esq., "having been
    # sworn", "/s/", a litigation role) so they fire on attorneys, witnesses, and
    # named parties — but NOT on ordinary capitalized words in running text. All
    # share the "Personal Name" label so counts roll up with the labeled pattern.
    (
        "Personal Name",
        re.compile(r"\b([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){1,3}),?\s+(?:Esq|ESQ)\b\.?"),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        "Personal Name",
        re.compile(
            r"\b([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){1,3}),?"
            r"\s+[Hh]aving\s+been\s+(?:duly\s+)?sworn"
        ),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        "Personal Name",
        re.compile(r"/s/\s*([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){1,3})"),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        "Personal Name",
        re.compile(
            r"\b([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){1,3}),"
            r"\s+(?:Claimant|Respondent|Petitioner|Defendant|Plaintiff|Appellant|"
            r"Appellee|Movant|Deponent|Affiant)\b"
        ),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        # Deposition/transcript speaker tags: "MR. PASTORE:", "BY MS. SHOFI:",
        # "DR. KRUSE :". Anchored on an honorific + a colon so it fires on speaker
        # labels (where the name recurs dozens of times through Q&A) but NOT on an
        # honorific used in ordinary prose ("asked Mr. Pastore about the memo").
        "Personal Name",
        re.compile(r"\b(?:MR|MS|MRS|DR|MISS)\.?\s+([A-Z][A-Za-z'\-]+)(?=\s*:)"),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), m.group(1)[0] + "."),
    ),
    (
        # LLC / contract parties: "by and between Aleli Coniker", "by and among
        # John Smith and ...". Anchored to the contract recital phrase (never seen
        # in tax tables), so it won't fire on a bare "between" in financial prose.
        "Personal Name",
        re.compile(r"\bby\s+and\s+(?:between|among)\s+([A-Z][a-z][A-Za-z'\-]*(?:\s+[A-Z]\.)?\s+[A-Z][a-z][A-Za-z'\-]*)"),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        # LLC operating-agreement member/manager designations:
        # "Aleli Coniker, as a Member", "Jane Doe, as Managing Manager". The comma
        # before "as" and two capitalized name tokens keep it off body prose like
        # "the Company as a member" (no comma, single capitalized word). Role word
        # is case-insensitive ('Member' or 'member'); name tokens stay case-exact.
        "Personal Name",
        re.compile(
            r"\b([A-Z][a-z][A-Za-z'\-]*(?:\s+[A-Z]\.)?\s+[A-Z][a-z][A-Za-z'\-]*),"
            r"\s+as\s+(?:an?\s+|the\s+|its\s+|sole\s+)*(?:[Mm]anaging\s+)?(?:[Mm]ember|[Mm]anager)s?\b"
        ),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
    (
        # Signature-block / party-line titles: "Joseph V. Coniker, Manager",
        # "Jane Doe, Trustee". Like the litigation-role pattern but for the LLC /
        # trust titles that follow a name directly after a comma (no "as"). Mainly
        # valuable because it *discovers* the name, so the sweep below masks the
        # signature line and every other recurrence too.
        "Personal Name",
        re.compile(
            r"\b([A-Z][a-z][A-Za-z'\-]*(?:\s+[A-Z]\.)?\s+[A-Z][a-z][A-Za-z'\-]*),"
            r"\s+(?:Managing\s+Member|Sole\s+Member|Managing\s+Manager|Member|Manager|"
            r"Trustee|Grantor|Settlor|Authorized\s+(?:Member|Signatory|Representative))\b"
        ),
        lambda m: m.group(0).replace(m.group(1), "[NAME MASKED]"),
        lambda m: m.group(0).replace(m.group(1), _first_last_initial(m.group(1))),
    ),
]


def _first_last_initial(name: str) -> str:
    """Soft-mode legal name: first name + last initial (handles ALL-CAPS source).
    'JOSEPH M. PASTORE' -> 'Joseph P.'  ·  'Coniker' -> 'C.'"""
    parts = [p for p in name.replace(".", "").split() if p]
    if len(parts) >= 2:
        return f"{parts[0].title()} {parts[-1][0].upper()}."
    return f"{parts[0][0].upper()}." if parts else name


_NAME_TOKEN_RE = re.compile(r"[A-Z][a-z][A-Za-z'\-]*$")   # a Titlecase full word
_INITIAL_RE = re.compile(r"[A-Z]\.?$")                     # a middle initial


def _clean_name(nm: str):
    """Return nm only if it is a real 2-3 token personal name safe to sweep
    document-wide (First [M.] Last). Rejects ALL-CAPS entity/series/header words
    ('BELLES EDUCATION', 'CONIKER SYSTEMS'), OCR garbage ('AAT', 'Uisatdtn'), and
    over-captured runs — so the batch-wide sweep can never mask a company name.
    A real name is First [M.] Last: first/last are Titlecase words and the middle,
    if present, must be an INITIAL — so 'Cabin Aleli Coniker' (middle is a full
    word) and 'Two Aleli Coniker' are rejected, but 'Joseph V. Coniker' is kept."""
    toks = nm.split()
    if len(toks) == 2:
        return nm if (_NAME_TOKEN_RE.match(toks[0]) and _NAME_TOKEN_RE.match(toks[1])) else None
    if len(toks) == 3:
        if (_NAME_TOKEN_RE.match(toks[0]) and _INITIAL_RE.match(toks[1])
                and _NAME_TOKEN_RE.match(toks[2])):
            return nm
    return None


def _soft_name(m: re.Match) -> str:
    """Replace name with First L. format — first name + last initial only."""
    full_name = m.group(1).strip()
    parts = full_name.split()
    if len(parts) >= 2:
        masked = f"{parts[0]} {parts[-1][0]}."
    else:
        masked = f"{parts[0][0]}."
    return m.group(0).replace(full_name, masked)

# Standalone date pattern (only after DOB-labeled pattern fails — catches bare dates in context)
_BARE_DATE = re.compile(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b")


# "Hard PII" — unambiguous, high-harm identifiers that should never sit in a file
# even when the rest of the text is kept verbatim. Transcripts mask ONLY these.
HARD_PII_LABELS = {
    "Social Security Number",
    "Social Security Number (no dashes)",
    "Card Number",
}


# Human-friendly category labels for the "Preserve Original" warning — collapses
# the internal pattern variants (spaced/ISO/compact SSN, etc.) into the plain
# categories a user understands. Mirrors the categories in _PATTERNS.
_CATEGORY_LABELS = [
    "Social Security Numbers",
    "Card numbers",
    "Bank, account & routing numbers",
    "Dates of birth",
    "Medical record numbers",
    "Personal names",
    "Email addresses",
    "Phone numbers",
    "Street addresses",
]


def category_labels() -> list[str]:
    """The PII categories the built-in masker removes — used to warn the user
    exactly what is NOT removed when Preserve Original is ON."""
    return list(_CATEGORY_LABELS)


def sweep_names(text: str, names, mode: MaskMode) -> tuple[str, int]:
    """Mask every plaintext occurrence of each full name in `names`. Longest
    names first so 'Joseph V. Coniker' is handled before 'Aleli Coniker', and
    letter boundaries keep 'Coniker' inside the entity 'Coniker Systems' intact.
    Used both for the per-document sweep and the batch-wide sweep (a name an
    anchor identified in one file is masked across the whole run). Returns
    (new_text, total_occurrences_masked). No-op when mode is 'none'."""
    if mode == "none" or not names:
        return text, 0
    total = 0
    for nm in sorted(names, key=len, reverse=True):
        toks = nm.split()
        if len(toks) < 2 or len(nm) < 5:
            continue
        sweep = re.compile(
            r"(?<![A-Za-z])" + r"\s+".join(re.escape(t) for t in toks) + r"(?![A-Za-z])"
        )
        hits = sweep.findall(text)
        if not hits:
            continue
        total += len(hits)
        text = sweep.sub("[NAME MASKED]" if mode == "full" else _first_last_initial(nm), text)
    return text, total


def mask_text(text: str, mode: MaskMode, only: set | None = None,
              collect: set | None = None) -> tuple[str, list[MaskStat]]:
    """Mask PII in text. By default every built-in pattern runs (used for
    documents). Pass `only` (e.g. HARD_PII_LABELS) to restrict masking to that
    subset of pattern labels — used for transcripts, which stay verbatim apart
    from hard PII plus the user's own replacements.

    `collect`: optional set the caller passes in to RECEIVE the full personal
    names this call discovered via anchors. The batch worker accumulates these
    across files, then runs a final `sweep_names` over the combined output so a
    name identified in one document is masked in every document of the run.
    Purely additive — when omitted, behavior is identical to before."""
    if mode == "none":
        return text, []

    stats: dict[str, MaskStat] = {}
    discovered_names: set[str] = set()   # multi-token names found via anchors

    def apply(name: str, pattern: re.Pattern, repl_full, repl_soft, validator=None) -> None:
        nonlocal text
        if only is not None and name not in only:
            return
        replacement = repl_full if mode == "full" else repl_soft
        matches = [m for m in pattern.finditer(text) if validator is None or validator(m)]
        if not matches:
            return
        # Remember every full (>=2 token) personal name an anchor identified, so the
        # sweep below can mask its OTHER occurrences (succession lists, signature
        # lines, schedules, depo speaker tags) that no single anchor reaches.
        if name == "Personal Name":
            for m in matches:
                try:
                    nm = re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".,;:")
                except (IndexError, re.error):
                    continue
                cn = _clean_name(nm)
                if cn:
                    discovered_names.add(cn)
        if name not in stats:
            stats[name] = MaskStat(label=name, count=0)
        stats[name].count += len(matches)
        if validator is None:
            text = pattern.sub(replacement, text)
        else:
            # Only replace matches that pass the validator; leave others intact.
            text = pattern.sub(lambda m: replacement(m) if validator(m) else m.group(0), text)

    for entry in _PATTERNS:
        name, pattern, repl_full, repl_soft = entry[0], entry[1], entry[2], entry[3]
        validator = entry[4] if len(entry) > 4 else None
        apply(name, pattern, repl_full, repl_soft, validator)

    # ── Discovered-name sweep (within this document) ─────────────────────────
    # A name only had to be identified ONCE (by an Esq./sworn/"as a Member"/etc.
    # anchor) for every other plaintext occurrence of that same full name to be a
    # leak. Mask them all here, and hand the names up to `collect` so the batch
    # worker can sweep them across the OTHER documents in the run too.
    if discovered_names and (only is None or "Personal Name" in only):
        if collect is not None:
            collect |= discovered_names
        text, swept = sweep_names(text, discovered_names, mode)
        if swept:
            if "Personal Name" not in stats:
                stats["Personal Name"] = MaskStat(label="Personal Name", count=0)
            stats["Personal Name"].count += swept

    return text, list(stats.values())
