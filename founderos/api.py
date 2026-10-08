"""HTTP layer: webhook listeners, dashboard API and the static UI."""
from __future__ import annotations

import json
import math
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import func, select

from . import __version__, anomalies, connectors, db, ingest, insights, jobs, kpis
from .config import settings
from .models import CohortCell, Customer, InventoryItem, Insight, Metric, Order, Payment, RawEvent, Report, Shipment

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="FounderOS", version=__version__)


@app.on_event("startup")
def _startup() -> None:
    db.init_db()


def require_admin(x_admin_key: Optional[str] = Header(None)) -> None:
    key = settings().admin_key
    if key and x_admin_key != key:
        raise HTTPException(401, "invalid admin key")


# ------------------------------------------------------------------ webhooks
def _accept(source: str, topic: str, raw: bytes, event_id: Optional[str]) -> Dict[str, Any]:
    try:
        payload = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    with db.session_scope() as s:
        return ingest.process_event(s, source, topic, payload, event_id)


@app.post("/webhooks/shopify")
async def shopify_webhook(request: Request, x_shopify_hmac_sha256: Optional[str] = Header(None),
                          x_shopify_topic: str = Header("orders/create"), x_shopify_webhook_id: Optional[str] = Header(None)):
    raw = await request.body()
    if not ingest.verify_shopify(raw, x_shopify_hmac_sha256):
        raise HTTPException(401, "bad signature")
    return _accept("shopify", x_shopify_topic, raw, x_shopify_webhook_id)


@app.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request, x_razorpay_signature: Optional[str] = Header(None),
                           x_razorpay_event_id: Optional[str] = Header(None)):
    raw = await request.body()
    if not ingest.verify_razorpay(raw, x_razorpay_signature):
        raise HTTPException(401, "bad signature")
    try:
        event = json.loads(raw).get("event", "")
    except ValueError:
        raise HTTPException(400, "invalid JSON")
    return _accept("razorpay", event, raw, x_razorpay_event_id)


@app.post("/webhooks/shiprocket")
async def shiprocket_webhook(request: Request, x_api_key: Optional[str] = Header(None)):
    raw = await request.body()
    if not ingest.verify_shiprocket(x_api_key):
        raise HTTPException(401, "bad token")
    return _accept("shiprocket", "status", raw, None)


class AdRow(BaseModel):
    date: date
    campaign: str
    spend: float
    impressions: int = 0
    clicks: int = 0
    purchases: int = 0
    purchase_value: float = 0


@app.post("/ingest/meta-ads", dependencies=[Depends(require_admin)])
def ingest_meta_ads(rows: List[AdRow]):
    """Manual/marketing-log ingestion (e.g. from a spreadsheet or Zapier)."""
    with db.session_scope() as s:
        return {"upserted": connectors.upsert_ad_rows(s, [r.dict() for r in rows])}


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


# ------------------------------------------------------------------ dashboard API
def _as_of(s, d: Optional[date]) -> date:
    d = d or kpis.latest_date(s)
    if d is None:
        raise HTTPException(404, "No metrics yet. Run `python -m founderos seed` then `python -m founderos run-daily`.")
    return d


