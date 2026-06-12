"""
Call Transcript Converter — High-Integrity Edition
Source: Apple Voice Memos (single audio stream, no automatic speaker labels)

Design principles:
- Preserve ALL spoken content verbatim — the full transcript is the primary AI reference
- Extract only what is literally present: names, phones, emails, addresses, prices, dates
- No automatic masking — only user-defined replacements apply
- Attempt speaker attribution where self-introductions make it clear; never fabricate
- Joe's spoken context note at end of recording is parsed as structured metadata
- Honest gaps: if a field cannot be determined, say so rather than guess
"""

import re
from datetime import datetime


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

_MONTH_NAMES = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4, 'may': 5,
    'june': 6, 'july': 7, 'august': 8, 'september': 9, 'october': 10,
    'november': 11, 'december': 12,
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'sept': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def _parse_month_name(m):
    month = _MONTH_NAMES.get(m.group(1).lower(), 0)
    day = int(m.group(2))
    year = int(m.group(3))
    if month and 1 <= day <= 31 and 2000 <= year <= 2099:
        return datetime(year, month, day)
    return None


def _parse_month_day_spoken(m):
    """Handle 'June 4th', 'June 5', 'it's June 4th' without a year."""
    month = _MONTH_NAMES.get(m.group(1).lower(), 0)
    day = int(m.group(2))
    if month and 1 <= day <= 31:
        # Assume current year context from process_date
        return datetime(datetime.now().year, month, day)
    return None


def _parse_iso(m):
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _parse_mdy(m):
    try:
        mo, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= day <= 31 and 2000 <= year <= 2099:
            return datetime(year, mo, day)
    except ValueError:
        pass
    return None


_DATE_PARSERS = [
    (r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})', _parse_month_name),
    (r'\b(\d{4})[/-](\d{2})[/-](\d{2})\b', _parse_iso),
    (r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b', _parse_mdy),
    # Spoken month+day without year ("It's June 4th", "June 5th") — lower priority
    (r"(?:it'?s\s+)?(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?(?!\s*,?\s*\d{4})", _parse_month_day_spoken),
]


def _extract_date(text: str) -> datetime:
    for pattern, parser in _DATE_PARSERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result = parser(m)
            if result:
                return result
    return datetime.now()


def _explicit_date(text: str):
    """Return a date only if one is explicitly present in the text, else None —
    so the summary never claims a call date that wasn't actually spoken."""
    for pattern, parser in _DATE_PARSERS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            result = parser(m)
            if result:
                return result
    return None


# ---------------------------------------------------------------------------
# Pre-call intro detection
# The call owner sometimes speaks a quick note BEFORE accepting the call:
# "This is a call with Bolton's" or "Calling Crown Electric about the water heater"
# This is separate from the post-call context note.
# ---------------------------------------------------------------------------

_PRE_CALL_RE = re.compile(
    r'^(?:(?:hey[,\s]+)?this is a call with|calling|call with|'
    r'hey[,\s]+(?:I\'?m\s+)?(?:about to call|calling)|'
    r'this call is with|quick note[,\s]+this is)\s+([A-Za-z][A-Za-z\s&,\'\.]{2,60}?)(?:\.|,|\n|$)',
    re.IGNORECASE | re.MULTILINE
)


def _extract_pre_call_intro(text: str) -> str:
    """Extract any spoken intro made before the call was accepted."""
    # Only look in the first 300 characters
    head = text[:300]
    m = _PRE_CALL_RE.search(head)
    if m:
        intro = m.group(0).strip().rstrip('.,')
        if len(intro) > 8:
            return intro
    return ""


# ---------------------------------------------------------------------------
# Split transcript: call body vs. owner's post-call context note
# The call owner often dictates a summary at the END of the recording.
# That section is parsed separately — it contains the most reliable facts.
# ---------------------------------------------------------------------------

_CONTEXT_TRIGGERS = re.compile(
    r'(?:for the record|just for context|so for the record|so just for context|'
    r'this is the owner|I\'m the owner|I am the owner|as a side note for AI|side note for AI|'
    r'when AI gets this|note for AI|okay so that was|so that was|'
    r'just had a call with|just to recap|so for context|'
    # Generic "this is [name]" at END of recording (post-call summary)
    r'this is [A-Z][a-z]+,?\s+the owner)',
    re.IGNORECASE
)


def _split_transcript(text: str) -> tuple[str, str]:
    """
    Split into (call_body, context_note).
    Returns (full_text, "") if no context note detected.
    """
    m = _CONTEXT_TRIGGERS.search(text)
    if m:
        body = text[:m.start()].strip()
        note = text[m.start():].strip()
        return body, note
    return text.strip(), ""


# ---------------------------------------------------------------------------
# Contact extraction — phone, email, address
# Handles both standard formatted and spoken forms.
# ---------------------------------------------------------------------------

_PHONE_RE = re.compile(
    r'\b(\d{3}[-.\s]\d{3}[-.\s]\d{4}|\(\d{3}\)\s*\d{3}[-.\s]\d{4})\b'
)

# Standard email
_EMAIL_STANDARD_RE = re.compile(
    r'\b([A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b'
)

# Spoken email: "wagonwheel2017 at gmail" / "wagon wheel 2017 at gmail dot com"
# Captures multi-word usernames and requires a real domain name (not common English words)
_EMAIL_SPOKEN_RE = re.compile(
    r'\b([A-Za-z0-9][A-Za-z0-9._]*(?:\s+[A-Za-z0-9._]+){0,3})\s+at\s+([A-Za-z]{3,20})\s*(?:dot\s*(com|net|org|gov|edu|io))?\b',
    re.IGNORECASE
)

# Real consumer mail providers — a spoken "<user> at <provider>" is an email
# even without an explicit "dot com". Anything else needs the TLD spoken.
_KNOWN_MAIL_DOMAINS = {
    'gmail', 'yahoo', 'outlook', 'hotmail', 'icloud', 'aol', 'comcast',
    'att', 'verizon', 'proton', 'protonmail', 'live', 'msn', 'me', 'mac',
}

