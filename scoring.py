"""Transparent lead scoring. Weights live in config.DEFAULT_WEIGHTS and can be edited in the app."""
import datetime as dt
import json

import db


def _days_old(date_str):
    try:
        return (dt.date.today() - dt.date.fromisoformat(date_str[:10])).days
    except (ValueError, TypeError):
        return None


def score_lead(c, w):
    """c: a lead_rows() dict. Returns (score 0-100, [plain-English reasons])."""
    pts, why = 0.0, []
    n = c["active_jobs"]
    if n:
        pts += w["active_vacancy"]
        why.append(f"+{w['active_vacancy']:g} active HR vacancy")
        extra = min((n - 1) * w["per_extra_vacancy"], w["extra_vacancy_cap"])
        if extra:
            pts += extra
            why.append(f"+{extra:g} for {n} relevant vacancies")
        ages = [_days_old(j["date_posted"] or j["retrieved_at"]) for j in c["jobs"] if j["status"] == "active"]
        newest = min((d for d in ages if d is not None), default=None)
        if newest is not None:
            for limit, key in ((3, "posted_3d"), (7, "posted_7d"), (14, "posted_14d"), (30, "posted_30d")):
                if newest <= limit:
                    pts += w[key]
                    why.append(f"+{w[key]:g} newest ad is {newest} day(s) old")
                    break
    else:
        why.append("0 for vacancy: no currently active HR listing")
    if c["website"]:
        key = "website_verified" if c["website_confidence"] in ("High", "Medium") else "website_probable"
        pts += w[key]
        why.append(f"+{w[key]:g} official website identified ({c['website_confidence']})")
    p = c.get("phone")
    if p:
        key = {"High": "phone_high", "Medium": "phone_medium"}.get(p["confidence"], "phone_low")
        pts += w[key]
        why.append(f"+{w[key]:g} business phone found ({p['confidence']} confidence)")
    if c["dm_count"]:
        pts += w["decision_maker"]
        why.append(f"+{w['decision_maker']:g} HR decision-maker recorded")
    if c["is_agency"]:
        pts += w["agency_penalty"]
        why.append(f"{w['agency_penalty']:g} advertiser looks like a recruitment agency, not the employer")
    return max(0, min(100, round(pts))), why


def rescore_all():
    w = db.get_weights()
    for c in db.lead_rows():
        s, why = score_lead(c, w)
        db.update_company(c["id"], score=s, score_explain=json.dumps(why))
