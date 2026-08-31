"""Multi-provider relay sender CLI (one account per provider).

Rotates across the providers in providers.json. Each provider is used until
its configured daily cap is spent, then the next takes over. Stops entirely
when every provider's daily cap is exhausted for the day (quotas reset next
day automatically).

Works over HTTPS (port 443) - no port 25, no MX, no PTR needed. Runs fine on
Render free, since these are just outbound HTTPS POSTs to the provider APIs.

Usage:
    python send_relay.py --config providers.json -f hello@YOURDOMAIN.com -s "Hi" -b body.txt
    python send_relay.py --config providers.json -f hello@YOURDOMAIN.com -s "Hi" -b body.txt --limit 50 --dry-run
    Ctrl+C to stop early.

Set the daily_cap of each slot to its provider's real free quota, and verify
your sending domain's SPF/DKIM/DMARC on every provider (the sender domain is
where reputation counts, since the provider owns the delivering IP).
"""

import argparse
import io
import os
import sys
import time

from relay import RelaySender, RelayResult, RelayStatus
from seendb import SeenStore

TAG = {
    RelayStatus.SENT: "SENT ",
    RelayStatus.FAILED: "FAIL ",
    RelayStatus.INVALID: "SKIP ",
    RelayStatus.QUOTA: "QUOTA",
    RelayStatus.ERROR: "ERROR",
}


def read_lines(path: str) -> list:
    with open(path, "r", encoding="utf-8-sig") as f:
        return [ln.strip() for ln in f if ln.strip()]


class EventVariants:
    """Load an event's message variants and rotate them per recipient.

    Layout (drop a file to add a variant, delete a file to remove one):
        events/<event>/subject.txt   -> subject line (optional)
        events/<event>/msg_1.txt     -> plain-text variant 1
        events/<event>/msg_1.html    -> html variant 1 (optional)
        events/<event>/msg_2.txt     -> plain-text variant 2
        ... and so on.

    --event <name> points at events/<name>/. Each recipient gets one variant,
    rotating round-robin so no two neighbours share the same message.
    """

    def __init__(self, name: str, events_dir: str = "events"):
        self.root = os.path.join(events_dir, name)
        if not os.path.isdir(self.root):
            raise FileNotFoundError("event folder not found: " + self.root)
        pairs = self._load_pairs()
        if not pairs:
            raise FileNotFoundError("no msg_*.txt variants found in " + self.root)
        self.pairs = pairs
        self.name = name
        self.subject = self._read_optional("subject.txt")

    def _read_optional(self, filename: str):
        path = os.path.join(self.root, filename)
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
        return ""

    def _load_pairs(self):
        pairs = []
        for f in sorted(os.listdir(self.root)):
            if not f.startswith("msg_") or not f.endswith(".txt"):
                continue
            base = f[:-4]
            with open(os.path.join(self.root, f), "r", encoding="utf-8") as fh:
                text = fh.read()
            html_path = os.path.join(self.root, base + ".html")
            html = None
            if os.path.isfile(html_path):
                with open(html_path, "r", encoding="utf-8") as fh:
                    html = fh.read()
            pairs.append((base, text, html))
        return pairs

    def variant_for(self, index: int, subject: str = ""):
        _, text, html = self.pairs[index % len(self.pairs)]
        return subject or self.subject, text, html

    def __len__(self):
        return len(self.pairs)


