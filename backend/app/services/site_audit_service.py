"""站点链路对账：一个域名从被访问到拿到响应，中间每一跳都得有人认领。

网站页、证书页和端口暴露面各答一段：站点列表说「nginx 里有这个 server_name」，证书页说
「这张证书还剩几天」，暴露面说「这个端口有没有人听」。三段各自都对，合起来仍然漏掉线上
最常见的那两类事故：

① 反代目标没人听。`nginx -t` 永远通过——它只校验语法，不校验 `127.0.0.1:3100` 上到底有没有
   进程。于是配置正确、服务在跑、证书有效，用户拿到的却是 502，而这三页全是绿的。
② server 块开了 ssl，证书却不含这个域名（签的时候漏了一个子名、多域名只签下一个），以及
   同一个文件里 80 也听着、却没有 301 跳转——明文照样进得去。
③ 默认档没人认领。Host 没匹配上任何 server_name 的请求（以及不带 SNI 的 TLS 握手）不会 404，
   而是落到「显式写了 default_server 的第一个块」，一处都没写时落到**加载顺序里第一个**听这个口
   的块。Debian 上 conf.d 先于 sites-enabled，所以给一个新站写配置就把全机未知 Host 的归属换掉了；
   若那个块还写着 `return 301 https://$host…`，任何解析到本机的域名都能借它跳到自己身上。

这一层不重新解析 nginx、不重新数端口、不重新算证书天数：配置走 `nginx_ops.loaded_configs()`
（与站点列表同一个读者），监听表走 `exposure_service`，证书清单走 `cert_ops`。这里只做 join
与判定，所以一次对账里出现的每个数字都能在别的页面上找到同一个出处。

严格只读：不 reload nginx、不改 vhost、不签证书、不重启应用。502 的成因大多在应用侧（进程
没起来、端口填错），面板替你 reload 或改配置只会把现场抹掉。处置一律给能复制走的只读命令。

三处如实承认局限，不装成全知：
① 只判本机看得见的目标。`proxy_pass` 指向别的机器时本机没有监听表可对照，标成未确认；判死
   会逼人在一个根本没错的地址上找问题。
② 监听表整个读不到（没有 ss、ss 失败、条目封顶）时，所有反代判定一起降为未确认，绝不折成
   「没人听」——那等于把一次探测失败说成一个不存在的故障。
③ 证书只判覆盖关系。到期天数归证书项，那里已经按 `alert_ssl_days` 判过一次；这里再判就是
   同一件事两个出处，阈值一改就分叉。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from .. import probe
from ..severity import SEV_INFO, SEV_RISK, SEV_WARN
from ..util import tallied
from . import cert_ops, exposure_service, nginx_ops, site_claim_service
from .nginx_ops import LOCAL_TARGETS

CACHE_TTL = 60.0
# 判词里点名点几条：与自检项那句同源，所以两处看到的名单是同一份。
NAME_LIMIT = 3
NOTE_LISTEN_UNKNOWN = "监听表这一轮读不到：反代目标是不是没人听，这里判不了"
NOTE_CERT_UNKNOWN = "证书清单这一轮读不到：域名与证书的覆盖关系判不了"


def _listen_state(exp: dict) -> tuple[bool, set[int], str]:
    """监听表：端口集合 + 这份集合能不能用来判「没人听」。"""
    ports: set[int] = set()
    for p in exp.get("ports") or []:
        try:
            ports.add(int(p["port"]))
        except (KeyError, TypeError, ValueError):
            continue
    if not exp.get("ss_available", True):
        return False, ports, "这台机器上没有 ss 或 ss 执行失败"
    if exp.get("ports_capped"):
        return False, ports, f"监听表条目已封顶在 {len(ports)} 条"
    return True, ports, ""


def _cert_state(certs: dict) -> tuple[bool, set[str], str]:
    """证书覆盖到的域名集合，以及这份清单到底读没读到。

    `source == "none"` 是 certbot 与证书目录都给不出东西（读不到），此时不能判某个域名缺证书；
    `certbot`/`openssl` 给出的空清单是确认的「本机没有证书」，照判。
    """
    covered: set[str] = set()
    for c in certs.get("certs") or []:
        for d in c.get("domains") or []:
            n = str(d).strip().lower()
            if n:
                covered.add(n)
    if (certs.get("source") or "none") == "none":
        return False, covered, certs.get("reason") or "证书清单读不到"
    return True, covered, ""


def _apps_of(link_domains: list[str], attributed: dict[str, dict]) -> str | None:
    """这份配置归哪个应用：一个文件里多个域名可能分属不同应用，那就都点名。

    归属一律取自纳管登记表（`site_claim_service.attribution`），这里不再自己按端口认第二次：
    过去两处各猜各的，站点页与链路对账会给同一个域名报出不同的应用。
    """
    names: list[str] = []
    for d in link_domains:
        n = (attributed.get(d) or {}).get("app")
        if n and n not in names:
            names.append(n)
    return "、".join(names) if names else None


def _domains_of(link: dict) -> str:
    """判词里怎么称呼这个 server 块：有域名报域名，没有就报文件名。"""
    doms = link.get("domains") or []
    if doms:
        return "、".join(doms[:2]) + ("…" if len(doms) > 2 else "")
    return str(link.get("file") or "（无名配置）")


def _targets(raw: list[dict], listen_ok: bool, listen_ports: set[int]) -> tuple[list[dict], list[int], list[str]]:
    """反代目标逐条定性：本机且监听表可用才判得死，其余一律 listening=None。"""
    rows: list[dict] = []
    dead: list[int] = []
    remote: list[str] = []
    for t in raw:
        host = str(t.get("host") or "")
        port = t.get("port")
        local = host.strip().lower() in LOCAL_TARGETS
        state = None
        if port is None:
            state = None
        elif not local:
            remote.append(f"{host}:{port}")
        elif not listen_ok:
            state = None
        else:
            state = int(port) in listen_ports
        if state is False:
            dead.append(int(port))
        rows.append({"host": host, "port": port, "local": local, "listening": state})
    return rows, dead, remote


def _build(configs: list[dict], exp: dict, certs: dict, apps: list[dict], dirs_present: bool,
           reg: dict[str, dict] | None, dflt: dict) -> dict:
    """join：每个 server 块一条链路 + 判词。判定全在这里，页面与 CLI 只渲染。"""
    listen_ok, listen_ports, listen_reason = _listen_state(exp)
    certs_ok, cert_names, certs_reason = _cert_state(certs)
    attributed = site_claim_service.attribution(configs, apps, reg or {})

    links: list[dict] = []
    dead_rows: list[dict] = []
    nocert_rows: list[dict] = []
    plain_rows: list[dict] = []
    remote_rows: list[str] = []
    foreign_rows: list[str] = []

    for cfg in configs:
        a = cfg.get("analysis") or {}
        domains = [str(d).strip().lower() for d in (a.get("serverNames") or []) if str(d).strip()]
        listen = sorted({int(p) for p in (a.get("listenPorts") or []) if p})
        ssl = bool(a.get("ssl"))
        targets, dead, remote = _targets(a.get("proxyTargets") or [], listen_ok, listen_ports)
        uncovered = [d for d in domains if d not in cert_names] if (ssl and domains and certs_ok) else []
        # 只有 80 与 443 都听着、又确实没有 301/308 到 https，才是「明文进得去」。
        no_redirect = ssl and 80 in listen and 443 in listen and not a.get("httpsRedirect")
        app = _apps_of(domains, attributed)
        link = {
            "file": str(cfg.get("name") or ""),
            "domains": domains,
            "listen": listen,
            "ssl": ssl,
            "redirect": bool(a.get("httpsRedirect")),
            "targets": targets,
            "dead": dead,
            "uncovered": uncovered,
            "no_redirect": no_redirect,
            "app": app,
        }
        links.append(link)
        remote_rows.extend(remote)
        if dead:
            dead_rows.append(link)
        if uncovered:
            nocert_rows.append(link)
        if no_redirect:
            plain_rows.append(link)
        if app is None and (domains or targets):
            foreign_rows.append(_domains_of(link))

    findings = _findings(links, dead_rows, nocert_rows, plain_rows, remote_rows,
                         foreign_rows, listen_ok, listen_reason, certs_ok, certs_reason, dflt)
    names = {d for link in links for d in link["domains"]}
    return {
        "as_of": datetime.now(UTC).isoformat(timespec="seconds"),
        "cached": False,
        "nginx": {
            "ok": dirs_present and bool(configs),
            "reason": (None if dirs_present and configs else
                       ("nginx 的配置目录都不存在（conf.d 与 sites-available），这台机器上没有可对的站点"
                        if not dirs_present else
                        "nginx 加载的配置里一个 server 块都没有——站点列表是空的，不是读不到")),
            "files": len(configs),
            "domains": len(names),
        },
        "listen": {"ok": listen_ok, "reason": listen_reason, "ports": len(listen_ports)},
        "certs": {"ok": certs_ok, "reason": certs_reason, "domains": len(cert_names)},
        "defaults": dflt,
        "links": links,
        "findings": findings,
        "counts": _counts(findings),
        "notes": [
            "只读：不 reload nginx、不改 vhost、不签证书、不重启应用；处置命令请手工执行",
            "证书只看覆盖关系，到期天数以证书项为准（同一个 alert_ssl_days 阈值）",
            "proxy_pass 指向别的机器时本机判不了，标未确认而不是没人听",
            "默认档按 nginx -T 报出的加载顺序算：Host 没对上任何 server_name 的请求就落到它",
        ],
    }


def _default_findings(dflt: dict) -> list[dict]:
    """默认档的判词：Host 没对上任何 server_name 的请求（含无 SNI 的 TLS 握手）归谁。

    这一档跟「某个域名配错了」不是一个量级：它影响的是**这台机器上所有没登记的域名**，
    而改任何一个站的文件都可能把它换掉，nginx 还一声不响。所以反射 Host 的那种算要紧。
    """
    if not dflt.get("ok"):
        return [{
            "id": "default-server-unknown",
            "severity": SEV_WARN,
            "title": "默认 server 未确认",
            "detail": f"{dflt.get('reason') or 'nginx -T 读不到'}，这一轮判不了"
                      "「Host 没匹配上任何 server_name 的请求落到哪个 server」。"
                      "这不等于「默认档没问题」——只是没查。",
            "fix": "nginx -T | grep -n 'default_server'",
        }]
    out: list[dict] = []
    for row in dflt.get("ports") or []:
        port = row["port"]
        who = "、".join(row["domains"][:2]) or "（server_name _）"
        where = f"{row['file']} 里的 {who}"
        how = "显式 default_server" if row.get("explicit") else "没人写 default_server，按加载顺序顶上"
        if row["hostReflect"]:
            out.append({
                "id": f"default-reflect-{port}",
                "severity": SEV_RISK,
                "title": f"端口 {port} 的默认档把请求方带来的域名原样跳转出去",
                "detail": f"未知 Host 会落到 {where}（{how}），而它跳转用的是 `return 301 https://$host…`："
                          "Location 由请求头自己决定。任何解析到本机的名字——打错的域名、"
                          "过期没摘的旧域、别人能改的泛解析——都会被送到 https://那个名字/，"
                          "在用户看来这一跳是本站发起的。把跳转目标写成固定的域名就没有这条路。",
                "fix": "nginx -T | grep -n 'return 301 https://$host'",
            })
        elif not row["catchAll"] and row["domains"]:
            extra = "；没带 SNI 的 TLS 握手拿到的也是这一张证书" if row.get("ssl") else ""
            out.append({
                "id": f"default-named-{port}",
                "severity": SEV_WARN,
                "title": f"端口 {port} 的默认档是具名站点 {row['domains'][0]}",
                "detail": f"这台机器上没有 server 块对 {port} 写 default_server，nginx 让 {where} 顶上（{how}）。"
                          "后果是任何解析到本机的域名都会被当成这一个站点服务：访问日志、"
                          f"应用收到的 Host 头与它自己的「我是谁」判断里会混进不属于它的名字{extra}。"
                          f"要么在 {port} 上显式写 default_server 指定归属，"
                          "要么补一个什么都不服务的默认档（`server_name _; return 444;`）把未知 Host 关在门外。",
                "fix": f"nginx -T | grep -n 'listen {port}.*default_server'",
            })
    return out


def _findings(links, dead_rows, nocert_rows, plain_rows, remote_rows, foreign_rows,
              listen_ok, listen_reason, certs_ok, certs_reason, dflt: dict) -> list[dict]:
    """把 join 结果折成判词：每条都带一句「为什么」和一个能复制的只读动作。"""
    out: list[dict] = []

    # ① 反代目标没人听：这一类会直接 502，逐块点名。
    for link in dead_rows:
        ports = sorted({int(p) for p in link["dead"]})
        who = _domains_of(link)
        alt = "|".join(str(p) for p in ports[:NAME_LIMIT])
        out.append({
            "id": f"dead-{link['file']}",
            "severity": SEV_RISK,
            "title": f"{who} 的反代目标没人听",
            "detail": (f"{link['file']} 把请求转到 {'、'.join(str(p) for p in ports)}，"
                       "而本机这些端口上没有监听。nginx -t 只校验语法，不会发现这件事，"
                       "所以配置、服务、证书三页都绿，用户拿到的是 502。"
                       "先看应用进程在不在，再决定是改端口还是把它拉起来。"),
            "fix": f"ss -ltn | grep -E ':({alt})\\b' && choyeonctl status",
        })

    # ② 监听表读不到：这会让上面那一判失明，必须单独说出来，不能沉默地少一条判词。
    if not listen_ok:
        unconfirmed = sum(1 for link in links for t in link["targets"]
                          if t["local"] and t["port"] is not None and t["listening"] is None)
        if unconfirmed:
            out.append({
                "id": "listen-unknown",
                "severity": SEV_WARN,
                "title": f"{unconfirmed} 个反代目标未确认",
                "detail": f"{listen_reason or '监听表没读到'}，这一轮判不了「是不是没人听」。"
                          "注意这不等于「目标都在」——只是没查。",
                "fix": "choyeonctl ports",
            })

    # ③ 证书覆盖：只说「这张证书不含这个域名」，天数归证书项。
    if nocert_rows:
        names = [f"{_domains_of(link)}（{link['file']}）" for link in nocert_rows]
        out.append({
            "id": "cert-coverage",
            "severity": SEV_WARN,
            "title": tallied(names, " 个 HTTPS 站点的证书不含它的域名"),
            "detail": "server 块开了 ssl，但这台机器的证书清单里没有一张包含这个域名——"
                      "浏览器会直接报证书名不匹配，而不是「快要到期」。"
                      "到期天数以证书项为准，这里只看覆盖关系。",
            "fix": "certbot certificates",
        })
    elif not certs_ok:
        ssl_named = sum(1 for link in links if link["ssl"] and link["domains"])
        if ssl_named:
            out.append({
                "id": "cert-unknown",
                "severity": SEV_WARN,
                "title": f"{ssl_named} 个 HTTPS 站点的证书覆盖未确认",
                "detail": f"{certs_reason or '证书清单没读到'}，这一轮判不了域名有没有被证书包含。"
                          "这不等于「证书都对」——只是没查。",
                "fix": "certbot certificates",
            })

    # ④ 明文可达：80 与 443 都听着却没有跳转。
    if plain_rows:
        names = [_domains_of(link) for link in plain_rows]
        out.append({
            "id": "plain-http",
            "severity": SEV_WARN,
            "title": tallied(names, " 个站点不跳转 https"),
            "detail": "同一个配置里 80 与 443 都在听，但没有 301/308 到 https："
                      "手打 http:// 或旧链接进来的用户拿的是明文，口令与会话都不在 TLS 里。"
                      "只有 443 的站点不算在这里。",
            "fix": "curl -sI http://" + (names[0] if names else "example.com") + " | head -1",
        })

    # ⑤ 默认档：未知 Host 归谁。一份新站的配置就能换掉全机的这一档，所以单独判。
    if links:
        out.extend(_default_findings(dflt))

    # ⑥ 知道就好：本机看不见的目标、面板不认识 vhost。
    if remote_rows:
        uniq = sorted(set(remote_rows))
        out.append({
            "id": "remote-targets",
            "severity": SEV_INFO,
            "title": tallied(uniq, " 个反代目标在别的机器上", unit="个"),
            "detail": "这些 proxy_pass 指向的不是 127.0.0.1，本机没有监听表可对照，"
                      "对不得上不代表它坏了；要判活得以那个地址本身为准。",
            "fix": "curl -sS -o /dev/null -w '%{http_code}\\n' http://" + uniq[0],
        })
    if foreign_rows:
        named = sorted(set(foreign_rows))
        out.append({
            "id": "unmanaged",
            "severity": SEV_INFO,
            "title": tallied(named, " 个 server 块还没有纳管", unit="个"),
            "detail": "纳管登记表里没有这些域名，面板认不出它们归哪个应用——手工配的 vhost、"
                      "应用改名后遗留的配置都长这样，面板的启停、探活与回滚够不到它们。"
                      "判得出归属的直接点「全部纳管」登记，判不出的（共用端口、端口没登记）"
                      "要在网站页上手工指定应用，面板不替你猜。",
            "fix": "choyeonctl site claim-all",
        })
    return out


def _counts(findings: list[dict]) -> dict:
    out = {"risk": 0, "warn": 0, "info": 0}
    for f in findings:
        out[f["severity"]] = out.get(f["severity"], 0) + 1
    return out


_LOCK = asyncio.Lock()
_cache: dict = probe.new_cache()


async def _app_rows() -> list[dict]:
    """应用表读法与站点页共用 `site_claim_service.app_rows`，这里只负责"读不到就留空"。"""
    try:
        return await site_claim_service.app_rows()
    except Exception:  # noqa: BLE001 - 认领不到应用名不影响链路判定，那一列如实留空
        return []


async def audit(refresh: bool = False) -> dict:
    """带 TTL 的对账入口；并发与连点合并成一轮 nginx 扫描（见 app/probe.py）。"""
    return await probe.read(_cache, _LOCK, _reprobe, refresh=refresh, ttl=CACHE_TTL)


async def _reprobe() -> dict:
    present = nginx_ops.config_dirs_present()
    configs = await asyncio.to_thread(nginx_ops.loaded_configs)
    # 默认档要按加载顺序算，顺序只有 `nginx -T` 知道（目录扫描给不出 nginx.conf 里的
    # include 先后），所以它自己走 loaded_conf_paths 那条 15 秒缓存，不跟上面这份共用。
    dflt = await nginx_ops.default_servers()
    exp = await exposure_service.audit()
    certs = await cert_ops.list_certs()
    apps = await _app_rows()
    try:
        reg = await site_claim_service.claims()
    except Exception:  # noqa: BLE001 - 登记表读不到时那一列留空，不拖累链路判定本身
        reg = {}
    return _build(configs, exp, certs, apps, present, reg, dflt)


def reset_cache() -> None:
    probe.reset(_cache)


def doctor_item(data: dict) -> dict:
    """自检项折法：读不到就 warn，真有人拿到 502 才 fail。

    「没配上」与「配错」里只有前者值得把自检点红：反代目标没人听意味着现在访问就是 502。
    面板不替你 reload nginx，也不替你改 vhost——那两件事改错是全站域名一起不可用。
    """
    nginx = data.get("nginx") or {}
    if not nginx.get("ok"):
        return {"status": "warn",
                "detail": nginx.get("reason") or "读不到 nginx 配置，站点链路无法对账",
                "fix": "choyeonctl site-audit"}
    findings = data.get("findings") or []
    risky = [f["title"] for f in findings if f["severity"] == SEV_RISK]
    warns = [f["title"] for f in findings if f["severity"] == SEV_WARN]
    head = f"{nginx.get('files')} 份配置、{nginx.get('domains')} 个域名"
    if risky:
        return {"status": "fail", "detail": tallied(risky, " 处站点链路要紧") + f"；{head}",
                "fix": "choyeonctl site-audit"}
    if warns:
        return {"status": "warn", "detail": tallied(warns, " 处站点链路值得处理") + f"；{head}",
                "fix": "choyeonctl site-audit"}
    info = int((data.get("counts") or {}).get("info") or 0)
    detail = f"站点链路对得上：{head}，反代目标都在监听、证书都覆盖、明文都跳转"
    if info:
        detail += f"（另有 {info} 条只是让你知道）"
    return {"status": "pass", "detail": detail, "fix": ""}
