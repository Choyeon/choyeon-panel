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
    choyeonctl backup run|list|jobs|targets|add-target|push|link
    choyeonctl firewall list|allow|deny|remove|on|off
    choyeonctl ban list|add|rm        # 封禁 sshd 爆破来源
    choyeonctl updates
    choyeonctl storage
    choyeonctl logs [-n 100] | restart | stop | start | upgrade
    choyeonctl webstats [--file N] [--minutes M] [--list]
    choyeonctl site list|claim|unclaim|claim-all   # 站点纳管（只写登记表，不碰 nginx）
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

from app import avatars, config, security, severity  # noqa: E402
from app import database as dbm  # noqa: E402
from app.errors import BadInput, public_message  # noqa: E402
from app.routers.meta import run_checks  # noqa: E402
from app.services import (  # noqa: E402
    access_stats,
    backup_service,
    ban_service,
    cloud_backup,
    exposure_service,
    firewall_service,
    load_service,
    login_audit_service,
    memory_service,
    pending_service,
    site_audit_service,
    site_claim_service,
    storage_service,
    updates_service,
    web_logs,
)
from app.util import CLI_LOG_LINES_MAX, human_bytes, redact_creds, timeout_note  # noqa: E402

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
        return {"code": 124, "out": "", "err": timeout_note(str(cmd[0]), timeout)}
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
        Out.error(f"{timeout_note(Path(script).name, DEPLOY_TIMEOUT_SEC)}；"
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


def _secret_from_stdin() -> str:
    """异地目标的 Secret Key 只从标准输入读，这一条没有 argv 逃生口。

    `--password` 保留命令行写法只是为了兼容旧脚本，并且会打警告。新加的凭据入口不重复
    那个错误：argv 进 `ps -ef`、进世界可读的 `/proc/<pid>/cmdline`、还进 root 的
    shell 历史，而这台机器上跑着别人的业务应用——对象存储的长期密钥对它们同样可见。
    """
    s = sys.stdin.readline().rstrip("\r\n")
    if not s:
        Out.error("--secret-stdin 未从标准输入读到 Secret Key", EXIT_USAGE)
    return s


async def cmd_user(a: argparse.Namespace) -> dict:
    if a.user_cmd == "list":
        # totp 是"开没开"这个布尔，密钥本身不参与输出也不进 JSON：这个终端会被拍照、
        # 贴进工单、录进 CI 日志，而拿到 base32 就等于拿到别人的第二因子。
        rows = dbm.query("SELECT id,username,role,created_at,totp_secret<>'' AS totp FROM users ORDER BY id")
        Out.result(
            rows,
            "\n".join(f"{r['id']:>3}  {r['username']:<20} {r['role']:<7} {'2fa' if r['totp'] else '-'}"
                      for r in rows) or "(无用户)",
        )
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

    if a.user_cmd == "2fa-disable":
        # 逃生口：验证器丢了（换手机、卸载、账号没备份）时，网页端"关闭两步验证"要求
        # 再输一枚当前有效的动态口令——那人恰好输不出。这条路径只给 root：它是拿
        # root 权限换掉一个账号的第二因子，而不是拿口令换。
        if not a.name:
            Out.error("2fa-disable 需要 <name>", EXIT_USAGE)
        if a.name == "all":
            Out.error("2fa-disable 一次只关一个账号，请给出用户名", EXIT_USAGE)
        u = dbm.query_one("SELECT * FROM users WHERE username=?", (a.name,))
        if not u:
            Out.error(f"用户 {a.name} 不存在", EXIT_FAIL)
        if not str(u.get("totp_secret") or ""):
            Out.error(f"{a.name} 本来就没开两步验证，没有可关闭的东西", EXIT_FAIL)
        if not a.yes:
            Out.error(
                f"关闭两步验证会让 {a.name} 退回只有口令保护，确认请加 --yes："
                f"choyeonctl user 2fa-disable {a.name} --yes",
                EXIT_USAGE,
            )
        dbm.set_totp(a.name, "")
        # 连带把这个账号的会话全部吊销：会来跑这条命令的原因通常是"手机不在手边"，
        # 而这种时候并不确定手里有票的是不是本人。留一句提醒，比留一个后门便宜。
        security.bump_user_epoch(a.name)
        dbm.audit(a.name, "cli:user:2fa-disable", "该账号两步验证已关闭，会话全部吊销")
        Out.result({"ok": True, "username": a.name, "enabled": False, "revokedSessions": True},
                   f"已关闭 {a.name} 的两步验证并吊销其全部会话（请重新登录，并在设置页重新绑定验证器）")
        return {"ok": True, "username": a.name, "enabled": False}

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
    上限取 `CLI_LOG_LINES_MAX`，定时任务白名单校验共用同一个常量：
    终端里能跑通的 `--lines` 不应该登记成定时任务时被拒。
    """
    return max(1, min(int(raw or 1), CLI_LOG_LINES_MAX))


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
        dep = await apps_service.deploy_app(a.id, actor="cli")
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
        res = await apps_service.rollback_app(a.id, actor="cli")
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
        # 后缀的唯一出处在 backup_service：自检项与这里必须数同一批文件，
        # 否则 `backup list` 只列 tar 包，定时跑出来的 PG 备份在 CLI 里像不存在一样。
        suffixes = backup_service.ARTIFACT_SUFFIXES
        files = [
            f
            for f in Path(config.BACKUP_DIR).glob("**/*")
            if f.is_file() and f.name.endswith(suffixes)
        ]
        files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        rows = [{"file": str(f), "size": f.stat().st_size, "mtime": int(f.stat().st_mtime)} for f in files[:50]]
        Out.result(rows, "\n".join(f"{r['size']:>10}  {r['file']}" for r in rows) or "(无备份)")
        return {"backups": rows}

    if a.backup_cmd == "jobs":
        # 任务列表与网页备份页共用同一个读者（list_backups）：终端上判"哪条任务昨晚没成"
        # 必须和页面看到同一份留痕，不能各查各的列。
        rows = backup_service.list_backups()
        jobs = []
        for r in rows:
            status = str(r.get("last_status") or "") or "未执行"
            jobs.append({
                "id": r["id"],
                "label": backup_service.run_label(r),
                "kind": r["kind"],
                "target": r["target"],
                "schedule": r["schedule"],
                "enabled": bool(r["enabled"]),
                "target_missing": bool(r.get("target_missing")),
                "last_run": r.get("last_run") or "",
                "last_status": status,
                "last_note": r.get("last_note") or "",
                "cloud_target_name": r.get("cloud_target_name"),
                "artifacts": len(r.get("files") or []),
            })
        failed = [j for j in jobs if j["last_status"] not in ("ok", "未执行")]
        lines = [
            f"{j['id']:>3}  {j['label']:<26}  {j['schedule']:<7}  "
            f"{'启用' if j['enabled'] else '停用'}  {(j['last_run'] or '-'):<19}  {j['last_status']}"
            + (f"  ← {j['last_note'][:60]}" if j["last_note"] else "")
            for j in jobs
        ]
        Out.result({"jobs": jobs, "failed": failed},
                   "\n".join(lines) or "(无备份任务)")
        return {"jobs": jobs, "failed": failed}

    if a.backup_cmd == "targets":
        # 列表面向排障：一眼要能看出"这个桶还能不能用"，所以 last_error 直接进人读输出
        # 而不是只躺在 --json 里（脚本会读 JSON，人读终端）。
        rows = cloud_backup.list_targets()
        Out.result(
            rows,
            "\n".join(
                f"{r['id']:>3}  {r['name']:<16} {'启用' if r['enabled'] else '停用'}  "
                f"{r['bucket']} @ {r['endpoint']}  前缀={r['prefix'] or '/'}  "
                f"已推 {r['pushed_count']} 份  凭据={'有' if r['has_secret'] else '缺'}"
                + (f"  上次失败：{r['last_error']}" if r["last_error"] else "")
                for r in rows
            ) or "(还没配置异地目标)",
        )
        return {"targets": rows}

    if a.backup_cmd in ("add-target", "edit-target"):
        if a.backup_cmd == "add-target":
            miss = [k for k, v in (("name", a.name), ("endpoint", a.endpoint),
                                   ("bucket", a.bucket), ("access-key", a.access_key)) if not v]
            if miss:
                Out.error(f"add-target 缺少参数：{'、'.join('--' + m for m in miss)}", EXIT_USAGE)
            if not a.secret_stdin:
                Out.error("add-target 必须用 --secret-stdin 传 Secret Key（命令行参数会留在 ps 与 shell 历史里）",
                          EXIT_USAGE)
        if a.enable and a.disable:
            Out.error("--enable 与 --disable 不能同时使用", EXIT_USAGE)
        # 只把"这次真的给了"的字段交给服务层：edit-target 是按字段 PATCH 的，
        # 把 argparse 的缺省值一起传过去等于"没写的字段一律清空"。
        body = {
            k: v
            for k, v in {
                "name": a.name, "endpoint": a.endpoint, "bucket": a.bucket, "region": a.region,
                "access_key": a.access_key, "prefix": a.prefix, "path_style": a.path_style,
            }.items()
            if v is not None
        }
        if a.enable:
            body["enabled"] = True
        elif a.disable:
            body["enabled"] = False
        if a.secret_stdin:
            body["secret_key"] = _secret_from_stdin()
        try:
            if a.backup_cmd == "add-target":
                t = cloud_backup.create_target(body)
                dbm.audit("cli", "cli:backup:target:create", str(t["name"]))
            else:
                if not a.id:
                    Out.error("edit-target 需要 --id", EXIT_USAGE)
                t = cloud_backup.update_target(int(a.id), body)
                dbm.audit("cli", "cli:backup:target:update", f"{a.id}")
        except Exception as e:  # noqa: BLE001
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
            return {}
        Out.result(t, f"异地目标已{'创建' if a.backup_cmd == 'add-target' else '更新'}：{t['name']}")
        return {"ok": True, "target": t}

    if a.backup_cmd == "del-target":
        if not a.id:
            Out.error("del-target 需要 --id", EXIT_USAGE)
        if not a.yes:
            Out.error("删除异地目标会让所有挂在它上面的备份任务停止推送，确认请加 --yes", EXIT_USAGE)
        try:
            r = cloud_backup.delete_target(int(a.id))
        except Exception as e:  # noqa: BLE001
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
            return {}
        dbm.audit("cli", "cli:backup:target:delete", f"{a.id}")
        # 这句必须说：远端对象一个都没删（面板不会替人删数据），删掉的只是这台机器上的配置。
        Out.result(r, "已删除目标配置，桶里的对象与本机备份都还在（需要清理请到对象存储侧自行删除）")
        return {"ok": True, **r}

    if a.backup_cmd == "test-target":
        if not a.id:
            Out.error("test-target 需要 --id", EXIT_USAGE)
        try:
            res = cloud_backup.probe(int(a.id))
        except Exception as e:  # noqa: BLE001
            Out.error(f"连通性测试失败：{public_message(e) or type(e).__name__}", EXIT_FAIL)
            return {}
        dbm.audit("cli", "cli:backup:target:test", str(res["target"]))
        Out.result(res, f"{res['target']}：桶可读，前缀下 {res['objects_under_prefix']} 个对象"
                       f"（{human_bytes(res['bytes_under_prefix'])}）")
        return {"ok": True, **res}

    if a.backup_cmd == "push":
        if not a.id:
            Out.error("push 需要 --id <备份任务 id>", EXIT_USAGE)
        try:
            res = cloud_backup.push_now(int(a.id), int(a.target) if a.target else 0)
        except Exception as e:  # noqa: BLE001
            dbm.audit("cli", "cli:backup:push", f"{a.id}(失败: {redact_creds(str(e))[:120]})")
            Out.error(f"推送失败：{public_message(e) or type(e).__name__}", EXIT_FAIL)
            return {}
        dbm.audit("cli", "cli:backup:push", f"{a.id}({len(res.get('pushed') or [])} 份)")
        if res.get("skipped"):
            Out.result(res, f"未推送：{res['skipped']}")
        else:
            Out.result(res, f"已推送 {len(res['pushed'])} 份到异地"
                            + (f"，按 keep 删掉远端最旧 {len(res['pruned'])} 份" if res.get("pruned") else ""))
        return {"ok": True, **res}

    if a.backup_cmd == "link":
        # 关联＝"这个任务跑完之后往哪儿推"，写的是 backups.cloud_target_id 一列。
        # 走服务层的同一个校验（目标必须存在），不在 CLI 里自己 INSERT：绕过校验的那一条路
        # 就是"链接指向一个已删除的目标"这种只在备份失败那天才被发现的故障。
        if not a.id:
            Out.error("link 需要 --id <备份任务 id>", EXIT_USAGE)
        if a.target is None:
            Out.error("link 需要 --target <异地目标 id>，取消关联用 --target 0", EXIT_USAGE)
        try:
            # 必须 await：update_backup 是 async def，漏掉 await 只会造一个从不执行的
            # 协程对象——except 抓不到任何东西，CLI 照样打印「已关联到目标 N」并写审计，
            # 而 backups.cloud_target_id 一个字都没改。实测 choyeonctl backup link 是
            # 静默空操作，连 --target 999999（目标不存在）都报成功。
            await backup_service.update_backup(int(a.id), {"cloud_target_id": int(a.target)})
        except Exception as e:  # noqa: BLE001
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
            return {}
        dbm.audit("cli", "cli:backup:link", f"{a.id}->{a.target}")
        Out.result({"ok": True, "backup_id": int(a.id), "cloud_target_id": int(a.target)},
                   f"备份任务 {a.id} 已{'关联到目标 ' + str(a.target) if int(a.target) else '取消关联异地'}")
        return {"ok": True}

    if a.backup_cmd in ("cloud-list", "cloud-pull"):
        # 这两条走的是同一组参数校验：都必须是「某条任务 + 某个异地目标」，
        # 拉回还多一个产物名。分开写两份 if a.id/if a.target 早晚有一份漏掉 --target，
        # 而漏掉的那一条会去 list 别的桶——「看得见」错桶比看不见更糟。
        if not a.id:
            Out.error(f"{a.backup_cmd} 需要 --id <备份任务 id>", EXIT_USAGE)
        if not a.target:
            Out.error(f"{a.backup_cmd} 需要 --target <异地目标 id>", EXIT_USAGE)
        try:
            if a.backup_cmd == "cloud-list":
                res = cloud_backup.list_for_job(int(a.target), int(a.id))
            else:
                if not a.name:
                    Out.error("cloud-pull 需要 --name <产物名>（先用 cloud-list 看有哪些份）", EXIT_USAGE)
                res = cloud_backup.pull_one(int(a.target), int(a.id), a.name)
        except Exception as e:  # noqa: BLE001
            note = redact_creds(str(e))[:120]
            dbm.audit("cli", f"cli:backup:{a.backup_cmd}", f"{a.id}(失败: {note})")
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
            return {}

        if a.backup_cmd == "cloud-list":
            dbm.audit("cli", "cli:backup:cloud-list", f"{a.id}@{a.target}({len(res['files'])} 份)")
            lines = [
                f"{f['size']:>12}  {f['modified']:<25}  {'本机有' if f['local'] else '仅异地'}  {f['name']}"
                for f in res["files"]
            ]
            tail = "" if not res["truncated"] else f"（列举已达上限，桶里这个前缀共 {res['remote_total']} 个对象）"
            Out.result(res, "\n".join(lines) or f"目标「{res['target']}」的 {res['bucket']}/{res['prefix']} "
                                                f"下没有任务 {a.id} 名下的产物{tail}")
            return {"ok": True, **res}

        dbm.audit("cli", "cli:backup:cloud-pull", f"{a.id}({res['name']})")
        Out.result(res, f"已从「{res['target']}」拉回 {res['name']}（{human_bytes(res['bytes'])}）"
                        f"到 {config.BACKUP_DIR}，"
                        + ("现在可以用 backup restore 恢复" if res["restoreable"]
                           else "这一类产物不支持自动恢复，请手工处理"))
        return {"ok": True, **res}

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
        Out.error(f"{timeout_note(Path(script).name, DEPLOY_TIMEOUT_SEC)}；"
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
    remove 需要它，而让人再去跑一次 --json 只会多一次可被删错的时间窗。

    三态照实打印：`active` 为 None 时说"状态没读到"并附原因，最后一行也不写「共 0 条」——
    终端上那两个字看起来跟"这台机器没有任何放行规则"一模一样，而它真正的意思是没读到。
    """
    rules = data.get("rules") or []
    reasons = data.get("protected_reasons") or {}
    active = data.get("active")
    bad = firewall_service.unread(data)
    state = "已启用" if active is True else ("未启用" if active is False else "状态没读到")
    head = f"{data.get('tool', 'none')} · " + state
    if bad:
        head += f"（{bad}）"
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
    bad_rules = firewall_service.rules_unread(data)
    if bad_rules:
        lines.append(f"规则表没读到，这一条不等于「没有放行规则」（{bad_rules}）")
    else:
        lines.append(f"共 {len(rules)} 条")
    return "\n".join(lines)


async def cmd_ban(a: argparse.Namespace) -> dict:
    """封禁 sshd 爆破来源：写的还是本机防火墙那一张表，读的也是同一份结果。

    这里不重新发明防火墙。ufw 的规则仍由 firewall_service 写删，编号+签名的删除比对、
    自锁守卫一次都不绕。ban_service 只多回答三件事：封谁、这条是谁封的、封完到底生效没有
    ——最后一条每次写完都重新列表确认，命令返回 0 而地址不在表里按失败处理。
    """
    verb = a.ban_cmd
    tool = await firewall_service.detect()
    if verb != "list" and tool not in (firewall_service.UFW, firewall_service.FIREWALLD):
        Out.error("这台机器上既没有 ufw 也没有 firewalld，没有可封禁的地方；"
                  "持续对抗爆破的长期方案是装 fail2ban 的 sshd jail", EXIT_PRECONDITION)

    try:
        if verb == "list":
            data = await ban_service.list_bans()
        elif verb == "add":
            data = await ban_service.ban(a.ip or "", None, int(a.hits or 0))
        else:
            if tool == firewall_service.UFW and (not a.num or not a.expect):
                Out.error("rm 需要 --num 与 --expect：ufw 按编号删除，而编号会随任何一次删除前移。"
                          "先用 choyeonctl ban list --json 读回这条封禁的 num 与 sig", EXIT_USAGE)
            data = await ban_service.unban(a.ip or "", int(a.num or 0), a.expect or "", None)
    except BadInput as e:
        Out.error(public_message(e), EXIT_USAGE)
    except Exception as e:  # noqa: BLE001
        Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)

    table = _ban_table(data)
    Out.result(data, table if verb == "list" else f"{data.get('detail') or '已执行'}\n{table}")
    return data


