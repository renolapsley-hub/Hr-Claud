"""SQLite persistence for leads. SQLite is persistent only when the host preserves its disk."""
from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_DB = "hr_leads.sqlite3"

FIELDS = [
    "job_id", "company", "job_title", "job_url", "job_location", "date_posted",
    "job_summary", "company_website", "job_source", "discovered_at", "active_status",
    "extraction_status", "retrieval_method", "company_match_reason", "company_match_confidence",
    "office_phone", "phone_type", "phone_source_url", "phone_source_type", "phone_checked_at",
    "phone_status", "phone_confidence", "phone_reason", "decision_maker_name",
    "decision_maker_title", "decision_maker_source", "decision_maker_status",
    "outreach_status", "notes", "last_checked"
]

def normalise_company_name(name):
    import re
    value = (name or "").lower().strip()
    value = re.sub(r"\b(limited|ltd|plc|llp|incorporated|inc|uk)\b", "", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()

def company_key(record):
    site = (record.get("company_website") or "").strip()
    host = urlparse(site if "://" in site else "https://" + site).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or normalise_company_name(record.get("company", ""))

class LeadStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True) if self.db_path != ":memory:" else None
        self._init()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self):
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_url TEXT NOT NULL UNIQUE,
                    job_id TEXT DEFAULT '',
                    company TEXT DEFAULT '',
                    job_title TEXT DEFAULT '',
                    job_location TEXT DEFAULT '',
                    date_posted TEXT DEFAULT '',
                    job_summary TEXT DEFAULT '',
                    company_website TEXT DEFAULT '',
                    job_source TEXT DEFAULT '',
                    discovered_at TEXT DEFAULT '',
                    active_status TEXT DEFAULT 'Unknown',
                    extraction_status TEXT DEFAULT '',
                    retrieval_method TEXT DEFAULT '',
                    company_match_reason TEXT DEFAULT '',
                    company_match_confidence TEXT DEFAULT 'Low',
                    office_phone TEXT DEFAULT '',
                    phone_type TEXT DEFAULT '',
                    phone_source_url TEXT DEFAULT '',
                    phone_source_type TEXT DEFAULT '',
                    phone_checked_at TEXT DEFAULT '',
                    phone_status TEXT DEFAULT 'Not checked',
                    phone_confidence TEXT DEFAULT '',
                    phone_reason TEXT DEFAULT '',
                    decision_maker_name TEXT DEFAULT '',
                    decision_maker_title TEXT DEFAULT '',
                    decision_maker_source TEXT DEFAULT '',
                    decision_maker_status TEXT DEFAULT 'Not checked',
                    outreach_status TEXT DEFAULT 'New',
                    notes TEXT DEFAULT '',
                    last_checked TEXT DEFAULT ''
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_company ON leads(company)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_phone_status ON leads(phone_status)")

    def upsert_many(self, records):
        inserted = updated = 0
        with self._connect() as conn:
            for record in records:
                url = (record.get("job_url") or "").strip()
                if not url.startswith(("http://", "https://")):
                    continue
                exists = conn.execute("SELECT id FROM leads WHERE job_url=?", (url,)).fetchone()
                fields = [f for f in FIELDS if f in record]
                if "job_url" not in fields:
                    fields.insert(0, "job_url")
                values = [record.get(f, "") if record.get(f) is not None else "" for f in fields]
                if exists:
                    # Preserve user-maintained fields when incoming discovery values are empty.
                    assignments = ", ".join(f"{f}=CASE WHEN ?='' THEN {f} ELSE ? END" for f in fields if f != "job_url")
                    params = []
                    for f in fields:
                        if f == "job_url":
                            continue
                        val = record.get(f, "") or ""
                        params.extend([val, val])
                    params.append(url)
                    if assignments:
                        conn.execute(f"UPDATE leads SET {assignments} WHERE job_url=?", params)
                    updated += 1
                else:
                    cols = ", ".join(fields)
                    placeholders = ", ".join("?" for _ in fields)
                    conn.execute(f"INSERT OR IGNORE INTO leads ({cols}) VALUES ({placeholders})", values)
                    inserted += 1
        return {"inserted": inserted, "updated": updated}

    def all(self):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute("SELECT * FROM leads ORDER BY id DESC").fetchall()]

    def update(self, lead_id, fields):
        allowed = set(FIELDS) - {"job_url"}
        fields = {k: v for k, v in fields.items() if k in allowed}
        if not fields:
            return False
        sql = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [int(lead_id)]
        with self._connect() as conn:
            cur = conn.execute(f"UPDATE leads SET {sql} WHERE id=?", values)
            return cur.rowcount > 0

    def delete(self, lead_id):
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM leads WHERE id=?", (int(lead_id),))
            return cur.rowcount > 0

    def clear(self):
        with self._connect() as conn:
            conn.execute("DELETE FROM leads")

    def get(self, lead_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM leads WHERE id=?", (int(lead_id),)).fetchone()
            return dict(row) if row else None
