import asyncio
import json
import os
import re
import shutil
import time
from contextlib import suppress
from pathlib import Path

from .. import aio_db, config, locks
from .. import database as dbm
from ..util import (
    clean_abs_path,
    human_bytes,
    is_branch,
    is_domain,
    is_git_url,
    is_name,
    is_port,
    is_unit,
    redact_creds,
    run,
    run_shell_async,
    sub_port,
    unit_exec_arg_escape,
    write_secret_atomic,
)
from . import nginx_ops, systemd_ops
from .files_service import write_budget
from .systemd_ops import is_active, service_action

APP_ROOT = config.APP_ROOT
UNIT_DIR = config.UNIT_DIR

# 应用环境变量的落盘文件名，以及面板给 unit 补环境变量的 drop-in 名。
# 写成常量是因为 unit 模板、.panel.env 路径、drop-in 内容三处必须指向同一个文件，
# 而"面板说写了、systemd 读的是另一个名字"正是本批次要修的这类 bug。
# drop-in 名不能改：systemd 的 <unit>.d/ 里可能有别人的文件，面板只认自己这个名字。
ENV_FILE_NAME = ".panel.env"
ENV_DROP_IN_NAME = "panel-env.conf"

_DEPLOY_TASKS: set = set()
# 后台任务对应的部署 id：只有进程内还在跑的部署才在这个集合里。
# 看门狗据此区分"慢"和"孤儿"（见 reap_orphan_deployments）。
_RUNNING_DEPS: set[int] = set()

# 部署各步骤的命令超时。集中定义不是为了好看，是因为下面两件事都必须由它推导：
#   1) CLI 等待部署结束的时间（wait_deployment 默认值）
#   2) 看门狗判定一条 running 记录是否已经是孤儿
# 各写一个"看着够用"的数就是批次⑤G 修的那个 bug：CLI 的 1800s 小于
# clone(600)+install(1800) 的最坏路径，CLI 先放弃并随 asyncio.run 退出把部署任务取消，
# 记录停在 running，而 _ensure_no_deployment 让这个应用此后无法删除/回滚/改配置。
GIT_CLONE_T = 600
GIT_FETCH_T = 300
GIT_CHECKOUT_T = 120
GIT_RESET_T = 120
INSTALL_T = 1800
# 写 unit、渲染 .panel.env、应用 nginx 反代，再到重启并确认主进程稳定存活
DEPLOY_ONLINE_T = 300

# 取最坏路径：首次 clone（或 fetch+checkout+reset）+ 安装依赖 + 上线确认。
# 这是"一次部署最多可能占用多久"的上界，不是期望耗时。
DEPLOY_BUDGET_SEC = (
    max(GIT_CLONE_T, GIT_FETCH_T + GIT_CHECKOUT_T + GIT_RESET_T) + INSTALL_T + DEPLOY_ONLINE_T
)

TEMPLATE_TOKENS = ("{{name}}", "{{path}}", "{{start_cmd}}", "{{port}}")

_VERB_CN = {"start": "启动", "stop": "停止", "restart": "重启"}


def unit_name(app: dict) -> str:
    return app.get("unit_override") or f"panel-{app['name']}.service"


