import asyncio
import json
import os
import re
import time
from pathlib import Path

from .. import config
from .. import database as dbm
from ..util import is_branch, is_domain, is_git_url, is_name, is_port, run, run_shell_async
from . import nginx_ops, systemd_ops
from .systemd_ops import service_action

APP_ROOT = config.APP_ROOT
UNIT_DIR = config.UNIT_DIR

_DEPLOY_TASKS: set = set()

TEMPLATE_TOKENS = ("{{name}}", "{{path}}", "{{start_cmd}}", "{{port}}")


def unit_name(app: dict) -> str:
    return app.get("unit_override") or f"panel-{app['name']}.service"


def render_unit_from(tpl: str, app: dict) -> str:
    return (
        tpl.replace("{{name}}", app["name"])
        .replace("{{path}}", app["path"])
        .replace("{{start_cmd}}", json.dumps(app["start_cmd"]))
        .replace("{{port}}", str(app["port"] if app["port"] is not None else ""))
    )


def render_unit(app: dict) -> str:
    if app.get("unit_template"):
        return render_unit_from(app["unit_template"], app)
    return (
        f"[Unit]\n"
        f"Description=choyeon-panel app {app['name']}\n"
        f"After=network-online.target\n"
        f"Wants=network-online.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"WorkingDirectory={app['path']}\n"
        f"EnvironmentFile=-{app['path']}/.panel.env\n"
        f"ExecStart=/bin/bash -lc {json.dumps(app['start_cmd'])}\n"
        f"Restart=always\n"
        f"RestartSec=3\n"
        f"User=root\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def write_env_file(app: dict):
    try:
        vars_ = json.loads(app.get("env") or "[]")
    except (ValueError, TypeError):
        vars_ = []
    body = "\n".join(
        f'{x["k"]}="{str(x.get("v", "")).replace(chr(34), chr(92) + chr(34)).replace(chr(10), " ")}"'
        for x in vars_
        if isinstance(x, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", x.get("k") or "")
    )
    target = Path(app["path"]) / ".panel.env"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        os.chmod(target, 0o600)  # 环境变量常含密钥，禁止同机其他用户读取
    except OSError as e:
        raise RuntimeError(f"写入 .panel.env 失败：{e}") from e


async def ensure_unit(app: dict):
    if app.get("unit_override"):
        return
    Path(f"{UNIT_DIR}/{unit_name(app)}").write_text(render_unit(app))
    r = await run("systemctl", ["daemon-reload"])
    if r["code"] != 0:
        raise RuntimeError(r["out"])


async def remove_unit(app: dict):
    if app.get("unit_override"):
        return
    await run("systemctl", ["disable", "--now", unit_name(app)], timeout=30)
    p = f"{UNIT_DIR}/{unit_name(app)}"
    if os.path.exists(p):
        os.unlink(p)
    await run("systemctl", ["daemon-reload"])


_ss_cache: tuple[float, list[str]] | None = None


async def _listen_lines() -> list[str]:
    """读取监听端口表（5 秒内复用），异步执行避免阻塞事件循环。"""
    global _ss_cache
    if _ss_cache and time.time() - _ss_cache[0] < 5:
        return _ss_cache[1]
    out = await run_shell_async("ss -tlnp", timeout=4)
    _ss_cache = (time.time(), out.split("\n"))
    return _ss_cache[1]


async def detect_port(unit: str):
    r = await run("systemctl", ["show", unit, "-p", "MainPID", "--value"], timeout=5)
    try:
        main = int(r["out"].strip())
    except ValueError:
        main = 0
    if main <= 0:
        return None

    def _tree() -> set:
        pids = {main}
        queue = [main]
        while queue:
            p = queue.pop(0)
            try:
                for t in os.listdir(f"/proc/{p}/task"):
                    try:
                        kids = Path(f"/proc/{p}/task/{t}/children").read_text().split()
                    except OSError:
                        continue
                    for k in kids:
                        try:
                            kid = int(k)
                        except ValueError:
                            continue
                        if kid and kid not in pids:
                            pids.add(kid)
                            queue.append(kid)
            except OSError:
                pass
        return pids

    pids = await asyncio.to_thread(_tree)
    ports = set()
    for line in await _listen_lines():
        pm = re.search(r"pid=(\d+)", line)
        if not pm or int(pm.group(1)) not in pids:
            continue
        parts = line.strip().split()
        addr = parts[3] if len(parts) > 3 else ""
        port = re.search(r":(\d+)$", addr)
        if port:
            ports.add(int(port.group(1)))
    return min(ports) if ports else None


async def list_apps() -> list:
    rows = dbm.query("SELECT * FROM apps ORDER BY name")
    dom_map = nginx_ops.domains_by_port()

    async def _one(a: dict):
        running = await systemd_ops.is_active(unit_name(a))
        detected = await detect_port(unit_name(a)) if a["port"] is None and running else None
        port = a["port"] if a["port"] is not None else detected
        domains = [a["domain"]] if a["domain"] else (dom_map.get(port, []) if port is not None else [])
        deploying = bool(
            dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (a["id"],))
        )
        out = {k: v for k, v in a.items() if k != "env"}
        out.update(
            running=running,
            unit=unit_name(a),
            port=port,
            portAuto=a["port"] is None and detected is not None,
            domains=domains,
            deploying=deploying,
        )
        return out

    return list(await asyncio.gather(*(_one(a) for a in rows)))


def get_app(app_id: int):
    return dbm.query_one("SELECT * FROM apps WHERE id=?", (app_id,))


def _validate(i: dict, ignore_id: int = -1):
    if not is_name(i.get("name") or ""):
        raise RuntimeError("名称只能包含小写字母、数字、连字符，且以字母开头")
    if i.get("type") not in ("node", "python"):
        raise RuntimeError("类型必须是 node 或 python")
    if i.get("repo_url") and not is_git_url(i["repo_url"]):
        raise RuntimeError("仓库地址不合法")
    if i.get("branch") and not is_branch(i["branch"]):
        raise RuntimeError("分支名不合法")
    if i.get("port") is not None and not is_port(i["port"]):
        raise RuntimeError("端口必须在 1024-65535")
    if i.get("domain") and not is_domain(i["domain"]):
        raise RuntimeError("域名不合法")
    if not i.get("start_cmd") or len(i["start_cmd"]) > 500:
        raise RuntimeError("启动命令必填且不超过 500 字符")
    if i.get("path") and not re.match(r"^/[A-Za-z0-9._/-]{2,200}$", i["path"]):
        raise RuntimeError("安装目录必须是绝对路径且字符合法")
    if i.get("unit_template"):
        if len(i["unit_template"]) > 4000:
            raise RuntimeError("unit 模板过长")
        if re.search(r"\$\{|\$\(|`", i["unit_template"]):
            raise RuntimeError("unit 模板不允许 shell 替换语法")
    if i.get("unit_override") and not re.match(r"^[A-Za-z0-9@:._-]{1,64}\.service$", i["unit_override"]):
        raise RuntimeError("unit 名不合法")
    if i.get("db_names"):
        for d in i["db_names"].split(","):
            if d and not re.match(r"^[a-z0-9_]{1,63}$", d):
                raise RuntimeError(f"关联数据库名不合法: {d}")
    path = i.get("path") or f"{APP_ROOT}/{i['name']}"
    dup = dbm.query_one(
        "SELECT id FROM apps WHERE id IS NOT ?"
        " AND (name=? OR path=? OR (domain IS NOT NULL AND domain=? AND domain!=''))",
        (ignore_id, i["name"], path, i.get("domain") or ""),
    )
    if dup:
        raise RuntimeError("名称/路径/域名已被其他应用占用")
    if i.get("port"):
        busy = dbm.query_one("SELECT id FROM apps WHERE port=? AND id IS NOT ?", (i["port"], ignore_id))
        if busy:
            raise RuntimeError(f"端口 {i['port']} 已被应用 #{busy['id']} 使用")


def create_app(i: dict) -> dict:
    _validate(i)
    path = i.get("path") or f"{APP_ROOT}/{i['name']}"
    res = dbm.execute(
        "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,"
        "start_cmd,env,unit_override,db_names,unit_template)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            i["name"],
            i["type"],
            i.get("repo_url") or "",
            i.get("branch") or "main",
            path,
            i.get("port"),
            i.get("domain") or None,
            i.get("install_cmd") or None,
            i["start_cmd"],
            json.dumps(i.get("env") or []),
            i.get("unit_override") or None,
            i.get("db_names") or None,
            i.get("unit_template") or None,
        ),
    )
    return get_app(res)


