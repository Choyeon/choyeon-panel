"""站点纳管登记：域名归属由一条登记说了算，不再由端口猜出来。

为什么要有这张表：反代端口恰好等于某个应用登记的端口，是**巧合**而不是归属。本机上
两类巧合同时存在——两个应用共用一个端口，以及应用登记的端口与 nginx 里写的端口对不上
（choyeon-video 登记 3100，配置指向 3300）。按端口 join 会把域名安到错误的应用上，
所以面板此前的取舍是"宁可不认"，代价是所有手工写的 vhost 永远显示「未纳管的手工配置」，
面板对它们既说不出归属，也没法把它们和应用的启停、告警放在一起看。

纳管做什么、不做什么：
① 写一条 domain → app（或 domain → 面板自己）的登记，并**认领**它所在的那份配置文件；
② 不重写文件内容。手工 vhost 是多 server 块 + alias 跳转 + certbot 注入的证书行，
   不是 `render_vhost` 的形状，套一遍等于把别人的配置改回面板模板；
③ 纳管之后要改这份文件，走的还是 ⑤H 那条临界区（原子写 → nginx -t → 失败回滚 → reload），
   入口是应用详情页的 nginx 编辑器（`assert_app_conf` 按域名/反代端口放行，
   认领过的那份必然命中，所以纳管本身不会多开一条绕过校验的写路径）。

取消纳管只删这一行登记，nginx 的配置一个字节都不动——域名照旧服务。
"""

from __future__ import annotations

import asyncio

from .. import aio_db, config
from ..util import is_domain
from . import apps_service, nginx_ops, systemd_ops
from .nginx_ops import LOCAL_TARGETS

KIND_APP = "app"
KIND_PANEL = "panel"
SOURCE_MANUAL = "manual"
SOURCE_AUTO = "auto"
PANEL_LABEL = "面板入口"

NO_TARGET = ("这个域名的 server 块里没有指向本机的 proxy_pass（静态站或只做跳转），"
             "按端口判不出归属；可以在纳管时手工指定应用")
UNKNOWN_DOMAIN = "nginx 实际加载的配置里没有这个域名的 server 块，纳管只会登记一条查不到出处的归属"


def valid_domain(domain) -> str:
    return str(domain or "").strip().lower()


def port_index(apps: list[dict]) -> dict[int, list[dict]]:
    """端口 → 应用列表。共用端口留着不剔除，判定时如实说"归属不唯一"。"""
    out: dict[int, list[dict]] = {}
    for a in apps:
        if isinstance(a.get("port"), int):
            out.setdefault(a["port"], []).append(a)
    return out


def domain_index(apps: list[dict]) -> dict[str, dict]:
    """apps.domain 是应用自己登记的域名，本身就是归属证据（不是猜出来的）。"""
    out: dict[str, dict] = {}
    for a in apps:
        d = valid_domain(a.get("domain"))
        if d:
            out.setdefault(d, a)
    return out


def file_stem(fname: str) -> str:
    """conf.d 的 `panel-foo.conf` → `foo`：面板自己生成的文件按应用名前缀命名。"""
    name = str(fname or "")
    if name.endswith(".conf"):
        name = name[: -len(".conf")]
    if name.startswith("panel-"):
        name = name[len("panel-"):]
    return name


def domain_facts(configs: list[dict]) -> dict[str, dict]:
    """domain → 它在 nginx 里由哪份文件、哪些 server 块服务，根路径反代去哪儿。

    同一个域名通常出现在两块（80 跳转 + 443 主块），归属看**有反代目标的那一块**，
    文件名也记那一份：站点列表点「查看配置」指的必须是真正服务这个域名的文件。
    键统一小写：登记表里的域名按 `valid_domain` 存成小写，两边不同口径的话，
    `server_name Blog.Chooyeon.cc` 这种写法会让登记永远对不上配置。
    """
    facts: dict[str, dict] = {}
    for cfg in configs:
        fname = str(cfg.get("name") or "")
        for blk in cfg.get("blocks") or []:
            for raw in blk.get("serverNames") or []:
                dom = valid_domain(raw)
                if not dom:
                    continue
                f = facts.get(dom)
                if f is None:
                    f = facts[dom] = {
                        "file": fname, "files": [], "https": False, "redirect": False,
                        "websocket": False, "ports": [], "target": None, "redirect_host": None,
                    }
                if fname and fname not in f["files"]:
                    f["files"].append(fname)
                f["https"] = f["https"] or bool(blk.get("ssl"))
                f["redirect"] = f["redirect"] or bool(blk.get("httpsRedirect"))
                f["websocket"] = f["websocket"] or bool(blk.get("websocket"))
                for p in blk.get("ports") or []:
                    if p not in f["ports"]:
                        f["ports"].append(p)
                if f["target"] is None and blk.get("defaultTarget"):
                    f["target"] = blk["defaultTarget"]
                    f["file"] = fname
                if f["redirect_host"] is None:
                    raw = str(blk.get("redirectTarget") or "")
                    if raw and "$" not in raw and raw != dom:
                        f["redirect_host"] = raw
    return facts


