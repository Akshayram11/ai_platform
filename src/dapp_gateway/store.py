"""Gateway storage: hashed per-app keys and one row per LLM call. SQLite locally, Postgres in DataOS."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    case,
    create_engine,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

KEY_PREFIX = "dapp_"


def now() -> datetime:
    return datetime.now(UTC)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Base(DeclarativeBase):
    pass


class AppKey(Base):
    __tablename__ = "app_keys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    prefix: Mapped[str] = mapped_column(String(16))
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    app: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class Usage(Base):
    __tablename__ = "usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    app: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    # who was using the app: the signed-in DataOS user the app forwarded in x-dapp-user ("" = not attributed)
    end_user: Mapped[str] = mapped_column(String(128), default="", index=True)
    provider: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128), default="")
    path: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[int] = mapped_column(Integer, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


GROUPABLE = ("app", "tenant", "owner", "model", "provider", "day", "end_user")


class Store:
    def __init__(self, url: str | None = None):
        url = url or os.environ.get("DATABASE_URL", "sqlite:///./dapp-gateway.db")
        kw = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {"pool_pre_ping": True}
        self.engine = create_engine(url, **kw)
        Base.metadata.create_all(self.engine)
        self._migrate()

    def _migrate(self) -> None:
        """create_all never alters an existing table: add columns that newer code expects."""
        cols = {c["name"] for c in inspect(self.engine).get_columns("usage")}
        if "end_user" not in cols:
            with self.engine.begin() as conn:
                conn.execute(text("ALTER TABLE usage ADD COLUMN end_user VARCHAR(128) DEFAULT ''"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_usage_end_user ON usage (end_user)"))

    # ---- keys
    def issue_key(self, *, tenant: str, app: str, owner: str, rotate: bool) -> str:
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        with Session(self.engine) as s, s.begin():
            if rotate:
                s.execute(update(AppKey).where(AppKey.tenant == tenant, AppKey.app == app).values(revoked=True))
            s.add(AppKey(key_hash=hash_key(key), prefix=key[:12], tenant=tenant, app=app, owner=owner))
        return key

    def key_owner(self, tenant: str, app: str) -> str | None:
        with Session(self.engine) as s:
            row = s.execute(
                select(AppKey.owner).where(AppKey.tenant == tenant, AppKey.app == app, AppKey.revoked.is_(False))
                .order_by(AppKey.created.desc()).limit(1)
            ).first()
            return row[0] if row else None

    def revoke(self, tenant: str, app: str) -> int:
        with Session(self.engine) as s, s.begin():
            r = s.execute(update(AppKey).where(AppKey.tenant == tenant, AppKey.app == app,
                                               AppKey.revoked.is_(False)).values(revoked=True))
            return r.rowcount or 0

    def revoke_prefix(self, tenant: str, app: str, prefix: str) -> int:
        """Revoke one key, found by the prefix the dashboard shows. 0 if there is no such active key."""
        with Session(self.engine) as s, s.begin():
            r = s.execute(update(AppKey).where(AppKey.tenant == tenant, AppKey.app == app, AppKey.prefix == prefix,
                                               AppKey.revoked.is_(False)).values(revoked=True))
            return r.rowcount or 0

    def lookup(self, key: str) -> AppKey | None:
        if not key.startswith(KEY_PREFIX):
            return None
        with Session(self.engine) as s:
            k = s.execute(select(AppKey).where(AppKey.key_hash == hash_key(key))).scalar_one_or_none()
            if k is None or k.revoked:
                return None
            s.expunge(k)
            return k

    def list_keys(self, owner: str | None) -> list[dict]:
        with Session(self.engine) as s:
            q = select(AppKey).order_by(AppKey.created.desc())
            if owner:
                q = q.where(AppKey.owner == owner)
            return [{"prefix": k.prefix, "tenant": k.tenant, "app": k.app, "owner": k.owner,
                     "created": k.created.isoformat(), "revoked": k.revoked} for k in s.execute(q).scalars()]

    # ---- usage
    def record(self, **row) -> None:
        with Session(self.engine) as s, s.begin():
            s.add(Usage(**row))

    def summary(self, *, owner: str | None, app: str | None, tenant: str | None, since_hours: int,
                group_by: list[str]) -> list[dict]:
        group_by = [g for g in group_by if g in GROUPABLE] or ["app"]
        cols = []
        for g in group_by:
            if g == "day":
                cols.append(func.date(Usage.ts).label("day"))
            else:
                cols.append(getattr(Usage, g).label(g))
        q = select(
            *cols,
            func.count().label("requests"),
            func.sum(Usage.input_tokens).label("input_tokens"),
            func.sum(Usage.output_tokens).label("output_tokens"),
            func.sum(Usage.cache_read_tokens).label("cache_read_tokens"),
            func.sum(Usage.cache_write_tokens).label("cache_write_tokens"),
            func.sum(case((Usage.status >= 400, 1), else_=0)).label("errors"),
        ).where(Usage.ts >= now() - timedelta(hours=since_hours))
        if owner:
            q = q.where(Usage.owner == owner)
        if app:
            q = q.where(Usage.app == app)
        if tenant:
            q = q.where(Usage.tenant == tenant)
        q = q.group_by(*cols).order_by(*cols)
        with Session(self.engine) as s:
            rows = [dict(r._mapping) for r in s.execute(q)]
        for r in rows:
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "errors"):
                r[k] = int(r[k] or 0)
            if "day" in r:
                r["day"] = str(r["day"])
        return rows


def _is_model(model: str, name: str) -> bool:
    """`gpt-6-luna` matches `gpt-6-luna` and `gpt-6-luna-2026-09-22`, but not `gpt-6-luna-pro` or `gpt-4o-mini`
    for `gpt-4o`: an unknown variant shows no cost rather than the wrong one."""
    if model == name:
        return True
    rest = model[len(name) + 1:] if model.startswith(name + "-") else ""
    return bool(rest) and rest.replace("-", "").isdigit()


class Pricing:
    """USD per million tokens, matched by exact model name or name plus a date suffix (see _is_model). Configure via DAPP_PRICING (JSON):
    {"claude-sonnet": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}, ...}
    No built-in prices: they change, so cost is only shown when you configure them.
    """

    def __init__(self, raw: str | None = None):
        raw = raw if raw is not None else os.environ.get("DAPP_PRICING", "")
        self.table: dict[str, dict[str, float]] = json.loads(raw) if raw.strip() else {}

    def cost(self, row: dict) -> float | None:
        model = row.get("model") or ""
        match = max((p for p in self.table if _is_model(model, p)), key=len, default=None)
        if match is None:
            return None
        p = self.table[match]
        return round(sum(row.get(f"{k}_tokens", 0) * p.get(k, 0) for k in
                         ("input", "output", "cache_read", "cache_write")) / 1e6, 6)
