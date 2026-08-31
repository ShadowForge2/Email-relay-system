"""Autonomous poll-and-send loop.

Polls fresh targets from the pooler, sends each one at a time through the
provider slots (randomized delay between sends, random message variants, skip
bounces without retry), keeps polling more until every provider's daily cap is
reached, then sleeps until the cap resets (midnight) and starts again.

Clear terminal / Render logs at every step:
    [poll ] got 50 new targets (british)
    [send ] -> a@gmail.com  [variant 2]  (30s wait)
    [sent ] a@gmail.com  201 accepted by brevo
    [skip ] b@outlook.com  invalid syntax
    [bnce ] c@yahoo.com  brevo: HTTP 550 (no retry)
    [quota] all providers at daily cap; sleeping until midnight

Environment (Run locally or on Render):
  PROVIDERS_JSON       provider slots with real keys (Render) or PROVIDERS_CONFIG file
  PROVIDERS_CONFIG     path to providers.json (local default)
  DATABASE_URL         PostgreSQL DSN (Render) -> persistent dedupe + daily caps
  SEEN_DB              sqlite fallback (local)
  FROM_EMAIL           sender address (required)
  FROM_DISPLAY         sender display name
  EVENT                event name under events/ for variants (default cpbloomfx)
  POOLER_TOKEN         Bearer secret for the pooler (required)
  POOLER_BASE          pooler base URL
  POOLER_AUDIENCES     space-separated audiences (default: british arabian)
  POOLER_CHANNEL       mailbox wording (default: gmail)
  POOLER_BATCH         how many to poll per batch (default 50)
  DELAY_LO / DELAY_HI  randomized seconds between sends (default 30 / 90)

Usage:
    python run_loop.py --dry-run        # show the flow without calling APIs
    python run_loop.py --once           # poll + send one batch, then exit
    python run_loop.py                  # run forever
"""

import argparse
import json
import os
import random
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta

from poll_emails import poll as pooler_poll
from relay import RelayStatus, RelaySender
from scheduler import run_scheduler
from send_relay import EventVariants
from seendb import CampaignControl, PgDailyCount, store_from_env

CONFIG = os.environ.get("PROVIDERS_CONFIG", "providers.json")
PROVIDERS_JSON = os.environ.get("PROVIDERS_JSON", "")
SEEN_DB = os.environ.get("SEEN_DB", "seen.sqlite")
FROM_EMAIL = os.environ.get("FROM_EMAIL", "")
FROM_DISPLAY = os.environ.get("FROM_DISPLAY", "")
EVENT = os.environ.get("EVENT", "cpbloomfx")
POOLER_BASE = os.environ.get("POOLER_BASE", "https://email-database-api-pooler1000.onrender.com")
POOLER_TOKEN = os.environ.get("POOLER_TOKEN", "CHANGE-ME-PLEASE-KEEP-SAFE-9f2d")
POOLER_AUDIENCES = (os.environ.get("POOLER_AUDIENCES", "british arabian")
                    .split() or ["british", "arabian"])
POOLER_CHANNEL = os.environ.get("POOLER_CHANNEL", "gmail")
POOLER_BATCH = int(os.environ.get("POOLER_BATCH", "50"))
DELAY_LO = int(os.environ.get("DELAY_LO", "30"))
DELAY_HI = int(os.environ.get("DELAY_HI", "90"))
CAMPAIGN_ACTIVE = os.environ.get("CAMPAIGN_ACTIVE", "").strip().lower() in ("true", "1", "on", "start")


def log(tag, message):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}][{tag}] {message}", flush=True)


def seconds_until_midnight():
    now = datetime.now()
    nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, (nxt - now).total_seconds())


