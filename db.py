"""SQLite storage, migrations and all queries."""
import datetime as dt
import json
import sqlite3

import config
import matching


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


MIGRATIONS = [
    # v1 - initial schema
    """
    CREATE TABLE companies (
        id INTEGER PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL UNIQUE, alt_names TEXT DEFAULT '',
        website TEXT, website_method TEXT, website_confidence TEXT, careers_url TEXT,
        country TEXT, company_number TEXT, registered_address TEXT, registry_status TEXT,
        enrichment_status TEXT DEFAULT 'Pending', enrichment_note TEXT, enriched_at TEXT, last_verified TEXT,
        is_agency INTEGER DEFAULT 0, first_seen TEXT, score REAL DEFAULT 0, score_explain TEXT DEFAULT '[]');
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
        source TEXT NOT NULL, source_id TEXT, title TEXT, description TEXT, url TEXT, url_norm TEXT,
        dup_key TEXT UNIQUE, location TEXT, employment_type TEXT, date_posted TEXT,
        retrieved_at TEXT, last_seen TEXT, status TEXT DEFAULT 'active', matched_roles TEXT, check_note TEXT,
        UNIQUE(source, source_id));
    CREATE INDEX idx_jobs_company ON jobs(company_id);
    CREATE INDEX idx_jobs_urlnorm ON jobs(url_norm);
    CREATE TABLE phones (
        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
        number_e164 TEXT, number_intl TEXT, raw TEXT, phone_type TEXT, country TEXT, location_label TEXT,
        source_url TEXT, contact_page_url TEXT, method TEXT, confidence TEXT, verification TEXT,
        status TEXT DEFAULT 'current', note TEXT, found_at TEXT, verified_at TEXT);
    CREATE INDEX idx_phones_company ON phones(company_id);
    CREATE TABLE decision_makers (
        id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
        name TEXT, title TEXT, source_url TEXT, verification TEXT, found_at TEXT);
    CREATE TABLE outreach (
        company_id INTEGER PRIMARY KEY REFERENCES companies(id),
        status TEXT DEFAULT 'new', notes TEXT DEFAULT '', updated_at TEXT);
    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
    """,
]


def init_db():
    con = connect()
    version = con.execute("PRAGMA user_version").fetchone()[0]
    for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
        con.executescript(script)
        con.execute(f"PRAGMA user_version={i}")
    con.commit()
    con.close()


def rows(sql, args=()):
    con = connect()
    try:
        return [dict(r) for r in con.execute(sql, args).fetchall()]
    finally:
        con.close()


def one(sql, args=()):
    r = rows(sql, args)
    return r[0] if r else None


def run(sql, args=()):
    con = connect()
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


# ---------- settings ----------
def get_weights():
    r = one("SELECT value FROM settings WHERE key='weights'")
    w = dict(config.DEFAULT_WEIGHTS)
    if r:
        try:
            w.update({k: float(v) for k, v in json.loads(r["value"]).items() if k in w})
        except (ValueError, TypeError):
            pass
    return w


def set_weights(new):
    clean = {k: float(v) for k, v in new.items() if k in config.DEFAULT_WEIGHTS}
    run("INSERT INTO settings(key,value) VALUES('weights',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (json.dumps(clean),))
    return get_weights()


# ---------- companies & jobs ----------
def upsert_company(name, country=None):
    """Return (company_id, created). Matches carefully so name variants share one company."""
    norm = matching.name_norm(name)
    if not norm:
        return None, False
    con = connect()
    try:
        existing = {r["name_norm"]: r["id"] for r in con.execute("SELECT id,name_norm FROM companies")}
        hit = matching.find_similar(norm, existing.keys())
        if hit:
            cid = existing[hit]
            if hit != norm:  # remember the variant spelling
                con.execute("UPDATE companies SET alt_names = alt_names || ? WHERE id=? AND alt_names NOT LIKE ?",
                            (name + "; ", cid, f"%{name}%"))
                con.commit()
            return cid, False
        cur = con.execute(
            "INSERT INTO companies(name,name_norm,country,is_agency,first_seen) VALUES(?,?,?,?,?)",
            (name.strip(), norm, country or config.COUNTRY.upper(), int(matching.is_agency(name)), now()))
        con.execute("INSERT INTO outreach(company_id,status,updated_at) VALUES(?, 'new', ?)", (cur.lastrowid, now()))
        con.commit()
        return cur.lastrowid, True
    finally:
        con.close()


def upsert_job(company_id, rec):
    """rec: JobRecord. Returns 'new' | 'seen' (duplicate)."""
    key = matching.dup_key(rec.company, rec.title, rec.location)
    con = connect()
    try:
        row = con.execute("SELECT id FROM jobs WHERE (source=? AND source_id=?) OR dup_key=? OR "
                          "(url_norm=? AND url_norm<>'')",
                          (rec.source, rec.source_id, key, matching.url_norm(rec.url))).fetchone()
        if row:
            con.execute("UPDATE jobs SET last_seen=?, status='active' WHERE id=?", (now(), row["id"]))
            con.commit()
            return "seen"
        con.execute(
            "INSERT INTO jobs(company_id,source,source_id,title,description,url,url_norm,dup_key,location,"
            "employment_type,date_posted,retrieved_at,last_seen,status,matched_roles) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?)",
            (company_id, rec.source, rec.source_id, rec.title, rec.description, rec.url,
             matching.url_norm(rec.url), key, rec.location, rec.employment_type, rec.date_posted,
             now(), now(), ", ".join(matching.matched_roles(rec.title))))
        con.commit()
        return "new"
    finally:
        con.close()


def mark_stale():
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=config.STALE_AFTER_DAYS)).isoformat()
    run("UPDATE jobs SET status='stale' WHERE status='active' AND last_seen < ?", (cutoff,))


