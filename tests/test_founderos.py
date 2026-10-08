import base64, hashlib, hmac, json
from datetime import date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from founderos import anomalies, db, ingest, kpis
from founderos.models import Base, Metric, Order, Payment, RawEvent, Shipment
from sqlalchemy import select


@pytest.fixture()
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("SHOPIFY_WEBHOOK_SECRET", "shp")
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", "rzp")
    monkeypatch.setenv("SHIPROCKET_WEBHOOK_TOKEN", "tok")
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    db.configure("sqlite:///:memory:")
    db.init_db()
    from founderos.api import app
    with TestClient(app) as c:
        yield c


def shp_sig(body: bytes) -> str:
    return base64.b64encode(hmac.new(b"shp", body, hashlib.sha256).digest()).decode()


def order_payload(oid=1, cust=10, created="2026-09-20T10:00:00+05:30", gross="1000", disc="100", cod=False):
    return {"id": oid, "name": f"#{oid}", "created_at": created, "total_line_items_price": gross,
            "total_discounts": disc, "customer": {"id": cust, "email": "a@b.c"},
            "payment_gateway_names": ["Cash on Delivery (COD)"] if cod else ["Razorpay"],
            "line_items": [{"sku": "X", "title": "X", "quantity": 2, "price": "500"}]}


def post_order(client, payload, wid="w1"):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/shopify", content=body, headers={
        "X-Shopify-Hmac-Sha256": shp_sig(body), "X-Shopify-Topic": "orders/create", "X-Shopify-Webhook-Id": wid})


def test_rejects_bad_signature(client):
    r = client.post("/webhooks/shopify", content=b"{}", headers={"X-Shopify-Hmac-Sha256": "nope"})
    assert r.status_code == 401
    assert client.post("/webhooks/shiprocket", json={}, headers={"x-api-key": "bad"}).status_code == 401


def test_order_ingest_is_idempotent(client):
    assert post_order(client, order_payload()).json()["status"] == "ok"
    assert post_order(client, order_payload()).json()["status"] == "duplicate"
    # same order re-sent with a new webhook id updates instead of duplicating
    post_order(client, order_payload(disc="200"), wid="w2")
    with db.session_scope() as s:
        orders = s.execute(select(Order)).scalars().all()
        assert len(orders) == 1 and orders[0].discount == 200 and orders[0].units == 2


def test_razorpay_amounts_and_signature(client):
    body = json.dumps({"event": "payment.failed", "payload": {"payment": {"entity": {
        "id": "pay_1", "amount": 54900, "fee": 0, "status": "failed", "method": "upi",
        "error_code": "GATEWAY_ERROR", "created_at": 1789000000, "notes": {}}}}}).encode()
    sig = hmac.new(b"rzp", body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": sig})
    assert r.json()["status"] == "ok"
    with db.session_scope() as s:
        p = s.execute(select(Payment)).scalar_one()
        assert p.amount == 549.0 and p.status == "failed" and p.error_code == "GATEWAY_ERROR"


def test_shiprocket_lifecycle_and_out_of_order(client):
    h = {"x-api-key": "tok"}
    base = {"awb": "A1", "order_id": "1", "freight_charges": 60}
    client.post("/webhooks/shiprocket", headers=h, json=dict(base, current_status="DELIVERED", current_timestamp="24 09 2026 10:00:00"))
    client.post("/webhooks/shiprocket", headers=h, json=dict(base, current_status="PICKED UP", current_timestamp="21 09 2026 10:00:00"))
    with db.session_scope() as s:
        sh = s.execute(select(Shipment)).scalar_one()
        assert sh.status == "delivered" and sh.delivered_at is not None and sh.cost == 60


def test_kpis_math(client):
    day = date(2026, 9, 20)
    post_order(client, order_payload(1, 10, "2026-09-20T10:00:00+05:30", "1000", "100"), "a")
    post_order(client, order_payload(2, 11, "2026-09-20T12:00:00+05:30", "500", "0", cod=True), "b")
    post_order(client, order_payload(3, 10, "2026-09-21T09:00:00+05:30", "800", "0"), "c")  # repeat customer next day
    with db.session_scope() as s:
        m = kpis.compute_day(s, day)
        assert m["orders"] == 2 and m["gross_revenue"] == 1500 and m["discounts"] == 100
        assert m["net_revenue"] == 1400 and m["aov"] == 700 and m["cod_share"] == 50
        assert m["new_customers"] == 2 and m["repeat_rate"] == 0
        m2 = kpis.compute_day(s, date(2026, 9, 21))
        assert m2["returning_customers"] == 1 and m2["new_customers"] == 0 and m2["repeat_rate"] == 100


def test_cohort_retention(client):
    post_order(client, order_payload(1, 10, "2026-08-10T10:00:00+05:30"), "a")
    post_order(client, order_payload(2, 11, "2026-08-11T10:00:00+05:30"), "b")
    post_order(client, order_payload(3, 10, "2026-09-05T10:00:00+05:30"), "c")
    with db.session_scope() as s:
        kpis.compute_cohorts(s)
        cells = {(c.cohort_month, c.offset): c for c in s.execute(select(kpis.CohortCell)).scalars()}
        assert cells[("2026-08", 0)].size == 2 and cells[("2026-08", 0)].retained == 2
        assert cells[("2026-08", 1)].retained == 1


def test_anomaly_detector_flags_spike_only(client):
    end = date(2026, 9, 20)
    with db.session_scope() as s:
        for i in range(15):
            d = end - timedelta(days=15 - i)
            s.add(Metric(date=d, key="payment_failures", value=1 + (i % 2)))   # 1,2,1,2...
            s.add(Metric(date=d, key="orders", value=40 + (i % 3)))
        s.add(Metric(date=end, key="payment_failures", value=9))
        s.add(Metric(date=end, key="orders", value=41))
        s.flush()
        found = {a["metric_key"]: a for a in anomalies.detect(s, end)}
        assert found["payment_failures"]["severity"] == "critical"
        assert "orders" not in found