def _ban_table(data: dict) -> str:
    """`ban list` 的人类可读形态，与网页「登录安全」页签读的是同一个出处。

    ok=false 时不印「共 0 条」：终端上这两个字读起来是「这台机器没封任何人」，
    而它真正的意思是没读到——跟 firewall list 守同一条规矩。
    """
    tool = data.get("tool", "none")
    bans = data.get("bans") or []
    if not data.get("ok"):
        return f"{tool} · 封禁列表没读到，这一条不等于「没有封禁」（{data.get('reason') or '未知原因'}）"
    cap = data.get("maxBans") or ban_service.MAX_BANS
    marker = data.get("marker") or ban_service.MARKER
    lines = [f"{tool} · 封禁 {len(bans)} 条（上限 {cap}，标记 {marker}）"]
    for b in bans:
        num = f"[{b['num']}]  " if b.get("num") else ""
        gap = ""
        if not b.get("runtime"):
            gap = "（只在永久配置里，当前还没生效）"
        elif not b.get("permanent"):
            gap = "（重启后失效，永久配置里还没有）"
        sig = f"  sig={b['sig']}" if b.get("sig") else ""
        lines.append(f"  {num}{b.get('ip', '')}{gap}{sig}  {b.get('comment') or ''}".rstrip())
    if any(b.get("sig") for b in bans):
        lines.append("解封：choyeonctl ban rm <ip> --num <编号> --expect <sig>")
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
    err = d.get("probe_error")
    total = int(d.get("pending_total") or 0)
    if err:
        # 读不到就说读不到。旧写法把 pending_total=0 排进 head，等于对着失败的探测
        # 打印「待装 0 个」——自检与终端一起绿，机器却可能带着洞。
        head = f"待装更新读不到（{err}），这一条不等于「没有安全更新」"
    else:
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
    elif d.get("family") == "debian" and d.get("reboot_required") is None:
        lines.append(f"要不要重启判不出：{d.get('reboot_reason') or '读不到已装内核列表'}")
    for n in d.get("notes") or []:
        lines.append(f"[note] {n}")
    age = d.get("index_age_hours")
    if age is not None and not err and age > updates_service.STALE_TRUST_HOURS:
        # 索引旧到什么程度就不能当结论用了：判据在服务层，终端和自检项说同一句话。
        lines.append(
            f"清单读的是 {age:.0f} 小时前的本地索引，超过 "
            f"{updates_service.STALE_TRUST_HOURS:.0f} 小时，「没有安全更新」这句话不可信"
        )
    pkgs = d.get("packages") or []
    shown = pkgs[:20]
    for p in shown:
        mark = "  [security]" if p.get("security") else ""
        lines.append(f"  {p['name']:<38} {p.get('current') or '-':<26} -> {p.get('candidate') or '?'}{mark}")
    if len(pkgs) > len(shown):
        lines.append(f"  ...共 {len(pkgs)} 个，完整清单用 --json")
    cmds = d.get("commands") or {}
    if cmds.get("refresh_index") and (err or (age is not None and age > updates_service.STALE_TRUST_HOURS)):
        lines.append(f"刷新索引：  {cmds['refresh_index']}")
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
        sev = severity.SEV_LABEL.get(f["severity"], f["severity"])
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
        mark = f"  ← {severity.SEV_LABEL.get(flag, flag)}" if flag else ""
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

    p2 = d.get("panel") or {}
    if p2:
        # 这一行说的是面板自己那扇门：它发的是 root shell，第二因子的覆盖面属于登录安全，
        # 不属于"账号管理"——所以它出现在这里，而不是 choyeonctl user list 的注释里。
        lines.append(
            f"面板账号：{p2.get('accounts', 0)} 个 · 开了两步验证 {p2.get('with_totp', 0)} 个 · "
            f"过去 24 小时面板登录失败 {p2.get('recent_failed', 0)} 次"
            if p2.get("ok") else f"面板账号：{p2.get('note') or '读不到'}"
        )

    guard = d.get("guard") or {}
    if guard.get("units"):
        # 判词由服务端算（phrase），逐单元的说法也由服务端算（desc）：这里不再自己比较状态串，
        # 否则同一个事实在第 ③ 处又长出一套说法，而它们迟早对不上。
        parts = [f"{g['name']} {g['desc']}" for g in guard["units"]]
        lines.append(f"爆破防护：{guard.get('phrase') or '这一项没核对成'}（{'、'.join(parts)}）")
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
        f"监听端口 {c.get('total', 0)} 个：外部可达 {c.get('exposed', 0)}、判不了 {c.get('unknown', 0)}、"
        f"被防火墙挡住 {c.get('blocked', 0)}、预期内 {c.get('expected', 0)}、仅本机 {c.get('internal', 0)}"
    ]
    if fw.get("unread"):
        fw_line = f"防火墙 {fw.get('tool')} 状态没读到（{fw['unread']}）：依赖放行表的判定全部标未确认"
    else:
        fw_line = (
            f"防火墙 {fw.get('tool')}{'：已启用' if fw.get('active') is True else '：未启用/未安装'}，"
            f"默认入站 {fw.get('default_incoming') or '未知'}"
        )
    lines.append(fw_line)
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
        sev = severity.SEV_LABEL.get(f["severity"], f["severity"])
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
    hu = human_bytes
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
        sev = severity.SEV_LABEL.get(f["severity"], f["severity"])
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
        sev = severity.SEV_LABEL.get(f["severity"], f["severity"])
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