def update_app(app_id: int, i: dict) -> dict:
    cur = get_app(app_id)
    if not cur:
        raise RuntimeError("应用不存在")
    merged = dict(cur)
    merged.update({k: v for k, v in i.items() if k != "env"})
    _validate(merged, app_id)
    vars_ = i["env"] if "env" in i and i["env"] is not None else json.loads(cur["env"] or "[]")
    dbm.execute(
        "UPDATE apps SET name=?,type=?,repo_url=?,branch=?,path=?,port=?,domain=?,install_cmd=?,start_cmd=?,env=?,"
        "unit_override=?,db_names=?,unit_template=?,updated_at=datetime('now') WHERE id=?",
        (
            merged["name"],
            merged["type"],
            merged.get("repo_url") or "",
            merged.get("branch") or "main",
            merged.get("path") or cur["path"],
            merged.get("port"),
            merged.get("domain") or None,
            merged.get("install_cmd") or None,
            merged["start_cmd"],
            json.dumps(vars_),
            merged.get("unit_override") or None,
            merged.get("db_names") or None,
            merged.get("unit_template") or None,
            app_id,
        ),
    )
    return get_app(app_id)


async def delete_app(app_id: int, purge: bool):
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError("部署进行中，不能删除")
    await remove_unit(app)
    if app.get("domain"):
        await nginx_ops.remove_vhost(app["name"])
    dbm.execute("DELETE FROM apps WHERE id=?", (app_id,))
    if purge and app["path"].startswith(APP_ROOT) and os.path.exists(app["path"]):
        import shutil

        shutil.rmtree(app["path"], ignore_errors=True)


