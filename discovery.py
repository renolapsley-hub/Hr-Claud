"""Permitted, best-effort public job discovery and structured page parsing."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote_plus, urljoin, urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

USER_AGENT = ("Mozilla/5.0 (compatible; HRLeadFinder/2.0; +https://example.org/bot-info)")
DEFAULT_TIMEOUT = 12
SEARCH_ENDPOINTS = (
    ("https://html.duckduckgo.com/html/", "q"),
    ("https://lite.duckduckgo.com/lite/", "q"),
)

ROLE_TERMS = [
    "HR Manager", "Head of HR", "CHRO", "HR Director",
    "Talent Acquisition Manager", "Talent Acquisition Director",
    "HR Business Partner", "Recruitment Manager",
    "People Operations Manager", "HR Officer",
]
LOCATIONS = ["United Kingdom", "Glasgow", "Edinburgh", "London", "Manchester",
             "Birmingham", "Remote UK"]

def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def normalise_url(url: str) -> str:
    """Canonicalise URL conservatively; preserve query strings that may identify jobs."""
    url = (url or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    try:
        p = urlparse(url)
        if not p.netloc:
            return ""
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = re.sub(r"/{2,}", "/", p.path or "/")
        if path != "/":
            path = path.rstrip("/")
        return p._replace(scheme="https", netloc=host, path=path, fragment="").geturl()
    except Exception:
        return ""

def google_jobs_url(query, location="United Kingdom"):
    return "https://www.google.com/search?ibp=htl;jobs&q=" + quote_plus(f"{query} {location}")

def google_search_url(query):
    return "https://www.google.com/search?q=" + quote_plus(query)

def search_url(source, query):
    q = quote_plus(query)
    return {
        "Google Jobs": "https://www.google.com/search?ibp=htl;jobs&q=" + q,
        "Google Search": "https://www.google.com/search?q=" + q,
        "Indeed": "https://uk.indeed.com/jobs?q=" + q,
        "LinkedIn": "https://www.linkedin.com/jobs/search/?keywords=" + q,
        "Adzuna": "https://www.adzuna.co.uk/search?q=" + q,
    }.get(source, google_search_url(query))

def safe_get(url, timeout=DEFAULT_TIMEOUT, session=None):
    if not url or urlparse(url).scheme not in ("http", "https"):
        return None
    client = session or requests
    try:
        response = client.get(url, timeout=timeout, headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-GB,en;q=0.9",
        }, allow_redirects=True)
        if response.status_code >= 400:
            return None
        content_type = response.headers.get("content-type", "").lower()
        if content_type and not any(t in content_type for t in ("html", "text", "json", "xml")):
            return None
        return response
    except requests.RequestException:
        return None

def _decode_search_url(href):
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        return parse_qs(parsed.query).get("uddg", [href])[0]
    return href

def ddg_search(query, limit=8, session=None):
    """Best-effort parse of public DDG HTML; a blocked/changed page returns []."""
    client = session or requests
    for endpoint, key in SEARCH_ENDPOINTS:
        try:
            response = client.get(endpoint, params={key: query}, timeout=DEFAULT_TIMEOUT,
                                  headers={"User-Agent": USER_AGENT,
                                           "Accept-Language": "en-GB,en;q=0.9"})
            if response.status_code != 200 or len(response.text) < 300:
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            anchors = soup.select("a.result__a, a.result-link")
            found, seen = [], set()
            for a in anchors:
                title = " ".join(a.get_text(" ", strip=True).split())
                href = _decode_search_url(a.get("href", "").strip())
                href = unquote(href) if "uddg=" in href else href
                if not title or not href.startswith(("http://", "https://")):
                    continue
                host = urlparse(href).netloc.lower()
                if not host or "duckduckgo.com" in host:
                    continue
                if any(block in host for block in (
                    "facebook.com", "instagram.com", "google.com", "youtube.com"
                )):
                    continue
                href = normalise_url(href)
                if not href or href in seen:
                    continue
                seen.add(href)
                parent = a.find_parent(class_=re.compile(r"result|web-result", re.I))
                snippet = ""
                if parent:
                    sn = parent.select_one(".result__snippet, .result-snippet, .snippet")
                    if sn:
                        snippet = sn.get_text(" ", strip=True)
                found.append({"title": title, "url": href, "snippet": snippet,
                              "retrieval_method": "public_search_html"})
                if len(found) >= limit:
                    return found
            if found:
                return found
        except requests.RequestException:
            continue
    return []

def flatten_jsonld(soup):
    output = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text()
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        queue = data if isinstance(data, list) else [data]
        while queue:
            obj = queue.pop(0)
            if isinstance(obj, dict):
                output.append(obj)
                graph = obj.get("@graph")
                if isinstance(graph, list):
                    queue.extend(graph)
            elif isinstance(obj, list):
                queue.extend(obj)
    return output

def type_has(obj, wanted):
    typ = obj.get("@type", [])
    if isinstance(typ, str):
        typ = [typ]
    return any(wanted.lower() == str(t).split("/")[-1].lower() for t in typ)

def text_value(value):
    return value.strip() if isinstance(value, str) else ""

def org_name(value):
    if isinstance(value, dict):
        return text_value(value.get("name")) or text_value(value.get("legalName"))
    return text_value(value)

def parse_job_html(html, page_url, fetched_at=None):
    soup = BeautifulSoup(html or "", "html.parser")
    title = ""
    og = soup.find("meta", attrs={"property": "og:title"})
    if og:
        title = text_value(og.get("content"))
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    canonical_tag = soup.find("link", rel="canonical")
    canonical = canonical_tag.get("href") if canonical_tag else ""
    job_url = normalise_url(urljoin(page_url, canonical or page_url))
    company = location = posted = description = company_website = job_id = ""
    extraction_status = "Page fetched; no JobPosting structured data found"
    active_status = "Unknown"
    for obj in flatten_jsonld(soup):
        if not type_has(obj, "JobPosting"):
            continue
        title = text_value(obj.get("title")) or title
        company = org_name(obj.get("hiringOrganization"))
        posted = text_value(obj.get("datePosted"))
        valid_through = text_value(obj.get("validThrough"))
        job_id = text_value(obj.get("identifier")) if isinstance(obj.get("identifier"), str) else ""
        description = BeautifulSoup(str(obj.get("description", "")), "html.parser").get_text(" ", strip=True)[:2000]
        loc = obj.get("jobLocation")
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        if isinstance(loc, dict):
            address = loc.get("address", {})
            if isinstance(address, dict):
                location = ", ".join(x for x in [
                    text_value(address.get("addressLocality")),
                    text_value(address.get("addressRegion")),
                    text_value(address.get("addressCountry")),
                ] if x)
        org = obj.get("hiringOrganization")
        if isinstance(org, dict):
            same = org.get("sameAs")
            if isinstance(same, list):
                same = same[0] if same else ""
            company_website = text_value(org.get("url")) or text_value(same)
        active_status = "Potentially expired" if valid_through and valid_through[:10] < now_iso()[:10] else "Active or unconfirmed"
        extraction_status = "Structured JobPosting extracted"
        break
    if not company:
        extraction_status += "; company requires review"
    if not title:
        extraction_status += "; title requires review"
    host = urlparse(job_url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return {
        "job_id": job_id,
        "company": company,
        "job_title": title,
        "job_url": job_url,
        "job_location": location,
        "date_posted": posted,
        "job_summary": description,
        "company_website": company_website,
        "job_source": host,
        "discovered_at": fetched_at or now_iso(),
        "active_status": active_status,
        "extraction_status": extraction_status,
        "retrieval_method": "job_page",
        "company_match_reason": "Schema.org JobPosting" if company else "Needs manual review",
        "company_match_confidence": "High" if company and company_website else ("Medium" if company else "Low"),
        "office_phone": "",
        "phone_type": "",
        "phone_source_url": "",
        "phone_source_type": "",
        "phone_checked_at": "",
        "phone_status": "Not checked",
        "phone_confidence": "",
        "phone_reason": "",
        "decision_maker_name": "",
        "decision_maker_title": "",
        "decision_maker_source": "",
        "decision_maker_status": "Not checked",
        "outreach_status": "New",
        "notes": "",
        "last_checked": "",
    }

def company_hint_from_title(title):
    # Deliberately conservative; only a hint, never a verified company match.
    for sep in (" at ", " | ", " - ", " – ", " — "):
        parts = title.rsplit(sep, 1)
        if len(parts) == 2:
            candidate = parts[-1].strip()
            if 2 <= len(candidate) <= 80 and not any(x in candidate.lower() for x in ("jobs", "careers", "vacancy")):
                return candidate
    return ""

def likely_job_result(result):
    text = (result.get("title", "") + " " + result.get("snippet", "") + " " + result.get("url", "")).lower()
    return any(s in text for s in (
        "job", "career", "vacancy", "hr manager", "human resources",
        "talent acquisition", "people partner", "recruitment", "chro"
    ))

def discover_jobs(query, limit=8, delay=0.2, session=None):
    results = ddg_search(query, limit=max(limit * 2, limit), session=session)
    records = []
    for result in results:
        if not likely_job_result(result):
            continue
        page = safe_get(result["url"], session=session)
        if page:
            record = parse_job_html(page.text, page.url)
            if not record["job_title"]:
                record["job_title"] = result["title"]
                record["extraction_status"] += "; title from search result"
            if not record["job_summary"]:
                record["job_summary"] = result.get("snippet", "")
            if not record["company"]:
                record["company"] = company_hint_from_title(result["title"])
                if record["company"]:
                    record["company_match_reason"] = "Title-derived hint; unverified"
                    record["company_match_confidence"] = "Low"
            record["retrieval_method"] = "public_search_then_job_page"
        else:
            record = {
                "job_id": "", "company": company_hint_from_title(result["title"]),
                "job_title": result["title"], "job_url": result["url"],
                "job_location": "", "date_posted": "", "job_summary": result.get("snippet", ""),
                "company_website": "", "job_source": urlparse(result["url"]).netloc,
                "discovered_at": now_iso(), "active_status": "Unknown",
                "extraction_status": "Search result only; job page inaccessible; review required",
                "retrieval_method": "public_search_html",
                "company_match_reason": "Title-derived hint; unverified",
                "company_match_confidence": "Low", "office_phone": "", "phone_type": "",
                "phone_source_url": "", "phone_source_type": "", "phone_checked_at": "",
                "phone_status": "Not checked", "phone_confidence": "", "phone_reason": "",
                "decision_maker_name": "", "decision_maker_title": "", "decision_maker_source": "",
                "decision_maker_status": "Not checked", "outreach_status": "New", "notes": "",
                "last_checked": "",
            }
        records.append(record)
        if len(records) >= limit:
            break
        if delay:
            time.sleep(delay)
    return records, len(results)



def parse_rss_feed(feed_url, limit=20, session=None):
    """Parse a user-supplied public RSS/Atom feed. Feed URLs must be public and accessible."""
    import xml.etree.ElementTree as ET
    response = safe_get(feed_url, session=session)
    if not response:
        return []
    try:
        xml_root = ET.fromstring(response.text)
    except ET.ParseError:
        return []
    records = []
    # RSS 2.0 channel/item and Atom entry support.
    entries = list(xml_root.findall(".//item"))
    if not entries:
        entries = [node for node in xml_root.iter() if node.tag.split("}")[-1].lower() == "entry"]
    for entry in entries[:limit]:
        def child_text(*names):
            for child in list(entry):
                if child.tag.split("}")[-1].lower() in [n.lower() for n in names] and child.text:
                    return child.text.strip()
            return ""
        title = child_text("title")
        link = child_text("link")
        if not link:
            for child in list(entry):
                if child.tag.split("}")[-1].lower() == "link":
                    link = child.attrib.get("href", "")
                    if link:
                        break
        link = urljoin(feed_url, link)
        summary = child_text("description", "summary", "content")
        posted = child_text("pubDate", "published", "updated", "date")
        if not title or not link.startswith(("http://", "https://")):
            continue
        records.append({
            "title": BeautifulSoup(title, "html.parser").get_text(" ", strip=True),
            "url": normalise_url(link),
            "snippet": BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)[:2000],
            "date_posted": posted,
            "retrieval_method": "public_rss_feed",
        })
    return records

def parse_manual_urls(text):
    urls = []
    for line in (text or "").splitlines():
        value = line.strip().strip(" ,;")
        if value.startswith(("https://", "http://")):
            norm = normalise_url(value)
            if norm and norm not in urls:
                urls.append(norm)
    return urls