def judge(facts: dict[str, dict], apps: list[dict]) -> dict[str, dict]:
    """逐域名判定归属：判不出的 kind 为 None 并给出可以直接展示的理由。

    证据强度依次是：反代到面板自身端口（那是面板入口，永远不算应用）＞ 应用登记的域名 ＞
    根路径反代端口（且该端口只属于一个应用）＞ 配置文件名与应用名同名 ＞ 跟随 301 目标域名。
    每条都要在 note 里点明依据，因为「全部纳管」的结果必须能被逐行质疑。
    """
    by_port = port_index(apps)
    by_domain = domain_index(apps)
    by_name: dict[str, list[dict]] = {}
    for a in apps:
        name = str(a.get("name") or "")
        if name:
            by_name.setdefault(name, []).append(a)

    out = {dom: _judge_one(dom, f, by_port, by_domain, by_name, config.PORT)
           for dom, f in facts.items()}
    # 第二遍：www / blog 这类只做别名跳转的块，跟随它 301 去的那个域名。
    # 必须放在第一遍之后——目标域名先判完才知道有没有可跟随的归属。
    for dom, f in facts.items():
        v = out[dom]
        if v["kind"] is not None:
            continue
        hit = out.get(f.get("redirect_host") or "")
        if hit and hit["kind"] == KIND_APP and hit["app"]:
            out[dom] = _verdict(KIND_APP, {"id": hit["app_id"], "name": hit["app"], "port": hit["app_port"]},
                                f, f"跟随 {f['redirect_host']} 的 301 归属（这个块只做别名跳转）")
    return out


def _verdict(kind: str | None, app: dict | None, fact: dict, note: str, reason: str = "") -> dict:
    return {
        "kind": kind,
        "app_id": app.get("id") if app else None,
        "app": app.get("name") if app else None,
        "app_port": app.get("port") if app else None,
        "file": fact.get("file") or "",
        "note": note,
        "reason": reason,
    }


def _judge_one(dom, f, by_port, by_domain, by_name, panel_port) -> dict:
    t = f.get("target")
    local_port = None
    if t and str(t.get("host") or "").strip().lower() in LOCAL_TARGETS and isinstance(t.get("port"), int):
        local_port = t["port"]
    if local_port == panel_port:
        return _verdict(KIND_PANEL, None, f, f"根路径反代 127.0.0.1:{panel_port}，这是面板自己的入口")
    own = by_domain.get(dom)
    if own:
        return _verdict(KIND_APP, own, f, "应用登记的域名就是它")
    if local_port is not None:
        hits = by_port.get(local_port) or []
        if len(hits) == 1:
            return _verdict(KIND_APP, hits[0], f, f"根路径反代 127.0.0.1:{local_port}")
        if len(hits) > 1:
            names = "、".join(str(a.get("name")) for a in hits)
            return _verdict(None, None, f, "",
                             f"根路径反代 127.0.0.1:{local_port}，但 {local_port} 由 {names} 共用，"
                             "归属不唯一；请手工指定应用")
        return _verdict(None, None, f, "",
                        f"根路径反代 127.0.0.1:{local_port}，面板里没有应用登记这个端口；请手工指定应用")
    named = by_name.get(str(f.get("file") or "")) or by_name.get(file_stem(str(f.get("file") or "")))
    if named and len(named) == 1:
        return _verdict(KIND_APP, named[0], f, f"配置文件名 {f.get('file')} 与该应用同名")
    return _verdict(None, None, f, "", NO_TARGET)


