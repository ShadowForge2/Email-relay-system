"""HTTPS relay transport with daily-cap key rotation.

Sends through ESP REST APIs (no port 25 needed), stacking multiple
``(provider, api_key, daily_cap)`` slots. The engine uses a slot until that
slot's configured daily cap is spent for the day, then the next slot takes
over, and so on until the day's send target is done or every slot is
exhausted.

example providers.json::

    [
      {"provider": "brevo",    "api_key": "...", "daily_cap": 300},
      {"provider": "smtp2go",  "api_key": "...", "daily_cap": 200},
      {"provider": "mailjet",  "api_key": "...", "api_secret": "...", "daily_cap": 200},
      {"provider": "sendpulse","api_key": "...", "api_secret": "...", "daily_cap": 400}
    ]

Compliance note: one key = one account. Adding several keys of the SAME
provider means several accounts, which most ESPs treat as quota-farming and
close when detected. Keep one slot per provider unless you have written
permission.
"""

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from queue import Queue
from typing import Callable, Dict, Iterable, List, Optional

VALID_ADDRESS_RE = r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$'


class RelayStatus(Enum):
    SENT = "sent"
    FAILED = "failed"
    INVALID = "invalid"
    QUOTA = "quota"          # all slots exhausted for the day
    ERROR = "error"          # provider rejected / unreachable


@dataclass
class RelayResult:
    address: str
    status: RelayStatus
    message: str
    slot: str = ""


@dataclass
class SlotStats:
    provider: str
    label: str
    daily_cap: int
    used_today: int = 0


class DailySlots:
    """Tracks today's real sends per slot. Usage resets at midnight."""

    def __init__(self, slots: list):
        self._slots = slots
        self._used = {id(s): 0 for s in slots}
        self._day = date.today()
        self._lock = threading.Lock()

    def _roll(self):
        today = date.today()
        if today != self._day:
            self._day = today
            for k in self._used:
                self._used[k] = 0

    def remaining(self, slot) -> int:
        with self._lock:
            self._roll()
            return max(0, slot.daily_cap - self._used[id(slot)])

    def claim(self, slot) -> bool:
        with self._lock:
            self._roll()
            if self._used[id(slot)] >= slot.daily_cap:
                return False
            self._used[id(slot)] += 1
            return True

    def release(self, slot):
        with self._lock:
            self._roll()
            self._used[id(slot)] = max(0, self._used[id(slot)] - 1)

    def all_exhausted(self) -> bool:
        with self._lock:
            self._roll()
            return all(self._used[id(s)] >= s.daily_cap for s in self._slots) if self._slots else True

    def snapshot(self) -> List[SlotStats]:
        with self._lock:
            self._roll()
            return [SlotStats(s.provider, s.label, s.daily_cap, self._used[id(s)]) for s in self._slots]


@dataclass
class ProviderSlot:
    provider: str
    label: str
    api_key: str
    api_secret: str = ""
    account_id: str = ""
    daily_cap: int = 100
    _token: Optional[str] = field(default=None, repr=False)
    _token_expires: float = 0.0


def _json_post(url: str, headers: Dict[str, str], payload: dict, timeout: int = 30):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
        status = resp.status
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except Exception:
            parsed = {}
        return status, parsed


def _cred_header(slot: ProviderSlot) -> Dict[str, str]:
    if slot.provider == "smtp2go":
        return {"Content-Type": "application/json", "Accept": "application/json"}
    if slot.provider == "mailjet":
        token = base64.b64encode("{}:{}".format(slot.api_key, slot.api_secret).encode()).decode()
        return {"Authorization": "Basic " + token,
                "Content-Type": "application/json", "Accept": "application/json"}
    if slot.provider == "sendpulse":
        return {"Authorization": "Bearer " + slot._token,
                "Content-Type": "application/json"}
    if slot.provider == "ahasend":
        return {"Authorization": "Bearer " + slot.api_key, "Content-Type": "application/json"}
    if slot.provider == "mailtrap":
        return {"Authorization": "Bearer " + slot.api_key, "Content-Type": "application/json",
                "User-Agent": "email-delivery/1.0 (http://xprfire.site)"}
    if slot.provider == "ses":
        return {}  # signed inline in send_via_slot
    # brevo
    return {"api-key": slot.api_key, "Content-Type": "application/json", "Accept": "application/json"}


def _obtain_sendpulse_token(slot: ProviderSlot):
    if slot._token and time.time() < slot._token_expires:
        return
    token_data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": slot.api_key,
        "client_secret": slot.api_secret,
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.sendpulse.com/oauth/access_token",
        data=token_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        parsed = json.loads(resp.read().decode("utf-8"))
    slot._token = parsed.get("access_token", "")
    slot._token_expires = time.time() + int(parsed.get("expires_in", 3600)) - 300