# Words that should NOT be treated as email domain names
_FAKE_DOMAINS = {
    'the', 'this', 'that', 'those', 'these', 'their', 'there', 'then', 'than',
    'and', 'but', 'for', 'with', 'from', 'into', 'onto', 'over', 'under',
    'all', 'any', 'one', 'two', 'some', 'our', 'your', 'his', 'her', 'its',
    'not', 'now', 'here', 'just', 'back', 'down', 'what', 'when', 'where',
    'which', 'who', 'how', 'why', 'got', 'get', 'put', 'let', 'did', 'has',
    'had', 'was', 'are', 'were', 'been', 'being', 'done', 'said', 'new',
    'old', 'out', 'off', 'still', 'about', 'going', 'getting', 'replacing',
    'checking', 'trying', 'looking', 'calling', 'coming', 'doing', 'making',
    # Additional common words that appear near "at" in speech
    'premium', 'standard', 'regular', 'basic', 'right', 'time', 'point',
    'least', 'most', 'best', 'first', 'last', 'same', 'each', 'both',
    'work', 'home', 'place', 'part', 'cost', 'price', 'rate', 'level',
}

_ADDRESS_RE = re.compile(
    # Require street number ≥ 3 digits (avoids "240 on each... places", "6 year...")
    # OR 1-2 digits only when followed by a proper capitalized street name
    r'\b(\d{3,5}\s+(?:old\s+)?[A-Za-z][A-Za-z0-9 ]{1,35}\b'
    r'(?:Street|Avenue|Boulevard|Highway|Drive|Road|Lane|Circle|Court|Place|Trail|Way)'
    r'(?:\s+\d{1,3})?'                           # route number: "Highway 20"
    r'(?:\s+in\s+[A-Za-z][A-Za-z ,]{0,40})?)',   # "in City, State"
    re.IGNORECASE
)


def _normalize_spoken_email(m) -> str:
    """Convert 'wagonwheel2017 at gmail dot com' → 'wagonwheel2017@gmail.com'"""
    user = re.sub(r'\s+', '', m.group(1))  # remove spaces from username
    domain = m.group(2).lower().strip()
    tld = (m.group(3) or 'com').lower()
    return f"{user}@{domain}.{tld}"


def _extract_contacts(text: str) -> dict:
    phones = list(dict.fromkeys(_PHONE_RE.findall(text)))

    emails = list(dict.fromkeys(_EMAIL_STANDARD_RE.findall(text)))
    for m in _EMAIL_SPOKEN_RE.finditer(text):
        username = m.group(1).strip()
        domain = m.group(2).strip().lower()
        spoken_tld = m.group(3)  # the "dot com/net/..." part, if it was actually said
        # Skip if domain is a phone area code (all digits)
        if domain.isdigit():
            continue
        # Skip if domain is a common English word (not a real domain)
        if domain.lower() in _FAKE_DOMAINS:
            continue
        # Only accept when it's clearly an email: either a known mail provider
        # (gmail/yahoo/...) or an explicit "dot com/net/..." was spoken. This
        # stops phrases like "Mike are you at Bolton" → mikeareyou@bolton.com.
        if not spoken_tld and domain not in _KNOWN_MAIL_DOMAINS:
            continue
        # Also skip if clean username is too short after removing spaces
        clean_check = re.sub(r'\s+', '', username.lower())
        if len(clean_check) < 4:
            continue
        # Skip very common false-positive username phrases including action verbs
        if username.lower() in ('a', 'an', 'the', 'just', 'had', 'it', 'we', 'you',
                                'i', 'power', 'looking', 'we have', 'i have'):
            continue
        # Skip if username starts with a verb (e.g. "Calling", "Sending", "Going")
        first_word = username.split()[0].lower() if username.split() else ''
        if first_word in ('calling', 'sending', 'going', 'checking', 'getting',
                          'making', 'having', 'being', 'doing', 'reaching',
                          'talking', 'speaking', 'asking', 'telling', 'following'):
            continue
        # Skip if more than 3 words in username (probably a sentence fragment)
        if len(username.split()) > 3:
            continue
        # Strip leading article (e.g. "a wagon wheel 2017" → "wagon wheel 2017")
        username = re.sub(r'^(?:a|an|the)\s+', '', username, flags=re.IGNORECASE)
        # Normalize spaces out of username: "wagon wheel 2017" → "wagonwheel2017"
        clean_user = re.sub(r'\s+', '', username)
        tld = (m.group(3) or 'com').lower()
        normalized = f"{clean_user}@{domain}.{tld}"
        if normalized not in emails:
            emails.append(normalized)

    addresses = []
    seen_addr = set()
    for m in _ADDRESS_RE.finditer(text):
        addr = m.group(1).strip().rstrip('.,')
        key = addr[:30].lower()
        if key not in seen_addr and len(addr) > 10:
            seen_addr.add(key)
            addresses.append(addr)

    return {
        "phones": phones,
        "emails": emails,
        "addresses": addresses,
    }


# ---------------------------------------------------------------------------
# Participant / company extraction
# Since Apple Voice Memos gives a single stream, we identify parties from
# explicit self-introductions. Joe (the call owner) is always a party.
# ---------------------------------------------------------------------------

# Patterns that identify the OTHER party (not Joe)
_OTHER_PARTY_RE = [
    # "it's Christine" / "it's Josh again" — anywhere in text, EXCLUDING "this is the X"
    # (which identifies a location, not a person)
    (r"(?:it'?s)\s+([A-Z][a-z]{2,15})\b", 1),
    (r"(?:this is)\s+([A-Z][a-z]{2,15})\b(?!\s+(?:the|a|an)\b)", 1),
    # "with Josh with Crown Electric"
    (r"\bwith\s+([A-Z][a-z]{2,15})\s+(?:with|from|at)\s+([A-Za-z][A-Za-z\s&]{3,40})", 1),
    # "hey sir it's Josh" / "hey this is Christine"
    (r"hey[,\s]+(?:sir[,\s]+|there[,\s]+)?(?:it'?s|this is)\s+([A-Z][a-z]{2,15})\b", 1),
]

# Exclude property/location names from participants
_LOCATION_WORDS = {'cabins', 'cabin', 'hotel', 'lodge', 'inn', 'resort', 'property',
                   'house', 'home', 'apartments', 'complex', 'building', 'office',
                   'center', 'church', 'school', 'park', 'street', 'road', 'drive'}

