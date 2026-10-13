"""Offline tests: logic, database, enrichment on sample HTML, API routes, CSV export.
These use made-up HTML/job records ONLY inside the tests - the app itself never ships sample data.
Run:  python -m unittest -v test_offline"""
import csv
import io
import os
import tempfile
import time
import unittest

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["HOST_DELAY"] = "0"

import config  # noqa: E402
import db  # noqa: E402
import enrich  # noqa: E402
import exporter  # noqa: E402
import matching  # noqa: E402
import phones  # noqa: E402
import scoring  # noqa: E402
import sources  # noqa: E402
from http_client import ApiHttpError, Page  # noqa: E402

CONTACT_HTML = """<html><head><title>Contact | Acme Widgets</title>
<script type="application/ld+json">{"@type":"Organization","name":"Acme Widgets","telephone":"+44 20 7946 0018",
"address":{"addressLocality":"London"}}</script></head><body>
<p>Head office: <a href="tel:+442079460018">020 7946 0018</a></p><p>Fax: 020 7946 0019</p>
<p>Company number: 01234567</p><p>© Acme Widgets Ltd, 1 High St, London EC1A 1BB</p></body></html>"""
HOME_HTML = """<html><head><title>Acme Widgets - Home</title></head><body><a href="/contact-us">Contact us</a>
<a href="/careers">Careers</a><p>Welcome. United Kingdom.</p></body></html>"""


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    def get(self, url):
        for k, v in self.pages.items():
            if url.rstrip("/") == k.rstrip("/"):
                return Page(url=url, final_url=url, status=200, html=v)
        return Page(url=url, status=404, error="HTTP 404")


class Logic(unittest.TestCase):
    def test_company_matching(self):
        self.assertEqual(matching.name_norm("Acme Widgets Ltd."), matching.name_norm("ACME WIDGETS LIMITED"))
        self.assertEqual(matching.find_similar("acme widgets", {"acme widgets", "zeta"}), "acme widgets")
        self.assertIsNone(matching.find_similar("acme widgets", {"acme tools"}))

    def test_roles_and_agency(self):
        self.assertIn("HR Manager", matching.matched_roles("Senior HR Manager (Maternity cover)"))
        self.assertEqual(matching.matched_roles("Warehouse Operative"), [])
        self.assertTrue(matching.is_agency("Hays Specialist Recruitment"))
        self.assertFalse(matching.is_agency("Acme Widgets Ltd"))

    def test_phone_rules(self):
        self.assertEqual(phones.normalise("020 7946 0018")["e164"], "+442079460018")
        self.assertIsNone(phones.normalise("07700 900123"))   # mobile excluded
        self.assertIsNone(phones.normalise("01234567"))

    def test_url_norm(self):
        self.assertEqual(matching.url_norm("https://www.x.com/jobs/1/?utm=a#f"), matching.url_norm("https://x.com/jobs/1"))


class Enrichment(unittest.TestCase):
    def test_extract_and_choose(self):
        pages = {"https://acme.co.uk/": HOME_HTML, "https://acme.co.uk/contact-us": CONTACT_HTML}
        f = FakeFetcher(pages)
        home = f.get("https://acme.co.uk/")
        sps, careers, _ = enrich.collect_site(home, f)
        self.assertTrue(careers.endswith("/careers"))
        cands = [c for sp in sps for c in enrich.extract_phones(sp, "GB")]
        nums = {c["e164"] for c in cands}
        self.assertEqual(nums, {"+442079460018"})            # fax + company number ignored
        primary, alts = enrich.choose(cands, "High")
        self.assertEqual((primary["confidence"], primary["type"]), ("High", "main office"))
        self.assertEqual(enrich.verify_site("Acme Widgets Ltd", home), "High")

    def test_never_downgrade(self):
        cid, _ = db.upsert_company("Downgrade Test Ltd")
        good = {"e164": "+442079460018", "intl": "+44 20 7946 0018", "raw": "x", "type": "main office", "country": "GB",
                "source_url": "https://a/contact", "method": "tel-link", "confidence": "High", "primary": True}
        weak = dict(good, e164="+441614960000", intl="+44 161 496 0000", confidence="Low")
        db.save_phone(cid, good)
        self.assertEqual(db.save_phone(cid, weak), "kept existing number")
        cur = db.rows("SELECT number_e164 FROM phones WHERE company_id=? AND status='current'", (cid,))
        self.assertEqual(cur[0]["number_e164"], "+442079460018")


