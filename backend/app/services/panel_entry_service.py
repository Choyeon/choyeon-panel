"""面板自己的对外入口（设置页「面板域名」）。

这个服务的边界是刻意收窄的：

- **读**：哪一份配置是面板入口、对外域名是什么、证书落在哪个 lineage、80 有没有跳 443。
  读不到就说读不到（`unreadable`），不把「nginx -T 读不动」说成「面板没有域名入口」。
- **改域名**：只改那份文件里的 `server_name`、证书路径与 certbot 的 `$host` 判断，
  listen / proxy_pass / 手工 location 一律原样保留；写完 `nginx -t` → reload →
  回环 HTTPS 复核（连证书链一起校），任一步不过就把快照放回去再 reload。
- **不签证书**：ACME 失败会把面板入口留在「配置指向新域名、证书还是旧的」那种半死状态，
  一次设置兜不住。新域名没有证书就拒绝，并把 certbot 命令原样交出去。
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import config
from ..log import warn
from ..util import is_domain, run
from . import nginx_ops

PANEL_LABEL = "面板入口"

# 只认整行：注释里的 server_name 不许被改（用户把老域名写在注释里留档是常态）
RE_SERVER_NAME_LINE = re.compile(r"^([ \t]*)server_name[ \t]+([^;]+);", re.M)
RE_SSL_CERT = re.compile(r"^[ \t]*ssl_certificate[ \t]+([^;]+);", re.M)
# lineage 目录取自 config.TLS_DIR，不在这里再写死一份 /etc/letsencrypt/live：
# CP_TLS_DIR 改过的机器上，写死的那份会把所有证书都判成「不在 live 目录」。
_LIVE_PREFIX = config.TLS_DIR.rstrip("/") + "/"
RE_LIVE_LINEAGE = re.compile(r"^" + re.escape(_LIVE_PREFIX) + r"([^/]+)/")
# certbot 的明文块：`if ($host = 老域名) { return 301 ... }`
RE_HOST_IF = re.compile(r"if \(\$host = ([^)]+)\)")


class PanelEntryError(RuntimeError):
    """可解释的拒绝：前置条件不满足，或改完复核没过（已回滚），运维据此处理。"""


def _panel_entries() -> list[dict]:
    """反代目标就是面板端口的配置，按加载顺序给出（顺序决定同一域名谁生效）。"""
    out: list[dict] = []
    for c in nginx_ops.loaded_configs():
        if nginx_ops.is_panel_proxy(c):
            out.append(c)
    return out


def _read_text(path: str) -> tuple[str, str, str]:
    """(状态, 正文, 原因)——状态三档：ok / missing / unreadable。"""
    try:
        return "ok", Path(path).read_text(errors="replace"), ""
    except FileNotFoundError:
        return "missing", "", ""
    except OSError as e:
        return "unreadable", "", type(e).__name__


def _ssl_state(text: str) -> tuple[bool, str | None, str]:
    """(有没有 ssl_certificate, lineage 名, 拒绝理由)。

    证书路径不在 certbot 的 live 目录下时**不猜**：那种文件换了域名，证书文件名还是旧的，
    nginx -t 照样过，浏览器却会报证书只覆盖旧名——这比拒绝切换难查得多。
    """
    certs = [m.group(1).strip() for m in RE_SSL_CERT.finditer(text)]
    if not certs:
        return False, None, ""
    lineages = set()
    for p in certs:
        m = RE_LIVE_LINEAGE.match(p)
        if not m:
            return True, None, f"证书路径不是 certbot 的 live 目录（{Path(p).name}），面板不替你改这一份"
        lineages.add(m.group(1))
    if len(lineages) != 1:
        return True, None, "这份配置里引用了不止一张证书，面板不替你判断该换哪一张"
    return True, lineages.pop(), ""


def _entry_row(c: dict) -> dict:
    state, text, why = _read_text(c["file"])
    a = c["analysis"]
    ssl_on, lineage, ssl_reason = _ssl_state(text) if state == "ok" else (False, None, why)
    return {
        "file": c["file"],
        "state": state,
        "reason": why or ssl_reason,
        "names": [n for n in (a.get("serverNames") or []) if n and n != "_"],
        "listenPorts": a.get("listenPorts") or [],
        "ssl": bool(a.get("ssl")),
        "lineage": lineage,
        "httpsRedirect": bool(a.get("httpsRedirect")),
    }


def _primary(rows: list[dict]) -> str:
    """对外域名取「443 那份的第一个具名域名」，没有 TLS 就取加载顺序里的第一个。"""
    for r in rows:
        if r["ssl"] and r["names"]:
            return r["names"][0]
    for r in rows:
        if r["names"]:
            return r["names"][0]
    return ""


async def detail() -> dict:
    """面板入口现状。读不到当前生效配置时 ok=False + unreadable=True。"""
    if not nginx_ops.config_dirs_present():
        return {"ok": False, "unreadable": True, "label": PANEL_LABEL,
                "reason": "本机没有 nginx 配置目录，面板没有域名入口", "entries": []}
    loaded = await nginx_ops.loaded_conf_paths()
    if loaded is None:
        return {"ok": False, "unreadable": True, "label": PANEL_LABEL,
                "reason": "nginx -T 读不到当前生效的配置，判断不了面板的对外入口", "entries": []}
    entries = [_entry_row(c) for c in _panel_entries()]
    if not entries:
        return {"ok": True, "unreadable": False, "label": PANEL_LABEL, "domain": "",
                "entries": [], "port": config.PORT,
                "reason": "没有哪份配置反代到面板端口：面板要么只经 IP:端口 访问，要么入口不在 nginx 里"}
    domain = _primary(entries)
    unreadable = [e for e in entries if e["state"] != "ok"]
    bad_ssl = [e for e in entries if e["ssl"] and not e["lineage"]]
    notes: list[str] = []
    if unreadable:
        notes.append("有入口配置文件读不动，改域名会被拒绝")
    if bad_ssl:
        notes.append("有入口配置的证书不在 certbot 的 live 目录下，改域名会被拒绝")
    if not any(e["httpsRedirect"] for e in entries):
        notes.append("没有 80 → 443 的跳转块，明文访问仍然直通面板")
    return {
        "ok": True, "unreadable": False, "label": PANEL_LABEL,
        "port": config.PORT, "domain": domain,
        "entries": entries,
        "changeable": not unreadable and not bad_ssl,
        "notes": notes,
    }


def rewrite_names(text: str, new_domain: str) -> str:
    """把这份配置换成新域名：server_name、live 证书目录、certbot 的 $host 判断。

    只动这三样。listen、proxy_pass、location 里手写的规则一个字都不碰——
    面板入口这份文件常常还有管理员自己加的 client_max_body_size、WebSocket 头之类。
    """
    out = RE_SERVER_NAME_LINE.sub(lambda m: f"{m.group(1)}server_name {new_domain};", text)
    out = re.sub(re.escape(_LIVE_PREFIX) + r"[^/;\s]+/", f"{_LIVE_PREFIX}{new_domain}/", out)
    out = RE_HOST_IF.sub(f"if ($host = {new_domain})", out)
    return out


def _https_port(rows: list[dict]) -> int | None:
    for r in rows:
        if r["ssl"]:
            ports = [p for p in r["listenPorts"] if p not in (80, 81)]
            if ports:
                return ports[0]
    return None


async def _loopback_probe(domain: str, port: int) -> tuple[bool, str]:
    """绕过 DNS：把新域名解析到 127.0.0.1 再发一次 HTTPS 请求，证书链一起校。

    `-k` 会连「证书还是旧域名」一起放过，那就把这次切换最该验证的东西关掉了。
    """
    r = await run("curl", [
        "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "--resolve", f"{domain}:{port}:127.0.0.1",
        "--max-time", "10", f"https://{domain}/",
    ], timeout=15)  # 比 curl 自己的 --max-time 多留 5 秒：让 curl 的报错先进来，而不是被子进程预算砍掉
    code = (r["out"] or "").strip().splitlines()[-1:] or [""]
    status = code[0].strip()
    if r["code"] != 0:
        return False, (r["out"] or "").strip()[:200] or "curl 退出码非 0"
    if not (status.startswith("2") or status.startswith("3")):
        return False, f"回环 HTTPS 返回 {status or '空'}"
    return True, status


async def change_domain(new_domain: str) -> dict:
    """把面板入口换成 new_domain。成功返回 {ok, domain, changed, files, verified, notes}。

    失败一律抛 PanelEntryError，且盘上文件与 nginx 都已回到改动前那一份
    （快照放回 + 再 reload 一次）。
    """
    new = (new_domain or "").strip().lower().rstrip(".")
    if not is_domain(new):
        raise PanelEntryError(f"不是合法域名：{new_domain!r}")
    entries = _panel_entries()
    if not entries:
        raise PanelEntryError(
            "没有哪份配置反代到面板端口，改不了域名：面板要么只经 IP:端口 访问，"
            "要么入口不在这台机器的 nginx 里。请先按 deploy/nginx-panel.conf.example "
            "给面板配一个域名入口，再来这里换。")
    rows = [_entry_row(c) for c in entries]
    broken = [r for r in rows if r["state"] != "ok"]
    if broken:
        raise PanelEntryError(
            f"{Path(broken[0]['file']).name} 读不动（{broken[0]['reason'] or broken[0]['state']}）："
            "这一刻既拿不到盘上正在生效的那一份，也就没有能在复核失败后放回去的快照。"
            "请先把权限修好，或直接用编辑器手工改这份文件。")
    bad_ssl = [r for r in rows if r["ssl"] and not r["lineage"]]
    if bad_ssl:
        raise PanelEntryError(f"{Path(bad_ssl[0]['file']).name}：{bad_ssl[0]['reason']}")
    old_names = sorted({n for r in rows for n in r["names"]})
    if new in old_names:
        return {"ok": True, "domain": new, "changed": False, "files": [r["file"] for r in rows],
                "verified": None, "notes": ["域名没变，盘上什么都没写"]}
    if not await nginx_ops.cert_lineage(new):
        email = await _cert_email()
        raise PanelEntryError(
            f"{new} 还没有证书。面板不在设置里替你跑 ACME：签发失败会留下"
            "「配置指向新域名、证书还是旧的」那种半死入口，比不改更难收拾。"
            f"请先签好证书再回来切换：certbot --nginx -d {new} --redirect"
            + (f" -m {email}" if email else " --register-unsafely-without-email")
            + " --non-interactive --agree-tos")

    https_port = _https_port(rows)
    snapshots: list[tuple[str, str]] = []
    async with nginx_ops._nginx_section():
        for r in rows:
            owner = await nginx_ops.domain_owner(new, r["file"])
            if owner:
                raise PanelEntryError(
                    f"{new} 已经由 {Path(owner).name} 定义：同一域名有两份配置时 nginx 只认"
                    "加载顺序里的第一份，强行切过去会让那边的手工配置静默失效。"
                    "先让它不再占用这个域名，或换一个域名。")
            state, text, why = _read_text(r["file"])
            if state != "ok":
                raise PanelEntryError(f"{Path(r['file']).name} 读取状态在写入前变成了 {state}（{why}），未改动任何文件")
            snapshots.append((r["file"], text))
        try:
            for path, text in snapshots:
                nginx_ops._write_conf(path, rewrite_names(text, new))
            check = await run("nginx", ["-t"])
            if check["code"] != 0:
                raise PanelEntryError(f"nginx -t 没过：{(check['out'] or '').strip()[:300]}")
            reload = await run("systemctl", ["reload", "nginx"])
            if reload["code"] != 0:
                raise PanelEntryError(f"nginx reload 失败：{(reload['out'] or '').strip()[:300]}")
            verified: dict = {"probe": None}
            if https_port:
                ok, detail_text = await _loopback_probe(new, https_port)
                verified["probe"] = {"ok": ok, "target": f"https://{new}:{https_port}/", "detail": detail_text}
                if not ok:
                    raise PanelEntryError(
                        f"配置写进去了、nginx 也 reload 了，但回环访问 {new} 没过（{detail_text}）："
                        "多半是 DNS 还没指到这台机器、或 443 上的证书不覆盖这个域名")
        except Exception as e:  # noqa: BLE001 - 任何意外都必须把入口放回去
            rb = await _rollback(snapshots)
            if isinstance(e, PanelEntryError):
                raise PanelEntryError(f"{e}；{rb or '已回滚，盘上与 nginx 均已回到改动前那一份'}") from e
            raise PanelEntryError(
                f"改域名过程中出错（{type(e).__name__}）：{rb or '已回滚，盘上与 nginx 均已回到改动前那一份'}") from e
    nginx_ops.invalidate_domain_cache()
    return {
        "ok": True, "domain": new, "changed": True,
        "files": [p for p, _ in snapshots],
        "oldDomains": old_names,
        "verified": verified,
        "notes": [
            "回环复核绕过了 DNS：它证明的是本机 443 上的证书与新配置可用，公网解析是否已指向本机要另行确认",
            "旧域名不再指向面板：它的 vhost（如果有）保持原样，不在这里删除",
        ],
    }


async def _rollback(snapshots: list[tuple[str, str]]) -> str:
    """把快照放回去再 reload 一次。返回还需额外交代的话（空串 = 回滚干净）。

    这里不抛异常：用户要看的结论是「刚才那次切换为什么失败」，回滚自身的问题接在后面说，
    而不是把它顶掉。写回失败与 reload 失败必须分开讲——前者是盘上可能仍是新配置，
    后者盘上已经对了，只是 nginx 内存里还留着刚才那份。
    """
    write_fail: list[str] = []
    for path, text in snapshots:
        try:
            nginx_ops._write_conf(path, text)
        except OSError as e:
            write_fail.append(f"{Path(path).name}: {type(e).__name__}")
    if write_fail:
        msg = ("回滚没有写回盘上（" + "；".join(write_fail) +
               "），那份可能仍是新配置，请立即用编辑器核对面板入口文件与 nginx -t")
        warn(msg)
        return msg
    r = await run("systemctl", ["reload", "nginx"])
    if r["code"] != 0:
        msg = ("已回滚盘上那一份，但回滚后的 nginx reload 失败（" +
               (r["out"] or "").strip()[:200] +
               "）：nginx 内存里可能仍是刚才那份配置，请立即执行 nginx -t && systemctl reload nginx 核对")
        warn(msg)
        return msg
    return ""


async def _cert_email() -> str:
    """证书提醒邮箱：certbot 续期已经在用同一份设置，这里不另建一个键。"""
    try:
        from .. import database as dbm
        return str(dbm.get_setting("alert_email_to") or "").strip()
    except Exception:  # noqa: BLE001 - 邮箱只是给命令加个参数，读不到就不加
        return ""
