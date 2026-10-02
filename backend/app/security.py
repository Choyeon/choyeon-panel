import hashlib
import hmac
import secrets
import time

import jwt

from .database import jwt_secret

ALGO = "HS256"


def hash_pass(password: str) -> str:
    salt = secrets.token_hex(16)
    key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64, maxmem=32 * 1024 * 1024)
    return f"{salt}:{key.hex()}"


def check_pass(password: str, stored: str) -> bool:
    try:
        salt, key = stored.split(":")
        got = hashlib.scrypt(password.encode(), salt=salt.encode(), n=2**14, r=8, p=1, dklen=64, maxmem=32 * 1024 * 1024)
        return hmac.compare_digest(got.hex(), key)
    except (ValueError, TypeError):
        return False


def sign_token(username: str, role: str) -> str:
    now = int(time.time())
    payload = {"sub": username, "role": role, "iat": now, "exp": now + 12 * 3600}
    return jwt.encode(payload, jwt_secret(), algorithm=ALGO)


def verify_token(token: str) -> dict:
    return jwt.decode(token, jwt_secret(), algorithms=[ALGO])


# login rate limit: >10 attempts per minute per ip
_attempts: dict[str, list] = {}


def rate_limited(ip: str) -> bool:
    now = time.time()
    rec = _attempts.get(ip)
    if not rec or now - rec[1] > 60:
        _attempts[ip] = [1, now]
        return False
    rec[0] += 1
    return rec[0] > 10


def cleanup_attempts():
    now = time.time()
    for ip in list(_attempts):
        if now - _attempts[ip][1] > 600:
            del _attempts[ip]
