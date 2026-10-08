"""Webhook authentication + the single entry point that stores raw events and
routes them to normalisers. Used by the HTTP layer and by the seed script."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import RawEvent
from . import normalizers


# ---------- signature verification ----------
def verify_shopify(raw: bytes, header: Optional[str]) -> bool:
    secret = settings().shopify_secret
    if not secret:
        return settings().allow_unsigned
    digest = base64.b64encode(hmac.new(secret.encode(), raw, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(digest, header or "")


def verify_razorpay(raw: bytes, header: Optional[str]) -> bool:
    secret = settings().razorpay_secret
    if not secret:
        return settings().allow_unsigned
    digest = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, header or "")


def verify_shiprocket(token: Optional[str]) -> bool:
    secret = settings().shiprocket_token
    if not secret:
        return settings().allow_unsigned
    return hmac.compare_digest(secret, token or "")


# ---------- routing ----------
def _dispatch(s: Session, source: str, topic: str, payload: Dict[str, Any]) -> str:
    if source == "shopify":
        return normalizers.handle_shopify(s, topic, payload)
    if source == "razorpay":
        return normalizers.handle_razorpay(s, topic, payload)
    if source == "shiprocket":
        return normalizers.handle_shiprocket(s, payload)
    return "unknown source"


def process_event(s: Session, source: str, topic: str, payload: Dict[str, Any],
                  event_id: Optional[str] = None) -> Dict[str, Any]:
    """Store the raw event (deduplicated) and normalise it. Returns a small status dict."""
    if not event_id:
        event_id = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:40]
    ev = RawEvent(source=source, event_id=event_id, topic=topic, payload=payload)
    s.add(ev)
    try:
        s.flush()
    except IntegrityError:
        s.rollback()
        return {"status": "duplicate", "event_id": event_id}
    try:
        with s.begin_nested():
            result = _dispatch(s, source, topic, payload)
        ev.processed = True
        return {"status": "ok", "result": result, "event_id": event_id}
    except Exception as exc:  # keep the raw event so it can be replayed by the ingest workflow
        ev.error = f"{type(exc).__name__}: {exc}"[:500]
        return {"status": "error", "error": ev.error, "event_id": event_id}


def replay_failed(s: Session, limit: int = 500) -> Dict[str, int]:
    """Re-run normalisation for events that previously errored (e.g. out-of-order arrivals)."""
    rows = s.execute(select(RawEvent).where(RawEvent.processed.is_(False)).limit(limit)).scalars().all()
    fixed = still = 0
    for ev in rows:
        try:
            with s.begin_nested():
                _dispatch(s, ev.source, ev.topic, ev.payload)
            ev.processed, ev.error = True, None
            fixed += 1
        except Exception as exc:
            ev.error = f"{type(exc).__name__}: {exc}"[:500]
            still += 1
    return {"replayed": fixed, "still_failing": still}
