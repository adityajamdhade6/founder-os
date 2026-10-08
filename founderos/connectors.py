"""Pull-based sources: Meta Marketing API (ad spend) and inventory spreadsheets (CSV)."""
from __future__ import annotations

import csv
import json
from datetime import date, datetime
from typing import Any, Dict, Iterable, List

import requests
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import AdSpend, InventoryItem


def upsert_ad_rows(s: Session, rows: Iterable[Dict[str, Any]]) -> int:
    n = 0
    for r in rows:
        d = r["date"] if isinstance(r["date"], date) else date.fromisoformat(str(r["date"]))
        row = s.execute(select(AdSpend).where(AdSpend.date == d, AdSpend.campaign == r["campaign"])).scalar_one_or_none()
        if row is None:
            row = AdSpend(date=d, campaign=r["campaign"])
            s.add(row)
        row.spend = float(r.get("spend") or 0)
        row.impressions = int(float(r.get("impressions") or 0))
        row.clicks = int(float(r.get("clicks") or 0))
        row.purchases = int(float(r.get("purchases") or 0))
        row.purchase_value = float(r.get("purchase_value") or 0)
        n += 1
    s.flush()
    return n


def pull_meta_ads(s: Session, since: date, until: date) -> int:
    cfg = settings()
    if not (cfg.meta_access_token and cfg.meta_ad_account_id):
        return 0
    acct = cfg.meta_ad_account_id if cfg.meta_ad_account_id.startswith("act_") else f"act_{cfg.meta_ad_account_id}"
    url = f"https://graph.facebook.com/v19.0/{acct}/insights"
    params = {"access_token": cfg.meta_access_token, "level": "campaign", "time_increment": 1, "limit": 500,
              "fields": "campaign_name,spend,impressions,clicks,actions,action_values",
              "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()})}
    out: List[Dict[str, Any]] = []
    while url:
        r = requests.get(url, params=params, timeout=60)
        r.raise_for_status()
        j = r.json()
        for row in j.get("data", []):
            pick = lambda arr: next((float(a["value"]) for a in (arr or []) if a["action_type"] == "purchase"), 0.0)
            out.append({"date": row["date_start"], "campaign": row["campaign_name"], "spend": row.get("spend"),
                        "impressions": row.get("impressions"), "clicks": row.get("clicks"),
                        "purchases": pick(row.get("actions")), "purchase_value": pick(row.get("action_values"))})
        url, params = (j.get("paging") or {}).get("next"), {}
    return upsert_ad_rows(s, out)


def import_inventory_csv(s: Session, path: str) -> int:
    """CSV columns: sku,name,stock,unit_cost,price,reorder_level"""
    n = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sku = r["sku"].strip()
            it = s.get(InventoryItem, sku) or InventoryItem(sku=sku, name=r.get("name", sku))
            it.name = r.get("name", it.name)
            it.stock = int(float(r.get("stock") or 0))
            it.unit_cost = float(r.get("unit_cost") or 0)
            it.price = float(r.get("price") or 0)
            it.reorder_level = int(float(r.get("reorder_level") or 0))
            it.updated_at = datetime.utcnow()
            s.add(it)
            n += 1
    s.flush()
    return n
