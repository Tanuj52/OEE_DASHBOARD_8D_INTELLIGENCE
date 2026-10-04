# Manufacturing 8D Intelligence System

AI-powered multi-agent system that automates the **8D Problem Solving** process with integrated **Predictive Maintenance** for manufacturing.

**Live demo:** _public link added after deployment_ · **Built with:** Snowflake Cortex Code (CoCo), Snowflake Cortex AI_COMPLETE, Cortex Agents + Cortex Analyst, Streamlit

## The problem it solves

Plants run two worlds that rarely meet: OT (machine sensors, live alarms) and IT (ERP orders, maintenance
logs, inventory, quality records). A bearing that starts to wear shows up first as vibration, but the cost
only becomes visible later as scrap, a late customer order and an emergency repair. This app converges both
in Snowflake and closes the loop:

| Step | What happens | Where |
|------|--------------|-------|
| Sense | Every machine streams vibration, temperature, **spindle speed (RPM)**, pressure, power and coolant flow | Live Monitoring |
| Predict | An online model learns each machine's normal, flags drift, tells wear from a process change and forecasts time to failure with a range | Live Monitoring |
| Act | A forecast raises a planned work order, a breach stops the machine and raises an emergency one, with parts reserved, technician, cost and the customer orders it puts at risk | Maintenance, Spare parts, Business impact |
| Investigate | 11 agents run the full 8D (D0–D8) over the sensor history, ERP process records (RPM, temperature, pressure per order), SPC, lot traceability and maintenance logs | 8D Investigations, Reports |
| Follow up | Every action in the report becomes a tracked task with an owner and due date, verified effective after 14 days | Corrective actions |
| Ask | Plain-English questions across machines, work orders, parts, orders and actions | Plant Assistant (Cortex Agent) |
| Triage | One command center for alerts, risk, cost of poor quality, value at risk and OEE | Command Center |

## What It Does

- **Predicts machine failures** from sensor data before they cause quality defects
- **Automates the full 8D investigation** — from defect detection through root cause analysis to corrective action plans
- **Traces lots** through the production chain (product -> order -> BOM -> material -> supplier -> customer)
- **Calculates SPC** (Cpk, control charts, Nelson rules) to quantify process capability
- **Computes COPQ** (Cost of Poor Quality) — scrap, rework, warranty, investigation costs
- **Generates structured 8D reports** ready for quality auditors

## Architecture

```
11 agents in a conditional state machine:

  SCAN Sensor Monitor -> PdM Predictive Maintenance -> <escalate?>
       NO  -> stop (maintenance recommendation only)
       YES -> D0 Trigger -> D1 Team -> D2 Problem -> D3 Containment
              -> D4 Root Cause -> D5 Corrective -> D6 Implementation
              -> D7 Prevention -> D8 Closure + report

Each agent first calls plant-data tools (SPC, lot traceability, COPQ, prediction,
maintenance history, ...) and then reasons over that evidence with the selected LLM.
```

The agents and analytics live in a framework-free core in `snowflake_app/core/`:

| Module | Purpose |
|--------|---------|
| `core/data.py` | Plant data access and analytics over the 17 Snowflake tables (bundled CSVs for offline use only) |
| `core/monitoring.py` | Live monitoring engine: telemetry stream, online anomaly model, PSI drift, RUL |
| `core/pipeline.py` | Agent specs, tool grounding, prompts, escalation rule, sequential runner |
| `core/capa.py` | Corrective actions read from the 8D sections, due dates, owners, effectiveness rule |
| `core/parts.py` | Spare-part stock ledger, reservations, forecast demand, reorder suggestions |
| `core/impact.py`, `core/orderbook.py` | Orders at risk, downtime cost, best maintenance window; the rolling order book |
| `core/digest.py` | The daily digest email (also run by a Snowflake stored procedure) |

The Streamlit app runs the pipeline in a worker thread and renders it with fragments.

## Features

Pages: Command Center, Live Monitoring, Maintenance, Spare parts, Business impact, 8D Investigations,
Corrective actions, Lot Traceability, Quality & SPC, Reports and Settings, plus the Plant Assistant chat.

- Snowflake `AI_COMPLETE` only in the UI (bind-parameter SQL). Clients for Groq, Gemini, NVIDIA NIM,
  OpenAI and Ollama remain in `services/llm.py` for reference; list them in
  `VISIBLE_PROVIDERS` (`services/settings.py`) to show them again
