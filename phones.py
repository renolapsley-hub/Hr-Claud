"""Phone number finding and normalising. Uses the 'phonenumbers' library; falls back to a simple
UK-only parser if the library is missing (so tests still run)."""
import re

import config

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat, PhoneNumberType, geocoder
    HAVE_LIB = True
except ImportError:  # pragma: no cover
    HAVE_LIB = False

_UK_FALLBACK = re.compile(r"(?<!\d)(?:\+44\s?\(?0?\)?|\(?0)\d[\d\s\-()]{7,13}\d(?!\d)")


def _digits(s):
    return re.sub(r"\D", "", s)


def normalise(raw, region=None):
    """Return dict(e164, intl, country, area, kind) or None if it is not a plausible business number."""
    region = region or config.PHONE_REGION
    if HAVE_LIB:
        try:
            num = phonenumbers.parse(raw, region)
        except phonenumbers.NumberParseException:
            return None
        if not phonenumbers.is_valid_number(num):
            return None
        t = phonenumbers.number_type(num)
        kind = next((n for n in ("FIXED_LINE", "MOBILE", "FIXED_LINE_OR_MOBILE", "TOLL_FREE", "VOIP", "UAN",
                                 "SHARED_COST") if getattr(PhoneNumberType, n) == t), "OTHER")
        if t == PhoneNumberType.PREMIUM_RATE:
            return None
        if t == PhoneNumberType.MOBILE and not config.ALLOW_MOBILE_NUMBERS:
            return None
        area = ""
        try:
            area = geocoder.description_for_number(num, "en") or ""
        except Exception:
            pass
        return {"e164": phonenumbers.format_number(num, PhoneNumberFormat.E164),
                "intl": phonenumbers.format_number(num, PhoneNumberFormat.INTERNATIONAL),
                "country": phonenumbers.region_code_for_number(num) or region, "area": area, "kind": kind}
    # ---- fallback: UK numbers only ----
    if region != "GB":
        return None
    d = _digits(raw)
    if raw.strip().startswith("+44") or d.startswith("44"):
        d = d[2:].lstrip("0") if d.startswith("44") else d
    elif d.startswith("0044"):
        d = d[4:].lstrip("0")
    elif d.startswith("0"):
        d = d[1:]
    else:
        return None
    if not (9 <= len(d) <= 10) or d[0] == "9":          # wrong length, or premium-rate 09
        return None
    if d[0] == "7" and not config.ALLOW_MOBILE_NUMBERS:  # skip mobiles: often a person's, not a switchboard
        return None
    if d[:2] in ("20", "23", "24", "28", "29"):          # 2-digit area codes e.g. London 20
        intl = f"+44 {d[:2]} {d[2:6]} {d[6:]}"
    elif d[:3] in ("113", "114", "115", "116", "117", "118") or (d[0] == "1" and d[2] == "1"):
        intl = f"+44 {d[:3]} {d[3:6]} {d[6:]}"          # e.g. 161, 121, 131
    else:
        intl = f"+44 {d[:4]} {d[4:]}"
    return {"e164": "+44" + d, "intl": intl, "country": "GB", "area": "", "kind": "UNVALIDATED"}


def find_in_text(text, region=None):
    """Yield (raw, start, end) for phone-like strings in text."""
    region = region or config.PHONE_REGION
    if HAVE_LIB:
        for m in phonenumbers.PhoneNumberMatcher(text, region):
            yield m.raw_string, m.start, m.end
    else:
        for m in _UK_FALLBACK.finditer(text):
            yield m.group(0), m.start(), m.end()