def render_unit_from(tpl: str, app: dict) -> str:
    return (
        tpl.replace("{{name}}", app["name"])
        .replace("{{path}}", app["path"])
        # 同 render_unit：自定义模板里 {{start_cmd}} 展开出来的行一样会被 systemd
        # 处理，`%X`/`${VAR}` 的展开风险与默认模板没有区别，所以这里同样转义。
        .replace("{{start_cmd}}", json.dumps(unit_exec_arg_escape(app["start_cmd"])))
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
        f"EnvironmentFile=-{app['path']}/{ENV_FILE_NAME}\n"
        # 实测：ExecStart 整行（引号内也一样）会被 systemd 展开 `%X` 与 `${VAR}`。
        # 启动命令里出现 `%b` 时，`/bin/bash -lc "printf tag=%b"` 真的会执行成
        # `printf tag=/bin/sh`（unit 文本原样落进 /etc/systemd/system 起服务测出）；
        # 出现 `${DB_PASSWORD}` 时 systemd 会先拿 unit 自己的环境（含本 app 的
        # .panel.env）替换掉它，替换结果直接进 argv——/proc/<pid>/cmdline 是全局
        # 可读的，等于把密钥暴露给机器上任何用户，而 `${PORT:-3000}` 这类写法
        # 更会被吞成空串，进程 rc=0，面板只显示"启动成功"。
        # 所以这里用 unit_exec_arg_escape 把 `$`、`%` 各翻倍，展开交回 bash。
        # 路径不用处理：clean_abs_path 的白名单不含 `%` 和 `$`。
        f"ExecStart=/bin/bash -lc {json.dumps(unit_exec_arg_escape(app['start_cmd']))}\n"
        f"Restart=always\n"
        f"RestartSec=3\n"
        f"User=root\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def _env_escape(v) -> str:
    r"""把环境变量值写成 systemd EnvironmentFile 的双引号内容。

    EnvironmentFile 里双引号内的 `\` 是转义符，所以必须先转义 `\` 再转义 `"`。
    旧写法只转义了 `"`，实测后果比"值显示不对"严重得多：
      值 `abc\\`（口令、Windows 路径结尾极常见）写成 `TRAILBS="abc\"`，
      结尾的 `\` 把右引号吃掉，字符串不闭合，systemd 一路读到下一行的引号才停——
      journal 里 TRAILBS 的实际值变成 `abc"\nDOUBLEBS=a\b"`，
      而下一行的 DOUBLEBS 直接被吞掉、根本没有被定义。
    也就是说前一个变量的值会吃掉后一个变量：应用拿到错乱的配置，
    而部署日志显示成功，排查时完全看不出是环境变量出了问题。
    `\n`/`\r` 换成空格：换行本身也会截断这一行。
    `$` 不需要处理——已实测 systemd 不做变量展开（`cost $HOME` 原样传入）。
    """
    s = str(v if v is not None else "")
    s = s.replace(chr(92), chr(92) * 2)
    s = s.replace(chr(34), chr(92) + chr(34))
    return s.replace(chr(10), " ").replace(chr(13), " ")


def env_file_path(app: dict) -> Path:
    return Path(app["path"]) / ENV_FILE_NAME


def env_body(app: dict) -> str:
    try:
        vars_ = json.loads(app.get("env") or "[]")
    except (ValueError, TypeError):
        vars_ = []
    return "\n".join(
        f'{x["k"]}="{_env_escape(x.get("v", ""))}"'
        for x in vars_
        # fullmatch：$ 在 re.match 下允许末尾一个换行过关，键名 "A\n" 会被写成
        # 跨行的 `A\n="v"`，systemd 读到的是两个畸形行。
        if isinstance(x, dict) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", x.get("k") or "")
    )


def write_env_file(app: dict) -> str:
    """写安装目录下的 .panel.env，返回 written / unchanged / no-dir 三态之一。

    三态是给调用方判断"这次保存到底要不要重启"用的：内容没变还重启一次服务，等于每次
    点保存都把线上进程抖一下；目录不存在则是"还没落地"，不能说成已生效。
    """
    body = env_body(app)
    target = env_file_path(app)
    if not target.parent.is_dir():
        # 应用还没部署过（目录都不存在）时不要顺手把目录建出来：git clone 拒绝往
        # "已存在且非空"的目录里写，而这里正是那份非空内容，用户会看到"部署失败"却
        # 完全不知道和刚才保存配置有关。目录会由部署自己创建，届时再写这个文件。
        return "no-dir"
    try:
        if target.is_file() and target.read_text(encoding="utf-8", errors="replace") == body + "\n":
            # 内容一致也要把权限补回 0600：手工 chmod 过、或者从备份恢复回来的文件
            # 权限未必对，而这里正是唯一一次机会。
            os.chmod(target, 0o600)
            return "unchanged"
        write_secret_atomic(target, body + "\n")
    except OSError as e:
        raise RuntimeError(f"写入 .panel.env 失败：{e}") from e
    return "written"


def _env_drop_in_dir(unit: str) -> Path:
    return Path(UNIT_DIR) / f"{unit}.d"


def _env_drop_in_text(app: dict) -> str:
    return (
        "# 由 choyeon-panel 写入：把应用目录下的 .panel.env 挂给这个 unit。\n"
        "# 面板不改写 unit 本体，删掉本文件（以及空掉的同名目录）就恢复原样。\n"
        "[Service]\n"
        f"EnvironmentFile=-{app['path']}/{ENV_FILE_NAME}\n"
    )


def unit_reads_env_file(app: dict) -> bool:
    """这个 unit 生效的配置里是否已经引用了**这个应用自己的** .panel.env。

    两个都必须匹配：
    - 只认 EnvironmentFile= 行。以 # 开头的注释写法 systemd 不会加载，判成"已经引用"
      就等于把环境变量永久地判成已生效。
    - 认整条路径。安装目录改过之后，unit 里那行指向的还是旧目录，环境变量照样读不到，
      但按文件名匹配会回"已经引用"，于是这次保存又被判成已生效。
    """
    unit = unit_name(app)
    want = str(env_file_path(app))
    files = [Path(UNIT_DIR) / unit, *_env_drop_in_dir(unit).glob("*.conf")]
    for f in files:
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("EnvironmentFile") and want in line:
                return True
    return False


async def ensure_env_link(app: dict) -> str:
    """保证面板写的 .panel.env 真的会被这个 unit 读进来，返回本次动作。

    为什么需要这一步：unit 里那行 `EnvironmentFile=` 只存在于面板的**默认模板**。纳管
    外部 unit（unit_override）时面板不写 unit 文件，自定义 unit_template 也可能整段漏掉
    这一行——两种情况下环境变量都写进了 .panel.env，进程却一个字节都读不到，而界面照样
    报"已保存"。这就是"改了没反应"的根因，比不写文件更糟。
    补 drop-in 而不是改别人的 unit：systemd 的 <unit>.d/ 就是为此准备的，面板自己的
    默认模板已经引用了该文件，所以正常托管应用一次都不会走到写文件这步。
    返回值：written / unchanged / no-unit / invalid-unit（后两者由调用方如实上报）。
    """
    if unit_reads_env_file(app):
        return "unchanged"
    unit = unit_name(app)
    if not is_unit(unit):
        return "invalid-unit"
    if not (Path(UNIT_DIR) / unit).is_file():
        # 本机根本没有这个 unit 文件：补一个没人读的 drop-in 只会留下一份孤儿配置。
        return "no-unit"
    d = _env_drop_in_dir(unit)
    d.mkdir(parents=True, exist_ok=True)
    target = d / ENV_DROP_IN_NAME
    want = _env_drop_in_text(app)
    try:
        same = target.is_file() and target.read_text(errors="replace") == want
        if not same:
            write_secret_atomic(target, want)
    except OSError as e:
        raise RuntimeError(f"写入 systemd drop-in 失败：{e}") from e
    if same:
        return "unchanged"
    r = await run("systemctl", ["daemon-reload"])
    if r["code"] != 0:
        raise RuntimeError(r["out"] or "daemon-reload 失败")
    return "written"


async def remove_env_link(app: dict) -> bool:
    """删掉面板写过的 drop-in（应用删除、或 unit 已经自己引用 .panel.env 时）。"""
    unit = unit_name(app)
    if not is_unit(unit):
        return False
    d = _env_drop_in_dir(unit)
    target = d / ENV_DROP_IN_NAME
    if not target.is_file():
        return False
    target.unlink()
    with suppress(OSError):
        # 目录里还有别人的 drop-in 时 rmdir 会失败，正是要留着它。
        d.rmdir()
    await run("systemctl", ["daemon-reload"])
    return True


async def ensure_unit(app: dict, warn=None):
    """写 unit、daemon-reload，并让它进开机目标。

    enable 这一步以前完全缺位：面板只 start 不 enable，而 systemd 不会自己把新写的 unit
    纳入开机目标——"这次上线成功"和"机器重启后它还在"是两件事。线上接管 Rosetta 时这个
    缺口最刺眼：手工写的两个 unit 原本都是 enabled，面板接管后反而会在重启后丢服务，
    托管比不托管还少一层保障。
    自定义 unit_template 没写 [Install] 时 enable 会被 systemd 拒绝（rc=1），那种 unit
    照样能手动启动，所以降级成提醒（warn），不把一次已经上线的部署判成失败。
    """
    if app.get("unit_override"):
        return
    # 半截 unit 当场看不出来（还没 daemon-reload），要等下一次 reload 才被读进去。
    write_secret_atomic(Path(f"{UNIT_DIR}/{unit_name(app)}"), render_unit(app))
    r = await run("systemctl", ["daemon-reload"])
    if r["code"] != 0:
        raise RuntimeError(r["out"])
    e = await run("systemctl", ["enable", "--", unit_name(app)], timeout=30)
    if e["code"] != 0 and warn is not None:
        warn(
            f"!!> unit 已写入，但没能设置开机自启：{(e['out'] or '').strip()[:160]}"
            "（机器重启后这个应用不会自己起来；unit 里要有 [Install] WantedBy= 才能 enable）"
        )


async def remove_unit(app: dict):
    if app.get("unit_override"):
        return
    await run("systemctl", ["disable", "--now", unit_name(app)], timeout=30)
    p = f"{UNIT_DIR}/{unit_name(app)}"
    if os.path.exists(p):
        os.unlink(p)
    await run("systemctl", ["daemon-reload"])


_ss_cache: tuple[float, list[str]] | None = None


async def _listen_lines(force: bool = False) -> list[str]:
    """读取监听端口表（5 秒内复用），异步执行避免阻塞事件循环。"""
    global _ss_cache
    if not force and _ss_cache and time.time() - _ss_cache[0] < 5:
        return _ss_cache[1]
    out = await run_shell_async("ss -tlnp", timeout=4)
    _ss_cache = (time.time(), out.split("\n"))
    return _ss_cache[1]


async def detect_port(unit: str, force: bool = False):
    r = await run("systemctl", ["show", unit, "-p", "MainPID", "--value"], timeout=5)
    try:
        main = int(r["out"].strip())
    except ValueError:
        main = 0
    if main <= 0:
        return None
    return await _port_for_pid(main, await _listen_lines(force))


def _pid_tree(main: int) -> set:
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


async def _port_for_pid(main: int, lines: list) -> int | None:
    pids = await asyncio.to_thread(_pid_tree, main)
    ports = set()
    for line in lines:
        # 一行可以挂着多个共享同一监听套接字的进程（cluster/预派生 worker 先 bind
        # 再 fork，`ss` 就把它们写进同一个 users:(...)）。旧写法用 re.search 只取
        # 第一个 pid：主进程排在后面时这行整个被跳过，于是端口探测随机失灵——
        # 表现为应用确实在监听、面板却显示「端口未知」，域名/反代对齐也拿不到端口。
        pids_on_line = {int(x) for x in re.findall(r"pid=(\d+)", line)}
        if not (pids_on_line & pids):
            continue
        parts = line.strip().split()
        addr = parts[3] if len(parts) > 3 else ""
        port = re.search(r":(\d+)$", addr)
        if port:
            ports.add(int(port.group(1)))
    return min(ports) if ports else None


async def list_apps() -> list:
    rows = await aio_db.query("SELECT * FROM apps ORDER BY name")
    units = [unit_name(a) for a in rows]
    # 一次 systemctl show 取代每 app 两次子进程；一次 deployments 查询取代 N 次
    states, dom_map, deploying = await asyncio.gather(
        systemd_ops.unit_states(units),
        asyncio.to_thread(nginx_ops.domains_by_port),
        aio_db.query("SELECT app_id FROM deployments WHERE status='running'"),
    )
    busy = {r["app_id"] for r in deploying}

    async def _one(a: dict):
        st = states.get(unit_name(a)) or dict(systemd_ops.UNKNOWN_STATE)
        running = st["active"]
        detected = None
        if a["port"] is None and running and st["mainPid"] > 0:
            detected = await _port_for_pid(st["mainPid"], await _listen_lines())
        port = a["port"] if a["port"] is not None else detected
        domains = [a["domain"]] if a["domain"] else (dom_map.get(port, []) if port is not None else [])
        out = {k: v for k, v in a.items() if k != "env"}
        out.update(
            running=running,
            unit=unit_name(a),
            # running 只是"健康运行中"这一个事实。前端原来只能二选一显示"运行中/已停止"，
            # 于是正在启动、上次启动失败、这台机器上根本没有这个 unit，全都被说成"已停止"。
            unitState=st["state"],
            unitMissing=st["missing"],
            port=port,
            portAuto=a["port"] is None and detected is not None,
            domains=domains,
            deploying=a["id"] in busy,
        )
        return out

    return list(await asyncio.gather(*(_one(a) for a in rows)))


def get_app(app_id: int):
    return dbm.query_one("SELECT * FROM apps WHERE id=?", (app_id,))


def _app_path(i: dict) -> str:
    """取应用安装目录：先补默认值，再统一校验 + 规范化。

    规范化必须在写库之前做，否则 `/root/www/a/../../www/a` 与 `/root/www/a`
    是两个不同的字符串，却指向同一个目录——重复检查按字符串比较就会放行，
    两个应用共用一个工作目录，后部署的那个覆盖前一个的 unit 与 .panel.env。
    """
    return clean_abs_path(i.get("path") or f"{APP_ROOT}/{i['name']}", "安装目录")


def _stored_path_key(raw) -> str:
    """把库里已存的 path 折成一个可比较的键。

    历史数据里可能有旧校验放行过的 `..` 写法，clean_abs_path 会直接拒绝它们；
    如果因此跳过比对，等于"老数据写法的同一目录"继续躲得过重复检查——
    新应用照样能和它共用工作目录。这里对老数据放宽到只做 normpath：
    只用来比较，不用来拼命令或写盘，所以把 .. 折掉比忽略更安全。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return clean_abs_path(raw, "路径")
    except RuntimeError:
        return os.path.normpath(raw) if raw.startswith("/") else ""


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
    if i.get("unit_template"):
        if len(i["unit_template"]) > 4000:
            raise RuntimeError("unit 模板过长")
        if re.search(r"\$\{|\$\(|`", i["unit_template"]):
            raise RuntimeError("unit 模板不允许 shell 替换语法")
    if i.get("unit_override") and not (
        is_unit(i["unit_override"]) and i["unit_override"].endswith(".service")
    ):
        # 旧写法自己写了一份正则，比 util.UNIT_RE 宽：首字符允许 -@:. 三种。
        # 而 '-' 开头的 unit 名会被 systemctl 当命令行选项解析，实测
        # `systemctl status -ping.service` 返回 0 并打印整机状态——
        # 面板把"选项解析成功"读成"这个应用在跑"，列表显示运行中、
        # 重启按钮一路绿灯，实际那个应用从来没有被碰过。
        # 现在与读接口共用 util.is_unit（首字符必须是字母/数字）并要求 .service 后缀，
        # systemctl 侧另加 `--` 结束选项标记（见 systemd_ops.is_active）。
        raise RuntimeError("unit 名不合法：需以字母或数字开头，仅含 A-Za-z0-9@:._-，并以 .service 结尾")
    if i.get("db_names"):
        for d in i["db_names"].split(","):
            # fullmatch 而不是 match：`$` 允许末尾一个换行通过，于是 "appdb\n"
            # 也算合法库名。这个名字后面直接进 `psql -d`、sh -c 里的 pg_dump 命令串
            # 与 SQL 字面量，带着换行的那一份会在 shell 里断成两个参数。
            if d and not re.fullmatch(r"[a-z0-9_]{1,63}", d):
                raise RuntimeError(f"关联数据库名不合法: {d}")
    path = _app_path(i)
    # 要害目录检查按"最终目录"做，而不是只看用户填没填 path：不填时默认值是
    # {APP_ROOT}/{name}，而 name 允许 choyeon-panel——那条记录会指向面板自己的
    # 代码目录，点一次部署就把自己 git reset 掉，purge 更是直接 rmtree。
    _check_not_protected(path)
    dup = dbm.query_one(
        "SELECT id FROM apps WHERE id IS NOT ?"
        " AND (name=? OR path=? OR (domain IS NOT NULL AND domain=? AND domain!=''))",
        (ignore_id, i["name"], path, i.get("domain") or ""),
    )
    if dup:
        raise RuntimeError("名称/路径/域名已被其他应用占用")
    # 已经存在的记录里可能有本次改动之前写入的、写法不同的同一路径（带尾斜杠、带 ..），
    # 这里按规范化结果再比一次，避免"两条不同字符串指向同一目录"混过检查。
    for row in dbm.query("SELECT id, path FROM apps WHERE id IS NOT ?", (ignore_id,)):
        if _stored_path_key(row["path"]) == path:
            raise RuntimeError(f"安装目录与应用 #{row['id']} 相同")
    if i.get("port"):
        busy = dbm.query_one("SELECT id FROM apps WHERE port=? AND id IS NOT ?", (i["port"], ignore_id))
        if busy:
            raise RuntimeError(f"端口 {i['port']} 已被应用 #{busy['id']} 使用")


def _ensure_no_deployment(app_id: int, action: str):
    """删除之外，回滚与改配置同样不能和进行中的部署并发。

    部署任务在创建时就拿到了 app 字典的快照，回滚却在同一目录上跑
    `git reset --hard`：两边会互相把 HEAD 拧到不同 commit，
    部署结束时写回 unit 的还是旧快照，现场无法解释。
    旧代码只给 delete_app 加了这道守卫。

    这是数据库状态位那一层的守卫，批次㉝ 之后它只是第二道闸：`status='running'`
    是"上次有人记得写、下次没人记得清"的状态，进程被 OOM 杀掉就永远停在那里；
    真正的互斥由 `locks.key_app(app_id)` 那把 flock 提供（跨 CLI/timer/网页三个入口，
    且由内核在进程死亡时释放）。保留这一层的理由是它还能挡住"锁之外的人"——
    手工往 deployments 表插一行、或旧版本面板留下的行。
    """
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError(f"部署进行中，不能{action}")


def _ensure_app_dir(i: dict, path: str) -> None:
    """没有仓库就没有 clone 去建目录，所以在这里把它补建出来。

    线上实测：`choyeonctl app create p5g-hang --start-cmd "sleep 30"` 之后点部署，
    日志走到「跳过拉取（未配置仓库，使用现有目录 /root/www/p5g-hang）」，紧接着
    "!!! 部署失败: 目录不存在"——应用记录留下了，部署却永远跑不通，而这条路径是
    CLI 明确支持的（--start-cmd 不带 --repo/--path），前端建应用时只填目录也一样。
    带仓库时不建：git clone 自己会建，且目录已存在且非空时它会拒绝，那个拒绝是有
    意义的（不覆盖别人的工作区）。要害目录已由 _validate 里的 _check_not_protected
    挡掉，这里不会把面板自己的代码目录当成应用目录创建。
    """
    if i.get("repo_url") or os.path.isdir(path):
        return
    Path(path).mkdir(parents=True, exist_ok=True)


def _budget_probe_dir(path: str) -> str:
    """落盘余量要问的是分区，而 clone 的目标目录此刻往往还不存在。

    `os.statvfs` 只能问已经存在的路径，所以往上退到最近的真目录——分区是同一块。
    """
    d = os.path.abspath(path)
    while d != os.path.dirname(d) and not os.path.isdir(d):
        d = os.path.dirname(d)
    return d


_MIN_DEPLOY_ROOM = 64 * 1024 * 1024


def _assert_disk_room(dest: str, what: str) -> dict:
    """clone / 装依赖这种"要写多少只有它自己知道"的步骤，动手前问同一个读者。

    和备份、解包那些入口比的是同一个数：`write_budget()`，出处仍是设置页的
    `alert_disk_pct`。这里没有声明值可比（仓库多大、npm 依赖装多大是外部决定的），
    所以判的是余量本身：room 归零说明这块盘已经压过警戒线，再往下写就是把它顶穿；
    room 只剩一点点同样要拒——半路的 ENOSPC 会把 node_modules 留在半份状态，
    下一次的失败原因从"盘满了"变成"依赖装不上"，比原地拒绝更难查。所以另外要求
    一个下限 `_MIN_DEPLOY_ROOM`：任何一次真实的 install（venv + 几个包 / npm 依赖）
    都不止 64MB，够不到这个数就不是"这次部署小所以可以通融"。
    面板以 root 运行，顶穿的第一现场往往是它自己的 SQLite，后果不是"这一次部署
    失败"，是这台机器上所有业务一起停摆。
    """
    b = write_budget(dest)
    if b["room"] < _MIN_DEPLOY_ROOM:
        raise RuntimeError(
            f"{what}要往 {dest} 所在分区写字节量未定的内容，而按磁盘警戒线 {b['line']}% "
            f"只排得出 {human_bytes(b['room'])}（实际剩余 {human_bytes(b['avail'])}），"
            f"低于一次安装至少需要的 {human_bytes(_MIN_DEPLOY_ROOM)}："
            "先清理这块盘，或在设置页把磁盘告警阈值调到更贴近 100%；已拒绝，未写入任何文件"
        )
    return b


def _discard_partial_checkout(path: str, dep_id: int, preserve: bool) -> None:
    """这一次没拉成的目录，如果它在这之前根本不存在、而且连代码都没拉完，就删掉它。

    留着比删掉更糟：下一次部署会对"已存在且非空"的目录执行 git clone 而当场失败
    （那个拒绝本身是有意义的，见 `_ensure_app_dir` 的注释），用户只能先手工删目录，
    而面板上看到的是一句看不出根因的失败。
    `preserve` 是调用方给出的两种"不许动"：目录事先存在（里面可能是别人的工作区），
    或者这一次代码已经完整拉到手（此时失败在装依赖/重启，盘上是可用的一份代码；
    删掉它等于把用户已经等了几分钟的 clone 也一起扔掉）。
    判据放在参数里而不是两条 except 各自记得，是因为漏一次就等于给"别人的目录"
    装了删除路径。
    """
    if preserve:
        return
    try:
        _check_not_protected(path)
    except RuntimeError:
        return
    with suppress(OSError):
        shutil.rmtree(path)
        _append_log(dep_id, "==> 已删除这次没拉成的目录，下次部署从空目录重新开始")


def create_app(i: dict) -> dict:
    _validate(i)
    path = _app_path(i)
    _ensure_app_dir(i, path)
    parent_id = None
    if i.get("parent_id"):
        parent_id = int(i["parent_id"])
        if not get_app(parent_id):
            raise RuntimeError("组合项目的父应用不存在")
    res = dbm.execute(
        "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,"
        "start_cmd,env,unit_override,db_names,unit_template,parent_id,component_role)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            parent_id,
            str(i.get("component_role") or "")[:16],
        ),
    )
    return get_app(res)


def update_app(app_id: int, i: dict) -> dict:
    cur = get_app(app_id)
    if not cur:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "修改配置")
    merged = dict(cur)
    merged.update({k: v for k, v in i.items() if k != "env"})
    _validate(merged, app_id)
    vars_ = i["env"] if "env" in i and i["env"] is not None else json.loads(cur["env"] or "[]")
    # 改端口时同步 start_cmd / env 里写死的旧端口，与建号期的模板替换同款。
    # 不这样做的话面板记录的是新端口、进程仍监听旧端口，套上 nginx 就是 502，
    # 而 UI 上一切正常。显式改了 start_cmd 或 env 时以用户输入为准，不再猜。
    old_port, new_port = cur.get("port"), merged.get("port")
    if None not in (old_port, new_port) and str(old_port) != str(new_port):
        if "start_cmd" not in i or i.get("start_cmd") in (None, ""):
            merged["start_cmd"] = sub_port(merged["start_cmd"], old_port, new_port)
        if "env" not in i or i["env"] is None:
            vars_ = [
                {**e, "v": sub_port(e.get("v", ""), old_port, new_port)} if isinstance(e, dict) else e
                for e in vars_
            ]
    dbm.execute(
        "UPDATE apps SET name=?,type=?,repo_url=?,branch=?,path=?,port=?,domain=?,install_cmd=?,start_cmd=?,env=?,"
        "unit_override=?,db_names=?,unit_template=?,parent_id=?,component_role=?,updated_at=datetime('now') WHERE id=?",
        (
            merged["name"],
            merged["type"],
            merged.get("repo_url") or "",
            merged.get("branch") or "main",
            # 写规范化后的路径：与 _validate 里重复检查用的值保持一致，
            # 否则检查用 /etc、入库存 /root/www/a/../../etc，下一次比对又错开。
            _app_path(merged),
            merged.get("port"),
            merged.get("domain") or None,
            merged.get("install_cmd") or None,
            merged["start_cmd"],
            json.dumps(vars_),
            merged.get("unit_override") or None,
            merged.get("db_names") or None,
            merged.get("unit_template") or None,
            merged.get("parent_id") or None,
            str(merged.get("component_role") or "")[:16],
            app_id,
        ),
    )
    if cur["name"] != merged["name"]:
        # 改名在这一步已经进库了，后面两件事必须跟着做，而且要放在这里而不是
        # apply_edited：apply_edited 有 unit_override、域名冲突等多条提前返回的路径，
        # 少跑一次就留下按旧名字编的状态——路由那边照样报"已保存"。
        # systemd unit 与 nginx 站点也在改名范围内，但那部分由 apply_edited 负责。
        from . import alerts_service, backup_service

        alerts_service.rename_app(cur["name"], merged["name"])
        backup_service.rename_app(cur["name"], merged["name"])
    return get_app(app_id)


def _check_not_protected(path: str):
    """安装目录不能指向面板自己或它的要害目录。

    部署流程会对 path 执行 `git fetch` + `git reset --hard origin/<branch>`，
    purge 还会 rmtree 它。把某个应用的 path 填成面板仓库（通常就在 APP_ROOT 之下，
    如 /root/www/choyeon-panel）后点部署，等于让面板把自己的代码切走甚至删掉，
    随后 systemd 重启失败，整台机器失去管理入口。
    """
    real = clean_abs_path(path, "安装目录")
    protected = {
        "面板程序目录": str(config.BASE),
        "面板数据目录": str(config.DATA_DIR),
        "systemd 配置目录": str(config.UNIT_DIR),
        "nginx 配置目录": "/etc/nginx",
        "证书目录": "/etc/letsencrypt",
    }
    for label, p in protected.items():
        try:
            root = clean_abs_path(p, label)
        except RuntimeError:
            continue
        if real == root or real.startswith(root + "/"):
            raise RuntimeError(f"安装目录不能位于{label}（{root}）之内或与之相同")


def _append_note(out: dict, msg: str) -> None:
    """把提醒追加进 note，而不是覆盖已有的那条。

    note 原来是"最后一个写它的分支赢"：一次保存里"没设上自启"和"服务没重启成功"可以同时
    成立，只显示后一条会让人以为自启是好的。
    """
    out["note"] = f'{out["note"]}；{msg}' if out.get("note") else msg


async def _apply_external_unit(old: dict, new_app: dict, renamed: bool) -> dict:
    """纳管模式（外部 unit）下的同步：unit 本体一个字都不改，但环境变量必须真的落地。

    以前的写法是一句"unit 与环境变量文件不由面板写入"直接返回，等于把面板自己管辖的
    那一半也一起放弃了。.panel.env 写在应用目录里，跟 unit 归谁写没有关系。线上实测的
    后果就是"环境变量改了没反应"：界面上 ADMIN_PASSWORD 已经是新值，磁盘上那个
    .panel.env 根本不存在（面板从来没写过它），外部 unit 里也没有 EnvironmentFile 那行。

    所以这里做三件事：写 .panel.env、补 systemd drop-in（<unit>.d/panel-env.conf，
    不改别人的 unit 文件）、在服务本来就在跑时重启一次。仍然不做的：渲染 unit、
    写 nginx 站点——那是外部 unit 的边界，改动的确只进了记录，就照实说。
    """
    out = {"applied": False, "restarted": False, "note": ""}
    unit = unit_name(new_app)
    notes = []
    # 这些字段活在 unit 本体里（ExecStart / WorkingDirectory），面板不写外部 unit，
    # 所以它们确实"只改了记录"——必须和环境变量区分开说，否则一句"只改了记录"
    # 会把已经生效的环境变量也说成没生效。
    unit_scoped = [k for k in ("start_cmd", "port", "path") if old.get(k) != new_app.get(k)]
    if unit_scoped or renamed:
        who = "、".join({"start_cmd": "启动命令", "port": "端口", "path": "安装目录"}.get(k, k) for k in unit_scoped)
        notes.append(
            f"{who or '名字'}属于 unit 本体，面板不写外部 unit：这部分改动要在 {unit} 里同步后重启才生效"
        )
    try:
        env_state = write_env_file(new_app)
        link_state = await ensure_env_link(new_app)
    except Exception as e:  # noqa: BLE001
        notes.append(f"环境变量没能落地：{str(e)[:160]}")
        out["note"] = "；".join(notes)
        return out
    if env_state == "no-dir":
        notes.append(f"应用目录 {new_app['path']} 还不存在，环境变量会在下一次部署时写入")
    elif link_state == "invalid-unit":
        notes.append(f"{unit} 不是合法的 unit 名，面板没有写环境变量的挂载配置")
    elif link_state == "no-unit":
        notes.append(f"本机找不到 {unit}，面板只写了 .panel.env；unit 就位后重启该服务即可读到")
    elif env_state == "unchanged" and link_state == "unchanged":
        out["applied"] = True
        notes.append("环境变量没有变化，服务未重启")
    elif not await is_active(unit):
        out["applied"] = True
        notes.append(f"环境变量已写入并由 {unit} 加载；服务当前没有运行，改动在下一次启动时生效")
    else:
        try:
            await restart_and_verify(
                unit, new_app["port"], warn=lambda t: notes.append(t.removeprefix("!!> "))
            )
            out["applied"] = True
            out["restarted"] = True
        except Exception as e:  # noqa: BLE001
            notes.append(f"环境变量已写入，但 {unit} 没能重启成功：{str(e)[:160]}（值已落盘，下次启动生效）")
    out["note"] = "；".join(notes)
    return out


async def apply_edited(old: dict, new_app: dict) -> dict:
    """PATCH 之后把改动真正落到 systemd / .panel.env / nginx，服务在跑就顺手重启。

    旧写法只有"改名"才会重建 unit 与站点（原来的 cleanup_renamed），其余字段只进
    数据库：改了启动命令、端口、域名或环境变量，界面显示"已保存"，进程却还按老
    配置跑着，要生效只能再点一次「部署」——而部署连依赖都要重装，几分钟起步，
    用户既不敢点也等不到，于是"改了没反应"成了常态。

    返回 {"applied","restarted","note"}，让路由如实说这次到底生效了没有：外部
    unit（unit_override）的 unit 本体与 nginx 不归面板写，那部分照实说"只改了记录"，
    但环境变量属于应用目录，照样写、照样挂、照样重启（见 _apply_external_unit）。
    """
    out = {"applied": True, "restarted": False, "note": ""}
    if not old:
        return out
    renamed = old["name"] != new_app["name"]
    # 改名前没在跑的应用不该被改名"顺手启动"，所以先记下状态再拆
    was_active = await is_active(unit_name(old))
    # vhost 文件名跟着 name 变，所以"建新"与"拆旧"的先后决定站点断多久：
    # 先拆旧的，一旦新的 nginx -t 不过（域名填错、证书路径没了），旧文件已经没了，
    # 站点当场下线，界面却只剩一句"下次部署会补上"。只有新记录里确实有一份
    # 可接替的 vhost 时才把拆旧推迟到建好之后；其余情况必须当场拆，
    # 否则留下指向死端口 / 已改名应用的孤儿 server 块。
    will_apply_vhost = bool(
        not new_app.get("unit_override") and new_app.get("domain") and new_app.get("port")
    )
    if renamed:
        # 只拆不建是一半问题，只建不拆是另一半：旧 unit 带 Restart=always 会继续
        # 占着端口、旧站点还在把域名转过去，而面板记录里已经没有这个名字，
        # 界面上再也管不到这两个孤儿。
        await remove_unit(old)
        if not will_apply_vhost:
            await nginx_ops.remove_vhost(old["name"])
    if new_app.get("unit_override"):
        r = await _apply_external_unit(old, new_app, renamed)
        if not old.get("unit_override") and not renamed:
            # 名字没变就从"面板托管"切到"外部纳管"：panel-<name>.service 还留在 systemd 里
            # 带着旧 ExecStart 继续跑，而面板已经认为这个应用归外部 unit 了。删它等于停服务，
            # 不能在一次保存里顺手做，只能把残留态说清楚。
            _append_note(r, (
                f"已切为外部 unit 纳管，但面板此前写的 {unit_name(old)} 还在 systemd 里运行"
                "（确认外部 unit 已接管后，到「systemd 单元」页停用并删除它）"
            ))
        return r
    if old.get("unit_override"):
        # 从外部 unit 切回面板托管：unit 名换了（panel-<name>.service），旧 unit 的
        # drop-in 不会有人再读，留着就是指向同一份 .panel.env 的孤儿配置。
        await remove_env_link(old)
    try:
        write_env_file(new_app)
        await ensure_unit(new_app, warn=lambda t: _append_note(out, t.removeprefix("!!> ")))
        # 自定义 unit_template 里漏写 EnvironmentFile 时，面板写的 .panel.env 永远读不到，
        # 而界面照样报"已生效"。补 drop-in 是这一步的兜底，默认模板自带那行时它是空操作。
        if await ensure_env_link(new_app) == "written":
            _append_note(out, "该 unit 的配置里没有引用 .panel.env，面板已用 drop-in 补上（不改 unit 本体）")
        if new_app.get("domain") and new_app.get("port"):
            await nginx_ops.apply_vhost(
                {"name": new_app["name"], "domain": new_app["domain"], "port": new_app["port"]}
            )
            if renamed:
                try:
                    await nginx_ops.remove_vhost(old["name"])
                except Exception as e:  # noqa: BLE001
                    # 新配置已经在服务了，旧文件删不掉是"多一份残留"，不是"没生效"，
                    # 所以这里只追加说明，不把整次保存判成失败。
                    _append_note(out, f"新的反向代理已生效，但旧配置文件没能删除：{str(e)[:120]}")
        elif old.get("domain") and not renamed:
            # 域名被清空、或只剩域名没有端口：留着那个 server 块就是一个指向死端口的 502
            await nginx_ops.remove_vhost(new_app["name"])
    except nginx_ops.DomainOwnedElsewhere as e:
        out["applied"] = False
        _append_note(out, f"配置已保存，但该域名的反向代理仍由别处的文件托管，面板没有覆盖：{str(e)[:180]}")
        return out
    except Exception as e:  # noqa: BLE001
        out["applied"] = False
        _append_note(out, f"配置已保存，但没能同步到 systemd/nginx：{str(e)[:160]}（下次部署会补上）")
        return out
    # daemon-reload 只改"下次启动用什么"，跑着的进程还是旧的 ExecStart，
    # 所以在服务本来就在跑的时候必须重启一次，否则这次编辑依旧不生效。
    if was_active and renamed or await is_active(unit_name(new_app)):
        try:
            await restart_and_verify(unit_name(new_app), new_app["port"])
            out["restarted"] = True
        except Exception as e:  # noqa: BLE001
            _append_note(out, f"配置已写入，但服务没能重启成功：{str(e)[:160]}")
    else:
        # 应用本来就没在跑（包括"上一次保存写了坏命令、服务已经崩掉"这种）：
        # 不擅自启动，但必须说清这次改动要等下一次启动或部署才生效，
        # 否则界面只剩一句"已保存"，用户会以为新配置已经在跑了。
        _append_note(out, "配置已写入，但服务当前没有运行，改动在下一次启动或部署时生效")
    return out


def _check_purge_target(path: str) -> str:
    """只做校验、不删，返回规范化后的目标目录。

    把"这个目录能不能删"的判断提前到拆除动作之前：unit 与 nginx 都拆了、
    记录也删了才发现目录不能删，就留下一个面板上看不见的孤儿目录。
    """
    try:
        real = clean_abs_path(path, "应用目录")
    except RuntimeError as e:
        raise RuntimeError(f"拒绝删除应用目录：{e}") from None
    root = clean_abs_path(APP_ROOT, "APP_ROOT")
    if real == root or not real.startswith(root + "/"):
        raise RuntimeError(f"拒绝删除 {real}：不在应用根目录 {root}/ 之下")
    return real


def _purge_app_dir(path: str):
    """删除应用目录，只在"确属 APP_ROOT 之下"时动手，失败必须抛错。

    两个都已实测的问题：
    1. `path.startswith(APP_ROOT)` 是纯字符串前缀，APP_ROOT=/root/www 时
       /root/www-other、/root/www2、/root/wwwbackups 全部判定为"在里面"，
       purge 会把邻居目录整个删掉（构造目录验证过，确实被 rmtree 删除）。
    2. `shutil.rmtree(..., ignore_errors=True)` 会把失败整段吞掉：
       目录还在，但应用记录已经删了，面板上再也看不到它，成了没人管的孤儿目录。
    历史数据里可能有带 .. 的 path，规范化失败时直接拒绝而不是猜测。
    """
    real = _check_purge_target(path)
    if not os.path.exists(real):
        return
    try:
        shutil.rmtree(real)
    except OSError as e:
        raise RuntimeError(f"删除应用目录失败：{e}") from e


async def delete_app(app_id: int, purge: bool, actor: str = "system"):
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "删除")
    guard = locks.acquire(locks.key_app(app_id), "删除", actor)
    try:
        # purge 的目标先判定，避免 unit 与 nginx 都拆了才发现目录不能删、记录却已删掉
        if purge:
            _check_purge_target(app["path"])
        await remove_unit(app)
        # drop-in 是面板写进 /etc/systemd/system 的唯一"非 unit 本体"文件，必须跟着删：
        # 外部 unit 的 unit 本体不归面板管、删掉记录后照样在跑，但它那个 <unit>.d 里
        # 留着的面板配置会在应用目录被 purge 之后继续指向一个不存在的 .panel.env。
        await remove_env_link(app)
        if app.get("domain"):
            await nginx_ops.remove_vhost(app["name"])
        dbm.execute("DELETE FROM apps WHERE id=?", (app_id,))
        # 部署记录只属于这个应用，删掉应用后没有任何入口能读到它们（含完整日志文本）。
        dbm.execute("DELETE FROM deployments WHERE app_id=?", (app_id,))
        # 告警标记的键名里带应用名，不跟着删就挂在一个不存在的对象上，
        # 而且同名新建的应用会直接继承（见 alerts_service.forget_app）。
        from . import alerts_service

        alerts_service.forget_app(app["name"])
        if purge:
            _purge_app_dir(app["path"])
    finally:
        guard.release()


async def app_action(app_id: int, verb: str, actor: str = "system") -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if verb == "rollback":
        return await rollback_app(app_id, actor)
    # 部署流程自己会在结束时 restart；这时用户再点一次停止/重启，
    # 两边抢同一个服务，出现"部署日志说成功、服务实际没起来"的现场。
    _ensure_no_deployment(app_id, f"{_VERB_CN.get(verb, verb)}服务")
    unit = unit_name(app)
    # 同 restart_and_verify：崩过一次的应用会被 systemd 的启动上限直接拒掉
    # （"Start request repeated too quickly"），用户分不清是自己命令写错还是面板坏了。
    if verb in ("start", "restart") and not await is_active(unit):
        await run("systemctl", ["reset-failed", "--", unit], timeout=30)
    await service_action(unit, verb)
    if verb in ("start", "restart"):
        # 只信 systemctl 的返回码会把"起来就崩"说成启动成功（见 restart_and_verify）。
        # 手动点「启动/重启」的正是等着看效果的用户，这里必须等到进程稳定存活。
        await _wait_stable(unit)
    # service_action 失败会抛异常，走到这里就代表命令确实执行成功了。
    # 旧实现把 is_active 的返回值同时当作 ok：stop 成功后服务不再是 active，
    # 于是对外报 ok:false（HTTP 200），而 AGENTS.md 的约定是失败走 4xx/5xx，
    # 成功响应里的 ok 必须为 true——否则 CLI/前端按字段判断会把正常停止当失败。
    return {"ok": True, "active": await systemd_ops.is_active(unit)}


def _append_log(dep_id: int, text: str):
    # 单一落库入口，凭据在这里挡掉：git 的命令行与 stderr 都可能回显带 token 的仓库地址
    dbm.append_deploy_log(dep_id, redact_creds(text))


# 重启后给进程留多少时间起立。不能只信 systemctl 的返回码：Type=simple +
# Restart=always 的单元"起来就崩"时 restart 照样返回 0，于是部署写着"完成"、
# 界面全绿，端口却空着、域名 502——用户只能靠访问失败反推部署其实没成功。
UNIT_START_GRACE_SEC = 15
# 判定"起来了"要在连续两次采样里看到同一个主进程活着。
UNIT_STABLE_SAMPLES = 2
# 采样间隔：生产上 1s 足够让 python/node 完成 bind；测试把它调小以免每个用例等两秒。
UNIT_SAMPLE_SEC = 1
# 刚重启完就查端口必然读到空值：进程已 fork 但还没 bind。给绑定的这几秒留出余量，
# 否则每次正常部署都配一句"域名反代可能 502"的假警报，真出错时反而没人信。
# 5 次实测不够：线上 Rosetta 后端是 `uvicorn --workers 2`，重启 5s 后 FastAPI 还在 import
# （数据库连接池、路由注册都在启动期），部署日志于是固定带一句假警报。窗口按"秒"算，
# 至少要盖住一个 python 应用的冷启动；端口真的对不上时多等 15s 也只影响这一次部署的尾巴。
PORT_WAIT_TRIES = 20


async def _live_pid(unit: str) -> int:
    """服务处于 active 且主进程确实还活着时返回它的 pid，否则 0。

    只看 `systemctl is-active` 不够：实测 Restart=always 的单元在崩溃自动重启的
    间隙里也会回 active，"起来就崩"的应用于是照样被判部署成功。
    """
    st = (await systemd_ops.unit_states([unit])).get(unit) or {}
    pid = int(st.get("mainPid") or 0)
    return pid if st.get("active") and pid > 0 and os.path.exists(f"/proc/{pid}") else 0


async def _wait_stable(unit: str) -> None:
    """等主进程连续 UNIT_STABLE_SAMPLES 次采样存活（且是同一个 pid），超时则抛错。

    只看 `systemctl is-active` 或 restart 的返回码都不够：实测 Restart=always 的单元
    在崩溃自动重启的间隙里也会回 active，"起来就崩"的应用于是照样被判部署成功——
    界面全绿、端口空着、域名 502，用户只能靠访问失败反推。
    要求"连续两次是同一个进程"是因为崩溃循环每 3 秒换一个新 pid。
    """
    deadline = time.monotonic() + UNIT_START_GRACE_SEC
    last, stable = 0, 0
    while time.monotonic() < deadline:
        await asyncio.sleep(UNIT_SAMPLE_SEC)
        pid = await _live_pid(unit)
        stable = stable + 1 if pid and pid == last else (1 if pid else 0)
        last = pid
        if stable >= UNIT_STABLE_SAMPLES:
            return
    raise RuntimeError(f"{unit} 在 {UNIT_START_GRACE_SEC}s 内主进程没能稳定存活，请看运行日志")


async def restart_and_verify(unit: str, port=None, *, warn=None) -> None:
    """重启并确认主进程稳定活着；端口对不上只作提醒，不判失败。

    端口是否监听没法当判据：worker 型应用本就不监听，而填错端口的代价只是 nginx
    502，把它做成硬失败会把一次正常上线说成失败。
    """
    # 崩到 systemd 的启动上限（默认 10s 内 5 次）之后，restart 会被直接拒绝：
    # "Start request repeated too quickly"。最容易撞上它的正是"把坏启动命令改对了再保存"
    # 这一次——配置已经写对了，服务却起不来，用户看不出两者没关系。
    # 既然这次调用就是要主动把服务拉起来，先把失败计数清掉（对正常停止的服务是空操作）。
    if not await is_active(unit):
        await run("systemctl", ["reset-failed", "--", unit], timeout=30)
    await service_action(unit, "restart")
    await _wait_stable(unit)

    if port and warn:
        listening = None
        for attempt in range(PORT_WAIT_TRIES):
            listening = await detect_port(unit, force=attempt > 0)
            if listening == port:
                return
            await asyncio.sleep(UNIT_SAMPLE_SEC)
        if listening is None:
            warn(
                f"!!> 重启后 {PORT_WAIT_TRIES}s 内没探到 {unit} 监听任何端口"
                f"（面板记录的是 {port}）：应用还在初始化的话这条提醒会自己消失，"
                "端口填错则域名反代可能 502"
            )
        else:
            warn(f"!!> {unit} 监听在 {listening}，与面板记录的 {port} 不一致，域名反代可能 502")


async def deploy_app(app_id: int, actor: str = "system") -> int:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError("该应用已有部署任务在运行")
    # 锁在 INSERT 之前拿，且这里到 INSERT 之间没有任何 await：两个入口（网页 + CLI）
    # 同时点部署时，先抢到锁的那个才写得出 running 行，后一个当场拿到"谁在占用"的文案。
    # 锁交给后台任务释放——部署本身要跑几十分钟，中途释放就等于把守卫撤了。
    guard = locks.acquire(locks.key_app(app_id), "部署", actor)
    dep_id = None
    try:
        dep_id = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
        _RUNNING_DEPS.add(dep_id)
        task = asyncio.create_task(_run_deployment(app, dep_id, guard))
        _DEPLOY_TASKS.add(task)

        def _done(_t):
            _DEPLOY_TASKS.discard(_t)
            _RUNNING_DEPS.discard(dep_id)

        task.add_done_callback(_done)
    except Exception:
        # 任务没起来（事件循环正在关、INSERT 抛错）就必须在这里放锁：
        # 否则那把锁跟着一个永远不会结束的"进行中部署"把应用钉死。
        guard.release()
        if dep_id is not None:
            # 记录也要当场收尾：这一行对应的任务从未被创建，等回收器等于让删除/回滚/
            # 改配置被一条假 running 挡满 DEPLOY_BUDGET_SEC。
            _RUNNING_DEPS.discard(dep_id)
            _append_log(dep_id, "!!! 部署未能启动：后台任务没建起来，本次记录直接记为失败")
            dbm.execute(
                "UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE id=? AND status='running'",
                (dep_id,),
            )
        raise
    return dep_id


async def wait_deployment(dep_id: int, app_id: int, timeout: int = DEPLOY_BUDGET_SEC) -> dict:
    """等一次部署真正结束。

    CLI 与脚本都在自己的事件循环里调用 deploy_app；`asyncio.run` 退出时会取消
    尚未跑完的后台任务。等待时间必须覆盖 _run_deployment 的最坏路径（见
    DEPLOY_BUDGET_SEC 的说明），否则调用方先放弃，任务被取消，记录停在 running，
    而 _ensure_no_deployment 会让这个应用此后无法删除/回滚/改配置。
    超时不再抛错后无人收摊：CancelledError 会让记录落到 failed，
    进程被 kill 的残留则由 reap_orphan_deployments 回收。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = dbm.query_one("SELECT status FROM deployments WHERE id=?", (dep_id,))
        if row and row["status"] != "running":
            return row
        await asyncio.sleep(1)
    raise RuntimeError(
        f"部署 #{dep_id} 超过 {timeout}s 仍未结束（应用 #{app_id}）。"
        f"若面板进程仍在运行，可用 app logs --id {app_id} 继续观察；"
        "面板会把它回收为失败，届时即可重新部署或删除。"
    )


def reap_orphan_deployments() -> list[int]:
    """把没有后台任务在跑、却又停在 running 的部署记录判为失败。

    两种成因：面板被 kill/OOM/升级重启（进程没了，任务也没了），
    以及旧版 CLI 提前退出把任务取消。main.py 只在进程启动时清一次，
    所以"启动后不久被 kill、之后一直没重启"的实例里，这个应用会永久无法
    删除/回滚/改配置——_ensure_no_deployment 拦住所有动作，用户只能等下一次重启。
    判定条件用 started_at 超过 DEPLOY_BUDGET_SEC，且 id 不在本进程的
    _RUNNING_DEPS 里：前者排除还在正常跑的慢部署，后者排除本进程正在跑的任务，
    两者都满足才认为它已经是孤儿。
    """
    rows = dbm.query(
        "SELECT id,app_id FROM deployments WHERE status='running'"
        " AND started_at < datetime('now', ?)",
        (f"-{DEPLOY_BUDGET_SEC} seconds",),
    )
    reaped: list[int] = []
    for row in rows:
        if row["id"] in _RUNNING_DEPS:
            continue
        _append_log(row["id"], "!!! 部署已中止：超过最坏耗时上限且面板内没有对应任务，回收为失败")
        dbm.execute(
            "UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE id=? AND status='running'",
            (row["id"],),
        )
        reaped.append(row["id"])
    return reaped


async def cancel_inflight_deployments() -> None:
    """进程退出前取消在跑的部署任务，让记录当场落到 failed。

    reap_orphan_deployments 的判定要求 started_at 已超过 DEPLOY_BUDGET_SEC，
    所以「部署中被 systemctl restart」这一类只靠回收器要再等 45 分钟才解锁
    删除/回滚/改配置。这里在 lifespan shutdown 阶段主动取消：CancelledError
    分支会写日志并把状态收尾，gather 保证这条 UPDATE 在事件循环关闭前跑完。
    """
    tasks = [t for t in _DEPLOY_TASKS if not t.done()]
    if not tasks:
        return
    for t in tasks:
        t.cancel()
    with suppress(Exception):
        await asyncio.gather(*tasks, return_exceptions=True)


async def resolve_repo_dir(path: str) -> str | None:
    """应用目录所在 Git 工作树的根；目录不在任何仓库里时返回 None。

    monorepo 的组件目录（`<仓库>/frontend`）自己没有 `.git`。凡是按 `app.path` 是否
    含 `.git` 来判"有没有仓库"的地方都会把这种应用判错，线上后果各不相同：部署会转去
    `git clone` 到已存在且非空的目录（git 直接拒绝，组件永远无法通过面板部署）、回滚在
    入口守卫就报"应用目录不是 Git 仓库"、而同步靠另一份内联实现才恰好是对的。
    """
    if os.path.isdir(f"{path}/.git"):
        return path
    top = await run("git", ["-C", path, "rev-parse", "--show-toplevel"], cwd=None, timeout=30)
    lines = (top["out"] or "").strip().splitlines()
    cand = lines[-1] if lines else ""
    return cand if cand and os.path.isdir(f"{cand}/.git") else None


async def _current_sha(path: str) -> str | None:
    r = await run("git", ["rev-parse", "--short", "HEAD"], cwd=path, timeout=30)
    return r["out"] if r["code"] == 0 else None


async def _commit_present(path: str, sha: str) -> bool:
    """部署记录里这个 commit 还在不在仓库里。

    浅克隆（`clone --depth 1`）与应用仓库被 force-push 后 gc 掉的提交，都不在对象库里；
    拿它当回滚目标只会得到 `git reset --hard` 的 "bad object"，而且每次都挡在同一个位置，
    用户既退不回更早的版本，也看不懂为什么。
    """
    r = await run("git", ["cat-file", "-e", sha + "^{commit}"], cwd=path, timeout=30)
    return r["code"] == 0


async def _is_newer_than(path: str, current: str | None, sha: str) -> bool | None:
    """sha 是不是 current 的后代（比当前更新的版本）。git 判不出时回 None。

    实测（2.47）：`merge-base --is-ancestor A B` 返回 0=A 是 B 的祖先、1=不是、128=对象不存在。
    """
    if not current:
        return None
    r = await run("git", ["merge-base", "--is-ancestor", current, sha], cwd=path, timeout=30)
    if r["code"] == 0:
        return True
    if r["code"] == 1:
        return False
    return None


async def rollback_app(app_id: int, actor: str = "system") -> dict:
    """回滚到**上一个**成功部署的版本，并重启服务。

    旧实现取"最近一条成功记录"的 commit，而部署成功时记下的正是当前正在运行的
    commit，于是 `git reset --hard <当前 commit>` 变成空操作——用户点了"回滚"却什么都没变
    （只有"最近一次部署失败"这种场景才恰好正确）。
    改成"以工作区实际 commit 为基准，往前找第一个不同的成功版本"之后，还剩一半没解决：
    只看"不同"不看"先后"，**连着点两次回滚会跳到最新版本去**。线上实测（批次⑤N，
    部署 v1 → 部署 v2 → 同步 v3 → 回滚 → 再回滚）：第一次正确退回 v2，第二次返回
    `{"commit": v3}` 并把服务切回 v3——用户想继续退，面板却把他推到最新发布版。
    所以候选要过三道：不是当前版本、commit 还在仓库里、不是当前的后代（即真的更早）。
    git 判不出先后时（对象缺失之外的错误）保留旧口径当候选，宁可多退一步也不要让回滚失效。
    """
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "回滚")
    # 回滚此前不在**任何**互斥里：它只读 deployments 的 running 行，自己一行都不写，
    # 于是点完回滚再点部署/同步/删除/恢复，两条 `git reset --hard` 与两次服务重启
    # 叠在同一个工作区上（连点两次回滚同样是两份并发 reset）。锁按 app id 分，
    # 与 deploy/sync/delete/restore 用的是同一把（见 app/locks.py）。
    guard = locks.acquire(locks.key_app(app_id), "回滚", actor)
    try:
        return await _rollback_body(app, app_id)
    finally:
        guard.release()


