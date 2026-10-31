import asyncio
import mimetypes
import os
import re
import stat
import time
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import aio_db, avatars, config, errors, security, totp
from . import database as dbm
from .deps import AuthMiddleware, BodyCapMiddleware
from .log import warn
from .routers import (
    alerts,
    apps,
    backups,
    cron,
    dbops,
    extras,
    files,
    meta,
    metrics,
    sites,
    system,
    terminal,
)
from .services import alerts_service, apps_service, metrics_store

START = time.time()

# 部分发行版的 /etc/mime.types 里没有 .webmanifest，回落到 application/octet-stream
# 会让浏览器直接忽略清单，图标尺寸/主题色全部失效。
mimetypes.add_type("application/manifest+json", ".webmanifest")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 上次进程退出可能留下 running 状态的部署记录，标记为失败避免前端一直转圈
    try:
        dbm.execute("UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE status='running'")
    except Exception as e:  # noqa: BLE001
        warn("reset running deployments failed:", e)

    # 延迟导入：backup_service 与 apps_service 互相引用，放到模块顶层会在 import 期转圈
    from .services import backup_service, cron_service

    try:
        # 上次进程是被杀的还是正常退的要分得开：恢复中途死掉会留下一个"面板停掉的
        # 服务"+半份归档，而这件事只有这条记录知道。判词进 journal 与审计，不静默清标记。
        for _line in await backup_service.resume_interrupted_restores():
            warn("restore interrupted:", _line)
    except Exception as e:  # noqa: BLE001
        warn("restore reconcile failed:", e)

    try:
        await backup_service.sync_all_timers()
    except Exception as e:  # noqa: BLE001
        warn("backup timers sync failed:", e)

    try:
        # 计划任务的 unit 只在增删改时渲染，所以升级面板不会碰到存量任务的 ExecStart——
        # 批次㉝ 给 ExecStart 套的 flock 因此在老任务上根本不存在。这一趟把盘上的 unit
        # 对回库里的行；没有任务时一次 systemctl 都不发。
        await cron_service.sync_all_units()
    except Exception as e:  # noqa: BLE001
        warn("cron units sync failed:", e)

    alerts_service.start_checker()
    hk = asyncio.create_task(_housekeeping())
    # 指标采样单独成任务：历史曲线是"越多越有用"的数据，一次偶发异常不能把
    # 清理任务一起拖停，反之亦然。
    sampler = asyncio.create_task(metrics_store.loop())
    try:
        yield
    finally:
        # 在跑的部署任务必须在这里收尾：不取消的话记录会停在 running，
        # 而回收器要求超过最坏耗时上限才动手，这期间该应用删不掉也回滚不了。
        with suppress(Exception):
            await apps_service.cancel_inflight_deployments()
        sampler.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await sampler
        hk.cancel()
        alerts_service.stop_checker()


app = FastAPI(
    title="choyeon-panel",
    version=config.VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
# 先加的先被包住：BodyCap 在 Auth 内侧，所以它的 413 照样带上安全响应头并进访问日志，
# 而 Auth 的 401/403 短路发生在读 body 之前（不认证也能把 body 读完的那类路由本来就没有）。
app.add_middleware(BodyCapMiddleware)
app.add_middleware(AuthMiddleware)
errors.install_handlers(app)


@app.exception_handler(RequestValidationError)
async def validation_handler(_req: Request, _exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"error": "请求参数不合法"})


@app.exception_handler(404)
async def not_found(_req: Request, _exc):
    return JSONResponse(status_code=404, content={"error": "not found"})


@app.exception_handler(Exception)
async def unhandled(_req: Request, exc: Exception):
    # 原文一律只进日志：CP_LOG 打开时把 traceback 贴给浏览器，等于向任何登录用户
    # （以及他们截图里的会话）公开内部路径与依赖结构。
    warn("unhandled", type(exc).__name__, exc)
    return JSONResponse(
        status_code=500,
        content={"error": errors.public_message(exc, fallback="服务器内部错误"), "code": "internal"},
    )


# ---------- public auth ----------
@app.get("/api/health")
async def health():
    return {"ok": True, "uptime": round(time.time() - START), "version": config.VERSION}


@app.get("/api/auth/status")
async def auth_status():
    # 未登录可访问：只告知是否需要初始化，不回显已有账号名
    return {"needsSetup": not dbm.query_one("SELECT 1 FROM users LIMIT 1")}