async def cmd_site_audit(a: argparse.Namespace) -> dict:
    """站点链路对账：一个域名从被访问到拿到响应，中间每一跳都要有人认领。

    严格只读——不 reload nginx、不改 vhost、不签证书、不重启应用。502 的成因大多在应用侧
    （进程没起来、端口填错），面板替你 reload 或改配置只会把现场抹掉。
    """
    data = await site_audit_service.audit(refresh=bool(a.refresh))
    if not (data.get("nginx") or {}).get("ok"):
        Out.error((data.get("nginx") or {}).get("reason")
                  or "读不到 nginx 配置，站点链路无法对账", EXIT_PRECONDITION)
    Out.result(data, _site_audit_text(data))
    return data


def _site_audit_text(d: dict) -> str:
    ng = d.get("nginx") or {}
    li = d.get("listen") or {}
    ce = d.get("certs") or {}
    c = d.get("counts") or {}
    lines = ["站点链路对账（只读：不 reload nginx、不改 vhost、不签证书、不重启应用）"]
    lines.append(f"配置：{ng.get('files')} 份、{ng.get('domains')} 个域名"
                 + ("" if ng.get("ok") else f"（{ng.get('reason') or '读不到'}）"))
    lines.append("监听表：" + (f"{li.get('ports')} 个端口在听" if li.get("ok")
                          else f"读不到（{li.get('reason')}），这一轮的反代目标都标未确认"))
    lines.append("证书：" + ("清单已读到，只看覆盖关系（到期天数见证书项）" if ce.get("ok")
                         else f"读不到（{ce.get('reason')}），覆盖关系未确认"))
    for f in d.get("findings") or []:
        lines.append(f"[{f['severity']}] {f['title']}")
        lines.append(f"    {f['detail']}")
        lines.append(f"    修复：{f['fix']}")
    if not d.get("findings"):
        lines.append(f"没有判词：{c.get('risk', 0)}/{c.get('warn', 0)}/{c.get('info', 0)} 档都是空的——"
                     "反代目标都在监听、证书都覆盖、明文都跳转")
    for link in (d.get("links") or [])[:20]:
        doms = "、".join(link.get("domains") or []) or "（无 server_name）"
        tgt = "、".join(
            f"{t['host']}:{t['port']}" + ("(没人听)" if t["listening"] is False
                                          else "(未确认)" if t["listening"] is None else "")
            for t in link.get("targets") or [] if t.get("port") is not None) or "非反代"
        lines.append(f"  {link['file']} · {doms} · 听 {link.get('listen')} · {tgt}"
                     + (f" · 应用 {link['app']}" if link.get("app") else ""))
    for n in d.get("notes") or []:
        lines.append(f"注：{n}")
    return "\n".join(lines)


