"""Job discovery. Each source uses an official API with your own free key. Add a new source by
subclassing Source and appending it to SOURCES."""
import datetime as dt
import html
import re
from dataclasses import dataclass

import config
import matching
from http_client import ApiHttpError, api_get


@dataclass
class JobRecord:
    company: str
    title: str
    url: str
    source: str
    source_id: str
    description: str = ""
    location: str = ""
    employment_type: str = ""
    date_posted: str = ""


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def parse_date(value):
    if not value:
        return ""
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return dt.datetime.strptime(value[:len(dt.datetime.now().strftime(fmt))], fmt).date().isoformat()
        except ValueError:
            continue
    return ""


class Source:
    name = "base"
    setup_hint = ""

    def configured(self):
        return False

    def fetch(self, role, location, pages, max_days):
        raise NotImplementedError


class AdzunaSource(Source):
    name = "Adzuna"
    setup_hint = "Set ADZUNA_APP_ID and ADZUNA_APP_KEY (free at developer.adzuna.com)."

    def configured(self):
        return bool(config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY)

    def fetch(self, role, location, pages, max_days):
        out = []
        for page in range(1, pages + 1):
            params = {"app_id": config.ADZUNA_APP_ID, "app_key": config.ADZUNA_APP_KEY, "results_per_page": 50,
                      "what_phrase": role, "max_days_old": max_days, "sort_by": "date",
                      "content-type": "application/json"}
            if location:
                params["where"] = location
            data = api_get(f"https://api.adzuna.com/v1/api/jobs/{config.COUNTRY}/search/{page}", params=params)
            results = data.get("results", [])
            for r in results:
                out.append(JobRecord(
                    company=clean((r.get("company") or {}).get("display_name")), title=clean(r.get("title")),
                    url=r.get("redirect_url", ""), source=self.name, source_id=str(r.get("id", "")),
                    description=clean(r.get("description")), location=clean((r.get("location") or {}).get("display_name")),
                    employment_type=" ".join(x for x in [r.get("contract_time"), r.get("contract_type")] if x).replace("_", " "),
                    date_posted=parse_date(r.get("created"))))
            if len(results) < 50:
                break
        return out


class ReedSource(Source):
    name = "Reed"
    setup_hint = "Set REED_API_KEY (free at reed.co.uk/developers/jobseeker). UK only."

    def configured(self):
        return bool(config.REED_API_KEY) and config.COUNTRY == "gb"

    def fetch(self, role, location, pages, max_days):
        out, cutoff = [], dt.date.today() - dt.timedelta(days=max_days)
        for page in range(pages):
            params = {"keywords": role, "resultsToTake": 100, "resultsToSkip": page * 100}
            if location:
                params["locationName"] = location
            data = api_get("https://www.reed.co.uk/api/1.0/search", params=params, auth=(config.REED_API_KEY, ""))
            results = data.get("results", [])
            for r in results:
                posted = parse_date(r.get("date"))
                if posted and posted < cutoff.isoformat():
                    continue
                kind = ", ".join(k for k, f in (("full time", "fullTime"), ("part time", "partTime")) if r.get(f))
                out.append(JobRecord(
                    company=clean(r.get("employerName")), title=clean(r.get("jobTitle")), url=r.get("jobUrl", ""),
                    source=self.name, source_id=str(r.get("jobId", "")), description=clean(r.get("jobDescription")),
                    location=clean(r.get("locationName")), employment_type=kind, date_posted=posted))
            if len(results) < 100:
                break
        return out


SOURCES = [AdzunaSource(), ReedSource()]


def source_status():
    return [{"name": s.name, "configured": s.configured(), "hint": "" if s.configured() else s.setup_hint}
            for s in SOURCES]


def discover(roles, location, pages, max_days, log, progress):
    """Run every configured source. One failing source never stops the others.
    Returns (records, errors)."""
    records, errors = [], []
    active = [s for s in SOURCES if s.configured()]
    if not active:
        errors.append("No job source is configured. Add an Adzuna or Reed key (see README).")
        return records, errors
    total, done = len(active) * len(roles), 0
    for src in active:
        for role in roles:
            done += 1
            progress(done, total)
            try:
                found = src.fetch(role, location, pages, max_days)
                kept = [r for r in found if r.company and r.title and matching.matched_roles(r.title)]
                records.extend(kept)
                log(f"{src.name}: '{role}' -> {len(found)} results, {len(kept)} relevant")
            except ApiHttpError as e:
                errors.append(f"{src.name}: {e}")
                log(f"{src.name} problem: {e}")
                if e.status in (401, 403, 429):   # no point hammering a source that refuses us
                    break
            except Exception as e:  # keep going whatever happens
                errors.append(f"{src.name}: unexpected error ({e.__class__.__name__})")
                log(f"{src.name} unexpected error: {e}")
    return records, errors
