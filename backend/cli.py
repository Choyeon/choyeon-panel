#!/usr/bin/env python3
"""choyeonctl —— choyeon-panel 命令行工具（网页端能做的一切，都能在终端完成）。

设计约定（面向脚本与 AI 智能体调用）：
1. 所有命令都可加全局 `--json`，输出结构化结果，便于机器解析；
2. 退出码固定语义：0 成功 / 1 执行失败 / 2 用法错误 / 3 前置条件不满足；
3. 非交互优先：口令、端口等都通过参数传入，需要确认的地方用 `--yes` 跳过；
4. 命令契约可由 `choyeonctl schema` 直接读出，无需看源码。

用法：
    choyeonctl deploy                # 一键部署（含依赖安装与前端构建）
    choyeonctl status --json         # 面板运行状态
    choyeonctl doctor [--json]       # 安全与健康自检
    choyeonctl user create alice --password 'xxx' [--role admin]
    choyeonctl app list|create|deploy|rollback|logs
    choyeonctl backup run|list
    choyeonctl logs [-n 100] | restart | stop | start | upgrade
    choyeonctl schema --json         # 输出命令契约（AI 读取入口）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.routers.meta import run_checks  # noqa: E402

SERVICE = "choyeon-panel"
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_PRECONDITION = 0, 1, 2, 3


class Out:
    """统一输出：--json 时打印 JSON，否则打印人类可读文本。"""

    json_mode = False

    @classmethod
    def result(cls, data, text: str = ""):
        if cls.json_mode:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        elif text:
            print(text)

    @classmethod
    def info(cls, msg: str):
        if not cls.json_mode:
            print(msg)

    @classmethod
    def error(cls, msg: str, code: int = EXIT_FAIL):
        if cls.json_mode:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[fail] {msg}", file=sys.stderr)
        raise SystemExit(code)


def have_systemd() -> bool:
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


async def sh(cmd: list[str], timeout: int = 60) -> dict:
    """执行外部命令并回传 code/out/err；命令不存在时 code=127。"""
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError:
        return {"code": 127, "out": "", "err": f"命令不存在: {cmd[0]}"}
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except TimeoutError:
        p.kill()
        return {"code": 124, "out": "", "err": f"超时 {timeout}s"}
    return {
        "code": p.returncode,
        "out": out.decode(errors="replace").strip(),
        "err": err.decode(errors="replace").strip(),
    }


def health_url() -> str:
    return f"http://127.0.0.1:{config.PORT}/api/health"


async def http_json(path: str, timeout: int = 10) -> tuple[int, dict | str]:
    """用 curl 取后端 JSON；curl 不可用时返回 (0, '')。"""
    if not shutil.which("curl"):
        return 0, ""
    url = f"http://127.0.0.1:{config.PORT}{path}"
    r = await sh(["curl", "-fsS", "--max-time", str(timeout), url], timeout=timeout + 5)
    if r["code"] != 0:
        return r["code"], r["err"] or r["out"]
    try:
        return 200, json.loads(r["out"])
    except json.JSONDecodeError:
        return 200, r["out"]


# ---------------------------------------------------------------- 命令实现
async def cmd_deploy(a: argparse.Namespace) -> dict:
    """一键部署：复用 scripts/install.sh，避免两套安装逻辑漂移。"""
    if os.geteuid() != 0:
        Out.error("部署需要 root 权限（面板要管理 systemd / nginx）", EXIT_PRECONDITION)
    script = ROOT / "scripts" / "install.sh"
    if not script.exists():
        Out.error(f"缺少 {script}", EXIT_PRECONDITION)

    env = dict(os.environ)
    if a.prefix:
        env["CP_PREFIX"] = a.prefix
    if a.port:
        env["CP_PORT"] = str(a.port)
    if a.skip_nginx:
        env["CP_SKIP_NGINX"] = "1"
    if a.no_deps:
        env["CP_SKIP_DEPS"] = "1"
    if a.branch:
        env["CP_BRANCH"] = a.branch

    Out.info(f"==> 执行 {script}")
    p = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, check=False)
    if not Out.json_mode:
        print(p.stdout)
        if p.stderr.strip():
            print(p.stderr, file=sys.stderr)

    if p.returncode != 0:
        Out.error(f"安装脚本退出码 {p.returncode}", EXIT_FAIL)

    ok_health = await wait_health(30)
    admins = dbm.query_one("SELECT COUNT(*) c FROM users")
    return {
        "ok": ok_health,
        "url": f"http://127.0.0.1:{a.port or config.PORT}",
        "healthy": ok_health,
        "adminCreated": bool(admins and admins["c"]),
        "next": "无管理员时打开 URL 注册，或 choyeonctl user create <name> --password <pw>",
    }


async def wait_health(timeout: int = 30) -> bool:
    for _ in range(timeout):
        code, body = await http_json("/api/health", timeout=3)
        if code == 200 and isinstance(body, dict) and body.get("ok"):
            return True
        await asyncio.sleep(1)
    return False


async def cmd_status(_a: argparse.Namespace) -> dict:
    checks = await run_checks()
    service = "unknown"
    if have_systemd():
        r = await sh(["systemctl", "is-active", SERVICE])
        service = r["out"] or r["err"] or "unknown"
    code, health = await http_json("/api/health", timeout=5)
    data = {
        "version": config.VERSION,
        "port": config.PORT,
        "host": config.HOST,
        "dataDir": config.DATA_DIR,
        "service": service,
        "healthy": code == 200 and isinstance(health, dict) and bool(health.get("ok")),
        "health": health if isinstance(health, dict) else None,
        "doctor": checks["status"],
        "url": health_url(),
    }
    Out.result(data, (
        f"版本 {config.VERSION}  监听 {config.HOST}:{config.PORT}\n"
        f"服务 {service}  健康 {'正常' if data['healthy'] else '异常'}\n"
        f"自检 {checks['status']}（pass {checks['counts']['pass']} / "
        f"warn {checks['counts']['warn']} / fail {checks['counts']['fail']}）\n"
        f"数据目录 {config.DATA_DIR}"
    ))
    return data


async def cmd_doctor(_a: argparse.Namespace) -> dict:
    res = await run_checks()
    if not Out.json_mode:
        icon = {"pass": "✓", "warn": "!", "fail": "✗"}
        for i in res["items"]:
            line = f"  {icon.get(i['status'], '?')} {i['title']}: {i['detail']}"
            print(line)
            if i["fix"] and i["status"] != "pass":
                print(f"      修复: {i['fix']}")
        print(f"结论: {res['status']}")
    return res


async def cmd_user(a: argparse.Namespace) -> dict:
    if a.user_cmd == "list":
        rows = dbm.query("SELECT id,username,role,created_at FROM users ORDER BY id")
        Out.result(rows, "\n".join(f"{r['id']:>3}  {r['username']:<20} {r['role']}" for r in rows) or "(无用户)")
        return {"users": rows}

    if a.user_cmd == "create":
        if not a.name or not a.password:
            Out.error("create 需要 <name> 与 --password", EXIT_USAGE)
        if not a.password or len(a.password) < 8:
            Out.error("密码至少 8 位", EXIT_USAGE)
        import re

        if not re.match(r"^[A-Za-z0-9_-]{3,32}$", a.name):
            Out.error("用户名只能包含字母、数字、下划线与连字符（3-32 位）", EXIT_USAGE)
        role = "viewer" if a.role == "viewer" else "admin"
        if dbm.query_one("SELECT id FROM users WHERE username=?", (a.name,)):
            Out.error(f"用户 {a.name} 已存在", EXIT_FAIL)
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (a.name, security.hash_pass(a.password), role),
        )
        dbm.audit(a.name, "cli:user:create", role)
        Out.result({"ok": True, "username": a.name, "role": role}, f"已创建 {a.name}（{role}）")
        return {"ok": True, "username": a.name, "role": role}

    if a.user_cmd == "reset":
        if not a.name or not a.password:
            Out.error("reset 需要 <name> 与 --password", EXIT_USAGE)
        u = dbm.query_one("SELECT * FROM users WHERE username=?", (a.name,))
        if not u:
            Out.error(f"用户 {a.name} 不存在", EXIT_FAIL)
        if len(a.password) < 8:
            Out.error("密码至少 8 位", EXIT_USAGE)
        dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (security.hash_pass(a.password), a.name))
        # 改密后吊销该用户所有已签发 token，避免旧凭证继续可用
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:reset")
        Out.result({"ok": True, "username": a.name, "revokedTokens": True}, f"已重置 {a.name} 密码并吊销旧 token")
        return {"ok": True, "username": a.name}

    if a.user_cmd == "delete":
        if not a.name:
            Out.error("delete 需要 <name>", EXIT_USAGE)
        u = dbm.query_one("SELECT * FROM users WHERE username=?", (a.name,))
        if not u:
            Out.error(f"用户 {a.name} 不存在", EXIT_FAIL)
        if not a.yes:
            Out.error(f"删除用户不可撤销，确认请加 --yes：choyeonctl user delete {a.name} --yes", EXIT_USAGE)
        dbm.execute("DELETE FROM users WHERE username=?", (a.name,))
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:delete")
        Out.result({"ok": True, "username": a.name}, f"已删除 {a.name}")
        return {"ok": True}

    Out.error(f"未知子命令: {a.user_cmd}", EXIT_USAGE)
    return {}


async def cmd_app(a: argparse.Namespace) -> dict:
    from app.services import apps_service
    from app.templates import apply_template, list_templates

    if a.app_cmd == "templates":
        rows = list_templates()
        Out.result(rows, "\n".join(f"{t['key']:<16} {t['name']:<14} {t['desc']}" for t in rows))
        return {"templates": rows}

    if a.app_cmd == "list":
        rows = await apps_service.list_apps()
        lines = [f"{r['id']:>3}  {r['name']:<20} {r['type']:<7} {r.get('port') or '-'}" for r in rows]
        Out.result(rows, "\n".join(lines) or "(无应用)")
        return {"apps": rows}

    if a.app_cmd == "create":
        if not a.name:
            Out.error("create 需要 <name>", EXIT_USAGE)
        payload = {
            "name": a.name,
            "repo_url": a.repo or "",
            "branch": a.branch or "main",
            "path": a.path or "",
            "port": a.port,
            "domain": a.domain or "",
            "template": a.template or "",
        }
        if not (a.template or a.start_cmd):
            Out.error("需要 --template 或 --start-cmd 之一", EXIT_USAGE)
        if a.start_cmd:
            payload["start_cmd"] = a.start_cmd
            payload["type"] = a.type or "node"
            payload["install_cmd"] = a.install_cmd or ""
        try:
            app = apps_service.create_app(apply_template(payload))
        except Exception as e:  # noqa: BLE001 统一把业务校验错误转成 CLI 失败
            Out.error(str(e), EXIT_FAIL)
        dbm.audit("cli", "app:create", app["name"])
        Out.result(app, f"已创建应用 #{app['id']} {app['name']}")
        return app

    if a.app_cmd == "deploy":
        if not a.id:
            Out.error("deploy 需要 --id", EXIT_USAGE)
        dep = await apps_service.deploy_app(a.id)
        dbm.audit("cli", "app:deploy", str(a.id))
        Out.result({"deployment": dep, "app": a.id}, f"部署已启动：deployment #{dep}（用 app logs --id {a.id} 查看）")
        return {"deployment": dep}

    if a.app_cmd == "rollback":
        if not a.id:
            Out.error("rollback 需要 --id", EXIT_USAGE)
        res = await apps_service.rollback_app(a.id)
        dbm.audit("cli", "app:rollback", str(a.id))
        Out.result({"ok": True, **res}, f"已回滚到 {res.get('commit') or '上一版本'}")
        return {"ok": True, **res}

    if a.app_cmd == "logs":
        if not a.id:
            Out.error("logs 需要 --id", EXIT_USAGE)
        row = dbm.query_one(
            "SELECT id,status,commit_sha,started_at,finished_at,log FROM deployments"
            " WHERE app_id=? ORDER BY id DESC LIMIT 1",
            (a.id,),
        )
        if not row:
            Out.error("该应用还没有部署记录", EXIT_FAIL)
        log = row["log"] or ""
        tail = "\n".join(log.splitlines()[-a.lines:])
        Out.result({"id": row["id"], "status": row["status"], "commit": row["commit_sha"], "log": tail}, tail)
        return {"id": row["id"], "status": row["status"], "log": tail}

    Out.error(f"未知子命令: {a.app_cmd}", EXIT_USAGE)
    return {}


async def cmd_backup(a: argparse.Namespace) -> dict:
    if a.backup_cmd == "run":
        script = ROOT / "scripts" / "backup.sh"
        if not script.exists():
            Out.error(f"缺少 {script}", EXIT_PRECONDITION)
        r = await sh(["bash", str(script)], timeout=300)
        if r["code"] != 0:
            Out.error(r["err"] or "备份失败", EXIT_FAIL)
        files = sorted(Path(config.BACKUP_DIR).glob("manual/panel-*.tar.gz"), key=os.path.getmtime)
        latest = str(files[-1]) if files else None
        Out.result({"ok": True, "file": latest}, f"备份完成：{latest}")
        return {"ok": True, "file": latest}

    if a.backup_cmd == "list":
        files = sorted(Path(config.BACKUP_DIR).glob("**/*.tar.gz"), key=os.path.getmtime, reverse=True)
        rows = [{"file": str(f), "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)} for f in files[:50]]
        Out.result(rows, "\n".join(f"{r['size']:>10}  {r['file']}" for r in rows) or "(无备份)")
        return {"backups": rows}

    Out.error(f"未知子命令: {a.backup_cmd}", EXIT_USAGE)
    return {}


async def cmd_service(a: argparse.Namespace) -> dict:
    verb = a.service_cmd
    if not have_systemd():
        Out.error("未检测到 systemd，无法用 CLI 管理服务", EXIT_PRECONDITION)
    r = await sh(["systemctl", verb, SERVICE], timeout=60)
    if r["code"] != 0:
        Out.error(r["err"] or f"systemctl {verb} 失败", EXIT_FAIL)
    ok = await wait_health(30) if verb in ("start", "restart") else True
    Out.result({"ok": True, "action": verb, "healthy": ok}, f"{verb} 完成")
    return {"ok": True, "action": verb, "healthy": ok}


async def cmd_logs(a: argparse.Namespace) -> dict:
    if have_systemd():
        r = await sh(["journalctl", "-u", SERVICE, "-n", str(a.lines), "--no-pager"], timeout=30)
        text = r["out"]
    else:
        text = "(无 systemd，请查看启动终端输出)"
    Out.result({"lines": text.splitlines()}, text)
    return {"lines": text.splitlines()}


async def cmd_upgrade(_a: argparse.Namespace) -> dict:
    script = ROOT / "scripts" / "update.sh"
    if not script.exists():
        Out.error(f"缺少 {script}", EXIT_PRECONDITION)
    r = await sh(["bash", str(script)], timeout=900)
    if r["code"] != 0:
        Out.error(r["err"] or "升级失败（脚本已尝试自动回滚）", EXIT_FAIL)
    ok = await wait_health(30)
    Out.result({"ok": ok, "healthy": ok}, "升级完成" if ok else "升级后健康检查未通过")
    return {"ok": ok}


async def cmd_schema(_a: argparse.Namespace) -> dict:
    """机器可读命令契约：AI 智能体读这一份就够，不用翻源码。"""
    schema = {
        "cli": "choyeonctl",
        "version": config.VERSION,
        "conventions": {
            "json": "所有命令支持全局 --json，输出结构化结果",
            "exitCodes": {"0": "成功", "1": "执行失败", "2": "用法错误", "3": "前置条件不满足"},
            "nonInteractive": "口令等敏感值通过参数传入；破坏性操作需显式 --yes",
        },
        "commands": [
            {"name": "deploy", "args": ["--prefix", "--port", "--branch", "--skip-nginx", "--no-deps"],
             "desc": "一键部署（依赖→构建→systemd→健康检查）"},
            {"name": "upgrade", "args": [], "desc": "升级到最新代码，失败自动回滚"},
            {"name": "status", "args": [], "desc": "版本/监听/服务/健康/自检汇总"},
            {"name": "doctor", "args": [], "desc": "安全与健康自检，含每项修复建议"},
            {"name": "user list|create|reset|delete", "args": ["<name>", "--password", "--role", "--yes"],
             "desc": "账号管理（改密会吊销旧 token）"},
            {"name": "app templates|list|create|deploy|rollback|logs",
             "args": ["--id", "--name", "--template", "--repo", "--path", "--port", "--domain", "--lines"],
             "desc": "应用全生命周期管理"},
            {"name": "backup run|list", "args": [], "desc": "面板数据备份"},
            {"name": "start|stop|restart", "args": [], "desc": "systemd 服务控制"},
            {"name": "logs", "args": ["--lines"], "desc": "查看面板日志"},
            {"name": "schema", "args": [], "desc": "输出本契约"},
        ],
        "deployRecipe": [
            "choyeonctl deploy --port 3210",
            "choyeonctl user create admin --password <随机口令>",
            "choyeonctl doctor",
            "choyeonctl status --json",
        ],
    }
    Out.result(schema, json.dumps(schema, ensure_ascii=False, indent=2))
    return schema


# ---------------------------------------------------------------- 参数解析
def build_parser() -> argparse.ArgumentParser:
    # 共享 parent：让 --json 既能写在子命令前也能写在子命令后，减少调用方踩坑
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="以 JSON 输出（供脚本/AI 解析）")

    p = argparse.ArgumentParser(
        prog="choyeonctl", description="choyeon-panel 命令行管理工具", parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("deploy", help="一键部署面板", parents=[common])
    d.add_argument("--prefix", help="安装目录，默认 /root/choyeon-panel")
    d.add_argument("--port", type=int, help="监听端口，默认 3210")
    d.add_argument("--branch", help="部署指定分支")
    d.add_argument("--skip-nginx", action="store_true", help="跳过 nginx 反代配置")
    d.add_argument("--no-deps", action="store_true", help="跳过系统依赖安装")
    d.set_defaults(fn=cmd_deploy)

    sub.add_parser("upgrade", help="升级并自动回滚失败", parents=[common]).set_defaults(fn=cmd_upgrade)
    sub.add_parser("status", help="运行状态", parents=[common]).set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="安全与健康自检", parents=[common]).set_defaults(fn=cmd_doctor)

    u = sub.add_parser("user", help="账号管理", parents=[common])
    u.add_argument("user_cmd", choices=["list", "create", "reset", "delete"])
    u.add_argument("name", nargs="?")
    u.add_argument("--password")
    u.add_argument("--role", choices=["admin", "viewer"], default="admin")
    u.add_argument("--yes", action="store_true")
    u.set_defaults(fn=cmd_user)

    ap = sub.add_parser("app", help="应用管理", parents=[common])
    ap.add_argument("app_cmd", choices=["templates", "list", "create", "deploy", "rollback", "logs"])
    ap.add_argument("--id", type=int)
    ap.add_argument("--name")
    ap.add_argument("--template")
    ap.add_argument("--type", choices=["node", "python"])
    ap.add_argument("--repo")
    ap.add_argument("--branch")
    ap.add_argument("--path")
    ap.add_argument("--port", type=int)
    ap.add_argument("--domain")
    ap.add_argument("--start-cmd")
    ap.add_argument("--install-cmd")
    ap.add_argument("--lines", type=int, default=50)
    ap.set_defaults(fn=cmd_app)

    b = sub.add_parser("backup", help="备份管理", parents=[common])
    b.add_argument("backup_cmd", choices=["run", "list"])
    b.set_defaults(fn=cmd_backup)

    for verb in ("start", "stop", "restart"):
        s = sub.add_parser(verb, help=f"systemctl {verb} {SERVICE}", parents=[common])
        s.set_defaults(fn=cmd_service, service_cmd=verb)

    lg = sub.add_parser("logs", help="查看面板日志", parents=[common])
    lg.add_argument("--lines", "-n", type=int, default=100)
    lg.set_defaults(fn=cmd_logs)

    sub.add_parser("schema", help="输出命令契约（机器可读）", parents=[common]).set_defaults(fn=cmd_schema)
    return p


async def amain() -> int:
    parser = build_parser()
    a = parser.parse_args()
    Out.json_mode = bool(getattr(a, "json", False))
    try:
        await a.fn(a)
    except SystemExit:
        raise
    except KeyboardInterrupt:
        Out.error("已取消", EXIT_FAIL)
    except Exception as e:  # noqa: BLE001 CLI 顶层兜底：任何异常都要变成可读错误与非 0 退出码
        Out.error(f"{type(e).__name__}: {e}", EXIT_FAIL)
    return EXIT_OK


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    raise SystemExit(main())
