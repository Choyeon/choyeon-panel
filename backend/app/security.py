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
# `\Z` 而不是 `$`：`$` 允许结尾多一个换行，"admin\n" 会以 match 通过。名字是 token 的
# sub、也是审计与目录名的原料，带一个看不见却能分开两个同名账号的换行进来最难查。
USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}\Z")
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


def burn_kdf(password: str) -> None:
    """按同一套 scrypt 参数空烧一次，结果丢掉。

    账号不存在时如果直接短路返回，未知用户名 ~1ms、已存在用户名 ~50ms，
    配上"用户名或密码错误"这条一模一样的提示也白搭——响应时间本身就把账号名单念出来了
    （面板的口令规则是 `^[A-Za-z0-9_-]{3,32}$`，可枚举）。空烧一次让两条分支耗时一致。
    """
    try:
        data = password.encode() or b"-"
    except UnicodeEncodeError:
        data = b"-"
    hashlib.scrypt(data, salt=b"choyeon-panel-burn", n=2**14, r=8, p=1, dklen=64, maxmem=32 * 1024 * 1024)


async def burn_kdf_async(password: str) -> None:
    async with _scrypt_sem():
        await asyncio.to_thread(burn_kdf, password)


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


def _epoch_head(v: str) -> int:
    """epoch 的 time_ns 前缀；十六进制跨位长时字典序会骗人（"fff" < "1000" 才是对的）。"""
    try:
        return int(str(v).split(".")[0], 16)
    except ValueError:
        return -1


def _user_epoch(username: str) -> str:
    """当前生效的 token 版本号：全局值与该用户墓碑里较新的那个。

    旧写法是 `token_epoch:<user> or <全局>`，于是第一个被 bump 过的用户会**永久**把全局值
    挡在门外：等管理员按下"踢下所有人"，改过密的人（恰恰是最可能持有长期 token 的那批）
    手里的旧票照样能用。两边都是单调的 time_ns，取较大者就对了：全局 bump 之后全员旧票
    一起失效，之后单个用户 bump 仍然只影响他自己。
    """
    from . import database as dbm

    user_v = dbm.get_setting(f"token_epoch:{username}")
    global_v = _global_epoch()
    if user_v and _epoch_head(user_v) > _epoch_head(global_v):
        return user_v
    return global_v


def session_epoch(username: str) -> str:
    """当前 token 版本号，给长连接（网页终端）复核身份用。

    握手时抓一份，之后定时再读一次比对：改密、CLI 重置/删号、管理员降权都会 bump
    这个版本号，对不上就说明签发那一刻的授权已经收回了。
    """
    return _user_epoch(username)


def session_identity(username: str) -> tuple[str, str]:
    """长连接复验用的身份快照 =（库里的角色，token 版本号）。同步 sqlite，调用方放线程里跑。

    只看 epoch 不够：管理员把某人降为只读时改的是 users.role，不 bump epoch，那台已经
    握手的 root shell/日志流就感觉不到。账号不存在按 LookupError 抛：调用方把"查不到人"
    和"变了"同样当成失效。
    """
    from . import database as dbm

    row = dbm.query_one("SELECT role FROM users WHERE username=?", (username,))
    if not row:
        raise LookupError(f"用户 {username} 不存在")
    return str(row["role"] or "admin"), _user_epoch(username)


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


# 登录失败会写进审计表的 action 名，唯一一份登记在这里：main.py 的每一条失败分支用它自己的
# 字面量写，而 login_audit_service 统计"过去一天有多少人在撞面板的门"要按同一组名字查。
# 两处各写一遍的结局是加一条失败分支忘了登记（或多写一个不存在的名字），判词从此少算或多算，
# 而这个数决定"没人开两步验证"是提示还是要紧——所以它由 test_batch23 拿源码核对，不许漂。
LOGIN_FAILED_ACTIONS = ("login_failed", "login_totp_missing", "login_totp_failed")

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


# ---------- 按账号的失败计数 ----------
# IP 桶挡不住分布式慢爆：对方有几百个 IP（代理池、肉鸡），每个 IP 每分钟 9 次，
# 打在同一个 admin 账号上的命中率就是 9×N。爆破了 admin 口令就等于拿到
# /api/terminal 的 root shell，这一层必须按账号也收一道。
# 与 IP 桶的区别：这里只在**失败**时计数（成功登录不消耗名额，也不该把
# 多台设备正常使用的账号算成攻击），且窗口起点固定——不随每次失败顺延，
# 否则"每 50 秒失败一次"的慢 drip 会让桶永不过期，把锁变成永久封禁。
# 代价是任何人都能用错误的口令把某个已知账号锁最多一分钟；这是可用性换
# 爆破成本，和已有的 IP 桶是同一套取舍（LOGIN_LIMIT / LOGIN_WINDOW_SEC）。

ACCOUNT_PREFIX = "acct:"


def account_key(username: str) -> str:
    return ACCOUNT_PREFIX + str(username or "").strip().lower()[:64]


def account_locked(key: str) -> bool:
    rec = _attempts.get(key)
    if not rec:
        return False
    if time.time() - rec[1] > config.LOGIN_WINDOW_SEC:
        _attempts.pop(key, None)
        return False
    return rec[0] > config.LOGIN_LIMIT


def note_failure(key: str) -> None:
    now = time.time()
    rec = _attempts.get(key)
    if not rec or now - rec[1] > config.LOGIN_WINDOW_SEC:
        _attempts[key] = [1, now]
    else:
        rec[0] += 1


def cleanup_attempts() -> None:
    now = time.time()
    for ip in list(_attempts):
        if now - _attempts[ip][1] > 600:
            del _attempts[ip]
