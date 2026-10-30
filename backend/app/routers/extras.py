import re
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from .. import aio_db, avatars, security, totp
from .. import database as dbm
from ..deps import need_admin as _need_admin
from ..errors import err_json
from ..services import alerts_service, nginx_ops, panel_entry_service
from ..util import SEARCH_Q_MAX, is_http_url, like_contains, list_ladder, parse_cursor, parse_limit, redact_creds

router = APIRouter()

# 每应用覆写探活期望状态码：key 里带应用名，所以单独一条规则，且只允许安全字符
PROBE_OVERRIDE_KEY = re.compile(
    rf"^{re.escape(alerts_service.PROBE_EXPECT_PREFIX)}([\w.@-]{{1,64}})$")

# 「面板认识哪些告警配置键」只有 alerts_service 的 ALERT_LIMITS + ALERT_TEXT_MAX。
# 这里过去另写两份键名——一条正则的 alternation 当白名单、一份列表当读取范围——
# 新增一个键要三处一起改才生效，漏哪一处都不报错：漏读取列表就是「网页改了没反应」，
# 漏白名单就是 POST 静默丢掉。所以这里不许再出现任何 `alert_` 键名，
# 白名单和读取列表都在请求时向 `alert_keys()` 要（不是 import 期快照，
# test_batch50b 会当场往登记表里加一个键，验证路由真的跟着走）。
# 取值约束同样只剩一处：必须在保存时就校验——`int(dbm.get_setting("alert_disk_pct") or 85)`
# 在巡检里，存进 "abc" 之后每次巡检都抛 ValueError，被循环吞掉，表现为「告警从此不再触发」，
# 而网页端一切正常，最难查的那种坏法。
# 区间不在这里定义：`ALERT_LIMITS` 是巡检读取时用的那一份，这里再抄一份 (lo, hi) 的话，
# 两边一旦不同步就会出现「这个数页面让你存、巡检却按钳制后的值办事」。
ALERT_INT_RANGE = {k: (lo, hi) for k, (_, lo, hi) in alerts_service.ALERT_LIMITS.items()}
ALERT_MAX_LEN = alerts_service.ALERT_TEXT_MAX


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
    # totp 只回"开没开"这个布尔（SQLite 的 1/0），密钥本身不出库：用户列表是 admin 日常
    # 会展开看的表，把 20 字节 base32 放进响应等于把每个账号的第二因子交给任何能读到
    # 这个接口的人（以及浏览器历史、审计日志里的一次误贴）。
    return dbm.query("SELECT id,username,role,nickname,avatar,created_at,totp_secret<>'' AS totp"
                     " FROM users ORDER BY id")


