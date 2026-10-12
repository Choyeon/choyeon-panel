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
    choyeonctl firewall list|allow|deny|remove|on|off
    choyeonctl updates
    choyeonctl storage
    choyeonctl logs [-n 100] | restart | stop | start | upgrade
    choyeonctl schema --json         # 输出命令契约（AI 读取入口）
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import NoReturn

BACKEND_DIR = Path(__file__).resolve().parent
ROOT = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

from app import avatars, config, security  # noqa: E402
from app import database as dbm  # noqa: E402
from app.errors import BadInput, public_message  # noqa: E402
from app.routers.meta import run_checks  # noqa: E402
from app.services import (  # noqa: E402
    exposure_service,
    firewall_service,
    login_audit_service,
    memory_service,
    pending_service,
    storage_service,
    updates_service,
)

SERVICE = "choyeon-panel"
EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_PRECONDITION = 0, 1, 2, 3
# 部署超时：AGENTS.md 写"全新机器约 3-8 分钟，前端构建较慢，超时给到 15 分钟"。
DEPLOY_TIMEOUT_SEC = 15 * 60


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
    def error(cls, msg: str, code: int = EXIT_FAIL) -> NoReturn:
        if cls.json_mode:
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False, indent=2))
        else:
            print(f"[fail] {msg}", file=sys.stderr)
        raise SystemExit(code)


def have_systemd() -> bool:
    return bool(shutil.which("systemctl")) and os.path.isdir("/run/systemd/system")


async def sh(cmd: list[str], timeout: int = 60, env: dict | None = None,
             stdin_devnull: bool = False) -> dict:
    """执行外部命令并回传 code/out/err；命令不存在时 code=127，超时 code=124。

    start_new_session + killpg：安装/升级脚本会派生 apt、npm、node 子进程，
    只 kill 直接子进程会留下一堆孤儿继续跑，超时后现场更难判断。
    """
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL if stdin_devnull else None,
            env=env, start_new_session=True,
        )
    except FileNotFoundError:
        return {"code": 127, "out": "", "err": f"命令不存在: {cmd[0]}"}
    try:
        out, err = await asyncio.wait_for(p.communicate(), timeout=timeout)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await p.wait()
        return {"code": 124, "out": "", "err": f"超时 {timeout}s（已终止整个进程组）"}
    return {
        "code": p.returncode,
        "out": out.decode(errors="replace").strip(),
        "err": err.decode(errors="replace").strip(),
    }


def health_url(port: int | None = None) -> str:
    return f"http://127.0.0.1:{port or config.PORT}/api/health"


async def http_json(path: str, timeout: int = 10, port: int | None = None) -> tuple[int, dict | str]:
    """用 curl 取后端 JSON；curl 不可用时返回 (0, '')。"""
    if not shutil.which("curl"):
        return 0, ""
    url = f"http://127.0.0.1:{port or config.PORT}{path}"
    r = await sh(["curl", "-fsS", "--max-time", str(timeout), url], timeout=timeout + 5)
    if r["code"] != 0:
        return r["code"], r["err"] or r["out"]
    try:
        return 200, json.loads(r["out"])
    except json.JSONDecodeError:
        return 200, r["out"]


# ---------------------------------------------------------------- 命令实现
def _deploy_target(a: argparse.Namespace) -> tuple[Path, Path, int]:
    """按 scripts/{common,install}.sh 的取值口径算出这次部署真正落地的位置与端口。

    不能读 CLI 进程自己的 config.*：config 来自**当前仓库**的 .env，而 --prefix/--port
    写的是**即将生成**的那份配置。实测在 /root/www/choyeon-panel 里执行
    `choyeonctl deploy --port 3211`，服务被正确渲染成监听 3211 并且健康，CLI 却按
    config.PORT=3210 探测，30 秒后返回 healthy:false 并以退出码 1 结束——
    自动化据此判定"部署失败"，把一次成功的部署当成故障去排障。

    口径与 common.sh 的 _env_default 一致：显式环境变量 > 目标目录的 .env > 内置默认。
    """
    prefix = Path(a.prefix) if a.prefix else _default_prefix()
    env_file = config._parse_env_file(prefix / ".env")
    data_dir = Path(_first_nonempty(os.environ.get("CP_DATA_DIR"),
                                    env_file.get("CP_DATA_DIR"),
                                    str(prefix / "data")))
    raw_port = _first_nonempty(str(a.port) if a.port else "",
                               os.environ.get("CP_PORT"),
                               env_file.get("CP_PORT"),
                               str(config.PORT))
    port = int(raw_port) if raw_port.isdigit() else config.PORT
    return prefix, data_dir, port


def _first_nonempty(*values: str | None) -> str:
    for v in values:
        if v and v.strip():
            return v.strip()
    return ""


def _default_prefix() -> Path:
    """镜像 common.sh 的 _default_prefix：仓库结构完整就地部署，否则退回默认安装目录。"""
    if (ROOT / ".git").exists() and (ROOT / "backend" / "app" / "main.py").exists():
        return ROOT
    return Path("/root/choyeon-panel")


def _user_count(db_file: Path) -> int:
    """直接读目标库计数，不走 dbm —— dbm 绑的是 CLI 进程自己的 CP_DATA_DIR。"""
    if not db_file.is_file():
        return 0
    import sqlite3

    try:
        con = sqlite3.connect(db_file)
        try:
            row = con.execute("SELECT COUNT(*) FROM users").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return 0
    return int(row[0]) if row else 0


async def cmd_deploy(a: argparse.Namespace) -> dict:
    """一键部署：复用 scripts/install.sh，避免两套安装逻辑漂移。"""
    if os.geteuid() != 0:
        Out.error("部署需要 root 权限（面板要管理 systemd / nginx）", EXIT_PRECONDITION)
    script = ROOT / "scripts" / "install.sh"
    if not script.exists():
        Out.error(f"缺少 {script}", EXIT_PRECONDITION)

    prefix, data_dir, port = _deploy_target(a)

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
    # CLI 的 subprocess 没有 tty，install.sh 的 read_value 拿不到输入必然跳过 nginx 步骤；
    # 与其让"是否配置反代"这个决定悄悄不发生，不如显式声明并让文档给出域名接入的入口。
    env["CP_SKIP_NGINX"] = env.get("CP_SKIP_NGINX", "1")

    Out.info(f"==> 执行 {script}（可能要几分钟，前端构建较慢）")
    # 走统一的 sh() 而不是 subprocess.run(capture_output=True)：
    # 后者无 timeout，apt 等 dpkg 锁、npm ci 卡网络时会**永久挂住**且不返回任何 JSON，
    # 而 AGENTS.md 承诺 choyonctl 面向自动化、以 JSON + 退出码判断，挂死等于毁掉这个契约。
    # stdin 关成 /dev/null：install.sh 的 read_value 本已按 `[ -t 0 ]` 判交互，
    # 显式断开更稳（父 stdin 是终端时子脚本不会读到东西停在那等输入）。
    r = await sh(["bash", str(script)], env=env, stdin_devnull=True, timeout=DEPLOY_TIMEOUT_SEC)
    if not Out.json_mode:
        if r["out"]:
            print(r["out"])
        if r["err"]:
            print(r["err"], file=sys.stderr)
    if r["code"] == 124:
        Out.error(f"安装脚本超时（>{DEPLOY_TIMEOUT_SEC}s），已终止整个进程组；"
                  f"可手工执行 bash {script} 看卡在哪一步", EXIT_FAIL)
    if r["code"] != 0:
        Out.error(f"安装脚本退出码 {r['code']}", EXIT_FAIL)

    ok_health = await wait_health(30, port=port)
    admins = _user_count(data_dir / "panel.db")
    data = {
        "ok": ok_health,
        "url": f"http://127.0.0.1:{port}",
        "healthy": ok_health,
        "prefix": str(prefix),
        "dataDir": str(data_dir),
        "adminCreated": bool(admins),
        # 不再写"打开 URL 注册"：setup 只在 users 表为空时放行，而这里刚建号就可能已有
        # viewer；也不再示范 `--password <pw>`——那会把口令留在 argv、/proc 和 shell 历史里。
        "next": '创建管理员：printf \'%s\' "$PW" | choyeonctl user create admin --password-stdin',
    }
    if str(prefix.resolve()) != str(ROOT):
        data["cliHint"] = (
            f"本次安装目录是 {prefix}，而当前 CLI 属于 {ROOT}；后续命令请用 "
            f"{prefix}/bin/choyeonctl（install.sh 已把 /usr/local/bin/choyeonctl 指向它），"
            "否则会读写另一份数据目录"
        )
    if not ok_health:
        data["error"] = f"部署脚本已执行完，但 {health_url(port)} 在 30s 内没有返回健康状态"
        data["hint"] = "journalctl -u choyeon-panel -n 50 看启动失败原因；端口占用换 --port 重试"
    # 这两个命令过去只 return、从不 Out.result，而 amain 又丢弃返回值：
    # `choyeonctl deploy --json` 实际输出 0 字节且 exit 0，AGENTS.md 承诺的
    # "以 JSON 字段 + 退出码做判断"在这条主路径上完全不成立。
    Out.result(
        data,
        f"部署完成：{data['url']}  健康 {'是' if ok_health else '否'}  "
        f"已有管理员 {'是' if data['adminCreated'] else '否'}",
    )
    if not ok_health:
        raise SystemExit(EXIT_FAIL)
    return data


