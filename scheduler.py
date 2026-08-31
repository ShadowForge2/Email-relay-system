"""One-at-a-time randomized campaign scheduler (server side).

Drives send_relay-style variant rotation but on the deployed app, sending
one message at a time with a randomized delay between sends, skipping
bounces/invalid addresses (no retry), and stopping at each provider's daily
cap. Daily send counters are persisted (Postgres) so restarts don't leak past
daily caps.

Usage (from app.py):
    from scheduler import run_scheduler
    stats = run_scheduler(sender, store, recipients, variants=[...],
                          quota=quota, delay=(30, 90), max_sends=0,
                          on_result=callable(addr, tag, message))
"""

import random
import time
from typing import Callable, Iterable, List, Optional, Tuple

from relay import RelayStatus


def _quota_room(quota, slots) -> bool:
    """True if at least one provider slot still has persisted daily room."""
    if quota is None:
        return True
    for s in slots:
        if quota.used_today(s.label) < s.daily_cap:
            return True
    return False


def run_scheduler(
    sender, store, recipients: Iterable[str],
    variants: Optional[List[Tuple[str, str, Optional[str]]]] = None,
    delay: Tuple[int, int] = (30, 90),
    max_sends: int = 0, dry_run: bool = False,
    on_result: Optional[Callable] = None,
    quota=None,
) -> dict:
    """Send to each recipient one at a time.

    ``variants`` is an ordered list of (subject, text, html) variants; the
    next variant is picked at random per recipient (no two neighbours share
    the same message when >= 2 variants exist). Recipients already in ``store``
    are skipped. Providers are reached via ``sender.send`` (which enforces
    in-memory daily caps). ``quota`` (if given) enforces persisted daily caps
    so restarts don't leak past a provider's limit. Bounces/errors are not
    retried.
    """
    if delay and len(delay) == 2:
        lo, hi = max(0, int(delay[0])), max(int(delay[0]), int(delay[1]))
    else:
        lo, hi = 0, 0

    sent, skipped, invalid, bounced, quota_hit, duplicates = 0, 0, 0, 0, 0, 0
    last_choice = -1
    started = time.time()
    slots = getattr(sender, "slots", []) or []

    for raw_addr in recipients:
        addr = str(raw_addr).strip()
        if not addr:
            continue

        if not dry_run and quota is not None and not _quota_room(quota, slots):
            quota_hit += 1
            break

        if not dry_run and store is not None:
            if store.seen_mailed(addr):
                duplicates += 1
                continue
            store.register_mailed(addr)

        if variants:
            while True:
                choice = random.randrange(len(variants))
                if len(variants) == 1 or choice != last_choice:
                    last_choice = choice
                    break
            subject, text, html = variants[choice]
        else:
            subject, text, html = "", "", None

        res = sender.send(addr, subject, text or "", html, dry_run=dry_run)
        if res.status in (RelayStatus.INVALID,):
            invalid += 1
        elif res.status is RelayStatus.QUOTA:
            quota_hit += 1
        elif res.status is RelayStatus.FAILED:
            bounced += 1  # provider rejected / unreachable -> skip, no retry
        elif res.status is RelayStatus.SENT:
            sent += 1
            if quota is not None and res.slot:
                try:
                    quota.increment(res.slot)
                except Exception:
                    pass
        else:
            skipped += 1
        if on_result:
            on_result(addr, _tag(res), res.message)

        if max_sends and sent >= max_sends:
            break

        if sent and (lo or hi) and not dry_run:
            time.sleep(random.randint(lo, hi))

    return {
        "sent": sent, "invalid": invalid, "bounced": bounced,
        "quota": quota_hit, "duplicates": duplicates, "skipped": skipped,
        "elapsed_seconds": round(time.time() - started, 2),
    }


def _tag(res) -> str:
    return {
        RelayStatus.SENT: "SENT", RelayStatus.FAILED: "BOUNCE",
        RelayStatus.INVALID: "SKIP", RelayStatus.QUOTA: "QUOTA",
        RelayStatus.ERROR: "ERROR",
    }.get(res.status, str(res.status.value))