@router.post("/api/users")
async def users_create(req: Request):
    if r := _need_admin(req):
        return r
    body = await _body(req)
    username = str(body.get("username") or "")
    password = body.get("password")
    if not security.USERNAME_RE.match(username):
        return JSONResponse(status_code=400, content={"error": "用户名不合法"})
    pw_err = security.password_error(password)
    if pw_err:
        return JSONResponse(status_code=400, content={"error": pw_err})
    role = body.get("role") or "admin"
    if role not in security.ROLES:
        # 不能写成 `"viewer" if role == "viewer" else "admin"`：脚本里把 role 打成
        # "readonly" 时请求照样 200，账号却被静默升成 admin——降权笔误变成提权。
        return JSONResponse(status_code=400, content={"error": f"角色只能是 {'、'.join(security.ROLES)}"})
    # scrypt 放线程里：async 处理函数里直接算会卡住整个事件循环（见 main.py 登录）
    pw_hash = await security.hash_pass_async(password)
    try:
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (username, pw_hash, role),
        )
    except sqlite3.IntegrityError:
        # 只把「唯一约束冲突」解释成用户名已存在。
        # 旧写法是 except Exception：磁盘满、库被锁、WAL 写不进都会显示
        # 「用户名已存在」（实测），管理员反复换个名字还是这句，日志里也查不到，
        # 属于最难排查的一类误报。其余异常交给全局处理器变 500。
        return JSONResponse(status_code=400, content={"error": "用户名已存在"})
    await aio_db.audit(req.state.cp_sub, "user:create", f"{username}({role})")
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
        if body["role"] not in security.ROLES:
            # 同 users_create：`"viewer" if x=="viewer" else "admin"` 会把笔误的
            # "readonly" 当成"不是 viewer"，于是降权请求实际把人升成 admin 还返回 200。
            return JSONResponse(status_code=400, content={"error": f"角色只能是 {'、'.join(security.ROLES)}"})
        new_role = body["role"]
        if new_role != u["role"] and u["username"] == req.state.cp_sub:
            return JSONResponse(status_code=400, content={"error": "不能修改自己的角色"})
        if new_role == u["role"]:
            new_role = None  # 传了等于没传：不算变更，免得一次空保存把人踢下线
    new_hash = None
    if body.get("password"):
        pw_err = security.password_error(body["password"])
        if pw_err:
            return JSONResponse(status_code=400, content={"error": pw_err})
        new_hash = await security.hash_pass_async(body["password"])
    new_nickname = None
    if "nickname" in body:
        new_nickname = str(body["nickname"] or "").strip()[:32]
    new_avatar = None
    if "avatar" in body:
        new_avatar = str(body["avatar"] or "").strip()[:256]

    if new_role is None and new_hash is None and new_nickname is None and new_avatar is None:
        await aio_db.audit(req.state.cp_sub, "user:update", f"{u['username']}(无变更)")
        return {"ok": True}

    if new_role is not None:
        dbm.execute("UPDATE users SET role=? WHERE id=?", (new_role, u["id"]))
    if new_hash is not None:
        dbm.execute("UPDATE users SET pass_hash=? WHERE id=?", (new_hash, u["id"]))
    if new_nickname is not None:
        dbm.execute("UPDATE users SET nickname=? WHERE id=?", (new_nickname, u["id"]))
    if new_avatar is not None:
        dbm.execute("UPDATE users SET avatar=? WHERE id=?", (new_avatar, u["id"]))
    # role 与 password 都只存在于已签发的 token 里（中间件读的是 token 的 role，
    # 不是库里的），改完必须吊销旧 token，否则把 admin 降成 viewer 后
    # 对方手上的旧 token 在 TTL 内（最长 72h）依旧是 admin——连写操作都拦不住（实测）。
    # 昵称与头像不在 token 里（票面只有 sub/role/ver），跟着一起吊销就是误伤：
    # 管理员只想改个显示名，对方下一个请求即 401、网页终端被 4401 关掉，
    # 而审计里只留下一条看不出原因的 user:update。
    if new_role is not None or new_hash is not None:
        security.bump_user_epoch(u["username"])
    # 审计里写清改了哪几项：一条光写人名的 user:update，事后既看不出管理员
    # 有没有把某个人升成 admin，也看不出是谁把谁的口令重置了。
    # 只记字段名——口令、头像路径这些值本身不进审计表。
    changed = ",".join(
        name for name, val in (
            ("role", new_role), ("password", new_hash), ("nickname", new_nickname), ("avatar", new_avatar),
        ) if val is not None
    )
    await aio_db.audit(req.state.cp_sub, "user:update", f"{u['username']}({changed})" if changed else u["username"])
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
    await run_in_threadpool(avatars.purge, u["id"])
    security.bump_user_epoch(u["username"])
    # 已用口令的步数桶是按用户名记的，删号不清就等于把"这个人名下用过哪一步"留给了
    # 下一个同名账号（⑤L 的同名继承口径）：新建同名用户后，第一枚撞上旧步数的口令会被当重放拒掉。
    totp.forget_used(u["username"])
    await aio_db.audit(req.state.cp_sub, "user:delete", u["username"])
    return {"ok": True}


@router.get("/api/settings/alerts")
async def alerts_get(req: Request):
    keys = alerts_service.alert_keys()
    secret_keys = alerts_service.ALERT_SECRET_KEYS
    rows = await aio_db.query(
        "SELECT key,value FROM settings WHERE key IN ({}) OR key LIKE ?".format(
            ",".join("?" * len(keys))),
        [*keys, f"{alerts_service.PROBE_EXPECT_PREFIX}%"],
    )
    stored = {r["key"]: (r["value"] or "") for r in rows}
    out = {k: ("" if req.state.cp_role != "admin" and k in secret_keys else stored.get(k, ""))
           for k in keys}
    # 按应用覆写的期望状态码：值里没有秘密，两种角色都可以看到当前覆写了哪些应用。
    pfx = alerts_service.PROBE_EXPECT_PREFIX
    out["probe_overrides"] = {key[len(pfx):]: val
                              for key, val in stored.items() if key.startswith(pfx)}
    # 前缀本身也给页面：拼键名这件事由后端的答案来，不在 web 里再抄一份。
    out[alerts_service.PROBE_PREFIX_FIELD] = pfx
    # 默认值与可填区间随载荷一起给出（出处是 alerts_service.ALERT_LIMITS）：
    # 库里没写过某个键时，页面要显示的那个「默认」必须就是巡检实际使用的那个默认，
    # 输入框要拦的那条区间必须就是保存校验用的那条区间。
    out["limits"] = alerts_service.alert_limits()
    # 「要紧档」比配置线高的那 10 个点只在服务层算一次，这里把它当事实告诉页面，
    # 页面才能解释为什么自己配了 80 却在 90 才看见红仪表。
    out["crit_step"] = alerts_service.CRIT_STEP
    return out


