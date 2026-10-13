# HR Lead Finder

Finds UK companies advertising HR jobs (via the official Adzuna and Reed APIs), then looks for each
company's official website and publicly listed main phone number. Phone, website and score are always
shown with their source and confidence. Nothing is guessed: if no number is found it says **Not found**.

## What works without keys vs with keys
| Feature | Needs |
|---|---|
| Dashboard opens, settings, manual LinkedIn/Indeed search links | nothing |
| Real job listings | **at least one** of Adzuna (free) or Reed (free, UK only) |
| Website + phone finding | nothing extra (uses Wikidata + public pages + domain checks) |
| Better website finding | optional `BRAVE_API_KEY` |
| Companies House company number/address | optional `COMPANIES_HOUSE_API_KEY` |
| Password protection | `APP_PASSWORD` (set it when hosted online) |

No keys = no job source = the app tells you "Setup needed". That is a setup step, not a bug.

## Step 1 - get free keys (iPhone Safari, ~10 minutes)
1. **Adzuna**: developer.adzuna.com -> Register -> copy **App ID** and **App Key**.
2. **Reed**: reed.co.uk/developers/jobseeker -> sign in -> copy your API key.
Quotas change; check each dashboard. Adzuna requires the "Jobs by Adzuna" credit (already in the footer).

## Step 2 - put the code on GitHub (private repo)
1. Create a free account at github.com. Tap **+ -> New repository**, name it `hr-lead-finder`, choose **Private**.
2. In the **Files** app, tap the zip to unzip it.
3. In the repo tap **Add file -> Upload files**, select all the unzipped files (they are all in one flat folder), then **Commit changes**.

## Step 3A - run it on Render (always-on link, no terminal)
1. render.com -> sign up with GitHub -> **New + -> Blueprint** -> pick your repo (it reads `render.yaml`).
2. Fill in the environment values: `APP_PASSWORD`, `ADZUNA_APP_ID`, `ADZUNA_APP_KEY`, `REED_API_KEY`, `CONTACT_EMAIL`. Tap **Apply**.
3. Wait for "Live", open the `.onrender.com` link, enter any username and your password.
4. Add the link to your Home Screen (Share -> Add to Home Screen).

**Free-tier trade-offs (honest):** the app sleeps after ~15 min idle (first load takes ~1 min) and Render's free disk
is wiped on restart/redeploy, so **export CSV or tap Export -> Database backup regularly**. Some company sites block
cloud servers; those show "Site declined automated access" and we do not try to get round it.

## Step 3B - alternative: GitHub Codespaces (keeps your data while the codespace exists)
1. In your repo tap **Code -> Codespaces -> Create**. (VS Code in Safari is fiddly on a phone; use landscape.)
2. In the terminal: `pip install -r requirements.txt` then create a `.env` file from `.env.example`, then `python app.py`.
3. Open the **Ports** tab and tap the globe next to port 8000.

## Using it
1. **Refresh jobs** - pulls HR vacancies from every connected source (one source failing never stops the rest; errors are listed).
2. **Find phone numbers** - checks up to 25 top-scoring unchecked companies per tap (be gentle; each site is visited slowly).
3. Tap a lead for score reasons, phone sources, jobs, notes, and outreach status. **Export** -> CSV or Excel.

## How phone confidence works
- **High**: found as a `tel:` link or structured data (or labelled head office/switchboard) on the company's own contact/about/locations page.
- **Medium**: plain text on the official site, not clearly labelled.
- **Low**: ambiguous (several unlabelled numbers), or the website itself was only weakly matched. Treat as unverified.
A lower-confidence number never replaces a better one; it is stored as an alternative, and replacements keep history.
Mobile numbers are skipped by default (`ALLOW_MOBILE_NUMBERS=1` to allow). A switchboard is not the HR team's direct line.

## Testing
- Jobs: tap **Refresh jobs**; the banner shows counts per source. Leads appear below.
- Phones: tap a lead -> **Find phone number now**. Check the source link opens a real page showing the number.
- CSV: Export -> CSV; open in Excel/Sheets (21 columns).
- Code tests (Codespaces): `python -m unittest -v test_offline` (uses made-up HTML only inside the tests).

## Troubleshooting
- "Setup needed" -> add an API key and restart/redeploy.
- "credentials rejected" -> key mistyped or not yet activated.
- "rate limit or quota reached" -> wait; reduce how often you refresh.
- "Website not found" -> paste the real website in the lead and tap Save website.
- "robots.txt does not allow automated access" / "declined automated access" -> the site says no; call from a manual search instead.
- Data vanished on Render -> expected on free tier; use backups/exports.

## Limits
Decision-makers are only added when a company page publishes structured data naming an HR person, or you add one
with a source URL. LinkedIn and Indeed are not scraped (they forbid it); the Settings page links to their searches.
Job ads are a signal of need, not proof of buying intent. Check UK rules (PECR/TPS/CTPS) before cold calling companies.

## Files
app.py (API) - config.py - sources.py (job APIs) - matching.py - db.py - enrich.py + phones.py + http_client.py
- scoring.py - exporter.py - tasks.py - index.html (dashboard) - test_offline.py
