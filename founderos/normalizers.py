"""Source-specific payload -> unified schema. All functions are idempotent upserts,
so replaying a webhook (retries, backfills) never double-counts."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import Customer, InventoryItem, Order, Payment, Shipment


# ---------- time helpers ----------
def _tz() -> ZoneInfo:
    return ZoneInfo(settings().timezone)


def to_local(value: Any) -> datetime:
    """ISO string / unix seconds / datetime -> naive datetime in the business timezone."""
    if value is None:
        return datetime.now(_tz()).replace(tzinfo=None)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, timezone.utc).astimezone(_tz()).replace(tzinfo=None)
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip().replace("Z", "+00:00")
        for fmt in ("%d %m %Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None  # type: ignore
        if dt is None:
            dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        return dt  # assume already business-local
    return dt.astimezone(_tz()).replace(tzinfo=None)


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------- Shopify ----------
def _unit_costs(s: Session) -> Dict[str, float]:
    return {r.sku: r.unit_cost for r in s.execute(select(InventoryItem)).scalars()}


def _is_cod(payload: Dict[str, Any]) -> bool:
    gws = payload.get("payment_gateway_names") or []
    return any("cod" in str(g).lower() or "cash on delivery" in str(g).lower() for g in gws)


def upsert_shopify_order(s: Session, p: Dict[str, Any]) -> Order:
    ext = str(p["id"])
    order = s.execute(select(Order).where(Order.external_id == ext)).scalar_one_or_none()
    created = to_local(p.get("created_at"))

    # customer
    customer = None
    c = p.get("customer") or {}
    if c.get("id") is not None:
        cext = str(c["id"])
        customer = s.execute(select(Customer).where(Customer.external_id == cext)).scalar_one_or_none()
        if customer is None:
            customer = Customer(external_id=cext, email=c.get("email"),
                                name=" ".join(filter(None, [c.get("first_name"), c.get("last_name")])) or None,
                                first_order_at=created)
            s.add(customer)
            s.flush()
        elif customer.first_order_at is None or created < customer.first_order_at:
            customer.first_order_at = created

    items = p.get("line_items") or []
    units = sum(int(i.get("quantity") or 0) for i in items)
    gross = _f(p.get("total_line_items_price")) or sum(_f(i.get("price")) * int(i.get("quantity") or 0) for i in items)
    costs = _unit_costs(s)
    if items and all(i.get("sku") in costs for i in items):
        cogs = sum(costs[i["sku"]] * int(i.get("quantity") or 0) for i in items)
    else:
        cogs = gross * settings().default_cogs_ratio

    if order is None:
        order = Order(external_id=ext, created_at=created)
        s.add(order)
    order.name = str(p.get("name") or ext)
    order.customer_id = customer.id if customer else None
    order.created_at = created
    order.gross = gross
    order.discount = _f(p.get("total_discounts"))
    order.units = units
    order.cogs = cogs
    order.payment_method = "cod" if _is_cod(p) else "prepaid"
    order.status = "cancelled" if p.get("cancelled_at") else "open"
    order.line_items = [{"sku": i.get("sku"), "title": i.get("title"), "qty": i.get("quantity"),
                         "price": _f(i.get("price"))} for i in items]
    s.flush()
    return order


def apply_shopify_refund(s: Session, p: Dict[str, Any]) -> None:
    rid = f"shopify-refund-{p['id']}"
    order_ext = str(p.get("order_id"))
    amount = sum(_f(t.get("amount")) for t in (p.get("transactions") or []) if t.get("kind", "refund") == "refund")
    if s.execute(select(Payment).where(Payment.external_id == rid)).scalar_one_or_none() is None:
        s.add(Payment(external_id=rid, order_ref=order_ext, created_at=to_local(p.get("created_at")),
                      amount=amount, status="refunded", method="shopify"))
        s.flush()
    order = s.execute(select(Order).where(Order.external_id == order_ext)).scalar_one_or_none()
    if order:
        order.refund_amount = _f(s.execute(select(func.sum(Payment.amount)).where(
            Payment.order_ref == order_ext, Payment.status == "refunded")).scalar())


def handle_shopify(s: Session, topic: str, p: Dict[str, Any]) -> str:
    if topic in ("orders/create", "orders/updated", "orders/paid", "orders/cancelled"):
        upsert_shopify_order(s, p)
        return "order upserted"
    if topic == "refunds/create":
        apply_shopify_refund(s, p)
        return "refund applied"
    return "ignored"


# ---------- Razorpay ----------
def handle_razorpay(s: Session, event: str, body: Dict[str, Any]) -> str:
    entity = (((body.get("payload") or {}).get("payment") or {}).get("entity")) or {}
    if event not in ("payment.captured", "payment.failed", "payment.authorized") or not entity:
        return "ignored"
    status = {"payment.captured": "captured", "payment.failed": "failed"}.get(event)
    if status is None:  # authorized: wait for capture
        return "ignored"
    notes = entity.get("notes") or {}
    ref = notes.get("shopify_order_id") or notes.get("order_ref") or entity.get("order_id")
    ext = str(entity["id"])
    pay = s.execute(select(Payment).where(Payment.external_id == ext)).scalar_one_or_none()
    if pay is None:
        pay = Payment(external_id=ext, created_at=to_local(entity.get("created_at")), status=status)
        s.add(pay)
    pay.order_ref = str(ref) if ref else None
    pay.status = status
    pay.amount = _f(entity.get("amount")) / 100.0  # paise -> rupees
    pay.fee = _f(entity.get("fee")) / 100.0
    pay.method = entity.get("method")
    pay.error_code = entity.get("error_code") if status == "failed" else None
    s.flush()
    return f"payment {status}"


# ---------- Shiprocket ----------
_STATUS_MAP = [
    ("rto", "rto"), ("undelivered", "rto"), ("delivered", "delivered"),
    ("cancel", "cancelled"), ("picked", "shipped"), ("transit", "shipped"),
    ("out for delivery", "shipped"), ("shipped", "shipped"), ("dispatch", "shipped"),
    ("new", "pending"), ("pickup", "pending"), ("ready", "pending"),
]


def map_shiprocket_status(raw: str) -> str:
    r = (raw or "").lower()
    for needle, mapped in _STATUS_MAP:
        if needle in r:
            return mapped
    return "pending"


def handle_shiprocket(s: Session, p: Dict[str, Any]) -> str:
    awb = str(p.get("awb") or p.get("shipment_id") or "")
    if not awb:
        return "ignored"
    status = map_shiprocket_status(p.get("current_status", ""))
    ts = to_local(p.get("current_timestamp"))
    ship = s.execute(select(Shipment).where(Shipment.external_id == awb)).scalar_one_or_none()
    if ship is None:
        ship = Shipment(external_id=awb, created_at=ts)
        s.add(ship)
    ship.order_ref = str(p.get("order_id")) if p.get("order_id") is not None else ship.order_ref
    ship.courier = p.get("courier_name") or ship.courier
    cost = _f(p.get("freight_charges") or p.get("shipment_charge"))
    if cost:
        ship.cost = cost
    if status == "shipped" and ship.shipped_at is None:
        ship.shipped_at = ts
    if status == "delivered":
        ship.shipped_at = ship.shipped_at or ts
        ship.delivered_at = ts
    if status == "rto":
        ship.shipped_at = ship.shipped_at or ts
        ship.rto_at = ship.rto_at or ts
    # never move a terminal state backwards when events arrive out of order
    terminal = {"delivered", "rto", "cancelled"}
    if not (ship.status in terminal and status not in terminal):
        ship.status = status
    s.flush()
    return f"shipment {ship.status}"