class FlakySource(sources.Source):
    name = "Flaky"
    def configured(self): return True
    def fetch(self, *a): raise ApiHttpError(429, "rate limit or daily quota reached - try again later")


class GoodSource(sources.Source):
    name = "Good"
    def configured(self): return True
    def fetch(self, role, location, pages, max_days):
        if role != "HR Manager":
            return []
        mk = lambda i, co, t: sources.JobRecord(co, t, f"https://jobs.example/{i}", self.name, str(i), "=cmd()", "London", "full time", "2026-10-10")
        return [mk(1, "Acme Widgets Ltd", "HR Manager"), mk(2, "ACME WIDGETS LIMITED", "HR Manager"),   # same job twice
                mk(3, "Acme Widgets Ltd", "HR Officer"), mk(4, "Cafe Co", "Barista")]


class AppFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app as appmod
        cls.app = appmod
        cls.c = appmod.app.test_client()
        sources.SOURCES[:] = [FlakySource(), GoodSource()]

    def wait(self, tid):
        for _ in range(100):
            t = self.c.get(f"/api/tasks/{tid}").json
            if t["state"] != "running":
                return t
            time.sleep(0.1)
        self.fail("task did not finish")

    def test_full_flow(self):
        t = self.wait(self.c.post("/api/refresh", json={}).json["task"])
        self.assertEqual(t["state"], "done", t)
        self.assertTrue(any("Flaky" in e for e in t["errors"]))          # failure reported...
        self.assertIn("2 new jobs", t["summary"])                        # ...and the good source still worked
        leads = self.c.get("/api/leads").json
        self.assertEqual([l["name"] for l in leads], ["Acme Widgets Ltd"])  # deduped; barista filtered out
        self.assertEqual(leads[0]["active_jobs"], 2)
        self.assertEqual(leads[0]["phone"], None)                         # honest: no number yet
        # enrichment with fake web pages
        enrich.Fetcher = lambda: FakeFetcher({"https://www.acmewidgets.co.uk/": HOME_HTML.replace("Home", "Home"),
                                              "https://www.acmewidgets.co.uk/contact-us": CONTACT_HTML})
        self.app.Fetcher = enrich.Fetcher
        enrich.candidates_wikidata = lambda n: []
        cid = leads[0]["id"]
        t = self.wait(self.c.post("/api/enrich", json={"company_id": cid}).json["task"])
        self.assertEqual(t["state"], "done", t)
        d = self.c.get(f"/api/leads/{cid}").json
        self.assertEqual(d["phones"][0]["number_intl"], "+44 20 7946 0018")
        self.assertEqual(d["phones"][0]["confidence"], "High")
        self.assertGreater(d["score"], 60)
        self.assertTrue(d["score_explain"])
        # outreach + export
        self.assertTrue(self.c.post(f"/api/leads/{cid}/outreach", json={"status": "contacted", "notes": "called"}).json["ok"])
        raw = self.c.get("/api/export.csv").data
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))                  # BOM for Excel
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
        self.assertEqual(rows[0], exporter.COLUMNS)
        self.assertEqual(len(rows[0]), 21)
        self.assertEqual(rows[1][8], "+44 20 7946 0018")
        self.assertEqual(rows[1][19], "Contacted")
        self.assertTrue(rows[1][3].startswith("'="))                      # formula text neutralised
        self.assertEqual(self.c.get("/api/export.xlsx").status_code, 200)
        # second refresh adds nothing new
        t = self.wait(self.c.post("/api/refresh", json={}).json["task"])
        self.assertIn("0 new jobs", t["summary"])

    def test_decision_maker_needs_source(self):
        r = self.c.post("/api/leads/1/decision-makers", json={"name": "A B", "title": "Head of HR"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
