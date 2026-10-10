"""定时任务中心：把 5 段 crontab 表达式渲染成 systemd timer。

三条不可让渡的约束（沿用 backup_service 的 timer 渲染纪律）：

1. **命令只能来自白名单的固定 argv**。ExecStart 里出现 `sh -c` 等于把「定时任务」
   变成任意代码执行入口——表达式与参数都由网页提交，被注入后没人看得见，
   所以这里连 shell 元字符都不放进字符集，而不是「拼好之后再检查一遍」。
2. **解析失败就报错，不做宽容解析**。`@daily`、6/7 段、`mon`/`jan` 英文名如果
   「看起来像 cron 就收下」，渲染出来的 OnCalendar 会是个错误的时间，任务照跑且
   面板显示"已启用"——这种"跑了但跑错点"的表现比直接报错难查十倍。
3. **禁用与删除必须把 .service 与 .timer 两份文件都清掉并 daemon-reload**。
   残留的 timer 会继续按计划唤醒一个已经不存在的服务，且 `systemctl list-timers`
   里看得见却关不掉。
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import re
import shutil
import time
from pathlib import Path

from .. import aio_db
from .. import config
from .. import database as dbm
from ..errors import BadInput, NotFound
from ..util import kill_group, redact_creds, run, unit_env_value, unit_exec_arg_escape, unit_pct_escape
from .backup_service import _unit_dir, _write_if_changed

PANEL_SERVICE = "choyeon-panel"
RUN_TIMEOUT = 600
LOG_TAIL_BYTES = 4096
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
# 单个参数允许的字符集：字母数字与 _ @ : . / = + -。
# 刻意不含空格 ; | & ` $ ( ) < > " ' \ * ? # ! % { } [ ] ~ ^ —— 这些正是 shell 的
# 命令分隔、替换与通配符；字符集先挡住，ExecStart 与 create_subprocess_exec 两侧才都安全。
ARG_RE = re.compile(r"^[A-Za-z0-9_@:./=+-]{1,128}$")
MAX_ARGS = 12
MAX_ARGV_CHARS = 600

# 字段顺序即 crontab 的 5 段。dow 上界写成 7：Vixie cron 里 7 与 0 同指周日，
# 收下后统一归一到 0（渲染 systemd 时再按 Sun..Sat 映射）。
FIELDS = (
    ("minute", 0, 59, "分钟"),
    ("hour", 0, 23, "小时"),
    ("dom", 1, 31, "日期"),
    ("mon", 1, 12, "月份"),
    ("dow", 0, 7, "星期"),
)
DOW_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
DOW_CN = ("日", "一", "二", "三", "四", "五", "六")
# 每一项形如 * | */n | a | a-b | a-b/n | a/n（省略步长按 1）
_TOKEN_RE = re.compile(r"^(\*|\d{1,2}(?:-\d{1,2})?)(?:/(\d{1,2}))?$")

# 允许被定时的二进制：只放本仓库的 CLI / 部署文档里真实存在、且不需要人盯着执行的命令。
# 依据（读过 bin/choyeonctl → backend/cli.py、meta.py 与 AGENTS.md）：
# - choyeonctl：面板自己的命令契约（backup run / doctor / status / app deploy…），
#   参数不接受路径或口令，是文档推荐的入口，出事也只在面板能力范围内。
# - certbot：AGENTS.md 第 4 步 HTTPS 流程就用它，`renew` 无交互且幂等。
# - journalctl：meta.py 的 doctor 修复建议里就写着 `journalctl --vacuum-size=200M`，
#   磁盘告警最常见的成因正是 journal 无人清理。
# - systemctl：只放开 restart，且 unit 必须是面板纳管的（见 _managed_units），
#   否则等于让定时任务去动系统服务。
# 没有放开的：pg_dump / VACUUM / 面板 SQLite 维护——本仓库没有对应 CLI 子命令
# （备份走 backup_runner 的 systemd timer，PG 侧只有 psql 交互路径），
# 凭空写一个二进制名只会让任务每次跑完都留一句 "命令不存在"。
ALLOWED_CMDS = ("choyeonctl", "certbot", "journalctl", "systemctl")

# choyeonctl 放开的子命令（固定参数；logs 的 --lines N、app deploy|rollback 的 --id N 单独校验）。
# start|stop|restart 不在内：面板重启自己该由人决定，不交给无人复核的定时计划。
CHOYEONCTL_FIXED = (("backup", "run"), ("backup", "list"), ("doctor",), ("status",), ("app", "list"), ("logs",))

PRESET_CMDS: tuple[dict, ...] = (
    {"label": "面板数据备份 choyeonctl backup run", "argv": ["choyeonctl", "backup", "run"]},
    {"label": "面板自检 choyeonctl doctor", "argv": ["choyeonctl", "doctor"]},
    {"label": "备份产物列表 choyeonctl backup list", "argv": ["choyeonctl", "backup", "list"]},
    {"label": "面板日志 choyeonctl logs", "argv": ["choyeonctl", "logs"]},
    {"label": "应用列表 choyeonctl app list", "argv": ["choyeonctl", "app", "list"]},
    {"label": "证书续期 certbot renew", "argv": ["certbot", "renew"]},
    {"label": "清理 journal journalctl --vacuum-size=200M", "argv": ["journalctl", "--vacuum-size=200M"]},
    {"label": "重启已纳管服务 systemctl restart <unit>", "argv": ["systemctl", "restart"], "needs_unit": True},
)

_VACUUM_SIZE_RE = re.compile(r"^--vacuum-size=\d{1,6}[MG]$")
_VACUUM_TIME_RE = re.compile(r"^--vacuum-time=\d{1,6}[hd]$")
_LINES_RE = re.compile(r"^--lines=([1-9]|[1-9]\d{1,3}|1000)$")
_DIGIT_RE = re.compile(r"^\d{1,9}$")


# ------------------------------------------------------------------ 表达式解析
def _check_value(raw: str, lo: int, hi: int, label: str) -> int:
    v = int(raw)
    if v < lo or v > hi:
        raise ValueError(f"{label}「{raw}」超出范围（应为 {lo}-{hi}）")
    return v


def _parse_field(token: str, key: str, lo: int, hi: int, label: str) -> tuple[set[int], bool]:
    """返回 (命中值集合, 是否写成星号形式)。星号标志决定 dom/dow 的 cron「或」语义。"""
    if not token:
        raise ValueError(f"{label} 段不能为空")
    values: set[int] = set()
    star = False
    for item in token.split(","):
        m = _TOKEN_RE.match(item)
        if not m:
            raise ValueError(
                f"{label} 段无法识别「{item}」：只支持 *、数字、a-b 范围与 /步长，"
                "不支持 mon、jan 这类英文名，也不支持 @daily/@reboot"
            )
        base, step_txt = m.group(1), m.group(2)
        step = int(step_txt) if step_txt else 1
        # 步长 0 会让 range 抛内置异常，必须先把错误转成面向用户的中文提示
        if step_txt and step < 1:
            raise ValueError(f"{label} 段的步长必须大于 0（收到 /{step_txt}）")
        if base == "*":
            start, end = lo, hi
            star = True
        elif "-" in base:
            a, b = base.split("-")
            start = _check_value(a, lo, hi, label)
            end = _check_value(b, lo, hi, label)
            if start > end:
                raise ValueError(f"{label} 段的范围起点 {start} 不能大于终点 {end}")
        else:
            start = _check_value(base, lo, hi, label)
            # `5/15` 是 Vixie cron 写法：从 5 起每 15 递增到该段上界；无步长时就是单个值
            end = hi if step_txt else start
        values.update(range(start, end + 1, step))
    if key == "dow":
        values = {0 if v == 7 else v for v in values}
    if not values:
        raise ValueError(f"{label} 段没有命中任何时刻")
    return values, star


def parse_expr(expr: object) -> dict:
    """解析 5 段 crontab，返回结构化 spec（含中文 human）。非法输入抛 ValueError。"""
    text = str(expr or "").strip()
    if not text:
        raise ValueError("cron 表达式不能为空")
    tokens = text.split()
    if len(tokens) != 5:
        raise ValueError(
            f"cron 表达式必须是 5 段（分 时 日 月 星期），收到 {len(tokens)} 段。"
            "不支持 @reboot/@daily 等宏，也不支持 6 段的秒级写法"
        )
    if any(t.startswith("@") for t in tokens):
        raise ValueError("不支持 @reboot/@daily 这类宏写法，请改成 5 段表达式")
    if re.search(r"[A-Za-z]", text):
        raise ValueError("不支持 mon、jan 这类英文名，星期与月份只能用数字（星期 0=周日）")

    spec: dict = {"expr": " ".join(tokens), "stars": {}}
    for token, (key, lo, hi, label) in zip(tokens, FIELDS, strict=True):
        values, star = _parse_field(token, key, lo, hi, label)
        spec[key] = sorted(values)
        spec["stars"][key] = star
    spec["human"] = humanize(spec)
    return spec


def _is_full(values: list[int], lo: int, hi: int) -> bool:
    return len(values) == hi - lo + 1


def _runs(values: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for v in sorted(values):
        if out and v == out[-1][-1] + 1:
            out[-1].append(v)
        else:
            out.append([v])
    return out


def _arithmetic_step(values: list[int], lo: int) -> int | None:
    """从段下界开头的等差序列（`*/15`、`5/15` 那种）返回步长，否则 None。"""
    if len(values) < 2 or values[0] != lo:
        return None
    step = values[1] - values[0]
    if step < 2:
        return None
    return step if values == list(range(lo, values[-1] + 1, step)) else None


def _num_txt(values: list[int], unit: str) -> str:
    """数字集合 → 「09-18 时」「1、15、30 日」这类中文串。"""
    parts = [f"{r[0]:02d}" if len(r) == 1 else f"{r[0]:02d}-{r[-1]:02d}" for r in _runs(values)]
    return "、".join(parts) + unit


def _dow_cn_runs(values: list[int]) -> str:
    return "、".join(
        f"周{DOW_CN[r[0]]}" if len(r) == 1 else f"周{DOW_CN[r[0]]}至周{DOW_CN[r[-1]]}" for r in _runs(values)
    )


def humanize(spec: dict) -> str:
    minute, hour = spec["minute"], spec["hour"]
    dom, mon, dow = spec["dom"], spec["mon"], spec["dow"]
    # 展示用「是否覆盖整段」判断（`*/2` 并没有覆盖整段），
    # 而 dom/dow 的 cron「或」语义要用星号标志，两者不能混用。
    mon_all = _is_full(mon, 1, 12)
    dom_all = _is_full(dom, 1, 31)
    dow_all = _is_full(dow, 0, 6)
    mon_txt = "" if mon_all else f"每年 {_num_txt(mon, ' 月')}"

    # 先说"哪一天"再说"几点"，读起来才是中文而不是字段清单
    day_txt = f"每月 {_num_txt(dom, ' 日')}" if mon_all else f"{mon_txt} {_num_txt(dom, ' 日')}"
    if dom_all and dow_all:
        when = f"{mon_txt} 每天" if mon_txt else "每天"
    elif dom_all:
        when = f"{mon_txt} 每{_dow_cn_runs(dow)}" if mon_txt else f"每{_dow_cn_runs(dow)}"
    elif dow_all:
        when = day_txt
    else:
        # 两者都限定 = cron 的「或」语义，中文如实说明（systemd 渲染不出来，to_oncalendar 报错）
        when = f"{day_txt} 或 每{_dow_cn_runs(dow)}（满足其一即执行）"

    min_all = _is_full(minute, 0, 59)
    hour_all = _is_full(hour, 0, 23)
    if min_all and hour_all:
        clock = "每分钟"
    elif min_all:
        clock = f"{_num_txt(hour, ' 点') if len(hour) > 1 else f'{hour[0]:02d} 点'}内每分钟"
    elif hour_all:
        step = _arithmetic_step(minute, 0)
        clock = f"每 {step} 分钟" if step else f"每小时的 {_num_txt(minute, ' 分')}"
    elif len(hour) == 1 and len(minute) == 1:
        clock = f"{hour[0]:02d}:{minute[0]:02d}"
    elif len(minute) == 1 and minute[0] == 0:
        step = _arithmetic_step(hour, 0)
        clock = f"每 {step} 小时整点" if step else f"{_num_txt(hour, ' 点')}整点"
    elif len(hour) == 1:
        clock = f"{hour[0]:02d} 点 {_num_txt(minute, ' 分')}"
    else:
        clock = f"{_num_txt(hour, ' 点')} 的 {_num_txt(minute, ' 分')}"
    # 「每天 每 15 分钟」里的"每天"是废话：分钟级本身已经在说频次
    if when == "每天" and clock.startswith("每"):
        return clock
    return f"{when} {clock}"


# ------------------------------------------------------------------ systemd 渲染
def _set_txt(values: list[int], lo: int, hi: int) -> str:
    """数值集合 → systemd 日历字段：覆盖整段用 *，连续段用 a..b，其余用逗号列表。

    刻意不生成 `a..b/n` 步长写法：systemd 认，但那等于把"解析对不对"再交回一个
    此处无法验证的展开过程；展开成显式列表后，OnCalendar 与面板显示的中文说明
    是同一份数据，看 unit 文件就能确认到底几点跑。
    """
    if _is_full(values, lo, hi):
        return "*"
    return ",".join(f"{r[0]:02d}" if len(r) == 1 else f"{r[0]:02d}..{r[-1]:02d}" for r in _runs(values))


def _dow_txt(values: list[int]) -> str:
    """cron 0-6（0=周日）→ systemd 的 Sun..Sat。

    只在真正连续的段里用 `..`：sorted 后的 0 与 6 不相邻，所以永远不会生成
    systemd 不接受的回绕区间（Sat..Sun）。
    """
    parts = []
    for r in _runs(values):
        parts.append(DOW_NAMES[r[0]] if len(r) == 1 else f"{DOW_NAMES[r[0]]}..{DOW_NAMES[r[-1]]}")
    return ",".join(parts)


def to_oncalendar(spec: dict) -> str:
    """spec → OnCalendar=。cron 的 dom/dow「或」语义 systemd 表达不了，如实报错。"""
    stars = spec["stars"]
    if not stars["dom"] and not stars["dow"]:
        raise ValueError(
            "该表达式同时限定了「几号」和「星期几」：cron 是满足其一即执行，"
            "systemd timer 是两者都满足才执行。为避免按错误的时间执行，请拆成两条任务"
        )
    date = "*-{0}-{1}".format(_set_txt(spec["mon"], 1, 12), _set_txt(spec["dom"], 1, 31))
    clock = "{0}:{1}:00".format(_set_txt(spec["hour"], 0, 23), _set_txt(spec["minute"], 0, 59))
    dow = "" if _is_full(spec["dow"], 0, 6) else _dow_txt(spec["dow"]) + " "
    return f"{dow}{date} {clock}"


def preview(expr: object) -> dict:
    """只读预览：中文说明 + OnCalendar，不写库也不写 unit。"""
    spec = parse_expr(expr)
    out = {"expr": spec["expr"], "human": spec["human"], "oncalendar": None, "error": None}
    try:
        out["oncalendar"] = to_oncalendar(spec)
    except ValueError as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------ 命令白名单
def _resolve_binary(name: str) -> str:
    """白名单命令名 → 绝对路径（ExecStart 要绝对路径才起得来）。"""
    if name == "choyeonctl":
        # install.sh 才会建 /usr/local/bin/choyeonctl 软链；只走过 upgrade 的实例
        # 只有仓库内入口（meta.py 的 doctor 正是在报这件事），两处都认。
        for cand in (Path("/usr/local/bin/choyeonctl"), Path(config.BASE) / "bin" / "choyeonctl"):
            if cand.exists():
                return str(cand)
        raise BadInput("本机没有 choyeonctl 入口，无法作为定时任务执行（请先 ln -sf 仓库内 bin/choyeonctl）")
    path = shutil.which(name)
    if not path:
        raise BadInput(f"命令 {name} 在本机不存在，无法作为定时任务执行")
    return path


def _managed_units() -> list[str]:
    """面板能重启的 unit：面板自身 + apps 表里的应用（含 unit_override）。"""
    from . import apps_service

    units = [f"{PANEL_SERVICE}.service"]
    for a in dbm.query("SELECT name, unit_override FROM apps"):
        units.append(apps_service.unit_name(a))
    return units


def _lines_ok(val: str) -> bool:
    """`--lines N` 的空格写法与 `--lines=N` 共用同一条 1..1000 口径。"""
    return bool(_LINES_RE.match(f"--lines={val}"))


def _validate_choyeonctl(rest: list[str]) -> None:
    if not rest:
        raise BadInput("choyeonctl 需要子命令，例如 choyeonctl backup run")
    if tuple(rest) in CHOYEONCTL_FIXED:
        return
    if rest[0] == "logs" and len(rest) == 2 and _LINES_RE.match(rest[1]):
        return
    # `logs --lines 200`（文档里的空格写法）与 `logs --lines=200` 等价，都得认。
    # 上限 1000 与 CLI 的 _tail() 同口径：0 会让它打印整份日志。
    if rest[0] == "logs" and len(rest) == 3 and rest[1] == "--lines" and _lines_ok(rest[2]):
        return
    if len(rest) == 4 and rest[0] == "app" and rest[1] in ("deploy", "rollback") \
            and rest[2] == "--id" and _DIGIT_RE.match(rest[3]):
        return
    raise BadInput(
        "choyeonctl 只允许：backup run / backup list / doctor / status / app list / "
        "logs [--lines N] / app deploy|rollback --id N"
    )



def _validate_argv(argv: object) -> list[str]:
    """argv → 通过白名单校验的字符串列表；任何不合规写法都抛 BadInput（中文）。"""
    if not isinstance(argv, list) or not argv:
        raise BadInput("命令必须是固定参数数组（argv），不能为空")
    if len(argv) > MAX_ARGS:
        raise BadInput(f"命令参数最多 {MAX_ARGS} 个")
    out: list[str] = []
    for a in argv:
        if not isinstance(a, str) or not ARG_RE.match(a):
            raise BadInput(
                "参数只允许字母、数字与 _ @ : . / = + -（长度 1-128）。"
                "空格、; | & $ 反引号 引号 等 shell 元字符一律拒绝"
            )
        out.append(a)
    if sum(len(x) for x in out) > MAX_ARGV_CHARS:
        raise BadInput("命令整体过长")
    head = out[0]
    if "/" in head:
        raise BadInput("命令名只能用白名单里的裸命令，不允许写路径")
    if head not in ALLOWED_CMDS:
        raise BadInput(f"命令 {head} 不在白名单内，可用命令：{'、'.join(ALLOWED_CMDS)}")
    rest = out[1:]
    if head == "choyeonctl":
        _validate_choyeonctl(rest)
    elif head == "certbot":
        if rest != ["renew"]:
            raise BadInput("certbot 只允许 renew 子命令，且不接受额外参数")
    elif head == "journalctl":
        if len(rest) != 1 or not (_VACUUM_SIZE_RE.match(rest[0]) or _VACUUM_TIME_RE.match(rest[0])):
            raise BadInput("journalctl 只允许清理类参数：--vacuum-size=200M 或 --vacuum-time=7d")
    else:  # systemctl
        if len(rest) != 2 or rest[0] != "restart":
            raise BadInput("systemctl 只允许 restart，且必须带上一个 unit")
        if rest[1] not in _managed_units():
            raise BadInput("systemctl restart 仅限面板纳管的 unit（面板自身或应用服务）")
    return out


def _exec_start(argv: list[str]) -> str:
    """ExecStart 行：绝对路径 + 白名单参数，逐字下发。没有 shell，也没有变量展开的余地。"""
    parts = [_resolve_binary(argv[0]), *[unit_exec_arg_escape(a) for a in argv[1:]]]
    return "ExecStart=" + " ".join(f'"{p}"' if " " in p else p for p in parts)


# ------------------------------------------------------------------ unit 落地
def _unit_base(tid: int) -> str:
    return f"panel-cron-{tid}"


def _unit_files(tid: int) -> tuple[str, str]:
    u = _unit_base(tid)
    d = _unit_dir()
    return f"{d}/{u}.service", f"{d}/{u}.timer"


def _tz() -> str:
    """时区名写进 OnCalendar，避免"面板说本地时间、timer 按 UTC 跑"的错位。"""
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        link = str(Path("/etc/localtime").resolve())
    except OSError:
        return "UTC"
    return link.split("/zoneinfo/")[1] if "/zoneinfo/" in link else "UTC"


async def _sync_units(task: dict) -> None:
    """按库里的行渲染或清理 unit 对；禁用与删除共用同一段清理逻辑。"""
    svc, tmr = _unit_files(task["id"])
    base = _unit_base(task["id"])
    if not task["enabled"]:
        await run("systemctl", ["disable", "--now", f"{base}.timer"], timeout=30)
        for f in (svc, tmr):
            if Path(f).exists():
                Path(f).unlink()
        await run("systemctl", ["daemon-reload"])
        return
    argv = _validate_argv(json.loads(task["argv"] or "[]"))
    cal = to_oncalendar(parse_expr(task["expr"]))
    # 与 backup_service 同样的两处转义：CP_* 路径来自 .env/环境变量，含 % 会被 systemd
    # 展开成说明符（WorkingDirectory 直接起不来，Environment 值会被改写）。
    wd = unit_pct_escape(str(Path(config.BASE, "backend")))
    data_dir = unit_env_value(config.DATA_DIR)
    py = unit_env_value(str(Path(config.BASE, "backend", ".venv", "bin", "python")))
    _write_if_changed(
        svc,
        f"[Unit]\nDescription=choyeon-panel scheduled task #{task['id']} {task['name']}\n"
        f"[Service]\nType=oneshot\nWorkingDirectory={wd}\n{_exec_start(argv)}\n"
        f'Environment="CP_DATA_DIR={data_dir}"\nEnvironment="CP_CLI_PYTHON={py}"\n',
    )
    _write_if_changed(
        tmr,
        f"[Unit]\nDescription=choyeon-panel cron timer #{task['id']} {task['name']}\n"
        f"[Timer]\nOnCalendar={cal} {_tz()}\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    await run("systemctl", ["daemon-reload"])
    r = await run("systemctl", ["enable", "--now", f"{base}.timer"], timeout=30)
    if r["code"] != 0:
        raise RuntimeError(r["out"] or "systemctl enable 失败")


async def _cleanup_units(task: dict) -> None:
    await _sync_units({**task, "enabled": 0})


# ------------------------------------------------------------------ CRUD 与执行
def _row_out(row: dict) -> dict:
    out = dict(row)
    argv: list[str] = []
    try:
        parsed = json.loads(row.get("argv") or "[]")
        if isinstance(parsed, list):
            argv = [str(x) for x in parsed]
    except (TypeError, ValueError):
        argv = []
    out["argv_list"] = argv
    out["cmd"] = redact_creds(" ".join(argv))
    try:
        out["human"] = parse_expr(row.get("expr") or "")["human"]
        out["expr_error"] = False
    except ValueError:
        # 历史脏数据（手工写进库的表达式）不该让整个列表 500：如实标出来交给人修
        out["human"] = ""
        out["expr_error"] = True
    out["timer"] = f"{_unit_base(row['id'])}.timer"
    return out


async def list_tasks() -> list[dict]:
    rows = await aio_db.query("SELECT * FROM cron_task ORDER BY id")
    items = [_row_out(r) for r in rows]
    states = await _timer_states([i["timer"] for i in items])
    results = await _unit_results([_unit_base(i["id"]) for i in items])
    for i in items:
        st = states.get(i["timer"]) or {}
        i["timer_active"] = bool(st.get("active", False))
        i["timer_next"] = st.get("next", "")
        i["timer_unit_state"] = st.get("enabled", "")
        _merge_last_run(i, results.get(_unit_base(i["id"])) or {})
    return items


def _merge_last_run(item: dict, res: dict) -> None:
    """把 systemd 记录的那次执行并进列表行，比库里的 last_run 新就覆盖它。

    只有 run_now 会写 cron_task.last_run，而绝大多数任务是被 timer 唤醒的：不读 systemd
    的话界面长期显示"未执行"或几周前手动那一次，而定时器其实天天在跑。
    `last_source` 如实说明这条结果来自哪一侧（timer 跑的输出在 journald 里，见 run_log）。
    """
    item["running"] = bool(res.get("running"))
    item["last_source"] = "manual" if item.get("last_run") else ""
    if not res.get("start") or not res.get("status"):
        return
    if (item.get("last_run") or "") >= res["start"]:
        return
    item["last_run"] = res["start"]
    item["last_status"] = res["status"]
    item["last_source"] = "timer"


def _parse_show(text: str) -> list[dict]:
    """`systemctl show` 多单元输出 → 每个单元一个 dict。

    块之间用空行分隔，而**属性顺序由 systemd 自己决定**：实测
    NextElapseUSecRealtime 排在 Id 之前，所以"看到 Id 才开始收属性"的写法
    会把下次执行时间整列丢掉（这块真在生产上红过：Timer 列永远显示"未取到"）。
    """
    blocks: list[dict] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        key, sep, val = line.partition("=")
        if sep:
            cur[key.strip()] = val.strip()
    if cur:
        blocks.append(cur)
    return blocks


_TS_DROP_RE = re.compile(r"^(?:\w{3}\s+)?(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")


def _local_ts(raw: str) -> str:
    """systemd 的 `Sun 2026-10-11 03:30:10 CST` → `2026-10-11 03:30:10`（本机本地时间）。

    与 run_now 写库的 time.strftime 同格式，前端 fmtDateTime 才不用区分两种来源。
    """
    m = _TS_DROP_RE.match(raw or "")
    return m.group(1) if m else ""


def _exec_result(block: dict) -> str:
    """systemd 的 Result + ExecMainStatus → 面板用的状态串（与 run_now 同词汇）。"""
    if not _local_ts(block.get("ExecMainStartTimestamp", "")):
        return ""
    result = block.get("Result", "")
    if block.get("ExecMainStatus") == "0" and result in ("success", ""):
        return "ok"
    if result == "timeout":
        return "timeout"
    code = block.get("ExecMainStatus", "")
    if code.isdigit() and code != "0":
        return f"exit {code}"
    return f"result {result or 'failed'}"


async def _unit_results(bases: list[str]) -> dict[str, dict]:
    """每个 panel-cron-N.service 最近一次执行的时间/退出码/是否正在跑。"""
    if not bases:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "ActiveState", "-p", "ExecMainStartTimestamp",
         "-p", "ExecMainStatus", "-p", "Result", *[f"{b}.service" for b in bases]],
        timeout=10,
    )
    out: dict[str, dict] = {}
    for block in _parse_show(r["out"]):
        uid = block.get("Id", "")
        if not uid.endswith(".service"):
            continue
        start = _local_ts(block.get("ExecMainStartTimestamp", ""))
        out[uid[: -len(".service")]] = {
            "start": start,
            "status": _exec_result(block),
            "running": block.get("ActiveState") == "active",
        }
    return out


async def _timer_states(units: list[str]) -> dict:
    """systemctl show 批量取 timer 状态；unit 不存在或本机没有 systemd 时按未知处理。

    属性名用 NextElapseUSecRealtime（systemd 的真实属性名；`NextElapsedUSec` 取不到值，
    照抄只会得到一个永远空白的"下次执行"列）。
    """
    if not units:
        return {}
    r = await run(
        "systemctl",
        ["show", "-p", "Id", "-p", "ActiveState", "-p", "UnitFileState", "-p", "NextElapseUSecRealtime", *units],
        timeout=10,
    )
    out: dict[str, dict] = {}
    for block in _parse_show(r["out"]):
        uid = block.get("Id", "")
        if not uid:
            continue
        nxt = block.get("NextElapseUSecRealtime", "")
        out[uid] = {
            "active": block.get("ActiveState") == "active",
            "next": "" if nxt in ("n/a", "infinity") else nxt,
            "enabled": block.get("UnitFileState", ""),
        }
    return out


async def run_log(tid: int) -> dict:
    """读某条任务最近一次执行的输出，来源取"时间上更新的那一侧"。

    手动执行由面板自己捕获输出并写进 log_tail，timer 唤醒的那一轮不经过面板、
    输出只在 journald 里。两边各自只知道自己的最后一次，所以必须比时间：
    刚手动跑完就去看 journald，读到的是上一次定时器那轮，等于答非所问。
    `--since` 卡在这次启动的时间点上，否则 -n 会把历史几十轮一起倒回来。
    """
    row = await aio_db.query_one("SELECT log_tail, last_run, last_status FROM cron_task WHERE id=?", (tid,))
    if not row:
        raise NotFound("定时任务不存在")
    base = _unit_base(tid)
    res = (await _unit_results([base])).get(base) or {}
    stored = {"unit": f"{base}.service", "source": "stored", "start": row["last_run"] or "",
              "status": row["last_status"] or "", "log": redact_creds(row["log_tail"] or "")}
    if not res.get("start") or (row["last_run"] or "") >= res["start"]:
        return stored
    args = ["-u", f"{base}.service", "--no-pager", "-o", "cat", "-n", "400", "--since", res["start"]]
    r = await run("journalctl", args, timeout=20)
    text = redact_creds(r["out"] or "")
    if r["code"] != 0 and not text.strip():
        # 无 systemd / 无权限的机器上 journalctl 会整段失败，退回库里那份而不是显示空白；
        # 时间取 systemd 的（它更新），状态取它的退出码，输出只能是库里那份。
        return {**stored, "start": res["start"], "status": res["status"] or stored["status"]}
    return {
        "unit": f"{base}.service",
        "source": "journal",
        "start": res.get("start", ""),
        "status": res.get("status", ""),
        "log": text[-LOG_TAIL_BYTES * 2:],
    }


def _spec_or_bad(expr: object) -> str:
    """入库前双闸：能解析，且 systemd 真能按它执行。"""
    try:
        spec = parse_expr(expr)
        to_oncalendar(spec)
    except ValueError as e:
        raise BadInput(str(e)) from e
    return spec["expr"]


async def create_task(body: dict) -> dict:
    name = str(body.get("name") or "")
    if not NAME_RE.match(name):
        raise BadInput("任务名只能以小写字母开头，由小写字母、数字与 - 组成（2-39 位）")
    if await aio_db.query_one("SELECT id FROM cron_task WHERE name=?", (name,)):
        raise BadInput(f"任务名 {name} 已存在")
    expr = _spec_or_bad(body.get("expr"))
    argv = _validate_argv(body.get("argv"))
    enabled = 1 if (body.get("enabled") is None or body.get("enabled")) else 0
    res = await aio_db.execute(
        "INSERT INTO cron_task(name,expr,argv,enabled) VALUES(?,?,?,?)",
        (name, expr, json.dumps(argv, ensure_ascii=False), enabled),
    )
    row = await aio_db.query_one("SELECT * FROM cron_task WHERE id=?", (res,))
    if not row:
        raise RuntimeError("任务创建后未能读回，请检查数据库")
    try:
        await _sync_units(row)
    except Exception as e:  # noqa: BLE001
        # unit 没落地却留下启用的行，列表就显示一条"永远不会跑"的任务
        await aio_db.execute("DELETE FROM cron_task WHERE id=?", (res,))
        await _cleanup_units(row)
        raise BadInput(f"定时任务落地失败，已撤销：{e}") from e
    return _row_out(row)


async def update_task(tid: int, body: dict) -> dict:
    cur = await aio_db.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))
    if not cur:
        raise NotFound("定时任务不存在")
    expr = cur["expr"] if body.get("expr") is None else _spec_or_bad(body.get("expr"))
    argv = cur["argv"] if body.get("argv") is None else json.dumps(_validate_argv(body.get("argv")), ensure_ascii=False)
    enabled = cur["enabled"] if body.get("enabled") is None else (1 if body.get("enabled") else 0)
    await aio_db.execute("UPDATE cron_task SET expr=?,argv=?,enabled=? WHERE id=?", (expr, argv, enabled, tid))
    row = await aio_db.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))
    if not row:
        raise NotFound("定时任务不存在")
    try:
        await _sync_units(row)
    except Exception as e:  # noqa: BLE001
        # 库里已是新值、systemd 还是旧 timer，两边不一致且界面看不出来：还原改动前那行再报错
        await aio_db.execute(
            "UPDATE cron_task SET expr=?,argv=?,enabled=? WHERE id=?", (cur["expr"], cur["argv"], cur["enabled"], tid)
        )
        try:
            await _sync_units(cur)
        except Exception:  # noqa: BLE001
            pass  # 还原时还同步不上就是环境问题，别盖掉原始报错
        raise BadInput(f"定时任务更新失败，已还原为改动前的配置：{e}") from e
    return _row_out(row)


async def delete_task(tid: int) -> str:
    row = await aio_db.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))
    if not row:
        raise NotFound("定时任务不存在")
    await _cleanup_units(row)
    await aio_db.execute("DELETE FROM cron_task WHERE id=?", (tid,))
    return row["name"]


async def run_now(tid: int, actor: str = "system") -> dict:
    """管理员手动执行一次：固定 argv 直接 exec，输出脱敏后只留最后 4KB。"""
    row = await aio_db.query_one("SELECT * FROM cron_task WHERE id=?", (tid,))
    if not row:
        raise NotFound("定时任务不存在")
    argv = _validate_argv(json.loads(row["argv"] or "[]"))
    # 两道闸各挡一种重叠：systemd 那条挡"timer 正在跑这一轮"，文件锁挡"两个管理员同时点"。
    # 重叠跑 `choyeonctl backup run` 会写出两份备份、pg_dump 会互相挤，出错的还是数据库。
    base = _unit_base(tid)
    if (await _unit_results([base])).get(base, {}).get("running"):
        raise BadInput("定时器正在执行这个任务，请等这一轮结束后再手动执行")
    lock = _lock_path(tid)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise BadInput("这个任务正在执行中（上一次还没跑完），请等它结束") from None
    try:
        return await _exec_task(tid, row, argv, actor)
    finally:
        os.close(fd)  # 关描述符即释放锁


def _lock_path(tid: int) -> Path:
    return Path(config.DATA_DIR, "cron", f"{tid}.lock")


async def _exec_task(tid: int, row: dict, argv: list[str], actor: str) -> dict:
    started = time.time()
    code, raw, timed_out = 0, b"", False
    try:
        proc = await asyncio.create_subprocess_exec(
            _resolve_binary(argv[0]),
            *argv[1:],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(Path(config.BASE, "backend")),
            start_new_session=True,
        )
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=RUN_TIMEOUT)
            code = proc.returncode if proc.returncode is not None else 1
        except TimeoutError:
            kill_group(proc)
            code, timed_out = 124, True
    except FileNotFoundError as e:
        code, raw = 127, str(e).encode()
    # 先脱敏再截断：截断点会切掉 user: 前缀，先截就可能让口令漏出去
    masked = redact_creds(raw.decode(errors="replace")).encode(errors="replace")
    tail = masked[-LOG_TAIL_BYTES:].decode(errors="replace")
    status = "timeout" if timed_out else ("ok" if code == 0 else f"exit {code}")
    await aio_db.execute(
        "UPDATE cron_task SET last_run=?, last_status=?, log_tail=? WHERE id=?",
        (time.strftime("%Y-%m-%d %H:%M:%S"), status, tail, tid),
    )
    await aio_db.audit(actor, "cron:run", f"{row['name']}(code={code}, {round(time.time() - started, 1)}s)")
    return {"code": code, "status": status, "log": tail}


def command_options() -> dict:
    """给前端的白名单说明：可用命令与可重启的 unit，都由服务端真实配置给出，前端不抄常量。"""
    return {
        "allowed": list(ALLOWED_CMDS),
        "presets": [dict(p) for p in PRESET_CMDS],
        "units": _managed_units(),
        "note": "定时任务以 root 直接执行固定参数，不经过 shell；不在白名单内的命令不会被保存",
    }
