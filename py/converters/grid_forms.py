"""
AI Ready — Grid-form extraction (Schedule E, D, Form 8949, ...)

Tax schedules with a GRID layout report one line across several value columns —
e.g. Schedule E lists each rental property in its own column (A/B/C), so a single
line like "Rents received" has up to three values. The 1040 single-value-column
extractor can't represent that. This module extracts one (line × column) CELL per
value and tags it, so every value is independently auditable:

    | Schedule E | line 3 | Rents received | Property A | 50,879. | p.32 |
    | Schedule E | line 3 | Rents received | Property B | 12,889. | p.32 |

Geometry (independent of the 1040 path): per page it finds the real value COLUMNS
by x-clustering (a column must have several values, so stray description digits and
the line-number box don't form one), then assigns each value to (nearest line label
to its left on the same row) × (which column its x falls in). Generalizable: works
for any property-column or data-column grid with the same layout characteristics.
"""

import re

# A money token must look formatted: a thousands comma, a trailing dot, or a sign.
# This matches both "23,334." (dotted) and "1,022,715" (H&R Block, no dot) while
# REJECTING bare unsigned integers ("8", "21", "1040") — those are line-number
# echoes/form numbers, never amounts, and letting them through put garbage in the
# grid columns. The cost is a few tiny un-formatted H&R Block values (e.g. a $7
# expense) are skipped — preferred over emitting a wrong number.
_MONEY = re.compile(
    r'^(?:'
    r'-?\d{1,3}(?:,\d{3})+\.?'        # comma group, any sign: 72,622  -31,572  1,830.
    r'|-?\d{1,3}(?:,\d{3})*\.'        # ends with a dot:        5.  0.  23,334.
    r'|-\d{1,3}(?:,\d{3})*'           # signed bare integer:    -5
    r'|\(\d{1,3}(?:,\d{3})*\.?\)'     # parenthesised negative: (5)  (1,234)
    r')$')
# A bare 1-3 digit integer: AMBIGUOUS — could be a small amount ("7") or a line-
# number echo ("7"). It is treated as a value ONLY when it lands in a column that
# already contains formatted money (see _in_money_column), so an echo sitting in
# the line-number band is never admitted. 4+ digit bare integers are years/form
# numbers, never amounts, so they are excluded here too.
_WEAK_INT = re.compile(r'^\d{1,3}$')
_DATE = re.compile(r'^\d{2}/\d{2}/\d{2}$')


# ── Schedule E (Form 1040) — Part I, per-property rental grid (lines 3-22) ────
_SCHED_E_LINES = {
    "3":  "Rents received",
    "4":  "Royalties received",
    "5":  "Advertising",
    "6":  "Auto and travel",
    "7":  "Cleaning and maintenance",
    "8":  "Commissions",
    "9":  "Insurance",
    "10": "Legal and other professional fees",
    "11": "Management fees",
    "12": "Mortgage interest paid to banks, etc.",
    "13": "Other interest",
    "14": "Repairs",
    "15": "Supplies",
    "16": "Taxes",
    "17": "Utilities",
    "18": "Depreciation expense or depletion",
    "19": "Other expenses",
    "20": "Total expenses (add lines 5 through 19)",
    "21": "Subtract line 20 from line 3 (rents) or 4 (royalties)",
    "22": "Deductible rental real estate loss after limitation (Form 8582)",
}

# Schedule E Part I — the schedule-wide totals block (single column) below the
# per-property grid. 23a-e are column sums across ALL properties; 24-26 roll them
# into the rental real estate income/(loss). On a multi-page Part I these are filled
# only on the primary page (the continuation pages leave the block blank).
_SCHED_E_TOTALS = {
    "23a": "Total of all amounts reported on line 3 for all rental properties",
    "23b": "Total of all amounts reported on line 4 for all royalty properties",
    "23c": "Total of all amounts reported on line 12 for all properties",
    "23d": "Total of all amounts reported on line 18 for all properties",
    "23e": "Total of all amounts reported on line 20 for all properties",
    "24":  "Income. Add positive amounts shown on line 21. Do not include any losses",
    "25":  "Losses. Add royalty losses from line 21 and real estate losses from line 22",
    "26":  "Total rental real estate and royalty income or (loss). Combine lines 24 and 25",
}

# Schedule E page 2 — the single-column summary lines of Parts II-V. The detail
# rows (28-29 partnerships, 33-34 estates/trusts, 38 REMICs) are multi-column grids
# handled separately; these are the per-part totals and the line 41 grand total.
_SCHED_E_PAGE2 = {
    "30": "Add columns (h) and (k) of line 29a",
    "31": "Add columns (g), (i), and (j) of line 29b",
    "32": "Total partnership and S corporation income or (loss). Combine lines 30 and 31",
    "35": "Add columns (d) and (f) of line 34a",
    "36": "Add columns (c) and (e) of line 34b",
    "37": "Total estate and trust income or (loss). Combine lines 35 and 36",
    "39": "Combine columns (d) and (e) only. Include in the total on line 41 below",
    "40": "Net farm rental income or (loss) from Form 4835",
    "41": "Total income or (loss). Combine lines 26, 32, 37, 39, and 40",
    "42": "Reconciliation of farming and fishing income",
    "43": "Reconciliation for real estate professionals",
}


