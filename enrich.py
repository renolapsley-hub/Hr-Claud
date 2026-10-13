"""Company enrichment: official website -> contact pages -> public phone numbers (+ optional HR names).
Nothing is ever invented: if a number is not found on a public page it stays 'Not found'."""
import json
import re
import urllib.parse
from dataclasses import dataclass

from bs4 import BeautifulSoup

import config
import db
import matching
import phones
from http_client import ApiHttpError, Fetcher, Page, api_get

KINDS = [  # (kind, url words) - first match wins
    ("contact", ("contact", "get-in-touch", "getintouch", "enquir", "reach-us", "speak-to")),
    ("locations", ("location", "our-offices", "offices", "branches", "find-us", "where-we")),
    ("team", ("our-team", "leadership", "our-people", "management-team", "meet-the-team", "directors", "/team")),
    ("about", ("about", "who-we-are", "our-story", "our-company")),
    ("careers", ("career", "vacanc", "join-us", "work-with-us", "work-for-us")),
]
MAIN_WORDS = re.compile(r"head office|main office|switchboard|reception|general enquir|main line|main number|"
                        r"headquarters|registered office|telephone|phone|call us|tel\b", re.I)
STRONG_MAIN = re.compile(r"head office|main office|switchboard|reception|general enquir|main line|main number|"
                         r"headquarters", re.I)
BRANCH_RE = re.compile(r"([A-Z][A-Za-z]+(?: [A-Z][A-Za-z]+)?)\s+(?:office|branch|studio|store|site|depot)")
FAX_RE = re.compile(r"\b(fax|facsimile)\s*[:.\-]?\s*$|\bf\s*:\s*$", re.I)
REG_RE = re.compile(r"(company|registration|registered|reg\.?|vat)\s*(no|number|num)?\.?\s*:?\s*$", re.I)
HR_TITLE = re.compile(r"(human resources|\bhr\b|people|talent|recruitment).*(director|head|manager|chief|officer|lead)|"
                      r"chief (people|human)", re.I)
UK_POSTCODE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]? ?\d[A-Z]{2}\b")


def _ts():
    return db.now()


# ------------------------------------------------------------------ website discovery
def _domain_of(url):
    return urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")


def _is_aggregator(url):
    d = _domain_of(url)
    return any(d == a or d.endswith("." + a) for a in config.AGGREGATOR_DOMAINS)


def _root(url):
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