@app.get("/api/overview")
def overview(date_: Optional[date] = Query(None, alias="date"), days: int = 30):
    with db.session_scope() as s:
        d = _as_of(s, date_)
        today = kpis.day_values(s, d)
        # inventory KPIs are a snapshot stored on the latest computed day only
        latest = kpis.latest_date(s)
        if d != latest and latest:
            for k, v in kpis.day_values(s, latest).items():
                if kpis.KPI_BY_KEY[k].group == "Inventory":
                    today.setdefault(k, v)
        found = anomalies.detect(s, d)
        flagged = {a["metric_key"]: a for a in found}
        cards = []
        for k in kpis.KPI_DEFS:
            if k.key not in today:
                continue
            hist = kpis.series(s, k.key, d - timedelta(days=14), d)
            prev_vals = [v for dt, v in hist if dt < d][-7:]
            base = sum(prev_vals) / len(prev_vals) if prev_vals else None
            delta = ((today[k.key] - base) / base * 100) if base else None
            status = "neutral"
            if k.key in flagged and flagged[k.key]["severity"] in ("critical", "warning"):
                status = "bad"
            elif k.key in flagged and flagged[k.key]["severity"] == "good":
                status = "good"
            cards.append({"key": k.key, "label": k.label, "group": k.group, "unit": k.unit, "good": k.good,
                          "value": today[k.key], "baseline": base, "delta_pct": delta, "status": status,
                          "spark": [v for _, v in hist]})
        series = {key: [{"date": dt.isoformat(), "value": v}
                        for dt, v in kpis.series(s, key, d - timedelta(days=days - 1), d)]
                  for key in ("net_revenue", "net_profit", "orders", "net_margin_pct", "cac", "repeat_rate",
                              "payment_success_rate", "blended_roas", "aov")}
        penalty = sum(12 if a["severity"] == "critical" else 6 for a in found if a["severity"] in ("critical", "warning"))
        score = round(100 * math.exp(-penalty / 110))  # decays smoothly; never a misleading hard 0
        st, en = datetime_bounds(d)
        pay = s.execute(select(Payment.method, Payment.status, func.count(), func.sum(Payment.amount))
                        .where(Payment.created_at >= st - timedelta(days=6), Payment.created_at < en,
                               Payment.status.in_(["captured", "failed"])).group_by(Payment.method, Payment.status)).all()
        ship = s.execute(select(Shipment.status, func.count()).where(Shipment.created_at >= en - timedelta(days=30),
                                                                      Shipment.created_at < en).group_by(Shipment.status)).all()
        life = lifetime(s, d)
        return {"as_of": d.isoformat(), "brand": settings().brand, "currency": settings().currency,
                "lifetime": life["totals"], "cumulative": life["cumulative"], "pnl_30d": life["pnl_30d"],
                "health": {"score": score, "label": "Healthy" if score >= 85 else "Watch" if score >= 60 else "Needs attention",
                           "issues": len([a for a in found if a["severity"] in ("critical", "warning")])},
                "kpis": cards, "series": series, "groups": kpis.GROUP_ORDER,
                "payments": [{"method": m or "other", "status": st_, "count": c, "value": float(v or 0)} for m, st_, c, v in pay],
                "shipments_30d": {st_: c for st_, c in ship}}


def _sum(s, key: str, start: Optional[date], end: date) -> float:
    q = select(func.coalesce(func.sum(Metric.value), 0.0)).where(Metric.key == key, Metric.date <= end)
    if start:
        q = q.where(Metric.date >= start)
    return float(s.execute(q).scalar() or 0)


def lifetime(s, d: date) -> Dict[str, Any]:
    """Since-inception totals, 30-day growth, run-rate and a 30-day P&L waterfall."""
    first = s.execute(select(func.min(Metric.date))).scalar() or d
    tot = {k: _sum(s, k, None, d) for k in ("net_revenue", "gross_revenue", "net_profit", "ad_spend", "orders", "cogs")}
    cust = s.execute(select(func.count()).select_from(Customer)).scalar() or 0
    repeat = s.execute(select(func.count()).select_from(
        select(Order.customer_id).where(Order.status != "cancelled", Order.customer_id.is_not(None))
        .group_by(Order.customer_id).having(func.count() > 1).subquery())).scalar() or 0
    l30 = _sum(s, "net_revenue", d - timedelta(days=29), d)
    p30 = _sum(s, "net_revenue", d - timedelta(days=59), d - timedelta(days=30))
    profit30 = _sum(s, "net_profit", d - timedelta(days=29), d)
    cum, run = [], 0.0
    for dt, v in s.execute(select(Metric.date, Metric.value).where(Metric.key == "net_revenue", Metric.date <= d)
                           .order_by(Metric.date)).all():
        run += v
        cum.append({"date": dt.isoformat(), "value": round(run, 2)})
    pnl = {k: _sum(s, k, d - timedelta(days=29), d) for k in
           ("gross_revenue", "discounts", "refunds", "net_revenue", "cogs", "shipping_cost", "payment_fees", "ad_spend", "net_profit")}
    return {"cumulative": cum, "pnl_30d": pnl, "totals": {
        **tot, "since": first.isoformat(), "days": (d - first).days + 1, "customers": cust,
        "repeat_customer_pct": round(100 * repeat / cust, 1) if cust else 0,
        "margin_pct": round(100 * tot["net_profit"] / tot["net_revenue"], 1) if tot["net_revenue"] else 0,
        "roas": round(tot["net_revenue"] / tot["ad_spend"], 2) if tot["ad_spend"] else 0,
        "aov": round(tot["net_revenue"] / tot["orders"]) if tot["orders"] else 0,
        "cac": round(tot["ad_spend"] / cust) if cust else 0,
        "last30_revenue": l30, "prev30_revenue": p30, "last30_profit": profit30,
        "growth_pct": round(100 * (l30 - p30) / p30, 1) if p30 else None,
        "run_rate_annual": round(l30 * 365 / 30)}}