def _user_token(username: str, role: str, nickname: str = '', avatar: str = '') -> dict:
    return {
        "token": security.sign_token(username, role),
        "role": role,
        "username": username,
        "nickname": nickname,
        "avatar": avatar,
    }


@app.post("/api/auth/setup")
async def setup(req: Request):
    ip = client_ip(req)
    if security.rate_limited(ip):
        return JSONResponse(status_code=429, content={"error": "尝试过多，请稍后再试"})
    body = await _json_body(req)
    # 判据是"表里一个账号都没有"，与 /api/auth/status 的 needsSetup 同一个口径：
    # 原来按 admin 计数，管理员被 CLI 删光而机器上还留着 viewer 时，setup 会对公网
    # 重新开放——谁先 POST 谁就是 root，不需要猜任何东西。那种残局请用 CLI 建号。
    existing = dbm.query_one("SELECT COUNT(*) c FROM users")
    if existing and existing["c"] > 0:
        return JSONResponse(status_code=403, content={"error": "初始化已完成，请用 choyeonctl user create 建号"})
    # str() 兜的是 JSON 里 {"username": 123} 这种合法但离谱的输入：不转的话
    # 123.strip() 直接 AttributeError，一个未登录请求换来 500 和一条脏审计。
    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "")
    if not security.USERNAME_RE.match(username):
        return JSONResponse(status_code=400, content={"error": "用户名只能包含字母、数字、下划线与连字符（3-32 位）"})
    pw_err = security.password_error(password)
    if pw_err:
        return JSONResponse(status_code=400, content={"error": pw_err})
    # 撞名走 400 而不是 500：CLI 在另一个进程里同时建号时 INSERT 会抛 IntegrityError，
    # 未登录请求换来一坨进 journal 的栈。
    status = dbm.create_first_admin(username, await security.hash_pass_async(password))
    if status == "not-empty":
        return JSONResponse(status_code=403, content={"error": "初始化已完成，请用 choyeonctl user create 建号"})
    if status == "name-taken":
        return JSONResponse(status_code=400, content={"error": "该用户名已被占用"})
    await aio_db.audit(username, "setup", "创建管理员")
    security.clear_attempts(ip)
    return _user_token(username, "admin")


