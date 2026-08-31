# Email Relay System

Self-hosted, provider-slot-based email delivery engine that rotates through
multiple ESP slots (Brevo, Mailtrap, SES, ...), dedupes recipients against a
SQLite seen-store, and supports per-recipient message-variant rotation.

Target regions: **British** and **Arabian** audiences.

## Features
- Provider slots in `providers.json` (see `providers.json.example`), each with
  its own daily cap; the relay uses a slot until exhausted, then moves on.
- HTTPS transports only — works on Render free (no port 25): Brevo, Mailtrap,
  and AWS SES (SigV4).
- SQLite dedupe so an address is never mailed twice.
- Per-recipient message rotation via `events/<name>/` (subject + N text/html
  variants), so no two neighbouring recipients receive the same message.
- `poll_emails.py` pulls fresh subscriber addresses from the pooler API.

## Setup
1. `pip install -r requirements.txt`
2. Copy `providers.json.example` to `providers.json` and fill in real API keys.
3. Poll recipients:
   ```
   python poll_emails.py --audience british --channel gmail --count 1000 --token <POOLER_TOKEN>
   ```
4. Send with variant rotation:
   ```
   python send_relay.py --event cpbloomfx --recipients recipients_english.txt --max-today 100 --dry-run
   ```

## Endpoints (app.py)
- `GET /health` — liveness
- `GET /status` — per-slot quota usage
- `POST /send` — send to one or more addresses
- `POST /campaign` — scheduled send: one-at-a-time, randomized delays, random
  message variants, skip bounces (no retry), stops at provider daily caps.
  Body: `{"to": [...], "event": "cpbloomfx", "delay_lo": 30, "delay_hi": 90, "limit": 100}`

## Deploy to Render
Requires the Render free **web** service plus a free **PostgreSQL** database
(the free tier's filesystem is ephemeral — every restart wipes local files, so
dedupe and daily caps must live in Postgres).

1. Connect the GitHub repo to a Render Blueprint (uses `render.yaml`).
2. Set these env vars (never commit them to git):
   - `PROVIDERS_JSON` — the JSON array that would otherwise live in
     `providers.json`, e.g. `[{"provider":"brevo","api_key":"...","daily_cap":300}]`
   - `FROM_EMAIL` and `FROM_DISPLAY`
   - `DATABASE_URL` is wired automatically from the PostgreSQL database.
3. Trigger a send with `POST /campaign`.

> The real `providers.json` (with live keys) is gitignored and is **not** in the
> repo. On Render inject it via `PROVIDERS_JSON`.

## Security
`providers.json`, recipient lists, seen DB, and private DKIM keys are gitignored
and never committed. Put real credentials in `providers.json` (local) or the
`PROVIDERS_JSON` env var (Render), and use env vars / `--token` for the pooler
secret.