_COMPANY_RE = re.compile(
    r'\b(?:with|from|at|it\'?s)\s+([A-Z][A-Za-z\s&\-\']{2,40}?'
    r'(?:Electric|Plumbing|Services|Heating|Cooling|HVAC|Home|Roofing|'
    r'Construction|Contracting|Realty|Management|Insurance|'
    r'Group|Company|Co\.|Inc\.|LLC|Corp))\b',
    re.IGNORECASE
)

_NAME_SKIP = {
    'i', 'we', 'you', 'it', 'that', 'this', 'okay', 'yeah', 'yes', 'no',
    "sir", "ma'am", 'mr', 'mrs', 'ms', 'dr', 'sorry', 'hello', 'hey', 'hi',
    'well', 'so', 'now', 'here', 'there', 'just', 'still', 'back', 'right',
    'good', 'great', 'sure', 'fine', 'done', 'thanks', 'thank', 'welcome',
    'bye', 'goodbye', 'calling', 'getting', 'going', 'looking', 'trying',
    'talking', 'thinking', 'saying', 'asking', 'coming', 'putting', 'due',
    'the', 'my', 'and', 'but', 'for', 'with', 'from', 'into', 'onto',
    'what', 'when', 'where', 'which', 'who', 'how', 'any', 'all', 'both',
    'june', 'july', 'august', 'september', 'october', 'november', 'december',
    'january', 'february', 'march', 'april', 'may', 'monday', 'tuesday',
    'wednesday', 'thursday', 'friday', 'saturday', 'sunday', 'new',
    'old', 'let', 'got', 'get', 'see', 'one', 'two', 'had', 'has', 'have',
    'said', 'told', 'need', 'want', 'make', 'take', 'give', 'keep', 'sent',
}


def _extract_parties(text: str) -> list[dict]:
    """
    Extract named parties from the transcript.
    Returns list of {name, company, note} dicts.
    Honest about what we know vs what we don't.
    """
    parties = {}  # name_lower → dict

    for pattern, name_group in _OTHER_PARTY_RE:
        for m in re.finditer(pattern, text, re.MULTILINE):
            name = m.group(name_group).strip().rstrip(',.;:')
            if not name or not name[0].isupper():
                continue
            if name.lower() in _NAME_SKIP:
                continue
            # Skip location/property names (e.g. "the Chocolate Cabins")
            if any(w in name.lower().split() for w in _LOCATION_WORDS):
                continue
            key = name.lower()
            if key not in parties:
                parties[key] = {"name": name, "company": "", "note": ""}

            # Try to get company from same match if present
            if m.lastindex and m.lastindex >= 2:
                try:
                    co = m.group(2).strip().rstrip(',.;:')
                    if co and not parties[key]["company"]:
                        parties[key]["company"] = co
                except IndexError:
                    pass

    # Find company names nearby each identified person
    for m in _COMPANY_RE.finditer(text):
        co = m.group(1).strip()
        start = max(0, m.start() - 80)
        snippet = text[start:m.end() + 20].lower()
        for key in parties:
            if key in snippet and not parties[key]["company"]:
                parties[key]["company"] = co
                break

    # Also check context note for explicit "that was [Company]" mentions
    # Only search the LAST 400 chars (context note region) to avoid body fragments like
    # "that was Matt did that" being treated as a company name.
    context_tail = text[-400:] if len(text) > 400 else text
    companies_in_note = re.findall(
        r'that was\s+([A-Z][A-Za-z\s&\-\']{2,30}?)(?:\.|,|and)', context_tail, re.IGNORECASE
    )
    for co in companies_in_note:
        co = co.strip()
        # Must start with uppercase and not be a common phrase fragment
        if not co or not co[0].isupper():
            continue
        # Skip junk phrases like "a different call", "the guy", "for AI"
        first_word = co.lower().split()[0] if co.split() else ''
        if first_word in ('a', 'an', 'the', 'my', 'your', 'his', 'her',
                          'their', 'our', 'this', 'that', 'these', 'those',
                          'for', 'with', 'about', 'just'):
            continue
        # Skip if ends with a verb/filler — it's a sentence fragment, not a company name
        last_word = co.lower().split()[-1] if co.split() else ''
        if last_word in ('did', 'was', 'is', 'are', 'were', 'has', 'had', 'done',
                         'that', 'this', 'it', 'them', 'him', 'her', 'there'):
            continue
        # If we already have a party, associate; otherwise add as unnamed contact
        added = False
        for key in parties:
            if not parties[key]["company"]:
                parties[key]["company"] = co
                added = True
                break
        if not added and co:
            co_lower = co.lower()
            # Only add if not already covered by an existing party's company
            already = any(
                co_lower in p.get("company", "").lower() or
                p.get("company", "").lower() in co_lower
                for p in parties.values()
                if p.get("company")
            )
            if not already:
                co_key = co_lower[:20]
                if co_key not in parties:
                    parties[co_key] = {"name": "", "company": co, "note": "vendor"}

    # Deduplicate by first name
    seen_first = {}
    for key, p in list(parties.items()):
        if p["name"]:
            first = p["name"].split()[0].lower()
            if first not in seen_first:
                seen_first[first] = p
            else:
                existing = seen_first[first]
                if len(p["name"]) > len(existing["name"]) or (p["company"] and not existing["company"]):
                    seen_first[first] = p
        else:
            # Company-only entry — keep as-is
            seen_first[key] = p

    return list(seen_first.values())


def _format_party(p: dict) -> str:
    name = p.get("name", "")
    company = p.get("company", "")
    # Don't repeat the name in the company string
    if name and company:
        # Remove the name from company if it's a prefix (e.g. "Josh with Crown Electric")
        clean_co = re.sub(r'^' + re.escape(name) + r'\s+(?:with|from|at)\s+', '', company, flags=re.IGNORECASE).strip()
        if clean_co:
            return f"{name} — {clean_co}"
        return name
    elif name:
        return name
    elif company:
        return company
    return ""


# ---------------------------------------------------------------------------
# Topic extraction — what this call is about
# Context note has the best signal; fall back to noun phrases.
# ---------------------------------------------------------------------------