@app.post("/api/auth/login")
async def login(req: Request):
    ip = client_ip(req)
    if security.rate_limited(ip):
        return JSONResponse(status_code=429, content={"error": "尝试过多，请稍后再试"})
    body = await _json_body(req)
    username = str(body.get("username") or "").strip()
    akey = security.account_key(username)
    # 按账号这道检查放在 scrypt 之前：它拦的就是"同一个名字被反复试"，
    # 放在哈希之后等于让每次拦截都照样烧掉 16MB 内存和几十毫秒 CPU。
    if security.account_locked(akey):
        return JSONResponse(status_code=429, content={"error": "该账号尝试过多，请稍后再试"})
    user = dbm.query_one("SELECT * FROM users WHERE username=?", (username,))
    password = str(body.get("password") or "")
    # scrypt(n=2^14) 单次约 16MB/几十毫秒：直接在 async 里跑会冻住整个事件循环，
    # 两三个人同时登录就能把仪表盘轮询一起拖死。
    # 长到离谱的口令连 scrypt 都不进：那是对方挑着打的 CPU 放大，
    # 而 LOGIN_PW_MAX_LEN 比任何真实口令都宽，正常账号不会被挡在外面。
    plausible = bool(password) and len(password) <= security.LOGIN_PW_MAX_LEN
    if user and plausible:
        ok = await security.check_pass_async(password, user["pass_hash"])
    else:
        ok = False
        if plausible:
            # 用户名不存在也要烧掉同样的一次 scrypt：短路的话"未知用户 ~1ms /
            # 已知用户 ~50ms"就是公开账号名单，而 401 文案一致并救不了这点。
            await security.burn_kdf_async(password)
    # 这几条审计一律走 aio_db：dbm.audit 是拿全局 RLock 的 INSERT + commit。
    # 打在事件循环上时，撞库流量的每一条都要循环停一停，撞上 WAL checkpoint 就是
    # 整站冻住（仪表盘轮询、SSE 日志、WS 终端一起卡）；登录路由是唯一免认证入口，
    # 也就是唯一能被无限放大的那条。
    if not ok:
        # 只给真实存在的账号记账号桶：不存在的名字没什么可爆破，记了等于让
        # 陌生人随意撑大 _attempts 的键空间（清理每 600 秒才跑一次）。
        if user:
            security.note_failure(akey)
        await aio_db.audit(username or "?", "login_failed", ip)
        return JSONResponse(status_code=401, content={"error": "用户名或密码错误"})
    role = user["role"] or "admin"
    if not security.USERNAME_RE.match(user["username"]):
        # 老版本放行过的用户名可能不符合当前规则，而 verify_token 会拒绝这种主体。
        # 照签的话用户看到的是"登录成功，然后每个请求 401"，无从下手；在这里说清楚。
        await aio_db.audit(user["username"], "login_rejected", ip)
        return JSONResponse(
            status_code=403,
            content={"error": "该账号的用户名不符合当前规则，请由管理员用 choyeonctl user create 重建账号"},
        )
    secret = str(user.get("totp_secret") or "")
    if secret:
        # 第二因子只在**口令已经对了**之后才开口要：先要口令的话，一个不存在的账号、一个存在的
        # 账号、一个开了 2FA 的账号会给出三种不同响应，那等于把账号名单念给撞库的人听——
        # 而 `/api/auth/login` 是全面板唯一免认证的入口。
        code = totp.normalize_code(body.get("code"))
        if not code:
            await aio_db.audit(user["username"], "login_totp_missing", ip)
            return JSONResponse(
                status_code=401,
                # digits/period 一起下发：登录页输入框的位数与"多少秒换一枚"的提示都从这一份读，
                # 前端抄一个"6"的话，改参数那天就是"输入框只让填 6 位、校验要 8 位"那种坏法。
                # 这条响应只在口令已经对了之后才出现，多带三个公开参数不构成账号探测面。
                content={"error": f"请输入验证器里的 {totp.DIGITS} 位动态口令",
                         "code": "totp_required", **totp.totp_params()},
            )
        if not totp.verify(secret, code, username=user["username"]):
            # 口令错与口令对但动态口令错，都要消耗账号桶的名额：动态口令只有 10^DIGITS 种，
            # 不限速的话它就是那条"每次失败不花钱"的旁路。
            security.note_failure(akey)
            await aio_db.audit(user["username"], "login_totp_failed", ip)
            return JSONResponse(
                status_code=401,
                content={"error": "动态口令不正确或已过期，一枚口令只用一次；对不上表请等下一步再试",
                         "code": "totp_invalid", **totp.totp_params()},
            )
    await aio_db.audit(user["username"], "login")
    security.clear_attempts(ip)
    security.clear_attempts(akey)
    return _user_token(user["username"], role, user.get("nickname", ""), user.get("avatar", ""))


@app.post("/api/auth/logout")
async def logout(req: Request):
    """退出登录：吊销**这个账号**当前签发的全部 token。

    token 是无状态 JWT，服务端没有会话表可以只作废"手里这一枚"，所以诚实的做法是把
    这个账号的所有设备一起登出（bump 该用户的 epoch），并在返回里把这个事实说清楚 ——
    旧版前端只清 localStorage，用户以为退出了，那枚票在 TTL 内（最长 72h）还能拿来
    调 /api/terminal 拿 root shell。
    """
    sub = req.state.cp_sub
    security.bump_user_epoch(sub)
    await aio_db.audit(sub, "logout", "该账号全部会话已吊销")
    return {"ok": True, "scope": "all_devices"}


# ---------- 两步验证（TOTP）自助管理 ----------
# 只作用于**自己**这个账号：cp_sub 来自已验签的 token， body 里的用户名一律不看。
# 开与关都要求再输一次账号口令——手里攥着一枚偷来的 token 的人，不应该顺手把主人的
# 第二因子关掉（那会让他下次只需口令就能进）；同理开启也要口令，避免"会话被劫持后
# 攻击者绑上自己的验证器，反过来把真主人锁在门外"。
# 关闭还要一枚当前有效的动态口令：手机丢了走 `choyeonctl user 2fa-disable`（root 才能跑）。

@app.get("/api/auth/2fa")
async def twofa_status(req: Request):
    """当前账号的两步验证状态。只回"开没开"与生效参数，密钥永不出库。"""
    me = req.state.cp_sub
    return {"enabled": bool(dbm.totp_state(me)), **totp.totp_params()}