async def cmd_site(a: argparse.Namespace) -> dict:
    """站点纳管：域名归属登记在面板自己的表里，nginx 的一个字节都不动。

    纳管＝写一行「这个域名归哪个应用」并认领它所在的那份配置文件；取消纳管只删这一行。
    不写 nginx、不 reload：手工 vhost 是多 server 块 + alias 跳转 + certbot 注入的证书行，
    不是面板模板 render 出来的形状，套一遍等于把别人的配置改回面板模板。
    """
    verb = a.site_cmd
    if verb == "list":
        rows, reason = await site_claim_service.sites_rows()
        if reason:
            Out.error(reason, EXIT_PRECONDITION)
        data = {"sites": rows, "counts": _site_counts(rows)}
        Out.result(data, _site_list_text(rows))
        return data
    if verb == "claim":
        if not (a.domain or ""):
            Out.error("claim 需要域名", EXIT_USAGE)
        if a.app and not a.app.isdigit():
            Out.error("--app 是应用 id（choyeonctl app list 的第一列），不是应用名", EXIT_USAGE)
        try:
            data = await site_claim_service.claim(a.domain, int(a.app) if a.app else None)
        except Exception as e:  # noqa: BLE001
            dbm.audit("cli", "cli:site:claim", f"{a.domain}(失败: {redact_creds(str(e))[:120]})")
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
        site_audit_service.reset_cache()
        dbm.audit("cli", "cli:site:claim", f"{data['domain']} → {data['app'] or '面板入口'}")
        Out.result(data, f"已纳管 {data['domain']}：{data['note']}（认领 {data['file'] or '无配置文件'}）")
        return data
    if verb == "unclaim":
        if not (a.domain or ""):
            Out.error("unclaim 需要域名", EXIT_USAGE)
        try:
            data = await site_claim_service.unclaim(a.domain)
        except Exception as e:  # noqa: BLE001
            Out.error(public_message(e) or type(e).__name__, EXIT_FAIL)
        site_audit_service.reset_cache()
        dbm.audit("cli", "cli:site:unclaim", data["domain"])
        # removed=false 时不说"已取消纳管"：那行登记本来就不在，说反了会让人以为
        # 之前有个归属被解掉了，而它从来没被纳管过。
        Out.result(data, f"{data['domain']}：" +
                  ("登记已删除，nginx 配置未做任何改动" if data["removed"] else "本来就没有登记"))
        return data
    data = await site_claim_service.claim_all()
    site_audit_service.reset_cache()
    dbm.audit("cli", "cli:site:claim-all",
              f"纳管 {len(data['claimed'])} 个，判不出归属 {len(data['skipped'])} 个")
    Out.result(data, _site_claim_all_text(data))
    return data


def _site_counts(rows: list[dict]) -> dict:
    return {"total": len(rows),
            "claimed": sum(1 for r in rows if r["claimed"]),
            "unclaimed": sum(1 for r in rows if not r["claimed"]),
            "stale": sum(1 for r in rows if r["claim_stale"])}


def _site_list_text(rows: list[dict]) -> str:
    c = _site_counts(rows)
    lines = [f"站点纳管：{c['total']} 个域名，{c['claimed']} 个已登记、{c['unclaimed']} 个未登记"
             + (f"，其中 {c['stale']} 条登记认领的配置文件已不在 nginx 加载列表里" if c["stale"] else "")]
    for r in rows:
        who = r["app"] or "—（未纳管）"
        mark = ("已纳管" if r["claimed"] else
                ("可判定" if r["suggest_kind"] == "panel" or r["suggest_app"] else "判不出"))
        gap = "（认领的文件已不在加载列表）" if r["claim_stale"] else ""
        ports = f"{r['port'] if r['port'] is not None else '—'}"
        if r["port_mismatch"]:
            ports += f"≠登记 {r['app_port']}"
        lines.append(f"  {r['domain']} · {who}（{mark}）· {r['file'] or '未认领文件'} · {ports}{gap}")
        if not r["claimed"]:
            why = r["suggest_note"] or r["suggest_reason"]
            if why:
                lines.append(f"      {why}")
    if c["unclaimed"]:
        lines.append("纳管：choyeonctl site claim <域名>（判不出的加 --app <应用 id> 手工指定）")
        lines.append("批量：choyeonctl site claim-all --json（判得出的落登记，判不出的原样列出理由）")
    return "\n".join(lines)


def _site_claim_all_text(d: dict) -> str:
    lines = [f"批量纳管：登记 {len(d['claimed'])} 个，判不出归属 {len(d['skipped'])} 个"]
    for r in d["claimed"]:
        lines.append(f"  {r['domain']} → {r['app'] or '面板入口'}（{r['note']}）")
    for r in d["skipped"]:
        lines.append(f"  {r['domain']} 未纳管：{r['reason']}")
    if d["skipped"]:
        lines.append("这些必须手工指定应用（site claim <域名> --app <应用 id>），面板不替你猜。")
    return "\n".join(lines)


async def cmd_load(a: argparse.Namespace) -> dict:
    """负载与 CPU 归因：机器有多忙、这份忙是谁造成的、有没有被自己的 CPU 配额压住。

    严格只读——不 renice、不 kill、不 restart 服务、不改 CPUQuota= / cpu.weight。load 高有两类相反
    的原因（CPU 不够 vs 磁盘跟不上），处置方向也相反；放宽配额是容量决策，杀进程与重启会直接断业务，
    所以这里只报归因和能当场复制走的只读命令。
    """
    data = await load_service.audit(refresh=bool(a.refresh))
    if not (data.get("load") or {}).get("ok") and not (data.get("cpu") or {}).get("ok"):
        Out.error((data.get("load") or {}).get("reason")
                  or (data.get("cpu") or {}).get("reason")
                  or "读不到 load average 与 /proc/stat，无法归因", EXIT_PRECONDITION)
    Out.result(data, _load_text(data))
    return data