def main():
    ap = argparse.ArgumentParser(description="Multi-provider relay sender (one account per provider)")
    ap.add_argument("recipients", help="file with one recipient email per line")
    ap.add_argument("--config", required=True, help="providers.json with one account per provider")
    ap.add_argument("-f", "--from", dest="from_email", required=True, help="sender hello@yourdomain.com")
    ap.add_argument("--display", default=None, help="display name for sender")
    ap.add_argument("-s", "--subject", default="", help="email subject (overrides event subject)")
    ap.add_argument("-b", "--body", default=None, help="plain-text body file (single message mode)")
    ap.add_argument("-bhtml", "--body-html", default=None, help="html body file (single message mode)")
    ap.add_argument("--event", default=None,
                    help="event folder under events/ holding msg_1.txt..msg_N.txt variants; rotate per recipient")
    ap.add_argument("--limit", type=int, default=0, help="stop after N successful sends (0 = until all quota used)")
    ap.add_argument("--max-today", type=int, default=0,
                    help="hard cap for THIS run/day (warm-up ramp). Stops once N total sends are reached.")
    ap.add_argument("--dry-run", action="store_true", help="validate + rotate only, do not call the APIs")
    ap.add_argument("--seen-db", default="seen.sqlite", help="SQLite tracking ever-mailed addresses")
    ap.add_argument("--no-seen", action="store_true", help="disable persistent dedupe")
    ap.add_argument("--priority", default=None, nargs="*",
                    help="provider labels in priority order; defaults to JSON order")
    args = ap.parse_args()

    event = None
    if args.event:
        event = EventVariants(args.event)
        print(f"[event] {event.name}: loaded {len(event)} variant(s) "
              f"({ ', '.join(b for b, _, _ in event.pairs) })"
              + (f", subject: {event.subject}" if event.subject else "") + "\n")

    def send_params(i: int):
        if event is not None:
            return event.variant_for(i, args.subject)
        with open(args.body, "r", encoding="utf-8") as f:
            text = f.read()
        html = None
        if args.body_html:
            with open(args.body_html, "r", encoding="utf-8") as f:
                html = f.read()
        return args.subject, text, html

    recipients = read_lines(args.recipients)
    if not recipients:
        print("No recipients found.")
        return
    if args.max_today and len(recipients) > args.max_today:
        recipients = recipients[:args.max_today]
        print(f"[warm-up] capping today's batch to {args.max_today} recipients\n")

    slots = RelaySender.load_slots(args.config)
    if args.priority:
        order = {name: i for i, name in enumerate(args.priority)}
        slots.sort(key=lambda s: order.get(s.label, len(order)))
    if not slots:
        print("No provider slots loaded from", args.config)
        return

    store = None if args.no_seen else SeenStore(args.seen_db)
    sender = RelaySender(
        from_email=args.from_email,
        from_display=args.display,
        slots=slots,
        seen_store=store,
    )

    out_handle = None
    counts = {"sent": 0, "failed": 0, "invalid": 0, "quota": 0, "duplicates": 0}
    print(f"Relay sending {len(recipients)} recipients via {len(slots)} provider(s) (HTTPS)\n")
    start = time.time()
    i = 0
    try:
        def iter_recipients():
            for j, addr in enumerate(recipients):
                subj, text, html = send_params(j)
                yield addr, subj, text, html

        results = []
        def on_result(r):
            nonlocal i
            i += 1
            print(f"{TAG[r.status]}  {r.address:42} {r.message}")
        for addr, subj, text, html in iter_recipients():
            results.append(sender.send(addr, subj, text, html, dry_run=args.dry_run))
            on_result(results[-1])
    except KeyboardInterrupt:
        print("\n(interrupted)")
        stats = None

    sent = sum(1 for r in results if r.status is RelayStatus.SENT)
    failed = sum(1 for r in results if r.status is RelayStatus.FAILED)
    invalid = sum(1 for r in results if r.status is RelayStatus.INVALID)
    quota = sum(1 for r in results if r.status is RelayStatus.QUOTA)
    dup = sum(1 for r in results if r.status is RelayStatus.ERROR and "seen" in r.message.lower())

    elapsed = time.time() - start
    print("\n=== SUMMARY ===")
    for k, v in (("sent", sent), ("failed", failed), ("invalid", invalid), ("quota", quota)):
        print(f"  {k:11}: {v}")
    if sent and elapsed:
        print(f"  rate      : {sent / elapsed:.1f}/sec")
    print("\n  per-provider usage (today):")
    try:
        for sl in sender._daily.snapshot():
            print(f"    {sl.label:14} {sl.used_today:>4}/{sl.daily_cap:<4}")
    except Exception:
        pass

    if store is not None:
        store.close()
    print(f"Results logged (per-send) to console; ever-mailed tracked in {args.seen_db}")


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()