@app.post("/api/auth/2fa/setup")
async def twofa_setup(req: Request):
    """生成一枚新密钥并给出 otpauth URI；此时还没生效，必须拿 enable 验一次才算开。

    没有"待生效"的服务端状态：密钥由响应带回、enable 时原样送回，同一进程重启也不会让
    用户卡在"扫了码却没有对应记录"。开启这一步仍然要口令＋正确的一次口令，所以即便响应
    被第三个人看到，他也没法替主人把 2FA 打开（口令只在他手上）。
    """
    ip = client_ip(req)
    body = await _json_body(req)
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    if dbm.totp_state(me["username"]):
        return JSONResponse(status_code=400, content={"error": "该账号已开启两步验证，请先关闭再重新绑定"})
    if not await _password_ok(me, body.get("password")):
        await aio_db.audit(me["username"], "twofa_setup_denied", ip)
        return JSONResponse(status_code=400, content={"error": "口令不正确，未生成密钥"})
    secret = totp.new_secret()
    await aio_db.audit(me["username"], "twofa_setup", "生成新密钥（未生效）")
    return {"secret": secret, "account": me["username"],
            "otpauth": totp.provisioning_uri(secret, me["username"]), **totp.totp_params()}


@app.post("/api/auth/2fa/enable")
async def twofa_enable(req: Request):
    """用 setup 给出的密钥换一次正确口令，验过才写库——"能扫上码"不等于"绑定了"。"""
    ip = client_ip(req)
    body = await _json_body(req)
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    if dbm.totp_state(me["username"]):
        return JSONResponse(status_code=400, content={"error": "该账号已开启两步验证"})
    if not await _password_ok(me, body.get("password")):
        await aio_db.audit(me["username"], "twofa_enable_denied", ip)
        return JSONResponse(status_code=400, content={"error": "口令不正确"})
    code = totp.normalize_code(body.get("code"))
    try:
        secret = totp.normalize_secret(body.get("secret"))
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "密钥不合法，请重新生成后再扫"})
    if not totp.verify(secret, code, username=me["username"]):
        # 没验中就不记步数（verify 只在成功时记），也不写库：一枚猜中的口令才能绑定。
        await aio_db.audit(me["username"], "twofa_enable_failed", ip)
        return JSONResponse(status_code=400, content={"error": "动态口令不正确或已过期，请重试"})
    if not dbm.set_totp(me["username"], secret):
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    await aio_db.audit(me["username"], "twofa_enable", "两步验证已开启")
    return {"ok": True, "enabled": True, **totp.totp_params()}


@app.post("/api/auth/2fa/disable")
async def twofa_disable(req: Request):
    """关闭本账号的两步验证：口令 + 一枚当前有效的动态口令。"""
    ip = client_ip(req)
    body = await _json_body(req)
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    secret = dbm.totp_state(me["username"])
    if not secret:
        return JSONResponse(status_code=400, content={"error": "该账号没有开启两步验证"})
    if not await _password_ok(me, body.get("password")):
        await aio_db.audit(me["username"], "twofa_disable_denied", ip)
        return JSONResponse(status_code=400, content={"error": "口令不正确"})
    if not totp.verify(secret, body.get("code"), username=me["username"]):
        await aio_db.audit(me["username"], "twofa_disable_failed", ip)
        return JSONResponse(
            status_code=400,
            content={"error": "动态口令不正确或已过期。手机不在手边请由 root 在服务器上执行 "
                              "choyeonctl user 2fa-disable 关闭"},
        )
    dbm.set_totp(me["username"], "")
    totp.forget_used(me["username"])
    await aio_db.audit(me["username"], "twofa_disable", "两步验证已关闭")
    return {"ok": True, "enabled": False, **totp.totp_params()}


async def _password_ok(me: dict, raw: object) -> bool:
    """核对本人口令。与自助改密同一把尺子（LOGIN_PW_MAX_LEN），长到离谱就当不对，不进 scrypt。"""
    pw = str(raw or "")
    if not me or not pw or len(pw) > security.LOGIN_PW_MAX_LEN:
        return False
    return await security.check_pass_async(pw, me["pass_hash"])


async def _json_body(req: Request) -> dict:
    try:
        body = await req.json()
    except Exception:  # noqa: BLE001
        body = None
    return body if isinstance(body, dict) else {}