def _load_text(d: dict) -> str:
    c = d.get("counts") or {}
    ld = d.get("load") or {}
    cp = d.get("cpu") or {}
    psi = d.get("psi") or {}
    pr = d.get("procs") or {}
    q = d.get("quota") or {}
    pct = cp.get("pct") or {}
    cores = d.get("cores") or 0
    upt = d.get("uptime_sec") or 0
    lines = ["负载与 CPU 归因（只读：不 renice、不 kill、不 restart、不改 CPUQuota=）"]
    lines.append(f"采样：窗口 {d.get('window_sec')} 秒 · {cores} 核 · 时钟 {d.get('hz')} tick/秒"
                 + (f" · 开机 {upt // 86400} 天" if upt else ""))

    if ld.get("ok"):
        per = round((ld.get("one") or 0) / cores, 2) if cores else None
        per15 = round((ld.get("fifteen") or 0) / cores, 2) if cores else None
        lines.append(f"load：{ld.get('one')}/{ld.get('five')}/{ld.get('fifteen')}"
                     f"（每核排队 {per}，15 分钟档 {per15}）；"
                     f"此刻 {ld.get('runnable')} 个可运行 / 整机 {ld.get('total_tasks')} 个任务")
    else:
        lines.append(f"load：{ld.get('reason') or '读不到'}")

    if cp.get("ok"):
        order = ("user", "nice", "system", "idle", "iowait", "irq", "softirq", "steal")
        parts = " ".join(f"{k} {pct.get(k)}%" for k in order if pct.get(k) is not None)
        lines.append("CPU 时间分解：" + (parts or "窗口里没有增量"))
        lines.append(f"  上下文切换 {cp.get('ctxt_per_sec')} 次/秒 · 新建进程 {cp.get('forks_per_sec')} 个/秒"
                     f" · run queue {cp.get('procs_running')} · 卡在等 IO {cp.get('procs_blocked')}")
    else:
        lines.append(f"CPU 时间分解：{cp.get('reason') or '读不到'}")

    ctrl = psi.get("controllers") or {}
    if psi.get("ok"):
        seg = []
        for kind in ("cpu", "io"):
            some = (ctrl.get(kind) or {}).get("some") or {}
            if some:
                seg.append(f"{kind} some avg10 {some.get('avg10')}%（60 秒 {some.get('avg60')}%）")
        lines.append("PSI 压力：" + "；".join(seg))
    else:
        lines.append(f"PSI 压力：{psi.get('reason') or '读不到'}")

    if pr.get("ok"):
        st = " · ".join(f"{k} {v}" for k, v in sorted((pr.get("states") or {}).items()))
        cut = "，预算用完这一轮没扫完" if pr.get("budget_hit") else ""
        lines.append(f"进程状态：看了 {pr.get('scanned')} 个{cut}（{st}），D/R 状态 {pr.get('blocked_total')} 个")
        for r in (pr.get("busy") or [])[:8]:
            unit = r.get("unit") or "(不归单元)"
            busy = f" 窗口内 {r.get('cpu_ms'):>6}ms 状态 {r.get('state')}  "
            lines.append("  " + _cpad(str(r.get("comm"))[:20], 22) + busy + unit[:34])
        if not (pr.get("busy") or []):
            lines.append("  这一秒没有任何进程消耗 CPU（窗口增量全为 0）")
        hidden = (pr.get("busy_total") or 0) - len(pr.get("busy") or [])
        if hidden > 0:
            lines.append(f"  （另有 {hidden} 个进程的窗口增量没列出来）")
        for r in (pr.get("blocked") or []):
            if r.get("state") != "D":
                continue
            lines.append("  [D] " + _cpad(str(r.get("comm"))[:20], 22)
                         + f" pid {r.get('pid')}  卡在 {r.get('wchan') or '（读不到 wchan）'}")
    else:
        lines.append(f"进程：{pr.get('reason') or '读不到'}")

    if q.get("ok"):
        units = q.get("units") or []
        lines.append(f"配额与限流：{q.get('dirs')} 个 cgroup 有 cpu.max，{q.get('capped_total')} 个设了限额，"
                     f"这一轮 {q.get('throttled_total')} 个被限流（列出 {len(units)}/{q.get('unit_total')} 个）")
        for r in units:
            cap = f"{r.get('quota_cores')} 核" if r.get("capped") else "未设限"
            wp = r.get("window_pct")
            tail = f"（{wp}%）" if wp is not None else ""
            sec = round((r.get("total_throttled_usec") or 0) / 1e6, 2)
            star = "  ← 面板自己" if r.get("self") else ""
            lines.append("  " + _cpad(str(r.get("unit"))[:30], 32) + _cpad(cap, 9)
                         + f"窗口 {r.get('window_throttled')}/{r.get('window_periods')} 周期{tail} "
                         + f"累计 {r.get('total_throttled')} 次/{sec} 秒{star}")
    else:
        lines.append(f"配额与限流：{q.get('reason') or '读不到'}")

    lines.append(f"判词：要紧 {c.get('risk', 0)} · 值得处理 {c.get('warn', 0)} · 知道就好 {c.get('info', 0)}")
    for f in d.get("findings") or []:
        sev = severity.SEV_LABEL.get(f["severity"], f["severity"])
        lines.append(f"  [{sev}] {f['title']}")
        if f.get("detail"):
            lines.append(f"      {f['detail']}")
        if f.get("fix"):
            lines.append(f"      处置：{f['fix']}")
    if not d.get("findings"):
        lines.append("  这一轮没有归因结论；读不到的节见上面几行的原因。")

    for n in d.get("notes") or []:
        lines.append(f"说明：{n}")
    lines.append(f"检查时间 {d.get('as_of')}" + ("（命中缓存）" if d.get("cached") else ""))
    return "\n".join(lines)


async def cmd_webstats(a: argparse.Namespace) -> dict:
    """网站访问流量统计：状态码分布、TOP 路径与客户端、每分钟请求数。

    严格只读：来源发现复用面板日志中心那份白名单（CP_LOG_ROOTS ∪ CP_FILE_ROOTS），
    只往文件尾读有限字节，不写任何临时文件，也不把原始日志行打出来——
    访问日志的查询串里常带 token，统计用的是去掉查询串的路径。
    """
    files = web_logs.nginx_log_files()
    if a.list:
        data = {"files": [{"i": i, "label": f["label"], "kind": f["kind"], "site": f["site"],
                           "path": f["path"]} for i, f in enumerate(files)],
                "roots": web_logs.log_roots()}
        if not files:
            Out.error(web_logs.discovery_hint(), EXIT_PRECONDITION)
        Out.result(data, "\n".join(
            [f"日志来源 {len(files)} 个（扫描根：{', '.join(web_logs.log_roots())}）"]
            + [f"  [{it['i']}] {it['kind']:<6} {it['label']}  {it['path']}" for it in data["files"]]))
        return data
    try:
        idx = int(a.file or 0)
        minutes = int(a.minutes or 60)
    except ValueError:
        Out.error("--file 是 --list 里读回的序号、--minutes 是窗口分钟数，两者都必须是整数", EXIT_USAGE)
    if not 1 <= minutes <= access_stats.SERIES_MAX:
        Out.error(f"--minutes 只能是 1-{access_stats.SERIES_MAX}（最多统计 24 小时）", EXIT_USAGE)
    if a.days is not None and not a.daily:
        Out.error("--days 只配 --daily 用：--minutes 是分钟窗口，--daily 才是按天趋势，"
                  "两种口径不要混着写", EXIT_USAGE)
    try:
        days = int(a.days) if a.days is not None else access_stats.DAY_LADDER[1]
    except ValueError:
        Out.error("--days 是天数（整数）", EXIT_USAGE)
    if not 1 <= days <= access_stats.MAX_DAYS:
        Out.error(f"--days 只能是 1-{access_stats.MAX_DAYS}（最多回看 {access_stats.MAX_DAYS} 天，"
                  "再老的历史早就被 logrotate 删了）", EXIT_USAGE)
    if not files:
        Out.error(web_logs.discovery_hint(), EXIT_PRECONDITION)
    if not 0 <= idx < len(files):
        Out.error(f"--file 序号超出范围（可用 0-{len(files) - 1}，先用 choyeonctl webstats --list 看清单）",
                  EXIT_USAGE)
    picked = files[idx]
    if picked["kind"] != "access":
        access_idx = [str(i) for i, f in enumerate(files) if f["kind"] == "access"]
        Out.error(f"[{idx}] {picked['label']} 是错误日志，里面没有请求行可统计；"
                  f"访问日志的序号是 {'、'.join(access_idx) if access_idx else '无'}", EXIT_USAGE)
    if a.daily:
        entries = web_logs.log_series(picked["path"])
        data = access_stats.collect_days(entries, days)
        data["source"] = picked["label"]
        data["site"] = picked["site"]
        data["series"] = [{"name": e["name"], "gen": e["gen"], "gz": e["gz"], "bytes": e["bytes"]}
                          for e in entries]
        Out.result(data, _webdaily_text(data))
        return data
    data = access_stats.collect(picked["path"], minutes)
    data["source"] = picked["label"]
    data["site"] = picked["site"]
    Out.result(data, _webstats_text(data))
    return data


