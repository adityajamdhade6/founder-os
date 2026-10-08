"""Environment-driven settings. Read at call time so tests can override env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass
class Settings:
    database_url: str
    timezone: str
    brand: str
    currency: str
    shopify_secret: str
    razorpay_secret: str
    shiprocket_token: str
    admin_key: str
    allow_unsigned: bool
    openai_api_key: str
    openai_model: str
    slack_bot_token: str
    slack_channel_id: str
    slack_webhook_url: str
    meta_access_token: str
    meta_ad_account_id: str
    default_cogs_ratio: float
    payment_fee_pct: float
    reports_dir: Path


def settings() -> Settings:
    e = os.environ.get
    return Settings(
        database_url=e("DATABASE_URL", f"sqlite:///{ROOT / 'data' / 'founderos.db'}"),
        timezone=e("BUSINESS_TZ", "Asia/Kolkata"),
        brand=e("BRAND_NAME", "INHAUS Coffee"),
        currency=e("CURRENCY", "INR"),
        shopify_secret=e("SHOPIFY_WEBHOOK_SECRET", ""),
        razorpay_secret=e("RAZORPAY_WEBHOOK_SECRET", ""),
        shiprocket_token=e("SHIPROCKET_WEBHOOK_TOKEN", ""),
        admin_key=e("ADMIN_API_KEY", ""),
        allow_unsigned=e("ALLOW_UNSIGNED_WEBHOOKS", "0") == "1",
        openai_api_key=e("OPENAI_API_KEY", ""),
        openai_model=e("OPENAI_MODEL", "gpt-4o-mini"),
        slack_bot_token=e("SLACK_BOT_TOKEN", ""),
        slack_channel_id=e("SLACK_CHANNEL_ID", ""),
        slack_webhook_url=e("SLACK_WEBHOOK_URL", ""),
        meta_access_token=e("META_ACCESS_TOKEN", ""),
        meta_ad_account_id=e("META_AD_ACCOUNT_ID", ""),
        default_cogs_ratio=float(e("DEFAULT_COGS_RATIO", "0.42")),
        payment_fee_pct=float(e("PAYMENT_FEE_PCT", "2.0")),
        reports_dir=Path(e("REPORTS_DIR", str(ROOT / "reports"))),
    )
