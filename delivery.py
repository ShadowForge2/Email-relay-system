import smtplib
import email.utils
import time
import uuid
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from typing import List, Optional, Callable, Dict
from dataclasses import dataclass, field
from enum import Enum
import threading
import html


class DeliveryStatus(Enum):
    DELIVERED = "delivered"
    OPENED = "opened"
    BOUNCED = "bounced"
    PENDING = "pending"
    ERROR = "error"
    NOT_CONFIRMED = "not_confirmed"


@dataclass
class DeliveryResult:
    email: str
    status: DeliveryStatus
    message: str
    sent_at: float = 0.0
    confirmed_at: float = 0.0
    tracking_id: str = None
    smtp_response: str = None


class DeliveryConfirmation:
    def __init__(
        self,
        from_email: str,
        smtp_host: str = None,
        smtp_port: int = 587,
        smtp_user: str = None,
        smtp_password: str = None,
        use_tls: bool = True,
        bounce_host: str = None,
        bounce_user: str = None,
        bounce_password: str = None,
        bounce_folder: str = "INBOX",
        tracking_base_url: str = None,
        timeout: int = 30,
    ):
        self.from_email = from_email
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.use_tls = use_tls
        self.bounce_host = bounce_host
        self.bounce_user = bounce_user
        self.bounce_password = bounce_password
        self.bounce_folder = bounce_folder
        self.tracking_base_url = tracking_base_url
        self.timeout = timeout

        self._tracked: Dict[str, DeliveryResult] = {}
        self._lock = threading.Lock()

    def _new_tracking_id(self) -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _make_pending(email: str, tracking_id: str) -> DeliveryResult:
        return DeliveryResult(
            email=email,
            status=DeliveryStatus.PENDING,
            message="Pending",
            sent_at=time.time(),
            tracking_id=tracking_id,
        )

    def build_message(self, address: str, tracking_id: str) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["From"] = self.from_email
        msg["To"] = address
        msg["Subject"] = "Delivery Confirmation"
        msg["Message-ID"] = email.utils.make_msgid(domain=address.split("@")[-1])

        text = "Confirmation of email delivery."
        html_body = f"<html><body><p>Please click to confirm.</p>"

        if self.tracking_base_url:
            pixel_url = f"{self.tracking_base_url}/pixel/{tracking_id}"
            html_body += f'<img src="{pixel_url}" width="1" height="1" alt="" style="display:none" />'

        html_body += "</body></html>"

        msg.attach(MIMEText(text, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        return msg

    def send(self, email: str) -> DeliveryResult:
        tracking_id = self._new_tracking_id()
        result = DeliveryResult(
            email=email,
            status=DeliveryStatus.PENDING,
            message="Starting send",
            sent_at=time.time(),
            tracking_id=tracking_id,
        )

        with self._lock:
            self._tracked[tracking_id] = result

        if not self.smtp_host:
            result.status = DeliveryStatus.ERROR
            result.message = "No SMTP host configured to send"
            return result

        msg = self.build_message(email, tracking_id)

        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=self.timeout) as server:
                server.ehlo()
                if self.use_tls:
                    if server.has_extn("starttls"):
                        server.starttls()
                        server.ehlo()
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password)
                server.sendmail(self.from_email, [email], msg.as_string())
                result.status = DeliveryStatus.DELIVERED
                result.message = "Accepted by server for delivery"
                result.smtp_response = "sent ok"
        except Exception as e:
            result.status = DeliveryStatus.ERROR
            result.message = f"Send failed: {e}"

        return result

    def check_bounces(self, known_valid: Optional[List[str]] = None) -> List[DeliveryResult]:
        bounced = []
        if not (self.bounce_host and self.bounce_user and self.bounce_password):
            return bounced

        try:
            with imaplib.IMAP4_SSL(self.bounce_host, timeout=self.timeout) as mail:
                mail.login(self.bounce_user, self.bounce_password)
                mail.select(self.bounce_folder)
                _, data = mail.search(None, "ALL")
                for num in data[0].split():
                    _, msg_data = mail.fetch(num, "(RFC822)")
                    raw = msg_data[0][1]
                    m = email_lib.message_from_bytes(raw)
                    body = ""
                    if m.is_multipart():
                        for part in m.walk():
                            if part.get_content_type() == "text/plain":
                                body = part.get_payload(decode=True).decode(errors="ignore")
                                break
                    else:
                        body = m.get_payload(decode=True).decode(errors="ignore")

                    original_address = self._extract_unbounce_address(m, body)
                    if original_address:
                        bounced.append(DeliveryResult(
                            email=original_address,
                            status=DeliveryStatus.BOUNCED,
                            message="Bounce received (mailbox does not exist)",
                        ))
        except Exception as e:
            return []

        for b in bounced:
            self._mark_bounced(b.email)

        return bounced

    @staticmethod
    def _extract_unbounce_address(msg, body: str) -> Optional[str]:
        for header in ("X-Failed-Recipients", "X-Original-To", "Final-Recipient"):
            v = msg.get(header)
            if v:
                addr = v.strip()
                if ";" in addr:
                    addr = addr.split(";", 1)[-1]
                addr = addr.strip().strip("<>").strip()
                if "@" in addr:
                    return addr

        for line in body.splitlines():
            if "Final-Recipient" in line and ";" in line:
                addr = line.split(";", 1)[1].strip().strip("<>").strip()
                if "@" in addr:
                    return addr
            if "550" in line and "@" in line:
                tokens = line.split()
                for t in tokens:
                    if "@" in t:
                        return t.strip("<>;:.,()").strip()
        return None

    def record_open(self, tracking_id: str) -> bool:
        with self._lock:
            res = self._tracked.get(tracking_id)
            if res:
                res.status = DeliveryStatus.OPENED
                res.confirmed_at = time.time()
                res.message = "Tracking pixel fetched - inbox confirmed"
                return True
        return False

    def _mark_bounced(self, email: str):
        with self._lock:
            for res in self._tracked.values():
                if res.email == email and res.status in (DeliveryStatus.PENDING, DeliveryStatus.DELIVERED):
                    res.status = DeliveryStatus.BOUNCED
                    res.confirmed_at = time.time()
                    res.message = "Mailbox does not exist"

    def confirm_by_open(self, tracking_id: str) -> Optional[DeliveryResult]:
        return self.record_open(tracking_id) and self._tracked.get(tracking_id)

    def deliverability_report(
        self,
        emails: List[str],
        wait_seconds: int = 60,
        progress_callback: Optional[Callable[[int, int, DeliveryResult], None]] = None,
    ) -> Dict[str, DeliveryResult]:
        # Send first
        results = {}
        for i, email in enumerate(emails):
            r = self.send(email)
            results[email] = r
            if progress_callback:
                progress_callback(i + 1, len(emails), r)

        # Wait for bounce window, then check
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            self.check_bounces()
            time.sleep(min(5, max(1, deadline - time.time())))

        return results

    @staticmethod
    def build_http_handler():
        try:
            from http.server import BaseHTTPRequestHandler, HTTPServer
        except Exception:
            return None

        inner = {}

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                parts = [p for p in path.split("/") if p]
                if len(parts) >= 2 and parts[0] == "pixel":
                    tid = parts[1]
                    deliverer = inner.get("deliverer")
                    if deliverer:
                        deliverer.record_open(tid)
                    self.send_response(200)
                    self.send_header("Content-Type", "image/gif")
                    self.end_headers()
                    self.wfile.write(b"\x47\x49\x46\x38\x39\x61\x01\x00\x01\x00\x00\x00\x00")
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):
                pass

        def start(host="0.0.0.0", port=8000, deliverer=None):
            inner["deliverer"] = deliverer
            httpd = HTTPServer((host, port), _Handler)
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            return httpd

        return start
