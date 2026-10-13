import pandas as pd

EXPORT_COLUMNS = [
    ("company", "Company Name"), ("job_title", "Job Title"), ("job_url", "Job URL"),
    ("job_location", "Job Location"), ("date_posted", "Date Posted"),
    ("job_summary", "Job Description"), ("job_source", "Job Source"),
    ("company_website", "Company Website"), ("office_phone", "Main Office Phone"),
    ("phone_type", "Phone Type"), ("phone_source_url", "Phone Source URL"),
    ("phone_status", "Phone Verification Status"), ("phone_confidence", "Phone Confidence"),
    ("decision_maker_name", "Decision-Maker Name"), ("decision_maker_title", "Decision-Maker Title"),
    ("decision_maker_source", "Decision-Maker Source"), ("discovered_at", "Date Discovered"),
    ("last_checked", "Date Last Checked"), ("outreach_status", "Outreach Status"), ("notes", "Notes")
]

def export_dataframe(records):
    frame = pd.DataFrame(records)
    cols = [field for field, _ in EXPORT_COLUMNS]
    frame = frame.reindex(columns=cols)
    frame.columns = [label for _, label in EXPORT_COLUMNS]
    return frame

def export_csv_bytes(records):
    return export_dataframe(records).to_csv(index=False).encode("utf-8-sig")