def _webdaily_text(d: dict) -> str:
    """按天趋势的终端形态：一列日期 + 一句"跟上个周期比怎么样" + 一句回看余量。"""
    rows = d.get("rows") or []
    out = [f"网站按天流量：{d.get('source')} · 最近 {d.get('days')} 天"
           f"（{rows[0]['date'] if rows else '—'} → {rows[-1]['date'] if rows else '—'}）"]
    if not rows:
        return "\n".join(out + [
            f"没读到可解析的请求行：翻了 {d.get('scannedFiles')} 份世代"
            f"（盘上 {human_bytes(int(d.get('diskBytes') or 0))}），坏行 {d.get('malformed')} 条。"
            "这个站点此刻确实没人访问，或者日志格式既不是 combined 也不是 common。"])
    out.append(f"{'日期':<12}{'请求':>8}{'独立客户端':>10}{'下行':>10}{'4xx':>6}{'5xx':>6}")
    for r in rows:
        out.append(f"{r['date']:<12}{r['pv']:>8}{r['uv']:>10}"
                   f"{human_bytes(int(r['bytes'])):>10}{r['e4xx']:>6}{r['e5xx']:>6}")
    cur, prev = d.get("current") or {}, d.get("previous") or {}
    if d.get("comparable"):
        delta = d.get("deltaPvPct") or 0
        arrow = "持平" if delta == 0 else ("上升" if delta > 0 else "下降")
        out.append(f"环比：这 {d.get('days')} 天 {cur.get('pv')} 次请求，上一周期 {prev.get('pv')} 次"
                   f"（{arrow} {abs(delta)}%）")
    else:
        why = {"no-data": "上一周期一天日志都没读到",
               "history": f"日志只到 {d.get('historyFrom') or '—'}，往前凑不满一个周期",
               "clipped": "有世代被读量上限截断或恰好读不到（撞上 logrotate），两边口径不一致"}
        out.append(f"不做环比：{why.get(prev.get('reason'), '上一周期依据不足')}"
                   f"（上一周期只留住 {prev.get('days')} 天）")
    if d.get("uvFloored") or any(r.get("uvFloored") for r in rows):
        out.append(f"  注：独立客户端最多只记 {access_stats.MAX_KEYS} 个，"
                   "uvFloored=true 的行请读成「至少这么多人」")
    if d.get("outside"):
        out.append(f"  窗口外（更早）另有 {d['outside']} 次请求未计入")
    if d.get("truncatedFiles"):
        out.append(f"  注：{d['truncatedFiles']} 份世代超过单份读量上限 "
                   f"{human_bytes(access_stats.DAILY_FILE_BYTES)}，只读了开头")
    if d.get("unreadableFiles"):
        out.append(f"  注：{d['unreadableFiles']} 份世代这次没读到，多半正被 logrotate 改名或清理")
    out.append(f"回看：读了 {d.get('scannedFiles')}/{d.get('totalFiles')} 份世代、"
               f"{d.get('scannedLines')} 行，盘上 {human_bytes(int(d.get('diskBytes') or 0))}，"
               f"最早到 {d.get('historyFrom') or '—'}")
    return "\n".join(out)