async def app_action(app_id: int, verb: str):
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if verb == "rollback":
        return await rollback_app(app_id)
    await service_action(unit_name(app), verb)
    return await systemd_ops.is_active(unit_name(app))


def _append_log(dep_id: int, text: str):
    dbm.append_deploy_log(dep_id, text)


async def deploy_app(app_id: int) -> int:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError("该应用已有部署任务在运行")
    res = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
    dep_id = res
    task = asyncio.create_task(_run_deployment(app, dep_id))
    _DEPLOY_TASKS.add(task)
    task.add_done_callback(_DEPLOY_TASKS.discard)
    return dep_id


async def _current_sha(path: str) -> str | None:
    r = await run("git", ["rev-parse", "--short", "HEAD"], cwd=path, timeout=30)
    return r["out"] if r["code"] == 0 else None


async def rollback_app(app_id: int) -> dict:
    """回滚到上一个成功部署记录的 commit，并重启服务。"""
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if not os.path.isdir(f"{app['path']}/.git"):
        raise RuntimeError("应用目录不是 Git 仓库，无法回滚")
    last = dbm.query_one(
        "SELECT id,commit_sha FROM deployments WHERE app_id=? AND status='success' AND commit_sha IS NOT NULL"
        " ORDER BY id DESC LIMIT 1",
        (app_id,),
    )
    if not last:
        raise RuntimeError("没有可回滚的成功部署记录")
    r = await run("git", ["reset", "--hard", last["commit_sha"]], cwd=app["path"], timeout=120)
    if r["code"] != 0:
        raise RuntimeError(f"回滚失败：{r['out']}")
    await service_action(unit_name(app), "restart")
    return {"ok": True, "commit": last["commit_sha"], "deployment": last["id"]}


