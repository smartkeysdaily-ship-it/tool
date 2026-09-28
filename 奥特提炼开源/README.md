# UPI VIPPRO Tool API — ChatGPT Plus QR Automation

Local web tool that bulk-processes ChatGPT accounts (`email|password|2fa`), logs in with TLS fingerprint spoofing, caches sessions, submits them to a **Rust UPI Bot API**, streams payment QR codes over SSE, optionally notifies Telegram, and writes result files.

Designed for operators who need a headed UI + REST API on `localhost` with reliable realtime job updates.

---

## Features

- **Bulk account input** — paste `email|pass|totp` lines; validate preview before submit
- **Session cache** — reuse valid ChatGPT sessions to skip login when possible
- **UPI bot integration** — submit session → stream QR / payment link via SSE from `upiapi.linhtd.com` (or your own base URL)
- **Done = QR delivered** — receiving a payment QR/link marks the job **Done** (green); concurrency slot frees immediately so the queue keeps draining
- **Check Plus (side task)** — optional plan check after Done; does **not** block the pipeline
- **Pause / Resume** — running jobs continue; queued work is held until resume
- **Realtime UI** — Alpine.js dashboard with SSE (`seq` + gap detection + poll fallback)
- **Telegram** — optional QR / event notifications
- **Blocklist** — auto-block emails after repeated failures
- **SQLite persistence** — jobs survive process restart
- **Settings modal** — edit config without touching YAML (secrets masked in API responses)

---

## Requirements

| Item | Version / note |
|------|----------------|
| Python | **3.11+** recommended |
| OS | macOS / Linux / Windows |
| Network | Access to ChatGPT auth + your UPI bot API |
| UPI API token | `upi_…` key from your VIPPRO / UPI bot provider |

---

## Quick start

```bash
cd gpt_plus_qr_automation

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# First run creates config.yaml with defaults (edit token!)
python -m app.main
```

Open the UI in your browser:

```text
http://127.0.0.1:8787
```

> **Many people might face error** if they copy the Uvicorn log line  
> `Uvicorn running on http://0.0.0.0:8787` into the browser.  
> `0.0.0.0` is only the **bind** address — it is **not** a valid URL.  
> Edge/Chrome show `ERR_ADDRESS_INVALID`. Always open **`http://127.0.0.1:8787`**.

---

## Account format

One account per line:

```text
user@example.com|Password123!|JBSWY3DPEHPK3PXP
```

| Field | Rules |
|-------|--------|
| email | Valid email |
| password | Non-empty, max 128 chars |
| 2fa secret | Base32 TOTP secret (typically 16–64 chars) |

Invalid lines are counted in the left-panel preview and skipped / reported on submit.

**Draft persistence:** the textarea is saved to `localStorage` (`plusqr.accountsDraft`) and restored on reload. Use **Clear** (with confirm) to wipe it.

---

## Configuration

`config.yaml` is created on first launch if missing. It is **gitignored** — never commit real tokens.

You can edit:

1. **Settings** in the UI (gear icon) → **Save settings**
2. Or `config.yaml` directly, then restart

### Default settings

| Key | Default | Description |
|-----|---------|-------------|
| `rust_bot_base_url` | `https://upiapi.linhtd.com` | UPI bot API base |
| `rust_bot_token` | `upi_changeme` | API key (**required**) |
| `telegram_bot_token` | `""` | Telegram bot token |
| `telegram_chat_id` | `""` | Destination chat |
| `telegram_enabled` | `false` | Master Telegram switch |
| `telegram_mode` | `qr_only` | `off` \| `qr_only` \| `all` |
| `concurrency_limit` | `5` | Parallel pipeline workers `[1–20]` |
| `poll_interval_seconds` | `120` | Plus-check poll interval |
| `poll_budget` | `3` | Plus-check attempts budget |
| `hard_timeout_seconds` | `300` | Per-job hard timeout |
| `pipeline_retry_limit` | `1` | Soft-error pipeline retries |
| `skip_session_cache` | `false` | Force fresh login every job |
| `flow_mode` | `auto` | `auto` = queue immediately; `manual` = hold until Start |
| `blocklist_enabled` | `true` | Enable fail streak blocklist |
| `blocklist_auto_threshold` | `5` | Failures before auto-block |
| `stale_threshold_seconds` | `300` | No-progress → stale |
| `session_cache_ttl_buffer_seconds` | `300` | Reuse session if TTL buffer allows |
| `login_backoff_seconds` | `[2, 5, 15]` | Login backoff ladder |

`GET /api/settings` returns the **full** API key for the local Settings UI (not masked). Leaving the token field empty on save keeps the previous value.

Use **Test connection** in Settings to verify credit / reserved / user id from `GET /api/v1/me` on the UPI API.