# ── Schedule D (Form 1040) — Capital Gains and Losses ────────────────────────
# Page 1 is a 4-value-column grid (d Proceeds / e Cost / g Adjustments /
# h Gain-loss); page 2 (Part III) is a single-column summary.
_SCHED_D_LINES = {
    "1a": "Short-term totals (1099-B/1099-DA, basis reported to IRS, no adjustments)",
    "1b": "Short-term totals from Form(s) 8949 with Box A or Box G checked",
    "2":  "Short-term totals from Form(s) 8949 with Box B or Box H checked",
    "3":  "Short-term totals from Form(s) 8949 with Box C or Box I checked",
    "4":  "Short-term gain from Form 6252 and gain or (loss) from Forms 4684, 6781, 8824",
    "5":  "Net short-term gain or (loss) from partnerships, S corps, estates, trusts (K-1)",
    "6":  "Short-term capital loss carryover",
    "7":  "Net short-term capital gain or (loss). Combine lines 1a-6 in column (h)",
    "8a": "Long-term totals (1099-B/1099-DA, basis reported to IRS, no adjustments)",
    "8b": "Long-term totals from Form(s) 8949 with Box D or Box J checked",
    "9":  "Long-term totals from Form(s) 8949 with Box E or Box K checked",
    "10": "Long-term totals from Form(s) 8949 with Box F or Box L checked",
    "11": "Gain from Form 4797 Part I; long-term gain from Forms 2439, 6252, 4684, 6781, 8824",
    "12": "Net long-term gain or (loss) from partnerships, S corps, estates, trusts (K-1)",
    "13": "Capital gain distributions",
    "14": "Long-term capital loss carryover",
    "15": "Net long-term capital gain or (loss). Combine lines 8a-14 in column (h)",
}
_SCHED_D_PAGE2 = {
    "16": "Combine lines 7 and 15 and enter the result",
    "18": "28% Rate Gain Worksheet amount (line 7 of that worksheet)",
    "19": "Unrecaptured Section 1250 Gain Worksheet amount (line 18 of that worksheet)",
    "21": "If line 16 is a loss, the smaller of the loss or ($3,000)/($1,500)",
}
# Column body labels keyed by the header letter.
_SCHED_D_COLS = {"d": "(d) Proceeds", "e": "(e) Cost", "g": "(g) Adjustments",
                 "h": "(h) Gain or loss"}


def _cluster(xs, gap=25):
    """Group sorted x-centers into columns separated by > `gap` points."""
    xs = sorted(xs)
    cols = [[xs[0]]]
    for x in xs[1:]:
        if x - cols[-1][-1] > gap:
            cols.append([x])
        else:
            cols[-1].append(x)
    return cols


def _is_sched_e_part1(page_text: str) -> bool:
    head = page_text[:600]
    # The Part I page carries the Schedule E form title ("Supplemental Income and
    # Loss"); page 2 (Parts II-V) does not. H&R Block puts the "Income or Loss
    # From Rental" Part I subhead lower than 600 chars, so key off the title.
    return bool(re.search(r'SCHEDULE\s+E\b', head, re.IGNORECASE)
                and (re.search(r'Supplemental\s+Income\s+and\s+Loss', head, re.IGNORECASE)
                     or re.search(r'Income\s+or\s+Loss\s+From\s+Rental', head, re.IGNORECASE)))


def _is_sched_e_page2(page_text: str) -> bool:
    head = page_text[:400]
    return bool(re.search(r'Schedule\s+E\s*\(Form\s+1040\)', head, re.IGNORECASE)
                and re.search(r'Page\s*2', head, re.IGNORECASE))


def _extract_single_col(page, line_map: dict, x_label_min: float, y_top: float = 0.0,
                        y_tol: int = 6) -> list:
    """Extract one value per known line from a single-value-column block.

    For each line label in `line_map` that appears as a right-side line-number BOX
    (xc > x_label_min · page-width — never the description-side label or an in-text
    line reference), bind the nearest money value to its right on the SAME row. A
    line with no value to its right is omitted, never invented. Returns [(line, value)].
    `y_top` (fraction of page height) clips to a block lower on the page."""
    W, H = page.rect.width, page.rect.height
    labels, values = [], []
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        xc = (x0 + x1) / 2
        yc = (y0 + y1) / 2
        if yc < H * y_top:
            continue
        t = word.strip()
        if _MONEY.match(t):
            values.append((xc, yc, t))
        elif t in line_map and xc > W * x_label_min:
            labels.append((t, xc, yc))
    out = []
    for ln, lx, ly in labels:
        cands = [(vx, vt) for vx, vy, vt in values if vx > lx and abs(vy - ly) <= y_tol]
        if not cands:
            continue
        out.append((ln, min(cands, key=lambda c: c[0] - lx)[1]))
    return out


def _extract_sched_e_totals(page) -> list:
    """Schedule E Part I totals block (lines 23a-26), single value column.
    Label boxes sit at ~x 64% (23a-e) / ~78% (24-26); the 0.62 floor excludes the
    description-side labels and the 'Combine lines 24 and 25' in-text references."""
    return _extract_single_col(page, _SCHED_E_TOTALS, 0.62, y_top=0.79)


def _extract_sched_e_page2(page) -> list:
    """Schedule E page 2 summary lines (Parts II-V). Label boxes at ~x 60-78%."""
    return _extract_single_col(page, _SCHED_E_PAGE2, 0.58)