- No LangChain dependency, so it can run in Streamlit in Snowflake
- Reports library with search, filters and a page per report: PDF, Word and Markdown downloads and
  one-click email to saved recipient lists (Snowflake email, SMTP or SendGrid)
- Maintenance: work orders raised from early warnings and protective stops, repair and return to
  service, plant availability and downtime; state written to `MANUFACTURING_8D.OPERATIONS`
- Corrective actions: every containment (D3), corrective (D6) and preventive (D7) action in a finished
  8D report becomes a tracked action with a named owner and due date; done corrective actions are
  verified effective after 14 days without a repeat breakdown, or reopen the 8D
- Spare parts & purchasing: stock ledger on top of `PARTS_INVENTORY`; work orders reserve their parts,
  completing a repair issues what was used; reorder suggestions include parts that machines heading for
  failure will need, and flag when a vendor lead time is too long; requested → approved → ordered → received
- Business impact: the rolling customer order book per machine, orders a stop makes late (value,
  penalties, customers), cost per hour of downtime, the best window for planned work, alternate machines
  and OEE; shown on work orders, Live Monitoring, the Command Center and machine-down emails
- Daily digest email: overdue actions, parts to order, purchase requests and orders at risk
- Plant Assistant: a chat window in the bottom-right corner backed by a Cortex Agent

## Quick Start

### Streamlit Version (Local)
```bash
cd OEE_DASHBOARD_8D_INTELLIGENCE/snowflake_app
pip install -r requirements.txt
streamlit run streamlit_app.py
```
The app reads `~/.snowflake/connections.toml` for Snowflake; pick a saved connection (or enter
credentials) under **Settings > Snowflake connection**.

### Set up any Snowflake account (one command)
Everything the Streamlit app uses lives in one database. `sql/setup.py` builds it in any account:
```bash
python sql/setup.py --connection <name> [--database MANUFACTURING_8D] [--warehouse COMPUTE_WH] [--app-role <role>]
                    [--timezone Asia/Kolkata]
#   data       PRODUCTION schema, 16 tables loaded with row-count checks (incl. PRODUCTS, PART_VENDORS), views
#   email      EIGHT_D_REPORT_EMAIL notification integration + REPORT_STAGE
#   ops        OPERATIONS schema: machine state, work orders, events, recipient lists,
#              email log, INVESTIGATIONS (8D reports) and APP_SETTINGS (plant-wide settings)
#   business   ORDER_BOOK (rolled nightly by ROLL_ORDER_BOOK), CAPA_ACTIONS, PART_TRANSACTIONS,
#              PURCHASE_ORDERS, V_PART_STOCK / V_CAPA_OPEN, the digest procedures and two daily tasks
#   assistant  PLANT_OPERATIONS_SV semantic view + PLANT_ASSISTANT Cortex Agent
#   grants     what the app owner role needs (only with --app-role)
```
`--steps data,ops` runs a subset; `--database`/`--warehouse` rewrite the names in the scripts. The
stored procedures are Python: `setup.py` zips `sql/procs.py` with `core/orderbook.py`, `core/impact.py`
and `core/digest.py` into `OPERATIONS.APP_LIB`, so Snowflake runs the same code as the app. The tasks
(`EIGHT_D_ROLL_ORDER_BOOK` 00:15, `EIGHT_D_DAILY_DIGEST` 07:00) use `--timezone` or the account's
`TIMEZONE`; the digest hour and time zone can be changed in Settings › Email & recipients.

**Nothing is hard-coded per account.** Object names derive from one database
(`services/settings.names()`): inside Snowflake it is the database the Streamlit app is deployed in
(`CURRENT_DATABASE()`), locally `EIGHT_D_DATABASE` or `MANUFACTURING_8D`. Moving accounts means
running `setup.py` and pointing the local app at the new connection (Settings > Snowflake connection).

**Snowflake is the only backend.** Pages and agents read the Snowflake tables; if they cannot be
read the app shows a "Plant data unavailable" page with the reason and a Retry button instead of
silently switching to files. Work orders, machine state, 8D reports, corrective actions, the parts
ledger, purchase orders, recipient lists, the email log and plant-wide settings (email, automation,
assistant, model, business costs, digest; never secrets) are stored in
`OPERATIONS`, so they survive restarts and redeploys and every viewer sees the same plant. The
bundled CSVs in `data/synthetic` are what `setup.py` loads; the app only reads them directly when
"Local files" is chosen explicitly for an offline demo.

