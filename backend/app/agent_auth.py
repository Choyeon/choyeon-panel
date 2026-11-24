"""API key authentication and operation helpers for automation clients."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from fastapi import Request
from fastapi.responses import JSONResponse

from . import database as dbm

KEY_RE = re.compile(r"^cp_live_([a-f0-9]{16})\.([A-Za-z0-9_-]{32,})\Z")
DEFAULT_SCOPES = ("system:read", "apps:read", "apps:write", "deploy:write")
ALL_SCOPES = frozenset(
    {
        "system:read",
        "apps:read",
        "apps:write",
        "deploy:write",
        "sites:write",
        "backups:read",
        "backups:write",
        "audit:read",
    }
)
_REQUESTS: dict[tuple[int, str], deque[float]] = defaultdict(deque)


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _expiry(days: int | None) -> str | None:
    if days is None:
        days = 90
    if not isinstance(days, int) or days < 1 or days > 365:
        raise ValueError("有效期必须是 1-365 天")
    return (datetime.now(UTC) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _safe_scopes(scopes) -> list[str]:
    if scopes is None:
        scopes = list(DEFAULT_SCOPES)
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("至少需要一个权限域")
    values = sorted({str(s) for s in scopes})
    unknown = [s for s in values if s not in ALL_SCOPES]
    if unknown:
        raise ValueError(f"未知权限域：{', '.join(unknown)}")
    return values


def create_key(name: str, scopes=None, expires_days: int | None = None, actor: str = "") -> dict:
    name = str(name or "").strip()
    if not name or len(name) > 64:
        raise ValueError("API Key 名称不能为空且不超过 64 个字符")
    chosen = _safe_scopes(scopes)
    public_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"cp_live_{public_id}.{secret}"
    expires_at = _expiry(expires_days)
    dbm.execute(
        "INSERT INTO api_keys(public_id,name,secret_hash,scopes,expires_at,created_by) VALUES(?,?,?,?,?,?)",
        (public_id, name, _hash_secret(secret), json.dumps(chosen), expires_at, str(actor or "")[:64]),
    )
    row = dbm.query_one("SELECT * FROM api_keys WHERE public_id=?", (public_id,))
    dbm.audit(actor or "system", "api-key:create", name)
    return {**public_view(row), "token": token}


def public_view(row: dict) -> dict:
    try:
        scopes = json.loads(row.get("scopes") or "[]")
    except (TypeError, ValueError):
        scopes = []
    return {
        "id": int(row["id"]),
        "public_id": row["public_id"],
        "name": row["name"],
        "scopes": scopes,
        "expires_at": row.get("expires_at"),
        "last_used_at": row.get("last_used_at"),
        "revoked_at": row.get("revoked_at"),
        "created_by": row.get("created_by") or "",
        "created_at": row.get("created_at"),
        "active": not row.get("revoked_at") and not _expired(row.get("expires_at")),
    }


def _expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp <= datetime.now(UTC)
    except ValueError:
        return True


def list_keys() -> list[dict]:
    return [public_view(r) for r in dbm.query("SELECT * FROM api_keys ORDER BY id DESC")]


def revoke_key(key_id: int, actor: str = "") -> dict:
    row = dbm.query_one("SELECT * FROM api_keys WHERE id=?", (int(key_id),))
    if not row:
        raise LookupError("API Key 不存在")
    if not row.get("revoked_at"):
        dbm.execute("UPDATE api_keys SET revoked_at=datetime('now') WHERE id=?", (int(key_id),))
        dbm.audit(actor or "system", "api-key:revoke", row["name"])
    return public_view(dbm.query_one("SELECT * FROM api_keys WHERE id=?", (int(key_id),)))


def authenticate(raw: str | None) -> dict | None:
    if not raw or not raw.startswith("cp_live_"):
        return None
    match = KEY_RE.match(raw)
    if not match:
        return None
    public_id, secret = match.groups()
    row = dbm.query_one("SELECT * FROM api_keys WHERE public_id=?", (public_id,))
    if not row or row.get("revoked_at") or _expired(row.get("expires_at")):
        return None
    if not secrets.compare_digest(_hash_secret(secret), str(row.get("secret_hash") or "")):
        return None
    dbm.execute(
        "UPDATE api_keys SET last_used_at=datetime('now') WHERE id=? "
        "AND (last_used_at IS NULL OR last_used_at < datetime('now','-60 seconds'))",
        (row["id"],),
    )
    try:
        scopes = set(json.loads(row.get("scopes") or "[]"))
    except (TypeError, ValueError):
        scopes = set()
    return {"id": int(row["id"]), "public_id": public_id, "name": row["name"], "scopes": scopes}


def rate_limited(key_id: int, method: str) -> bool:
    """Per-process rolling minute limit; requests are counted after successful auth."""
    now = time.monotonic()
    bucket_name = "write" if method.upper() in {"POST", "PUT", "PATCH", "DELETE"} else "read"
    bucket = _REQUESTS[(int(key_id), bucket_name)]
    while bucket and now - bucket[0] >= 60:
        bucket.popleft()
    limit = 10 if bucket_name == "write" else 60
    if len(bucket) >= limit:
        return True
    bucket.append(now)
    return False


def require_scope(req: Request, scope: str):
    scopes = getattr(req.state, "cp_scopes", set())
    if scope not in scopes:
        return JSONResponse(
            status_code=403,
            content={"error": f"API Key 缺少权限域：{scope}", "code": "scope_required", "scope": scope},
        )
    return None


def actor(req: Request) -> str:
    return str(getattr(req.state, "cp_sub", "api") or "api")


def operation_id() -> str:
    return f"op_{secrets.token_urlsafe(18)}"