# ── 1040 column schedules (Schedule 1, 3, ...) — single far-right value column ─
# These share Form 1040's layout: line number at the far LEFT, value at the far
# RIGHT. (Distinct from the right-label totals blocks handled by _extract_single_col.)
def _extract_left_label_col(page, line_map: dict, x_label_max: float = 0.10,
                            x_val_min: float = 0.78, y_top: float = 0.0) -> dict:
    """Extract {line: value} from a far-left-label / far-right-value form.

    Each known line label (a left-column line-number box) binds the RIGHTMOST money
    value on its row — the main value column — within a vertical window that tolerates
    the variable label-to-value offset on wrapped description lines. Lines with no
    value on their row are omitted, never invented."""
    W, H = page.rect.width, page.rect.height
    labels, values = [], []
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        xc = (x0 + x1) / 2
        yc = (y0 + y1) / 2
        if yc < H * y_top:
            continue
        t = word.strip()
        # _MONEY already rejects bare unsigned integers (line-number echoes), so a
        # value that survives here is a real amount even if small (e.g. "8.").
        if _MONEY.match(t) and xc > W * x_val_min:
            values.append((xc, yc, t))
        elif t in line_map and xc < W * x_label_max:
            labels.append((t, yc))
    byln = {}
    for vx, vy, vt in values:
        best, best_dy = None, None
        for ln, ly in labels:
            dy = vy - ly                 # value sits at, or just below, its label
            if dy < -8 or dy > 15:
                continue
            if best_dy is None or abs(dy) < abs(best_dy):
                best_dy, best = dy, ln
        if best is not None:
            byln.setdefault(best, []).append((vx, vt))
    return {ln: max(vs, key=lambda c: c[0])[1] for ln, vs in byln.items()}


def _extract_column_schedule(doc, file_slug: str, tax_year: str, form_name: str,
                             is_page, line_map: dict, column_of, x_val_min=0.78) -> list:
    """Generic extractor for a 1040 column schedule. Dedups duplicate filing/records
    copies by their (line→value) signature."""
    seen, entries = set(), []
    n = doc.page_count
    # A schedule's Part II often continues on the next page, whose own header is
    # just "Part II" (the "Schedule N (Form 1040)" id sits in the footer). Scan
    # each header-matched page AND its immediate successor — but only if that
    # successor doesn't itself start a new schedule/form, so we never bleed into
    # the next attachment.
    matched = [i for i in range(1, n + 1) if is_page(doc[i - 1].get_text())]
    pages_to_scan = set(matched)
    for i in matched:
        nxt = i + 1
        if nxt <= n and nxt not in matched:
            head = doc[nxt - 1].get_text()[:200]
            if not re.search(r'\bSCHEDULE\b', head, re.IGNORECASE) and \
               not re.match(r'\s*Form\s+\d', head, re.IGNORECASE):
                pages_to_scan.add(nxt)
    for i in sorted(pages_to_scan):
        page = doc[i - 1]
        vals = _extract_left_label_col(page, line_map, x_val_min=x_val_min)
        if not vals:
            continue
        sig = frozenset(vals.items())
        if sig in seen:
            continue
        seen.add(sig)
        anchor = f"[p.{i}](#{_anchor(file_slug, i)})"
        for ln, val in sorted(vals.items(), key=lambda r: _linekey(r[0])):
            entries.append({
                "line": f"line {ln}",
                "description": line_map.get(ln, ""),
                "column": column_of(ln),
                "value": val,
                "page": anchor,
                "tax_year": tax_year,
                "form": form_name,
            })
    return entries


_SCHED_1_LINES = {
    "1":  "Taxable refunds, credits, or offsets of state and local income taxes",
    "2a": "Alimony received",
    "3":  "Business income or (loss) (Schedule C)",
    "4":  "Other gains or (losses) (Form 4797)",
    "5":  "Rental real estate, royalties, partnerships, S corps, trusts (Schedule E)",
    "6":  "Farm income or (loss) (Schedule F)",
    "7":  "Unemployment compensation",
    "9":  "Total other income. Add lines 8a through 8z",
    "10": "Additional income. Combine lines 1-7 and 9 (to Form 1040, line 8)",
    "11": "Educator expenses",
    "12": "Certain business expenses of reservists, performing artists (Form 2106)",
    "13": "Health savings account deduction (Form 8889)",
    "14": "Moving expenses for Armed Forces (Form 3903)",
    "15": "Deductible part of self-employment tax (Schedule SE)",
    "16": "Self-employed SEP, SIMPLE, and qualified plans",
    "17": "Self-employed health insurance deduction",
    "18": "Penalty on early withdrawal of savings",
    "19a": "Alimony paid",
    "20": "IRA deduction",
    "21": "Student loan interest deduction",
    "23": "Archer MSA deduction",
    "25": "Total other adjustments. Add lines 24a through 24z",
    "26": "Total adjustments. Add lines 11-23 and 25 (to Form 1040, line 10)",
}


def _is_sched_1(page_text: str) -> bool:
    # Match on the schedule number + its distinctive subtitle rather than a literal
    # "SCHEDULE 1 (Form 1040)" adjacency — H&R Block prints the OMB/agency lines
    # between "SCHEDULE 1" and "(Form 1040)".
    head = page_text[:250]
    if re.search(r'\bSCHEDULE\s+1\b', head, re.IGNORECASE) and \
       re.search(r'Additional\s+Income', head, re.IGNORECASE):
        return True
    # A self-identifying page-2 header (e.g. "Schedule 1 (Form 1040) 2025 Page 2").
    return bool(re.search(r'Schedule\s+1\s*\(Form\s+1040\)', head, re.IGNORECASE)
                and re.search(r'Page\s*2', head, re.IGNORECASE))


def extract_schedule_1(doc, file_slug: str, tax_year: str) -> list:
    col = lambda ln: ("Part I — income" if _linekey(ln)[0] <= 10
                      else "Part II — adjustments")
    return _extract_column_schedule(doc, file_slug, tax_year, "Schedule 1",
                                    _is_sched_1, _SCHED_1_LINES, col)


