import re
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import database as dbm
from .. import security
from ..deps import need_admin as _need_admin
from ..services import alerts_service, nginx_ops
from ..util import is_http_url

router = APIRouter()

ALLOWED_ALERT_KEYS = re.compile(r"^alert_(telegram_bot|telegram_chat|webhook_url|disk_pct|ssl_days)$")

# 各 key 的取值约束：None 表示只做长度限制。
# 必须在保存时就校验——`int(dbm.get_setting("alert_disk_pct") or 85)` 在巡检里，
# 存进 "abc" 之后每次巡检都抛 ValueError，被循环吞掉，表现为「告警从此不再触发」，
# 而网页端一切正常，最难查的那种坏法。
ALERT_INT_RANGE = {"alert_disk_pct": (50, 99), "alert_ssl_days": (0, 60)}
ALERT_MAX_LEN = {"alert_telegram_bot": 128, "alert_telegram_chat": 64, "alert_webhook_url": 512}


async def _body(req: Request) -> dict:
    try:
        b = await req.json()
    except Exception:  # noqa: BLE001
        b = None
    return b if isinstance(b, dict) else {}


@router.get("/api/users")
async def users_list(req: Request):
    if r := _need_admin(req):
        return r
    return dbm.query("SELECT id,username,role,created_at FROM users ORDER BY id")


@router.post("/api/users")
async def users_create(req: Request):
    if r := _need_admin(req):
        return r
    body = await _body(req)
    username = body.get("username") or ""
    password = body.get("password") or ""
    if not re.match(r"^[A-Za-z0-9_-]{3,32}$", username):
        return JSONResponse(status_code=400, content={"error": "用户名不合法"})
    if not password or len(password) < 8:
        return JSONResponse(status_code=400, content={"error": "密码至少 8 位"})
    role = "viewer" if body.get("role") == "viewer" else "admin"
    try:
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (username, security.hash_pass(password), role),
        )
    except sqlite3.IntegrityError:
        # 只把「唯一约束冲突」解释成用户名已存在。
        # 旧写法是 except Exception：磁盘满、库被锁、WAL 写不进都会显示
        # 「用户名已存在」（实测），管理员反复换个名字还是这句，日志里也查不到，
        # 属于最难排查的一类误报。其余异常交给全局处理器变 500。
        return JSONResponse(status_code=400, content={"error": "用户名已存在"})
    dbm.audit(req.state.cp_sub, "user:create", f"{username}({role})")
    return {"ok": True}


@router.patch("/api/users/{uid}")
async def users_update(uid: str, req: Request):
    if r := _need_admin(req):
        return r
    try:
        u = dbm.query_one("SELECT * FROM users WHERE id=?", (int(uid),))
    except ValueError:
        u = None
    if not u:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    body = await _body(req)

    # 先把所有变更校验完、再一次性落库。
    # 旧写法是「改 role → 落库 → 校验 password → 落库」，于是
    # {"role":"viewer","password":"short"} 会返回 400「密码至少 8 位」，
    # 但 role 已经真的改成 viewer 了（实测）：调用方以为整笔请求没生效，
    # 用户权限却已经掉了一级，而且这条分支连审计都不写，事后无从排查。
    new_role = None
    if body.get("role"):
        new_role = "viewer" if body["role"] == "viewer" else "admin"
        if new_role != u["role"] and u["username"] == req.state.cp_sub:
            return JSONResponse(status_code=400, content={"error": "不能修改自己的角色"})
        if new_role == u["role"]:
            new_role = None  # 传了等于没传：不算变更，免得一次空保存把人踢下线
    new_hash = None
    if body.get("password"):
        if len(body["password"]) < 8:
            return JSONResponse(status_code=400, content={"error": "密码至少 8 位"})
        new_hash = security.hash_pass(body["password"])

    if new_role is None and new_hash is None:
        dbm.audit(req.state.cp_sub, "user:update", f"{u['username']}(无变更)")
        return {"ok": True}

    if new_role is not None:
        dbm.execute("UPDATE users SET role=? WHERE id=?", (new_role, u["id"]))
    if new_hash is not None:
        dbm.execute("UPDATE users SET pass_hash=? WHERE id=?", (new_hash, u["id"]))
    # role 与 password 都只存在于已签发的 token 里（中间件读的是 token 的 role，
    # 不是库里的），改完必须吊销旧 token，否则把 admin 降成 viewer 后
    # 对方手上的旧 token 在 TTL 内（最长 72h）依旧是 admin——连写操作都拦不住（实测）。
    # 改密本来就要吊销；这里合并成「任何一次实际变更都吊销」。
    security.bump_user_epoch(u["username"])
    dbm.audit(req.state.cp_sub, "user:update", u["username"])
    return {"ok": True}


