import asyncio
import hashlib
import hmac
import re
import secrets
import time
from weakref import WeakKeyDictionary

import jwt

from . import config
from .database import jwt_secret
from .database import token_epoch as _global_epoch

ALGO = "HS256"

# 用户名的唯一权威写法：注册、建号（main.py / cli.py）与 token 主体校验（verify_token）
# 必须用同一个表达式，否则会出现"能建号但签发的 token 自己验不过"这种死锁。
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
ROLES = ("admin", "viewer")

# scrypt 的参数是 32MB 临时内存/次。放到线程池后并行度不再受事件循环限制，
# 不加约束的话一次登录风暴就能占掉几百 MB（实测 6 并发把 cgroup 顶到 108MB）。
# 2 个并发既保住了"登录不冻结全站"，也把峰值压在 64MB 以内。
# 信号量按事件循环绑定缓存：测试里每个用例各自 asyncio.run，共用一个会报跨循环错误。
_SCRYPT_CONCURRENCY = 2
_sem_by_loop: WeakKeyDictionary = WeakKeyDictionary()


def _scrypt_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sem_by_loop.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_SCRYPT_CONCURRENCY)
        _sem_by_loop[loop] = sem
    return sem


async def hash_pass_async(password: str) -> str:
    async with _scrypt_sem():
        return await asyncio.to_thread(hash_pass, password)


async def check_pass_async(password: str, stored: str) -> bool:
    async with _scrypt_sem():
        return await asyncio.to_thread(check_pass, password, stored)


# 口令长度的上下限。四条设置路径（setup / 建号 / 管理员改密 / 自助改密）必须用同一把尺子，
# 否则一处放行、另一处拒绝，用户按提示改完还是 400。
PW_MIN_LEN = 8
PW_MAX_LEN = 128
# 登录时的纯防放大上限：比 PW_MAX_LEN 宽一个数量级，放行历史上用超长口令建过的账号，
# 同时挡掉"拿一条 200MB 的口令让我算 scrypt"这种 CPU 放大。
LOGIN_PW_MAX_LEN = 1024


def password_error(password, label: str = "密码") -> str | None:
    """口令能不能拿来建号/改密的唯一判据；合法返回 None。

    上限不是洁癖：scrypt 的耗时随输入长度增长，一次大输入就是一场 DoS。
    编码检查也不是走过场：JSON 里的孤立代理字符（无法用 UTF-8 表达的那类码点）
    照样是合法输入，但它 encode() 就抛 UnicodeEncodeError —— 不设防的话用户在
    hash_pass 里收到的是 500，而不是"口令不合法"。
    """
    if not isinstance(password, str) or len(password) < PW_MIN_LEN:
        return f"{label}至少 {PW_MIN_LEN} 位"
    if len(password) > PW_MAX_LEN:
        return f"{label}最多 {PW_MAX_LEN} 位"
    try:
        password.encode()
    except UnicodeEncodeError:
        return f"{label}含无法编码的字符"
    return None


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
    except (ValueError, TypeError, UnicodeEncodeError):
        # UnicodeEncodeError 来自 password.encode()：登录接口不能因为对方送来一个
        # 孤立代理字符就抛 500，那既不是"密码错误"也不是可重试的状态码。
        return False


def _user_epoch(username: str) -> str:
    """每用户密钥版本号：改密 / 删号后自增，只让该用户已签发的 token 失效。"""
    from . import database as dbm

    return dbm.get_setting(f"token_epoch:{username}") or _global_epoch()


def session_epoch(username: str) -> str:
    """当前 token 版本号，给长连接（网页终端）复核身份用。

    握手时抓一份，之后定时再读一次比对：改密、CLI 重置/删号、管理员降权都会 bump
    这个版本号，对不上就说明签发那一刻的授权已经收回了。
    """
    return _user_epoch(username)


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
    sub = payload.get("sub")
    # `require: sub` 只保证这个 claim **存在**，空串照样过：实测一把 sub="" 且 role=admin
    # 的 token 能通过中间件并真的执行了建应用/部署/删应用，而审计里记不到任何主体
    # ——出事后无法追责。签名密钥泄露本已是最高危，但身份层必须自己把关：主体要么
    # 是合法用户名，要么这个 token 就不算认证通过。
    if not isinstance(sub, str) or not USERNAME_RE.match(sub):
        raise jwt.InvalidTokenError("token subject invalid")
    if payload.get("role") not in ROLES:
        raise jwt.InvalidTokenError("token role invalid")
    if payload.get("ver") != _user_epoch(sub):
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
