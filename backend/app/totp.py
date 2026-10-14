"""RFC 6238 TOTP：面板登录的第二因子。

只用标准库（hmac/hashlib/base64/struct/secrets）：这一层在登录路径上，多一个依赖就多一分
供应链面，而算法本身只有几十行。

三个数字只有这一处出处：`STEP_SECONDS`、`DIGITS`、`VERIFY_WINDOW`。它们既进口令算式，也经
`provisioning_uri()` 写进 otpauth URI（验证器 App 读的是 URI 里的 period/digits，不是它自己猜的），
还随 setup 响应经 `totp_params()` 下发给前端填输入框的 `maxlength` 与提示语。前端不抄数字。
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
from urllib.parse import quote

STEP_SECONDS = 30
DIGITS = 6
# 允许前后各一步的时钟差：手机与服务器的钟不可能秒级同步，0 步就是"快了 20 秒的人登不进"。
# 再宽是拿安全换方便——一步 30 秒，±1 已经是 90 秒的接受窗口。
VERIFY_WINDOW = 1
# RFC 4226 推荐的密钥长度＝SHA1 的分组大小；更短熵不够，更长没有意义。
SECRET_BYTES = 20
SECRET_MIN_BYTES = 8
SECRET_MAX_BYTES = 64
ISSUER = "choyeon-panel"

_B32_RE = re.compile(r"^[A-Z2-7]+$")
# `[0-9]` 而不是 `\d`：Python 的 `\d` 是 Unicode 意义上的十进制数字，全角 `１２３４５６`、
# 天城文 `१२३४५६` 都会通过。那样 normalize_code 会把一串非 ASCII 交给
# `hmac.compare_digest`，而它对非 ASCII 字符串直接抛 TypeError——粘一枚全角口令就是一个 500，
# 偏偏这条落在 `/api/auth/login` 这个全面板唯一免认证的入口上。
_CODE_RE = re.compile(r"^[0-9]{" + str(DIGITS) + r"}$")
# 零宽连接符/BOM/蒙古文导航符：粘贴自二维码文本或某些 App 的详情面时会带进来，
# 而 `str.isspace()` 不认它们。写成转义而不是字面字符，这个文件里不该有看不见的东西。
_INVISIBLE = "".join(chr(c) for c in (0x200B, 0x2060, 0xFEFF, 0x180E, 0x2028, 0x2029))


def _strip_sep(raw: str) -> str:
    return "".join(ch for ch in raw if not (ch.isspace() or ch in _INVISIBLE))


def _b32_decode(s: str) -> bytes:
    try:
        return base64.b32decode(s + "=" * (-len(s) % 8))
    except Exception as e:  # noqa: BLE001
        raise ValueError("密钥不是合法的 base32") from e


def new_secret() -> str:
    """新生成一个 base32 密钥（不带填充）。"""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def normalize_secret(secret: object) -> str:
    """统一大小写、去掉空格与填充；非法一律抛 ValueError，调用方按 400 处理而不是 500。"""
    s = _strip_sep(str(secret or "")).upper().rstrip("=").replace("-", "")
    if not _B32_RE.match(s):
        raise ValueError("密钥不是合法的 base32")
    raw = _b32_decode(s)
    if len(raw) < SECRET_MIN_BYTES:
        raise ValueError(f"密钥太短（至少 {SECRET_MIN_BYTES} 字节）")
    if len(raw) > SECRET_MAX_BYTES:
        raise ValueError(f"密钥太长（最多 {SECRET_MAX_BYTES} 字节）")
    return s


def normalize_code(raw: object) -> str:
    """取纯数字口令（允许空格与连字符分隔）。不合式返回空串，调用方按"不正确"处理。"""
    if not isinstance(raw, str):
        return ""
    s = _strip_sep(raw).replace("-", "")
    return s if _CODE_RE.match(s) else ""


def hotp(secret_b32: str, counter: int) -> str:
    digest = hmac.new(_b32_decode(secret_b32), struct.pack(">Q", max(0, counter)), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    val = (struct.unpack(">I", digest[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** DIGITS)
    return str(val).zfill(DIGITS)


def current_step(now: float | None = None) -> int:
    return int((time.time() if now is None else now) // STEP_SECONDS)


# 已用步数（按账号）。同一窗口里重复提交同一枚口令必须拒——否则抓到一枚口令的人在 90 秒内
# 可以反复登录。口径是"步数严格递增"：用过 99 之后 99 不再接受，100/101 仍然可以。
# 与服务器的其它限速桶一样是进程内存态；重启后当前窗口重开，代价是一枚口令多用一次，
# 而这枚口令本来就在这 90 秒内有效。
_used: dict[str, int] = {}


def note_used(username: str, step: int) -> None:
    _used[username] = step


def forget_used(username: str) -> None:
    _used.pop(username, None)


def reset_used() -> None:
    """测试夹具用：清掉全部已用步数。"""
    _used.clear()


def cleanup_used(now: float | None = None) -> None:
    """丢掉不可能再被任何窗口接受的步数，防止已删除的账号名把字典撑大。"""
    cutoff = current_step(now) - VERIFY_WINDOW
    for name in list(_used):
        if _used[name] < cutoff:
            _used.pop(name, None)


def verify(secret: str, code: object, username: str = "", now: float | None = None) -> bool:
    """校验一枚口令；成功即记下它属于哪一步，同一步不再二次接受。"""
    norm = normalize_code(code)
    if not norm:
        return False
    try:
        b32 = normalize_secret(secret)
    except ValueError:
        return False
    step = current_step(now)
    last = _used.get(username, -10 ** 9) if username else -10 ** 9
    for delta in range(-VERIFY_WINDOW, VERIFY_WINDOW + 1):
        cand = step + delta
        if cand <= last:
            continue
        if hmac.compare_digest(hotp(b32, cand), norm):
            if username:
                note_used(username, cand)
            return True
    return False


def provisioning_uri(secret: str, account: str, issuer: str = ISSUER) -> str:
    """验证器 App 扫码用的 otpauth URI。"""
    b32 = normalize_secret(secret)
    q = f"?secret={b32}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={STEP_SECONDS}"
    # label 里的冒号转义成 %3A（Google 的 Authenticator key-uri 规范就是这么写的：
    # `otpauth://totp/Issuer%3Aaccount`）。裸冒号主流 App 也认，但 label 只该由我们这一处
    # 决定" issuer 与账号在哪里分界"，让账号名里出现的冒号原样进去就会切错。
    return f"otpauth://totp/{quote(f'{issuer}:{account}', safe='')}{q}"


def totp_params() -> dict:
    """随 setup 响应下发的生效参数；前端据此填 maxlength 与提示语，不自己抄。"""
    return {"digits": DIGITS, "period": STEP_SECONDS, "issuer": ISSUER}