async def wait_health(timeout: int = 30, port: int | None = None) -> bool:
    for _ in range(timeout):
        code, body = await http_json("/api/health", timeout=3, port=port)
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
    # ✓/✗ 在非 UTF-8 控制台（如 Windows GBK）会抛 UnicodeEncodeError，
    # 让一条只读自检命令整个崩掉；用 ASCII 标记更稳。
    icon = {"pass": "[ok]", "warn": "[!]", "fail": "[x]"}
    lines = []
    for i in res["items"]:
        lines.append(f"  {icon.get(i['status'], '[?]')} {i['title']}: {i['detail']}")
        if i["fix"] and i["status"] != "pass":
            lines.append(f"      修复: {i['fix']}")
    # 登录安全那一项的 fix 只是"去看完整清单"：关口令、上 fail2ban 这些处置都要人
    # 自己 SSH 进去做，不点明会让人以为照抄 fix 那一行就算修完了。
    if any(i.get("id") == "login" and i["status"] != "pass" for i in res["items"]):
        lines.append("      完整清单与判定依据：choyeonctl ssh-audit --json")
    lines.append(f"结论: {res['status']}")
    Out.result(res, "\n".join(lines))
    return res


def _warn(msg: str):
    """告警走 stderr，--json 时 stdout 仍然是纯 JSON。"""
    print(f"[warn] {msg}", file=sys.stderr)


def _password_arg(a: argparse.Namespace) -> str | None:
    """口令优先走 stdin。

    `--password <pw>` 会留在 argv 里：同机任何用户 `ps -ef` 就能看到，Linux 下
    /proc/<pid>/cmdline 世界可读，root 的 ~/.bash_history 还会原样存一条。面板这台机器上
    跑着别人的业务应用，它们也有读 /proc 的权限，所以管理员口令对它们可见。
    脚本里改用 `--password-stdin`（`printf '%s' "$PW" | choyeonctl user create x --password-stdin`）。
    """
    if getattr(a, "password_stdin", False):
        if a.password:
            Out.error("--password 与 --password-stdin 不能同时使用", EXIT_USAGE)
        pw = sys.stdin.readline().rstrip("\r\n")
        if not pw:
            Out.error("--password-stdin 未从标准输入读到口令", EXIT_USAGE)
        return pw
    if a.password:
        _warn("--password 走命令行会被 ps 与 shell 历史看到，自动化请改用 --password-stdin")
    return a.password


async def cmd_user(a: argparse.Namespace) -> dict:
    if a.user_cmd == "list":
        rows = dbm.query("SELECT id,username,role,created_at FROM users ORDER BY id")
        Out.result(rows, "\n".join(f"{r['id']:>3}  {r['username']:<20} {r['role']}" for r in rows) or "(无用户)")
        return {"users": rows}

    if a.user_cmd == "create":
        if not a.name:
            Out.error("create 需要 <name> 与 --password（或 --password-stdin）", EXIT_USAGE)
        if not security.USERNAME_RE.match(str(a.name)):
            Out.error("用户名只能包含字母、数字、下划线与连字符（3-32 位）", EXIT_USAGE)
        pw = _password_arg(a)
        if not pw:
            Out.error("create 需要 --password（或 --password-stdin）", EXIT_USAGE)
        pw_err = security.password_error(pw)
        if pw_err:
            Out.error(pw_err, EXIT_USAGE)
        role = a.role  # argparse 已用 choices 限定 admin/viewer
        if dbm.query_one("SELECT id FROM users WHERE username=?", (a.name,)):
            Out.error(f"用户 {a.name} 已存在", EXIT_FAIL)
        dbm.execute(
            "INSERT INTO users(username,pass_hash,role) VALUES(?,?,?)",
            (a.name, security.hash_pass(pw), role),
        )
        dbm.audit(a.name, "cli:user:create", role)
        Out.result({"ok": True, "username": a.name, "role": role}, f"已创建 {a.name}（{role}）")
        return {"ok": True, "username": a.name, "role": role}

    if a.user_cmd == "reset":
        if not a.name:
            Out.error("reset 需要 <name> 与 --password（或 --password-stdin）", EXIT_USAGE)
        u = dbm.query_one("SELECT * FROM users WHERE username=?", (a.name,))
        if not u:
            Out.error(f"用户 {a.name} 不存在", EXIT_FAIL)
        pw = _password_arg(a)
        if not pw:
            Out.error("reset 需要 --password（或 --password-stdin）", EXIT_USAGE)
        pw_err = security.password_error(pw)
        if pw_err:
            Out.error(pw_err, EXIT_USAGE)
        dbm.execute("UPDATE users SET pass_hash=? WHERE username=?", (security.hash_pass(pw), a.name))
        # 改密后吊销该用户所有已签发 token，避免旧凭证继续可用
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:reset")
        Out.result({"ok": True, "username": a.name, "revokedTokens": True}, f"已重置 {a.name} 密码并吊销旧 token")
        return {"ok": True, "username": a.name}

    if a.user_cmd == "revoke":
        # 应急吊销：怀疑口令泄露 / 有人被踢出后还想立刻断掉所有已登录会话时用的。
        # 不改动口令，只做「把 epoch 抬上去」，于是所有旧 token 当场失效。
        # 给一个名字只吊销该账号；给 all 连全局 epoch 一起抬，覆盖所有用户，
        # 包括之后才被 bump 过的单账号（全局值取的是当下时间，见 security._user_epoch）。
        if not a.name:
            Out.error("revoke 需要 <name> 或 all", EXIT_USAGE)
        if a.name == "all":
            dbm.bump_token_epoch()
            dbm.audit("cli", "cli:user:revoke", "all")
            Out.result({"ok": True, "scope": "all"}, "已吊销全部用户的会话")
            return {"ok": True, "scope": "all"}
        if not dbm.query_one("SELECT id FROM users WHERE username=?", (a.name,)):
            Out.error(f"用户 {a.name} 不存在", EXIT_FAIL)
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:revoke")
        Out.result({"ok": True, "username": a.name}, f"已吊销 {a.name} 的全部会话")
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
        # 头像文件也要跟着走：/api/avatars/<id>.png 免登录可取，行删了图还在，
        # 这张脸就永久挂在一个已不存在的账号上，面板里没有任何地方能清它。
        avatars.purge(u["id"])
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:delete")
        # 删掉最后一个管理员后，面板回到「未创建管理员」状态——此时任何能访问到面板的人
        # 都可以先一步把管理员注册走。只说"已删除"没人会意识到这一点。
        left = dbm.query_one("SELECT COUNT(*) c FROM users WHERE role='admin'")["c"]
        note = "" if left else (
            "；本机已无管理员，请立即 printf '%s' \"$PW\" | choyeonctl user create <name> --password-stdin --role admin"
        )
        Out.result({"ok": True, "username": a.name, "adminLeft": left}, f"已删除 {a.name}{note}")
        return {"ok": True, "adminLeft": left}

    Out.error(f"未知子命令: {a.user_cmd}", EXIT_USAGE)
    return {}


