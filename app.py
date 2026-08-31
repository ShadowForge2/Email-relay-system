"""Self-hosted email delivery service (deploy to Render).

Your own delivery API: it accepts a send request, rotates through the
provider slots in providers.json (HTTPS - works on Render free, no port 25),
and dedupes against seen.sqlite so nobody is mailed twice.

Endpoints:
  GET  /health            liveness
  GET  /status            per-slot daily quota usage
  POST /send              send to one or more addresses
  POST /campaign          bulk-send with an optional limit

Environment:
  PROVIDERS_CONFIG   path to providers.json   (default providers.json)
  SEEN_DB            sqlite dedupe file        (default seen.sqlite)
  FROM_EMAIL         default sender            (required at runtime if not in request)
  FROM_DISPLAY       default display name

Run locally:
  pip install -r requirements.txt
  uvicorn app:app --reload
"""

import os

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from typing import List, Optional

from relay import RelaySender, RelayResult, RelayStatus
from seendb import SeenStore

app = FastAPI(title="Email Delivery", version="1.0")

CONFIG = os.environ.get("PROVIDERS_CONFIG", "providers.json")
SEEN_DB = os.environ.get("SEEN_DB", "seen.sqlite")
DEFAULT_FROM = os.environ.get("FROM_EMAIL", "")
DEFAULT_DISPLAY = os.environ.get("FROM_DISPLAY", "")


class EmailIn(BaseModel):
    to: List[str] = Field(..., min_length=1)
    subject: str = ""
    body_text: Optional[str] = None
    body_html: Optional[str] = None
    from_email: Optional[str] = None
    display: Optional[str] = None
    dry_run: bool = False


class CampaignIn(EmailIn):
    limit: int = 0


def _build_sender(from_email: Optional[str], display: Optional[str], store) -> RelaySender:
    sender = from_email or DEFAULT_FROM
    if not sender:
        raise HTTPException(status_code=400, detail="from_email is required")
    slots = RelaySender.load_slots(CONFIG)
    if not slots:
        raise HTTPException(status_code=500, detail="no provider slots in " + CONFIG)
    return RelaySender(from_email=sender, from_display=display or DEFAULT_DISPLAY or sender,
                       slots=slots, seen_store=store)


def _send(req: EmailIn) -> dict:
    store = SeenStore(SEEN_DB)
    try:
        sender = _build_sender(req.from_email, req.display, store)
        try:
            stats = sender.send_many(
                req.to,
                subject=req.subject,
                body_text=req.body_text,
                body_html=req.body_html,
                dry_run=req.dry_run,
            )
        finally:
            sender.close()
    finally:
        store.close()
    return stats


@app.get("/health")
def health():
    return {"status": "ok", "service": "email-delivery"}


@app.get("/status")
def status():
    slots = RelaySender.load_slots(CONFIG)
    return {"slots": [{"provider": s.provider, "label": s.label, "daily_cap": s.daily_cap} for s in slots],
            "seen_db": SEEN_DB}


@app.post("/send")
def send_one(req: EmailIn):
    return _send(req)


@app.post("/campaign")
def send_campaign(req: CampaignIn):
    targets = req.to
    if req.limit and len(targets) > req.limit:
        targets = targets[:req.limit]
    limited = EmailIn(to=targets, subject=req.subject, body_text=req.body_text,
                      body_html=req.body_html, from_email=req.from_email,
                      display=req.display, dry_run=req.dry_run)
    return _send(limited)