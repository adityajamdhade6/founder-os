"""AI Insights engine (workflow #5). Sends structured metrics + detected anomalies to OpenAI and
stores the narrative. Without an API key (or on any API error) it falls back to a deterministic
rules-based narrative so the pipeline never blocks."""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

import requests
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from . import anomalies as anom
from .config import settings
from .kpis import KPI_BY_KEY, day_values, series
from .models import CohortCell, Insight

ACTIONS = {
    "payment_success_rate": "Check Razorpay for bank/UPI downtime and error-code mix; enable UPI intent and card-retry fallbacks.",
    "payment_failures": "Review failed payment error codes in Razorpay; contact support for any gateway-side incident.",
    "repeat_rate": "Trigger a win-back flow (email/WhatsApp) to customers whose last order was 25-45 days ago; check that reorder reminders are sending.",
    "returning_customers": "Verify retention flows (subscription reminders, post-purchase emails) fired today.",
    "new_customers": "Audit ad creatives and landing-page conversion; compare with last week's best-performing campaign.",
    "net_margin_pct": "Inspect discount codes and ad spend for the day; pause any coupon stacking or over-aggressive campaign.",
    "gross_margin_pct": "Look at discount depth and product mix; an unusually large coupon or COGS change is the usual culprit.",
    "net_profit": "Break profit down by discounts, shipping and ad spend to find which line moved.",
    "discounts": "Review active discount codes for leakage.",
    "cac": "Shift budget from the campaign with the worst cost per new customer; refresh creatives showing fatigue.",
    "blended_roas": "Trim spend on low-ROAS ad sets until conversion recovers.",
    "ad_spend": "Confirm the spend increase was intentional (budget caps, auto-scaling rules).",
    "ctr": "Creative fatigue is likely; rotate in new creatives and check audience overlap.",
    "rto_rate": "Call-confirm COD orders (or nudge to prepaid) in pin codes with high RTO; review courier performance.",
    "cod_share": "Offer a small prepaid discount at checkout to nudge COD buyers to pay online.",
    "avg_delivery_days": "Escalate with the courier partner; consider switching lanes with the worst delays.",
    "pending_shipments": "Clear the dispatch backlog; check pickup scheduling with the courier.",
    "on_time_dispatch_pct": "Review warehouse capacity and pickup cut-off times.",
    "refund_rate": "Read the latest refund reasons; look for a damaged batch or courier issue.",
    "orders": "Compare traffic and conversion rate to isolate whether the change is demand- or funnel-driven.",
    "net_revenue": "Break revenue down by channel and product to locate the change.",
    "aov": "Check bundle/upsell placement and free-shipping threshold.",
    "stockout_skus": "Raise a purchase order immediately and hide/pause ads for the stocked-out SKU.",
    "low_stock_skus": "Reorder now; lead time may exceed remaining cover.",
}


# ---------- formatting ----------
def fmt(unit: str, v: float) -> str:
    if unit == "inr":
        return f"₹{v:,.0f}"
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    if unit == "days":
        return f"{v:.1f}d"
    return f"{v:,.0f}"


def _headline(s: Session, d: date) -> List[Dict[str, Any]]:
    today = day_values(s, d)
    out = []
    for key in ["net_revenue", "orders", "aov", "net_profit", "net_margin_pct", "payment_success_rate", "repeat_rate",
                "cac", "blended_roas", "rto_rate", "pending_shipments"]:
        if key not in today:
            continue
        prev = [v for _, v in series(s, key, d - timedelta(days=7), d - timedelta(days=1))]
        base = sum(prev) / len(prev) if prev else None
        out.append({"key": key, "label": KPI_BY_KEY[key].label, "unit": KPI_BY_KEY[key].unit,
                    "value": today[key], "prev_7d_avg": round(base, 2) if base is not None else None})
    return out