def candidates_wikidata(name):
    """Official website from Wikidata (free, official API) when the company has a matching entry."""
    try:
        s = api_get("https://www.wikidata.org/w/api.php", headers={"User-Agent": config.USER_AGENT},
                    params={"action": "wbsearchentities", "search": name, "language": "en", "format": "json", "limit": 5})
        ids = [e["id"] for e in s.get("search", []) if matching.squash_name(e.get("label", "")) == matching.squash_name(name)]
        if not ids:
            return []
        d = api_get("https://www.wikidata.org/w/api.php", headers={"User-Agent": config.USER_AGENT},
                    params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims", "format": "json"})
        urls = []
        for ent in d.get("entities", {}).values():
            for claim in ent.get("claims", {}).get("P856", []):
                v = claim.get("mainsnak", {}).get("datavalue", {}).get("value")
                if v:
                    urls.append(v)
        return urls
    except (ApiHttpError, KeyError, TypeError):
        return []


def candidates_brave(name):
    if not config.BRAVE_API_KEY:
        return []
    try:
        d = api_get("https://api.search.brave.com/res/v1/web/search",
                    params={"q": f'"{name}" official website', "count": 10, "country": config.COUNTRY.upper()},
                    headers={"X-Subscription-Token": config.BRAVE_API_KEY, "Accept": "application/json"})
        return [r["url"] for r in d.get("web", {}).get("results", []) if r.get("url")]
    except (ApiHttpError, KeyError, TypeError):
        return []


def candidates_guess(name):
    toks = matching.tokens(name)
    slugs = {"".join(toks), "-".join(toks)}
    slugs = [s for s in slugs if len(s) >= 4]
    tlds = [".co.uk", ".com", ".org.uk", ".uk", ".org"] if config.COUNTRY == "gb" else [".com", f".{config.COUNTRY}"]
    return [f"https://www.{s}{t}/" for s in slugs for t in tlds][:8]


def verify_site(name, page):
    """'High' if the company name is in the page title/site name/copyright line, 'Medium' if only in the body."""
    soup = BeautifulSoup(page.html, "lxml")
    key = matching.squash_name(name)
    if not key:
        return None
    bits = [soup.title.get_text(" ") if soup.title else ""]
    for m in soup.find_all("meta", attrs={"property": ["og:site_name", "og:title"]}):
        bits.append(m.get("content", ""))
    text = soup.get_text(" ", strip=True)
    bits += re.findall(r"(?:©|copyright)[^.|]{0,80}", text, flags=re.I)
    if any(key in matching.squash(b) for b in bits):
        return "High"
    if len(key) >= 5 and key in matching.squash(text[:30000]):
        return "Medium"
    return None


def uk_signal(page):
    t = page.html[:200000]
    return (_domain_of(page.final_url or page.url).endswith(".uk") or "+44" in t or bool(UK_POSTCODE.search(t))
            or "united kingdom" in t.lower())


def find_website(c, fetcher, log):
    """Returns (url, method, confidence, home_page) or (None, ...)."""
    name = c["name"]
    tried = set()
    for method, urls in (("wikidata", candidates_wikidata(name)), ("search", candidates_brave(name)),
                         ("domain-check", candidates_guess(name))):
        for u in urls:
            if _is_aggregator(u) or _root(u) in tried:
                continue
            tried.add(_root(u))
            page = fetcher.get(_root(u))
            if not page.ok:
                continue
            conf = verify_site(name, page)
            if not conf:
                continue
            if method == "domain-check":
                if len(matching.squash_name(name)) < 8 and conf != "High":
                    continue
                if config.COUNTRY == "gb" and not uk_signal(page):
                    continue
                conf = "Medium" if conf == "High" else "Low"      # guesses never rate above Medium
            log(f"{name}: website {u} via {method} ({conf})")
            return page.final_url and _root(page.final_url) or _root(u), method, conf, page
    return None, None, None, None


# ------------------------------------------------------------------ pages
@dataclass
class SitePage:
    url: str
    kind: str
    soup: BeautifulSoup
    text: str


def _kind(href, text):
    h = (href + " " + text).lower()
    for kind, words in KINDS:
        if any(w in h for w in words):
            return kind
    return None


def _same_site(base, url):
    h = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    return h == base or h.endswith("." + base)


def _parse(page, kind):
    soup = BeautifulSoup(page.html, "lxml")
    sp = SitePage(page.final_url or page.url, kind, soup, "")
    clone = BeautifulSoup(page.html, "lxml")
    for t in clone(["script", "style", "noscript"]):
        t.decompose()
    sp.text = clone.get_text(" ", strip=True)
    return sp


def collect_site(home, fetcher):
    """Parse the homepage, then fetch up to MAX_PAGES_PER_SITE-1 contact/about/location/team pages."""
    base_url = home.final_url or home.url
    base = _domain_of(base_url)
    sp_home = _parse(home, "home")
    wanted, careers = {}, ""
    for a in sp_home.soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urllib.parse.urldefrag(urllib.parse.urljoin(base_url, href))[0]
        kind = _kind(urllib.parse.urlsplit(url).path, a.get_text(" ", strip=True))
        if kind == "careers":
            careers = careers or url
        elif kind and _same_site(base, url) and url.rstrip("/") != base_url.rstrip("/"):
            wanted.setdefault(kind, [])
            if url not in wanted[kind]:
                wanted[kind].append(url)
    if "contact" not in wanted:
        wanted["contact"] = [urllib.parse.urljoin(base_url, "/contact-us"), urllib.parse.urljoin(base_url, "/contact")]
    order = [("contact", 2), ("locations", 1), ("about", 1), ("team", 1)]
    queue = [(k, u) for k, n in order for u in wanted.get(k, [])[:n]]
    pages, notes = [sp_home], []
    for kind, url in queue:
        if len(pages) >= config.MAX_PAGES_PER_SITE:
            break
        if kind == "contact" and any(p.kind == "contact" for p in pages):
            continue                      # fallback guesses only if no contact page was found yet
        pg = fetcher.get(url)
        if pg.ok:
            pages.append(_parse(pg, kind))
        elif pg.robots_blocked:
            notes.append("robots.txt blocked " + url)
    return pages, careers, notes


# ------------------------------------------------------------------ phone extraction
def _json_ld(soup):
    for s in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(s.string or "")
        except (ValueError, TypeError):
            continue
        stack = [data]
        while stack:
            x = stack.pop()
            if isinstance(x, dict):
                yield x
                stack.extend(x.values())
            elif isinstance(x, list):
                stack.extend(x)


def extract_phones(sp, region):
    out, seen = [], set()

    def add(raw, method, before, after="", city=""):
        n = phones.normalise(raw, region)
        if not n or FAX_RE.search(before[-12:]) or REG_RE.search(before[-30:]):
            return
        label = "main" if STRONG_MAIN.search(before[-60:] + " " + after[:20]) else ""
        m = BRANCH_RE.search(before[-60:])
        branch = m.group(1) if m else ""
        key = (n["e164"], method)
        if key in seen:
            return
        seen.add(key)
        out.append({**n, "raw": raw.strip(), "method": method, "label": label, "branch": branch or city,
                    "page_kind": sp.kind, "source_url": sp.url})

    for a in sp.soup.select('a[href^="tel:"]'):
        raw = urllib.parse.unquote(a["href"][4:]).split(";")[0]
        ctx = a.parent.get_text(" ", strip=True) if a.parent else ""
        txt = a.get_text(" ", strip=True)
        add(raw, "tel-link", ctx.split(txt, 1)[0] if txt and txt in ctx else ctx)
    for obj in _json_ld(sp.soup):
        tel = obj.get("telephone")
        if isinstance(tel, str):
            addr = obj.get("address") if isinstance(obj.get("address"), dict) else {}
            add(tel, "structured-data", "", "", addr.get("addressLocality", ""))
    for raw, start, end in phones.find_in_text(sp.text, region):
        add(raw, "page-text", sp.text[max(0, start - 70):start], sp.text[end:end + 25])
    return out


def choose(all_cands, site_conf):
    """Pick the primary number, give every candidate a confidence, return (primary, alternatives)."""
    if not all_cands:
        return None, []
    by = {}
    for c in all_cands:
        by.setdefault(c["e164"], []).append(c)

    def strength(c):
        return ({"tel-link": 3, "structured-data": 3, "page-text": 1}[c["method"]]
                + {"contact": 3, "locations": 2, "about": 2, "home": 1}.get(c["page_kind"], 0)
                + (3 if c["label"] == "main" else 0))

    scored = []
    for e164, cs in by.items():
        best = max(cs, key=strength)
        best["_score"] = sum(strength(c) for c in cs)
        best["_pages"] = {c["source_url"] for c in cs}
        scored.append(best)
    scored.sort(key=lambda c: -c["_score"])
    top = scored[0]
    ambiguous = len(scored) > 1 and scored[1]["_score"] == top["_score"] and top["label"] != "main"
    for c in scored:
        structured = c["method"] in ("tel-link", "structured-data")
        core_page = c["page_kind"] in ("contact", "locations", "about")
        if site_conf == "Low" or c["page_kind"] not in ("contact", "locations", "about", "home"):
            conf = "Low"
        elif (structured and core_page) or (c["label"] == "main" and core_page):
            conf = "High"
        else:
            conf = "Medium"
        if c is top and ambiguous:
            conf = "Low"
        c["confidence"] = conf
        c["type"] = ("main office" if c["label"] == "main" or (c is top and not ambiguous and len(scored) == 1)
                     else "branch" if c["branch"] else "other publicly listed business number")
        c["location"] = c["branch"] or (c["area"] if c["country"] == "GB" else "")
        c["contact_url"] = c["source_url"] if c["page_kind"] == "contact" else ""
        c["primary"] = c is top
    return top, scored[1:6]


def extract_decision_makers(pages):
    out = []
    for sp in pages:
        for obj in _json_ld(sp.soup):
            if obj.get("@type") == "Person" and obj.get("name") and isinstance(obj.get("jobTitle"), str) \
                    and HR_TITLE.search(obj["jobTitle"]):
                out.append((str(obj["name"]).strip(), obj["jobTitle"].strip(), sp.url))
    return out


# ------------------------------------------------------------------ registry (optional, UK)
def registry_lookup(c):
    if not (config.COMPANIES_HOUSE_API_KEY and config.COUNTRY == "gb"):
        return
    try:
        d = api_get("https://api.company-information.service.gov.uk/search/companies",
                    params={"q": c["name"], "items_per_page": 5}, auth=(config.COMPANIES_HOUSE_API_KEY, ""))
    except ApiHttpError:
        return
    for it in d.get("items", []):
        if matching.name_norm(it.get("title", "")) == c["name_norm"]:
            db.update_company(c["id"], company_number=it.get("company_number"),
                              registered_address=it.get("address_snippet"), registry_status=it.get("company_status"))
            return


# ------------------------------------------------------------------ main entry
def _fresh(c):
    if not c["enriched_at"] or c["enrichment_status"] in ("Pending", None) or c["enrichment_status"].startswith("Error"):
        return False
    import datetime as dt
    age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(c["enriched_at"])
    return age.days < config.ENRICH_TTL_DAYS


def enrich_company(cid, force=False, fetcher=None, log=lambda m: None):
    fetcher = fetcher or Fetcher()
    c = db.get_company(cid)
    if not force and _fresh(c):
        return c["enrichment_status"]
    ts = _ts()

    def finish(status, note="", **extra):
        db.update_company(cid, enrichment_status=status, enrichment_note=note, enriched_at=ts, **extra)
        log(f"{c['name']}: {status}")
        return status

    try:
        registry_lookup(c)
        site, method, conf, home = c["website"], c["website_method"], c["website_confidence"], None
        if site:
            home = fetcher.get(site)
            if not home.ok:
                return finish("Site unreachable", home.error)
        else:
            site, method, conf, home = find_website(c, fetcher, log)
            if not site:
                return finish("Website not found", "No official site could be confirmed automatically. "
                              "You can paste the website manually.")
        pages, careers, notes = collect_site(home, fetcher)
        cands = []
        for sp in pages:
            cands += extract_phones(sp, config.PHONE_REGION)
        primary, alts = choose(cands, conf if conf in ("High", "Medium", "Low") else "Low")
        common = dict(website=site, website_method=method, website_confidence=conf, careers_url=careers or c["careers_url"],
                      last_verified=ts)
        if primary:
            for cand in [primary] + alts:
                db.save_phone(cid, cand, ts)
        for name, title, src in extract_decision_makers(pages):
            db.save_decision_maker(cid, name, title, src, "From company website - check before use")
        if primary:
            return finish("Phone found", "; ".join(notes), **common)
        return finish("Website found - no public phone", "; ".join(notes), **common)
    except Exception as e:  # never let one company stop a batch
        return finish(f"Error: {e.__class__.__name__}", str(e)[:200])