def _extract_topic(body: str, context_note: str) -> str:
    full = context_note + "\n" + body

    # Priority 1: explicit "related to", "it's about", "calling about"
    for pat in [
        r'(?:it\'?s? related to|this is about|calling about|related to)\s+(?:the\s+)?(.+?)(?:\.|,|$)',
        r'(?:this is the|this is a)\s+([A-Za-z\s]+(?:heater|water|electric|roof|plumb|HVAC|AC|heat|repair|cabin|property|rental|house|unit)[A-Za-z\s,\.]*)',
        r'(?:you\'re with|with)\s+([A-Z][A-Za-z\s]+(?:Electric|Plumbing|Services|Home|Heating|Roofing|Contracting))',
        r'(?:figure out|discuss)\s+(?:what\'?s? going on with\s+)?(?:this\s+)?(.+?)(?:\.|,|$)',
    ]:
        for m in re.finditer(pat, full[:3000], re.IGNORECASE):
            t = m.group(1).strip().rstrip('.,;')
            if 4 < len(t) < 80:
                return t.title()

    # Priority 2: most frequent concrete noun phrase (prefer multi-word phrases)
    _NOUN_RE = re.compile(
        r'\b(water heater|hot water|electric panel|circuit breaker|electrical panel|'
        r'plumb\w+|roof\w*|thermostat|heating element|HVAC|air condition\w*|'
        r'inspection|estimate|quote|invoice|bill|mortgage|insurance|'
        r'[A-Z][a-z]+ (?:issue|problem|repair|replacement|review|inspection|estimate|quote))\b',
        re.IGNORECASE
    )
    matches = _NOUN_RE.findall(full[:5000])
    if matches:
        freq = {}
        for match in matches:
            k = match.lower().strip()
            freq[k] = freq.get(k, 0) + 1
        # Prefer longer (more specific) matches even if slightly less frequent
        best = max(freq, key=lambda k: (freq[k] * 2 + len(k.split()), k))
        return best.title()

    # Priority 3: single most common content word
    words = re.findall(r'\b[a-z]{5,}\b', full.lower()[:4000])
    stop = {'going', 'getting', 'having', 'being', 'saying', 'which', 'about',
            'would', 'could', 'should', 'really', 'think', 'thing', 'that',
            'their', 'there', 'these', 'those', 'where', 'right', 'yeah',
            'okay', 'like', 'just', 'back', 'down', 'from', 'into', 'they',
            'them', 'then', 'when', 'what', 'this', 'with', 'because',
            'going', 'trying', 'actually', 'probably'}
    content = [w for w in words if w not in stop]
    if content:
        freq = {}
        for w in content:
            freq[w] = freq.get(w, 0) + 1
        best = max(freq, key=lambda k: freq[k])
        return best.title()

    return "Call Discussion"


def _topic_slug(topic: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '_', topic.lower()).strip('_')
    return slug[:40]


# ---------------------------------------------------------------------------
# Key facts extraction — prices/quotes, equipment, dates, appointments
# Focuses on context note first (most reliable), then call body.
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(
    # Captures price ranges like "$1,200 to $1,300" as one match
    r'(?:\$\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d{1,3}(?:,\d{3})*\s*dollars?)'
    r'(?:\s*(?:to|and|-)\s*\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})?)?'
    r'(?:\s+(?:for|parts?(?:\s+and\s+labor)?|labor|installed?|includ\w+|estimate|quote))?',
    re.IGNORECASE
)

_EQUIPMENT_RE = re.compile(
    r'\b(?:'
    r'Bradford\s+White|A\.?O\.?\s*Smith|Whirlpool|Rheem|Rinnai|Navien|GE|Kenmore|'
    r'\d+[-\s]gallon(?:\s+electric|\s+gas|\s+heat\s+pump)?(?:\s+water\s+heater|\s+tank)?|'
    r'(?:electric|gas|heat\s+pump)\s+water\s+heater|water\s+heater|'
    r'(?:top|bottom|upper|lower)\s+(?:heating\s+)?(?:element|thermostat)|'
    r'heating\s+element|'
    r'circuit\s+breaker|electrical\s+panel|breaker\s+box|'
    r'20\d{2}\s+model|pro\s*line'
    r')\b',
    re.IGNORECASE
)

_DATE_MENTION_RE = re.compile(
    r'(?:'
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
    r'|(?:June|July|August|September|October|November|December|January|February|March|April|May)\s+\d{1,2}(?:st|nd|rd|th)?'
    r'|tomorrow(?:\s+morning|\s+afternoon|\s+evening)?'
    r'|next\s+(?:week|Monday|Tuesday|Wednesday|Thursday|Friday)'
    r'|\d{1,2}:\d{2}\s*(?:AM|PM)'
    r'|\d{1,2}\s*(?:AM|PM)'
    r')'
    r'[^.!?]{0,80}',
    re.IGNORECASE
)

_WARRANTY_RE = re.compile(
    r'\b\d+[-\s]year\s+(?:manufacturer\s+)?warranty(?:\s+from\s+(?:the\s+)?manufacturer)?|'
    r'warranty\s+(?:from\s+(?:the\s+)?manufacturer|on\s+(?:their|our|the)\s+\w+(?:\s+and\s+\w+)?)',
    re.IGNORECASE
)