_SCHED_3_LINES = {
    "1":  "Foreign tax credit (Form 1116)",
    "2":  "Credit for child and dependent care expenses (Form 2441)",
    "3":  "Education credits (Form 8863, line 19)",
    "4":  "Retirement savings contributions credit (Form 8880)",
    "5a": "Residential clean energy credit (Form 5695, line 15)",
    "5b": "Energy efficient home improvement credit (Form 5695, line 32)",
    "7":  "Total other nonrefundable credits. Add lines 6a through 6z",
    "8":  "Add lines 1-4, 5a, 5b, and 7 (to Form 1040, line 20)",
    "9":  "Net premium tax credit (Form 8962)",
    "10": "Amount paid with request for extension to file",
    "11": "Excess social security and tier 1 RRTA tax withheld",
    "12": "Credit for federal tax on fuels (Form 4136)",
    "14": "Total other payments or refundable credits. Add lines 13a through 13z",
    "15": "Add lines 9-12 and 14 (to Form 1040, line 31)",
}


def _is_sched_3(page_text: str) -> bool:
    head = page_text[:250]
    if re.search(r'\bSCHEDULE\s+3\b', head, re.IGNORECASE) and \
       re.search(r'Additional\s+Credits\s+and\s+Payments', head, re.IGNORECASE):
        return True
    return bool(re.search(r'Schedule\s+3\s*\(Form\s+1040\)', head, re.IGNORECASE)
                and re.search(r'Page\s*2', head, re.IGNORECASE))


def extract_schedule_3(doc, file_slug: str, tax_year: str) -> list:
    col = lambda ln: ("Part I — nonrefundable credits" if _linekey(ln)[0] <= 8
                      else "Part II — payments & refundable credits")
    return _extract_column_schedule(doc, file_slug, tax_year, "Schedule 3",
                                    _is_sched_3, _SCHED_3_LINES, col)


# Form 8995 — Qualified Business Income Deduction (Simplified Computation).
# Two value columns (computation ~x74% / result ~x93%); line 1 is the per-business
# component table (its total is line 2), so it is not a single-value line.
_FORM_8995_LINES = {
    "2":  "Total qualified business income or (loss). Combine lines 1i-1v",
    "3":  "Qualified business net (loss) carryforward from the prior year",
    "4":  "Total qualified business income. Combine lines 2 and 3 (if ≤0, enter 0)",
    "5":  "Qualified business income component. Multiply line 4 by 20%",
    "6":  "Qualified REIT dividends and PTP income or (loss)",
    "7":  "Qualified REIT dividends and PTP (loss) carryforward from the prior year",
    "8":  "Total qualified REIT dividends and PTP income. Combine lines 6 and 7 (if ≤0, 0)",
    "9":  "REIT and PTP component. Multiply line 8 by 20%",
    "10": "Qualified business income deduction before income limitation. Add lines 5 and 9",
    "11": "Taxable income before qualified business income deduction",
    "12": "Net capital gain (qualified dividends + capital gain)",
    "13": "Subtract line 12 from line 11 (if ≤0, enter 0)",
    "14": "Income limitation. Multiply line 13 by 20%",
    "15": "Qualified business income deduction. Smaller of line 10 or 14 (to Form 1040, line 13)",
    "16": "Total qualified business (loss) carryforward. Combine lines 2 and 3 (if >0, 0)",
    "17": "Total qualified REIT dividends and PTP (loss) carryforward",
}


def _is_form_8995(page_text: str) -> bool:
    head = page_text[:160]
    return bool(re.search(r'Form\s+8995\b', head, re.IGNORECASE)
                and re.search(r'Simplified\s+Computation', head, re.IGNORECASE))


def extract_form_8995(doc, file_slug: str, tax_year: str) -> list:
    return _extract_column_schedule(doc, file_slug, tax_year, "Form 8995",
                                    _is_form_8995, _FORM_8995_LINES,
                                    lambda ln: "QBI computation", x_val_min=0.70)


# Schedule 8812 — Credits for Qualifying Children and Other Dependents.
_SCHED_8812_LINES = {
    "1":  "Amount from Form 1040, line 11 (adjusted gross income)",
    "2d": "Add lines 2a through 2c",
    "3":  "Add lines 1 and 2d",
    "4":  "Number of qualifying children under age 17 with the required SSN × $2,000",
    "5":  "Number of qualifying children × $2,000",
    "6":  "Number of other dependents × $500",
    "7":  "Add lines 5 and 6",
    "8":  "Add line 7 (and Puerto Rico amounts)",
    "9":  "Income threshold ($400,000 MFJ; $200,000 all others)",
    "10": "Subtract line 9 from line 3",
    "11": "Multiply line 10 by 5%",
    "12": "Credit before limitation (line 8 less line 11)",
    "13": "Tax liability limit (Credit Limit Worksheet)",
    "14": "Smaller of line 12 or 13. Child tax credit (to Form 1040, line 19)",
    "16a": "Subtract line 14 from line 12",
    "16b": "Number of qualifying children × $1,700",
    "17": "Smaller of line 16a or 16b",
    "18a": "Earned income",
    "19": "Subtract $2,500 from line 18a",
    "20": "Multiply line 19 by 15%",
    "27": "Additional child tax credit (to Form 1040, line 28)",
}


def _is_sched_8812(page_text: str) -> bool:
    head = page_text[:250]
    if re.search(r'\bSCHEDULE\s+8812\b', head, re.IGNORECASE) and \
       re.search(r'Qualifying\s+Children', head, re.IGNORECASE):
        return True
    return bool(re.search(r'Schedule\s+8812\s*\(Form\s+1040\)', head, re.IGNORECASE)
                and re.search(r'Page\s*2', head, re.IGNORECASE))


def extract_schedule_8812(doc, file_slug: str, tax_year: str) -> list:
    col = lambda ln: ("Child tax credit" if _linekey(ln)[0] <= 14
                      else "Additional child tax credit")
    return _extract_column_schedule(doc, file_slug, tax_year, "Schedule 8812",
                                    _is_sched_8812, _SCHED_8812_LINES, col)