def _webstats_text(d: dict) -> str:
    st = d.get("status") or {}
    lines = [f"访问统计：{d.get('source')} · 窗口 {d.get('windowMinutes')} 分钟"
             f"（{d.get('from') or '—'} → {d.get('to') or '—'}）"]
    if not d.get("parsed"):
        lines.append(f"这个窗口里没有可解析的请求行：扫描 {d.get('scannedLines')} 行，"
                     f"坏行 {d.get('malformed')} 条。日志格式可能是自定义 log_format"
                     "（既不是 combined 也不是 common），或者这个站点此刻确实没人访问。")
        return "\n".join(lines)
    lines.append(f"请求 {d['parsed']} 次 · 独立客户端 {d['clients']} 个 · "
                 f"下行 {human_bytes(int(d.get('bytesOut') or 0))} · 坏行 {d.get('malformed')}")
    if d.get("outsideWindow"):
        lines.append(f"  窗口外（更早）另有 {d['outsideWindow']} 行未计入")
    if d.get("truncatedRead"):
        lines.append("  注：日志比统计上限大，只看了尾部（truncatedRead）")
    lines.append("状态码：" + " ".join(f"{k} {st.get(k, 0)}" for k in
                                        ("2xx", "3xx", "4xx", "5xx", "1xx", "0xx") if st.get(k)))
    if d.get("topPaths"):
        lines.append("TOP 路径：")
        lines += [f"  {p['count']:>7}  {p['path']}" for p in d["topPaths"][:10]]
    if d.get("topClients"):
        lines.append("TOP 客户端：")
        lines += [f"  {c['count']:>7}  {c['key']}" for c in d["topClients"][:10]]
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
             "desc": "登录安全只读对账（sshd 有效配置 / 密钥与文件权限 / 空口令与 uid0 / "
                     "24h 失败来源 / 面板账号两步验证覆盖）",
             "notes": ["严格只读：不写 sshd_config、不 reload sshd、不改 authorized_keys——"
                       "SSH 是这台机器的主入口，改错就是把人锁在门外",
                       "配置来源会如实标注：sshd -T 是有效值，回落 sshd_config 时含注释掉的行",
                       "非 root 时 /etc/shadow 与他人 authorized_keys 读不到，在 notes 里说明而不是跳过",
                       "公钥只列算法/位数/备注/指纹，不回显公钥正文",
                       "读不到 sshd 配置时退出码 3；报告能产出就回 0，风险与否看 JSON 的 counts"]},
            {"name": "doctor", "args": [], "desc": "安全与健康自检，含每项修复建议"},
            {"name": "user list|create|reset|revoke|2fa-disable|delete",
             "args": ["<name>", "--password", "--password-stdin", "--role", "--yes"],
             "desc": "账号管理（改密会吊销旧 token；revoke 只吊销会话不改口令；list 带每个账号的两步验证状态）",
             "notes": ["create/reset 优先 --password-stdin：argv 里的口令同机用户 ps 可见",
                       "revoke <name> 只踢这个账号；revoke all 抬全局版本号，全员旧票一起失效",
                       "2fa-disable <name> --yes 是验证器丢失时的逃生口：只有 root 能跑，"
                       "关闭的同时吊销该账号全部会话；网页端关闭要口令＋一枚有效动态口令，不需要这个"]},
            {"name": "app templates|list|create|deploy|rollback|logs",
             "args": ["--id", "--name", "--template", "--type", "--repo", "--branch", "--path",
                      "--port", "--domain", "--start-cmd", "--install-cmd", "--lines"],
             "desc": "应用全生命周期管理",
             "notes": ["create 需要 --template 或 --start-cmd 之一；用 --start-cmd 时可配 --type/--install-cmd"]},
            {"name": "backup run|list|jobs|targets|add-target|edit-target|del-target|test-target"
                     "|push|link|cloud-list|cloud-pull",
             "args": ["--id", "--target", "--yes", "--name", "--endpoint", "--bucket", "--region",
                      "--access-key", "--secret-stdin", "--prefix",
                      "--path-style", "--no-path-style",
                      "--enable", "--disable"],
             "desc": "面板数据备份、备份任务留痕，以及 S3 兼容的异地备份目标",
             "notes": ["run 跑 scripts/backup.sh 备份面板数据，list 列本机产物",
                       "jobs 列备份任务与每条的最近一次执行留痕（timer 起的是独立进程，"
                       "留痕由 runner 自己写回 backups 表，终端与网页读同一份）",
                       "异地 Secret Key 只有 --secret-stdin 一条路：不提供 argv 写法，"
                       "因为它会留在 ps、世界可读的 /proc/<pid>/cmdline 和 root 的 shell 历史里",
                       "add-target 之后还要 link 才会真的推：目标是「这台机器往哪儿写」，"
                       "link 是「这个任务跑完交给哪个目标」",
                       "push 只补推没推过的产物（按 target+文件名判重）；link --target 0 取消关联",
                       "cloud-list 列这条任务在远端桶里有哪几份、本机有没有同一份；"
                       "cloud-pull --name 把仅存于远端的那一份拉回本机备份目录，"
                       "拉回才能恢复（恢复只读本机的文件）",
                       "cloud-pull 按块落盘并判两次大小，本机已有同名/符号链接/余量不足都在动手前拒绝；"
                       "中途断线不留半份文件——落在临时名上，列表与恢复都认不到它",
                       "del-target 只删配置：桶里的对象与本机备份一个都不动，面板不替人删数据",
                       "推送失败不会把这次备份改判成失败——少一份异地副本是要接手的一项，"
                       "不是备份没做；原因写在 targets 列表的「上次失败」里"]},
            {"name": "firewall list|allow|deny|remove|on|off",
             "args": ["--port", "--port-to", "--proto", "--source", "--comment", "--rate",
                      "--num", "--expect", "--yes"],
             "desc": "本机 ufw/firewalld 读写（与面板防火墙页同一套守卫）",
             "notes": ["拒绝任何会让这台机器进不去的写操作：sshd 监听端口、反代到面板的 nginx 入口端口、"
                                    "面板自身在非 loopback 上的监听端口，以及当前请求连进来的端口；没有绕过参数",

                       "remove 必须带 list 里读回的 sig（--expect）：编号会随任何一次删除前移",
                       "off 需要 --yes；on 会先确认 SSH 与面板入口端口已经在放行列表里",
                       "参数写歪退出码 2（用法错误），被自锁守卫拦下与防火墙自身报错为 1"]},
            {"name": "ban list|add|rm",
             "args": ["--hits", "--num", "--expect"],
             "desc": "sshd 爆破来源的一键封禁（写的还是本机防火墙那一张表）",
             "notes": ["只封单个公网地址：CIDR、域名、内网/回环/组播一律拒绝——封一片是人在终端上"
                       "想清楚的事，不该是一个顺手参数",
                       "复用 firewall_service 的编号+签名删除与自锁守卫，没有绕过参数；"
                       "ufw 用规则备注 choyeon-ban 标记，firewalld 用同名 ipset（rich rule 没有备注字段）",
                       "每一次写都重新列表确认：命令返回 0 而地址不在表里，按失败处理（退出码 1）",
                       "上限 50 条；重复封同一个地址只回「已经在列表里」，不会写第二条",
                       "地址过去 24 小时成功登录过照样封，但会先说明那会断开它的会话",
                       "rm（ufw）要带 ban list 读回的 --num 与 --expect；没有防火墙工具时退出码 3"]},
            {"name": "updates", "args": ["--refresh"],
             "desc": "待装系统补丁与安全更新（只读探测，面板不代跑 apt update/upgrade）",
             "notes": ["结论来自本地包索引；索引过旧会在 notes 里如实说明",
                       "判不出安全更新子集的发行版报 security_count=null，不会填 0 冒充没有",
                       "supported=false 时退出码 3（前置条件不满足）"]},
            {"name": "ports", "args": ["--refresh"],
             "desc": "监听端口与对外可达性对账（只读：不改绑定、不动防火墙）",
             "notes": ["判词五档：internal 仅本机、expected 预期对外、blocked 绑在全网但防火墙未放行、"
                       "exposed 外部可达且不在预期清单（含配了域名反代却绑全网的源站）、"
                       "unknown 防火墙现状没读到所以判不了",
                       "未启用防火墙时按\"非回环端口全都可达\"判定，不给 unknown——关掉 ufw 就是没有过滤；"
                       "但\"没读到\"与\"没启用\"是两件事，探测失败只会给 unknown，不会给 blocked",
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
            {"name": "load", "args": ["--refresh"],
             "desc": "负载与 CPU 归因对账（只读：不 renice、不 kill、不 restart、不改 CPUQuota=）",
             "notes": ["load average 是绝对排队数不是百分比，判据一律除以核数（每核 "
                       + str(load_service.LOAD_RISK_PER_CORE) + " 倍算要紧、"
                       + str(load_service.LOAD_WARN_PER_CORE) + " 倍算值得处理）；"
                       "2 核机器 load 4 与 64 核机器 load 4 是两件相反的事",
                       "Linux 的 load 把 D 状态（等 IO、不可中断）也算进去，所以另给 /proc/stat 的 "
                       "procs_blocked、D 名单与 iowait 占比，用来分开「CPU 不够」和「磁盘跟不上」",
                       "/proc/loadavg 第 4 个字段是「可运行/已注册任务总数」，不是卡在 IO 的数量；"
                       "真正的 blocked 只取 /proc/stat 的 procs_blocked 这一个数",
                       "每进程 CPU 是 " + str(load_service.SAMPLE_WINDOW_SEC) + " 秒窗口内的 tick 差值，"
                       "不是 ps 那种全生命周期平均（后者会把此刻空闲、跑了十几天的进程排到最前）",
                       "限流只读 cgroupfs 的 cpu.max / cpu.stat：systemctl show -p CPUQuota 不是有效属性名"
                       "（这台 Debian 13 返回空），CPUQuotaPerSecUSec 才是；cpu.max 的 80000 100000 就是 0.8 核",
                       "被限流的进程既不在 run queue 也不进 D 状态，load 与 CPU 曲线都好看，只有 "
                       "cpu.stat 的 nr_throttled 在涨——这是「机器不忙但活干不完」的第三种情况",
                       "/proc/stat 的 guest / guest_nice 已计在 user / nice 里，算占比要剔掉，否则总账超 100%",
                       "扫 /proc 有预算：最多 " + str(load_service.PROC_SCAN_CAP) + " 个进程、"
                       + str(load_service.SCAN_BUDGET_SEC) + " 秒，没扫完会如实标出来",
                       "load 与 /proc/stat 都读不到时退出码 3"]},
            {"name": "site-audit", "args": ["--refresh"],
             "desc": "站点链路对账（只读：不 reload nginx、不改 vhost、不签证书、不重启应用）",
             "notes": ["判的是四段连不连得上：server_name → proxy_pass 目标 → 证书覆盖 → 明文跳转；"
                       "配置解析、监听表与证书清单都复用已有读者，这一层只 join 不重算",
                       "nginx -t 只校验语法，不会发现 127.0.0.1:3100 上没进程；反代目标没人听是"
                       "配置、服务、证书三页全绿而用户拿到 502 的那一类",
                       "只有指向 127.0.0.1 / localhost / ::1 的目标能拿本机监听表判死；"
                       "指向别的机器的标成未确认，判死会逼人在一个根本没错的地址上找问题",
                       "ss 不可用或监听表被封顶时，缺席判定整轮降为未确认，不会折成「没人听」",
                       "证书只看覆盖关系（这张证书含不含这个域名），到期天数以证书项为准，"
                       "同一个 alert_ssl_days 阈值不在这里抄第二份",
                       "明文可达只在同一个文件里 80 与 443 都听着、且没有 301/308 到 https 时判",
                       "只出文件名不出绝对路径：/etc/nginx 下的目录结构本身就是攻击面信息",
                       "nginx 配置目录一个都不存在时退出码 3"]},
            {"name": "site list|claim|unclaim|claim-all", "args": ["--app"],
             "desc": "站点纳管：域名归属登记 + 认领配置文件（不写 nginx、不 reload）",
             "notes": ["归属由登记表说了算，不再按「反代端口等于哪个应用的端口」猜：共用端口和"
                       "应用登记的端口与配置对不上（3100 vs 3300）两种情况都会猜错",
                       "判定强度依次是：反代到面板自身端口（那是面板入口，单独一档 kind=panel）＞"
                       "应用登记的域名 ＞ 根路径 location / 的反代端口（且该端口只属于一个应用）＞"
                       "配置文件名与应用同名 ＞ 跟随字面量 301 的目标域名",
                       "判不出的一个都不猜：claim 直接报错说明原因，claim-all 把它们列进 skipped，"
                       "要落归属就带 --app <应用 id> 手工指定",
                       "纳管只写登记表这一行，nginx 的文件一个字节都不动；取消纳管也只删这一行，"
                       "域名照旧由原配置服务",
                       "之后要改这份文件，走的还是原有那条临界区（原子写 → nginx -t → 失败回滚 → reload），"
                       "纳管本身不多开一条绕过校验的写路径",
                       "应用被删除时它名下的登记一起清掉（派生态跟着对象走）",
                       "登记表里认领的文件不在 nginx 加载列表时标 claim_stale，不静默；"
                       "配置已删而登记还在的行照样出现在列表里，否则页面上没有入口能看见它",
                       "nginx 配置目录一个都不存在时退出码 3"]},
            {"name": "webstats", "args": ["--file", "--minutes", "--list", "--daily", "--days"],
             "desc": "网站访问流量统计（只读：状态码分布、TOP 路径与客户端、每分钟请求数）",
             "notes": ["来源白名单 = CP_LOG_ROOTS + CP_FILE_ROOTS，与网页端日志中心是同一份列表；"
                       "--file 序号在 --list、网页下拉和这个命令里指同一个文件",
                       "只往文件尾读 " + str(access_stats.MAX_BYTES // 1024 // 1024) + "MB、最多 "
                       + str(access_stats.MAX_LINES) + " 行，再大的日志只看尾部，truncatedRead 会如实标出来",
                       "路径与客户端各自最多 " + str(access_stats.MAX_KEYS) + " 个键，超出并进「…其他」并计数，"
                       "不会因为爬虫刷唯一 URL 就把内存吃光",
                       "统计口径不含查询串：访问日志的 ?token= 常带凭据，而榜单要回答的是哪个地址被访问得多",
                       "扫描器会把 TLS 记录打到 80 端口，nginx 用 `\\xHH` 文本记下不可打印字节；"
                       "这类行还原后认不出方法名，方法位统一记 \"-\"，不会挤掉真实流量的榜位",
                       "时间窗按日志里最新一行往回算，不按墙上时钟：一小时没人访问的站点用当前时间做窗口会得到全空表",
                       "--daily 是另一条读法：把轮替出来的世代（.1、.2.gz……）逐份解压再按天聚合，"
                       "最多回看 " + str(access_stats.MAX_DAYS) + " 天、最多 "
                       + str(web_logs.MAX_SERIES_FILES) + " 份、单份解压后 "
                       + str(access_stats.DAILY_FILE_BYTES // 1024 // 1024) + "MB，"
                       "被截断的份数写在 truncatedFiles 里",
                       "--daily 的环比只在上一周期依据完整时才给百分比（comparable=false 时 deltaPvPct 是 null）；"
                       "日志没那么长（history）、世代被截断（clipped）都宁可没有百分比，也不要一个假的",
                       "按天的独立客户端跨天去重后再数，单日最多记 " + str(access_stats.MAX_UV_PER_DAY)
                       + " 个、窗口并集最多 " + str(access_stats.MAX_KEYS)
                       + " 个，超出标 uvFloored 读成「至少这么多人」",
                       "找不到来源时退出码 3 并给出修复线索；选中错误日志（kind=error）是用法错误，退出码 2"],
             "exitCodes": {"0": "统计已产出（有没有异常看 JSON 里的数字）", "2": "序号/窗口写错或选到错误日志",
                           "3": "白名单里没有任何日志文件"}},
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
    u.add_argument("user_cmd", choices=["list", "create", "reset", "revoke", "2fa-disable", "delete"])
    u.add_argument("name", nargs="?", help="revoke 传 all 表示吊销全部用户；2fa-disable 是验证器丢失时的逃生口")
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

    b = sub.add_parser("backup", help="备份管理与异地目标", parents=[common])
    b.add_argument("backup_cmd", choices=[
        "run", "list", "jobs", "targets", "add-target", "edit-target", "del-target",
        "test-target", "push", "link", "cloud-list", "cloud-pull",
    ])
    b.add_argument("--id", type=int, help="备份任务 id（push/link/jobs/cloud-*）或异地目标 id（edit/del/test-target）")
    b.add_argument("--target", type=int, help="link/push/cloud-list/cloud-pull：异地目标 id，link 用 0 取消关联")
    b.add_argument("--yes", action="store_true", help="del-target：确认删除目标配置")
    b.add_argument("--name", help="add/edit-target：目标名；cloud-pull：要拉回的产物名")
    b.add_argument("--endpoint", help="add/edit-target：S3 兼容端点，如 s3.amazonaws.com、oss-cn-hangzhou.aliyuncs.com")
    b.add_argument("--bucket", help="add/edit-target：桶名")
    b.add_argument("--region", help="add/edit-target：区域，缺省 us-east-1")
    b.add_argument("--access-key", dest="access_key", help="add/edit-target：Access Key Id")
    b.add_argument("--secret-stdin", dest="secret_stdin", action="store_true",
                   help="add/edit-target：从标准输入读 Secret Key（不提供写在命令行里的选项）")
    b.add_argument("--prefix", help="add/edit-target：对象键前缀，如 panel-backup/")
    b.add_argument("--path-style", dest="path_style", action=argparse.BooleanOptionalAction, default=None,
                   help="add/edit-target：用 {endpoint}/{bucket} 寻址（自建 MinIO 需要），--no-path-style 关闭")
    b.add_argument("--enable", action="store_true", help="edit-target：启用这个目标（恢复自动推送）")
    b.add_argument("--disable", action="store_true", help="edit-target：停用这个目标（暂停自动推送）")
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

    bn = sub.add_parser("ban", help="sshd 爆破来源封禁（写的还是本机防火墙）", parents=[common])
    bn.add_argument("ban_cmd", choices=["list", "add", "rm"])
    bn.add_argument("ip", nargs="?", help="add/rm：单个公网地址，不接受 CIDR、域名或内网地址")
    bn.add_argument("--hits", type=int, help="add：过去 24 小时失败次数，只写进备注，不参与判定")
    bn.add_argument("--num", type=int, help="rm（ufw）：ban list 里这条封禁的规则编号")
    bn.add_argument("--expect", help="rm（ufw）：ban list 里这条封禁的 sig，服务端重新列表比对")
    bn.set_defaults(fn=cmd_ban)

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

    lo = sub.add_parser("load", help="负载与 CPU 归因对账（只读：不改配额、不杀进程）", parents=[common])
    lo.add_argument("--refresh", action="store_true", help="忽略 60 秒缓存，立即重采一个窗口")
    lo.set_defaults(fn=cmd_load)

    sa = sub.add_parser("site-audit", help="站点链路对账（只读：不 reload、不改配置、不签证书）",
                        parents=[common])
    sa.add_argument("--refresh", action="store_true", help="忽略 60 秒缓存，立即重新扫一遍 nginx 配置")
    sa.set_defaults(fn=cmd_site_audit)

    st = sub.add_parser("site", help="站点纳管：域名归属登记与配置文件认领（不写 nginx、不 reload）",
                        parents=[common])
    st.add_argument("site_cmd", choices=["list", "claim", "unclaim", "claim-all"])
    st.add_argument("domain", nargs="?", help="claim/unclaim：nginx 里正在服务的那个域名")
    st.add_argument("--app", help="claim：判不出归属时手工指定应用 id（choyeonctl app list 的第一列）")
    st.set_defaults(fn=cmd_site)

    ws = sub.add_parser("webstats", help="网站访问流量统计（只读：解析 nginx access.log）", parents=[common])
    ws.add_argument("--file", default="0", help="日志来源序号（webstats --list 读回，与网页端同一份）")
    ws.add_argument("--minutes", default="60", help=f"统计窗口分钟数（1-{access_stats.SERIES_MAX}）")
    ws.add_argument("--daily", action="store_true",
                  help="按天统计并和上一个同样长的周期比（会读轮替出来的历史世代）")
    ws.add_argument("--days", default=None,
                  help=f"按天统计的天数（1-{access_stats.MAX_DAYS}，只配 --daily 用，"
                       f"默认 {access_stats.DAY_LADDER[1]}）")
    ws.add_argument("--list", action="store_true", help="只列出可统计的日志来源")
    ws.set_defaults(fn=cmd_webstats)

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
