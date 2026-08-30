# Weekly Report Agent

A small FastAPI application that turns short weekly notes into a professional email, shows the complete email for review, and sends it through Gmail only after an explicit **Send** action.

The application deliberately does not use LangChain, LangGraph, CrewAI, an ORM, or a vector database. SQLite is the workflow record and Python's standard email tools build the message.

## Safety guarantees

- Nothing is sent during generation, editing, scheduling, or cancellation.
- Clicking **Send Report** is the approval action. The application fingerprints the exact ISO week, recipient, subject, and body and claims delivery in one SQLite transaction before calling Gmail.
- Editing or regenerating clears any earlier approval.
- One report row is allowed per ISO week, and a sent report cannot be claimed again.
- A network failure with an unknown Gmail result is marked `uncertain` and quarantined instead of being retried blindly. This prevents a possible duplicate; check Gmail Sent mail before resolving it manually.
- Gmail retries are bounded and apply only to explicit transient HTTP responses (`429`, `500`, `502`, `503`, `504`). Authentication and permanent errors are not retried.
- API keys, OAuth client secrets, tokens, the database, and `.env` are ignored by Git.

## Workflow

1. APScheduler runs at the configured local weekday and time. Its idempotent job creates the current week's pending report record and logs the dashboard path.
2. Open the dashboard and enter 2-3 short notes.
3. **Generate Report** calls the OpenAI Responses API and stores both the original notes and generated body.
4. Review the recipient, subject, and complete body.
5. Choose **Send Report**, **Edit**, or **Cancel**.
6. **Edit** supports direct changes or regeneration from additional instructions. Every changed version must be reviewed again.
7. **Send Report** records approval and sends through Gmail OAuth2. The dashboard retains report and delivery history.

Because this is a local web app, the Thursday “ask” appears as a pending current-week report on the dashboard and an application log reminder. Keep the app running and visit the dashboard; no separate SMS, desktop-notification, or chat service is required.

## Local setup

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set at minimum:

```dotenv
OPENAI_API_KEY=your_api_key
MANAGER_EMAIL=manager@example.com
```

The integration uses the OpenAI Responses API with `instructions`, `input`, and `response.output_text`. `OPENAI_MODEL` is configurable so you can select a text model available to your OpenAI project.

Start the app:

```bash
uvicorn app:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>.

## Gmail OAuth2 setup

1. In Google Cloud Console, create or select a project.
2. Enable the Gmail API.
3. Configure the OAuth consent screen and add your Google account as a test user if the app is in testing mode.
4. Create an **OAuth client ID** with application type **Desktop app**.
5. Download the client JSON to `credentials.json` (or set `GMAIL_CREDENTIALS_FILE` to its path).
6. On the first approved send, complete the browser OAuth flow. The refreshable token is written to `token.json` (or `GMAIL_TOKEN_FILE`).

Only the `gmail.send` scope is requested. Never commit either OAuth file. In a headless/Docker deployment, complete the first authorization in an environment where the local callback browser can run, then mount the resulting token securely.

## Configuration

| Variable | Default | Purpose |
|---|---:|---|
| `AGENT_ENABLED` | `true` | Enables scheduling, generation, and sending |
| `TIMEZONE` | `Asia/Kolkata` | Scheduler and week calculation timezone |
| `SCHEDULE_DAY` | `thu` | Three-letter scheduled weekday |
| `SCHEDULE_HOUR` | `17` | Local hour, 0-23 |
| `SCHEDULE_MINUTE` | `0` | Local minute, 0-59 |
| `MANAGER_EMAIL` | empty | Gmail recipient |
| `SENDER_NAME` | empty | Name used after the closing in generated drafts |
| `OPENAI_API_KEY` | empty | OpenAI API key |
| `OPENAI_MODEL` | `gpt-5-mini` | OpenAI text model |
| `DATABASE_PATH` | `data/weekly_reports.db` | SQLite file |
| `GMAIL_CREDENTIALS_FILE` | `credentials.json` | Google OAuth desktop-client JSON |
| `GMAIL_TOKEN_FILE` | `token.json` | Generated OAuth token |
| `GMAIL_MAX_RETRIES` | `3` | Retries after the initial Gmail request |
| `LOG_LEVEL` | `INFO` | Python logging level |

Changes to schedule configuration apply after restart. The dashboard always calculates the next run in the configured timezone.

On this workstation, `weekly_startup.py` can be registered as a desktop-login application using `deployment/weekly-report-agent.desktop`. It keeps the local server running and opens the dashboard automatically when the login day matches `SCHEDULE_DAY`. Internet access is required only for OpenAI generation and Gmail delivery; sending still requires approval.

## Docker

Create `.env`, then put the Google files in a local `secrets/` directory:

```text
secrets/
├── credentials.json
└── token.json
```

`token.json` may be absent until authorization is completed. Then run:

```bash
docker compose up --build
```

The Compose service binds only to `127.0.0.1:8000`, persists SQLite under `./data`, and mounts OAuth material under `./secrets`. Do not expose this single-user approval UI publicly without adding authentication and HTTPS at a reverse proxy.

## Verification

Run the standard-library workflow tests:

```bash
python3 -m unittest discover -v
```

The tests prove that approval is bound to exact content, edits are blocked during delivery, a successful week cannot be sent twice, uncertain outcomes are quarantined, and ISO week keys use the configured local time.

For a configuration smoke test without making API calls:

```bash
python3 -c "from config import get_settings; from database import Database; s=get_settings(); Database(s.database_path).initialize(); print('configuration and database OK')"
```

## Stored history

SQLite stores the original notes, user edit instructions, generated report, report date and ISO week, recipient, approval state and approval fingerprint, delivery state, sent flag and timestamp, Gmail message ID, and latest error. Each Gmail attempt also records its stable RFC message ID, start/completion time, outcome, provider ID, and error.

## Operational notes

- Back up `data/weekly_reports.db` and your OAuth token using your normal encrypted backup process.
- If a delivery is `uncertain`, inspect Gmail Sent mail for the shown week before changing database state. Automatic resending is intentionally blocked.
- Run one application process. SQLite protects duplicate claims, but APScheduler is embedded; multiple web workers would each run the reminder job (the unique week key keeps draft creation idempotent).
- Application errors are logged to stdout/stderr for Docker or service-manager collection; secrets are never intentionally logged.
# Weekly-report-to-Manager
