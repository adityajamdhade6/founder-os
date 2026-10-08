"""KPI engine: computes 40 daily business metrics + cohort retention (M0-M6) from the
normalised tables and stores them in `metrics` / `cohorts`. Recomputation is idempotent."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .config import settings
from .models import (AdSpend, CohortCell, Customer, InventoryItem, Metric, Order, Payment, Shipment)


@dataclass(frozen=True)
class KPI:
    key: str
    label: str
    group: str
    unit: str            # inr | count | pct | x | days
    good: Optional[str]  # "up" | "down" | None (neutral)
    watch: bool = False  # included in anomaly detection


KPI_DEFS: List[KPI] = [
    # Revenue
    KPI("gross_revenue", "Gross revenue", "Revenue", "inr", "up"),
    KPI("discounts", "Discounts given", "Revenue", "inr", None),
    KPI("refunds", "Refunds", "Revenue", "inr", "down"),
    KPI("net_revenue", "Net revenue", "Revenue", "inr", "up", True),
    KPI("orders", "Orders", "Revenue", "count", "up", True),
    KPI("aov", "Average order value", "Revenue", "inr", "up", True),
    KPI("units_sold", "Units sold", "Revenue", "count", "up"),
    KPI("refund_rate", "Refund rate", "Revenue", "pct", "down", True),
    # Payments
    KPI("payment_success_rate", "Payment success rate", "Payments", "pct", "up", True),
    KPI("payment_failures", "Failed payments", "Payments", "count", "down", True),
    KPI("failed_payment_value", "Failed payment value", "Payments", "inr", "down"),
    KPI("cod_share", "COD share of orders", "Payments", "pct", "down", True),
    # Fulfilment
    KPI("shipped_orders", "Orders shipped", "Fulfilment", "count", "up"),
    KPI("delivered_orders", "Orders delivered", "Fulfilment", "count", "up"),
    KPI("rto_rate", "RTO rate", "Fulfilment", "pct", "down", True),
    KPI("avg_delivery_days", "Avg delivery time", "Fulfilment", "days", "down", True),
    KPI("pending_shipments", "Pending shipments", "Fulfilment", "count", "down", True),
    KPI("on_time_dispatch_pct", "Dispatched within 48h", "Fulfilment", "pct", "up", True),
    # Customers
    KPI("new_customers", "New customers", "Customers", "count", "up", True),
    KPI("returning_customers", "Returning customers", "Customers", "count", "up"),
    KPI("repeat_rate", "Repeat purchase rate", "Customers", "pct", "up", True),
    KPI("active_customers_30d", "Active customers (30d)", "Customers", "count", "up"),
    KPI("avg_ltv", "Avg customer LTV", "Customers", "inr", "up"),
    # Marketing
    KPI("ad_spend", "Ad spend", "Marketing", "inr", None, True),
    KPI("impressions", "Impressions", "Marketing", "count", "up"),
    KPI("clicks", "Clicks", "Marketing", "count", "up"),
    KPI("ctr", "Click-through rate", "Marketing", "pct", "up", True),
    KPI("cpc", "Cost per click", "Marketing", "inr", "down"),
    KPI("cac", "Customer acquisition cost", "Marketing", "inr", "down", True),
    KPI("blended_roas", "Blended ROAS", "Marketing", "x", "up", True),
    # Profitability
    KPI("cogs", "Cost of goods sold", "Profitability", "inr", None),
    KPI("shipping_cost", "Shipping cost", "Profitability", "inr", "down"),
    KPI("payment_fees", "Payment gateway fees", "Profitability", "inr", "down"),
    KPI("gross_margin_pct", "Gross margin", "Profitability", "pct", "up", True),
    KPI("net_profit", "Net profit", "Profitability", "inr", "up", True),
    KPI("net_margin_pct", "Net margin", "Profitability", "pct", "up", True),
    # Inventory (snapshot of the latest computed day)
    KPI("inventory_value", "Inventory value (at cost)", "Inventory", "inr", None),
    KPI("low_stock_skus", "Low-stock SKUs", "Inventory", "count", "down"),
    KPI("stockout_skus", "Stocked-out SKUs", "Inventory", "count", "down"),
    KPI("min_days_of_cover", "Lowest days of cover", "Inventory", "days", "up"),
]
KPI_BY_KEY = {k.key: k for k in KPI_DEFS}
GROUP_ORDER = ["Revenue", "Profitability", "Payments", "Fulfilment", "Customers", "Marketing", "Inventory"]


def _bounds(d: date) -> Tuple[datetime, datetime]:
    start = datetime(d.year, d.month, d.day)
    return start, start + timedelta(days=1)


def _pct(n: float, d: float) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _order_lookup(s: Session, refs: Set[str]) -> Dict[str, Order]:
    """Shipments reference orders by Shopify id or by order name (#1001)."""
    if not refs:
        return {}
    out: Dict[str, Order] = {}
    rows = s.execute(select(Order).where(
        Order.external_id.in_(refs) | Order.name.in_(refs) | Order.name.in_(["#" + r for r in refs]))).scalars()
    for o in rows:
        out[o.external_id] = o
        out[o.name] = o
        out[o.name.lstrip("#")] = o
    return out


def compute_day(s: Session, d: date, snapshot: bool = False) -> Dict[str, float]:
    st, en = _bounds(d)
    m: Dict[str, float] = {}

    # ---- orders / revenue ----
    orders = s.execute(select(Order).where(Order.created_at >= st, Order.created_at < en,
                                           Order.status != "cancelled")).scalars().all()
    n = len(orders)
    gross = sum(o.gross for o in orders)
    disc = sum(o.discount for o in orders)
    refunds = sum(o.refund_amount for o in orders)
    net = gross - disc - refunds
    m.update(gross_revenue=gross, discounts=disc, refunds=refunds, net_revenue=net, orders=n,
             units_sold=sum(o.units for o in orders),
             aov=(gross - disc) / n if n else 0.0,
             refund_rate=_pct(refunds, gross - disc),
             cod_share=_pct(sum(1 for o in orders if o.payment_method == "cod"), n))
    cogs = sum(o.cogs for o in orders)

    # ---- payments ----
    pays = s.execute(select(Payment).where(Payment.created_at >= st, Payment.created_at < en,
                                           Payment.status.in_(["captured", "failed"]))).scalars().all()
    ok = [p for p in pays if p.status == "captured"]
    bad = [p for p in pays if p.status == "failed"]
    m.update(payment_success_rate=_pct(len(ok), len(pays)) if pays else 100.0,
             payment_failures=len(bad), failed_payment_value=sum(p.amount for p in bad))
    fees = sum(p.fee for p in ok)

    # ---- fulfilment ----
    created = s.execute(select(Shipment).where(Shipment.created_at >= st, Shipment.created_at < en)).scalars().all()
    shipped = s.execute(select(Shipment).where(Shipment.shipped_at >= st, Shipment.shipped_at < en)).scalars().all()
    delivered = s.execute(select(Shipment).where(Shipment.delivered_at >= st, Shipment.delivered_at < en)).scalars().all()
    rtos = s.execute(select(func.count()).select_from(Shipment).where(Shipment.rto_at >= st, Shipment.rto_at < en)).scalar() or 0
    # RTO rate on the day the outcome is known (delivered vs returned), so recent days aren't understated
    m.update(shipped_orders=len(shipped), delivered_orders=len(delivered),
             rto_rate=_pct(rtos, rtos + len(delivered)))
    dd = [(x.delivered_at - x.shipped_at).total_seconds() / 86400 for x in delivered if x.shipped_at]
    if dd:
        m["avg_delivery_days"] = round(sum(dd) / len(dd), 2)
    lookup = _order_lookup(s, {x.order_ref for x in shipped if x.order_ref})
    sla = [(x.shipped_at - lookup[x.order_ref].created_at) <= timedelta(hours=48)
           for x in shipped if x.order_ref in lookup]
    if sla:
        m["on_time_dispatch_pct"] = _pct(sum(sla), len(sla))
    # orders placed up to end of day that had not shipped by end of day
    open_orders = s.execute(select(Order).where(Order.created_at < en, Order.status != "cancelled",
                                                Order.created_at >= en - timedelta(days=30))).scalars().all()
    ship_by_ref: Dict[str, Optional[datetime]] = {}
    for x in s.execute(select(Shipment).where(Shipment.created_at < en)).scalars():
        if x.order_ref:
            ship_by_ref[x.order_ref] = x.shipped_at
    pending = 0
    for o in open_orders:
        sh = ship_by_ref.get(o.external_id, ship_by_ref.get(o.name.lstrip("#")))
        if sh is None or sh >= en:
            pending += 1
    m["pending_shipments"] = pending
    ship_cost = sum(x.cost for x in created)

    # ---- customers ----
    cust_ids = {o.customer_id for o in orders if o.customer_id}
    first = {c.id: c.first_order_at for c in s.execute(select(Customer).where(Customer.id.in_(cust_ids))).scalars()} if cust_ids else {}
    new_c = {c for c in cust_ids if first.get(c) and st <= first[c] < en}
    ret_c = {c for c in cust_ids if first.get(c) and first[c] < st}
    m.update(new_customers=len(new_c), returning_customers=len(ret_c),
             repeat_rate=_pct(len(ret_c), len(new_c) + len(ret_c)))
    m["active_customers_30d"] = s.execute(select(func.count(func.distinct(Order.customer_id))).where(
        Order.created_at >= en - timedelta(days=30), Order.created_at < en, Order.status != "cancelled")).scalar() or 0
    tot_rev, tot_cust = s.execute(select(func.sum(Order.gross - Order.discount - Order.refund_amount),
                                         func.count(func.distinct(Order.customer_id))).where(
        Order.created_at < en, Order.status != "cancelled")).one()
    m["avg_ltv"] = (tot_rev or 0) / tot_cust if tot_cust else 0.0

    # ---- marketing ----
    ads = s.execute(select(AdSpend).where(AdSpend.date == d)).scalars().all()
    spend = sum(a.spend for a in ads)
    imp, clk = sum(a.impressions for a in ads), sum(a.clicks for a in ads)
    m.update(ad_spend=spend, impressions=imp, clicks=clk, ctr=_pct(clk, imp),
             cpc=spend / clk if clk else 0.0,
             cac=spend / len(new_c) if new_c else 0.0,
             blended_roas=round(net / spend, 2) if spend else 0.0)

    # ---- profitability ----
    profit = net - cogs - ship_cost - fees - spend
    m.update(cogs=cogs, shipping_cost=ship_cost, payment_fees=fees,
             gross_margin_pct=_pct(net - cogs, net), net_profit=profit, net_margin_pct=_pct(profit, net))

    # ---- inventory snapshot ----
    if snapshot:
        items = s.execute(select(InventoryItem)).scalars().all()
        vel: Dict[str, float] = defaultdict(float)
        for o in s.execute(select(Order).where(Order.created_at >= en - timedelta(days=14), Order.created_at < en,
                                               Order.status != "cancelled")).scalars():
            for li in o.line_items or []:
                vel[li.get("sku")] += float(li.get("qty") or 0) / 14.0
        covers = [i.stock / vel[i.sku] for i in items if vel.get(i.sku)]
        m.update(inventory_value=sum(i.stock * i.unit_cost for i in items),
                 low_stock_skus=sum(1 for i in items if 0 < i.stock <= i.reorder_level),
                 stockout_skus=sum(1 for i in items if i.stock <= 0))
        if covers:
            m["min_days_of_cover"] = round(min(covers), 1)
    return {k: round(float(v), 4) for k, v in m.items()}


def store_day(s: Session, d: date, snapshot: bool = False) -> Dict[str, float]:
    values = compute_day(s, d, snapshot)
    s.execute(delete(Metric).where(Metric.date == d, Metric.key.in_(list(values.keys()))))
    s.add_all(Metric(date=d, key=k, value=v) for k, v in values.items())
    s.flush()
    return values


def compute_range(s: Session, start: date, end: date) -> int:
    d, count = start, 0
    while d <= end:
        store_day(s, d, snapshot=(d == end))
        d += timedelta(days=1)
        count += 1
    return count


# ---------- cohorts ----------
def _month_index(dt: datetime) -> int:
    return dt.year * 12 + dt.month - 1


def compute_cohorts(s: Session, max_offset: int = 6) -> int:
    """Monthly acquisition cohorts. retained(offset k) = cohort customers who ordered in month cohort+k."""
    rows = s.execute(select(Order.customer_id, Order.created_at,
                            Order.gross - Order.discount - Order.refund_amount)
                     .where(Order.status != "cancelled", Order.customer_id.is_not(None))).all()
    first: Dict[int, int] = {}
    for cid, ts, _ in rows:
        mi = _month_index(ts)
        if cid not in first or mi < first[cid]:
            first[cid] = mi
    active: Dict[Tuple[int, int], Set[int]] = defaultdict(set)   # (cohort, offset) -> customers
    revenue: Dict[Tuple[int, int], float] = defaultdict(float)
    for cid, ts, rev in rows:
        off = _month_index(ts) - first[cid]
        if 0 <= off <= max_offset:
            active[(first[cid], off)].add(cid)
            revenue[(first[cid], off)] += float(rev or 0)
    sizes: Dict[int, int] = defaultdict(int)
    for c, mi in first.items():
        sizes[mi] += 1
    now_mi = _month_index(datetime.now())
    latest_mi = max([now_mi] + list(sizes.keys())) if sizes else now_mi
    s.execute(delete(CohortCell))
    for cohort, size in sizes.items():
        for off in range(0, max_offset + 1):
            if cohort + off > latest_mi:
                break
            s.add(CohortCell(cohort_month=f"{cohort // 12:04d}-{cohort % 12 + 1:02d}", offset=off, size=size,
                             retained=len(active.get((cohort, off), ())), revenue=revenue.get((cohort, off), 0.0)))
    s.flush()
    return len(sizes)


# ---------- read helpers ----------
def series(s: Session, key: str, start: date, end: date) -> List[Tuple[date, float]]:
    rows = s.execute(select(Metric.date, Metric.value).where(Metric.key == key, Metric.date >= start,
                                                             Metric.date <= end).order_by(Metric.date)).all()
    return [(r[0], r[1]) for r in rows]


def latest_date(s: Session) -> Optional[date]:
    return s.execute(select(func.max(Metric.date))).scalar()


def day_values(s: Session, d: date) -> Dict[str, float]:
    return {k: v for k, v in s.execute(select(Metric.key, Metric.value).where(Metric.date == d)).all()}