---

## UI overview

| Area | Purpose |
|------|---------|
| **Accounts** (left) | Paste combos, Run now / Queue, Clear draft |
| **Jobs** (center) | Filters, Pause/Resume, bulk actions, per-row Start/Stop/Rerun/Plus/Delete |
| **Job log** (right, desktop) | Timeline for the selected job |
| **Results** (bottom) | Done accounts + Plus-check hits |
| **Settings** | UPI bot, Telegram, pipeline knobs |

### Flow modes

- **Auto** — submit → jobs enter `queued` and workers pick them up
- **Manual** — submit → jobs stay `held` until you click **Start** / **Start held**

### Pause behavior

- **Pause** — new / waiting jobs are held; jobs already running keep going
- **Resume** — requeues pause-held jobs

### Job actions

| Action | When |
|--------|------|
| Start | `held` |
| Stop | active states |
| Rerun | concluded (`failed`, `done`, `stopped`, …) → creates a **new** job |
| Check Plus | `done` / `qr_ready` (side task) |
| Delete | any (stops first if needed) |

**Retry failed** creates new jobs for failed/stale accounts and switches the filter to **Running** so you see them without a page reload.

---

## Job lifecycle

```text
submit
  ├─ hold     → held ──(start)──► queued
  └─ immediate → queued
        │
        ▼
   logging_in → session_ready → submitting_bot → awaiting_qr → qr_ready
        │                                                      │
        │                                                      ▼
        │                                                    done  ← terminal (QR / payment link)
        │                                                      │
        │                                            (optional) check-plan → plan_result
        ▼
   soft error → retry_scheduled → queued
   hard error / exhausted retries → failed
   stop → stopped
   watchdog → stale
```

**Important:** `done` means the UPI bot delivered a usable QR/payment link. The tool does **not** wait for Plus confirmation in the main pipeline. Use **Plus** on a row when you want a side check; results append to `results/plus_accounts.txt`.

---

## Architecture

```text
Browser UI (static/index.html, Alpine.js)
        │  REST + EventSource (/api/jobs/stream)
        ▼
FastAPI (app/main.py)
        │
        ├─ JobManager          concurrency, pause, retries, state machine
        ├─ LoginService        ChatGPT login (curl_cffi / sentinel)
        ├─ SessionCache        sessions/ on disk
        ├─ RustBotClient       UPI API + SSE parse
        ├─ TelegramNotifier    optional push
        ├─ PlusDetector        side plan check
        ├─ ResultWriter        results/*.txt
        ├─ BlocklistService    SQLite blocklist
        ├─ JobStream (SSE)     snapshot / job / bulk / ping / resync + seq
        └─ Database            data/jobs.sqlite3
```

### Realtime sync (SSE)

Events:

| Event | Payload |
|-------|---------|
| `snapshot` | Full job list + `seq` on connect |
| `job` | Single job public dict + `seq` |
| `bulk` | Action summary + full `jobs` + `seq` |
| `ping` | Keepalive every ~10s with current `seq` |
| `resync` | Sent under backpressure — client must refetch |

Client reliability:

1. Sequence gap detection → `GET /api/jobs`
2. Poll every 2s while jobs are live or SSE is down
3. Resync on tab focus / visibility / `online`
4. `X-Jobs-Seq` header prevents stale HTTP snapshots from overwriting newer SSE state

---

## Output files

| Path | When written | Format |
|------|--------------|--------|
| `results/done_accounts.txt` | Job reaches **Done** (QR) | `email\|password\|2fa` |
| `results/plus_accounts.txt` | Check Plus returns `plus` | same |
| `results/free_accounts.txt` | Legacy | same |
| `sessions/` | Cached login sessions | internal JSON |
| `data/jobs.sqlite3` | Jobs + blocklist | SQLite |
| `logs/` | Application logs | text |

Runtime dirs (`sessions/`, `results/`, `data/`, `logs/`, `config.yaml`) are gitignored.

---

## HTTP API

Base URL: `http://127.0.0.1:8787`

### Jobs

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/jobs` | List jobs (`X-Jobs-Seq` header) |
| `GET` | `/api/jobs/stream` | SSE stream |
| `POST` | `/api/jobs/submit` | Body: `{ raw_text, dispatch_mode, skip_session_cache? }` |
| `POST` | `/api/jobs/{id}/start` | Start held job |
| `POST` | `/api/jobs/{id}/stop` | Stop job |
| `POST` | `/api/jobs/{id}/rerun` | Spawn new job from concluded |
| `POST` | `/api/jobs/{id}/check-plan` | Side Plus check |
| `DELETE` | `/api/jobs/{id}` | Remove job |
| `GET` | `/api/jobs/{id}` | Single job |
| `GET` | `/api/jobs/{id}/qr.png` | QR image |

`dispatch_mode`: `default` \| `immediate` \| `hold`

### Bulk / pause

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/jobs/stop-all` | Stop active + pending |
| `POST` | `/api/jobs/pause` | Pause dispatch |
| `POST` | `/api/jobs/resume` | Resume dispatch |
| `GET` | `/api/jobs/pause-status` | `{ paused, … }` |
| `POST` | `/api/jobs/start-all-held` | Start every held job |
| `POST` | `/api/jobs/rerun-failed` | Rerun failed/stale |
| `DELETE` | `/api/jobs/clear?filter=failed\|done\|all&confirm=` | Clear jobs (`all` needs `confirm=true`) |