@router.delete("/api/users/{uid}")
async def users_delete(uid: str, req: Request):
    if r := _need_admin(req):
        return r
    try:
        u = dbm.query_one("SELECT * FROM users WHERE id=?", (int(uid),))
    except ValueError:
        u = None
    if not u:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    if u["username"] == req.state.cp_sub:
        return JSONResponse(status_code=400, content={"error": "不能删除自己"})
    dbm.execute("DELETE FROM users WHERE id=?", (u["id"],))
    security.bump_user_epoch(u["username"])
    dbm.audit(req.state.cp_sub, "user:delete", u["username"])
    return {"ok": True}


ALERT_KEYS = ["alert_telegram_bot", "alert_telegram_chat", "alert_webhook_url", "alert_disk_pct", "alert_ssl_days"]


@router.get("/api/settings/alerts")
async def alerts_get(req: Request):
    # 通知通道含密钥，仅管理员可读；只读账号返回空值以免泄露
    secret_keys = ("alert_telegram_bot", "alert_telegram_chat", "alert_webhook_url")
    if req.state.cp_role != "admin":
        return {k: ("" if k in secret_keys else (dbm.get_setting(k) or "")) for k in ALERT_KEYS}
    return {k: dbm.get_setting(k) or "" for k in ALERT_KEYS}


def _validate_alert(k: str, val: str) -> str | None:
    """返回错误信息；None 表示通过。空值等于关闭该项，直接放行。"""
    if val == "":
        return None
    if (maxlen := ALERT_MAX_LEN.get(k)) and len(val) > maxlen:
        return f"{k} 超过 {maxlen} 字符"
    if (rng := ALERT_INT_RANGE.get(k)):
        try:
            n = int(val)
        except ValueError:
            return f"{k} 必须是整数，收到 {val[:20]!r}"
        lo, hi = rng
        if not lo <= n <= hi:
            return f"{k} 必须在 {lo}-{hi} 之间，收到 {n}"
    if k == "alert_webhook_url" and not is_http_url(val):
        return "Webhook URL 必须以 http:// 或 https:// 开头"
    return None


@router.post("/api/settings/alerts")
async def alerts_save(req: Request):
    if r := _need_admin(req):
        return r
    body = await _body(req)
    accepted: dict[str, str] = {}
    errors: list[str] = []
    for k, v in body.items():
        if not ALLOWED_ALERT_KEYS.match(k):
            continue
        val = "" if v is None else str(v).strip()
        if err := _validate_alert(k, val):
            errors.append(err)
            continue
        accepted[k] = val
    # 先全部校验再落库：部分写入会让通道配置变成"一半新一半旧"的不可解释状态
    if errors:
        # 校验不过时一个字段都没写，也就没有"变更"可记。旧写法先落一条
        # settings:alerts 审计再返回 400，事后翻审计会以为配置真的改过。
        return JSONResponse(status_code=400, content={"error": "；".join(errors)})
    for k, val in accepted.items():
        dbm.set_setting(k, val)
    dbm.audit(req.state.cp_sub, "settings:alerts", ",".join(accepted.keys()) or "-")
    return {"ok": True}


@router.post("/api/settings/alerts/test")
async def alerts_test(req: Request):
    if r := _need_admin(req):
        return r
    delivered = await alerts_service.notify_test()
    if not delivered:
        return JSONResponse(status_code=400, content={"error": "没有可用通道，或全部投递失败（详见面板日志）"})
    return {"ok": True}


@router.post("/api/settings/alerts/run-checks")
async def alerts_run_checks(req: Request):
    if r := _need_admin(req):
        return r
    await alerts_service.run_checks()
    return {"ok": True}


@router.get("/api/audit")
async def audit_list(req: Request):
    if r := _need_admin(req):
        return r
    try:
        limit = int(req.query_params.get("limit") or 100)
    except ValueError:
        limit = 100
    # 必须 clamp 下界：SQLite 里 LIMIT 负数表示"不限制"，
    # 只写 min(limit, 500) 的话 ?limit=-1 会把整张审计表吐出来（实测 601 条全返回）。
    return dbm.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),))


@router.post("/api/system/certbot-renew")
async def certbot_renew(req: Request):
    if r := _need_admin(req):
        return r
    try:
        log = await nginx_ops.renew_certs()
    except Exception as e:  # noqa: BLE001
        return JSONResponse(status_code=400, content={"error": str(e)})
    # 审计写在执行之后：续期失败也先记一条"已执行"，事后无法区分成功与失败。
    dbm.audit(req.state.cp_sub, "certbot:renew")
    return {"ok": True, "log": log}