async def _rollback_body(app: dict, app_id: int) -> dict:
    repo_dir = await resolve_repo_dir(app["path"])
    if not repo_dir:
        raise RuntimeError("应用目录不在任何 Git 仓库里，无法回滚")
    rows = dbm.query(
        "SELECT id,commit_sha FROM deployments WHERE app_id=? AND status='success' AND commit_sha IS NOT NULL"
        " ORDER BY id DESC LIMIT 20",
        (app_id,),
    )
    if not rows:
        raise RuntimeError("没有可回滚的成功部署记录")
    current = await _current_sha(repo_dir)
    target = None
    seen: set[str] = set()
    for row in rows:
        sha = row["commit_sha"]
        if not sha or sha in seen:
            continue
        seen.add(sha)
        if sha == current:
            continue
        if not await _commit_present(repo_dir, sha):
            continue
        if await _is_newer_than(repo_dir, current, sha) is True:
            continue
        target = row
        break
    if not target:
        if seen == {current}:
            raise RuntimeError("没有更早的成功版本可回滚（当前已是最近一次成功部署）")
        raise RuntimeError(
            "没有可回滚的版本：部署记录里的 commit 除当前版本外，要么已不在仓库中（浅克隆/历史被改写），"
            "要么比当前更新"
        )
    r = await run("git", ["reset", "--hard", target["commit_sha"]], cwd=repo_dir, timeout=120)
    if r["code"] != 0:
        raise RuntimeError(f"回滚失败：{r['out']}")
    try:
        await restart_and_verify(unit_name(app), app["port"])
    except RuntimeError as e:
        # 工作区已经切到目标版本了，只说"回滚失败"会让人以为再点一次就好，
        # 实际要处理的是那个起不来的进程。
        raise RuntimeError(f"代码已回滚到 {target['commit_sha']}，但服务没起来：{e}") from None
    return {"ok": True, "commit": target["commit_sha"], "deployment": target["id"], "from": current}