@app.get("/api/products")
def products():
    """Revenue by SKU over the last 30 days, from order line items."""
    with db.session_scope() as s:
        end = kpis.latest_date(s) or date.today()
        st = kpis._bounds(end - timedelta(days=29))[0]
        agg: Dict[str, Dict[str, Any]] = {}
        for o in s.execute(select(Order).where(Order.created_at >= st, Order.status != "cancelled")).scalars():
            for li in o.line_items or []:
                a = agg.setdefault(li.get("sku") or "?", {"sku": li.get("sku"), "name": li.get("title"), "units": 0, "revenue": 0.0})
                a["units"] += int(li.get("qty") or 0)
                a["revenue"] += float(li.get("price") or 0) * int(li.get("qty") or 0)
        return {"items": sorted(agg.values(), key=lambda a: -a["revenue"])}


def datetime_bounds(d: date):
    return kpis._bounds(d)


@app.get("/api/cohorts")
def cohorts():
    with db.session_scope() as s:
        rows = s.execute(select(CohortCell).order_by(CohortCell.cohort_month, CohortCell.offset)).scalars().all()
        by: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            c = by.setdefault(r.cohort_month, {"cohort": r.cohort_month, "size": r.size, "retention": {}, "revenue": 0.0})
            c["retention"][r.offset] = round(100 * r.retained / r.size, 1) if r.size else 0
            c["revenue"] += r.revenue
        out = list(by.values())
        for c in out:
            c["ltv"] = round(c["revenue"] / c["size"]) if c["size"] else 0
        return {"cohorts": out}


@app.get("/api/inventory")
def inventory():
    with db.session_scope() as s:
        return {"items": [{"sku": i.sku, "name": i.name, "stock": i.stock, "reorder_level": i.reorder_level,
                           "unit_cost": i.unit_cost, "price": i.price,
                           "status": "out" if i.stock <= 0 else "low" if i.stock <= i.reorder_level else "ok"}
                          for i in s.execute(select(InventoryItem).order_by(InventoryItem.stock)).scalars()]}


@app.get("/api/insights")
def get_insights(date_: Optional[date] = Query(None, alias="date")):
    with db.session_scope() as s:
        d = date_ or s.execute(select(func.max(Insight.date))).scalar()
        if d is None:
            return {"date": None, "summary": None, "items": []}
        rows = s.execute(select(Insight).where(Insight.date == d).order_by(Insight.id)).scalars().all()
        rank = {"critical": 0, "warning": 1, "info": 2, "good": 3}
        summary = next((r for r in rows if r.kind == "summary"), None)
        items = sorted([r for r in rows if r.kind == "anomaly"], key=lambda r: rank.get(r.severity, 9))
        return {"date": d.isoformat(), "origin": summary.origin if summary else None,
                "summary": summary.body if summary else None,
                "items": [{"id": r.id, "severity": r.severity, "title": r.title, "body": r.body, "action": r.action,
                           "metric_key": r.metric_key} for r in items],
                "anomalies": anomalies.detect(s, d)}


class Question(BaseModel):
    question: str


@app.post("/api/insights/ask")
def ask(q: Question):
    with db.session_scope() as s:
        return insights.ask(s, q.question[:500], _as_of(s, None))


@app.get("/api/workflows")
def workflows():
    with db.session_scope() as s:
        counts = dict(s.execute(select(RawEvent.source, func.count()).group_by(RawEvent.source)).all())
        reps = s.execute(select(Report).order_by(Report.date.desc()).limit(10)).scalars().all()
        return {"workflows": jobs.status_board(), "events_received": counts,
                "reports": [{"date": r.date.isoformat(), "file": Path(r.path).name, "slack": r.slack_status} for r in reps],
                "ai_enabled": bool(settings().openai_api_key), "slack_enabled": bool(
                    (settings().slack_bot_token and settings().slack_channel_id) or settings().slack_webhook_url)}


@app.post("/api/workflows/run", dependencies=[Depends(require_admin)])
def run_workflows(background: BackgroundTasks, workflow: Optional[str] = None):
    if workflow and workflow not in jobs.BY_ID:
        raise HTTPException(404, "unknown workflow")
    background.add_task(jobs.run_daily, None, [workflow] if workflow else None, 8, True)
    return {"queued": workflow or "all"}


@app.get("/reports/{name}")
def download_report(name: str):
    path = (settings().reports_dir / Path(name).name)
    if not path.exists() or path.suffix != ".pdf":
        raise HTTPException(404)
    return FileResponse(path, media_type="application/pdf", filename=path.name)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