def client_ip(req: Request) -> str:
    """取用于限速的客户端 IP。

    反向代理场景必须解析 XFF，否则所有登录请求都记在 127.0.0.1 这一个桶里。
    但**最左**值是客户端自带的、可任意伪造（`X-Forwarded-For: 1.2.3.4` 每次换一个，
    限速就形同虚设）。nginx 的 `$proxy_add_x_forwarded_for` 会把真实对端追加到**最右**，
    所以只信最右一段；X-Real-IP 是本面板 nginx 模板里 `$remote_addr` 显式设的，
    客户端覆盖不了。

    前提是**这个头真是代理给的**：只听 127.0.0.1 不等于安全，本机任何进程都能绕过
    nginx 直连端口并随手编一个 X-Real-IP，于是每个伪造请求一个新桶、root 面板口令爆破
    零限速。所以先核对 TCP 对端在 CP_TRUSTED_PROXIES 里，不在就退回真实对端地址。
    """
    peer = req.client.host if req.client else ""
    if config.TRUST_PROXY and peer in config.TRUSTED_PROXIES:
        real = req.headers.get("x-real-ip")
        if real and (ip := real.strip()):
            return ip
        fwd = req.headers.get("x-forwarded-for")
        if fwd:
            for chunk in reversed(fwd.split(",")):
                if (ip := chunk.strip()):
                    return ip
    return peer or "?"


# ---------- self-service password (authenticated) ----------
@app.post("/api/auth/password")
async def change_password(req: Request):
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    body = await _json_body(req)
    old = str(body.get("old") or "")
    # 与登录同一把尺子：原密码长到离谱就当不对，不进 scrypt
    ok = bool(me) and bool(old) and len(old) <= security.LOGIN_PW_MAX_LEN \
        and await security.check_pass_async(old, me["pass_hash"])
    if not ok:
        return JSONResponse(status_code=400, content={"error": "原密码错误"})
    pw_err = security.password_error(body.get("new"), "新密码")
    if pw_err:
        return JSONResponse(status_code=400, content={"error": pw_err})
    role = me["role"] or "admin"
    new_hash = await security.hash_pass_async(body["new"])
    dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (new_hash, me["username"]))
    # 仅让当前用户的旧 token 失效，并立即签发新 token，避免用户被踢下线
    security.bump_user_epoch(me["username"])
    await aio_db.audit(me["username"], "password_change")
    return _user_token(me["username"], role, me.get("nickname", ""), me.get("avatar", ""))


@app.post("/api/auth/profile")
async def update_profile(req: Request):
    me = dbm.query_one("SELECT * FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    body = await _json_body(req)
    nickname = body.get("nickname")
    avatar = body.get("avatar")
    stmts = []
    if nickname is not None:
        stmts.append(("UPDATE users SET nickname=? WHERE id=?", (str(nickname).strip()[:32], me["id"])))
    if avatar is not None:
        stmts.append(("UPDATE users SET avatar=? WHERE id=?", (str(avatar).strip()[:256], me["id"])))
    # 一次事务：昵称与头像是一次保存的两个字段，逐条写会留下"名字换了、头像还是旧的"
    # 而请求已经 500，用户在页面上看到的就是"改了一半"，重放第二次才可能补齐。
    await aio_db.execute_many(stmts)
    await aio_db.audit(req.state.cp_sub, "profile:update")
    updated = dbm.query_one("SELECT nickname,avatar FROM users WHERE id=?", (me["id"],))
    return {"ok": True, "nickname": updated["nickname"], "avatar": updated["avatar"]}


# ---------- avatar upload (authenticated) ----------
# 只认这四类位图的魔数：接口收的是原始字节，若信 Content-Type 或文件名，
# 任何人都能以"头像"名义把 HTML/脚本写进 data 目录，再由同源的 GET 原样回吐——
# 那就是存储型 XSS 的起点（面板还带 admin 会话）。
_AVATAR_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "gif": "image/gif"}
_AVATAR_NAME_RE = re.compile(r"^(\d{1,10})\.(png|jpe?g|webp|gif)$")


