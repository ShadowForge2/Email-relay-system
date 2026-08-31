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
- `POST /campaign` — bulk send with optional limit

## Security
`providers.json`, recipient lists, seen DB, and private DKIM keys are gitignored
and never committed. Put real credentials in `providers.json` and use env vars /
`--token` for the pooler secret.