### Deploy to Streamlit in Snowflake (container runtime)
```bash
python sql/deploy_app.py --connection <name> [--database DB] [--warehouse WH] [--compute-pool POOL]
```
uploads only the app code, theme and fonts (never `.local_settings.json`, `.runs/`, `.ops/` or
tests), installs `requirements.txt` from Snowflake's PyPI mirror (`ARTIFACT_REPOSITORIES`, works on
trial accounts) and creates `<db>.PRODUCTION.EIGHT_D_INTELLIGENCE` on the Python 3.11 container
runtime. With Snowflake CLI 3.14+, `snow streamlit deploy --replace` with `snowflake.yml` does the
same (then set `ARTIFACT_REPOSITORIES` as noted in that file). Inside Snowflake the agents and data
use the app's embedded connection and owner role (needs `SNOWFLAKE.CORTEX_USER`). The deployed app
and a local copy pointed at the same account share `OPERATIONS`: run live monitoring in one of them.

### Public deployment (Streamlit Community Cloud)
The same app runs outside Snowflake against a Snowflake account. Put the connection in the app's
secrets (Community Cloud: *App settings › Secrets*; locally `.streamlit/secrets.toml`, git-ignored):
```toml
[snowflake]
account = "<orgname-accountname>"
user = "<user>"
role = "<role>"
warehouse = "COMPUTE_WH"
database = "MANUFACTURING_8D"
private_key = """-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----"""
# or: password = "..."  (key pair is preferred: a hosted app cannot answer an MFA prompt)
```
With a `[snowflake]` secret the app always uses it, binds to that database, and locks the connection
and data-source settings so visitors cannot repoint it. Live monitoring stops 20 minutes after the
last viewer leaves so the warehouse can suspend. Entry point: `snowflake_app/streamlit_app.py`
(requirements in `snowflake_app/requirements.txt`, theme in `.streamlit/config.toml`). See
`.streamlit/secrets.toml.example`.

### Reports: PDF, Word and email
Each finished investigation has its own page (`/report_view?id=<report id>`, linkable) with
PDF, Word (.docx) and Markdown downloads, built from the run on demand.

**Email** goes out with one click to the default saved list (**Email · Quality leads**); the menu
next to it sends to any other list or to custom recipients. Lists live in Settings › Email &
recipients and can also receive every finished report and machine-down alerts automatically. Pick
the transport there:

| Transport | Reaches | PDF | Setup |
|-----------|---------|-----|-------|
| Snowflake email | verified users of this account | secure link (stage, up to 7 days) | `sql/06_report_email.sql` |
| SMTP (Office 365, Gmail, SES, Mailgun, relay) | anyone | attached | `sql/08_external_email.sql` inside Snowflake |
| SendGrid | anyone | attached | `sql/08_external_email.sql` inside Snowflake |

Inside Snowflake, SMTP and SendGrid need an external access integration for the mail host and a
secret (`mail_smtp` as a PASSWORD secret, `mail_sendgrid_key` as a GENERIC_STRING secret) attached
to the app; uncomment the matching lines in `snowflake.yml`. Locally the values entered in Settings
are used. Every send, manual or automatic, is recorded in `OPERATIONS.EMAIL_LOG`.

### Plant Assistant (Cortex Agent)
`sql/09_assistant.sql` creates the semantic view `OPERATIONS.PLANT_OPERATIONS_SV` (machines, live
state, work orders, alerts, maintenance logs, defects, corrective actions, spare-part stock, purchase
orders and the customer order book) and the agent `OPERATIONS.PLANT_ASSISTANT`
(Cortex Analyst over it). The chat dock calls the Agents REST API with the container's session
token inside Snowflake and `SNOWFLAKE.CORTEX.DATA_AGENT_RUN` locally; each question carries a short
live-context summary (what is down, what is close to failure, the machine on screen).

## Data

16 tables of synthetic manufacturing data (plus the rolling ORDER_BOOK) with 3 embedded defect scenarios:

| Scenario | Machine | Issue | Detection |
|----------|---------|-------|-----------|
| A | MCH-005 CNC Lathe | Bearing degradation causing dimensional defects | Vibration trending 1.8 -> 5.0 mm/s |
| B | SUP-004 material | Contaminated steel causing surface porosity | 12% defect rate on affected lots |
| C | Line-D Night Shift | Process temperature drift from training gap | Bond failures cluster on night shift |

