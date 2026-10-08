"""Database engine/session handling. SQLite by default, PostgreSQL in production."""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_engine: Optional[Engine] = None
_Session: Optional[sessionmaker] = None


def _normalize_url(url: str) -> str:
    # Heroku/Supabase style URLs -> SQLAlchemy driver URL
    if url.startswith("postgres://"):
        return "postgresql+psycopg2://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def configure(url: Optional[str] = None) -> Engine:
    """(Re)build the engine. Called once at startup; tests call it with a temp DB."""
    global _engine, _Session
    url = _normalize_url(url or settings().database_url)
    kwargs = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            from sqlalchemy.pool import StaticPool
            kwargs["poolclass"] = StaticPool
        else:
            path = url.replace("sqlite:///", "", 1)
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    elif os.environ.get("VERCEL"):
        # serverless: don't hold connections across invocations (use the provider's pooled URL)
        from sqlalchemy.pool import NullPool
        kwargs["poolclass"] = NullPool
    if not url.startswith("sqlite"):
        kwargs["pool_pre_ping"] = True
    _engine = create_engine(url, future=True, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def engine() -> Engine:
    return _engine or configure()


def init_db() -> None:
    from .models import Base
    Base.metadata.create_all(engine())


def get_session() -> Session:
    engine()
    assert _Session is not None
    return _Session()


@contextmanager
def session_scope() -> Iterator[Session]:
    s = get_session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