def _is_sched_d_page1(page_text: str) -> bool:
    head = page_text[:600]
    return bool(re.search(r'SCHEDULE\s+D\b', head, re.IGNORECASE)
                and re.search(r'Capital\s+Gains\s+and\s+Losses', head, re.IGNORECASE))


def _is_sched_d_page2(page_text: str) -> bool:
    head = page_text[:300]
    return bool(re.search(r'Schedule\s+D\s*\(Form\s+1040\)', head, re.IGNORECASE)
                and re.search(r'Page\s*2', head, re.IGNORECASE))


def _extract_schedule_d_page1(page) -> list:
    """Schedule D page 1 multi-column grid → [(line, col_letter, value)].

    Columns are anchored to the (d)(e)(g)(h) HEADER letters (not value-density
    clustering — Schedule D columns are sparse). Values are right-aligned, so each
    sits at or to the right of its own header and left of the next column's header;
    a value therefore belongs to the column with the greatest header center ≤ its x.
    Line labels sit ~11 pt above their values, so each value binds to the nearest
    line label at or above it within a 20-pt window."""
    W, H = page.rect.width, page.rect.height
    headers = {}    # letter -> leftmost (true column-header) x-center
    labels = []     # (line, yc)
    strong = []     # (xc, yc, value_str) — formatted money
    weak = []       # (xc, yc, value_str) — bare ints, admitted only in a money column
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        xc = (x0 + x1) / 2
        yc = (y0 + y1) / 2
        t = word.strip()
        m = re.match(r'^\((d|e|g|h)\)$', t)
        if m and xc > W * 0.45:
            ltr = m.group(1)
            if ltr not in headers or xc < headers[ltr]:
                headers[ltr] = xc      # the real header is the leftmost occurrence
        elif _MONEY.match(t) and H * 0.20 < yc < H * 0.96 and xc > W * 0.45:
            f = _num_local(t)
            if f is not None and abs(f) >= 1:
                strong.append((xc, yc, t))
        elif _WEAK_INT.match(t) and H * 0.20 < yc < H * 0.96 and xc > W * 0.45:
            weak.append((xc, yc, t))
        elif t in _SCHED_D_LINES and xc < W * 0.09:
            # Line-number boxes sit in the far-left margin (x≈37–40). The tighter
            # 0.09 bound (was 0.15) excludes digits indented into the description,
            # e.g. the "2" in "go to Part III on page 2" (x≈87) on line 15's row —
            # which otherwise sat closer to line 15's value than line 15's own
            # label and stole it.
            labels.append((t, yc))
    if not headers or not strong:
        return []
    cols = sorted(headers.items(), key=lambda kv: kv[1])   # [('d',x),... left→right]

    def _col_of(vx):
        letter = cols[0][0]
        for hl, hx in cols:
            if hx <= vx + 2:        # right-aligned: greatest header center ≤ value
                letter = hl
        return letter

    # Bare integers are admitted ONLY in the gain/(loss) column (h), and only when
    # (h) also holds formatted money. (h) is the one Schedule D column whose values
    # are net results that can legitimately be a small whole number (e.g. line
    # 8a/15 = "51" beside line 1a/7's "1,779"). Columns (d) proceeds and (e) cost
    # are gross amounts — always comma-formatted — so a bare int there is a line-
    # number echo or a description digit bleeding into the column band, never a
    # value. Restricting to (h) recovers the real small gains with zero noise.
    money_cols = {_col_of(vx) for vx, _, _ in strong}
    values = list(strong) + [(vx, vy, vt) for vx, vy, vt in weak
                             if _col_of(vx) == 'h' and 'h' in money_cols]

    out, seen = [], set()
    for vx, vy, vt in values:
        letter = _col_of(vx)
        # line = nearest label AT OR ABOVE the value. The next label below is the
        # natural lower boundary, so no tight cap is needed — only a generous one
        # (45 pt) to reject orphans: line 1a/8a print their value at the BOTTOM of a
        # ~6-line description block, ~33 pt under the label, which the old 20-pt cap
        # dropped (losing the entire 1a/8a row).
        best_ln, best_dy = None, None
        for ln, ly in labels:
            dy = vy - ly
            if dy < -3 or dy > 45:
                continue
            if best_dy is None or dy < best_dy:
                best_dy, best_ln = dy, ln
        if best_ln is None:
            continue
        # Drop a value that is just its own line number reprinted (H&R Block echoes
        # the line label into the column band). A real amount equal to its line
        # number is essentially never seen; a blank line's echo is, so skip it.
        if vt.strip(" .()") == best_ln:
            continue
        key = (best_ln, letter)
        if key in seen:
            continue
        seen.add(key)
        out.append((best_ln, letter, vt))
    return out


def _num_local(value_str: str):
    try:
        return float(value_str.replace(',', '').replace('(', '-')
                     .replace(')', '').strip().rstrip('.'))
    except (ValueError, AttributeError):
        return None


def extract_schedule_d(doc, file_slug: str, tax_year: str) -> list:
    """Extract Schedule D capital-gains cells. Page 1 = d/e/g/h grid (lines 1a-15);
    page 2 = single-column summary (16-21). Dedups duplicate filing/records copies."""
    page1 = None    # (anchor, [(line, letter, value)])
    page2 = None    # (anchor, [(line, value)])
    for i, page in enumerate(doc, start=1):
        txt = page.get_text()
        anchor = f"[p.{i}](#{_anchor(file_slug, i)})"
        if page1 is None and _is_sched_d_page1(txt):
            cells = _extract_schedule_d_page1(page)
            if cells:
                page1 = (anchor, cells)
        elif page2 is None and _is_sched_d_page2(txt):
            p2 = _extract_single_col(page, _SCHED_D_PAGE2, 0.70)
            if p2:
                page2 = (anchor, p2)

    entries = []
    if page1:
        anchor, cells = page1
        order = {"d": 0, "e": 1, "g": 2, "h": 3}
        for line, letter, value in sorted(
                cells, key=lambda r: (_linekey(r[0]), order.get(r[1], 9))):
            entries.append({
                "line": f"line {line}",
                "description": _SCHED_D_LINES.get(line, ""),
                "column": _SCHED_D_COLS.get(letter, letter),
                "value": value,
                "page": anchor,
                "tax_year": tax_year,
                "form": "Schedule D",
            })
    if page2:
        anchor, p2 = page2
        for line, value in sorted(p2, key=lambda r: _linekey(r[0])):
            entries.append({
                "line": f"line {line}",
                "description": _SCHED_D_PAGE2.get(line, ""),
                "column": "Summary",
                "value": value,
                "page": anchor,
                "tax_year": tax_year,
                "form": "Schedule D",
            })
    return entries


