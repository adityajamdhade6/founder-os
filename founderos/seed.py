"""Demo data: ~90 days of INHAUS Coffee activity, generated as *real webhook payloads* and pushed
through the same ingestion code path as production. The final day contains deliberate problems
(repeat-order drop, payment-failure spike, margin squeeze from a coupon, CAC blow-out, stock-outs)
so the anomaly + AI workflows have something to find."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

from sqlalchemy import delete
from sqlalchemy.orm import Session

from . import connectors, ingest
from .models import InventoryItem

PRODUCTS = [
    # sku, title, price, unit_cost, stock, reorder_level, popularity
    ("INH-ESP-250", "Espresso Roast 250g", 499, 190, 160, 40, 30),
    ("INH-FLT-250", "Filter Coffee Blend 250g", 449, 170, 118, 35, 26),
    ("INH-COLD-6", "Cold Brew Bags (6)", 599, 235, 12, 30, 14),
    ("INH-MAG-01", "Ceramic Mug", 699, 280, 0, 15, 6),
    ("INH-DRP-10", "Drip Bags (10)", 549, 205, 96, 30, 16),
    ("INH-SUB-1KG", "Espresso Roast 1kg", 1699, 640, 34, 12, 8),
]
VOLUME = 0.917  # calibration knob: lifetime net revenue lands near Rs 3.5 lakh
FAIL_CODES = ["BAD_REQUEST_ERROR", "GATEWAY_ERROR", "SERVER_ERROR", "BANK_DECLINED"]
CITIES = ["Bengaluru", "Mumbai", "Delhi", "Pune", "Hyderabad", "Chennai", "Kolkata"]


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")


def seed(s: Session, days: int = 90, rng_seed: int = 7, verbose: bool = True) -> Dict[str, int]:
    rnd = random.Random(rng_seed)
    end_day = date.today() - timedelta(days=1)
    start_day = end_day - timedelta(days=days - 1)
    cutoff = datetime.combine(end_day + timedelta(days=1), datetime.min.time())  # no events from the future

    s.execute(delete(InventoryItem))
    for sku, title, price, cost, stock, reorder, _ in PRODUCTS:
        s.add(InventoryItem(sku=sku, name=title, price=price, unit_cost=cost, stock=stock, reorder_level=reorder))
    s.flush()

    weights = [p[6] for p in PRODUCTS]
    customers: List[int] = []          # customer ids that have ordered
    next_cust, next_order, next_pay, next_awb = 5_000_001, 1001, 1, 7_000_001
    counts = {"orders": 0, "payments": 0, "shipments": 0, "ad_rows": 0}
    ad_rows: List[Dict[str, Any]] = []

    for i in range(days):
        day = start_day + timedelta(days=i)
        is_last = day == end_day
        growth = VOLUME * (2.2 + 4.8 * i / days)
        weekend = 1.18 if day.weekday() >= 5 else 1.0
        n_orders = max(1, round(rnd.gauss(growth * weekend, 1.2)))
        if is_last:
            n_orders = 11
        p_new = max(0.55, 0.74 - 0.16 * i / days)             # ~30-45% of daily orders come from repeat buyers
        p_new_today = p_new if not is_last else 0.97          # repeat orders collapse on the last day
        new_today = 0

        for _ in range(n_orders):
            is_new = (not customers) or rnd.random() < p_new_today
            if is_new:
                cid = next_cust
                next_cust += 1
                customers.append(cid)
                new_today += 1
            else:
                cid = rnd.choice(customers)
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rnd.randint(7 * 3600, 23 * 3600 + 3000))
            n_items = rnd.choices([1, 2, 3], [0.62, 0.3, 0.08])[0]
            picks = rnd.choices(PRODUCTS, weights, k=n_items)
            lines: Dict[str, Dict[str, Any]] = {}
            for sku, title, price, *_ in picks:
                if sku == "INH-MAG-01" and is_last:   # stocked out: no more mug sales
                    continue
                li = lines.setdefault(sku, {"sku": sku, "title": title, "quantity": 0, "price": str(price)})
                li["quantity"] += 1
            if not lines:
                continue
            gross = sum(float(li["price"]) * li["quantity"] for li in lines.values())
            disc_rate = rnd.choice([0, 0, 0, 0, 0.05, 0.10]) if not is_last else rnd.choice([0, 0.05, 0.10, 0.10])
            discount = round(gross * disc_rate, 2)
            cod = rnd.random() < (0.30 if not is_last else 0.40)
            oid, name = next_order, f"#{next_order}"
            next_order += 1
            order = {"id": oid, "name": name, "created_at": _iso(ts), "currency": "INR",
                     "total_line_items_price": f"{gross:.2f}", "total_discounts": f"{discount:.2f}",
                     "total_price": f"{gross - discount:.2f}",
                     "payment_gateway_names": ["Cash on Delivery (COD)"] if cod else ["Razorpay"],
                     "customer": {"id": cid, "email": f"c{cid}@example.com", "first_name": "Cust", "last_name": str(cid)},
                     "shipping_address": {"city": rnd.choice(CITIES)},
                     "line_items": list(lines.values()), "cancelled_at": None}
            ingest.process_event(s, "shopify", "orders/create", order, event_id=f"shp-{oid}")
            counts["orders"] += 1

            if not cod:
                pid = f"pay_{next_pay:012d}"
                next_pay += 1
                amt = int((gross - discount) * 100)
                ingest.process_event(s, "razorpay", "payment.captured", {"payload": {"payment": {"entity": {
                    "id": pid, "amount": amt, "fee": int(amt * 0.02), "status": "captured", "method": rnd.choice(["upi", "card", "upi", "netbanking"]),
                    "created_at": _epoch(ts + timedelta(minutes=1)),
                    "notes": {"shopify_order_id": str(oid)}}}}}, event_id=f"rzp-{pid}")
                counts["payments"] += 1

            if rnd.random() < 0.02 and not is_last:
                rat = ts + timedelta(days=3)
                if rat < cutoff:
                    ingest.process_event(s, "shopify", "refunds/create", {
                        "id": 9_000_000 + oid, "order_id": oid, "created_at": _iso(rat),
                        "transactions": [{"kind": "refund", "amount": f"{gross - discount:.2f}"}]},
                        event_id=f"shp-refund-{oid}")

            # Shipment lifecycle (only events that already happened)
            awb = str(next_awb)
            next_awb += 1
            base = {"awb": awb, "order_id": str(oid), "courier_name": rnd.choice(["Delhivery", "Bluedart", "Xpressbees"]),
                    "freight_charges": rnd.choice([58, 64, 72, 85])}
            slow = rnd.random() < (0.06 if not is_last else 0.06)
            pick_at = ts + timedelta(hours=rnd.randint(6, 30) if not slow else rnd.randint(60, 90))
            rto = rnd.random() < (0.16 if cod else 0.03)
            deliver_at = pick_at + timedelta(days=rnd.randint(2, 6), hours=rnd.randint(0, 20))
            events = [("NEW", ts + timedelta(hours=1)), ("PICKED UP", pick_at)]
            events.append(("RTO DELIVERED", deliver_at + timedelta(days=2)) if rto else ("DELIVERED", deliver_at))
            for status, at in events:
                if at < cutoff:
                    ingest.process_event(s, "shiprocket", "status", dict(base, current_status=status,
                                         current_timestamp=at.strftime("%d %m %Y %H:%M:%S")))
            counts["shipments"] += 1

        # failed payments (checkout attempts that never became orders)
        for _ in range(5 if is_last else rnd.choice([0, 0, 0, 0, 1, 1])):
            pid = f"pay_{next_pay:012d}"
            next_pay += 1
            ts = datetime.combine(day, datetime.min.time()) + timedelta(seconds=rnd.randint(8 * 3600, 23 * 3600))
            ingest.process_event(s, "razorpay", "payment.failed", {"payload": {"payment": {"entity": {
                "id": pid, "amount": rnd.choice([54900, 89800, 119700]), "fee": 0, "status": "failed", "method": rnd.choice(["upi", "card"]),
                "error_code": "GATEWAY_ERROR" if is_last else rnd.choice(FAIL_CODES),
                "created_at": _epoch(ts), "notes": {}}}}}, event_id=f"rzp-{pid}")
            counts["payments"] += 1

        # Meta ads
        spend_total = (300 + 150 * n_orders) * rnd.uniform(0.9, 1.1) * (1.15 if is_last else 1.0)
        for camp, share, ctr in (("Prospecting - India", 0.7, 0.011), ("Retargeting - Cart", 0.3, 0.019)):
            spend = spend_total * share
            imp = int(spend / 75 * 1000 * rnd.uniform(0.95, 1.05) * (0.8 if is_last else 1.0))
            clk = int(imp * ctr * (0.7 if is_last else 1.0))
            ad_rows.append({"date": day, "campaign": camp, "spend": round(spend, 2), "impressions": imp, "clicks": clk,
                            "purchases": int(new_today * share), "purchase_value": round(new_today * share * 820, 2)})
        s.flush()
        if verbose and (i + 1) % 15 == 0:
            print(f"  seeded {i + 1}/{days} days")

    counts["ad_rows"] = connectors.upsert_ad_rows(s, ad_rows)
    return counts


def _epoch(local_dt: datetime) -> int:
    """True UTC epoch for a naive Asia/Kolkata (UTC+5:30) datetime."""
    return int(local_dt.replace(tzinfo=timezone.utc).timestamp()) - 19800