def _sniff_image(b: bytes) -> str | None:
    if b[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if b[:3] == b"\xff\xd8\xff":
        return "jpg"
    if b[:4] == b"RIFF" and b[8:12] == b"WEBP":
        return "webp"
    if b[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


@app.post("/api/auth/avatar")
async def upload_avatar(req: Request):
    me = dbm.query_one("SELECT id FROM users WHERE username=?", (req.state.cp_sub,))
    if not me:
        return JSONResponse(status_code=404, content={"error": "用户不存在"})
    limit = config.AVATAR_MAX_BYTES
    declared = req.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        return JSONResponse(status_code=413, content={"error": f"图片超过 {limit // (1024 * 1024)}MB"})
    # 流式读并边读边判：await req.body() 会先把整个请求体吃进内存，
    # 上限靠 Content-Length 判断又能被伪造（不发该头即分块传输），必须在读取过程中截断。
    buf = bytearray()
    async for chunk in req.stream():
        buf.extend(chunk)
        if len(buf) > limit:
            return JSONResponse(status_code=413, content={"error": f"图片超过 {limit // (1024 * 1024)}MB"})
    data = bytes(buf)
    if len(data) < 32:
        return JSONResponse(status_code=400, content={"error": "图片内容为空或过小"})
    ext = _sniff_image(data)
    if not ext:
        return JSONResponse(status_code=415, content={"error": "只支持 PNG / JPEG / WebP / GIF"})

    os.makedirs(config.AVATAR_DIR, exist_ok=True)
    # 文件名只用用户自增 id：不采用用户名（可能含 . 与 ..），也不采用客户端给的扩展名。
    # 旧文件先清掉，否则换一种扩展名重传会同时留下两张脸，公开路由按文件名各取各的。
    avatars.purge(me["id"])
    path = os.path.join(config.AVATAR_DIR, f"{me['id']}.{ext}")
    with open(path, "wb") as fh:
        fh.write(data)
    os.chmod(path, 0o644)
    url = f"/api/avatars/{me['id']}.{ext}?v={int(time.time())}"
    dbm.execute("UPDATE users SET avatar=? WHERE id=?", (url, me["id"]))
    await aio_db.audit(req.state.cp_sub, "avatar:upload", f"{len(data)}B")
    return {"avatar": url}


@app.get("/api/avatars/{name}")
async def get_avatar(name: str):
    # 免登录（<img> 带不了 Authorization），所以只按白名单形状取文件：
    # 文件名完全由正则捕获组重建，请求里的任何路径片段都进不了 os.path.join。
    m = _AVATAR_NAME_RE.match(name or "")
    if not m:
        return JSONResponse(status_code=404, content={"error": "not found"})
    ext = "jpg" if m.group(2) in ("jpg", "jpeg") else m.group(2)
    path = os.path.join(config.AVATAR_DIR, f"{m.group(1)}.{ext}")
    if not os.path.isfile(path):
        return JSONResponse(status_code=404, content={"error": "not found"})
    return FileResponse(path, media_type=_AVATAR_TYPES[ext], headers={"Cache-Control": "public, max-age=86400"})


# ---------- routers ----------
app.include_router(apps.router)
app.include_router(meta.router)
app.include_router(system.router)
app.include_router(terminal.router)
app.include_router(dbops.router)
app.include_router(backups.router)
app.include_router(files.router)
app.include_router(extras.router)
app.include_router(alerts.router)
app.include_router(cron.router)
app.include_router(sites.router)
app.include_router(metrics.router)


# ---------- background housekeeping ----------
def _prune_all() -> None:
    # 三个 prune 都是带子查询的 DELETE，走 database.py 那把全局 RLock 的同步 sqlite。
    dbm.prune_audit()
    dbm.prune_deployments()
    dbm.prune_metrics()
    # 告警历史不在这里裁：alerts_service._checker_loop 每轮按用户设置的
    # alert_keep_days / alert_keep_rows 裁一次。以前这儿跟着 prune_alerts() 用默认值
    # （30 天 / 2000 条）每 10 分钟再裁一遍，把设置顶成死配置——管理员调到 365 天，
    # 三十天前的记录照样当天就被删掉，而且没有任何地方说它被谁删的。


async def _housekeeping():
    while True:
        await asyncio.sleep(600)
        try:
            security.cleanup_attempts()
            # 已用动态口令的步数桶同样会随账号名增长（含已删除的账号），跟着限速桶一起回收。
            totp.cleanup_used()
            # 先回收孤儿部署，再 prune：两者都是同步 sqlite 调用，必须在别的工作线程里跑
            # （直接在循环上 prune，审计/指标表一大就会把整站冻住几十秒；
            # metrics_store.loop 里的同类清理早已走 to_thread）。
            await asyncio.to_thread(apps_service.reap_orphan_deployments)
            await asyncio.to_thread(_prune_all)
        except Exception as e:  # noqa: BLE001
            warn("housekeeping failed:", e)


# ---------- static frontend ----------
def _accepts_gzip(raw: bytes) -> bool:
    """Accept-Encoding 是否接受 gzip（`gzip;q=0` 是显式拒绝，不能只 grep 子串）。"""
    for part in raw.decode("latin-1", "replace").split(","):
        token, _, params = part.strip().partition(";")
        if token.lower() not in ("gzip", "*"):
            continue
        for p in params.split(";"):
            key, _, val = p.strip().partition("=")
            if key.lower() == "q":
                try:
                    return float(val) > 0
                except ValueError:
                    pass
        return True
    return False


class DistFiles(StaticFiles):
    """哈希产物长缓存、HTML 每次回源。

    StaticFiles 不发 Cache-Control，浏览器就按 Last-Modified 做启发式缓存（约"年龄的 10%"），
    发版后旧标签页仍拿到旧 index.html：里面引用的 chunk 已被新构建删掉，
    线上表现是一串 /assets/Apps-xxxx.js 404、动态 import 失败、页面白屏只剩控制台报错。
    """

    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code=status_code)
        name = str(full_path).replace("\\", "/")
        if name.endswith(".html"):
            resp.headers["Cache-Control"] = "no-cache"
        elif "/assets/" in name:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            # 未压缩的首屏是 956KB（framework 单文件就 856KB），协商到预压缩副本后是 273KB。
            resp.headers["Vary"] = "Accept-Encoding"
            gz = self._precompressed(name, scope, resp)
            if gz is not None:
                return gz
        else:
            resp.headers["Cache-Control"] = "public, max-age=3600"
        return resp

    def _precompressed(self, name: str, scope, resp):
        """命中构建期产出的 `<file>.gz`（web/scripts/gzip-assets.mjs）。

        刻意不在请求路径上压缩：日志接口是 text/event-stream，任何 gzip 中间件都会
        把流攒成一块，日志中心就变成"卡住不动"。Etag/Last-Modified 沿用原文件——两种
        编码是同一份内容的两种表示，客户端带旧 ETag 来验证时应得到 304 而不是整份重传。
        """
        if name.endswith(".gz") or resp.status_code != 200:
            return None
        if not any(k == b"accept-encoding" and _accepts_gzip(v) for k, v in scope.get("headers", [])):
            return None
        try:
            gz_stat = os.stat(f"{name}.gz")
        except OSError:
            return None
        if not stat.S_ISREG(gz_stat.st_mode):
            return None
        media, _ = mimetypes.guess_type(name)
        out = FileResponse(f"{name}.gz", stat_result=gz_stat, media_type=media)
        for h in ("etag", "last-modified", "cache-control"):
            if h in resp.headers:
                out.headers[h] = resp.headers[h]
        out.headers["Content-Encoding"] = "gzip"
        out.headers["Vary"] = "Accept-Encoding"
        return out


if config.WEB_DIST.exists():
    app.mount("/", DistFiles(directory=str(config.WEB_DIST), html=True), name="static")
else:

    @app.get("/")
    async def no_frontend():
        return JSONResponse(status_code=404, content={"error": "前端未构建，请先在 web/ 执行 npm run build"})


def run():
    import uvicorn

    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        proxy_headers=config.TRUST_PROXY,
        forwarded_allow_ips="*" if config.TRUST_PROXY else None,
        log_level="info" if config.LOG else "warning",
        # uvicorn 的访问日志打印的是原始 request target，包含整串查询参数：
        # 已在本机实测 `INFO: 127.0.0.1:x - "GET /x?token=SECRET123 HTTP/1.1" 200 OK`
        # 原样进入 stdout。面板的下载/EventSource/WebSocket 走 ?token= 鉴权，
        # 于是 CP_LOG=1 时长期凭证会被写进 journald（同机可读、按保留策略留存）。
        # 面板自己在 AuthMiddleware 里记了一条只含 path 的访问日志，关掉这份不会失去可观测性。
        access_log=False,
        server_header=False,
        date_header=False,
    )


if __name__ == "__main__":
    run()
