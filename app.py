"""Self-hosted email delivery service (deploy to Render).

Your own delivery API: it accepts a send request, rotates through the provider
slots in providers.json (HTTPS - works on Render free, no port 25), and dedupes
against a persistent seen-store so nobody is mailed twice.

Endpoints:
  GET  /health            liveness
  GET  /status            per-slot daily quota usage
  POST /send              send to one or more addresses
  POST /campaign          bulk-send with an optional limit

Environment:
  PROVIDERS_JSON  JSON array of provider slots (preferred on Render - keeps
                  API keys out of git). Falls back to PROVIDERS_CONFIG file.
  PROVIDERS_CONFIG   path to providers.json   (default providers.json)
  SEEN_DB            sqlite dedupe file        (default seen.sqlite)
  DATABASE_URL       PostgreSQL DSN (Render). When set, dedupe + daily-quota
                     counters persist across restarts (free tier restarts wipe
                     local disk otherwise).
  FROM_EMAIL         default sender            (required at runtime if not in request)
  FROM_DISPLAY       default display name

Run locally:
  pip install -r requirements.txt
  uvicorn app:app --reload
"""

import os

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field
from typing import List, Optional

from relay import RelaySender
from seendb import store_from_env, PgDailyCount, CampaignControl
from scheduler import run_scheduler
from send_relay import EventVariants

app = FastAPI(title="Email Delivery", version="1.0")

CONFIG = os.environ.get("PROVIDERS_CONFIG", "providers.json")
PROVIDERS_JSON = os.environ.get("PROVIDERS_JSON", "")
SEEN_DB = os.environ.get("SEEN_DB", "seen.sqlite")
DEFAULT_FROM = os.environ.get("FROM_EMAIL", "")
DEFAULT_DISPLAY = os.environ.get("FROM_DISPLAY", "")
EVENTS_DIR = os.environ.get("EVENTS_DIR", "events")


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
    event: Optional[str] = None
    delay_lo: int = 30
    delay_hi: int = 90


class CampaignControlIn(BaseModel):
    key: str = Field(..., description="true to start sending, false to stop")


def _load_slots():
    if PROVIDERS_JSON:
        slots = RelaySender.load_slots_json(PROVIDERS_JSON)
    else:
        slots = RelaySender.load_slots(CONFIG)
    return slots


def _build_sender(from_email: Optional[str], display: Optional[str]):
    sender = from_email or DEFAULT_FROM
    if not sender:
        raise HTTPException(status_code=400, detail="from_email is required")
    slots = _load_slots()
    if not slots:
        raise HTTPException(status_code=500, detail="no provider slots configured")
    return RelaySender(from_email=sender, from_display=display or DEFAULT_DISPLAY or sender,
                       slots=slots)


def _send(req: EmailIn) -> dict:
    store = None
    sender = None
    try:
        store = store_from_env(SEEN_DB)
        sender = _build_sender(req.from_email, req.display)
        sender._seen_store = store
        stats = sender.send_many(
            req.to,
            subject=req.subject,
            body_text=req.body_text,
            body_html=req.body_html,
            dry_run=req.dry_run,
        )
    finally:
        if sender is not None:
            sender.close()
        if store is not None and hasattr(store, "close"):
            store.close()
    return stats


@app.get("/health", response_model=None)
@app.head("/health")
def health(response: Response = None):
    """Liveness/readiness probe for Uptime Robot.

    Returns HTTP 200 when the service is up AND its dedupe/seen store is
    reachable, else HTTP 503. Uptime Robot treats 200 as UP and 503 as DOWN.
    """
    checks = {"status": "ok", "service": "email-delivery"}
    healthy = True
    try:
        store = store_from_env(SEEN_DB)
        try:
            checks["mailed_total"] = store.mailed_count()
            checks["seen_db"] = "postgres" if "Pg" in type(store).__name__ else SEEN_DB
        finally:
            store.close()
    except Exception as e:
        healthy = False
        checks["status"] = "degraded"
        checks["error"] = "seen_store_unavailable: " + str(e)[:120]
    if response is not None:
        response.status_code = 200 if healthy else 503
    return checks


@app.get("/status")
def status():
    slots = _load_slots()
    info = {}
    try:
        store = store_from_env(SEEN_DB)
        try:
            info["seen_db"] = "postgres" if hasattr(store, "_conn") and "Pg" in type(store).__name__ else SEEN_DB
            info["mailed_total"] = store.mailed_count()
        finally:
            store.close()
    except Exception:
        info["seen_db"] = "unavailable"
    return {
        "slots": [{"provider": s.provider, "label": s.label, "daily_cap": s.daily_cap} for s in slots],
        **info,
    }


@app.post("/send")
def send_one(req: EmailIn):
    return _send(req)


@app.post("/campaign")
def send_campaign(req: CampaignIn):
    targets = req.to
    if req.limit and len(targets) > req.limit:
        targets = targets[:req.limit]

    store = None
    sender = None
    try:
        store = store_from_env(SEEN_DB)
        sender = _build_sender(req.from_email, req.display)
        sender._seen_store = store

        variants = []
        if req.event:
            ev = EventVariants(req.event, EVENTS_DIR)
            variants = [(ev.subject, t, h) for _, t, h in ev.pairs]

        quota = None
        try:
            quota = PgDailyCount(store)
        except Exception:
            quota = None

        def on_result(addr, tag, message):
            print(f"{tag:8} {addr:42} {message}")

        stats = run_scheduler(
            sender, store, targets, variants=variants or None,
            delay=(req.delay_lo, req.delay_hi),
            max_sends=req.limit, dry_run=req.dry_run, on_result=on_result,
            quota=quota,
        )
        return stats
    finally:
        if sender is not None:
            sender.close()
        if store is not None and hasattr(store, "close"):
            store.close()


@app.get("/campaign/status")
def campaign_status():
    """Show whether the campaign switch is on/off and how many sent."""
    store = None
    try:
        store = store_from_env(SEEN_DB)
        ctl = CampaignControl(store)
        active = ctl.active()
        return {
            "campaign_active": active,
            "mailed_total": store.mailed_count(),
            "seen_db": SEEN_DB,
        }
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()


@app.post("/campaign/control")
def campaign_control(req: CampaignControlIn):
    """Set the campaign start/stop switch. key=true -> start, key=false -> stop."""
    value = "true" if str(req.key).strip().lower() in ("true", "1", "start", "on") else "false"
    store = None
    try:
        store = store_from_env(SEEN_DB)
        ctl = CampaignControl(store)
        ctl.set("campaign_active", value)
        return {"campaign_active": value, "mailed_total": store.mailed_count()}
    finally:
        if store is not None and hasattr(store, "close"):
            store.close()
