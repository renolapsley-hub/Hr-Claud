"""Company-name matching and duplicate keys (no database access here)."""
import difflib
import hashlib
import re
import urllib.parse

import config

LEGAL_WORDS = {
    "ltd", "limited", "plc", "llp", "llc", "inc", "incorporated", "corp", "corporation", "co",
    "company", "holdings", "group", "uk", "gb", "the", "and", "of",
}


def tokens(name):
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    parts = [p for p in s.split() if p]
    kept = [p for p in parts if p not in LEGAL_WORDS]
    return kept or parts


def name_norm(name):
    return " ".join(tokens(name))


def squash(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def squash_name(name):
    return "".join(tokens(name))


def find_similar(norm, existing_norms):
    """Exact normalised match first; otherwise a careful fuzzy match (very high threshold)."""
    if norm in existing_norms:
        return norm
    if len(norm) < 7:
        return None
    first = norm.split()[0]
    pool = [e for e in existing_norms if e.split()[0] == first]
    hit = difflib.get_close_matches(norm, pool, n=1, cutoff=0.93)
    return hit[0] if hit else None


def is_agency(name):
    n = f" {(name or '').lower()} "
    return any(h in n for h in config.AGENCY_HINTS)


KEEP_PARAMS = {"id", "jobid", "job_id", "jk", "gh_jid", "jid", "vacancyid"}


def url_norm(url):
    try:
        p = urllib.parse.urlsplit((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = p.netloc.lower().removeprefix("www.")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if k.lower() in KEEP_PARAMS]
    return f"{host}{p.path.rstrip('/')}" + (("?" + urllib.parse.urlencode(sorted(q))) if q else "")


def dup_key(company, title, location):
    raw = "|".join([name_norm(company), squash(title), squash((location or "").split(",")[0])])
    return hashlib.sha1(raw.encode()).hexdigest()


def matched_roles(title):
    t = (title or "").lower()
    return [role for role, pat in config.ROLE_PATTERNS.items() if re.search(pat, t)]