def _extract_grid_page(page, line_lo: int, line_hi: int,
                       col_label, min_col_members: int = 3, y_tol: int = 6,
                       y_floor: float = 0.0) -> list:
    """Return [(line, column_label, value_str)] for one grid page.
    col_label(i) maps 0-based column index (left→right) to a label string.

    y_floor (absolute points): ignore everything at or above it. Used to fence off
    a form's pre-grid matter whose digits collide with grid line numbers — e.g.
    Schedule E's "Type of Property" legend (1 Single Family … 5 Land … 7 Self-Rental
    … 8 Other), whose digits 1–8 overlap line numbers 3–8. The legend's "5 Land"
    sat on the same row as a stray "7" (from "7 Self-Rental"), and that false
    (label 5, value 7) pair claimed cell (line 5, col A) before the real "203"
    could — losing exactly that value (the 196 Schedule E shortfall)."""
    W, H = page.rect.width, page.rect.height
    labels = []   # (xc, yc, line)
    strong = []   # (xc, yc, value_str) — formatted money, defines the columns
    weak = []     # (xc, yc, value_str) — bare ints, admitted only inside a column
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        xc = (x0 + x1) / 2
        yc = (y0 + y1) / 2
        if yc <= y_floor:
            continue
        t = word.strip()
        if _MONEY.match(t) and yc > H * 0.08 and xc > W * 0.55:
            strong.append((xc, yc, t))
        elif _WEAK_INT.match(t) and yc > H * 0.08 and xc > W * 0.55:
            weak.append((xc, yc, t))
        elif re.match(r'^\d{1,2}[a-e]?$', t) and W * 0.44 < xc < W * 0.56:
            # ONLY the line-number box column (~x 50%). The description-side label
            # (~7%) and the "line N for all properties" references in the line 23a-e
            # totals block (~35%) must NOT be treated as grid line labels — that's
            # what made Property B pick up the 23a-e column totals.
            try:
                n = int(re.sub(r'\D', '', t))
            except ValueError:
                continue
            if line_lo <= n <= line_hi:
                labels.append((xc, yc, t.lower()))
    if not strong:
        return []

    # Real value columns are defined by FORMATTED money only — a line-number echo
    # column (all bare integers) can never become a value column this way.
    clusters = [c for c in _cluster([v[0] for v in strong]) if len(c) >= min_col_members]
    centers = [sum(c) / len(c) for c in clusters]
    if not centers:
        return []

    # Admit a bare integer as a value only if it lands inside one of those money
    # columns — recovers small un-formatted amounts (e.g. a $7 expense) while
    # rejecting line-number echoes, which sit in their own band away from a column.
    values = list(strong)
    for wx, wy, wt in weak:
        ci = min(range(len(centers)), key=lambda i: abs(centers[i] - wx))
        if abs(centers[ci] - wx) <= 12:
            values.append((wx, wy, wt))

    cells = []
    seen = set()
    for vx, vy, vt in values:
        ci = min(range(len(centers)), key=lambda i: abs(centers[i] - vx))
        if abs(centers[ci] - vx) > 12:      # value must sit IN a column
            continue
        best, best_dx = None, None
        for lx, ly, ln in labels:
            if lx >= vx or abs(vy - ly) > y_tol:
                continue
            dx = vx - lx
            if best_dx is None or dx < best_dx:
                best_dx, best = dx, ln
        if best is None:
            continue
        key = (best, ci)
        if key in seen:
            continue
        seen.add(key)
        cells.append((best, col_label(ci), vt))
    return cells


def _sched_e_grid_floor(page) -> float:
    """Y of Schedule E Part I's "Income" section header — the per-property grid
    (lines 3–22) starts just below it. Everything above (property address table,
    Type-of-Property table, and the type LEGEND whose digits 1–8 collide with line
    numbers) is fenced off. Keyed on the left-margin "Income" header below the
    title band; vendor-independent IRS form text. 0.0 if not found (no extra floor)."""
    W, H = page.rect.width, page.rect.height
    for w in page.get_text("words"):
        x0, y0, x1 = w[0], w[1], w[2]
        if w[4].strip().rstrip(':') == "Income" and x0 < W * 0.15 and y0 > H * 0.25:
            return y0 - 2
    return 0.0