def _tail(raw: int) -> int:
    """--lines 来自命令行，用之前先夹住。

    0 会让 `splitlines()[-0:]` 返回**整份**日志（本想只看尾巴，结果全量打印，
    部署日志上限是几十万字节）；负数会把 journalctl 直接打挂。
    """
    return max(1, min(int(raw or 1), 5000))


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
        # deploy_app 用 create_task 派发后台任务后立即返回；CLI 的 asyncio.run
        # 一退出就把这个任务取消掉，部署停在 status='running'，此后该应用无法
        # 删除/回滚/改配置（_ensure_no_deployment 拦住），直到面板重启才自愈。
        # 所以 CLI 必须等它真正结束再退。
        res = await apps_service.wait_deployment(dep, a.id)
        ok = res["status"] == "success"
        Out.result(
            {"deployment": dep, "app": a.id, "status": res["status"], "ok": ok},
            f"部署 #{dep} 结束：{res['status']}（用 app logs --id {a.id} 查看日志）",
        )
        if not ok:
            raise SystemExit(EXIT_FAIL)
        return {"deployment": dep, "app": a.id, "status": res["status"], "ok": ok}

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
        tail = "\n".join(log.splitlines()[-_tail(a.lines):])
        Out.result({"id": row["id"], "status": row["status"], "commit": row["commit_sha"], "log": tail}, tail)
        return {"id": row["id"], "status": row["status"], "log": tail}

    Out.error(f"未知子命令: {a.app_cmd}", EXIT_USAGE)
    return {}


async def cmd_backup(a: argparse.Namespace) -> dict:
    if a.backup_cmd == "run":
        script = ROOT / "scripts" / "backup.sh"
        if not script.exists():
            Out.error(f"缺少 {script}", EXIT_PRECONDITION)
        r = await sh(["bash", str(script)], timeout=300, stdin_devnull=True)
        if r["code"] == 124:
            Out.error("备份脚本超时（>300s），已终止", EXIT_FAIL)
        if r["code"] != 0:
            Out.error(r["err"] or "备份失败", EXIT_FAIL)
        files = sorted(Path(config.BACKUP_DIR).glob("manual/panel-*.tar.gz"), key=os.path.getmtime)
        latest = str(files[-1]) if files else None
        Out.result({"ok": True, "file": latest}, f"备份完成：{latest}")
        return {"ok": True, "file": latest}

    if a.backup_cmd == "list":
        # 与 doctor 的 _recent_backups 保持同一套后缀，
        # 否则 `backup list` 只列 tar 包，定时跑出来的 PG 备份在 CLI 里像不存在一样。
        suffixes = (".tar.gz", ".sql.gz", ".dump.gz", ".sql", ".dump", ".gz")
        files = [
            f
            for f in Path(config.BACKUP_DIR).glob("**/*")
            if f.is_file() and f.name.endswith(suffixes)
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
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
    healthy = await wait_health(30) if verb in ("start", "restart") else True
    data = {"ok": healthy, "action": verb, "healthy": healthy}
    Out.result(data, f"{verb} 完成" if healthy else f"{verb} 已执行，但健康检查未通过")
    # AGENTS.md 承诺"以 JSON 字段 + 退出码做判断"：服务没起来却回 ok:true/exit 0，
    # 自动化就会在挂掉的实例上继续往下跑下一步。
    if not healthy:
        raise SystemExit(EXIT_FAIL)
    return data


async def cmd_logs(a: argparse.Namespace) -> dict:
    if have_systemd():
        r = await sh(["journalctl", "-u", SERVICE, "-n", str(_tail(a.lines)), "--no-pager"], timeout=30)
        text = r["out"]
    else:
        text = "(无 systemd，请查看启动终端输出)"
    Out.result({"lines": text.splitlines()}, text)
    return {"lines": text.splitlines()}


async def cmd_upgrade(a: argparse.Namespace) -> dict:
    script = ROOT / "scripts" / "update.sh"
    if not script.exists():
        Out.error(f"缺少 {script}", EXIT_PRECONDITION)
    # AGENTS.md §7.5 把 `choyeonctl upgrade --no-pull` 写成"禁止手工编辑 unit"的正规替代路径
    # （改配置后重新渲染 unit + 重建 + 重启，不拉代码），update.sh 也早已支持该参数；
    # 但 CLI 侧一直没透传，照文档执行会得到 exit 2「用法错误」——文档指向的出口本身是坏的。
    cmd = ["bash", str(script)]
    if a.no_pull:
        cmd.append("--no-pull")
    r = await sh(cmd, timeout=DEPLOY_TIMEOUT_SEC, stdin_devnull=True)
    if r["code"] == 124:
        Out.error(f"升级脚本超时（>{DEPLOY_TIMEOUT_SEC}s），已终止整个进程组；"
                  "升级失败时脚本本应自动回滚，请先 choyeonctl status 确认现场", EXIT_FAIL)
    if r["code"] != 0:
        Out.error(r["err"] or "升级失败（脚本已尝试自动回滚）", EXIT_FAIL)
    healthy = await wait_health(30)
    data = {"ok": healthy, "healthy": healthy}
    Out.result(data, "升级完成" if healthy else "升级后健康检查未通过")
    if not healthy:
        raise SystemExit(EXIT_FAIL)
    return data


async def cmd_firewall(a: argparse.Namespace) -> dict:
    """防火墙读写：调用的是面板防火墙页同一套服务，也因此吃同一套自锁守卫。

    刻意不提供 --force / --yes-i-know 之类的绕过参数。CLI 多半是脚本在跑，脚本读到
    "SSH 可能要不通了"不会停下来想一想；把它做成不可绕过，人才是安全的。
    """
    verb = a.firewall_cmd
    tool = await firewall_service.detect()

    if verb != "list" and tool not in (firewall_service.UFW, firewall_service.FIREWALLD):
        Out.error("这台机器上既没有 ufw 也没有 firewalld，没有可管理的防火墙", EXIT_PRECONDITION)

    try:
        if verb == "list":
            data = await firewall_service.read(request=None)
        elif verb in ("allow", "deny"):
            if a.port is None:
                Out.error("allow/deny 需要 --port（端口区间再加 --port-to）", EXIT_USAGE)
            body = {"port": a.port, "action": verb, "proto": a.proto}
            if a.port_to is not None:
                body["port_to"] = a.port_to
            if a.source:
                body["source"] = a.source
            if a.comment:
                body["comment"] = a.comment
            if a.rate:
                body["rate"] = True
            data = await firewall_service.add_rule(body, tool)
        elif verb == "remove":
            if a.num is None or not a.expect:
                Out.error("remove 需要 --num 与 --expect：编号会随任何一次删除前移，"
                          "先用 choyeonctl firewall list --json 读回那条规则的 sig", EXIT_USAGE)
            data = await firewall_service.remove_rule(a.num, a.expect, tool)
        else:
            if verb == "off" and not a.yes:
                Out.error("停用防火墙会让所有监听端口直接暴露到公网，确认请加 --yes", EXIT_USAGE)
            data = await firewall_service.set_enabled(verb == "on", tool)
    except BadInput as e:
        # 参数写歪属于"用法错误"，让调用方按 AGENTS.md 的退出码表把它和真正的执行失败分开处理
        Out.error(public_message(e), EXIT_USAGE)
    except Exception as e:  # noqa: BLE001
        Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)

    if verb == "list":
        Out.result(data, _firewall_table(data))
        return data
    # 删除成功的答复要带上删掉了什么：那个结果名是服务重新列表确认过的，
    # 只回一句"已执行"等于让用户再跑一次 list 才敢相信。
    text = data.get("detail") or ""
    if verb == "remove":
        text = "已删除 " + (data.get("removed") or "") + ("；" + text if text else "")
        for extra in data.get("still_open") or []:
            # ufw 的 allow 一次写 v4+v6 两条，delete 只删一条；不把孪生那条说出来，
            # 用户会以为这个端口已经不对外开放了。
            text += f"\n注意：{extra} 仍在放行同一端口，需要单独删除"

    Out.result(data, text or "已执行")
    return data