def _ses_sign(method: str, url: str, headers: Dict[str, str], payload: bytes,
              access_key: str, secret_key: str, region: str) -> Dict[str, str]:
    """Sign an HTTP request using AWS Signature Version 4 for SES."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.hostname
    now = datetime.now(timezone.utc)
    datestamp = now.strftime("%Y%m%d")
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    canonical_uri = parsed.path
    canonical_querystring = ""
    content_type = "application/x-amz-json-1.1"
    x_amz_target = "SESV2.SendEmail"
    payload_hash = hashlib.sha256(payload).hexdigest()

    headers_to_sign = {
        "content-type": content_type,
        "host": host,
        "x-amz-date": amz_date,
        "x-amz-target": x_amz_target,
    }
    signed_headers = ";".join(sorted(headers_to_sign.keys()))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers_to_sign.items()))

    canonical_request = (
        f"{method}\n{canonical_uri}\n{canonical_querystring}\n"
        f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
    )
    credential_scope = f"{datestamp}/{region}/ses/aws4_request"
    string_to_sign = (
        f"AWS4-HMAC-SHA256\n{amz_date}\n{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
    )

    def _sign(key, msg):
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _sign(("AWS4" + secret_key).encode(), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "ses")
    k_signing = _sign(k_service, "aws4_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    auth_header = (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return {
        "Content-Type": content_type,
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": x_amz_target,
        "X-Amz-Content-Sha256": payload_hash,
        "Authorization": auth_header,
    }


def send_via_slot(from_email: str, from_display: str, to: str,
                  subject: str, text: str, html: Optional[str],
                  slot: ProviderSlot) -> str:
    """POST one email through the provider's REST API.

    Returns status line like "201 accepted by brevo". Raises on transport
    error / non-2xx (caller decides).
    """
    if slot.provider == "sendpulse":
        _obtain_sendpulse_token(slot)
        url = "https://api.sendpulse.com/smtp/emails"
        payload = {
            "sender": {"email": from_email, "name": from_display},
            "to": [{"email": to}],
            "email": {"subject": subject, "body": text or "", "html": html or ""},
        }
    elif slot.provider == "brevo":
        url = "https://api.brevo.com/v3/smtp/email"
        payload = {
            "sender": {"email": from_email, "name": from_display},
            "to": [{"email": to}],
            "subject": subject,
            "textContent": text or "",
        }
        if html:
            payload["htmlContent"] = html
    elif slot.provider == "mailjet":
        url = "https://api.mailjet.com/v3.1/send"
        payload = {"Messages": [{
            "From": {"Email": from_email, "Name": from_display},
            "To": [{"Email": to}],
            "Subject": subject,
            "TextPart": text or "",
            "HTMLPart": html or "",
        }]}
    elif slot.provider == "smtp2go":
        url = "https://api.smtp2go.com/v3/email/send"
        payload = {
            "api_key": slot.api_key,
            "to": [to],
            "sender": from_email,
            "subject": subject,
            "text_body": text or "",
        }
        if html:
            payload["html_body"] = html
    elif slot.provider == "ahasend":
        url = "https://api.ahasend.com/v2/accounts/{}/messages".format(slot.account_id)
        payload = {
            "from": {"email": from_email, "name": from_display},
            "recipients": [{"email": to}],
            "subject": subject,
            "text_content": text or "",
        }
        if html:
            payload["html_content"] = html
    elif slot.provider == "mailtrap":
        url = "https://send.api.mailtrap.io/api/send"
        payload = {
            "from": {"email": from_email, "name": from_display},
            "to": [{"email": to}],
            "subject": subject,
            "text": text or "",
        }
        if html:
            payload["html"] = html
    elif slot.provider == "ses":
        region = slot.account_id or "us-east-1"  # account_id stores region
        url = f"https://email.{region}.amazonaws.com/v2/email/outbound-emails"
        ses_payload = {
            "FromEmailAddress": f"{from_display} <{from_email}>",
            "Destination": {"ToAddresses": [to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {}
                }
            }
        }
        if text:
            ses_payload["Content"]["Simple"]["Body"]["Text"] = {"Data": text, "Charset": "UTF-8"}
        if html:
            ses_payload["Content"]["Simple"]["Body"]["Html"] = {"Data": html, "Charset": "UTF-8"}
        payload = ses_payload
        # Sign with SigV4 (will be handled in _json_post equivalent)
        payload_bytes = json.dumps(payload).encode("utf-8")
        headers = _ses_sign("POST", url, {}, payload_bytes, slot.api_key, slot.api_secret, region)
        req = urllib.request.Request(url, data=payload_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
            status = resp.status
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except Exception:
                parsed = {}
        if not (200 <= status < 300):
            raise RuntimeError("HTTP {} {} from {}".format(
                status, json.dumps(parsed)[:160], slot.provider))
        return "{} accepted by {}".format(status, slot.provider)
    else:
        raise ValueError("unknown provider: " + slot.provider)

    status, parsed = _json_post(url, _cred_header(slot), payload)
    if not (200 <= status < 300):
        raise RuntimeError("HTTP {} {} from {}".format(
            status, json.dumps(parsed)[:160], slot.provider))
    return "{} accepted by {}".format(status, slot.provider)


class RelaySender:
    """Threaded relay sender with daily-cap rotation across slots.

    Example:
        slots = RelaySender.load_slots("providers.json")
        sender = RelaySender(from_email="hello@x.com", slots=slots)
        sender.send_many(["a@gmail.com", "b@outlook.com"], subject="Hi", body_text="...")
    """

    def __init__(
        self,
        from_email: str,
        from_display: str = None,
        slots: Optional[List[ProviderSlot]] = None,
        seen_store=None,
        max_workers: int = 8,
        rotate_index: int = 0,
    ):
        if "@" not in from_email or " " in from_email:
            raise ValueError("from_email must be an address like hello@yourdomain.com")
        self.from_email = from_email
        self.from_display = from_display or from_email
        self.slots = slots or []
        self._seen_store = seen_store
        self._workers = max_workers
        self._daily = DailySlots(self.slots)
        self._rotate_index = max(0, rotate_index)
        self._lock = threading.Lock()
        self._results: List[RelayResult] = []

    @staticmethod
    def load_slots(path: str) -> List[ProviderSlot]:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        slots = []
        for i, item in enumerate(raw):
            slots.append(ProviderSlot(
                provider=item["provider"],
                label=item.get("label", "slot-{}".format(i + 1)),
                api_key=item["api_key"],
                api_secret=item.get("api_secret", ""),
                account_id=item.get("account_id", ""),
                daily_cap=int(item.get("daily_cap", 100)),
            ))
        return slots

    # ------------------------------------------------------------------ core

    def _pick_slot(self) -> Optional[ProviderSlot]:
        with self._lock:
            self._daily._roll()
            n = len(self.slots)
            for _ in range(n):
                slot = self.slots[self._rotate_index % n]
                self._rotate_index += 1
                if self._daily.remaining(slot) > 0:
                    return slot
        return None

    def send(
        self,
        to: str,
        subject: str = "",
        body_text: str = None,
        body_html: str = None,
        headers: Dict[str, str] = None,
        dry_run: bool = False,
    ) -> RelayResult:
        to = str(to).strip()
        if not re.match(VALID_ADDRESS_RE, to):
            return RelayResult(to, RelayStatus.INVALID, "Invalid address syntax")
        if dry_run:
            return RelayResult(to, RelayStatus.SENT, "dry-run (relay)")
        if not self.slots:
            return RelayResult(to, RelayStatus.ERROR, "No provider slots configured")
        slot = self._pick_slot()
        if slot is None:
            return RelayResult(to, RelayStatus.QUOTA, "All relay slots at daily cap")
        if not self._daily.claim(slot):
            return RelayResult(to, RelayStatus.QUOTA, "All relay slots at daily cap")
        try:
            status = send_via_slot(
                self.from_email, self.from_display, to,
                subject, body_text or "", body_html, slot,
            )
            return RelayResult(to, RelayStatus.SENT, status, slot=slot.label)
        except Exception as e:
            self._daily.release(slot)  # failure must not burn today's quota
            return RelayResult(to, RelayStatus.FAILED,
                               "{}: {}".format(slot.provider, str(e)[:160]), slot=slot.label)

    def send_many(
        self,
        recipients: Iterable[str],
        subject: str = "",
        body_text: str = None,
        body_html: str = None,
        headers: Dict[str, str] = None,
        dry_run: bool = False,
        on_result: Callable[[RelayResult], None] = None,
    ) -> dict:
        start = time.time()
        queue: Queue = Queue(maxsize=self._workers * 4)
        counts = {"sent": 0, "failed": 0, "invalid": 0, "quota": 0, "duplicates": 0}
        done = 0
        result_lock = threading.Lock()

        def _record(res: RelayResult):
            nonlocal done
            with result_lock:
                done += 1
                if res.status is RelayStatus.SENT:
                    counts["sent"] += 1
                elif res.status is RelayStatus.FAILED:
                    counts["failed"] += 1
                elif res.status is RelayStatus.INVALID:
                    counts["invalid"] += 1
                else:
                    counts["quota"] += 1
                self._results.append(res)

        def _worker():
            while True:
                item = queue.get()
                if item is None:
                    queue.task_done()
                    return
                res = self.send(item, subject, body_text, body_html, headers, dry_run=dry_run)
                _record(res)
                if on_result:
                    on_result(res)
                queue.task_done()

        workers = [threading.Thread(target=_worker, daemon=True) for _ in range(self._workers)]
        for w in workers:
            w.start()

        try:
            for address in recipients:
                a = str(address).strip()
                if not dry_run and self._seen_store is not None and not self._seen_store.register_mailed(a):
                    counts["duplicates"] += 1
                    continue
                queue.put(a)
        finally:
            for _ in range(self._workers):
                queue.put(None)
            for w in workers:
                w.join()

        totals = {
            "total": done,
            "duplicates": counts["duplicates"],
            "sent": counts["sent"],
            "failed": counts["failed"],
            "invalid": counts["invalid"],
            "quota": counts["quota"],
            "elapsed_seconds": round(time.time() - start, 2),
            "slots": [
                {"provider": s.provider, "label": s.label,
                 "daily_cap": s.daily_cap, "used_today": s.used_today}
                for s in self._daily.snapshot()
            ],
        }
        return totals

    def get_results(self) -> List[RelayResult]:
        return list(self._results)

    def close(self):
        pass