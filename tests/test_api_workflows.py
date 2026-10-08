"""End-to-end coverage for the dashboard API, workflows, PDF report and connectors."""
import pytest
from fastapi.testclient import TestClient

from founderos import db, jobs, kpis
from founderos.db import session_scope


@pytest.fixture()
def seeded(monkeypatch, tmp_path):
    for k in ("OPENAI_API_KEY", "SLACK_BOT_TOKEN", "SLACK_WEBHOOK_URL", "ADMIN_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALLOW_UNSIGNED_WEBHOOKS", "1")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    db.configure("sqlite:///:memory:")
    db.init_db()
    from founderos.seed import seed
    with session_scope() as s:
        seed(s, 40, verbose=False)
    end = jobs.default_report_date()
    with session_scope() as s:
        kpis.compute_range(s, end - __import__("datetime").timedelta(days=39), end)
        kpis.compute_cohorts(s)
    jobs.run_daily(end, only=["anomaly_detection", "ai_insights", "report_broadcast"])
    from founderos.api import app
    with TestClient(app) as c:
        yield c


def test_dashboard_endpoints(seeded):
    ov = seeded.get("/api/overview").json()
    assert len(ov["kpis"]) >= 40 and 0 <= ov["health"]["score"] <= 100
    assert seeded.get("/api/products").json()["items"]
    assert seeded.get("/api/inventory").json()["items"]
    assert seeded.get("/api/cohorts").json()["cohorts"]
    ins = seeded.get("/api/insights").json()
    assert ins["summary"] and ins["origin"] == "rules"  # no OpenAI key -> rules fallback
    assert seeded.post("/api/insights/ask", json={"question": "why did margin drop?"}).json()["answer"]


def test_report_is_a_pdf_and_dry_run(seeded):
    wf = seeded.get("/api/workflows").json()
    assert wf["reports"] and wf["reports"][0]["slack"] == "dry_run"
    r = seeded.get(f"/reports/{wf['reports'][0]['file']}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert seeded.get("/reports/..%2F..%2Fetc%2Fpasswd").status_code == 404


def test_admin_key_protects_mutations(seeded, monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "k")
    row = [{"date": "2026-01-01", "campaign": "c", "spend": 10}]
    assert seeded.post("/ingest/meta-ads", json=row).status_code == 401
    assert seeded.post("/api/workflows/run").status_code == 401
    assert seeded.post("/ingest/meta-ads", json=row, headers={"X-Admin-Key": "k"}).json() == {"upserted": 1}
    assert seeded.post("/api/workflows/run?workflow=nope", headers={"X-Admin-Key": "k"}).status_code == 404


def test_meta_ads_upsert_is_idempotent(seeded):
    row = [{"date": "2026-01-01", "campaign": "c", "spend": 10}]
    seeded.post("/ingest/meta-ads", json=row)
    seeded.post("/ingest/meta-ads", json=[{**row[0], "spend": 25}])
    from sqlalchemy import select
    from founderos.models import AdSpend
    with session_scope() as s:
        rows = s.execute(select(AdSpend).where(AdSpend.campaign == "c")).scalars().all()
        assert [r.spend for r in rows] == [25.0]


def test_inventory_csv_import(seeded, tmp_path):
    from founderos.connectors import import_inventory_csv
    from founderos.models import InventoryItem
    f = tmp_path / "stock.csv"
    f.write_text("sku,name,stock,unit_cost,price,reorder_level\nNEW1,New bag,5,100,300,10\n")
    with session_scope() as s:
        assert import_inventory_csv(s, str(f)) == 1
        it = s.get(InventoryItem, "NEW1")
        assert (it.stock, it.reorder_level) == (5, 10)
    assert any(i["sku"] == "NEW1" and i["status"] == "low" for i in seeded.get("/api/inventory").json()["items"])


def test_workflow_failure_is_recorded_and_halts(seeded, monkeypatch):
    monkeypatch.setitem(jobs.BY_ID["kpi_engine"], "fn", lambda *a: 1 / 0)
    res = jobs.run_daily()
    assert [r["status"] for r in res] == ["ok", "error"]  # stops after the failing step
    assert "ZeroDivisionError" in res[1]["detail"]


def test_cron_endpoint_requires_secret(seeded, monkeypatch):
    assert seeded.get("/api/cron/run-daily").status_code == 401  # no secret configured -> closed
    monkeypatch.setenv("CRON_SECRET", "s")
    assert seeded.get("/api/cron/run-daily", headers={"Authorization": "Bearer wrong"}).status_code == 401
    res = seeded.get("/api/cron/run-daily", headers={"Authorization": "Bearer s"}).json()["results"]
    assert [r["status"] for r in res] == ["ok"] * 6


def test_report_is_rebuilt_when_file_is_gone(seeded):
    import os
    from founderos.config import settings
    name = seeded.get("/api/workflows").json()["reports"][0]["file"]
    os.remove(settings().reports_dir / name)  # simulates an ephemeral serverless disk
    r = seeded.get(f"/reports/{name}")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert seeded.get("/reports/founderos-report-1999-01-01.pdf").status_code == 404