def _cohort_summary(s: Session) -> List[Dict[str, Any]]:
    rows = s.execute(select(CohortCell)).scalars().all()
    by: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        c = by.setdefault(r.cohort_month, {"cohort": r.cohort_month, "size": r.size, "revenue": 0.0, "m1": None})
        c["revenue"] += r.revenue
        if r.offset == 1 and r.size:
            c["m1"] = round(100 * r.retained / r.size, 1)
    out = []
    for c in by.values():
        c["ltv"] = round(c["revenue"] / c["size"], 0) if c["size"] else 0
        out.append(c)
    return sorted(out, key=lambda c: c["cohort"])


def build_context(s: Session, d: date) -> Dict[str, Any]:
    anoms = anom.detect(s, d)
    return {"date": d.isoformat(), "brand": settings().brand, "currency": settings().currency,
            "headline_kpis": _headline(s, d), "anomalies": anoms, "cohorts": _cohort_summary(s)}


# ---------- OpenAI ----------
SYSTEM = (
    "You are FounderOS, the operating analyst for {brand}, an early-stage direct-to-consumer brand in India "
    "(amounts in INR). You receive computed KPIs and statistically detected anomalies. Write for a busy founder: "
    "plain language, specific numbers, causes ranked by likelihood, and one concrete action per issue. "
    "NEVER invent numbers that are not in the data. Reply with JSON only: "
    '{{"summary": "<3-4 sentence executive summary>", "insights": [{{"title": "...", "body": "...", '
    '"severity": "critical|warning|info|good", "metric_key": "<kpi key or null>", "action": "..."}}]}}. '
    "Return at most 6 insights, merging anomalies that share a root cause."
)


def _openai_json(system: str, user: str) -> Optional[Dict[str, Any]]:
    cfg = settings()
    if not cfg.openai_api_key:
        return None
    r = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {cfg.openai_api_key}"},
        json={"model": cfg.openai_model, "temperature": 0.2, "response_format": {"type": "json_object"},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
        timeout=60)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])


# ---------- rules-based fallback ----------
def _rules_narrative(ctx: Dict[str, Any]) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    # metrics that are just a symptom of another flagged metric: show the cause only
    present = {a["metric_key"] for a in ctx["anomalies"]}
    symptom = {"payment_failures": "payment_success_rate", "net_profit": "net_margin_pct",
               "gross_margin_pct": "net_margin_pct", "returning_customers": "repeat_rate"}
    anoms = [a for a in ctx["anomalies"] if symptom.get(a["metric_key"]) not in present]
    for a in anoms[:8]:
        unit = a["unit"]
        if a["label"].startswith(("Stock-out", "Low stock")):
            body = (f"{a['label'].split(': ', 1)[1]} has {a['value']:.0f} units left "
                    f"(reorder level {a['baseline']:.0f}).")
            title = a["label"]
        else:
            word = "dropped" if a["delta"] < 0 else "rose"
            if unit == "pct":
                title = f"{a['label']} {word} {abs(a['delta']):.0f} pts to {fmt(unit, a['value'])}"
            elif abs(a["change_pct"]) >= 200:
                title = f"{a['label']} {'spiked' if a['delta'] > 0 else 'collapsed'} to {fmt(unit, a['value'])} (usually {fmt(unit, a['baseline'])})"
            else:
                title = f"{a['label']} {word} {abs(a['change_pct']):.0f}%"
            body = (f"{a['label']} was {fmt(unit, a['value'])} versus a 14-day average of "
                    f"{fmt(unit, a['baseline'])} ({a['z']:+.1f} standard deviations).")
        items.append({"title": title, "body": body, "severity": a["severity"], "metric_key": a["metric_key"],
                      "action": ACTIONS.get(a["metric_key"])})
    bad = [i for i in items if i["severity"] in ("critical", "warning")]
    k = {x["key"]: x for x in ctx["headline_kpis"]}
    parts = []
    if "net_revenue" in k:
        parts.append(f"Net revenue was {fmt('inr', k['net_revenue']['value'])} across "
                     f"{fmt('count', k.get('orders', {}).get('value', 0))} orders")
        if "net_margin_pct" in k:
            parts[-1] += f" at a {fmt('pct', k['net_margin_pct']['value'])} net margin."
    parts.append(f"{len(bad)} issue(s) need attention: " + "; ".join(i["title"] for i in bad[:3]) + "."
                 if bad else "No adverse anomalies were detected.")
    return {"summary": " ".join(parts), "insights": items}