def _validate_alert(k: str, val: str) -> str | None:
    """返回错误信息；None 表示通过。空值等于关闭该项，直接放行。"""
    if val == "":
        return None
    if k.startswith(alerts_service.PROBE_EXPECT_PREFIX):
        # 期望状态码只有 "2xx/3xx"、"200 204" 这一种形状，内容看不懂不影响主机健康
        # （服务侧会回落 2xx/3xx），所以不在这里做语义校验。长度上限问服务层要（批次㊿E）：
        # 这里以前是另一份写死的 32，而 `alert_` 键名不许出现在路由里（㊿B），所以拿的是一个函数。
        maxlen = alerts_service.probe_expect_max()
        if len(val) > maxlen:
            return f"{k} 超过 {maxlen} 字符"
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
    if k in alerts_service.ALERT_URL_KEYS and not is_http_url(val):
        return "Webhook URL 必须以 http:// 或 https:// 开头"
    # 邮件这一组的语义（哪个键是主机、哪个键是地址列表）只有服务层认识，
    # 键名字面量不许出现在路由里（㊿B），所以这里问的是一个函数要答案。
    return alerts_service.extra_validate(k, val)


@router.post("/api/settings/alerts")
async def alerts_save(req: Request):
    if r := _need_admin(req):
        return r
    body = await _body(req)
    accepted: dict[str, str] = {}
    errors: list[str] = []
    for k, v in body.items():
        m = PROBE_OVERRIDE_KEY.match(k)
        if k not in alerts_service.alert_keys() and not m:
            continue
        val = "" if v is None else str(v).strip()
        if m and val:
            # 应用名写错了这条覆写永远不会生效（服务侧按名字取），当场拒绝比留下幽灵键好
            name = m.group(1)
            known = await aio_db.query_one("SELECT 1 ok FROM apps WHERE name=?", (name,))
            if not known:
                errors.append(f"探活覆写的应用不存在：{name}")
                continue
        if err := _validate_alert(k, val):
            errors.append(err)
            continue
        accepted[k] = val
    # 先全部校验再落库：部分写入会让通道配置变成"一半新一半旧"的不可解释状态
    if errors:
        # 校验不过时一个字段都没写，也就没有"变更"可记。旧写法先落一条
        # settings:alerts 审计再返回 400，事后翻审计会以为配置真的改过。
        return JSONResponse(status_code=400, content={"error": "；".join(errors), "code": "bad_input"})
    for k, val in accepted.items():
        dbm.set_setting(k, val)
    await aio_db.audit(req.state.cp_sub, "settings:alerts", ",".join(accepted.keys()) or "-")
    return {"ok": True}


@router.post("/api/settings/alerts/test")
async def alerts_test(req: Request):
    if r := _need_admin(req):
        return r
    delivered = await alerts_service.notify_test()
    # 这条也要写审计：它往管理员的邮箱/飞书里真发一条消息，而下面那条
    # `run-checks` 同样会发通知、早就记了。测试通知事后查不到是谁发的，
    # 就等于"谁在半夜把告警发到老板手机上"这个问题没有答案。
    await aio_db.audit(req.state.cp_sub, "settings:alert-test", f"delivered={delivered}")
    if not delivered:
        return JSONResponse(status_code=400, content={"error": "没有可用通道，或全部投递失败（详见面板日志）"})
    return {"ok": True}


@router.post("/api/settings/alerts/run-checks")
async def alerts_run_checks(req: Request):
    if r := _need_admin(req):
        return r
    ran = await alerts_service.run_checks()
    # 手动跑一轮巡检会真的发通知，事后必须查得到是谁在什么时候点过
    await aio_db.audit(req.state.cp_sub, "settings:alert-checks",
                       "manual" + (f"({ran['skipped']})" if ran.get("skipped") else ""))
    return {"ok": True, **({"skipped": ran["skipped"]} if ran.get("skipped") else {})}


