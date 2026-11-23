# Daily Setter & Closer Performance Report

A web app that builds the **Calling Inputs** and **Booking Inputs** report for the sales floor
four times a day (**2, 5, 7 and 9 PM IST, Monday to Saturday**), keeps every report in history,
and (optionally) posts it to Slack.

| Section | Source | Sorted by |
|---|---|---|
| A · Calling Inputs: Setter Name, Calls, TT (in min), Connected, Unique Calls | FreJun CSV (uploaded) | TT, highest first, then Calls |
| B · Booking Inputs: Setter Name, Bookings, CC | HubSpot (automatic) | Bookings, highest first, then CC |

---

## 1. Daily use (operations team)

1. **Before each report time**, export the *users-table* CSV from FreJun and drop it on the
   **FreJun uploads** page. Don't rename the file: the export time is read from the name.
2. That's it. At 2/5/7/9 PM the report is generated and posted automatically.
   * If no fresh CSV has arrived, the app waits up to 20 minutes for one. Uploading during
     the wait releases it straight away.
   * After 20 minutes it goes ahead and marks Calling Inputs as **"No new FreJun CSV since
     last report"** (or **"FreJun CSV missing"** if there's none for the day). It never
     reuses yesterday's file.
3. Late CSV? Upload it, open the report and press **Re-run this cycle**. This makes
   version 2. Version 1 stays in history, and you can send the new one to Slack as a correction.

Other pages:

* **History**: any date, every cycle and version, with Excel/CSV downloads (one cycle or the whole day).
* **Compare**: 2 PM vs 5 PM, 5 PM vs 7 PM, 7 PM vs 9 PM (or 2 PM vs 9 PM), per person.
* **Through the day**: each person's TT, Bookings and CC at every cycle.
* **Roster**: set who is a setter or a closer. New names in a CSV appear here as
  *role not set*. Changes apply to reports generated afterwards; past reports are never changed.
* **Activity log**: every run, upload, retry, Slack post and error, with time stamps.
* **Status & setup**: today's cycles, connection health, the next scheduled runs, and a HubSpot connection test.

---

## 2. How every number is calculated

### Calling Inputs (FreJun CSV)

Inspected from a real export (`users-table-Monday_November_23_2026_at_06_57_PM.csv`):

| Report column | CSV column | Rule |
|---|---|---|
| Setter Name | `User` | Exactly as in the CSV |
| Calls | `Total calls` | whole number |
| **TT (in min)** | `Total duration` (e.g. `1h 43m 45s`) | seconds ÷ 60, shown to 1 decimal place |
| Connected | `Calls Connected` | whole number |
| Unique Calls | `Unique Calls` | whole number |

* `Total duration` is **talk time**: it equals *Average Talktime × Calls Connected* in the
  export. `Average Talktime` is never used.
* TT formats understood: `1h 2m 3s`, `HH:MM:SS`, `MM:SS`, and plain seconds (or plain
  minutes if the header says "min"). Unreadable values show as **—** with a warning, never as 0.
* Sorting uses **exact seconds**, so ties are real ties (then Calls decides).
* Checked against the manual 7 PM report of 23 Nov: Calls **675**, Connected **248** and
  Unique **483** match exactly. The manual sheet's TT total was **612** because it rounded
  each row to whole minutes inconsistently; the exact figure is **613.8**. Set `TT_DECIMALS=0`
  if you want whole minutes.
* People are matched to the roster by **email first**, then exact name. Two people with the
  same name but different emails are never merged.
* A person listed twice with identical numbers is counted once. Listed twice with
  *different* numbers, the file is rejected (re-export it), because the app won't guess which row is right.
* Setters and closers are shown in separate groups with their own totals, plus a
  **Setters + Closers** total. People with no role and no calls are hidden (the count is shown).

### Booking Inputs (HubSpot)

HubSpot has no API that returns a dashboard's numbers, so the app re-runs the two reports
behind the **Setters Matrix** dashboard through HubSpot's official CRM APIs:

| Column | HubSpot report | Logic |
|---|---|---|
| Bookings | *Meeting Booking - Daily* (155571610) | distinct **contacts** on deals whose *Date entered "Counselling Scheduled (Direct Enrolment)"* is today, excluding contacts in lists 1724 and 6614, grouped by the deal's **Setter** (`asst_counsellor`) |
| CC | *CCs - today* (163016740) | distinct **deals** whose *Date entered "Counselling Completed (Direct Enrolment)"* is today, grouped by **Setter** |

* "Today" runs from midnight IST to the report's time, so the 2 PM report shows the position at 2 PM.
* Setter names come from HubSpot's Setter field labels. If HubSpot has no name for an ID,
  the row says "HubSpot user 12345" and a warning is raised. Names are never made up.
* Deals with no setter appear as **Unassigned** (last row), just as in HubSpot.
* Rostered setters with nothing in HubSpot are listed with **0** and the note *"0 in HubSpot"*
  (set `INCLUDE_ZERO_SETTERS=false` to show only rows HubSpot returns).
