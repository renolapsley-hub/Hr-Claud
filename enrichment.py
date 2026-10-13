"""Public company contact-page enrichment with conservative verification."""
from __future__ import annotations
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse, quote_plus

import requests
from bs4 import BeautifulSoup

from discovery import safe_get, ddg_search, now_iso

try:
    import phonenumbers
except ImportError:
    phonenumbers = None

BLOCKED_HOST_PARTS = (
    "google.", "duckduckgo.", "linkedin.", "indeed.", "facebook.", "instagram.",
    "wikipedia.", "youtube.", "x.com", "glassdoor."
)
CONTACT_TERMS = ("contact", "about", "location", "office", "get-in-touch", "reach-us", "our-team")
PHONE_RE = re.compile(r"(?<!\w)(?:\+44|0044|0)\s?(?:\(?\d{2,5}\)?[\s.-]?)\d{3,4}[\s.-]?\d{3,4}(?!\w)")

def normalise_phone(raw, region="GB"):
    raw = re.sub(r"\s+", " ", (raw or "").strip(" .,:;|"))
    if not raw:
        return ""
    if phonenumbers:
        try:
            number = phonenumbers.parse(raw, region)
            if phonenumbers.is_possible_number(number):
                return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.INTERNATIONAL)
            return ""
        except Exception:
            return ""
    digits = re.sub(r"\D", "", raw)
    if 10 <= len(digits) <= 13:
        if raw.startswith("0") and len(digits) in (10, 11):
            return "+44 " + digits[1:]
        return raw
    return ""

def extract_phone_candidates(html):
    soup = BeautifulSoup(html or "", "html.parser")
    candidates = []
    for a in soup.select('a[href^="tel:"]'):
        raw = a.get("href", "")[4:]
        number = normalise_phone(raw)
        if number and number not in candidates:
            candidates.append(number)
    # Only inspect visible body text; avoid script/style data. These are candidates, not verified numbers.
    for node in soup(["script", "style", "noscript", "svg"]):
        node.decompose()
    text = soup.get_text(" ", strip=True)
    for match in PHONE_RE.finditer(text):
        number = normalise_phone(match.group(0))
        if number and number not in candidates:
            candidates.append(number)
    return candidates

def is_blocked_host(url):
    host = urlparse(url).netloc.lower()
    return any(part in host for part in BLOCKED_HOST_PARTS)

def clean_site_url(url):
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc or is_blocked_host(url):
        return ""
    return f"https://{parsed.netloc.lower()}"

def find_official_site(company, known_site=""):
    known = clean_site_url(known_site)
    if known:
        return known, "Job structured data or existing employer website", "Medium"
    if not company.strip():
        return "", "No company name", "None"
    results = ddg_search(f'"{company}" official website contact', limit=8)
    for result in results:
        site = clean_site_url(result.get("url", ""))
        if site:
            return site, "Public search result; official status requires confirmation", "Low"
    return "", "No likely official website found in accessible search results", "None"

def discover_contact_pages(site, max_pages=5):
    if not site:
        return []
    response = safe_get(site)
    if not response:
        return [site]
    soup = BeautifulSoup(response.text, "html.parser")
    pages = [response.url]
    base_host = urlparse(response.url).netloc.lower()
    for a in soup.find_all("a", href=True):
        href = urljoin(response.url, a["href"])
        label = (a.get_text(" ", strip=True) + " " + a["href"]).lower()
        if not any(term in label for term in CONTACT_TERMS):
            continue
        if urlparse(href).netloc.lower() != base_host:
            continue
        if href not in pages:
            pages.append(href)
        if len(pages) >= max_pages:
            break
    return pages[:max_pages]

def classify_phone(phone, page_url, visible_text=""):
    host = urlparse(page_url).netloc.lower()
    text = (visible_text or "").lower()
    if any(term in text for term in ("switchboard", "main office", "head office", "general enquiries", "general inquiries")):
        return "Main switchboard", "Medium", "Official page includes a main-office/switchboard label; verify manually"
    if any(term in text for term in ("recruitment", "talent acquisition", "careers")):
        return "Recruitment contact", "Low", "Page context appears recruitment-related, not necessarily the main switchboard"
    return "Business number — type unconfirmed", "Low", "Number found on a likely company page; no explicit switchboard label detected"

def enrich_company(company, known_site="", max_pages=5, delay=0.15):
    site, match_reason, match_confidence = find_official_site(company, known_site)
    result = {
        "company_website": site, "company_match_reason": match_reason,
        "company_match_confidence": match_confidence, "office_phone": "",
        "phone_type": "", "phone_source_url": "", "phone_source_type": "",
        "phone_checked_at": now_iso(), "phone_status": "Not found",
        "phone_confidence": "None", "phone_reason": "",
        "enrichment_status": "No company website found",
    }
    if not site:
        result["phone_reason"] = match_reason
        return result
    result["enrichment_status"] = "Company website found; checking public contact pages"
    for page_url in discover_contact_pages(site, max_pages=max_pages):
        response = safe_get(page_url)
        if not response:
            continue
        candidates = extract_phone_candidates(response.text)
        if candidates:
            soup = BeautifulSoup(response.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            number = candidates[0]
            phone_type, confidence, reason = classify_phone(number, response.url, page_text)
            result.update({
                "office_phone": number,
                "phone_type": phone_type,
                "phone_source_url": response.url,
                "phone_source_type": "Official-site candidate" if match_confidence != "Low" else "Candidate website; verify official ownership",
                "phone_status": "Candidate — manual verification required",
                "phone_confidence": confidence if match_confidence != "Low" else "Low",
                "phone_reason": reason,
                "enrichment_status": f"Found {len(candidates)} candidate number(s) on a public page",
            })
            return result
        if delay:
            time.sleep(delay)
    result.update({
        "phone_status": "Not found on checked pages",
        "phone_reason": "No plausible public UK telephone number found on checked pages",
        "enrichment_status": "Website found; no phone candidate detected",
    })
    return result

def mark_phone_verified(record, verified):
    """Manual review only; the user must have checked the source URL."""
    record = dict(record)
    record["phone_status"] = "Verified on source page" if verified else "Rejected after review"
    record["phone_confidence"] = "High" if verified else "None"
    record["phone_reason"] = "Manually confirmed by user against source page" if verified else "User rejected candidate after review"
    record["last_checked"] = now_iso()
    return record