def _merge_row(dom: str, f: dict, v: dict, row: dict | None, by_id: dict) -> dict:
    """把一个域名的三份事实合成一行：登记表说了算，登记表没有才用判定结果。"""
    target = f.get("target") or {}
    port = target["port"] if isinstance(target.get("port"), int) else (f.get("ports") or [None])[0]
    if row:
        kind = str(row.get("kind") or KIND_APP)
        app_id = row.get("app_id") if kind == KIND_APP else None
        app = by_id.get(app_id) if app_id is not None else None
        claimed, source, note = True, row.get("source"), row.get("note")
        # 认领的那份文件不在当前加载的配置里 = 对象已经没了（改名、删除、链接没启用）。
        stale = bool(row.get("file")) and row["file"] not in (f.get("files") or [])
        name = (app or {}).get("name") if kind == KIND_APP else (PANEL_LABEL if kind == KIND_PANEL else None)
        app_port = (app or {}).get("port") if kind == KIND_APP else None
        file = row.get("file") or v.get("file") or f.get("file") or ""
    else:
        kind = v["kind"] or ""
        app_id = v["app_id"]
        claimed, source, stale = False, None, False
        note = v["note"]
        name = PANEL_LABEL if kind == KIND_PANEL else v["app"]
        app_port = v["app_port"]
        file = v.get("file") or f.get("file") or ""
    return {
        "domain": dom,
        "app": name,
        "app_id": app_id if kind == KIND_APP else None,
        "port": port,
        "app_port": app_port,
        "port_mismatch": bool(name and kind == KIND_APP
                              and isinstance(app_port, int) and isinstance(port, int) and app_port != port),
        "https": bool(f.get("https")),
        "redirect": bool(f.get("redirect")),
        "websocket": bool(f.get("websocket")),
        "file": file,
        "files": f.get("files") or [],
        "active": None,
        "claimed": claimed,
        "claim_kind": kind,
        "claim_source": source,
        "claim_note": note,
        "claim_stale": stale,
        "suggest_kind": v["kind"] or "",
        "suggest_app": v["app"],
        "suggest_app_id": v["app_id"],
        "suggest_note": v["note"],
        "suggest_reason": v["reason"],
    }


def build_rows(configs: list[dict], apps: list[dict], reg: dict[str, dict]) -> list[dict]:
    """站点列表的行：nginx 里看到的域名 ∪ 登记表里的域名。

    登记表多出来的那些（配置删了还没解绑）也必须出现，否则那行登记在页面上没有任何入口
    能看见，只能去命令行删。
    """
    facts = domain_facts(configs)
    judged = judge(facts, apps)
    by_id = {a.get("id"): a for a in apps}
    return [_merge_row(dom, facts.get(dom) or {}, judged.get(dom) or _verdict(None, None, {}, "", NO_TARGET),
                       reg.get(dom), by_id)
            for dom in sorted(set(facts) | set(reg))]


def attribution(configs: list[dict], apps: list[dict], reg: dict[str, dict]) -> dict[str, dict]:
    """domain → 认得归属的那几行。链路对账的「应用」列读这里，不再自己按端口猜第二次。"""
    return {r["domain"]: r for r in build_rows(configs, apps, reg)
            if r["app"] or r["claim_kind"] == KIND_PANEL}


async def claims() -> dict[str, dict]:
    rows = await aio_db.query("SELECT domain,app_id,file,kind,source,note FROM site_claims")
    return {str(r["domain"]): r for r in rows}


async def app_rows() -> list[dict]:
    """站点这一层读应用的唯一口径：`id` 必须在列，纳管登记按 id 认应用，改名不会把归属带走。"""
    return await aio_db.query("SELECT id,name,port,domain,unit_override FROM apps ORDER BY name")


async def _snapshot() -> dict:
    """一次读齐三份事实（nginx 配置 / apps / 登记表），页面与三个写动作共用同一份取数。"""
    configs, apps = await asyncio.gather(
        asyncio.to_thread(nginx_ops.loaded_configs),
        app_rows(),
    )
    return {"configs": configs, "apps": apps, "reg": await claims()}


def sort_rows(rows: list[dict]) -> list[dict]:
    """已纳管的排前面并按应用分组，判不出归属的按域名排在后面。"""
    return sorted(rows, key=lambda r: (r["app"] is None, r["app"] or r["domain"], r["domain"]))


def site_domains() -> tuple[set[str], str | None]:
    """nginx 里在服务的域名集合，纯文件读：概览条的站点计数用它。

    计数以前抄了一份只取 server_name 的独立扫描，与站点页那份解析各走各的，
    两处对「有几个站」的答案会分叉；现在同一个读者。
    """
    if not nginx_ops.config_dirs_present():
        return set(), "未检测到 nginx 配置目录"
    return set(domain_facts(nginx_ops.loaded_configs())), None


async def sites_rows() -> tuple[list[dict], str | None]:
    """GET /api/sites 的全部内容都在这里合成，路由只负责把错误翻译成人话。"""
    if not nginx_ops.config_dirs_present():
        return [], "未找到 nginx 配置目录（本机可能未安装 nginx，或 CP_NGINX_CONF_DIRS 指向了不存在的路径）"
    snap = await _snapshot()
    rows = sort_rows(build_rows(snap["configs"], snap["apps"], snap["reg"]))
    by_id = {a["id"]: a for a in snap["apps"]}
    units = {apps_service.unit_name(a): a for a in snap["apps"]}
    states = await systemd_ops.unit_states(list(units))
    for r in rows:
        app = by_id.get(r["app_id"]) if r["app_id"] is not None else None
        if app is not None:
            r["active"] = bool((states.get(apps_service.unit_name(app)) or {}).get("active"))
    return rows, None