def _firewall_table(data: dict) -> str:
    """`firewall list` 的人类可读形态：与表格页同样的列，另把 sig 印出来——
    remove 需要它，而让人再去跑一次 --json 只会多一次可被删错的时间窗。"""
    rules = data.get("rules") or []
    reasons = data.get("protected_reasons") or {}
    head = f"{data.get('tool', 'none')} · " + ("已启用" if data.get("active") else "未启用")
    if data.get("default_incoming"):
        head += f" · 默认入站 {data['default_incoming']}"
    if data.get("zone"):
        head += f" · 默认区域 {data['zone']}"
    lines = [head]
    if data.get("protected"):
        lines.append("受保护端口（删掉这些端口唯一的放行来源会被拒绝）："
                     + "、".join(f"{p}（{reasons.get(str(p), '')}）" for p in data["protected"]))
    for r in rules:
        lo, hi = r.get("port"), r.get("port_to")
        span = "任意端口" if lo is None else (str(lo) if hi in (None, lo) else f"{lo}-{hi}")
        cols = [f"[{r.get('num', '')}]", span, r.get("token") or "",
                r.get("action") or "", r.get("source") or "Anywhere",
                "IPv6" if r.get("family") == "ipv6" else "IPv4",
                "受保护" if r.get("protected") else ""]
        lines.append("  ".join(cols).rstrip() + f"  sig={r.get('sig', '')}")
    lines.append(f"共 {len(rules)} 条")
    return "\n".join(lines)


async def cmd_updates(a: argparse.Namespace) -> dict:
    """待装补丁：只读回答"还差哪些更新、其中哪些是安全更新"。

    面板不代跑 `apt update` / `apt upgrade`：前者写索引并和 dpkg 抢锁，后者会顺带重启
    别的服务、可能换内核，而且失败了要人盯。命令给出来，按不按由人决定。
    """
    data = await updates_service.detect(refresh=bool(a.refresh))
    if not data.get("supported"):
        Out.error("未识别的发行版或没有受支持的包管理器，无法判断待装更新", EXIT_PRECONDITION)
    Out.result(data, _updates_text(data))
    return data