def _extract_key_facts(body: str, context_note: str) -> dict:
    full = context_note + "\n" + body

    # Prices — with surrounding context, deduplicate on the dollar amount
    prices = []
    seen_p = set()
    for m in _PRICE_RE.finditer(full):
        val = m.group(0).strip().rstrip(',.')
        if len(val) < 2:
            continue
        # Normalize key on just the numeric part to avoid duplicates
        num_key = re.sub(r'[^0-9]', '', val)[:8]
        if not num_key or num_key in seen_p:
            continue
        seen_p.add(num_key)
        # Also mark nearby price amounts as seen (handles "$1,200, $1,300" in same sentence)
        nearby = full[max(0, m.start()-5):min(len(full), m.end()+30)]
        for near_m in re.finditer(r'\$\s*(\d{1,3}(?:,\d{3})*)', nearby):
            seen_p.add(near_m.group(1).replace(',', ''))
        # Build context: look backwards to start of sentence, forwards to end of sentence
        # Start: find nearest sentence start before the price mention
        chunk_before = full[max(0, m.start() - 80):m.start()]
        sent_start = max(chunk_before.rfind('. '), chunk_before.rfind('! '),
                         chunk_before.rfind('? '), chunk_before.rfind('\n'))
        if sent_start >= 0:
            start = max(0, m.start() - 80) + sent_start + 2
        else:
            start = max(0, m.start() - 30)
        # End: find nearest sentence end after the price mention
        chunk_after = full[m.end():min(len(full), m.end() + 80)]
        sent_end = min(
            (chunk_after.find('. ') if chunk_after.find('. ') >= 0 else 999),
            (chunk_after.find('! ') if chunk_after.find('! ') >= 0 else 999),
            (chunk_after.find('? ') if chunk_after.find('? ') >= 0 else 999),
        )
        if sent_end < 80:
            end = m.end() + sent_end + 1
        else:
            end = min(len(full), m.end() + 60)
        # Snap the START forward to a word boundary so we never begin mid-word
        # ("you" -> "ou"). The end already lands on a sentence boundary, and
        # snapping it backward risks cutting the price itself, so leave it.
        if start > 0 and not full[start - 1].isspace():
            sp = full.find(' ', start)
            if sp != -1 and sp - start <= 12:
                start = sp + 1
        context = full[start:end].strip().replace('\n', ' ').rstrip('.,')
        prices.append(context)

    # Equipment
    equipment = list(dict.fromkeys(
        m.group(0).strip() for m in _EQUIPMENT_RE.finditer(full)
    ))

    # Warranties
    warranties = list(dict.fromkeys(
        m.group(0).strip() for m in _WARRANTY_RE.finditer(full)
    ))

    # Dates / appointments
    appts = []
    seen_a = set()
    for m in _DATE_MENTION_RE.finditer(full):
        val = m.group(0).strip().rstrip('.,;')
        key = val[:25].lower()
        if key not in seen_a and len(val) > 4:
            seen_a.add(key)
            appts.append(val)

    return {
        "prices": prices[:6],
        "equipment": equipment[:8],
        "warranties": warranties[:4],
        "appointments": appts[:8],
    }


# ---------------------------------------------------------------------------
# Action item extraction — real committed actions, not conversation fragments
# ---------------------------------------------------------------------------

_ACTION_PATTERNS = [
    # "I will / I'll" + action verb + subject
    r"I(?:'ll| will)\s+((?:call|email|send|get|check|follow\s+up|replace|schedule|confirm|"
    r"provide|contact|come|fix|repair|install|ask|find\s+out|look\s+into|put|include|"
    r"let\s+you\s+know|have\s+(?:all|the|those|an?\s+answer)|build|share)\s+[^.!?\n]{5,120})",

    # "we will / we'll" + action verb
    r"we(?:'ll| will)\s+((?:call|email|send|get|check|follow\s+up|replace|schedule|confirm|"
    r"provide|contact|come|fix|repair|install|give|build|let\s+you\s+know)\s+[^.!?\n]{5,120})",

    # "[Name] to / [Name] will" (attributed actions)
    r"([A-Z][a-z]{2,15})\s+(?:will|is going to|to)\s+"
    r"((?:call|email|send|get|check|replace|schedule|confirm|come|fix|repair|install|"
    r"ask|find\s+out|provide|let\s+you\s+know|give)[^.!?\n]{5,120})",

    # "going to [action]" after firm commitment ("I'm going to replace")
    r"I'?m\s+going\s+to\s+((?:replace|call|email|send|get|check|fix|repair|install|"
    r"ask|find\s+out|provide|contact|come\s+out|schedule)\s+[^.!?\n]{5,120})",
]

_ACTION_NOISE = re.compile(
    r'\b(?:going to the|going to heat up|going to happen|going to start|'
    r'going to get lukewarm|going to take a|going to have half|going to have to|'
    r'going to replace it because you\'?re|trying to get|get this figured out|'
    r'install it,?\s+et cetera|install it,?\s+etc|get it figured|'
    r'give me a quote|send me a quote|give you a quote)\b',
    re.IGNORECASE
)

_FRAG_PREFIX = re.compile(
    r'^(?:trying|getting|looking|thinking|having|being|saying|telling|asking|'
    r'heater|water|electrical|electric|breaker|thermostat|element|'
    r'going to the|going to heat)\b',
    re.IGNORECASE
)


def _extract_action_items(text: str) -> list[str]:
    items = []
    seen = set()

    for pattern in _ACTION_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            if m.lastindex == 2:
                person = m.group(1).strip().rstrip(',.;:')
                action = m.group(2).strip().rstrip('.,;')
                # Only attribute if real proper name
                if person and person[0].isupper() and person.lower() not in _NAME_SKIP:
                    item = f"{person}: {action}"
                else:
                    item = action
            else:
                item = m.group(1).strip().rstrip('.,;')

            if len(item) < 10 or len(item) > 200:
                continue
            if _ACTION_NOISE.search(item):
                continue
            if _FRAG_PREFIX.match(item):
                continue
            # Truncate at natural sentence boundary if too long
            if len(item) > 130:
                period = item[:130].rfind(' ')
                item = item[:period].rstrip('.,;') + '...' if period > 50 else item[:130]

            key = item[:45].lower()
            if key not in seen:
                seen.add(key)
                items.append(item)

        if len(items) >= 10:
            break

    return items[:10]


# ---------------------------------------------------------------------------
# Decision extraction
# ---------------------------------------------------------------------------

_DECISION_PATTERNS = [
    # Strong explicit decisions with enough context
    r"(?:we(?:'re| are) going with|decided to|agreed to|going with)\s+([^.!?\n]{20,120})",
    r"(?:let'?s (?:do|go with)|we'll (?:go with|use))\s+([^.!?\n]{15,100})",
    # Expert recommendation with full context
    r"(?:if it were my (?:house|home|property),? I (?:would|'d))\s+([^!?\n]{15,100}?)(?:\.\s|\n|$)",
    r"(?:I (?:personally )?(?:like|prefer|recommend))\s+([^!?\n]{15,100}?)(?:\.\s|\n|$)",
    # Explicit replacement/installation decision
    r"(?:should|going to)\s+replace\s+(?:it|this|the\s+\w+)\s+because\s+([^.!?\n]{15,120})",
]


