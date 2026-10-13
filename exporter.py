"""CSV / Excel export (one row per job; opens cleanly in Excel and Google Sheets)."""
import io
import re

import pandas as pd

import db

COLUMNS = ["Company Name", "Job Title", "Job Listing URL", "Job Description", "Job Location", "Date Posted",
           "Job Source", "Company Website", "Main Office Phone", "Phone Type", "Phone Verification Status",
           "Phone Source URL", "Contact Page URL", "Decision-Maker Name", "Decision-Maker Job Title",
           "Decision-Maker Source URL", "Lead Score", "Date Discovered", "Date Last Verified", "Outreach Status",
           "Notes"]
OUTREACH_LABELS = {"new": "New", "contacted": "Contacted", "qualified": "Qualified", "not_relevant": "Not relevant"}


def safe(v):
    """Stop spreadsheet formula injection from scraped text."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        if re.fullmatch(r"\+[\d ]+", v):      # a plain international phone number is safe as-is
            return v
        return "'" + v
    return v


def build_rows(include_expired=False, include_agencies=True):
    rows = []
    for c in db.lead_rows():
        if c["is_agency"] and not include_agencies:
            continue
        d = db.company_detail(c["id"])
        p = c.get("phone")
        dm = d["decision_makers"][0] if d["decision_makers"] else {}
        jobs = d["jobs"] if include_expired else [j for j in d["jobs"] if j["status"] in ("active", "stale")]
        for j in jobs or [None]:
            if j is None:
                continue
            rows.append({
                "Company Name": c["name"], "Job Title": j["title"], "Job Listing URL": j["url"],
                "Job Description": j["description"], "Job Location": j["location"], "Date Posted": j["date_posted"],
                "Job Source": j["source"], "Company Website": c["website"] or "",
                "Main Office Phone": p["number_intl"] if p else "Not found",
                "Phone Type": p["phone_type"] if p else "",
                "Phone Verification Status": p["verification"] if p else "Not found",
                "Phone Source URL": p["source_url"] if p else "", "Contact Page URL": p["contact_page_url"] if p else "",
                "Decision-Maker Name": dm.get("name", ""), "Decision-Maker Job Title": dm.get("title", ""),
                "Decision-Maker Source URL": dm.get("source_url", ""), "Lead Score": c["score"],
                "Date Discovered": j["retrieved_at"][:10], "Date Last Verified": (c["last_verified"] or "")[:10],
                "Outreach Status": OUTREACH_LABELS.get(c["outreach_status"], c["outreach_status"]),
                "Notes": c["outreach_notes"] or ""})
    rows.sort(key=lambda r: (-r["Lead Score"], r["Company Name"]))
    return rows


def frame(**kw):
    df = pd.DataFrame(build_rows(**kw), columns=COLUMNS)
    return df.apply(lambda col: col.map(safe)) if len(df) else df


def to_csv(**kw):
    # utf-8-sig adds the byte-order mark Excel needs to show £, é etc. correctly
    return frame(**kw).to_csv(index=False).encode("utf-8-sig")


def to_xlsx(**kw):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df = frame(**kw)
        df.to_excel(xw, index=False, sheet_name="Leads")
        ws = xw.sheets["Leads"]
        ws.freeze_panes = "A2"
        for i, col in enumerate(COLUMNS, start=1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = 60 if "Description" in col else 24
    return buf.getvalue()