Live faults: bearing wear (vibration), spindle thermal runaway (temperature), hydraulic leak (pressure),
drive motor overload (power), coolant blockage (coolant flow) and drive belt slip (spindle speed). The
live spindle speed is set around the ~2,500 rpm the ERP process records log per order (`PROCESS_RECORDS.SPEED_RPM`),
and the 8D agents compare the live reading with that machine's ERP baseline.

### Live monitoring and maintenance
The Streamlit app streams on the real clock: every machine reports all six signals each sampling
interval (1, 2 or 5 s), stamped with the wall-clock time it was produced, and keeps streaming when
you change pages or freeze the view.

Each machine has a persisted condition that develops in wall-clock time: healthy, slow wear (days
to the limit), a fault inside the planning window (hours), and optionally one machine close to
failure (tens of minutes) so a full stop-and-repair cycle can be followed in one session. Stopping
and restarting monitoring, or coming back the next day, continues the same timeline. The baseline
is learned from each machine's last normal period, the last 7 days are rebuilt at 10-minute and
1-minute resolution, and time to failure is fitted on those series, so it reads in minutes, hours
or days, is refitted every minute and counts down in between. The telemetry is generated; the
anomaly model, drift classifier and forecast are real and run on it.

Lifecycle: a machine whose signal stays above a critical limit for 10 s **stops automatically**,
stops streaming and gets an emergency work order (component, parts from `PARTS_INVENTORY`,
technician from `OPERATORS`, duration and cost from `MAINTENANCE_LOGS`). It stays down, across
restarts too, until the repair is started and completed on the Maintenance page; it then
**commissions** (learns its new normal with alerts off) and returns to service with a new model
version. A forecast inside the planning horizon raises a planned work order first. Automation
(Settings › Automation) can start an 8D investigation for every stop (queued if one is running) and
email the alert and the finished report.

What is stored where: telemetry stays in memory (bounded series per machine). Machine state, work
orders, alerts and state changes, recipient lists and the email log are written to
`MANUFACTURING_8D.OPERATIONS` (`sql/07_operations.sql`, created automatically when the role may),
only when something changes plus a health refresh every 5 minutes; with local files as the data
source they go to `snowflake_app/.ops`. The plant tables in `PRODUCTION` are only read. Finished
8D investigations are saved as JSON in `.runs/`; emailed PDFs go to `REPORT_STAGE` (Snowflake email)
or are attached (SMTP, SendGrid).

The labels are plain language (health, unusual behaviour, behaviour change, time to failure); turn
on **Engineer view** in Settings › Appearance to see the statistics behind them (Hotelling T² score
in σ, PSI, fit R², p-values).

Investigations run in a background worker, so you can move between pages while the agents work;
the sidebar shows a Running badge and the report appears in Reports when the run finishes.

## Project Structure

```
OEE_DASHBOARD_8D_INTELLIGENCE/
  data/                 - Plant data generator + CSVs
  sql/                  - Snowflake DDL and loader scripts
  snowflake_app/        - Streamlit app + shared core
    core/               - data, monitoring engine, agent pipeline
    tools/              - SPC, traceability, COPQ, prediction
    services/           - LLM providers, settings, run manager (queue), live monitor thread,
                          operations store, mail transports, automation, Plant Assistant client,
                          report export (PDF/Word)
    ui/                 - design kit (CSS, icons, charts), app shell, maintenance widgets, chat dock
    app_pages/          - the pages plus the report page (report_view.py)
    streamlit_app.py    - entry point (st.navigation)
```

## LLM Providers

The app shows Snowflake AI_COMPLETE only. Clients for the providers below remain in
`services/llm.py` and can be re-enabled in `VISIBLE_PROVIDERS`:

| Provider | Cost | Setup |
|----------|------|-------|
| Ollama | Free | `ollama pull llama3.1:8b` |
| Groq | Free tier | API key from groq.com |
| Google Gemini | Free tier | API key from aistudio.google.com |
| NVIDIA NIM | Free tier | API key from build.nvidia.com |
| Snowflake AI_COMPLETE | Credits | Snowflake account with Cortex |
| OpenAI | Paid | API key from platform.openai.com |