def update_company(cid, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    run(f"UPDATE companies SET {cols} WHERE id=?", (*fields.values(), cid))


def get_company(cid):
    return one("SELECT * FROM companies WHERE id=?", (cid,))


# ---------- phones (never silently downgrade) ----------
RANK = {"High": 3, "Medium": 2, "Low": 1}
VERIFICATION = {"High": "Verified on official website", "Medium": "Probable (official website, unlabelled)",
                "Low": "Unverified (low confidence)"}


def save_phone(cid, cand, ts=None):
    """cand: dict from enrich. Returns what happened, as text."""
    ts = ts or now()
    con = connect()
    try:
        cur = con.execute("SELECT * FROM phones WHERE company_id=? AND status='current'", (cid,)).fetchone()
        same = con.execute("SELECT * FROM phones WHERE company_id=? AND number_e164=?",
                           (cid, cand["e164"])).fetchone()
        if same:
            better = RANK[cand["confidence"]] > RANK.get(same["confidence"], 0)
            con.execute("UPDATE phones SET verified_at=?, source_url=?, confidence=?, verification=? WHERE id=?",
                        (ts, cand["source_url"] if better else same["source_url"],
                         cand["confidence"] if better else same["confidence"],
                         VERIFICATION[cand["confidence"] if better else same["confidence"]], same["id"]))
            con.commit()
            return "re-verified"
        status, note, result = "current", "", "added"
        want_current = cand.get("primary", True)
        if cur and want_current:
            if RANK[cand["confidence"]] > RANK.get(cur["confidence"], 0):
                con.execute("UPDATE phones SET status='superseded', note=? WHERE id=?",
                            (f"Replaced {ts[:10]} by higher-confidence number {cand['e164']}", cur["id"]))
                result = "replaced (history kept)"
            else:
                status, note, result = ("alternative",
                                        f"Found {ts[:10]}; not promoted because the current number is as or more reliable",
                                        "kept existing number")
        elif cur or not want_current:
            status = "alternative"
        con.execute(
            "INSERT INTO phones(company_id,number_e164,number_intl,raw,phone_type,country,location_label,source_url,"
            "contact_page_url,method,confidence,verification,status,note,found_at,verified_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, cand["e164"], cand["intl"], cand["raw"], cand["type"], cand["country"], cand.get("location", ""),
             cand["source_url"], cand.get("contact_url", ""), cand["method"], cand["confidence"],
             VERIFICATION[cand["confidence"]], status, note, ts, ts))
        con.commit()
        return result
    finally:
        con.close()


def save_decision_maker(cid, name, title, source_url, verification):
    if one("SELECT id FROM decision_makers WHERE company_id=? AND lower(name)=lower(?)", (cid, name)):
        return None
    return run("INSERT INTO decision_makers(company_id,name,title,source_url,verification,found_at) "
               "VALUES(?,?,?,?,?,?)", (cid, name, title, source_url, verification, now()))


# ---------- read models ----------
def lead_rows():
    """One dict per company with job/phone/outreach summary (filters are applied in app.py)."""
    comps = rows("SELECT c.*, o.status AS outreach_status, o.notes AS outreach_notes FROM companies c "
                 "LEFT JOIN outreach o ON o.company_id=c.id")
    jobs = rows("SELECT * FROM jobs WHERE status IN ('active','stale') ORDER BY date_posted DESC, retrieved_at DESC")
    phones = rows("SELECT * FROM phones WHERE status='current'")
    dms = rows("SELECT company_id, COUNT(*) n FROM decision_makers GROUP BY company_id")
    jb, ph, dm = {}, {}, {r["company_id"]: r["n"] for r in dms}
    for j in jobs:
        jb.setdefault(j["company_id"], []).append(j)
    for p in phones:
        ph[p["company_id"]] = p
    out = []
    for c in comps:
        js = jb.get(c["id"], [])
        active = [j for j in js if j["status"] == "active"]
        c.update(jobs=js, active_jobs=len(active), phone=ph.get(c["id"]), dm_count=dm.get(c["id"], 0),
                 newest=max([j["date_posted"] or j["retrieved_at"][:10] for j in js], default=None),
                 score_explain=json.loads(c["score_explain"] or "[]"))
        out.append(c)
    return out


def company_detail(cid):
    c = get_company(cid)
    if not c:
        return None
    c["score_explain"] = json.loads(c["score_explain"] or "[]")
    c["jobs"] = rows("SELECT * FROM jobs WHERE company_id=? ORDER BY date_posted DESC", (cid,))
    c["phones"] = rows("SELECT * FROM phones WHERE company_id=? ORDER BY status='current' DESC, found_at DESC", (cid,))
    c["decision_makers"] = rows("SELECT * FROM decision_makers WHERE company_id=?", (cid,))
    o = one("SELECT status, notes FROM outreach WHERE company_id=?", (cid,)) or {}
    c["outreach_status"], c["outreach_notes"] = o.get("status", "new"), o.get("notes", "")
    return c