def generate(s: Session, d: date) -> Dict[str, Any]:
    ctx = build_context(s, d)
    origin, error = "rules", None
    result: Optional[Dict[str, Any]] = None
    try:
        result = _openai_json(SYSTEM.format(brand=ctx["brand"]), json.dumps(ctx, default=str))
        if result:
            origin = "ai"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    if not result or "insights" not in result:
        result = _rules_narrative(ctx)
        origin = "rules"

    s.execute(delete(Insight).where(Insight.date == d))
    s.add(Insight(date=d, kind="summary", severity="info", title="Daily summary", body=str(result.get("summary", "")),
                  origin=origin, data={"anomaly_count": len(ctx["anomalies"]), "llm_error": error}))
    for it in result["insights"][:8]:
        sev = it.get("severity") if it.get("severity") in ("critical", "warning", "info", "good") else "info"
        s.add(Insight(date=d, kind="anomaly", severity=sev, title=str(it.get("title", ""))[:250],
                      body=str(it.get("body", "")), action=it.get("action"), metric_key=it.get("metric_key"),
                      origin=origin))
    s.flush()
    return {"origin": origin, "summary": result.get("summary"), "insights": len(result["insights"]),
            "anomalies": len(ctx["anomalies"]), "llm_error": error}


# ---------- Q&A console ----------
def ask(s: Session, question: str, d: date) -> Dict[str, Any]:
    ctx = build_context(s, d)
    ctx["trend_14d"] = {k: [round(v, 2) for _, v in series(s, k, d - timedelta(days=13), d)]
                        for k in ("net_revenue", "orders", "net_margin_pct", "repeat_rate", "cac", "payment_success_rate")}
    try:
        res = _openai_json(
            SYSTEM.format(brand=ctx["brand"]).split("Reply with JSON")[0]
            + 'Answer the founder\'s question using only the data provided. Reply with JSON: {"answer": "..."}',
            json.dumps({"question": question, "data": ctx}, default=str))
        if res and res.get("answer"):
            return {"answer": res["answer"], "origin": "ai"}
    except Exception:
        pass
    return {"answer": _rules_answer(ctx, question), "origin": "rules"}


def _rules_answer(ctx: Dict[str, Any], q: str) -> str:
    ql = q.lower()
    if "cohort" in ql or "ltv" in ql or "lifetime" in ql:
        cs = [c for c in ctx["cohorts"] if c["size"] >= 10]
        if cs:
            best = max(cs, key=lambda c: c["ltv"])
            return (f"The {best['cohort']} cohort has the highest lifetime value so far: ₹{best['ltv']:,.0f} per customer "
                    f"across {best['size']} customers" + (f", with {best['m1']}% returning in month 1." if best["m1"] is not None else "."))
    for k in ctx["headline_kpis"]:
        if k["label"].lower() in ql or k["key"].replace("_", " ") in ql:
            base = f" (7-day average {fmt(k['unit'], k['prev_7d_avg'])})" if k["prev_7d_avg"] is not None else ""
            return f"{k['label']} was {fmt(k['unit'], k['value'])} on {ctx['date']}{base}."
    if "margin" in ql or "why" in ql or "drop" in ql or "shrink" in ql:
        adverse = [a for a in ctx["anomalies"] if a["severity"] in ("critical", "warning")]
        if adverse:
            return "Likely drivers: " + "; ".join(
                f"{a['label']} {fmt(a['unit'], a['value'])} vs {fmt(a['unit'], a['baseline'])} typical" for a in adverse[:4]) + "."
    return _rules_narrative(ctx)["summary"]
