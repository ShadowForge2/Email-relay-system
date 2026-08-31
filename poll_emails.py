"""Poll subscriber email addresses from the pooler API into a recipients file.

Wraps the SUBSCRIBER POOLER query reference (API-Query.TXT). POSTs a JSON
query and writes the returned addresses one-per-line, ready for
send_mail.py / send_relay.py. Optionally dedupes against the persistent
SeenStore so addresses are never re-polled.

Examples:
    python poll_emails.py --audience british --channel gmail --count 1000
    python poll_emails.py --audiences arabian arabian british --count 1000
    python poll_emails.py --audience global --female-ratio 0.9 --count 500 --out to.txt
    python poll_emails.py --channel yahoo --extract --out pooled.txt
    python poll_emails.py --device-only --out to.txt
"""

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.request

from seendb import SeenStore

DEFAULT_BASE = "https://email-database-api-pooler1000.onrender.com"
DEFAULT_TOKEN = os.environ.get("POOLER_TOKEN", "")

ENDPOINTS = ("list", "next", "pool")


def post_json(url: str, token: str, payload: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return json.loads(body.decode("utf-8")) if body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError("HTTP {} {}: {}".format(e.code, e.reason, detail[:300]))
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: {}".format(e.reason))


def _as_email(v) -> str:
    if isinstance(v, dict):
        return str(v.get("email", "")).strip()
    return str(v).strip()


def poll(
    base: str, token: str, payload: dict, store=None, timeout: int = 60, extract: bool = False,
) -> list:
    """POST a query to the pooler and return the emails that are new (not in ``store``).

    Reusable by the CLI, the web app, and the autonomous loop. When ``store``
    is given, each returned address is registered and only fresh ones are
    returned.
    """
    endpoint = "pool" if extract else "list"
    if extract:
        payload = dict(payload)
        payload["extract"] = True
    url = "{}/v1/subscribers/{}".format(base.rstrip("/"), endpoint)
    data = post_json(url, token, payload, timeout=timeout)
    if not data.get("success"):
        raise RuntimeError("pooler returned failure: " + json.dumps(data)[:300])

    if endpoint == "pool":
        emails = [_as_email(x) for x in (data.get("pooled") or [])]
    else:
        emails = [_as_email(x) for x in (data.get("subscribers") or [])]
    emails = [e for e in emails if e]

    if store is not None:
        fresh = [e for e in emails if store.register_polled(e)]
        return fresh
    return emails


def main():
    ap = argparse.ArgumentParser(description="Poll subscriber emails from the pooler API")
    ap.add_argument("--base", default=DEFAULT_BASE, help="API base URL")
    ap.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer secret")
    ap.add_argument("--endpoint", choices=ENDPOINTS, default=None,
                    help="which endpoint to hit (default list, or pool with --extract)")
    ap.add_argument("--audience", default=None,
                    help="language group (british/arabian/iberian/european_germanic/slavic_europe/mediterranean/global)")
    ap.add_argument("--audiences", default=None, nargs="+",
                    help="weighted language list; repeat to boost a language")
    ap.add_argument("--channel", default=None, nargs="*",
                    help="mailbox wording(s) (primary= gmail, secondary= yahoo, office= outlook, hotlink= hotmail, apple= icloud, secure= protonmail, old= aol); omit = gmail")
    ap.add_argument("--count", type=int, default=1, help="1 .. 10000 (default 1)")
    ap.add_argument("--timeout", type=int, default=60,
                    help="HTTP read timeout in seconds (pooler compute can be slow)")
    ap.add_argument("--gender", default=None, choices=["male", "female", "mixed"],
                    help="gender pool (overrides female-ratio)")
    ap.add_argument("--female-ratio", type=float, default=None,
                    help="share of female entries, 0..1 (e.g. 0.85 = 85% female)")
    ap.add_argument("--person", default=None, help="explicit full name (bypasses pools)")
    ap.add_argument("--only", default=None, help="single-name username (bypasses pools)")
    ap.add_argument("--numeric", action="store_true",
                    help="append numeric variants to usernames")
    ap.add_argument("--extract", action="store_true",
                    help="(pool endpoint) pull back entries already pooled")
    ap.add_argument("--out", default="recipients.txt", help="output recipient file")
    ap.add_argument("--seen-db", default="seen.sqlite",
                    help="SQLite file tracking ever-polled addresses (default seen.sqlite)")
    ap.add_argument("--no-seen", action="store_true",
                    help="disable dedupe against the seen store entirely")
    args = ap.parse_args()

    endpoint = args.endpoint or ("pool" if args.extract else "list")

    if args.audience and args.audiences:
        print("Error: use --audience OR --audiences, never both.")
        return
    if args.gender == "mixed" and args.female_ratio:
        print("Note: gender=mixed overrides female-ratio; ignoring ratio.")

    payload = {"count": args.count}
    if args.audience:
        payload["audience"] = args.audience
    if args.audiences:
        payload["audiences"] = args.audiences
    if args.channel:
        payload["channel"] = args.channel
    if args.gender:
        payload["gender"] = args.gender
    if args.female_ratio is not None and args.gender != "mixed":
        payload["female_ratio"] = args.female_ratio
    if args.person:
        payload["person"] = args.person
    if args.only:
        payload["only"] = args.only
    if args.numeric:
        payload["numeric"] = True
    if args.extract:
        payload["extract"] = True

    url = "{}/v1/subscribers/{}".format(args.base.rstrip("/"), endpoint)
    print("POST {}  payload={}".format(url, json.dumps(payload)))
    try:
        data = post_json(url, args.token, payload, timeout=args.timeout)
    except RuntimeError as e:
        print("Error:", e)
        return

    if not data.get("success"):
        print("Error: API returned failure:", json.dumps(data)[:300])
        return

    def _as_email(v) -> str:
        if isinstance(v, dict):
            return str(v.get("email", "")).strip()
        return str(v).strip()

    emails = []
    if endpoint == "next":
        if data.get("subscriber"):
            emails = [_as_email(data["subscriber"])]
    elif endpoint == "pool" and args.extract:
        emails = [_as_email(x) for x in (data.get("pooled") or [])]
        print("pooled total={} shown={}".format(data.get("total"), data.get("shown")))
    else:
        emails = [_as_email(x) for x in (data.get("subscribers") or [])]

    emails = [e for e in emails if e]

    store = None if args.no_seen else SeenStore(args.seen_db)
    written = 0
    dup = 0
    if store is not None:
        fresh = []
        for e in emails:
            if store.register_polled(e):
                fresh.append(e)
            else:
                dup += 1
        emails = fresh
        store.close()

    with open(args.out, "a", encoding="utf-8") as f:
        for e in emails:
            f.write(e + "\n")
            written += 1

    print("\nNEWLY POLLED ({} adress):".format(written))
    for e in emails:
        print(e)
    print("Got {}  saved {} to {}  ({} already in {} )".format(
        written + dup, written, args.out, dup, args.seen_db if store is not None else "-"))


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "buffer"):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()