### Settings & account

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/settings` | Masked config |
| `PUT` | `/api/settings/{key}` | Update one field |
| `POST` | `/api/settings/bulk` | Update many fields |
| `POST` | `/api/test/rust-bot` | Ping UPI `/me` |
| `POST` | `/api/test/telegram` | Send test message |
| `GET` | `/api/account` | Credit / reserved / user |

### Results / sessions / blocklist

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/results/done` | Done file content |
| `GET` | `/api/results/plus` | Plus file content |
| `GET` | `/api/results/free` | Legacy free file |
| `GET` | `/api/session-cache` | Cached sessions |
| `DELETE` | `/api/session-cache/{email}` | Drop one cache |
| `DELETE` | `/api/session-cache` | Clear all caches |
| `GET` | `/api/blocklist` | List blocked emails |
| `POST` | `/api/blocklist` | Manual add |
| `DELETE` | `/api/blocklist/{email}` | Remove |
| `GET` | `/api/logs/export` | Export log |

### Example: submit held jobs

```bash
curl -sS -X POST http://127.0.0.1:8787/api/jobs/submit \
  -H 'Content-Type: application/json' \
  -d '{
    "raw_text": "user@example.com|Passw0rd!|JBSWY3DPEHPK3PXP",
    "dispatch_mode": "hold"
  }'
```

### Example: listen to SSE

```bash
curl -N http://127.0.0.1:8787/api/jobs/stream
```

---

## Project layout

```text
gpt_plus_qr_automation/
├── app/
│   ├── main.py              # FastAPI entry + lifespan
│   ├── job_manager.py       # Pipeline / workers / pause
│   ├── login_service.py     # ChatGPT login
│   ├── rust_bot_client.py   # UPI API + SSE parse
│   ├── sse.py               # Job event fan-out
│   ├── models.py            # States + ConfigModel
│   ├── config.py            # YAML config store
│   ├── db.py                # SQLite
│   ├── session_cache.py
│   ├── plus_detector.py
│   ├── result_writer.py
│   ├── telegram_notifier.py
│   ├── blocklist.py
│   └── routes/              # HTTP routers
├── static/index.html        # Single-page UI
├── test/                    # Smoke / check scripts
├── requirements.txt
├── config.yaml              # Local only (gitignored)
├── data/ sessions/ results/ logs/
└── README.md
```

---

## Testing

No heavy CI suite — use the check scripts under `test/`:

```bash
.venv/bin/python test/smoke_startup.py
.venv/bin/python test/check_default_settings.py
.venv/bin/python test/check_done_qr_terminal.py
.venv/bin/python test/check_pause_and_done_api.py
.venv/bin/python test/check_sse_stream.py
.venv/bin/python test/check_sse_ui_reliability.py
.venv/bin/python test/check_ui_persist_and_actions.py
.venv/bin/python test/syntax_check.py
```

---

## Security notes

- **Do not commit** `config.yaml`, `sessions/`, `results/`, or `data/`.
- Tokens are redacted in settings JSON and log scrubbing.
- Binding `0.0.0.0` exposes the UI on your LAN — use a firewall or bind `127.0.0.1` if you only need local access.
- Rotate any API key that was ever committed or shared.

---

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `ERR_ADDRESS_INVALID` / can't open page | You opened `http://0.0.0.0:8787`. Use **`http://127.0.0.1:8787`** instead |
| UI not updating | Hard refresh; check header shows **Realtime**; Pause/Retry still call `loadJobs` even if SSE drops |
| SSE reconnecting | Normal under network blips; poll fallback keeps list fresh |
| `credits_required` / low credit | Top up UPI API; Settings → Test connection |
| Login / CF failures | Prefer headed runs; avoid overloading concurrency |
| Jobs stuck held | Flow = Manual or Pause on — click **Start** / **Resume** |
| Token empty on save | Intentional — blank field keeps existing secret |

---

## License / disclaimer

Internal operator tool. You are responsible for complying with ChatGPT, payment, and API provider terms. Use only on accounts and infrastructure you are authorized to operate.