def _extract_decisions(text: str) -> list[str]:
    items = []
    seen = set()
    for pattern in _DECISION_PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            item = m.group(1).strip().rstrip('.,;')
            if len(item) < 15 or len(item) > 180:
                continue
            key = item[:40].lower()
            if key not in seen:
                seen.add(key)
                items.append(item)
        if len(items) >= 6:
            break
    return items[:6]


# ---------------------------------------------------------------------------
# Context note: structured parsing of Joe's spoken summary
# Joe often dictates at end: "Okay so that was [Company]. Number is [phone].
# It's [date]. They gave a quote of $X."
# This section contains the most reliable facts.
# ---------------------------------------------------------------------------

def _parse_context_note(note: str) -> dict:
    """Parse key facts from the spoken context/summary note."""
    result = {
        "companies": [],
        "phones": [],
        "emails": [],
        "addresses": [],
        "quotes": [],
        "appointments": [],
        "summary": note.strip(),
    }
    if not note:
        return result

    # Company names: "that was Bolton's", "so that was Pike Home Services"
    for m in re.finditer(
        r"(?:that was|so that was|this is|talking to|called?)\s+"
        r"([A-Z][A-Za-z\s&\-'\.]{2,40}?)(?:\.|,|and\s+their|\s+They|$)",
        note, re.IGNORECASE
    ):
        co = m.group(1).strip().rstrip(".,;' ")
        if len(co) > 2 and co.lower() not in _NAME_SKIP:
            first_word = co.lower().split()[0] if co.split() else ''
            if first_word in ('a', 'an', 'the', 'for', 'with', 'about', 'just',
                               'that', 'this', 'my', 'your', 'an', 'all', 'any'):
                continue
            result["companies"].append(co)

    # Phone numbers
    result["phones"] = _PHONE_RE.findall(note)

    # Emails — use same filtered extraction as _extract_contacts
    contacts = _extract_contacts(note)
    result["emails"] = contacts["emails"]

    # Addresses
    for m in _ADDRESS_RE.finditer(note):
        addr = m.group(1).strip().rstrip('.,')
        if len(addr) > 10:
            result["addresses"].append(addr)

    # Quotes
    for m in _PRICE_RE.finditer(note):
        val = m.group(0).strip().rstrip(',.')
        if len(val) > 2:
            # Grab context
            start = max(0, m.start() - 20)
            end = min(len(note), m.end() + 40)
            result["quotes"].append(note[start:end].strip().replace('\n', ' '))

    # Appointments
    for m in _DATE_MENTION_RE.finditer(note):
        val = m.group(0).strip().rstrip('.,;')
        if len(val) > 4:
            result["appointments"].append(val)

    return result


# ---------------------------------------------------------------------------
# Embedded AI instruction schema.
# AI Ready runs no AI and sends nothing off-device, so it does NOT generate the
# summary itself (regex can't judge materiality and would hallucinate — e.g.
# "step two" as a participant). Instead it emits the verbatim transcript plus
# THIS specification, so the user's own AI (ChatGPT/Claude) produces a
# decision-grade index where every item is traceable to the transcript.
# ---------------------------------------------------------------------------

