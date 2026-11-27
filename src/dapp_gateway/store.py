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
    Text,
    UniqueConstraint,
    case,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
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

APP_BUDGET, DEFAULT_USER_BUDGET = "", "*"     # Budget.user for the app's own budget, and for "every user" (the default)


class Budget(Base):
    """A monthly token budget (input + output + cached tokens, UTC calendar month) on an app: the app's ceiling (user ""),
    the default for each of its users (user "*"), or one person's own (user = their DataOS id). No user budget at all means
    a user may use the app's whole budget."""
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("tenant", "app", "user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    app: Mapped[str] = mapped_column(String(64), index=True)
    user: Mapped[str] = mapped_column(String(128), default="")
    tokens: Mapped[int] = mapped_column(Integer)
    set_by: Mapped[str] = mapped_column(String(128), default="")
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BudgetSpend(Base):
    """Running token counter per app (user "") and per app user, per month ("2026-11"). Bumped after every call, so a
    budget check reads one row instead of adding up the month."""
    __tablename__ = "budget_spend"
    __table_args__ = (UniqueConstraint("tenant", "app", "user", "month"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    app: Mapped[str] = mapped_column(String(64), index=True)
    user: Mapped[str] = mapped_column(String(128), default="")
    month: Mapped[str] = mapped_column(String(7))
    tokens: Mapped[int] = mapped_column(Integer, default=0)


def month_of(t: datetime | None = None) -> str:
    return (t or now()).strftime("%Y-%m")


def month_start(month: str) -> datetime:
    y, m = (int(x) for x in month.split("-"))
    return datetime(y, m, 1, tzinfo=UTC)


def next_month_start(month: str) -> datetime:
    y, m = (int(x) for x in month.split("-"))
    return datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=UTC)


def tokens_of(row: dict) -> int:
    """What counts against a budget: everything the model read or wrote, cached tokens included."""
    return sum(int(row.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


class Tool(Base):
    """An MCP server registered with the gateway. Callers reach it at /mcp/<tenant>/<name>; the gateway checks who
    they are, injects the tool's credential (callers never see it) and records every call."""
    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    url: Mapped[str] = mapped_column(String(512))
    description: Mapped[str] = mapped_column(String(512), default="")
    auth_header: Mapped[str] = mapped_column(String(64), default="")
    auth_secret: Mapped[str] = mapped_column(Text, default="")      # encrypted with the gateway's secret key
    allow: Mapped[str] = mapped_column(Text, default="[]")          # JSON list: user ids, "*", "app:<name>"
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    # "key": one credential for everyone (auth_header/auth_secret). "oauth": each person signs in to the MCP server
    # themselves; `oauth` holds the server's endpoints and the gateway's client registration (encrypted JSON).
    auth_mode: Mapped[str] = mapped_column(String(16), default="key")
    oauth: Mapped[str] = mapped_column(Text, default="")


class ToolToken(Base):
    """One person's sign-in to an OAuth MCP server: their access and refresh tokens, encrypted. The gateway adds the
    access token to that person's calls (or an app's calls made for them) and refreshes it; nobody else sees it."""
    __tablename__ = "tool_tokens"
    __table_args__ = (UniqueConstraint("tenant", "tool", "user"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(64), index=True)
    user: Mapped[str] = mapped_column(String(128), index=True)
    access: Mapped[str] = mapped_column(Text)
    refresh: Mapped[str] = mapped_column(Text, default="")
    expires: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[str] = mapped_column(String(512), default="")
    updated: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OAuthPending(Base):
    """A sign-in that has been started (state, PKCE verifier) and not finished yet. Lives ten minutes."""
    __tablename__ = "oauth_pending"
    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64))
    tool: Mapped[str] = mapped_column(String(64))
    user: Mapped[str] = mapped_column(String(128))
    verifier: Mapped[str] = mapped_column(Text)
    back: Mapped[str] = mapped_column(String(512), default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LocalModel(Base):
    """A model served by a DataOS app (llama.cpp) that people deployed from the dashboard. Callers ask for it by `name` on
    /openai/v1/chat/completions; the gateway sends the call to `url` with the model server's own key (encrypted here)."""
    __tablename__ = "local_models"
    __table_args__ = (UniqueConstraint("tenant", "name"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)
    app: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(String(512))
    key_secret: Mapped[str] = mapped_column(Text, default="")        # the model server's key, encrypted
    repo: Mapped[str] = mapped_column(String(256), default="")       # e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M
    description: Mapped[str] = mapped_column(String(512), default="")
    created: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ToolCall(Base):
    __tablename__ = "tool_calls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    tenant: Mapped[str] = mapped_column(String(64), index=True)
    tool: Mapped[str] = mapped_column(String(64), index=True)
    owner: Mapped[str] = mapped_column(String(128), index=True)     # the tool's owner, so they see all its calls
    caller: Mapped[str] = mapped_column(String(160), index=True)    # DataOS user id, or app:<name>
    end_user: Mapped[str] = mapped_column(String(128), default="")  # for app callers: who was using the app
    method: Mapped[str] = mapped_column(String(64), default="")     # JSON-RPC method, e.g. tools/call
    tool_name: Mapped[str] = mapped_column(String(128), default="")  # for tools/call: which function
    status: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


TOOL_GROUPABLE = ("tool", "tenant", "caller", "end_user", "method", "tool_name", "day")


def tool_view(t: Tool, *, full: bool) -> dict:
    """What callers may see of a tool. Never the credential; the allow list only for its owner/admins."""
    out = {"tenant": t.tenant, "name": t.name, "owner": t.owner, "description": t.description,
           "endpoint": f"/mcp/{t.tenant}/{t.name}", "has_auth": bool(t.auth_secret), "auth_mode": t.auth_mode or "key",
           "created": t.created.isoformat(), "updated": t.updated.isoformat()}
    if full:
        out.update(url=t.url, auth_header=t.auth_header, allow=json.loads(t.allow or "[]"))
    return out


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
        tool_cols = {c["name"] for c in inspect(self.engine).get_columns("tools")}
        with self.engine.begin() as conn:
            if "auth_mode" not in tool_cols:
                conn.execute(text("ALTER TABLE tools ADD COLUMN auth_mode VARCHAR(16) DEFAULT 'key'"))
            if "oauth" not in tool_cols:
                conn.execute(text("ALTER TABLE tools ADD COLUMN oauth TEXT DEFAULT ''"))

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
        """One LLM call, and its tokens added to the month's counters of the app and of its user."""
        used = tokens_of(row)
        with Session(self.engine) as s, s.begin():
            s.add(Usage(**row))
            if used and not str(row.get("app", "")).startswith("user:"):        # personal keys have no budgets
                month = month_of(row.get("ts"))
                for user in {"", row.get("end_user") or ""}:
                    self._bump(s, row["tenant"], row["app"], user, month, used)

    def _bump(self, s: Session, tenant: str, app: str, user: str, month: str, used: int) -> None:
        r = s.execute(update(BudgetSpend).where(BudgetSpend.tenant == tenant, BudgetSpend.app == app,
                                                BudgetSpend.user == user, BudgetSpend.month == month)
                      .values(tokens=BudgetSpend.tokens + used))
        if r.rowcount:
            return
        # first call of the month: start from what the month already used, this call included (autoflush). Two first
        # calls at once: the second insert fails on the unique key inside its savepoint, and it adds to the row instead.
        seed = self._month_usage(s, tenant, app, user, month)
        try:
            with s.begin_nested():
                s.add(BudgetSpend(tenant=tenant, app=app, user=user, month=month, tokens=seed))
        except IntegrityError:
            s.execute(update(BudgetSpend).where(BudgetSpend.tenant == tenant, BudgetSpend.app == app,
                                                BudgetSpend.user == user, BudgetSpend.month == month)
                      .values(tokens=BudgetSpend.tokens + used))

    @staticmethod
    def _month_usage(s: Session, tenant: str, app: str, user: str, month: str) -> int:
        q = select(func.coalesce(func.sum(Usage.input_tokens + Usage.output_tokens + Usage.cache_read_tokens
                                          + Usage.cache_write_tokens), 0)).where(
            Usage.tenant == tenant, Usage.app == app, Usage.ts >= month_start(month), Usage.ts < next_month_start(month))
        if user:
            q = q.where(Usage.end_user == user)
        return int(s.execute(q).scalar() or 0)

    # ---- budgets
    def spent(self, tenant: str, app: str, user: str = "", month: str | None = None) -> int:
        month = month or month_of()
        with Session(self.engine) as s:
            v = s.execute(select(BudgetSpend.tokens).where(BudgetSpend.tenant == tenant, BudgetSpend.app == app,
                                                           BudgetSpend.user == user, BudgetSpend.month == month)).scalar()
            return int(v) if v is not None else self._month_usage(s, tenant, app, user, month)

    def budgets(self, tenant: str, app: str) -> dict[str, int]:
        """{"": app budget, "*": default per user, "<user>": that user's}; missing keys are not set."""
        with Session(self.engine) as s:
            return {b.user: b.tokens for b in s.execute(select(Budget).where(Budget.tenant == tenant, Budget.app == app))
                    .scalars()}

    def set_budget(self, tenant: str, app: str, user: str, tokens: int | None, by: str) -> None:
        """Set (or, with tokens None, remove) one budget."""
        with Session(self.engine) as s, s.begin():
            if tokens is None:
                s.execute(delete(Budget).where(Budget.tenant == tenant, Budget.app == app, Budget.user == user))
                return
            r = s.execute(update(Budget).where(Budget.tenant == tenant, Budget.app == app, Budget.user == user)
                          .values(tokens=int(tokens), set_by=by, updated=now()))
            if not r.rowcount:
                s.add(Budget(tenant=tenant, app=app, user=user, tokens=int(tokens), set_by=by))

    def user_spend(self, tenant: str, app: str, month: str | None = None) -> dict[str, int]:
        """Tokens each user of the app used this month (from the counters, or the month's usage before them)."""
        month = month or month_of()
        with Session(self.engine) as s:
            rows = {r.user: r.tokens for r in s.execute(select(BudgetSpend).where(
                BudgetSpend.tenant == tenant, BudgetSpend.app == app, BudgetSpend.month == month,
                BudgetSpend.user != "")).scalars()}
            seen = s.execute(select(Usage.end_user).where(
                Usage.tenant == tenant, Usage.app == app, Usage.end_user != "", Usage.ts >= month_start(month),
                Usage.ts < next_month_start(month)).distinct()).scalars()
            for u in seen:
                if u not in rows:
                    rows[u] = self._month_usage(s, tenant, app, u, month)
            return rows

    def summary(self, *, owner: str | None, app: str | None, tenant: str | None, since_hours: int,
                group_by: list[str], model: str | None = None) -> list[dict]:
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
        if model:
            q = q.where(Usage.model == model)
        q = q.group_by(*cols).order_by(*cols)
        with Session(self.engine) as s:
            rows = [dict(r._mapping) for r in s.execute(q)]
        for r in rows:
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "errors"):
                r[k] = int(r[k] or 0)
            if "day" in r:
                r["day"] = str(r["day"])
        return rows

    # ---- tools (MCP servers)
    def get_tool(self, tenant: str, name: str) -> Tool | None:
        with Session(self.engine) as s:
            t = s.execute(select(Tool).where(Tool.tenant == tenant, Tool.name == name)).scalar_one_or_none()
            if t is not None:
                s.expunge(t)
            return t

    def save_tool(self, *, tenant: str, name: str, owner: str, url: str, description: str, auth_header: str,
                  auth_secret: str | None, allow: list[str] | None) -> Tool:
        """Create, or update an existing one. auth_secret None keeps the stored credential; "" removes it.
        allow None keeps the current list (a new tool starts with only its owner)."""
        with Session(self.engine) as s, s.begin():
            t = s.execute(select(Tool).where(Tool.tenant == tenant, Tool.name == name)).scalar_one_or_none()
            if t is None:
                t = Tool(tenant=tenant, name=name, owner=owner, allow="[]")
                s.add(t)
            t.url, t.description, t.auth_header, t.updated = url, description, auth_header, now()
            if auth_secret is not None:
                t.auth_secret = auth_secret
            if allow is not None:
                t.allow = json.dumps(sorted(set(allow)))
            s.flush()
            s.expunge(t)
            return t

    def set_tool_allow(self, tenant: str, name: str, allow: list[str]) -> list[str]:
        clean = sorted(set(allow))
        with Session(self.engine) as s, s.begin():
            s.execute(update(Tool).where(Tool.tenant == tenant, Tool.name == name)
                      .values(allow=json.dumps(clean), updated=now()))
        return clean

    def delete_tool(self, tenant: str, name: str) -> int:
        with Session(self.engine) as s, s.begin():
            s.execute(delete(ToolToken).where(ToolToken.tenant == tenant, ToolToken.tool == name))
            return s.execute(delete(Tool).where(Tool.tenant == tenant, Tool.name == name)).rowcount or 0

    # ---- OAuth MCP servers: the server's settings, each person's tokens, sign-ins in progress
    def set_tool_oauth(self, tenant: str, name: str, *, mode: str, sealed: str) -> None:
        with Session(self.engine) as s, s.begin():
            s.execute(update(Tool).where(Tool.tenant == tenant, Tool.name == name)
                      .values(auth_mode=mode, oauth=sealed, updated=now()))

    def get_token(self, tenant: str, tool: str, user: str) -> ToolToken | None:
        with Session(self.engine) as s:
            t = s.execute(select(ToolToken).where(ToolToken.tenant == tenant, ToolToken.tool == tool,
                                                  ToolToken.user == user)).scalar_one_or_none()
            if t is not None:
                s.expunge(t)
            return t

    def save_token(self, *, tenant: str, tool: str, user: str, access: str, refresh: str | None,
                   expires: datetime | None, scope: str) -> None:
        """refresh None keeps the stored refresh token (servers may not send a new one on refresh)."""
        with Session(self.engine) as s, s.begin():
            t = s.execute(select(ToolToken).where(ToolToken.tenant == tenant, ToolToken.tool == tool,
                                                  ToolToken.user == user)).scalar_one_or_none()
            if t is None:
                t = ToolToken(tenant=tenant, tool=tool, user=user, access=access)
                s.add(t)
            t.access, t.expires, t.scope, t.updated = access, expires, scope[:512], now()
            if refresh is not None:
                t.refresh = refresh

    def delete_token(self, tenant: str, tool: str, user: str) -> int:
        with Session(self.engine) as s, s.begin():
            return s.execute(delete(ToolToken).where(ToolToken.tenant == tenant, ToolToken.tool == tool,
                                                     ToolToken.user == user)).rowcount or 0

    def connected_users(self, tenant: str, tool: str) -> int:
        with Session(self.engine) as s:
            return len(list(s.execute(select(ToolToken.id).where(ToolToken.tenant == tenant,
                                                                 ToolToken.tool == tool)).scalars()))

    def add_pending(self, *, state: str, tenant: str, tool: str, user: str, verifier: str, back: str) -> None:
        with Session(self.engine) as s, s.begin():
            s.execute(delete(OAuthPending).where(OAuthPending.created < now() - timedelta(minutes=10)))
            s.add(OAuthPending(state=state, tenant=tenant, tool=tool, user=user, verifier=verifier, back=back[:512]))

    def pop_pending(self, state: str) -> OAuthPending | None:
        """The sign-in for this state, once: it is removed, and one older than ten minutes doesn't count."""
        with Session(self.engine) as s, s.begin():
            p = s.get(OAuthPending, state)
            if p is None:
                return None
            s.delete(p)
            created = p.created if p.created.tzinfo else p.created.replace(tzinfo=UTC)
            s.flush()
            s.expunge(p)
            return p if created > now() - timedelta(minutes=10) else None

    def list_tools(self) -> list[Tool]:
        with Session(self.engine) as s:
            rows = list(s.execute(select(Tool).order_by(Tool.tenant, Tool.name)).scalars())
            for t in rows:
                s.expunge(t)
            return rows

    # ---- local models (served by DataOS apps)
    def save_model(self, *, tenant: str, name: str, owner: str, app: str, url: str, key_secret: str, repo: str,
                   description: str) -> LocalModel:
        with Session(self.engine) as s, s.begin():
            m = LocalModel(tenant=tenant, name=name, owner=owner, app=app, url=url, key_secret=key_secret, repo=repo,
                           description=description)
            s.add(m)
            s.flush()
            s.expunge(m)
            return m

    def get_model(self, tenant: str, name: str) -> LocalModel | None:
        with Session(self.engine) as s:
            m = s.execute(select(LocalModel).where(LocalModel.tenant == tenant, LocalModel.name == name)
                          ).scalar_one_or_none()
            if m is not None:
                s.expunge(m)
            return m

    def list_models(self, tenant: str | None = None) -> list[LocalModel]:
        with Session(self.engine) as s:
            q = select(LocalModel).order_by(LocalModel.tenant, LocalModel.name)
            if tenant:
                q = q.where(LocalModel.tenant == tenant)
            rows = list(s.execute(q).scalars())
            for m in rows:
                s.expunge(m)
            return rows

    def delete_model(self, tenant: str, name: str) -> int:
        with Session(self.engine) as s, s.begin():
            return s.execute(delete(LocalModel).where(LocalModel.tenant == tenant, LocalModel.name == name)).rowcount or 0

    def record_tool_call(self, **row) -> None:
        with Session(self.engine) as s, s.begin():
            s.add(ToolCall(**row))

    def tool_summary(self, *, owner: str | None, tool: str | None, tenant: str | None, since_hours: int,
                     group_by: list[str]) -> list[dict]:
        group_by = [g for g in group_by if g in TOOL_GROUPABLE] or ["tool"]
        cols = [func.date(ToolCall.ts).label("day") if g == "day" else getattr(ToolCall, g).label(g) for g in group_by]
        q = select(*cols, func.count().label("calls"),
                   func.sum(case((ToolCall.error.is_(True), 1), else_=0)).label("errors"),
                   func.avg(ToolCall.latency_ms).label("avg_latency_ms"),
                   ).where(ToolCall.ts >= now() - timedelta(hours=since_hours))
        if owner:
            q = q.where(ToolCall.owner == owner)
        if tool:
            q = q.where(ToolCall.tool == tool)
        if tenant:
            q = q.where(ToolCall.tenant == tenant)
        with Session(self.engine) as s:
            rows = [dict(r._mapping) for r in s.execute(q.group_by(*cols).order_by(*cols))]
        for r in rows:
            r["errors"] = int(r["errors"] or 0)
            r["avg_latency_ms"] = int(r["avg_latency_ms"] or 0)
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