def extract_schedule_e(doc, file_slug: str, tax_year: str) -> list:
    """Extract Schedule E Part I per-property cells.

    A return can hold SEVERAL Schedule E forms (more than 3 rental properties) AND
    duplicate copies (filing + records). We dedup true copies by content signature
    (identical cell set) but keep distinct forms separate, numbering them. Property
    columns stay scoped to their form so values never collide across forms."""
    instances = []   # list of (anchor, cells) for each DISTINCT Schedule E form
    seen_sigs = set()
    totals = None    # (anchor, [(line, value)]) — schedule-wide block 23a-26, once
    page2 = None     # (anchor, [(line, value)]) — page-2 summary lines, once
    for i, page in enumerate(doc, start=1):
        txt = page.get_text()
        anchor = f"[p.{i}](#{_anchor(file_slug, i)})"
        if _is_sched_e_part1(txt):
            cells = _extract_grid_page(page, 3, 22, lambda c: chr(ord('A') + c),
                                       y_floor=_sched_e_grid_floor(page))
            if cells:
                sig = frozenset(cells)
                if sig not in seen_sigs:    # skip duplicate filing/records copies
                    seen_sigs.add(sig)
                    instances.append((anchor, cells))
            # Totals block is filled only on the primary Part I page; take the first
            # non-empty one and ignore the blank continuation pages + duplicate copies.
            if totals is None:
                tcells = _extract_sched_e_totals(page)
                if tcells:
                    totals = (anchor, tcells)
        elif _is_sched_e_page2(txt) and page2 is None:
            p2 = _extract_sched_e_page2(page)
            if p2:
                page2 = (anchor, p2)

    multi = len(instances) > 1
    entries = []
    for inst_no, (anchor, cells) in enumerate(instances, 1):
        for line, letter, value in sorted(cells, key=lambda r: (_linekey(r[0]), r[1])):
            col = f"Property {letter}"
            if multi:
                col = f"Sch E #{inst_no} · {col}"
            entries.append(_se_entry(f"line {line}", _SCHED_E_LINES.get(line, ""),
                                     col, value, anchor, tax_year))
    if totals:
        anchor, tcells = totals
        for line, value in sorted(tcells, key=lambda r: _linekey(r[0])):
            col = "All properties" if line.startswith("23") else "Schedule total"
            entries.append(_se_entry(f"line {line}", _SCHED_E_TOTALS.get(line, ""),
                                     col, value, anchor, tax_year))
    if page2:
        anchor, p2 = page2
        for line, value in sorted(p2, key=lambda r: _linekey(r[0])):
            entries.append(_se_entry(f"line {line}", _SCHED_E_PAGE2.get(line, ""),
                                     "Schedule total", value, anchor, tax_year))
    return entries


def _anchor(file_slug: str, i: int) -> str:
    return f"{file_slug}-page-{i:03d}" if file_slug else f"page-{i:03d}"


def _se_entry(line, description, column, value, page, tax_year) -> dict:
    return {
        "line": line,
        "description": description,
        "column": column,
        "value": value,
        "page": page,
        "tax_year": tax_year,
        "form": "Schedule E",
    }


def _linekey(ln: str):
    m = re.match(r'(\d+)([a-z]*)', ln)
    return (int(m.group(1)), m.group(2)) if m else (999, ln)


# ── Form 8949 — Sales and Other Dispositions of Capital Assets ───────────────
# Per-transaction detail grid: (a) description, (b) date acquired, (c) date sold,
# (d) proceeds, (e) cost basis, (f) adjustment code, (g) adjustment, (h) gain-loss.
# Each page ends with a Totals row that flows to Schedule D (lines 1b/2/3 for the
# short-term boxes, 8b/9/10 for long-term). A return holds the form 2-3 times
# (filing + records copies); identical pages are deduped by their transaction set.
_8949_HEADERS = [("a", 112), ("b", 198), ("c", 248), ("d", 306),
                 ("e", 371), ("f", 425), ("g", 479), ("h", 544)]


def _is_8949_page(page_text: str) -> bool:
    return "Form 8949" in page_text[:60]


def _8949_col(vx: float) -> str:
    """Column letter for a right-aligned value: greatest header center ≤ its x."""
    letter = _8949_HEADERS[0][0]
    for l, hx in _8949_HEADERS:
        if hx <= vx + 2:
            letter = l
    return letter


def _parse_8949_page(page) -> dict:
    """Parse one Form 8949 page → {part, totals{letter:str}, transactions[...]}.

    Rows are clustered by y. A row with ≥2 dates is a transaction (description on
    the left, proceeds/cost/adjustment/gain in their columns); the money-only row
    with no dates is the page Totals. Verbatim value strings are preserved."""
    W, H = page.rect.width, page.rect.height
    head = page.get_text()[:2500]
    part = ("Long-term" if ("Long-Term" in head and re.search(r'Part\s+II\b', head))
            else "Short-term")

    toks = []   # (yc, xc, kind, text)
    for w in page.get_text("words"):
        x0, y0, x1, y1, word = w[0], w[1], w[2], w[3], w[4]
        xc = (x0 + x1) / 2
        yc = (y0 + y1) / 2
        if yc < H * 0.45 or yc > H * 0.94:   # grid body only (skip header/footer)
            continue                          # 0.45 floor catches rows that start ~y377
        t = word.strip()
        if _DATE.match(t):
            toks.append((yc, xc, "date", t))
        elif _MONEY.match(t):
            toks.append((yc, xc, "money", t))
        elif xc < W * 0.31 and re.search(r'[A-Za-z0-9]', t):
            toks.append((yc, xc, "desc", t))
    if not toks:
        return {"part": part, "totals": {}, "transactions": []}

    # Cluster tokens into rows (consecutive y within 8 pt).
    toks.sort(key=lambda r: r[0])
    rows, cur, cy = [], [], None
    for yc, xc, kind, t in toks:
        if cy is None or yc - cy <= 8:
            cur.append((xc, kind, t))
        else:
            rows.append(cur)
            cur = [(xc, kind, t)]
        cy = yc
    if cur:
        rows.append(cur)

    transactions, totals = [], {}
    for row in rows:
        dates = sorted([(xc, t) for xc, k, t in row if k == "date"])
        money = [(xc, t) for xc, k, t in row if k == "money"]
        desc = " ".join(t for xc, t in sorted([(xc, t) for xc, k, t in row if k == "desc"]))
        cols = {}
        for xc, t in money:
            v = _num_local(t)
            if v is not None and abs(v) >= 1:
                cols.setdefault(_8949_col(xc), t)
        has_money = "d" in cols or "h" in cols
        if dates and has_money:
            # A lot acquired on multiple dates shows "VARIOUS" (no acquired date), so
            # the row carries only the sold date — still a real transaction.
            acquired = dates[0][1] if len(dates) >= 2 else "VARIOUS"
            sold = dates[-1][1]
            transactions.append({
                "description": desc, "acquired": acquired, "sold": sold,
                "proceeds": cols.get("d", ""), "cost": cols.get("e", ""),
                "adjustment": cols.get("g", ""), "gain": cols.get("h", ""),
            })
        elif money and not dates and len(cols) > len(totals):
            totals = cols     # the money-only row with the most columns is the Totals
    return {"part": part, "totals": totals, "transactions": transactions}


