"""Settings. Secrets come from environment variables only (never from the browser)."""
import os

try:  # optional: loads a local .env file when python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def env(key, default=""):
    return os.environ.get(key, default).strip()


DB_PATH = env("DB_PATH", "hr_leads.db")
COUNTRY = env("COUNTRY", "gb").lower()
if COUNTRY == "uk":
    COUNTRY = "gb"
PHONE_REGION = COUNTRY.upper()          # used by the phone parser, e.g. "GB"
DEFAULT_LOCATION = env("DEFAULT_LOCATION", "")  # e.g. "London" (blank = whole country)

# --- credentials (all optional; each unlocks one feature) ---
ADZUNA_APP_ID = env("ADZUNA_APP_ID")
ADZUNA_APP_KEY = env("ADZUNA_APP_KEY")
REED_API_KEY = env("REED_API_KEY")                      # UK only
BRAVE_API_KEY = env("BRAVE_API_KEY")                    # optional website finder
COMPANIES_HOUSE_API_KEY = env("COMPANIES_HOUSE_API_KEY")  # optional UK registry check
APP_PASSWORD = env("APP_PASSWORD")                      # protects the dashboard (strongly advised online)
CONTACT_EMAIL = env("CONTACT_EMAIL", "set-CONTACT_EMAIL@example.invalid")

# --- polite crawling ---
USER_AGENT = env("USER_AGENT", f"HRLeadFinder/1.0 (business research tool; contact: {CONTACT_EMAIL})")
REQUEST_TIMEOUT = int(env("REQUEST_TIMEOUT", "12"))
HOST_DELAY = float(env("HOST_DELAY", "1.5"))     # seconds between requests to the same site
MAX_BYTES = 1_500_000                            # per page
MAX_PAGES_PER_SITE = int(env("MAX_PAGES_PER_SITE", "5"))
ENRICH_TTL_DAYS = int(env("ENRICH_TTL_DAYS", "30"))   # don't re-query a company inside this window
STALE_AFTER_DAYS = int(env("STALE_AFTER_DAYS", "14"))  # unseen this long -> "stale"
ALLOW_MOBILE_NUMBERS = env("ALLOW_MOBILE_NUMBERS", "0") == "1"

# --- the roles you asked for (one search per role per source) ---
ROLE_QUERIES = [
    "HR Manager", "Head of HR", "HR Director", "Chief Human Resources Officer", "CHRO",
    "Talent Acquisition Manager", "Recruitment Manager", "People and Culture Manager",
    "HR Business Partner", "HR Officer", "People Operations Manager", "Talent Acquisition Director",
]
# A job is kept only if its TITLE matches one of these (API searches are fuzzy and return noise).
ROLE_PATTERNS = {
    "HR Manager": r"\b(hr|human resources) manager\b",
    "Head of HR": r"\bhead of (hr|human resources|people)\b",
    "HR Director": r"\b(hr|human resources|people) director\b",
    "CHRO": r"\bchro\b|\bchief (human resources|people|hr) officer\b",
    "Talent Acquisition Manager": r"\btalent acquisition manager\b",
    "Recruitment Manager": r"\brecruitment manager\b",
    "People and Culture Manager": r"\bpeople (and|&) culture (manager|lead|partner|director|head)\b",
    "HR Business Partner": r"\b(hr|human resources) business partner\b|\bhrbp\b",
    "HR Officer": r"\bhr officer\b|\bhuman resources officer\b",
    "People Operations Manager": r"\bpeople (operations|ops) manager\b",
    "Talent Acquisition Director": r"\b(talent acquisition|recruitment) director\b|\bdirector of (talent acquisition|recruitment)\b",
}

# Employers whose name contains these are probably recruitment agencies, not the real employer.
AGENCY_HINTS = [
    "recruitment", "recruiting", "resourcing", "staffing", "talent solutions", "executive search",
    "search & selection", "search and selection", "personnel", "hays", "michael page", "page personnel",
    "robert half", "randstad", "adecco", "manpower", "reed ", "pertemps", "kelly services",
    "harvey nash", "venn group", "tate ", "badenoch", "morgan mckinley", "nigel frank",
]
# Never treat these as a company's "official website".
AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "indeed.co.uk", "glassdoor.com", "glassdoor.co.uk", "facebook.com",
    "twitter.com", "x.com", "instagram.com", "youtube.com", "wikipedia.org", "companieshouse.gov.uk",
    "find-and-update.company-information.service.gov.uk", "endole.co.uk", "opencorporates.com",
    "dnb.com", "zoominfo.com", "crunchbase.com", "yell.com", "reed.co.uk", "totaljobs.com",
    "adzuna.co.uk", "cv-library.co.uk", "monster.co.uk", "jooble.org", "bloomberg.com", "trustpilot.com",
    "uk.linkedin.com", "check-a-company.co.uk", "companycheck.co.uk", "northdata.com", "rocketreach.co",
}

DEFAULT_WEIGHTS = {
    "active_vacancy": 30,        # at least one active relevant HR vacancy
    "per_extra_vacancy": 5,      # each additional relevant vacancy...
    "extra_vacancy_cap": 15,     # ...up to this many points
    "posted_3d": 20, "posted_7d": 15, "posted_14d": 10, "posted_30d": 5,
    "website_verified": 10, "website_probable": 5,
    "phone_high": 15, "phone_medium": 10, "phone_low": 4,
    "decision_maker": 10,
    "agency_penalty": -25,       # poster looks like a recruitment agency
}
