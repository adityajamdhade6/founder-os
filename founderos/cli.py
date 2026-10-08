"""`python -m founderos <command>`"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from . import db, jobs, kpis
from .connectors import import_inventory_csv
from .db import session_scope
from .models import Base


def main() -> None:
    p = argparse.ArgumentParser(prog="founderos", description="FounderOS business automation hub")
    sub = p.add_subparsers(dest="cmd", required=True)

    sv = sub.add_parser("serve", help="start the webhook listener + dashboard")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--reload", action="store_true")

    sd = sub.add_parser("seed", help="load ~90 days of demo INHAUS Coffee data (replaces all data)")
    sd.add_argument("--days", type=int, default=90)

    rd = sub.add_parser("run-daily", help="run the six workflows (schedule this with cron)")
    rd.add_argument("--date", type=date.fromisoformat, help="report date (default: yesterday)")
    rd.add_argument("--only", help="comma-separated workflow ids")
    rd.add_argument("--window", type=int, default=8, help="days of KPIs to (re)compute")
    rd.add_argument("--no-slack", action="store_true")

    bf = sub.add_parser("backfill", help="recompute KPIs for a longer history")
    bf.add_argument("--days", type=int, default=90)

    inv = sub.add_parser("import-inventory", help="load inventory from a CSV (sku,name,stock,unit_cost,price,reorder_level)")
    inv.add_argument("csv")

    sub.add_parser("workflows", help="list the six workflows")
    a = p.parse_args()

    if a.cmd == "serve":
        import uvicorn
        uvicorn.run("founderos.api:app", host=a.host, port=a.port, reload=a.reload)
        return

    db.init_db()
    if a.cmd == "seed":
        from .seed import seed
        eng = db.engine()
        Base.metadata.drop_all(eng)
        Base.metadata.create_all(eng)
        with session_scope() as s:
            print("Seeding webhook events through the ingestion pipeline...")
            print(seed(s, a.days))
        end = jobs.default_report_date()
        print("Backfilling KPI history...")
        with session_scope() as s:
            kpis.compute_range(s, end - timedelta(days=a.days - 1), end)
            kpis.compute_cohorts(s)
        for r in jobs.run_daily(end, only=["anomaly_detection", "ai_insights", "report_broadcast"], send=True):
            print(f"  {r['workflow']}: {r['status']} {r['detail'] or ''}"[:200])
        print("Done. Start the app with: python -m founderos serve")
    elif a.cmd == "run-daily":
        only = a.only.split(",") if a.only else None
        for r in jobs.run_daily(a.date, only, a.window, not a.no_slack):
            print(f"{r['workflow']:<20} {r['status']:<6} {(r['detail'] or '')[:160]}")
    elif a.cmd == "backfill":
        end = jobs.default_report_date()
        with session_scope() as s:
            print(kpis.compute_range(s, end - timedelta(days=a.days - 1), end), "days computed")
            kpis.compute_cohorts(s)
    elif a.cmd == "import-inventory":
        with session_scope() as s:
            print(import_inventory_csv(s, a.csv), "SKUs imported")
    elif a.cmd == "workflows":
        for i, w in enumerate(jobs.WORKFLOWS, 1):
            print(f"{i}. {w['id']:<18} {w['desc']}")