def extract_form_8949(doc, file_slug: str, tax_year: str):
    """Parse every Form 8949 page, dedup duplicate filing/records copies by their
    transaction set, and return (index_total_entries, detail_pages).
    `index_total_entries` are the compact page-Totals rows for the Tax Line Index;
    `detail_pages` carry the per-transaction rows for the appendix section."""
    seen, pages = set(), []
    for i, page in enumerate(doc, start=1):
        if not _is_8949_page(page.get_text()):
            continue
        info = _parse_8949_page(page)
        if not info["transactions"] and not info["totals"]:
            continue
        sig = (info["part"], tuple((t["description"], t["proceeds"])
                                   for t in info["transactions"]))
        if sig in seen:        # identical copy already captured
            continue
        seen.add(sig)
        info["page_no"] = i
        info["anchor"] = f"[p.{i}](#{_anchor(file_slug, i)})"
        pages.append(info)

    entries = []
    for info in pages:
        pg = info["page_no"]
        line = "2" if info["part"] == "Short-term" else "4"
        for letter, label in _SCHED_D_COLS.items():
            val = info["totals"].get(letter)
            if not val:
                continue
            entries.append({
                "line": f"Totals p.{pg}",
                "description": f"Form 8949 {info['part']} totals — flows to Schedule D "
                               f"(line {line})",
                "column": label,
                "value": val,
                "page": info["anchor"],
                "tax_year": tax_year,
                "form": "Form 8949",
                "_part": info["part"],
            })
    return entries, pages


def render_8949_appendix(pages: list) -> str:
    """Render the Form 8949 per-transaction detail as a Markdown appendix section.
    Each page shows its transactions plus a Σ-check row against the page Totals; a
    page whose transactions don't reconcile is disclosed (never silently wrong)."""
    if not pages:
        return ""
    L = ["## ◈ Form 8949 — Transaction Detail (Appendix)", "",
         "> Per-transaction capital-asset detail from Form 8949. Each page's "
         "transactions reconcile to its Totals row (which flows to Schedule D and "
         "is the authoritative figure). Values are verbatim from the form. Columns: "
         "(d) proceeds · (e) cost basis · (g) adjustment · (h) gain/loss. Acquired "
         '"VARIOUS" means the lot had multiple acquisition dates.', ""]
    for info in pages:
        txns, tot = info["transactions"], info["totals"]
        L.append(f"### {info['part']} — {info['anchor']} · {len(txns)} transactions")
        L += ["",
              "| # | Description | Acquired | Sold | (d) Proceeds | (e) Cost | "
              "(g) Adj | (h) Gain/loss |",
              "|---|---|---|---|--:|--:|--:|--:|"]
        sums = {"d": 0.0, "e": 0.0, "h": 0.0}
        for n, t in enumerate(txns, 1):
            for k, key in (("d", "proceeds"), ("e", "cost"), ("h", "gain")):
                v = _num_local(t[key])
                if v is not None:
                    sums[k] += v
            L.append(f"| {n} | {t['description']} | {t['acquired']} | {t['sold']} | "
                     f"{t['proceeds']} | {t['cost']} | {t['adjustment']} | {t['gain']} |")
        td, te, th = (_num_local(tot.get(c, "")) for c in ("d", "e", "h"))
        ok = all(tv is None or abs(sums[c] - tv) < 0.5
                 for c, tv in (("d", td), ("e", te), ("h", th)))
        L.append(f"| **Totals** | _from form_ |  |  | **{tot.get('d', '')}** | "
                 f"**{tot.get('e', '')}** | **{tot.get('g', '')}** | **{tot.get('h', '')}** |")
        status = "✓ reconciles" if ok else "⚠ detail incomplete — Totals authoritative"
        L.append(f"| _Σ check_ | {status} |  |  | {sums['d']:,.0f}. | {sums['e']:,.0f}. "
                 f"|  | {sums['h']:,.0f}. |")
        L.append("")
    return "\n".join(L)


def extract(doc, meta: dict, file_slug: str) -> list:
    """Run all grid-form extractors and return merged Tax Line Index cell entries.
    Schedule E + Schedule D cells, and Form 8949 page Totals. Form 8949 per-
    transaction detail is stashed in meta for the appendix renderer + validator."""
    tax_year = meta.get("tax_year") or meta.get("tax_period") or ""
    out = []
    for fn in (extract_schedule_e, extract_schedule_d, extract_schedule_1,
               extract_schedule_3, extract_form_8995, extract_schedule_8812):
        try:
            out += fn(doc, file_slug, tax_year)
        except Exception:
            pass    # extraction failure must never abort the main conversion
    try:
        totals, detail_pages = extract_form_8949(doc, file_slug, tax_year)
        out += totals
        meta["form_8949_pages"] = detail_pages
    except Exception:
        pass
    return out
