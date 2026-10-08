"""The six FounderOS workflows and the orchestrator that runs them daily.

  1. ingest_sync      - replay failed events, pull Meta ads, reconcile sources
  2. kpi_engine       - compute 40 daily KPIs for the trailing window
  3. cohort_analysis  - monthly retention cohorts (M0-M6) and cohort LTV
  4. anomaly_detection- z-score anomaly scan against the trailing 14 days
  5. ai_insights      - OpenAI narrative + recommended actions (rules fallback)
  6. report_broadcast - PDF report + Slack delivery
"""
from __future__ import annotations

import json
import traceback
from datetime import date, datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import anomalies, connectors, ingest, insights, kpis, reports
from .db import session_scope
from .models import WorkflowRun

Ctx = Dict[str, Any]


def wf_ingest_sync(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    out = dict(ingest.replay_failed(s))
    out["meta_rows"] = connectors.pull_meta_ads(s, d - timedelta(days=ctx["window"]), d)
    return out


def wf_kpi_engine(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    n = kpis.compute_range(s, d - timedelta(days=ctx["window"] - 1), d)
    return {"days_computed": n, "kpis_per_day": len(kpis.day_values(s, d))}


def wf_cohort_analysis(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    return {"cohorts": kpis.compute_cohorts(s)}


def wf_anomaly_detection(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    found = anomalies.detect(s, d)
    return {"anomalies": len(found), "critical": sum(1 for a in found if a["severity"] == "critical")}


def wf_ai_insights(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    return insights.generate(s, d)


def wf_report_broadcast(s: Session, d: date, ctx: Ctx) -> Dict[str, Any]:
    path = reports.build_pdf(s, d)
    status = reports.broadcast(s, d, path) if ctx.get("send", True) else "skipped"
    return {"pdf": path.name, "slack": status}


WORKFLOWS: List[Dict[str, Any]] = [
    {"id": "ingest_sync", "name": "Data ingestion & normalisation", "fn": wf_ingest_sync, "ai": False,
     "desc": "Replays failed webhook events, pulls Meta ad spend and reconciles Shopify, Razorpay and Shiprocket into one schema."},
    {"id": "kpi_engine", "name": "KPI engine", "fn": wf_kpi_engine, "ai": False,
     "desc": "Computes 40 daily KPIs: revenue, margins, payments, fulfilment, customers, CAC and inventory."},
    {"id": "cohort_analysis", "name": "Cohort & LTV analysis", "fn": wf_cohort_analysis, "ai": False,
     "desc": "Builds monthly acquisition cohorts with M1-M6 retention and lifetime value."},
    {"id": "anomaly_detection", "name": "Anomaly detection", "fn": wf_anomaly_detection, "ai": True,
     "desc": "Flags statistically unusual moves such as repeat-order drops, payment-failure spikes or margin shrinkage."},
    {"id": "ai_insights", "name": "AI insights engine", "fn": wf_ai_insights, "ai": True,
     "desc": "Explains what changed and why using OpenAI, then recommends one action per issue."},
    {"id": "report_broadcast", "name": "Report & Slack broadcast", "fn": wf_report_broadcast, "ai": True,
     "desc": "Generates the PDF business report and delivers it to the team's Slack channel."},
]
BY_ID = {w["id"]: w for w in WORKFLOWS}


def default_report_date() -> date:
    return date.today() - timedelta(days=1)  # last complete day


def run_workflow(wid: str, d: Optional[date] = None, window: int = 8, send: bool = True) -> Dict[str, Any]:
    """Runs one workflow in its own transaction and records a WorkflowRun row."""
    d = d or default_report_date()
    with session_scope() as s:
        run = WorkflowRun(workflow=wid, status="running")
        s.add(run)
        s.commit()
        try:
            detail = BY_ID[wid]["fn"](s, d, {"window": window, "send": send})
            run.status, run.detail = "ok", json.dumps(detail, default=str)
        except Exception as exc:
            s.rollback()
            run = s.get(WorkflowRun, run.id)
            run.status, run.detail = "error", f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}"
        run.finished_at = datetime.utcnow()
        s.commit()
        return {"workflow": wid, "status": run.status, "detail": run.detail}


def run_daily(d: Optional[date] = None, only: Optional[List[str]] = None, window: int = 8,
              send: bool = True) -> List[Dict[str, Any]]:
    results = []
    for w in WORKFLOWS:
        if only and w["id"] not in only:
            continue
        r = run_workflow(w["id"], d, window, send)
        results.append(r)
        if r["status"] == "error":  # downstream steps depend on upstream output
            break
    return results


def status_board() -> List[Dict[str, Any]]:
    board = []
    with session_scope() as s:
        for w in WORKFLOWS:
            last = s.execute(select(WorkflowRun).where(WorkflowRun.workflow == w["id"])
                             .order_by(WorkflowRun.id.desc()).limit(1)).scalar_one_or_none()
            board.append({"id": w["id"], "name": w["name"], "desc": w["desc"], "ai": w["ai"],
                          "last_status": last.status if last else "never",
                          "last_run": last.finished_at.isoformat() if last and last.finished_at else None,
                          "detail": last.detail if last else None})
    return board