_AI_TRANSCRIPT_SPEC = """## ⚙ AI SUMMARY SPECIFICATION — INSTRUCTIONS FOR THE AI READING THIS FILE

**Core principle.** The Full Transcript below is the SOLE authority for what was
said. Do not replace, reinterpret, embellish, or correct it. Your summary is only
a structured *index* of the transcript's useful content. If a fact, decision,
action item, participant, date, number, or commitment is not directly supported
by the transcript, mark it **uncertain** or **exclude** it. Every extracted item
must be traceable to transcript support and classified by reliability
(High / Medium / Low). Do not "summarize" — produce a *decision-grade index*.

**Transcription quality — read this first.** This is a VOICE-MEMO transcript and
may contain garbled or unintelligible passages: background noise, overlapping
voices, crosstalk, side conversations, or simple mis-hearings. Audio passages the
transcriber could not make sense of may be marked `⟦unclear audio⟧` in the text.
Where wording is unclear, nonsensical, internally contradictory, or likely
mis-transcribed, mark it **[transcription uncertain]** and do NOT present it as a
reliable quote, fact, figure, name, account number, or commitment. Never silently
"clean up" or guess a garbled passage into a confident statement — when in doubt,
say it is uncertain rather than risk misquoting the speaker.

**Redactions — read this too.** Transcripts are kept verbatim EXCEPT that hard PII
— Social Security numbers and payment-card numbers — is auto-masked (shown as
X-masked placeholders, e.g. `XXX-XX-XXXX`). Nothing else is auto-masked: spoken
account endings, dates, and figures are preserved as transcribed. Beyond hard PII,
the only other changes are the user's own specified replacements, if any. Whatever
was changed is listed in the **Redaction Disclosure** above, and each listed item
is a SUBSTITUTION, not a verbatim quote: never treat it as the exact word spoken,
never quote it as the speaker's literal words, and never guess or reconstruct the
original behind it. If no Redaction Disclosure is present, the transcript is exactly
as transcribed. If a redaction makes a passage ambiguous, mark it uncertain.

Produce these sections, in order:

**1. Metadata** — only what is explicitly present or reasonably inferable.
Call Title · Recording/File-Label Date · Processed Date · Call Date · Scheduled
Future Meeting Mentioned · Source Type · Participants · Speaker Identification
Confidence · Summary Reliability · Transcript Authority Statement. Keep these
four dates STRICTLY SEPARATE and never conflate them:
- **Recording / File Label Date** — the YY.MM.DD stamp on the file/label. A
  processing artifact, NOT when the call happened. Never present it as the call date.
- **Processed Date** — when AI Ready generated this file.
- **Call Date** — when the conversation actually took place. If not explicitly
  stated in the transcript, write exactly "No explicit call date stated in
  transcript." NEVER infer it from the file label date or from a scheduled future
  meeting.
- **Scheduled Future Meeting Mentioned** — any future meeting the parties agree to
  in the transcript (e.g. "Monday, June 8"). Label it as a future/scheduled date;
  it is NOT the call date. If none, write "None mentioned."
Do NOT list phrases, companies, topics, or transcript fragments as participants.
If speaker labels are absent: "Single-stream transcript; speaker separation
uncertain." Participants may be "User," "Advisor," or named people only if the
transcript supports them. Do NOT put the file label date in the Call Title.

**2. Executive Summary** — 5-10 concise bullets of material business points.
Separate what was *said* from what was *decided*. No filler, no invented
conclusions, no sales-language repetition unless material. Note uncertainty.

**3. Material Topics Discussed** — per topic: *What Was Said* (neutral) ·
*Why It Matters* · *Transcript Support* (short quote/paraphrase anchor) ·
*Status* (Clear / Needs follow-up / Verbal assurance only / Not actionable /
Conflicting-unclear) · *Reliability* (High/Medium/Low). Merge repetition; no
topics from incidental chatter.

**4. Decisions Made** — only actual decisions reached. Per decision: Decision ·
Made by · Basis in transcript · Confidence. "We discussed / may / should
consider / I'm thinking about" is NOT a decision. Label tentative ones
"Tentative"; flag any needing written confirmation.

**5. Action Items** — Action · Owner · Due Date · Trigger/Dependency ·
Transcript Support · Confidence. Must be a concrete future task. Exclude
personal/medical/social/incidental items and vague fragments ("get started,"
"send you that") unless the object is clear. Unknown owner → "Owner unclear";
unknown date → "No due date stated."

**6. Verbal Assurances Requiring Written Confirmation** — MANDATORY for any
financial, legal, tax, advisory, contractual, medical, or insurance call. Per
item: Verbal Assurance · Who Said It · Why It Matters · What Written Evidence Is
Needed · Risk If Not Confirmed. Flag fee estimates, tax-savings claims,
performance claims, fiduciary claims, service scope, authority limits. Do not
treat verbal assurances as contractual commitments.

**7. Open Questions** — unresolved questions grounded in the transcript that
affect decisions, money, taxes, contracts, risk, or implementation.

**8. Account / Entity / Asset Map** — if accounts/properties/entities/assets/
funds-flow appear: Name-or-Identifier · Type · Role/Purpose · Investable-or-
Operational · In Planning Model · In Managed Sleeve · Notes · Confidence.
Preserve account endings if present (do not invent full numbers). Distinguish
spending / reserve / retirement / business-operating / property / taxable-
investment / external accounts. Include a cash-flow map if movement is described.

**9. Financial Claims and Numbers** — per material number: Number/Amount · What
It Refers To · Speaker · Context · Decision Relevance · Confidence. Exclude
meaningless numbers. If a number looks garbled, mark "transcription uncertain."

**10. Risks / Red Flags** — Risk · Evidence · Impact · Recommended Follow-up.
(e.g. verbal claim not documented, fee unclear, tax benefit unsubstantiated,
account classification wrong, scope unclear, speaker/transcription uncertainty.)

**11. Non-Relevant / Excluded Content** — briefly note categories intentionally
excluded (family illness, vendor interruption, small talk, scheduling courtesy,
travel chatter). Never turn non-business chatter into action items.

**12. Source Transcript** — the Full Transcript below IS this section; do not
edit, clean, normalize, or remove awkward wording. It is the authoritative record.
It is your INPUT, not your output: reference it, do NOT reprint, echo, or quote it
in full unless the user explicitly asks. Redacted/generalized placeholders within
it are substitutions, not verbatim speech — do not un-redact them or guess their
originals.

**HARD-FAIL validation** — your output is invalid if: a participant is not a
person/organization; an action item is not a concrete future task; the call date
is inferred from a scheduled future meeting or from the file label/processed date;
the file label date is shown in the Call Title; a conclusion is stated more strongly
than the transcript supports; random numeric fragments are listed as key facts;
a transcript excerpt is labeled a "user note" when it wasn't provided as one; a
redacted or generalized placeholder is quoted as a verbatim word or its original
value is guessed; a financial/advisory call lacks the Verbal-Assurances section;
or the full transcript is not preserved as the source authority.

**Confidence** — High: clearly stated, unambiguous. Medium: stated once / speaker
unclear / slightly garbled but likely. Low: garbled, ambiguous speaker, unclear
number, or needs external-document confirmation. Use these labels aggressively.

**Priority for financial/advisory calls** — verbal assurances needing written
confirmation › action items › advisory scope › fees/costs › tax claims ›
authority/discretion › account roles › asset classification › outside accounts ›
data-quality issues › required written confirmations › next meeting/deadlines.
De-prioritize small talk, illness, jokes, analogies (unless they explain a
financial claim), travel chatter, interruptions, filler, transcription artifacts.
"""


# ---------------------------------------------------------------------------
# Main conversion entry point
# ---------------------------------------------------------------------------