async def sync_app(app_id: int, actor: str = "system") -> dict:
    """拉取远端最新代码并重启服务——比部署短的路径：不装依赖、不写 env/unit/nginx。

    代码改完想立刻上线时，走 deploy 要等 npm/pip 几分钟，sync 只有 fetch/reset/restart
    三步，通常在几秒内完成。仍然落一条 deployments 记录（status=success + commit_sha），
    这样同步上去的版本也能被「回滚」选中，不会出现"同步完发现坏了、回滚列表里找不到上一版"。

    锁必须拿在**第一个 await 之前**：旧写法在 `_ensure_no_deployment` 与 INSERT 之间
    await 了 resolve_repo_dir，于是连点两次「一键同步」时两个调用都过了守卫、各自插一条
    running 行，然后两份 fetch/`reset --hard` 打在同一个工作区上，HEAD 互相拧——面板报
    「同步完成」而盘上是哪一份代码取决于谁后跑完（批次㉝ 的竞态）。
    """
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "同步")
    if not app["repo_url"]:
        raise RuntimeError("应用未配置 Git 仓库地址，无法同步")
    if not is_git_url(app["repo_url"]):
        raise RuntimeError("Git 仓库地址不合法")
    branch = app["branch"] or "main"
    if not is_branch(branch):
        raise RuntimeError("分支名不合法")
    guard = locks.acquire(locks.key_app(app_id), "同步", actor)
    try:
        return await _sync_body(app, app_id, branch)
    finally:
        guard.release()


