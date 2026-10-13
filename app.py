"""HR Lead Finder - Flask app. Run locally:  python app.py   |  hosted: gunicorn app:app"""
import datetime as dt
import hmac
import json
import os

from flask import Flask, Response, jsonify, request, send_file

import config
import db
import enrich
import exporter
import scoring
import sources
import tasks
from http_client import Fetcher

app = Flask(__name__)
db.init_db()
OUTREACH = ["new", "contacted", "qualified", "not_relevant"]


@app.before_request
def require_password():
    if not config.APP_PASSWORD:
        return None
    a = request.authorization
    if a and hmac.compare_digest((a.password or ""), config.APP_PASSWORD):
        return None
    return Response("Password needed.", 401, {"WWW-Authenticate": 'Basic realm="HR Lead Finder"'})


def err(msg, code=400):
    return jsonify({"error": msg}), code


@app.get("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "index.html"))


# ---------------------------------------------------------------- read
@app.get("/api/status")
def status():
    return jsonify({"sources": sources.source_status(), "country": config.COUNTRY.upper(),
                    "roles": list(config.ROLE_PATTERNS), "brave": bool(config.BRAVE_API_KEY),
                    "registry": bool(config.COMPANIES_HOUSE_API_KEY), "password_set": bool(config.APP_PASSWORD),
                    "weights": db.get_weights(), "task": tasks.current()})


def _filtered(args):
    q = args.get("q", "").lower().strip()
    out = []
    since = int(args["days"]) if args.get("days", "").isdigit() else None
    for c in db.lead_rows():
        if not c["jobs"]:
            continue
        if c["is_agency"] and args.get("agencies") != "1":
            continue
        if args.get("title") and not any(args["title"] in (j["matched_roles"] or "") for j in c["jobs"]):
            continue
        if args.get("source") and not any(j["source"] == args["source"] for j in c["jobs"]):
            continue
        if args.get("location") and not any(args["location"].lower() in (j["location"] or "").lower() for j in c["jobs"]):
            continue
        if args.get("status") and c["outreach_status"] != args["status"]:
            continue
        if args.get("min_score", "").isdigit() and c["score"] < int(args["min_score"]):
            continue
        if since is not None and (not c["newest"] or c["newest"] < (dt.date.today() - dt.timedelta(days=since)).isoformat()):
            continue
        ph = args.get("phone", "")
        p = c["phone"]
        if (ph == "found" and not p) or (ph == "verified" and not (p and p["confidence"] == "High")) \
                or (ph == "missing" and p):
            continue
        if q and q not in (c["name"] + " " + " ".join(j["title"] + " " + (j["location"] or "") for j in c["jobs"])).lower():
            continue
        out.append(c)
    key = args.get("sort", "score")
    out.sort(key=(lambda c: c["name"].lower()) if key == "name" else
             (lambda c: (c["newest"] or ""),) if key == "recent" else (lambda c: -c["score"]),
             reverse=(key == "recent"))
    return out


def _card(c):
    top = c["jobs"][0]
    return {"id": c["id"], "name": c["name"], "score": c["score"], "website": c["website"],
            "enrichment_status": c["enrichment_status"], "active_jobs": c["active_jobs"], "newest": c["newest"],
            "top_job": {"title": top["title"], "location": top["location"], "source": top["source"]},
            "phone": ({"number": c["phone"]["number_intl"], "confidence": c["phone"]["confidence"],
                       "verification": c["phone"]["verification"]} if c["phone"] else None),
            "outreach_status": c["outreach_status"], "is_agency": bool(c["is_agency"])}


@app.get("/api/leads")
def leads():
    return jsonify([_card(c) for c in _filtered(request.args)])


@app.get("/api/stats")
def stats():
    all_leads = [c for c in db.lead_rows() if c["jobs"]]
    week = (dt.date.today() - dt.timedelta(days=7)).isoformat()
    return jsonify({
        "jobs": sum(c["active_jobs"] for c in all_leads), "companies": len(all_leads),
        "phones_found": sum(1 for c in all_leads if c["phone"]),
        "phones_verified": sum(1 for c in all_leads if c["phone"] and c["phone"]["confidence"] == "High"),
        "new_leads": sum(1 for c in all_leads if (c["first_seen"] or "")[:10] >= week and c["outreach_status"] == "new"),
        "pending_enrichment": sum(1 for c in all_leads if c["enrichment_status"] == "Pending")})


@app.get("/api/leads/<int:cid>")
def lead_detail(cid):
    d = db.company_detail(cid)
    if not d:
        return err("Lead not found", 404)
    cities = {(j["location"] or "").split(",")[0].strip().lower() for j in d["jobs"] if j["location"]}
    for p in d["phones"]:
        p["matches_job_location"] = bool(p["location_label"] and p["location_label"].lower() in cities)
    return jsonify(d)


# ---------------------------------------------------------------- actions
@app.post("/api/refresh")
def refresh():
    b = request.get_json(silent=True) or {}
    roles = [r for r in b.get("roles", config.ROLE_QUERIES) if r in config.ROLE_QUERIES] or config.ROLE_QUERIES
    location = (b.get("location") or config.DEFAULT_LOCATION).strip()
    pages = max(1, min(int(b.get("pages", 1)), 3))
    max_days = max(1, min(int(b.get("max_days", 14)), 60))

    def job(log, progress, errors):
        records, errs = sources.discover(roles, location, pages, max_days, log, progress)
        errors.extend(errs)
        new_jobs = dupes = new_companies = 0
        for rec in records:
            cid, created = db.upsert_company(rec.company)
            if not cid:
                continue
            new_companies += created
            if db.upsert_job(cid, rec) == "new":
                new_jobs += 1
            else:
                dupes += 1
        db.mark_stale()
        scoring.rescore_all()
        return f"{new_jobs} new jobs, {new_companies} new companies, {dupes} already known"

    try:
        return jsonify({"task": tasks.start("refresh", job)})
    except tasks.Busy as e:
        return err(str(e), 409)


@app.post("/api/enrich")
def enrich_route():
    b = request.get_json(silent=True) or {}
    force = bool(b.get("force"))
    if b.get("company_id"):
        ids, force = [int(b["company_id"])], True
    else:
        limit = max(1, min(int(b.get("limit", 25)), 100))
        cands = [c for c in db.lead_rows() if c["active_jobs"] and not c["is_agency"]
                 and (force or c["enrichment_status"] in ("Pending", None) or c["enrichment_status"].startswith("Error"))]
        cands.sort(key=lambda c: -c["score"])
        ids = [c["id"] for c in cands[:limit]]
    if not ids:
        return err("Nothing to enrich - every lead has already been checked recently.", 200)

    def job(log, progress, errors):
        fetcher, found = Fetcher(), 0
        for n, cid in enumerate(ids, 1):
            progress(n, len(ids))
            st = enrich.enrich_company(cid, force=force, fetcher=fetcher, log=log)
            found += st == "Phone found"
            if st.startswith("Error"):
                errors.append(f"{db.get_company(cid)['name']}: {st}")
        scoring.rescore_all()
        return f"Checked {len(ids)} companies, phone found for {found}"

    try:
        return jsonify({"task": tasks.start("enrich", job)})
    except tasks.Busy as e:
        return err(str(e), 409)


@app.post("/api/recheck")
def recheck():
    def job(log, progress, errors):
        cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat()
        todo = db.rows("SELECT id,url FROM jobs WHERE status IN ('active','stale') AND last_seen < ? LIMIT 40", (cutoff,))
        f, expired = Fetcher(), 0
        for n, j in enumerate(todo, 1):
            progress(n, len(todo))
            pg = f.get(j["url"])
            if pg.status in (404, 410):
                db.run("UPDATE jobs SET status='expired', check_note=? WHERE id=?", (f"HTTP {pg.status} on {db.now()[:10]}", j["id"]))
                expired += 1
            elif not pg.ok:
                db.run("UPDATE jobs SET check_note=? WHERE id=?", (f"Could not check: {pg.error}", j["id"]))
        scoring.rescore_all()
        return f"Rechecked {len(todo)} older listings, {expired} expired"

    try:
        return jsonify({"task": tasks.start("recheck", job)})
    except tasks.Busy as e:
        return err(str(e), 409)


@app.get("/api/tasks/<tid>")
def task(tid):
    t = tasks.get(tid)
    return jsonify(t) if t else err("Unknown task", 404)


@app.post("/api/leads/<int:cid>/outreach")
def set_outreach(cid):
    b = request.get_json(silent=True) or {}
    if b.get("status") not in OUTREACH:
        return err("Status must be one of: " + ", ".join(OUTREACH))
    if not db.get_company(cid):
        return err("Lead not found", 404)
    db.run("INSERT INTO outreach(company_id,status,notes,updated_at) VALUES(?,?,?,?) ON CONFLICT(company_id) DO UPDATE "
           "SET status=excluded.status, notes=excluded.notes, updated_at=excluded.updated_at",
           (cid, b["status"], b.get("notes", "")[:2000], db.now()))
    return jsonify({"ok": True})


@app.post("/api/leads/<int:cid>/website")
def set_website(cid):
    url = (request.get_json(silent=True) or {}).get("website", "").strip()
    if not url:
        return err("Enter a website address.")
    if not url.startswith("http"):
        url = "https://" + url
    db.update_company(cid, website=url, website_method="manual", website_confidence="High", enrichment_status="Pending")
    return jsonify({"ok": True})


@app.post("/api/leads/<int:cid>/decision-makers")
def add_dm(cid):
    b = request.get_json(silent=True) or {}
    if not (b.get("name") and b.get("title") and b.get("source_url")):
        return err("Name, job title and a source URL are all required - names without a source are not stored.")
    db.save_decision_maker(cid, b["name"].strip(), b["title"].strip(), b["source_url"].strip(), "Added manually - source supplied")
    scoring.rescore_all()
    return jsonify({"ok": True})


@app.delete("/api/decision-makers/<int:did>")
def del_dm(did):
    db.run("DELETE FROM decision_makers WHERE id=?", (did,))
    scoring.rescore_all()
    return jsonify({"ok": True})


@app.get("/api/settings/weights")
def get_weights():
    return jsonify({"weights": db.get_weights(), "defaults": config.DEFAULT_WEIGHTS})


@app.post("/api/settings/weights")
def post_weights():
    try:
        db.set_weights(request.get_json(silent=True) or {})
    except (ValueError, TypeError):
        return err("Weights must be numbers.")
    scoring.rescore_all()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- exports
def _export_args():
    return {"include_expired": request.args.get("expired") == "1", "include_agencies": request.args.get("agencies", "1") == "1"}


@app.get("/api/export.csv")
def export_csv():
    return Response(exporter.to_csv(**_export_args()), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=hr-leads-{dt.date.today()}.csv"})


@app.get("/api/export.xlsx")
def export_xlsx():
    return Response(exporter.to_xlsx(**_export_args()),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename=hr-leads-{dt.date.today()}.xlsx"})


@app.get("/api/db/download")
def db_download():
    """Backup of the whole database (useful on free hosts whose disk is wiped on restart)."""
    return send_file(os.path.abspath(config.DB_PATH), as_attachment=True, download_name="hr_leads_backup.db")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