def convert(text: str, call_index: int, process_date: str) -> dict:
    """
    Process a single raw call transcript block.
    HIGH-INTEGRITY: extracts only what is literally spoken.
    No automatic masking. Verbatim transcript always preserved.
    Owner's name is never included in participant list — referred to as "Owner".
    """
    # Split call body from owner's post-call context note (used only to derive a
    # filename topic).
    body, context_note = _split_transcript(text)

    # The filename is stamped with the PROCESSED date — never a date guessed from
    # the transcript. AI Ready cannot reliably tell a call date from a scheduled
    # future meeting (e.g. "let's meet June 8th"), so it never claims a call date;
    # the AI determines that from the transcript per the spec.
    try:
        call_date = datetime.strptime(process_date, "%Y-%m-%d")
    except Exception:
        call_date = datetime.now()
    date_str = call_date.strftime("%y.%m.%d")

    topic = _extract_topic(body, context_note)
    topic_slug = _topic_slug(topic)
    call_date_field = ("No explicit call date stated in transcript — determine from "
                       "the transcript per the spec; never infer it from the file "
                       "label date or a scheduled future meeting.")
    scheduled_meeting_field = ("If the transcript schedules a future meeting, the AI "
                               "lists it here (e.g. \"Monday, June 8\"). This is a "
                               "FUTURE date, NOT the call date.")

    # ── Build the AI-Ready transcript ────────────────────────────────────
    # AI Ready does NOT interpret the call (regex can't judge materiality and
    # would hallucinate — e.g. "step two" as a participant). It emits honest
    # metadata, the AI Summary Specification (so the user's own AI builds a
    # decision-grade index), and the verbatim transcript as source authority.
    # Per-call block: metadata + verbatim transcript. The AI spec and the file
    # title live ONCE at the top of the combined file (see combine()), so this
    # block nests cleanly whether the file holds one call or several.
    md = [
        "### Metadata",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Call Title | {topic} *(auto-derived — verify against transcript)* |",
        f"| Recording / File Label Date | {date_str} — processing/file label only; NOT the call date |",
        f"| Processed Date | {process_date} |",
        f"| Call Date | {call_date_field} |",
        f"| Scheduled Future Meeting Mentioned | {scheduled_meeting_field} |",
        "| Source Type | Apple Voice Memos — single audio stream (no automatic speaker separation) |",
        "| Speaker Identification | Single-stream transcript; speaker separation uncertain. |",
        "| Transcription Reliability | Voice memo — background noise or overlapping voices can garble words. Treat unclear or nonsensical passages as UNCERTAIN; do not quote them as fact. Passages the transcriber could not make out are marked ⟦unclear audio⟧. |",
        "",
        "### Full Transcript — Source Authority",
        "",
        "> Source authority. Verbatim EXCEPT for any redactions/generalizations disclosed in this file — those placeholders are substitutions, NOT the exact words spoken. Do not otherwise edit, clean, summarise, normalise, or attempt to un-redact.",
        "",
        text.strip(),
    ]

    return {
        "markdown": "\n".join(md),
        "date": call_date,
        "date_str": date_str,
        "topic": topic,
        "topic_slug": topic_slug,
        "process_date": process_date,
        "participants": [],
        "masks_applied": [],
    }


def build_redaction_disclosure(builtin: list, custom: list) -> str:
    """Build a disclosure block listing the redactions/generalizations applied to
    the transcript, so the reading AI never treats a placeholder as a verbatim
    spoken word (and never tries to reconstruct the original behind it).

    builtin: list of (label, count) tuples for built-in PII masks that fired.
    custom:  list of {replace_with, count, ...} dicts from the custom find/replace
             pass. Only the SUBSTITUTION side is listed — never the original term,
             so the disclosure itself leaks nothing.
    Returns "" when nothing was redacted (the transcript is fully verbatim)."""
    builtin = [(lbl, c) for lbl, c in builtin if c]
    custom = [s for s in custom if s.get("count")]
    if not builtin and not custom:
        return ""

    lines = [
        "## 🛡 Redaction Disclosure — this transcript is NOT 100% verbatim",
        "",
        "Sensitive content was redacted/generalized on-device before this file was "
        "written. Every item below is a SUBSTITUTION: do NOT treat a redacted or "
        "generalized placeholder as the exact word spoken, do NOT quote it as "
        "verbatim, and do NOT guess or reconstruct the original value behind it.",
        "",
    ]
    if builtin:
        lines.append(
            "**Built-in PII redactions** (shown in the text as X-masked "
            "placeholders, e.g. `XXX-XX-XXXX`):")
        lines.append("")
        for lbl, c in builtin:
            lines.append(f"- {lbl} — {c} redacted")
        lines.append("")
    if custom:
        lines.append(
            "**Custom generalizations applied** (the substituted text now appears "
            "in the transcript in place of what was actually said — treat it as a "
            "stand-in, not a verbatim quote):")
        lines.append("")
        for s in custom:
            rep = (s.get("replace_with") or "").strip()
            shown = f'"{rep}"' if rep else "[term removed entirely]"
            lines.append(f"- {shown} — {s['count']} occurrence(s)")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_filename(date: datetime, topic_slug: str) -> str:
    return f"transcript_{date.strftime('%y%m%d')}_{topic_slug}.md"


def combine(results: list[dict]) -> str:
    """Combine one or more processed call transcripts into a single AI-ready file.

    The whole output is wrapped in an outer START FILE / END FILE boundary so the
    user can paste several generated files into one ongoing master file and each
    batch stays clearly self-contained. Inside the wrapper: a title + the AI
    Summary Specification ONCE, then each call's metadata + verbatim transcript
    wrapped in its own explicit START/END CALL breaks."""
    n = len(results)
    process_date = results[0].get("process_date", "") if results else ""
    plural = "s" if n != 1 else ""
    parts = [
        f"# ===== START FILE: AI Ready batch — Processed {process_date} "
        f"({n} call{plural}) =====",
        "",
        f"# AI Ready — Call Transcript{plural}",
        "",
        f"> Generated by AI Ready | Processed {process_date}. On-device; not "
        "summarised or interpreted. Verbatim except for any redactions/"
        "generalizations disclosed in this file.",
        f"> This file contains **{n} call{plural}**. Build a SEPARATE decision-grade "
        "index for EACH call below, following the specification — and never mix "
        "facts across calls.",
        "> This batch is wrapped in START FILE / END FILE. If several batches are "
        "pasted together, treat each FILE block independently.",
        "",
        _AI_TRANSCRIPT_SPEC,
        "",
        "---",
        "",
        "## ▶ OUTPUT INSTRUCTION — read this last, before the calls",
        "",
        "For EACH call below, produce a SEPARATE index using **Sections 1–11** of "
        "the specification above. Treat the transcript under each call (its \"Full "
        "Transcript — Source Authority\") as that call's **Section 12 Source "
        "Authority** — your source of truth, NOT something to reprint. Do NOT "
        "reproduce, echo, or quote back Section 12 / the full transcript unless the "
        "user explicitly asks for it. Never mix facts across calls.",
    ]
    for i, r in enumerate(results, 1):
        parts += [
            "",
            "---",
            "",
            f"# ===== START CALL {i} of {n}: {r['topic']} =====",
            "",
            r["markdown"],
            "",
            f"# ===== END CALL {i} of {n} =====",
        ]
    parts += [
        "",
        f"# ===== END FILE: Processed {process_date} ({n} call{plural}) =====",
    ]
    return "\n".join(parts)