async def _sync_body(app: dict, app_id: int, branch: str) -> dict:
    path = app["path"]
    # monorepo 的组件目录没有 .git，仓库根才是能 fetch/reset 的地方（见 resolve_repo_dir）。
    repo_dir = await resolve_repo_dir(path) or path
    if not os.path.isdir(f"{repo_dir}/.git"):
        raise RuntimeError(f"{path} 下没有 Git 仓库，请先执行一次完整部署")

    dep_id = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
    before = await _current_sha(repo_dir)
    out: list[str] = []

    def log(text: str):
        out.append(text)
        _append_log(dep_id, text)

    try:
        if repo_dir != path:
            log(f"==> 仓库根 {repo_dir}（应用目录是其子目录，本次会更新整个仓库）")
        log(f"==> git fetch origin {branch}")
        r = await run("git", ["fetch", "origin", branch], cwd=repo_dir, timeout=300)
        log(redact_creds(r["out"]))
        if r["code"] != 0:
            raise RuntimeError(f"拉取远端失败（exit {r['code']}）")

        r = await run("git", ["checkout", branch], cwd=repo_dir, timeout=120)
        if r["code"] != 0:
            # 分支可能只存在于刚拉取的远端（本地从未 checkout 过）：
            # checkout 失败先试一次跟踪分支创建，仍失败才是真问题。
            log(redact_creds(r["out"]))
            r = await run("git", ["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, timeout=120)
            if r["code"] != 0:
                raise RuntimeError(f"切换分支失败（exit {r['code']}）")
        log(f"==> git reset --hard origin/{branch}")
        st = await run("git", ["status", "--porcelain"], cwd=repo_dir, timeout=60)
        if st["code"] == 0 and st["out"].strip():
            # reset --hard 只会丢已跟踪文件的本地改动，未跟踪目录（构建备份、缓存）不受影响，
            # 所以警告只统计跟踪改动，别把一堆 "??" 说成"要被覆盖"吓到人。
            dirty = [ln for ln in st["out"].splitlines() if ln.strip() and not ln.startswith("??")]
            untracked = [ln for ln in st["out"].splitlines() if ln.startswith("??")]
            if dirty:
                log(f"!!! 工作区有 {len(dirty)} 处未提交改动，将被覆盖：{'; '.join(dirty[:6])}")
            if untracked:
                log(f"==> 另有 {len(untracked)} 个未跟踪路径（不受本次同步影响）")
        r = await run("git", ["reset", "--hard", f"origin/{branch}"], cwd=repo_dir, timeout=120)
        log(redact_creds(r["out"]))
        if r["code"] != 0:
            raise RuntimeError(f"更新工作区失败（exit {r['code']}）")

        after = await _current_sha(repo_dir)
        log(f"==> 当前 commit {after or '未知'}" + (f"（同步前 {before}）" if before else ""))
        if before and after and before != after:
            log(f"==> 版本变化 {before} -> {after}")
        elif before and after == before:
            log("==> 远端无新提交，工作区版本未变化")

        log(f"==> 重启并确认服务 {unit_name(app)}")
        await restart_and_verify(unit_name(app), app["port"], warn=log)
        dbm.execute(
            "UPDATE deployments SET status='success', commit_sha=?, finished_at=datetime('now') WHERE id=?",
            (after, dep_id),
        )
        dbm.execute("UPDATE apps SET updated_at=datetime('now') WHERE id=?", (app_id,))
        log("==> 同步完成")
        return {
            "ok": True, "commit": after, "from": before,
            "changed": bool(before and after and before != after),
            "deployment": dep_id, "out": "\n".join(out),
        }
    except Exception as e:  # noqa: BLE001
        # 服务没起来却把记录留在 running，前端会一直显示"部署中"，
        # 且 _ensure_no_deployment 会让这个应用此后无法再同步/回滚。
        dbm.execute("UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE id=?", (dep_id,))
        log(f"!!! 同步失败: {e}")
        raise


async def _run_deployment(app: dict, dep_id: int, guard=None):
    """一次部署的正文。`guard` 是 deploy_app 抢到的 per-app 文件锁，收尾时必须在这里释放
    （任何一条出口——成功、失败、被取消——都不许把它留在手里，否则这个应用被一把
    已经没有主人的锁钉住，恢复/回滚/删除全部拒绝，直到面板重启）。"""
    try:
        await _deployment_body(app, dep_id)
    finally:
        if guard is not None:
            guard.release()


async def _deployment_body(app: dict, dep_id: int):
    def finish(ok: bool):
        dbm.execute(
            "UPDATE deployments SET status=?, finished_at=datetime('now') WHERE id=?",
            ("success" if ok else "failed", dep_id),
        )
        dbm.execute("UPDATE apps SET updated_at=datetime('now') WHERE id=?", (app["id"],))

    # 动工作区之前先记下当前 commit，失败时才能说清"盘上的代码和服务跑的代码是不是两份"
    # monorepo 的组件目录（`<仓库>/frontend`）自己没有 .git：仓库根才是要 fetch/reset 的地方，
    # 也是唯一取到 commit 的地方（见 resolve_repo_dir）。按 app.path 判仓库会把组件判成
    # "没有仓库"，线上后果是部署转去 clone 已存在且非空的目录、部署记录永远不写 commit_sha
    # （回滚因此查不到候选），失败时的 before/after 也全为空——那条"盘上已是新代码、服务还在
    # 跑旧代码"的提示对组件永远不触发（见 test_batch7a）。
    # 这两次 git 调用必须在 try 里面：放在 try 之前时，任务在这几步被取消会绕过下面
    # except CancelledError 的收尾，deployment 行留在 running，直到看门狗回收才解锁
    # 这个应用的删除/回滚/改配置（批次⑤G 修的就是这个）。
    repo_dir = None
    before = None
    # 失败收尾时判断"这个目录能不能删"的两个状态。初值取不许删那一侧：
    # 在真值还没取到之前就出事，删掉别人工作区的代价远大于留一份半份代码。
    path_existed = True
    pulled = True
    try:
        # 本次部署是不是这个目录的创建者（见 _discard_partial_checkout）。
        # 必须在任何写盘动作之前取，clone 之后它就变成 True 了。
        path_existed = os.path.isdir(app["path"])
        repo_dir = await resolve_repo_dir(app["path"])
        before = await _current_sha(repo_dir) if repo_dir else None
        pulled = False  # 从现在起，这个目录里的内容可能只有这次没拉完的一半
        if app["repo_url"]:
            if repo_dir:
                if repo_dir != app["path"]:
                    _append_log(dep_id, f"==> 仓库根 {repo_dir}（应用目录是其子目录，本次会更新整个仓库）")
                # 已在盘的仓库走增量 fetch，写入量同样由上游决定（一次 rebase 能把对象撑大几倍）。
                # 这一处也要问：拒在这里比拒在安装依赖更干净——一个字节都还没落，工作区和
                # 跑着的服务都还是上一版，用户看到的失败没有"盘上已是新代码"这个尾巴。
                _assert_disk_room(repo_dir, "拉取这个仓库的更新")
                _append_log(dep_id, f"==> git fetch/checkout {app['branch']}")
                r = await run("git", ["fetch", "origin", app["branch"]], cwd=repo_dir, timeout=GIT_FETCH_T)
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run("git", ["checkout", app["branch"]], cwd=repo_dir, timeout=GIT_CHECKOUT_T)
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run(
                    "git", ["reset", "--hard", f"origin/{app['branch']}"], cwd=repo_dir, timeout=GIT_RESET_T
                )
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
            else:
                # clone 之前目标目录通常还不存在，所以问分区要往上退到最近的真目录。
                _assert_disk_room(_budget_probe_dir(app["path"]), "克隆这个仓库")
                _append_log(dep_id, f"==> git clone -b {app['branch']} {app['repo_url']} -> {app['path']}")
                r = await run(
                    "git",
                    ["clone", "--depth", "1", "-b", app["branch"], app["repo_url"], app["path"]],
                    timeout=GIT_CLONE_T,
                )
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
        else:
            _append_log(dep_id, f"==> 跳过拉取（未配置仓库，使用现有目录 {app['path']}）")
        if not os.path.exists(app["path"]):
            raise RuntimeError(f"目录不存在: {app['path']}")
        pulled = True  # 到这里代码是完整的一份（或这一趟根本没拉取），盘上的目录已经值得留下

        # 装依赖是这一趟里真正能写几 GB 的一步（node_modules / venv），量级仍由外部决定：
        # package.json 里有多少个包不由面板说了算。判不过就当场拒，此时工作区只是拉好了代码，
        # 目录里没有半份依赖，下一次改完盘重跑是干净的。
        _budget = _assert_disk_room(app["path"], "安装依赖")
        _append_log(
            dep_id,
            f"==> 安装依赖 ({app['type']})（分区按警戒线 {_budget['line']}% 还能写 {human_bytes(_budget['room'])}）",
        )
        default_install = (
            "npm install"
            if app["type"] == "node"
            else "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        )
        install = app["install_cmd"] or default_install
        ir = await run("/bin/bash", ["-lc", install], cwd=app["path"], timeout=INSTALL_T)
        _append_log(dep_id, ir["out"] or "(no output)")
        if ir["code"] != 0:
            raise RuntimeError(f"安装依赖失败 (exit {ir['code']})")

        _append_log(dep_id, "==> 写入环境变量与 systemd unit")
        write_env_file(app)
        await ensure_unit(app, warn=lambda t: _append_log(dep_id, t))
        # 部署是环境变量的另一个入口：外部 unit / 自定义模板没写 EnvironmentFile 时，
        # 这里不补 drop-in，写好的 .panel.env 一样没人读，而部署日志显示"完成"。
        if await ensure_env_link(app) == "written":
            _append_log(dep_id, "!!> 该 unit 的配置里没有引用 .panel.env，面板已用 drop-in 补上（不改 unit 本体）")

        if app["domain"]:
            if app["port"]:
                _append_log(dep_id, f"==> 应用 nginx 反代 {app['domain']} -> 127.0.0.1:{app['port']}")
                try:
                    await nginx_ops.apply_vhost({"name": app["name"], "domain": app["domain"], "port": app["port"]})
                except nginx_ops.DomainOwnedElsewhere as e:
                    # 域名已经写在别处（比如手工维护的 sites-enabled 配置）：不覆盖，
                    # 也不为了这件"面板不托管"的事把跑了几分钟的部署判成失败。
                    # 但必须留下这一行——否则用户看到"部署完成"，域名行为却和面板无关。
                    _append_log(dep_id, f"!!> {e}")
            else:
                # 反代必须知道往哪个端口转，缺端口就只能不配；不写这一行的话
                # 用户只看到"部署完成"，域名却始终打不开，无从猜起是证书还是应用的问题。
                _append_log(dep_id, f"!!> 已配置域名 {app['domain']} 但未填端口，跳过 nginx 反代（补上端口后重新部署）")

        _append_log(dep_id, f"==> 重启并确认服务 {unit_name(app)}")
        await restart_and_verify(unit_name(app), app["port"], warn=lambda t: _append_log(dep_id, t))
        sha = await _current_sha(repo_dir) if repo_dir else None
        if sha:
            dbm.execute("UPDATE deployments SET commit_sha=? WHERE id=?", (sha, dep_id))
            _append_log(dep_id, f"==> 当前 commit {sha}（可在部署记录中回滚到此版本）")
        _append_log(dep_id, "==> 部署完成")
        finish(True)
    except asyncio.CancelledError:
        # CancelledError 继承 BaseException，下面的 except Exception 抓不到它。
        # 不接住时任务被取消（CLI 提前退出、面板优雅关闭）就什么都不写，
        # 记录永远停在 running，而 _ensure_no_deployment 会拦住这个应用的
        # 删除/回滚/改配置——只能等面板重启才自愈。
        # 这里尽力落终态；进程正在退出、写不动时也照样把取消传播出去，
        # 剩下的交给 reap_orphan_deployments 和启动自愈收摊。
        with suppress(Exception):
            _append_log(dep_id, "!!! 部署任务被取消（调用方退出或面板关闭）")
            finish(False)
            _discard_partial_checkout(app["path"], dep_id, path_existed or pulled)
        raise
    except Exception as e:  # noqa: BLE001
        _append_log(dep_id, f"!!! 部署失败: {e}")
        # 实测：git fetch/reset 之后安装依赖失败（exit 7）时，
        # 工作区已经变成新代码（CODE=v2），而服务进程还在跑旧代码，
        # 面板只写"部署失败"，看现场的人无法判断现在盘上到底是哪一份。
        # 这里不自动 reset 回去——用户可能正要在新代码上排查，静默改工作区更糟；
        # 只把差异写进日志，并指出可用的回滚目标。
        after = await _current_sha(repo_dir) if repo_dir else None
        if before and after and before != after:
            _append_log(
                dep_id,
                f"!!! 工作区已从 {before} 切到 {after}，但服务未被重启，仍在运行旧代码",
            )
            _append_log(dep_id, "    如需回到部署前的版本，用「回滚」或 choyeonctl app rollback --id 部署记录")
        finish(False)
        # 半份 clone 留在盘上只会把下一次部署变成一句看不出根因的"目录已存在且非空"。
        # 放在最后一行：上面那段 before/after 判词依赖 repo_dir（目录事先存在才有），
        # 而那正是这里不许删的情况，两者不会互相把对方的现场抹掉。
        _discard_partial_checkout(app["path"], dep_id, path_existed or pulled)


async def attach_ssl(app_id: int, email: str | None = None) -> str:
    app = get_app(app_id)
    if not app or not app["domain"]:
        raise RuntimeError("应用未配置域名")
    if app["port"] is None:
        raise RuntimeError("未填写端口且服务未运行，无法反代；请先在应用配置里填端口")
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
    _ensure_no_deployment(app_id, "修改 unit")
    # 补一次换行得到 normalized，文件和库里都写它。
    # 旧代码文件写 normalized、库写 content + "\n"，末尾本来就有换行时库存下两个换行；
    # 前端读回的 template 又带这个多余空行，用户原样再保存一次就累积成三个，
    # 部署时 render_unit 用的正是这份 unit_template，于是每部署一次现场就更难对齐。
    normalized = content if content.endswith("\n") else content + "\n"
    unit = unit_name(app)
    path = f"{UNIT_DIR}/{unit}"
    old = Path(path).read_text(errors="replace") if os.path.exists(path) else None

    def _restore():
        """失败回滚：原来没有这个文件就把刚写进去的删掉。

        旧实现只在 old is not None 时恢复，首次保存（或 unit 文件已被删）失败后，
        这份没通过 verify 的 unit 会留在 UNIT_DIR 里——当下没 daemon-reload 所以还没加载，
        可之后任何一次 daemon-reload（别的应用部署、面板重启）都会把它读进去。
        """
        with suppress(Exception):
            if old is None:
                os.unlink(path)
            else:
                write_secret_atomic(Path(path), old)

    write_secret_atomic(Path(path), normalized)
    verify = await run("systemd-analyze", ["verify", path], timeout=30)
    if verify["code"] != 0:
        _restore()
        raise RuntimeError(f"systemd-analyze verify 失败，已回滚：\n{verify['out']}")
    reload = await run("systemctl", ["daemon-reload"])
    if reload["code"] != 0:
        _restore()
        await run("systemctl", ["daemon-reload"])
        raise RuntimeError(f"daemon-reload 失败，已回滚：\n{reload['out']}")
    dbm.execute(
        "UPDATE apps SET unit_template=?,updated_at=datetime('now') WHERE id=?", (normalized, app_id)
    )
    return {"ok": True, "unit": unit, "restartRequired": True}


PROC_FIELDS = [
    "Id",
    "MainPID",
    "MemoryCurrent",
    "NRestarts",
    "ActiveEnterTimestamp",
    "CPUUsageNSec",
    "ExecMainStartTimestamp",
    "SubState",
    # 判词要一起取：只看 MainPID 会把"崩过一次、pid 还没清"说成"在运行"，
    # 而 failed / not-found / systemctl 查失败这三种情况对用户说的话完全不同。
    "ActiveState",
    "LoadState",
]


async def app_proc(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    unit = unit_name(app)
    args = ["show", "--", unit] + [f"-p{f}" for f in PROC_FIELDS]
    r = await run("systemctl", args, timeout=8)
    # 用批量解析的同一条路：逐行 find("=") 会让排在 Id 之前的属性串到上一个块里
    blocks = [b for b in systemd_ops.parse_show(r["out"]) if b.get("Id") == unit]
    got: dict[str, str] = blocks[0] if blocks else {}

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
    # 判词走 systemd_ops 那一份：MainPID>0 不代表在运行（failed 的 unit 会把死 pid 挂在上面
    # 等清理），而"这个 unit 本机没有"和"systemctl 查失败"都不是"进程没起来"。
    st = systemd_ops.derive_state(got) if got else dict(systemd_ops.UNKNOWN_STATE)
    return {
        "unit": unit,
        "subState": got.get("SubState") or "unknown",
        "state": st["state"],
        "unitMissing": st["missing"],
        "running": st["busy"],
        "pid": pid if st["busy"] else None,
        "memoryBytes": mem,
        "restarts": _int("NRestarts") or 0,
        "activeSince": _ts("ActiveEnterTimestamp"),
        "execStartAt": _ts("ExecMainStartTimestamp"),
        "cpuSeconds": round(cpu_ns / 1e6) / 1000 if cpu_ns is not None else None,
    }
