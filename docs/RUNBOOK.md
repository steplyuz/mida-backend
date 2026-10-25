# MIDA backend — Windows PowerShell runbook

All commands run in PowerShell from the `backend` folder unless stated otherwise.

## 1. First-time setup

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` in a text editor. Do not paste secrets into chat, tickets or logs. Required for a working local server:
- `APP_SECRET_KEY`: at least 32 random characters (`python -c "import secrets; print(secrets.token_hex(32))"`)
- `TELEGRAM_BOT_TOKEN`: from BotFather
- `TELEGRAM_WEBHOOK_SECRET`: at least 32 characters, must match the value given to Telegram `setWebhook`

Leave `MOCK_PAYMENT_PROVIDER=false` in anything that is not a local demo.

## 2. Database

```powershell
alembic upgrade head        # create or update the schema
alembic current             # shows the revision in use
alembic check               # must print: No new upgrade operations detected.
```

## 3. Run the server

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Check it is alive: `Invoke-RestMethod http://127.0.0.1:8000/health` (expects `status: ok`), and `http://127.0.0.1:8000/ready` (expects `database: ok`). Swagger is at `/docs` outside production.

Behind a reverse proxy, start uvicorn with `--proxy-headers --forwarded-allow-ips <proxy address>` and nothing wider. Otherwise every client looks like the proxy's address, and the per-address login limit throttles everyone together.

## 4. Tests and quality gates

```powershell
pytest                       # 395 tests; live Telegram tests are excluded by default
ruff check .
ruff format --check .
mypy
```

Live Telegram check (only with real credentials, never in CI):

```powershell
$env:TG_API_ID = "..."; $env:TG_API_HASH = "..."; $env:TG_SESSION_PATH = "C:\mida-secrets\admin.session"
pytest -m telegram_live
```

## 5. Backups

The database and the evidence files are separate. A database backup alone does not restore evidence.

```powershell
python scripts\backup_db.py --source mida.db --dest backups
Copy-Item -Recurse storage\evidence backups\evidence-$(Get-Date -Format yyyyMMdd-HHmm)
```

The script uses SQLite's online backup and checks the copy with `PRAGMA integrity_check`, so it is safe while the server runs. Store backups where only the operator can read them: they contain personal data. Test a restore at least once: copy a backup to a scratch name, point `DATABASE_URL` at it, start the server and read a deal.

## 6. Operating the Telegram group flow

- Failed group creation: `GET /api/v1/admin/outbox?status=failed`, then `POST /api/v1/admin/outbox/{id}/retry` (admin token).
- Group status for a deal: `GET /api/v1/deals/{id}/telegram-group`.
- Stuck group creation with an "started" audit event but no "created" event: an operator must check the admin account's group list by hand. A group may have been created and not recorded (see the README residual risk). Do not retry blindly.

## 7. Operating payments

- Reconciliation report: `GET /api/v1/admin/payments/reconciliation`. Mismatches are for a human to resolve; nothing changes automatically.
- Payment webhooks that are not applied show an outcome in the audit trail (`payment.flagged.*`). Read the reason before taking any action.

## 8. Retention jobs

Run from an interactive Python shell in the backend folder, with an explicit cutoff:

```powershell
python -c "import asyncio, datetime as d; from app.db.session import create_engine_from_url, create_session_factory; from app.services.retention import purge_idempotency_records as p; from app.core.config import get_settings; e=create_engine_from_url(get_settings().database_url); asyncio.run(p(create_session_factory(e)(), older_than=d.datetime.now(d.UTC)-d.timedelta(days=2)))"
```

Audit logs are never purged by these jobs. Deal messages and evidence have no retention policy yet; do not delete them by hand.

## 9. Rotating secrets

Rotating `APP_SECRET_KEY` signs out every user (their tokens stop verifying). Rotating `TELEGRAM_WEBHOOK_SECRET` requires calling Telegram `setWebhook` with the new value first. Rotate the mock provider secret only in local environments.

## 10. Stopping and starting

Stop: `Ctrl+C` in the uvicorn window. The worker finishes its current batch and stops. Start again with step 3.