def _updates_text(d: dict) -> str:
    lines = [f"{d.get('distro') or '未知发行版'} · {d.get('family')}"]
    sec = d.get("security_count")
    total = int(d.get("pending_total") or 0)
    head = f"待装 {total} 个" + (
        f"，其中安全更新 {sec} 个" if sec is not None else "（该发行版判不出安全更新子集）"
    )
    auto = d.get("auto_updates") or {}
    if auto.get("state") == "enabled":
        head += f" · 自动安全更新已开启（{auto.get('unit')}）"
    elif auto.get("state") == "disabled":
        head += " · 自动安全更新未开启"
    lines.append(head)
    if d.get("reboot_required"):
        lines.append("上次更新要求重启才生效")
    for n in d.get("notes") or []:
        lines.append(f"[note] {n}")
    pkgs = d.get("packages") or []
    shown = pkgs[:20]
    for p in shown:
        mark = "  [security]" if p.get("security") else ""
        lines.append(f"  {p['name']:<38} {p.get('current') or '-':<26} -> {p.get('candidate') or '?'}{mark}")
    if len(pkgs) > len(shown):
        lines.append(f"  ...共 {len(pkgs)} 个，完整清单用 --json")
    cmds = d.get("commands") or {}
    if sec and cmds.get("apply_security"):
        lines.append(f"只装安全更新：{cmds['apply_security']}")
    if total and cmds.get("apply_all"):
        lines.append(f"全部升级：  {cmds['apply_all']}")
    lines.append(f"检查时间 {d.get('checked_at')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_ssh_audit(a: argparse.Namespace) -> dict:
    """登录安全对账：这台机器的正门是 SSH，不是面板。

    只读——不写 sshd_config、不 reload sshd、不碰 authorized_keys。SSH 一旦改错
    就是把人锁在门外，而面板自己也从那扇门进来，所以这里只给判词和可复制的命令。
    """
    data = await login_audit_service.audit(refresh=bool(a.refresh))
    src = (data.get("sshd") or {}).get("source")
    if src in ("missing", "unreadable"):
        # 读不到 sshd 配置就没有对账的根基，编一份"看起来没问题"的清单比不查更坏。
        Out.error(f"读不到 sshd 配置（{src}），无法对账", EXIT_PRECONDITION)
    Out.result(data, _ssh_audit_text(data))
    return data


def _ssh_audit_text(d: dict) -> str:
    ssh = d.get("sshd") or {}
    att = d.get("attempts") or {}
    counts = d.get("counts") or {}
    kk = d.get("keys") or {}
    acc = d.get("accounts") or {}
    reach = {True: "对外可达", False: "仅本机或内网", None: "可达性判不出"}[ssh.get("reachable")]
    lines = [
        f"登录安全对账（配置来源：{ssh.get('source') or '—'} · sshd 端口 {ssh.get('port') or '—'} · {reach}）",
        f"概览：要紧 {counts.get('risk', 0)} 项 · 值得处理 {counts.get('warn', 0)} 项 · "
        f"知道就好 {counts.get('note', 0)} 项 · 过去 24 小时失败认证 {att.get('failed', 0)} 次"
        f"（{att.get('distinct_ips', 0)} 个来源地址）",
    ]
    for f in d.get("findings") or []:
        sev = login_audit_service.SEV_LABEL.get(f["severity"], f["severity"])
        lines.append(f"[{sev}] {f['title']}")
        if f.get("detail"):
            lines.append(f"    {f['detail']}")
        if f.get("fix"):
            lines.append(f"    处置：{f['fix']}")
    if not d.get("findings"):
        lines.append("结论：登录方式没有查到需要处理的问题。")

    lines.append("sshd 关键配置：")
    for r in d.get("settings") or []:
        flag = r.get("flag") or ""
        mark = f"  ← {login_audit_service.SEV_LABEL.get(flag, flag)}" if flag else ""
        lines.append(f"  {r['label']} = {r['value']}{mark}")
        if r.get("fix"):
            lines.append(f"      建议：{r['fix']}")

    lines.append(
        f"授权密钥：{kk.get('total_keys', 0)} 把，覆盖 {kk.get('scanned', 0)} 个账号"
        "（只列指纹与元信息，不读私钥，也不回显公钥正文）"
    )
    for p in kk.get("files") or []:
        lines.append(f"  {p['user']}  {p['path']}（{p['mode']} · 属主 {p['owner']}）")
        for k in p.get("keys") or []:
            tail = " · 算法偏弱" if k.get("weak") else ""
            lines.append(f"    {k['algo']} {k['bits']}位  {k['comment'] or '（无备注）'}  {k['fp']}{tail}")
        for flag in p.get("flags") or []:
            lines.append(f"    注意：{flag}")

    acc_line = (
        f"账号面：可登录 {acc.get('login_count', 0)} 个 · 空口令 {len(acc.get('empty_password') or [])} 个 · "
        f"额外 uid=0 {len(acc.get('uid0_extra') or [])} 个"
    )
    lines.append(acc_line + (" · /etc/shadow 读不到，空口令这项没核对" if acc.get("shadow_unreadable") else ""))
    for g, members in (acc.get("sudo_groups") or {}).items():
        lines.append(f"  {g} 组：{'、'.join(members) or '（无成员）'}")

    if d.get("guard"):
        parts = []
        for g in d["guard"]:
            state = "在跑" if g.get("active") else ("未运行" if g.get("installed") else "未安装")
            parts.append(f"{g['name']} {state}")
        lines.append("爆破防护：" + "、".join(parts))
    top = att.get("top") or []
    if top:
        lines.append("失败来源：" + "、".join(f"{t['ip']}（{t['count']} 次）" for t in top))
    ok = att.get("accepted") or []
    if ok:
        lines.append("成功登录方式：" + "、".join(f"{x['method']} {x['count']} 次" for x in ok))
    for n in d.get("notes") or []:
        lines.append(f"说明：{n}")
    lines.append("这一层只读：不写 sshd_config、不 reload sshd、不改 authorized_keys。")
    return "\n".join(lines)


async def cmd_ports(a: argparse.Namespace) -> dict:
    """端口暴露面：回答"这台机器对外开了哪些口子"。

    只读——不改绑定、不动防火墙。把一个端口从全网收回回环要改的是那个服务自己的启动
    参数，CLI 替不了它，猜错了还会把一个正常服务关掉，所以这里只给判词和建议。
    """
    data = await exposure_service.audit(refresh=bool(a.refresh))
    if not data.get("ports"):
        Out.error("读不到监听端口表（缺 ss 或 ss 执行失败），无法判断暴露面", EXIT_PRECONDITION)
    Out.result(data, _ports_text(data))
    return data


def _ports_text(d: dict) -> str:
    c = d.get("counts") or {}
    fw = d.get("firewall") or {}
    lines = [
        f"监听端口 {c.get('total', 0)} 个：外部可达 {c.get('exposed', 0)}、被防火墙挡住 {c.get('blocked', 0)}、"
        f"预期内 {c.get('expected', 0)}、仅本机 {c.get('internal', 0)}"
    ]
    lines.append(
        f"防火墙 {fw.get('tool')}{'：已启用' if fw.get('active') else '：未启用/未安装'}，"
        f"默认入站 {fw.get('default_incoming') or '未知'}"
    )
    for n in d.get("notes") or []:
        lines.append(f"[note] {n}")
    risky = [p for p in d.get("ports") or [] if p["verdict"] != exposure_service.INTERNAL]
    if risky:
        for p in risky:
            who = "、".join(p["processes"]) or "未知进程"
            lines.append(f"  [{p['verdict']}] {str(p['port']):<6} {who:<24} {p['addrs'][0]}")
            lines.append(f"          {p['reason']}")
            if p.get("fix"):
                lines.append(f"          建议：{p['fix']}")
    else:
        lines.append("对外端口都在预期清单里，没有多开的口子")
    lines.append(f"仅本机监听的 {c.get('internal', 0)} 个端口用 --json 看")
    lines.append(f"检查时间 {d.get('checked_at')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_storage(a: argparse.Namespace) -> dict:
    """存储占用对账：磁盘余量那一项只说"还剩多少"，这里回答"被谁占掉了"。

    只读——不删文件、不改配额、不动备份任务的 keep。删除旧备份有自己的流程
    （choyeonctl backup list 看清再动手），扫盘替人决定删哪个文件风险太大。
    """
    data = await storage_service.audit(refresh=bool(a.refresh))
    if not any(r.get("exists") for r in data.get("roots") or []):
        Out.error("登记的目录一个都没读到，无法对账", EXIT_PRECONDITION)
    Out.result(data, _storage_text(data))
    return data


def _storage_text(d: dict) -> str:
    c = d.get("counts") or {}
    disk = d.get("disk") or {}
    jr = d.get("journal") or {}
    lines = ["存储占用对账（只读：不删文件、不改配额）"]
    if disk.get("ok"):
        lines.append(
            f"分区（数据目录所在）共 {disk.get('total_human')}，已用 {disk.get('used_pct')}%，"
            f"剩 {disk.get('free_human')}"
        )
    else:
        lines.append("读不到分区信息，占用数字仍然有效，但没有总量可对")
    lines.append(f"systemd 日志：{jr.get('bytes_human') or '读不到'}")
    mts = d.get("mounts") or {}
    if mts:
        if not mts.get("ok"):
            lines.append(f"挂载点：{mts.get('reason') or '读不到 /proc/mounts'}，inode 与只读状态没法拉出来")
        else:
            def _pct(v):
                return "—" if v is None else f"{v}%"

            panel = mts.get("panel_mount")
            lines.append(f"挂载点 {mts.get('total', 0)} 个（面板数据在 {panel or '未知'}）：")
            for m in mts.get("items") or []:
                if m.get("unknown"):
                    lines.append(f"  {m['path']:<24} {m.get('fstype', '?'):<8} "
                                 f"挂着却读不到（{m.get('device', '?')}）")
                    continue
                marks = []
                if m.get("ro"):
                    marks.append("只读镜像" if m.get("snapshot") else "只读")
                if m.get("same_dev_as"):
                    marks.append(f"与 {m['same_dev_as']} 是同一块盘")
                if m.get("panel"):
                    marks.append("面板数据在这")
                line = (f"  {m['path']:<24} {m.get('fstype', '?'):<8} 已用 {_pct(m.get('used_pct'))}"
                        f"  剩 {m.get('free_human', '—')}  inode {_pct(m.get('inode_pct'))}")
                lines.append(line + ("  " + "，".join(marks) if marks else ""))
            if int(mts.get("omitted") or 0) > 0:
                lines.append(f"  另有 {mts['omitted']} 个挂载点没列出（都不比上面这些吃紧）")

    lines.append(f"合计 {c.get('bytes_total_human')}，{c.get('roots', 0)} 个目录：")
    for r in d.get("roots") or []:
        if not r.get("exists"):
            lines.append(f"  {r['label']}  {r['path']} —— 目录不存在")
            continue
        tail = f"{r['files']} 个文件"
        if r.get("nested_in"):
            tail += f"，与 {r['nested_in']} 重叠，合计只算外层那一份"
        if r.get("partial"):
            tail += "，没扫完，数字是下界"
        if r.get("errors"):
            tail += f"，{r['errors']} 处读不到"
        lines.append(f"  {r['label']} {r['bytes_human']}  {r['path']}（{tail}）")
        subs = r.get("subdirs") or []
        for sd in subs:
            lines.append(f"      {sd['name']}/  {sd['bytes_human']}")
        rest = int(r.get("branch_count") or 0) - len(subs)
        if subs and rest > 0:
            lines.append(f"      另有 {rest} 个一级子目录未列出（都不比上面这些大）")
        bak = r.get("backups") or {}
        if bak.get("count"):
            lines.append(
                f"      备份产物 {bak['count']} 份：最早 {bak.get('oldest')}，最新 {bak.get('newest')}"
            )

    lines.append(
        f"判词：要紧 {c.get('risk', 0)} · 值得处理 {c.get('warn', 0)} · 知道就好 {c.get('info', 0)}"
    )
    for f in d.get("findings") or []:
        sev = storage_service.SEV_LABEL.get(f["severity"], f["severity"])
        lines.append(f"  [{sev}] {f['title']}")
        if f.get("detail"):
            lines.append(f"      {f['detail']}")
        if f.get("fix"):
            lines.append(f"      处置：{f['fix']}")
    if not d.get("findings"):
        lines.append("  没有异常占用。")

    big = d.get("big_files") or []
    if big:
        lines.append(f"最大的 {len(big)} 个文件：")
        for b in big:
            lines.append(f"  {b['bytes_human']}  {b['path']}")

    for n in d.get("notes") or []:
        lines.append(f"说明：{n}")
    lines.append(f"检查时间 {d.get('as_of')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_memory(a: argparse.Namespace) -> dict:
    """内存压力与 OOM 对账：概览的内存仪表只说"占了多少"，这里回答"有没有在缺、谁在拿着、杀过没有"。

    只读——不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh。被 OOM 杀掉的进程不会
    自己报告，应用侧只表现为莫名其妙重启，这一项就是为了让那种事故在终端里也一眼可见。
    """
    data = await memory_service.audit(refresh=bool(a.refresh))
    if not (data.get("mem") or {}).get("ok"):
        Out.error((data.get("mem") or {}).get("reason") or "读不到 /proc/meminfo，无法对账",
                  EXIT_PRECONDITION)
    Out.result(data, _memory_text(data))
    return data


def _memory_text(d: dict) -> str:
    hu = memory_service._human
    c = d.get("counts") or {}
    mem = d.get("mem") or {}
    press = d.get("pressure") or {}
    rates = d.get("rates") or {}
    lines = ["内存压力与 OOM 对账（只读：不 kill 进程、不改 sysctl、不动 unit 上限）"]

    if mem.get("ok"):
        lines.append(
            f"总量 {hu(mem.get('total'))}：已用 {mem.get('used_pct')}%（{hu(mem.get('used'))}），"
            f"可腾出 {hu(mem.get('available'))}，其中页缓存与可回收 slab {hu(mem.get('reclaimable'))}"
        )
        devs = "、".join(f"{x['name']} {x['type']}" for x in mem.get("devices") or [])
        if mem.get("swap_total"):
            lines.append(
                f"swap 已用 {mem.get('swap_pct')}%（{hu(mem.get('swap_used'))} / "
                f"{hu(mem.get('swap_total'))}，{devs or '设备表读不到'}）"
            )
        else:
            lines.append("没有启用 swap：物理内存顶到头就直接进 OOM，没有换出的缓冲")
    else:
        lines.append(mem.get("reason") or "读不到 /proc/meminfo")

    some = press.get("some") if press.get("ok") else None
    if some:
        stalled = int((some.get("total") or 0) // 1_000_000)
        lines.append(
            f"PSI 停顿 some：avg10 {some.get('avg10')}% · avg300 {some.get('avg300')}%"
            f" · 自启动累计因缺内存阻塞 {stalled} 秒"
        )
    else:
        lines.append(f"PSI：{(press.get('reason') or '读不到') if not press.get('ok') else '读不到'}")

    if rates.get("ok"):
        seg = []
        for key, label, as_bytes in (("swap_in", "换入", True), ("swap_out", "换出", True),
                                     ("direct_reclaim", "直接回收", False),
                                     ("allocstall", "同步回收", False),
                                     ("majfault", "主缺页", False)):
            v = rates.get(key)
            if isinstance(v, int):
                seg.append(f"{label} {hu(v) if as_bytes else v}/秒")
        lines.append(f"{rates.get('secs')} 秒采样窗口：" +
                     ("、".join(seg) if seg else "速率全都读不出（这个窗口没采到，不是 0）"))
    else:
        lines.append("速率：本轮没有采到有效窗口")

    oom = d.get("oom") or {}
    kills = int(oom.get("kernel_total") or 0)
    uk = oom.get("unit_kills") or {}
    if kills or uk:
        head = f"OOM：内核自启动以来杀掉过 {kills} 个进程"
        if uk:
            head += f"，单元自己的上限另杀 {sum(int(v) for v in uk.values())} 次"
        lines.append(head)
        for e in oom.get("events") or []:
            who = (f"{e.get('comm')}({e.get('pid')})" if e.get("comm")
                   else (e.get("line") or "")[:70])
            lines.append(f"  {e.get('at')}  {who}")
        if oom.get("reason"):
            lines.append(f"  {oom['reason']}")
    else:
        lines.append("OOM：自启动以来没有杀过进程")

    units = d.get("units") or {}
    if units.get("ok"):
        rows = units.get("items") or []
        more = f"，另有 {units.get('omitted')} 个更小的没列" if units.get("omitted") else ""
        lines.append(f"占内存最多的 {len(rows)} 个单元（system.slice 共 {units.get('seen')} 个{more}）：")
        for r in rows:
            who = "面板自己" if r.get("self") else (r.get("app") or "")
            tail = []
            if isinstance(r.get("pct_of_host"), int):
                tail.append(f"整机 {r['pct_of_host']}%")
            if isinstance(r.get("pct_of_cap"), int):
                tail.append(f"自己上限 {r['pct_of_cap']}%")
            if r.get("kind") == "slice":
                tail.append("分组，含子单元")
            ev = r.get("events") or {}
            if ev.get("oom_kill") or ev.get("max"):
                tail.append(f"被上限杀 {ev.get('oom_kill', 0)} 次 / 拦过 {ev.get('max', 0)} 次")
            mark = f"  [{who}]" if who else ""
            extra = f"  （{'、'.join(tail)}）" if tail else ""
            lines.append(f"  {r['unit']:<36}{hu(r.get('current')):>9}{mark}{extra}")
    else:
        lines.append(f"单元表：{units.get('reason') or '读不到'}")

    procs = d.get("procs") or {}
    if procs.get("ok"):
        rows = procs.get("items") or []
        cut = "，这一轮没扫完" if procs.get("budget_hit") else ""
        lines.append(f"占内存最多的 {len(rows)} 个进程（全机 {procs.get('count')} 个{cut}）：")
        for r in rows:
            owner = (r.get("app") or r.get("unit")
                     or ("登录会话，不归面板管" if r.get("kind") == "scope" else "归不到单元"))
            lines.append(f"  {r['pid']:>7}  {hu(r.get('rss')):>9}  "
                         f"{(r.get('comm') or '')[:18]:<18}  {owner}")
    else:
        lines.append(f"进程表：{procs.get('reason') or '读不到'}")

    lines.append(
        f"判词：要紧 {c.get('risk', 0)} · 值得处理 {c.get('warn', 0)} · 知道就好 {c.get('info', 0)}"
    )
    for f in d.get("findings") or []:
        sev = memory_service.SEV_LABEL.get(f["severity"], f["severity"])
        lines.append(f"  [{sev}] {f['title']}")
        if f.get("detail"):
            lines.append(f"      {f['detail']}")
        if f.get("fix"):
            lines.append(f"      处置：{f['fix']}")
    if not d.get("findings"):
        lines.append("  没有内存压力判词。")

    for n in d.get("notes") or []:
        lines.append(f"说明：{n}")
    lines.append(f"检查时间 {d.get('as_of')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_pending(a: argparse.Namespace) -> dict:
    """改动未生效对账：补丁装上了、配置改了，但还在跑的进程没吃到它。

    严格只读——不 restart、不 reload、不 daemon-reload、不 reboot。安全补丁装上而进程没重启，
    是「以为修了」和「真修了」之间最容易出事的缝；而什么时候能断业务连接重启一个服务只有管理员
    知道，面板替你按下去就是在业务高峰里掐连接，所以这里只给能当场复制走的命令。
    """
    data = await pending_service.audit(refresh=bool(a.refresh))
    if not (data.get("kernel") or {}).get("ok") and not (data.get("stale") or {}).get("ok"):
        Out.error((data.get("kernel") or {}).get("reason")
                  or (data.get("stale") or {}).get("reason")
                  or "读不到内核与进程映射信息，无法对账", EXIT_PRECONDITION)
    Out.result(data, _pending_text(data))
    return data


def _cpad(text: str, width: int) -> str:
    """按显示宽度补空格：中日韩字符在等宽终端里占两格，用 str.ljust 会把中文那一列排歪。"""
    w = sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)
    return text + " " * max(0, width - w)


def _pending_text(d: dict) -> str:
    c = d.get("counts") or {}
    k = d.get("kernel") or {}
    st = d.get("stale") or {}
    rl = d.get("reloads") or {}
    lines = ["改动未生效对账（只读：不 restart、不 reload、不 daemon-reload、不 reboot）"]

    if k.get("ok"):
        verdict = {True: "需要重启", False: "不需要重启"}.get(k.get("needs_reboot"), "判定不了")
        newer = ", ".join(x.get("pkg", "") for x in (k.get("newer") or [])[:3])
        mark = "挂着" if (k.get("marker") or {}).get("present") else "没有"
        lines.append(f"内核：running {k.get('running')}，已装 {len(k.get('installed') or [])} 个镜像包"
                     + (f"，比它新的：{newer}" if newer else "") + f"；重启标记{mark} → {verdict}")
        if k.get("reason"):
            lines.append(f"  说明：{k['reason']}")
    else:
        lines.append(f"内核：{k.get('reason') or '读不到'}")

    if st.get("ok"):
        rows = st.get("units") or []
        cut = "，预算用完这一轮没扫完" if st.get("budget_hit") else ""
        lines.append(f"踩旧映射：看了 {st.get('scanned')} 个进程（全机 {st.get('procs_total')} 个，"
                     f"{st.get('elapsed_ms')} 毫秒{cut}），其中 {st.get('procs_stale')} 个踩着已被替换的文件，"
                     f"归成 {st.get('unit_total')} 个单元（{st.get('orphan_total')} 个不归 systemd 管）")
        for r in rows:
            labels = sorted({(f.get("pkg") or f["path"].rsplit("/", 1)[-1]) for f in r.get("files") or []})
            shown = r["unit"] or "(不属于任何单元)"
            star = "  ← 面板自己" if r.get("self") else ""
            lines.append("  " + _cpad(shown[:34], 36) + f" {r['procs']:>3} 进程  "
                         f"跑了 {_cpad(r['age_human'], 7)} {', '.join(labels[:3])[:46]}{star}")
        if st.get("unit_total", 0) > len(rows):
            lines.append(f"  （另有 {st['unit_total'] - len(rows)} 个单元没列出来）")
    else:
        lines.append(f"踩旧映射：{st.get('reason') or '读不到'}")

    for key, name in (("nginx", "nginx"), ("sshd", "sshd"), ("systemd", "systemd")):
        v = rl.get(key) or {}
        if not v.get("ok"):
            lines.append(f"{name}：{v.get('reason') or '读不到'}")
        elif key == "systemd":
            need = v.get("need_reload") or []
            lines.append(f"{name}：查了 {v.get('checked')} 个单元，"
                         + (f"{len(need)} 个待 daemon-reload（{', '.join(need[:4])}）" if need else "没有待重载"))
        else:
            pend = {True: "有待重载的改动", False: "没有待重载的改动"}.get(v.get("pending"), "判定不了")
            cnt = v.get("conf_files") if key == "nginx" else v.get("files")
            newest = str(v.get("newest_file") or "").rsplit("/", 1)[-1]
            extra = f"，最晚改的是 {newest}" if newest else ""
            lines.append(f"{name}：{cnt} 个配置文件参与比较{extra} → {pend}")

    apt = d.get("apt") or {}
    if apt.get("ok") and apt.get("last_start_date"):
        lines.append(f"最近一次 apt 事务：{apt['last_start_date']}（历史里共 {apt.get('transactions')} 笔）")

    lines.append(f"判词：要紧 {c.get('risk', 0)} · 值得处理 {c.get('warn', 0)} · 知道就好 {c.get('info', 0)}")
    for f in d.get("findings") or []:
        sev = pending_service.SEV_LABEL.get(f["severity"], f["severity"])
        lines.append(f"  [{sev}] {f['title']}")
        if f.get("detail"):
            lines.append(f"      {f['detail']}")
        for it in (f.get("files") or [])[:4]:
            pkg = it["pkg"] + (f" {it['version']}" if it.get("version") else "") if it.get("pkg") else "非 dpkg 归属"
            gone = "，路径整个没了" if it.get("gone") else ""
            lines.append(f"      · {it['path']}  [{pkg}]{gone}")
        if f.get("fix"):
            lines.append(f"      处置：{f['fix']}")
    if not d.get("findings"):
        lines.append("  没有待生效的改动。")

    for n in d.get("notes") or []:
        lines.append(f"说明：{n}")
    lines.append(f"检查时间 {d.get('as_of')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_schema(_a: argparse.Namespace) -> dict:
    """机器可读命令契约：AI 智能体读这一份就够，不用翻源码。"""
    schema = {
        "cli": "choyeonctl",
        "version": config.VERSION,
        "conventions": {
            "json": "所有命令支持全局 --json，输出结构化结果",
            "exitCodes": {"0": "成功", "1": "执行失败", "2": "用法错误", "3": "前置条件不满足"},
            "nonInteractive": "口令通过 --password-stdin 从标准输入传入"
            "（argv 形式仅限手工排障，会留在 ps 与 shell 历史里）；破坏性操作需显式 --yes",
        },
        "commands": [
            {"name": "deploy", "args": ["--prefix", "--port", "--branch", "--skip-nginx", "--no-deps"],
             "desc": "一键部署（依赖→构建→systemd→健康检查）",
             "notes": ["--branch 只在全新克隆时生效；安装目录已是 git 仓库时代码由 update.sh 拉取",
                       "--prefix 默认取当前 CLI 所在的仓库目录（结构完整时就地部署）",
                       "非交互执行，不会询问 nginx 反代；接入域名见 AGENTS.md 步骤 4"]},
            {"name": "upgrade", "args": ["--no-pull"],
             "desc": "升级到最新代码，失败自动回滚；--no-pull 只重建并重启（改配置后重新渲染 unit）"},
            {"name": "status", "args": [], "desc": "版本/监听/服务/健康/自检汇总"},
            {"name": "ssh-audit", "args": ["--refresh"],
             "desc": "登录安全只读对账（sshd 有效配置 / 密钥与文件权限 / 空口令与 uid0 / 24h 失败来源）",
             "notes": ["严格只读：不写 sshd_config、不 reload sshd、不改 authorized_keys——"
                       "SSH 是这台机器的主入口，改错就是把人锁在门外",
                       "配置来源会如实标注：sshd -T 是有效值，回落 sshd_config 时含注释掉的行",
                       "非 root 时 /etc/shadow 与他人 authorized_keys 读不到，在 notes 里说明而不是跳过",
                       "公钥只列算法/位数/备注/指纹，不回显公钥正文",
                       "读不到 sshd 配置时退出码 3；报告能产出就回 0，风险与否看 JSON 的 counts"]},
            {"name": "doctor", "args": [], "desc": "安全与健康自检，含每项修复建议"},
            {"name": "user list|create|reset|revoke|delete",
             "args": ["<name>", "--password", "--password-stdin", "--role", "--yes"],
             "desc": "账号管理（改密会吊销旧 token；revoke 只吊销会话不改口令）",
             "notes": ["create/reset 优先 --password-stdin：argv 里的口令同机用户 ps 可见",
                       "revoke <name> 只踢这个账号；revoke all 抬全局版本号，全员旧票一起失效"]},
            {"name": "app templates|list|create|deploy|rollback|logs",
             "args": ["--id", "--name", "--template", "--type", "--repo", "--branch", "--path",
                      "--port", "--domain", "--start-cmd", "--install-cmd", "--lines"],
             "desc": "应用全生命周期管理",
             "notes": ["create 需要 --template 或 --start-cmd 之一；用 --start-cmd 时可配 --type/--install-cmd"]},
            {"name": "backup run|list", "args": [], "desc": "面板数据备份"},
            {"name": "firewall list|allow|deny|remove|on|off",
             "args": ["--port", "--port-to", "--proto", "--source", "--comment", "--rate",
                      "--num", "--expect", "--yes"],
             "desc": "本机 ufw/firewalld 读写（与面板防火墙页同一套守卫）",
             "notes": ["拒绝任何会让这台机器进不去的写操作：sshd 监听端口、反代到面板的 nginx 入口端口、"
                                    "面板自身在非 loopback 上的监听端口，以及当前请求连进来的端口；没有绕过参数",

                       "remove 必须带 list 里读回的 sig（--expect）：编号会随任何一次删除前移",
                       "off 需要 --yes；on 会先确认 SSH 与面板入口端口已经在放行列表里",
                       "参数写歪退出码 2（用法错误），被自锁守卫拦下与防火墙自身报错为 1"]},
            {"name": "updates", "args": ["--refresh"],
             "desc": "待装系统补丁与安全更新（只读探测，面板不代跑 apt update/upgrade）",
             "notes": ["结论来自本地包索引；索引过旧会在 notes 里如实说明",
                       "判不出安全更新子集的发行版报 security_count=null，不会填 0 冒充没有",
                       "supported=false 时退出码 3（前置条件不满足）"]},
            {"name": "ports", "args": ["--refresh"],
             "desc": "监听端口与对外可达性对账（只读：不改绑定、不动防火墙）",
             "notes": ["判词四档：internal 仅本机、expected 预期对外、blocked 绑在全网但防火墙未放行、"
                       "exposed 外部可达且不在预期清单（含配了域名反代却绑全网的源站）",
                       "未启用防火墙时按\"非回环端口全都可达\"判定，不给 unknown——关掉 ufw 就是没有过滤",
                       "只看本机视野：云上安全组、上游 NAT 不在判定范围内，这一条写在 notes 里",
                       "非 root 时 ss 不给进程名，as_root=false，判定只按端口号给",
                       "读不到监听表时退出码 3"]},
            {"name": "storage", "args": ["--refresh"],
             "desc": "存储占用对账（只读：不删文件、不改配额，清理只给可复制的命令）",
             "notes": ["盘点面板数据、面板备份、journald 落盘处与 FILE_ROOTS 登记的目录，"
                       "每个根给一级子目录占用榜",
                       "扫盘有预算：每个根最多 ROOT_BUDGET_SEC 秒、MAX_DEPTH 层，"
                       "没扫完的目录会标成下界，不会给你一个假的精确数字",
                       "判词四档：journal 超 2GiB、备份累计超 4GiB、分区已用超 90%、"
                       "单个文件超 2GiB 点名（后者只点不判断）",
                       "只看本机文件系统：远端挂载、对象存储不在范围内",
                       "登记的目录一个都读不到时退出码 3"]},
            {"name": "memory", "args": ["--refresh"],
             "desc": "内存压力与 OOM 对账（只读：不 kill 进程、不改 sysctl、不动 unit 上限）",
             "notes": ["已用按 MemTotal - MemAvailable 计，与概览同口径；free(1) 的 used 含页缓存，"
                       "会比这里高一截",
                       "换页 / 直接回收 / PSI 只在 " + str(memory_service.RATE_WINDOW_SEC) +
                       " 秒窗口内采一次，回答的是此刻有没有在抖，不是历史曲线",
                       "单元表按 memory.current 排序，.slice 是分组（数字已含子单元），"
                       "面板自己那一行无条件留在表里",
                       "OOM：读 vmstat 的 oom_kill 计数，非 0 才去 kernel 日志取受害者；"
                       "单元 memory.events 的 oom_kill 单独说明是上限设低了",
                       "cgroup v2 与 CONFIG_PSI 读不到的节如实标原因，不会折成 0",
                       "读不到 /proc/meminfo 时退出码 3"]},
            {"name": "pending", "args": ["--refresh"],
             "desc": "改动未生效对账（只读：不 restart、不 reload、不 daemon-reload、不 reboot）",
             "notes": ["判据是 /proc/<pid>/maps 里结尾带 (deleted) 的映射，再和同路径当前文件的 inode 对一遍；"
                       "设备、/SYSV*、/dev/shm、memfd、匿名映射先滤掉，否则 postgres 的每个后端都是假阳性",
                       "需要重启是自己推的（已装 linux-image 与 running 内核做 dpkg 版本比较）；Debian 的 "
                       "/run/reboot-required 来自 update-notifier-common，服务器镜像默认不装它",
                       "nginx 比的是最晚启动的那个 worker 的时刻而不是 master：nginx -s reload 只换 worker",
                       "包名归属取自 dpkg，读不到就只说文件路径；判断「什么时候被装上」用 ctime 不用 mtime，"
                       "因为 dpkg 会保留包内文件的原始 mtime",
                       "扫 /proc 有预算：最多 " + str(pending_service.PROC_SCAN_CAP) + " 个进程、"
                       + str(pending_service.SCAN_BUDGET_SEC) + " 秒，没扫完会如实标出来",
                       "内核与进程映射都读不到时退出码 3"]},
            {"name": "start|stop|restart", "args": [], "desc": "systemd 服务控制"},
            {"name": "logs", "args": ["--lines"], "desc": "查看面板日志"},
            {"name": "schema", "args": [], "desc": "输出本契约"},
        ],
        "deployRecipe": [
            "choyeonctl deploy --port 3210",
            "printf '%s' \"<随机口令>\" | choyeonctl user create admin --password-stdin",
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
    d.add_argument("--prefix", help="安装目录；默认就是当前 CLI 所在的仓库目录，"
                                    "仓库结构不完整（无 .git）时才退回 /root/choyeon-panel")
    d.add_argument("--port", type=int, help="监听端口，默认 3210")
    d.add_argument("--branch", help="克隆指定分支（安装目录已是 git 仓库时不生效，改分支请走 upgrade）")
    d.add_argument("--skip-nginx", action="store_true", help="跳过 nginx 反代配置（CLI 调用本就非交互）")
    d.add_argument("--no-deps", action="store_true", help="跳过系统依赖安装")
    d.set_defaults(fn=cmd_deploy)

    up = sub.add_parser("upgrade", help="升级并自动回滚失败", parents=[common])
    up.add_argument("--no-pull", action="store_true",
                    help="不拉代码，仅重装依赖、重建前端、重新渲染 unit 并重启（改配置后用）")
    up.set_defaults(fn=cmd_upgrade)
    sub.add_parser("status", help="运行状态", parents=[common]).set_defaults(fn=cmd_status)
    sub.add_parser("doctor", help="安全与健康自检", parents=[common]).set_defaults(fn=cmd_doctor)

    u = sub.add_parser("user", help="账号管理", parents=[common])
    u.add_argument("user_cmd", choices=["list", "create", "reset", "revoke", "delete"])
    u.add_argument("name", nargs="?", help="revoke 传 all 表示吊销全部用户")
    u.add_argument("--password", help="口令走 argv，同机用户可见；脚本请改用 --password-stdin")
    u.add_argument("--password-stdin", action="store_true", help="从标准输入读取一行口令")
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

    fw = sub.add_parser("firewall", help="本机防火墙读写（ufw / firewalld）", parents=[common])
    fw.add_argument("firewall_cmd", choices=["list", "allow", "deny", "remove", "on", "off"])
    fw.add_argument("--port", type=int, help="端口，1-65535")
    fw.add_argument("--port-to", type=int, dest="port_to", help="区间结束端口（含）")
    fw.add_argument("--proto", choices=["tcp", "udp", "both"], default="tcp")
    fw.add_argument("--source", help="来源 IP 或 CIDR，留空表示任意地址")
    fw.add_argument("--comment", help="备注，最多 40 字")
    fw.add_argument("--rate", action="store_true", help="放行且限速（ufw 的 limit）")
    fw.add_argument("--num", type=int, help="remove：list 里的规则编号")
    fw.add_argument("--expect", help="remove：list 里那条规则的 sig，服务端重新列表比对")
    fw.add_argument("--yes", action="store_true", help="off：确认停用防火墙")
    fw.set_defaults(fn=cmd_firewall)

    up = sub.add_parser("updates", help="待装系统补丁与安全更新（只读探测）", parents=[common])
    up.add_argument("--refresh", action="store_true", help="忽略 30 分钟缓存，立即重新探测")
    up.set_defaults(fn=cmd_updates)

    pt = sub.add_parser("ports", help="监听端口与对外可达性（只读对账）", parents=[common])
    pt.add_argument("--refresh", action="store_true", help="忽略 5 分钟缓存，立即重新对账")
    pt.set_defaults(fn=cmd_ports)

    sa = sub.add_parser("ssh-audit", help="登录安全只读对账（不改任何 SSH 配置）", parents=[common])
    sa.add_argument("--refresh", action="store_true", help="忽略 5 分钟缓存，重新读一遍配置与日志")
    sa.set_defaults(fn=cmd_ssh_audit)

    st = sub.add_parser("storage", help="存储占用对账（只读扫描）", parents=[common])
    st.add_argument("--refresh", action="store_true", help="忽略 5 分钟缓存，立即重新扫一遍")
    st.set_defaults(fn=cmd_storage)

    mm = sub.add_parser("memory", help="内存压力与 OOM 对账（只读）", parents=[common])
    mm.add_argument("--refresh", action="store_true", help="忽略 90 秒缓存，重新采一个速率窗口")
    mm.set_defaults(fn=cmd_memory)

    pd = sub.add_parser("pending", help="改动未生效对账（只读：不重启、不重载）", parents=[common])
    pd.add_argument("--refresh", action="store_true", help="忽略 2 分钟缓存，立即重新扫一遍 /proc")
    pd.set_defaults(fn=cmd_pending)

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
