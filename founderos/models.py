"""Unified schema. Every source system (Shopify, Razorpay, Shiprocket, Meta, sheets)
is normalised into these tables so KPIs can be computed with plain SQL/Python."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RawEvent(Base):
    """Every webhook, stored verbatim: audit trail + idempotency + replay."""
    __tablename__ = "raw_events"
    __table_args__ = (UniqueConstraint("source", "event_id", name="uq_raw_source_event"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[str] = mapped_column(String(128))
    topic: Mapped[str] = mapped_column(String(64))
    payload: Mapped[Dict[str, Any]] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    first_order_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(32), default="")
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)  # business-local, naive
    gross: Mapped[float] = mapped_column(Float, default=0)      # line items before discount
    discount: Mapped[float] = mapped_column(Float, default=0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0)
    units: Mapped[int] = mapped_column(Integer, default=0)
    cogs: Mapped[float] = mapped_column(Float, default=0)
    payment_method: Mapped[str] = mapped_column(String(16), default="prepaid")  # prepaid | cod
    status: Mapped[str] = mapped_column(String(16), default="open")            # open | cancelled
    line_items: Mapped[List[Any]] = mapped_column(JSON, default=list)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)
    order_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    amount: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(16))  # captured | failed | refunded
    method: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fee: Mapped[float] = mapped_column(Float, default=0)


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    external_id: Mapped[str] = mapped_column(String(64), unique=True)  # AWB
    order_ref: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|shipped|delivered|rto|cancelled
    courier: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    shipped_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    delivered_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    rto_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True, index=True)
    cost: Mapped[float] = mapped_column(Float, default=0)


class AdSpend(Base):
    __tablename__ = "ad_spend"
    __table_args__ = (UniqueConstraint("date", "campaign", name="uq_ad_day_campaign"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    campaign: Mapped[str] = mapped_column(String(128))
    spend: Mapped[float] = mapped_column(Float, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    purchase_value: Mapped[float] = mapped_column(Float, default=0)


class InventoryItem(Base):
    __tablename__ = "inventory"
    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    stock: Mapped[int] = mapped_column(Integer, default=0)
    unit_cost: Mapped[float] = mapped_column(Float, default=0)
    price: Mapped[float] = mapped_column(Float, default=0)
    reorder_level: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Metric(Base):
    __tablename__ = "metrics"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[float] = mapped_column(Float)


class CohortCell(Base):
    __tablename__ = "cohorts"
    cohort_month: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM
    offset: Mapped[int] = mapped_column(Integer, primary_key=True)          # months since first order
    size: Mapped[int] = mapped_column(Integer)
    retained: Mapped[int] = mapped_column(Integer)
    revenue: Mapped[float] = mapped_column(Float, default=0)


class Insight(Base):
    __tablename__ = "insights"
    __table_args__ = (Index("ix_insight_date", "date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String(16))       # summary | anomaly
    severity: Mapped[str] = mapped_column(String(16))   # critical | warning | info | good
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)
    action: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    metric_key: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    data: Mapped[Dict[str, Any]] = mapped_column(JSON, default=dict)
    origin: Mapped[str] = mapped_column(String(16), default="rules")  # ai | rules
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    workflow: Mapped[str] = mapped_column(String(48), index=True)
    status: Mapped[str] = mapped_column(String(16))  # running | ok | error | skipped
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    path: Mapped[str] = mapped_column(String(512))
    slack_status: Mapped[str] = mapped_column(String(64), default="not_sent")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