async def _register(dom: str, file: str, kind: str, app_id, source: str, note: str) -> None:
    await aio_db.execute(
        """INSERT INTO site_claims(domain,app_id,file,kind,source,note) VALUES(?,?,?,?,?,?)
           ON CONFLICT(domain) DO UPDATE SET
             app_id=excluded.app_id, file=excluded.file, kind=excluded.kind,
             source=excluded.source, note=excluded.note, created_at=datetime('now')""",
        (dom, app_id, file, kind, source, note),
    )


def _decide(snap: dict, dom: str, app_id) -> dict:
    """给出这条登记要落的内容；判不出归属又没带 app_id 时抛错，不替用户猜。"""
    facts = domain_facts(snap["configs"])
    f = facts[dom]
    v = judge(facts, snap["apps"])[dom]
    if app_id is None:
        if v["kind"] is None:
            raise RuntimeError(v["reason"] or NO_TARGET)
        return {"kind": v["kind"], "app_id": v["app_id"] if v["kind"] == KIND_APP else None,
                "app": None if v["kind"] == KIND_PANEL else v["app"],
                "file": f.get("file") or "", "note": v["note"], "source": SOURCE_AUTO}
    app = next((a for a in snap["apps"] if a["id"] == int(app_id)), None)
    if not app:
        raise RuntimeError("应用不存在")
    note = f"手工指定为应用 {app['name']}"
    if v["kind"] == KIND_PANEL:
        note += f"（面板原判定：{v['note']}）"
    elif v["app_id"] is not None and v["app_id"] != app["id"]:
        note += f"（面板判定为 {v['app']}：{v['note']}）"
    return {"kind": KIND_APP, "app_id": app["id"], "app": app["name"],
            "file": f.get("file") or "", "note": note, "source": SOURCE_MANUAL}


async def claim(domain, app_id=None, snap: dict | None = None) -> dict:
    """登记归属并认领配置文件。**不写 nginx 配置、不 reload**。

    app_id 留空＝按判定结果落；判不出归属时如实抛错，让调用方带上 app_id 手工指定。
    """
    dom = valid_domain(domain)
    if not dom or not is_domain(dom):
        raise RuntimeError("域名不合法")
    snap = snap or await _snapshot()
    if dom not in domain_facts(snap["configs"]):
        raise RuntimeError(UNKNOWN_DOMAIN)
    d = _decide(snap, dom, app_id)
    await _register(dom, d["file"], d["kind"], d["app_id"], d["source"], d["note"])
    return {"domain": dom, "app": d["app"], "kind": d["kind"], "file": d["file"],
            "note": d["note"], "source": d["source"]}


async def unclaim(domain) -> dict:
    """只删这一行登记：nginx 的文件一个字节都不动，域名照旧由原配置服务。"""
    dom = valid_domain(domain)
    if not dom:
        raise RuntimeError("域名不合法")
    cur = await aio_db.query_one("SELECT domain FROM site_claims WHERE domain=?", (dom,))
    await aio_db.execute("DELETE FROM site_claims WHERE domain=?", (dom,))
    return {"domain": dom, "removed": bool(cur)}


async def claim_all(snap: dict | None = None) -> dict:
    """逐条判定的批量纳管：判得出归属的落登记，判不出的原样列出理由，一个都不猜。"""
    snap = snap or await _snapshot()
    facts = domain_facts(snap["configs"])
    judged = judge(facts, snap["apps"])
    claimed: list[dict] = []
    skipped: list[dict] = []
    for dom in sorted(set(facts) - set(snap["reg"])):
        if judged[dom]["kind"] is None:
            skipped.append({"domain": dom, "reason": judged[dom]["reason"] or NO_TARGET})
            continue
        d = _decide(snap, dom, None)
        await _register(dom, d["file"], d["kind"], d["app_id"], d["source"], d["note"])
        claimed.append({"domain": dom, "app": d["app"], "kind": d["kind"],
                        "file": d["file"], "note": d["note"]})
    # 判不出归属的域名总数（这一轮跳过的 + 之前手工登过但依旧判不出的），给页面当"还剩几个"
    unmanaged = {d for d, v in judged.items() if v["kind"] is None}
    return {"claimed": claimed, "skipped": skipped, "unmanaged": len(unmanaged)}


async def drop_app(app_id: int) -> int:
    """应用被删除时把它名下的登记一起清掉（批次⑤L 的口径：对象没了，派生态不能留着）。

    不走 `aio_db.execute` 的返回值：那是 lastrowid，与"清掉了几行"无关。
    """
    rows = await aio_db.query("SELECT domain FROM site_claims WHERE app_id=?", (app_id,))
    if rows:
        await aio_db.execute("DELETE FROM site_claims WHERE app_id=?", (app_id,))
    return len(rows)
