# FounderOS — Business Automation Hub

An AI-powered operating system for **INHAUS Coffee** that replaces manual reporting: it captures events from
Shopify, Razorpay, Shiprocket and Meta Ads in real time, normalises them into one database, computes 40 daily KPIs
plus cohort retention, detects anomalies, and has an LLM explain what changed — then ships a PDF to Slack.

```
Shopify ─┐                                   ┌─ Operations Dashboard
Razorpay ─┼─ webhooks ─▶ raw_events ─▶ unified schema ─▶ KPI engine ─▶ anomalies ─▶ AI insights ─┤
Shiprocket┤   (HMAC-verified,   (idempotent)   (SQLite/Postgres)                                  ├─ AI Insights Console
Meta/CSV ─┘    deduplicated)                                                                      └─ PDF ─▶ Slack
```

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m founderos seed      # 90 days of demo data, replayed through the real webhook pipeline
.venv/bin/python -m founderos serve     # http://127.0.0.1:8000
```

`seed` wipes the database. The last day of demo data deliberately contains problems (payment-failure spike,
repeat-order drop, coupon + ad-spend margin squeeze, stock-outs) so you can see the anomaly and AI workflows react.

## The six workflows (`python -m founderos workflows`)

| # | Workflow | What it does |
|---|----------|--------------|
| 1 | `ingest_sync` | Replays failed webhook events, pulls Meta ad spend, reconciles sources |
| 2 | `kpi_engine` | Computes 40 KPIs/day: revenue, margins, payments, fulfilment, customers, CAC/ROAS, inventory |
| 3 | `cohort_analysis` | Monthly acquisition cohorts, M0–M6 retention, cohort LTV |
| 4 | `anomaly_detection` | Flags metrics ≥2σ from their 14-day mean *and* materially moved (≥10%, or ≥3 pts for percentages) |
| 5 | `ai_insights` | OpenAI writes the summary + one action per issue; rules-based fallback with no key |
| 6 | `report_broadcast` | Builds the PDF and uploads it to Slack |

Schedule the pipeline (report covers the last complete day):

```
0 8 * * *  cd /path/to/founder\ os && .venv/bin/python -m founderos run-daily
```

## Connecting real sources

| Source | Endpoint | Auth |
|--------|----------|------|
| Shopify (`orders/create`, `orders/updated`, `orders/cancelled`, `refunds/create`) | `POST /webhooks/shopify` | `X-Shopify-Hmac-Sha256` with `SHOPIFY_WEBHOOK_SECRET` |
| Razorpay (`payment.captured`, `payment.failed`) | `POST /webhooks/razorpay` | `X-Razorpay-Signature` with `RAZORPAY_WEBHOOK_SECRET` |
| Shiprocket status updates | `POST /webhooks/shiprocket` | `x-api-key` = `SHIPROCKET_WEBHOOK_TOKEN` |
| Meta Ads | pulled if `META_ACCESS_TOKEN` + `META_AD_ACCOUNT_ID` set, or `POST /ingest/meta-ads` | `X-Admin-Key` |
| Inventory sheet | `python -m founderos import-inventory stock.csv` (`sku,name,stock,unit_cost,price,reorder_level`) | – |

Linking payments/shipments to orders: put the Shopify order id in the Razorpay payment's `notes.shopify_order_id`,
and send the Shopify order id or name as Shiprocket's `order_id`. Webhooks are deduplicated by event id, so retries
are safe. The listener is a plain FastAPI app — deploy it anywhere that runs ASGI (Fly, Render, Railway, a VM) and
point `DATABASE_URL` at Postgres.

## Notes and definitions

- Timestamps are converted to the business timezone (`BUSINESS_TZ`) on ingest, so "a day" means an IST day.
- Refunds are attributed to the *order's* date. RTO rate is measured on the day the outcome is known
  (`RTO / (RTO + delivered)`), so recent days aren't understated. Inventory KPIs are a snapshot of the latest day.
- COGS uses per-SKU `unit_cost` from inventory, falling back to `DEFAULT_COGS_RATIO` for unknown SKUs.
- Net profit = net revenue − COGS − shipping − gateway fees − ad spend.
- The OpenAI call sends aggregated KPIs and anomalies only — no customer records leave your database.

## Tests

```bash
.venv/bin/python -m pytest -q
```