@router.get("/api/audit")
async def audit_list(req: Request):
    if r := _need_admin(req):
        return r
    qp = req.query_params
    # LEFT JOIN 取昵称：audit.username 存的是登录名，而用户改完昵称后希望审计里看到的是昵称；
    # 用户被删掉后审计仍需保留（登录名查不到人），LEFT JOIN 而不是 JOIN 就是为了这个。
    limit = parse_limit(qp.get("limit"))
    # 过滤必须在服务端做：页面一屏只拿得到 limit 行，在客户端过滤的话「更早的 500 行之外」
    # 永远搜不到，还会如实显示成「没有匹配的记录」——把「没搜到这一屏」说成「没有」。
    q = (qp.get("q") or "").strip()[:SEARCH_Q_MAX]
    before, cursor_err = parse_cursor(qp.get("before"))
    if cursor_err:
        return JSONResponse(status_code=400, content={"error": cursor_err, "code": "bad_cursor"})
    where: list[str] = []
    args: list = []
    if before is not None:
        where.append("a.id < ?")
        args.append(before)
    if q:
        pat = like_contains(q)
        where.append("(a.username LIKE ? ESCAPE '\\' OR u.nickname LIKE ? ESCAPE '\\'"
                     " OR a.action LIKE ? ESCAPE '\\' OR a.detail LIKE ? ESCAPE '\\')")
        args += [pat] * 4
    tail = (" FROM audit a LEFT JOIN users u ON u.username=a.username"
            + (" WHERE " + " AND ".join(where) if where else ""))
    # 多取一条判「还有没有更早的」：count 是给文案用的真实匹配数，
    # 不能拿本页行数冒充。
    rows = await run_in_threadpool(
        dbm.query,
        "SELECT a.id,a.username,a.action,a.detail,a.created_at,u.nickname,u.avatar" + tail
        + " ORDER BY a.id DESC LIMIT ?",
        (*args, limit + 1),
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    total = await run_in_threadpool(dbm.query_one, "SELECT COUNT(*) AS n" + tail, tuple(args))
    # 原来是裸数组：一个会截断你的接口连「截断到多少」都没地方说，
    # 页面只好自己抄一份 limit 与档位（[100,200,500]）。现在跟 /api/alerts/history 同一形状。
    return {"rows": rows, "limit": limit, "ladder": list_ladder(),
            "q": q, "matched": total["n"] if total else None, "hasMore": has_more}


@router.post("/api/system/certbot-renew")
async def certbot_renew(req: Request):
    if r := _need_admin(req):
        return r
    try:
        res = await nginx_ops.renew_certs()
    except Exception as e:  # noqa: BLE001
        return err_json(e)
    # 审计写在执行之后：续期失败也先记一条"已执行"，事后无法区分成功与失败。
    # detail 记的是续期结论（几张/没几张），只记"已执行"的话，事后翻审计
    # 仍然分不清这次到底是续到了还是压根没到期。
    await aio_db.audit(req.state.cp_sub, "certbot:renew", res["summary"])
    # certbot 输出里可能带 --key-path 之类的参数值，出接口前统一脱敏；
    # renewed/unchanged/summary 是给界面直接用的，log 只在失败排查时看。
    return {"ok": True, **res, "log": redact_creds(res["log"])[:4000]}


@router.get("/api/system/panel-entry")
async def panel_entry(req: Request):
    """面板自己的对外入口。只读，admin 才能看：域名本身就是攻击面信息。"""
    if r := _need_admin(req):
        return r
    return await panel_entry_service.detail()


@router.post("/api/system/panel-entry/domain")
async def panel_entry_set_domain(req: Request):
    if r := _need_admin(req):
        return r
    body = await _body(req)
    domain = str(body.get("domain") or "").strip()
    try:
        res = await panel_entry_service.change_domain(domain)
    except panel_entry_service.PanelEntryError as e:
        return JSONResponse(status_code=400, content={"error": str(e), "code": "panel_entry_refused"})
    if res.get("changed"):
        # 纳管登记表里那条「面板入口」挂在旧域名上：不搬的话站点页会一直列一个
        # 已经不由面板服务的域名，而新域名显示成未纳管。只搬 kind=panel 的行。
        old = res.get("oldDomains") or []
        if old:
            marks = ",".join("?" * len(old))
            row = await aio_db.query_one(
                f"SELECT domain,file,source,note FROM site_claims WHERE kind='panel' AND domain IN ({marks})",
                tuple(old))
            if row:
                await aio_db.execute(
                    f"DELETE FROM site_claims WHERE kind='panel' AND domain IN ({marks})", tuple(old))
                # 新域名在登记表里另有归属时不动它：那条可能是应用认领的，
                # 用面板入口把它顶掉等于悄悄改掉别人的归属。
                taken = await aio_db.query_one("SELECT kind FROM site_claims WHERE domain=?", (res["domain"],))
                if not taken:
                    await aio_db.execute(
                        "INSERT OR REPLACE INTO site_claims(domain,app_id,file,kind,source,note)"
                        " VALUES(?,NULL,?,'panel',?,?)",
                        (res["domain"], row["file"], row["source"], row["note"]))
        await aio_db.audit(req.state.cp_sub, "settings:panel-domain",
                           f"{'/'.join(old) or '-'} -> {res['domain']}")
    return res
