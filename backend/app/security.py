import hashlib
import hmac
import secrets
import time

import jwt

from . import config
from .database import jwt_secret
from .database import token_epoch as _global_epoch

ALGO = "HS256"


def hash_pass(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64, maxmem=32 * 1024 * 1024)
    return f"{salt}:{key.hex()}"


def check_pass(password: str, stored: str) -> bool:
    try:
        salt, key = stored.split(":")
        # 参数与 hashlib.scrypt 官方示例一致：N=2^14, r=8, p=1, dklen=64
        got = hashlib.scrypt(
            password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64, maxmem=32 * 1024 * 1024
        )
        return hmac.compare_digest(got.hex(), key)
    except (ValueError, TypeError):
        return False


def _user_epoch(username: str) -> str:
    """每用户密钥版本号：改密 / 删号后自增，只让该用户已签发的 token 失效。"""
    from . import database as dbm

    return dbm.get_setting(f"token_epoch:{username}") or _global_epoch()


def bump_user_epoch(username: str) -> str:
    from . import database as dbm

    v = dbm.new_epoch()
    dbm.set_setting(f"token_epoch:{username}", v)
    return v


def sign_token(username: str, role: str) -> str:
    now = int(time.time())
    payload = {
        "sub": username,
        "role": role,
        "iat": now,
        "exp": now + config.TOKEN_TTL_HOURS * 3600,
        "ver": _user_epoch(username),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, jwt_secret(), algorithm=ALGO)


def verify_token(token: str) -> dict:
    payload = jwt.decode(token, jwt_secret(), algorithms=[ALGO], options={"require": ["exp", "sub"]})
    if payload.get("ver") != _user_epoch(payload.get("sub", "")):
        raise jwt.InvalidTokenError("token revoked")
    return payload


# ---------- 登录限速：每 IP 每分钟最多 LOGIN_LIMIT 次 ----------
_attempts: dict[str, list] = {}


def rate_limited(ip: str) -> bool:
    now = time.time()
    rec = _attempts.get(ip)
    if not rec or now - rec[1] > config.LOGIN_WINDOW_SEC:
        _attempts[ip] = [1, now]
        return False
    rec[0] += 1
    return rec[0] > config.LOGIN_LIMIT


def clear_attempts(ip: str) -> None:
    _attempts.pop(ip, None)


def cleanup_attempts() -> None:
    now = time.time()
    for ip in list(_attempts):
        if now - _attempts[ip][1] > 600:
            del _attempts[ip]