async def _run_deployment(app: dict, dep_id: int):
    def finish(ok: bool):
        dbm.execute(
            "UPDATE deployments SET status=?, finished_at=datetime('now') WHERE id=?",
            ("success" if ok else "failed", dep_id),
        )
        dbm.execute("UPDATE apps SET updated_at=datetime('now') WHERE id=?", (app["id"],))

    try:
        if app["repo_url"]:
            if os.path.exists(f"{app['path']}/.git"):
                _append_log(dep_id, f"==> git fetch/checkout {app['branch']}")
                r = await run("git", ["fetch", "origin", app["branch"]], cwd=app["path"], timeout=300)
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run("git", ["checkout", app["branch"]], cwd=app["path"])
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run("git", ["reset", "--hard", f"origin/{app['branch']}"], cwd=app["path"])
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
            else:
                _append_log(dep_id, f"==> git clone -b {app['branch']} {app['repo_url']} -> {app['path']}")
                r = await run(
                    "git", ["clone", "--depth", "1", "-b", app["branch"], app["repo_url"], app["path"]], timeout=600
                )
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
        else:
            _append_log(dep_id, f"==> 跳过拉取（未配置仓库，使用现有目录 {app['path']}）")
        if not os.path.exists(app["path"]):
            raise RuntimeError(f"目录不存在: {app['path']}")

        _append_log(dep_id, f"==> 安装依赖 ({app['type']})")
        default_install = (
            "npm install"
            if app["type"] == "node"
            else "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        )
        install = app["install_cmd"] or default_install
        ir = await run("/bin/bash", ["-lc", install], cwd=app["path"], timeout=1800)
        _append_log(dep_id, ir["out"] or "(no output)")
        if ir["code"] != 0:
            raise RuntimeError(f"安装依赖失败 (exit {ir['code']})")

        _append_log(dep_id, "==> 写入环境变量与 systemd unit")
        write_env_file(app)
        await ensure_unit(app)

        if app["domain"] and app["port"]:
            _append_log(dep_id, f"==> 应用 nginx 反代 {app['domain']} -> 127.0.0.1:{app['port']}")
            await nginx_ops.apply_vhost({"name": app["name"], "domain": app["domain"], "port": app["port"]})

        _append_log(dep_id, f"==> 重启服务 {unit_name(app)}")
        await service_action(unit_name(app), "restart")
        sha = await _current_sha(app["path"])
        if sha:
            dbm.execute("UPDATE deployments SET commit_sha=? WHERE id=?", (sha, dep_id))
            _append_log(dep_id, f"==> 当前 commit {sha}（可在部署记录中回滚到此版本）")
        _append_log(dep_id, "==> 部署完成")
        finish(True)
    except Exception as e:  # noqa: BLE001
        _append_log(dep_id, f"!!! 部署失败: {e}")
        finish(False)


async def attach_ssl(app_id: int, email: str | None = None) -> str:
    app = get_app(app_id)
    if not app or not app["domain"]:
        raise RuntimeError("应用未配置域名")
    out = await nginx_ops.issue_cert(app["domain"], email)
    await nginx_ops.apply_vhost({"name": app["name"], "domain": app["domain"], "port": app["port"]})
    return out


def read_unit(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    unit = unit_name(app)
    path = f"{UNIT_DIR}/{unit}"
    content = Path(path).read_text(errors="replace") if os.path.exists(path) else ""
    return {
        "unit": unit,
        "path": path,
        "managed": not app["unit_override"],
        "content": content,
        "template": app["unit_template"],
        "defaultTemplate": render_unit({**app, "unit_template": None}),
    }


async def save_unit(app_id: int, content: str) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if app["unit_override"]:
        raise RuntimeError("纳管模式：unit 由外部管理，面板不写文件")
    if not content.strip():
        raise RuntimeError("unit 内容不能为空")
    if len(content) > 8000:
        raise RuntimeError("unit 内容过长")
    if re.search(r"\$\{|\$\(|`", content):
        raise RuntimeError("unit 不允许 shell 替换语法")
    if "[Service]" not in content:
        raise RuntimeError("unit 必须包含 [Service] 段")
    if not re.search(r"^ExecStart=", content, re.M):
        raise RuntimeError("unit 必须包含 ExecStart=")
    unit = unit_name(app)
    path = f"{UNIT_DIR}/{unit}"
    old = Path(path).read_text(errors="replace") if os.path.exists(path) else None
    Path(path).write_text(content if content.endswith("\n") else content + "\n")
    verify = await run("systemd-analyze", ["verify", path], timeout=30)
    if verify["code"] != 0:
        if old is not None:
            Path(path).write_text(old)
        raise RuntimeError(f"systemd-analyze verify 失败，已回滚：\n{verify['out']}")
    reload = await run("systemctl", ["daemon-reload"])
    if reload["code"] != 0:
        if old is not None:
            Path(path).write_text(old)
        await run("systemctl", ["daemon-reload"])
        raise RuntimeError(f"daemon-reload 失败，已回滚：\n{reload['out']}")
    dbm.execute(
        "UPDATE apps SET unit_template=?,updated_at=datetime('now') WHERE id=?", (content + "\n", app_id)
    )
    return {"ok": True, "unit": unit, "restartRequired": True}


PROC_FIELDS = [
    "MainPID",
    "MemoryCurrent",
    "NRestarts",
    "ActiveEnterTimestamp",
    "CPUUsageNSec",
    "ExecMainStartTimestamp",
    "SubState",
]


async def app_proc(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    unit = unit_name(app)
    args = ["show", unit] + [f"-p{f}" for f in PROC_FIELDS]
    r = await run("systemctl", args, timeout=8)
    got: dict[str, str] = {}
    for line in r["out"].split("\n"):
        i = line.find("=")
        if i > 0:
            got[line[:i]] = line[i + 1:].strip()

    def _int(key):
        try:
            return int(got.get(key, ""))
        except ValueError:
            return None

    def _ts(key):
        v = got.get(key)
        return v if v and v != "n/a" else None

    pid = _int("MainPID") or 0
    mem = _int("MemoryCurrent")
    cpu_ns = _int("CPUUsageNSec")
    return {
        "unit": unit,
        "subState": got.get("SubState") or "unknown",
        "running": pid > 0,
        "pid": pid or None,
        "memoryBytes": mem,
        "restarts": _int("NRestarts") or 0,
        "activeSince": _ts("ActiveEnterTimestamp"),
        "execStartAt": _ts("ExecMainStartTimestamp"),
        "cpuSeconds": round(cpu_ns / 1e6) / 1000 if cpu_ns is not None else None,
    }