def egress_ip():
    for url in ("https://api.ipify.org", "https://icanhazip.com", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                return r.read().decode().strip()
        except Exception:
            continue
    return "unknown"


def load_slots():
    if PROVIDERS_JSON:
        return RelaySender.load_slots_json(PROVIDERS_JSON)
    return RelaySender.load_slots(CONFIG)


def default_payload(batch):
    payload = {"count": batch, "channel": POOLER_CHANNEL}
    if len(POOLER_AUDIENCES) == 1:
        payload["audience"] = POOLER_AUDIENCES[0]
    else:
        payload["audiences"] = POOLER_AUDIENCES
    return payload


def quota_room(quota, slots):
    if quota is None:
        return True
    return any(quota.used_today(s.label) < s.daily_cap for s in slots)


def run_forever(dry_run: bool = False, once: bool = False, _args=None):
    slots = load_slots()
    if not slots:
        log("error", "no provider slots (set PROVIDERS_JSON or PROVIDERS_CONFIG)")
        return

    store = store_from_env(SEEN_DB)
    sender = RelaySender(from_email=FROM_EMAIL or "dry@xprfire.site",
                         from_display=FROM_DISPLAY or "CPBLOOMFX", slots=slots)
    sender._seen_store = store

    variants = []
    try:
        ev = EventVariants(EVENT)
        variants = [(ev.subject, t, h) for _, t, h in ev.pairs]
        log("event", f"loaded {len(variants)} variant(s) from events/{EVENT}")
    except Exception as e:
        log("event", f"no event variants ({e}); sending plain text")

    quota = None
    try:
        quota = PgDailyCount(store)
    except Exception:
        quota = None

    ctl = None
    try:
        ctl = CampaignControl(store)
    except Exception:
        ctl = None
    log("switch", f"campaign start/stop control {'enabled' if ctl else 'disabled'}")

    log("start", f"providers={[s.label for s in slots]} targets={POOLER_AUDIENCES} "
                 f"batch={POOLER_BATCH} delay={DELAY_LO}-{DELAY_HI}s event={EVENT} "
                 f"seen={'postgres' if quota else SEEN_DB}")
    log("ip", f"egress IP visible to Brevo: {egress_ip()}")

    def on_result(addr, tag, message):
        log(tag.lower(), f"{addr}  {message}")

    try:
        while True:
            if not CAMPAIGN_ACTIVE:
                log("switch", "CAMPAIGN_ACTIVE=false; paused")
                time.sleep(30)
                continue

            if not dry_run and quota is not None and not quota_room(quota, slots):
                secs = seconds_until_midnight()
                log("quota", f"all providers at daily cap; sleeping {secs/3600:.1f}h until reset")
                time.sleep(min(secs, 3600))
                continue

            batch = POOLER_BATCH
            payload = default_payload(batch)
            log("poll", f"polling query {json.dumps(payload)}")
            try:
                targets = pooler_poll(
                    POOLER_BASE, POOLER_TOKEN, payload, store=store,
                    timeout=max(60, DELAY_HI + 60),
                )
            except Exception as e:
                log("poll", f"error: {e}; retrying in 60s")
                time.sleep(60)
                continue

            log("poll", f"got {len(targets)} NEW target(s)")
            for t in targets:
                log("poll", "  " + t)

            stats = run_scheduler(
                sender, store, targets, variants=variants or None,
                delay=(DELAY_LO, DELAY_HI),
                dry_run=dry_run, on_result=on_result, quota=quota,
            )
            log("done", f"batch finished: sent={stats['sent']} bounce={stats['bounced']} "
                        f"invalid={stats['invalid']} dupe={stats['duplicates']} "
                        f"quota={stats['quota']}")

            if once:
                break

            if dry_run:
                log("dry", "dry run complete")
                break

            log("sleep", "idle 45s before polling the next batch")
            time.sleep(45)

    except KeyboardInterrupt:
        log("stop", "interrupted")
    finally:
        sender.close()
        store.close()


def main():
    ap = argparse.ArgumentParser(description="Autonomous poll-and-send loop")
    ap.add_argument("--dry-run", action="store_true", help="show the flow, do not call APIs")
    ap.add_argument("--once", action="store_true", help="poll + send one batch, then exit")
    args = ap.parse_args()

    if not POOLER_TOKEN and not args.dry_run:
        log("error", "POOLER_TOKEN env var is required (set it before running)")
        return
    if not FROM_EMAIL and not args.dry_run:
        log("error", "FROM_EMAIL env var is required")
        return

    run_forever(dry_run=args.dry_run, once=args.once)


if __name__ == "__main__":
    main()
