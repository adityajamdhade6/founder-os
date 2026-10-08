"""Statistical anomaly detection over the metrics table (workflow #4).
A metric is flagged when today's value is >=2 sigma from its trailing 14-day mean AND the
move is material (>=10% relative, or >=3 points for percentage metrics)."""
from __future__ import annotations

import statistics
from datetime import date, timedelta
from typing import Any, Dict, List

from sqlalchemy.orm import Session

from .kpis import KPI_DEFS, day_values, series
from .models import InventoryItem
from sqlalchemy import select

MIN_HISTORY = 7
WINDOW = 14


def detect(s: Session, d: date) -> List[Dict[str, Any]]:
    today = day_values(s, d)
    found: List[Dict[str, Any]] = []
    for k in KPI_DEFS:
        if not k.watch or k.key not in today:
            continue
        hist = [v for _, v in series(s, k.key, d - timedelta(days=WINDOW), d - timedelta(days=1))]
        if len(hist) < MIN_HISTORY:
            continue
        mean = statistics.fmean(hist)
        sd = statistics.pstdev(hist)
        x = today[k.key]
        if sd == 0:
            continue
        z = (x - mean) / sd
        rel = (x - mean) / mean if mean else 0.0
        material = abs(x - mean) >= 3 if k.unit == "pct" else abs(rel) >= 0.10
        if abs(z) < 2 or not material:
            continue
        adverse = (k.good == "up" and x < mean) or (k.good == "down" and x > mean)
        favourable = (k.good == "up" and x > mean) or (k.good == "down" and x < mean)
        severity = "info"
        if adverse:
            severity = "critical" if abs(z) >= 3 else "warning"
        elif favourable:
            severity = "good"
        found.append({"metric_key": k.key, "label": k.label, "unit": k.unit, "value": round(x, 2),
                      "baseline": round(mean, 2), "z": round(z, 2), "change_pct": round(rel * 100, 1),
                      "delta": round(x - mean, 2), "severity": severity})
    # inventory rules
    for it in s.execute(select(InventoryItem)).scalars():
        if it.stock <= 0:
            found.append({"metric_key": "stockout_skus", "label": f"Stock-out: {it.name}", "unit": "count",
                          "value": 0, "baseline": it.reorder_level, "z": 0, "change_pct": -100.0,
                          "delta": -it.reorder_level, "severity": "critical", "sku": it.sku})
        elif it.stock <= it.reorder_level:
            found.append({"metric_key": "low_stock_skus", "label": f"Low stock: {it.name}", "unit": "count",
                          "value": it.stock, "baseline": it.reorder_level, "z": 0,
                          "change_pct": round(100 * (it.stock - it.reorder_level) / max(it.reorder_level, 1), 1),
                          "delta": it.stock - it.reorder_level, "severity": "warning", "sku": it.sku})
    order = {"critical": 0, "warning": 1, "info": 2, "good": 3}
    found.sort(key=lambda a: (order[a["severity"]], -abs(a["z"])))
    return found