* Click any Bookings or CC number to see the exact HubSpot deals and contacts behind it.

### When something fails

Calling and Booking are built separately. If HubSpot is down, the report still saves the
calling data, marks Booking as **failed** (no numbers shown), retries HubSpot automatically
after 10 minutes, and posts an **UPDATE** to Slack if the retry works. HubSpot calls are also
retried (with back-off) on rate limits and server errors. Every error is in the Activity log.

---

## 3. What you need to provide

| Item | Why | Where to get it |
|---|---|---|
| **HubSpot private app token** | Booking Inputs | HubSpot → Settings → Integrations → Private Apps → Create. Scopes: `crm.objects.deals.read`, `crm.objects.contacts.read`, `crm.schemas.deals.read`, `crm.objects.owners.read`, `crm.lists.read`. Copy the access token. |
| **Admin password** (+ optional viewer password) | Sign-in | Choose them yourself |
| **Slack incoming webhook URL** (optional) | Auto-posting | api.slack.com/apps → Create app → Incoming Webhooks → Add to channel |
| **A host** | Runs the app 24/7 | Render (below), any VPS, or an always-on office PC |

A private app token is HubSpot's recommended method for an internal tool that reads one
account. It is read-only with the scopes above. Keep it in the host's secret settings, never in the code.

---

## 4. Setting it up

### Option A: Render (easiest; about ₹1,000–1,500 per month)

1. Put this folder in a private GitHub repository.
2. On render.com: **New + → Blueprint**, then pick the repository. It reads `render.yaml`
   and creates the app plus a Postgres database.
3. When asked, fill in `ADMIN_PASSWORD`, `VIEWER_PASSWORD`, `HUBSPOT_TOKEN`,
   `SLACK_WEBHOOK_URL` (optional) and `PUBLIC_BASE_URL` (the app's Render address).
4. Open the address, sign in, go to **Status & setup → Test HubSpot connection**.
5. Upload today's FreJun CSV, then **Generate a report now** and compare it with the HubSpot dashboard.

Keep the instance on a paid plan. Free instances go to sleep and miss the schedule. If you
must use a host that sleeps, set `CRON_SECRET` and have a free external cron (e.g.
cron-job.org) call `POST https://<your-app>/api/cron/run` with header
`X-Cron-Secret: <secret>` every 5 minutes, Monday to Saturday, 2–10 PM IST. The call is safe
to repeat: it only creates reports that are missing.

### Option B: any server or office PC with Docker

```bash
cp .env.example .env        # then edit .env
docker compose up -d        # app on http://<machine>:8000, with Postgres
```

### Option C: run directly with Python 3.11+

```bash
pip install -r requirements.txt
cp .env.example .env        # then edit .env
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Always run **one** app process: the scheduler lives inside it. Duplicate runs are also
blocked in the database, but one process keeps things simple.

---

## 5. Automating the FreJun step (optional)

FreJun's public API does offer call logs and a per-user call-analytics endpoint
(`total_calls`, `answered_calls`, `total_minutes`), but it has **no Unique Calls figure**
and doesn't document the unit of `call_duration`. It can't yet reproduce the CSV
exactly, so the CSV upload remains the source of truth.

To skip the manual upload, any tool (Zapier, Make, a script that watches a mailbox or
Drive folder) can push the CSV automatically:

```bash
curl -X POST https://<your-app>/api/ingest/csv \
  -H "X-Ingest-Token: <INGEST_TOKEN>" \
  -F "file=@users-table-Monday_November_23_2026_at_06_57_PM.csv"
```

---

## 6. For developers

```
app/
  csv_parser.py   FreJun CSV → validated rows (header mapping, TT conversion, duplicates)
  hubspot.py      HubSpot client (retries) + the two report re-implementations
  reporting.py    snapshot builder, sorting, totals, compare, timeline
  scheduler.py    Mon–Sat cycle jobs, CSV wait, auto-retry, catch-up
  delivery.py     Slack message + once-per-cycle rule
  exports.py      Excel / CSV downloads
  roster.py       setter / closer roster and matching (seeded from seed_roster.json)
  main.py         FastAPI routes, sign-in
  static/         dashboard (plain HTML/CSS/JS, no build step)
tests/            pytest suite: real FreJun CSV + a mock HubSpot API
```

Run tests: `pip install -r requirements.txt && pytest -q` (52 tests).

Data model: `csv_uploads` (original file bytes kept for traceability), `roster`,
`snapshots` (one row per date + cycle + version, never edited), `deliveries`, `event_log`.

API summary: `GET /api/reports/latest`, `GET /api/reports?day=`, `GET /api/reports/{id}`,
`POST /api/reports/generate`, `GET /api/reports/{id}/download?format=xlsx|csv`,
`GET /api/days/{date}/download`, `GET /api/compare?a=&b=`, `GET /api/timeline?day=`,
`POST /api/uploads`, `POST /api/ingest/csv`, `GET|POST|PATCH /api/roster`, `GET /api/logs`,
`POST /api/cron/run`, `GET /healthz`.
