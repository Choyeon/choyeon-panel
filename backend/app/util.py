import asyncio
import os
import re
import subprocess
from contextlib import suppress

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,64}(\.service)?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
GIT_URL_RE = re.compile(r"^(https?://\S+|git@[\w.-]+:\S+)$")
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{2,200}$")
UNIT_NAME_FILE_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,64}\.service$")


def is_name(s): return bool(NAME_RE.match(s or ""))
def is_domain(s): return bool(DOMAIN_RE.match(s or ""))
def is_unit(s): return bool(UNIT_RE.match(s or ""))
def is_unit_file(s): return bool(UNIT_NAME_FILE_RE.match(s or ""))
def is_port(p): return isinstance(p, int) and 1024 <= p <= 65535
def is_branch(s): return bool(BRANCH_RE.match(s or ""))
def is_ident(s): return bool(IDENT_RE.match(s or ""))
def is_path(s): return bool(PATH_RE.match(s or ""))
def is_git_url(s): return bool(s) and (bool(GIT_URL_RE.match(s)) or s.startswith("/"))


def clean_abs_path(p, what="路径"):
    """校验并规范化"要拿去拼接/写盘/交 shell"的绝对路径，返回 normpath 结果。

    只做字符白名单是不够的：PATH_RE 里含 `.` 与 `/`，所以
    `/root/www/app/../../etc` 能通过校验，但内核解析后就是 /etc——面板以 root
    运行，等于把 .panel.env、WorkingDirectory、install_cmd 的 cwd 全搬到系统目录里。
    实测这种 path 还能绕过"路径已被其他应用占用"的重复检查（按字符串比较），
    两个应用指向同一个目录时后者会覆盖前者的 unit 与 .panel.env。
    因此这里必须：拒绝 `..` 段 → 规范化 → 再确认仍是绝对路径。
    """
    if not p or not isinstance(p, str):
        raise RuntimeError(f"{what}不能为空")
    if not is_path(p):
        raise RuntimeError(f"{what}不合法（仅允许字母、数字与 . _ / -，且必须是绝对路径）")
    parts = p.split("/")
    if ".." in parts:
        raise RuntimeError(f"{what}不允许 ..（收到：{p}）")
    norm = os.path.normpath(p)
    if not norm.startswith("/") or norm == "/":
        raise RuntimeError(f"{what}必须是 / 以下的绝对路径")
    if ".." in norm.split("/"):
        raise RuntimeError(f"{what}规范化后仍含 ..")
    return norm


def is_http_url(s):
    """仅允许 http/https 的绝对 URL，且必须有主机名。

    通知 webhook 由用户填写后交给 urllib，而 urllib 默认 handler 支持 file://：
    填 `file:///root/.ssh/id_rsa` 会让面板带着自己的权限去读本地文件，
    data:// 之类同样不会被真正 POST 出去。校验放在保存和发送两处。
    """
    if not s:
        return False
    try:
        from urllib.parse import urlsplit
        u = urlsplit(s)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and bool(u.netloc)

def sh_escape(s): return "'" + str(s).replace("'", "'\\''") + "'"


def unit_pct_escape(s) -> str:
    r"""unit 行里要写成字面量的文本：`%` 翻倍成 `%%`。

    systemd 对 unit 行里的 `%X` 说明符做展开，Environment=、ExecStart=、
    WorkingDirectory= 全都如此（沙箱真 systemd 实测，引号内也照展开）：
      `Environment=CP_DATA_DIR=/tmp/sp dir/a%b` → 进程拿到 `/tmp/sp`（空格截断），
      `Environment="V=/tmp/sp dir/a%b"`        → `%b` 展开成启动 ID；
      `ExecStart=/bin/sh -c "printf %b"`       → `%b` 展开成 `/bin/sh`，
        启动命令里的 `%h`/`%t`/`%n` 等同理会静默变成别的字符串；
      `WorkingDirectory=/a%b`                  → 目录不存在，单元直接起不来。
    而 `EnvironmentFile` 指向的文件内容**不做**说明符展开（实测 `V=a%b` 原样到达），
    所以 .panel.env 的转义（_env_escape）不带这一步。
    `%%` 是唯一的转义写法：实测文件写 `a\\%%b`（先转义反斜杠、再翻倍百分号），
    进程收到 `a\%b`，逐字无损。
    """
    return str(s if s is not None else "").replace("%", "%%")


def unit_exec_arg_escape(s) -> str:
    r"""ExecStart= 参数位置里要写成字面量的文本：`$` 与 `%` 各自翻倍。

    沙箱真 systemd 实测（unit 落盘 → systemctl start → 读 /proc/<bash>/cmdline，
    比较 systemd 交给 bash 的 argv[2]）：
      `ExecStart=/bin/bash -lc "echo ${ZZ}"`  → 实际交给 bash 的是 `echo hello`
        （systemd 用 unit 自己的环境展开了它，含 EnvironmentFile 读进来的值）；
      `ExecStart=/bin/bash -lc "echo ${NOPE}"` → `echo `—— unset 变量被替换成空串，
        命令照样 rc=0，面板上看到的是"启动成功"；
      `$ZZ`（不带花括号）systemd 不展开，原样下发——但 `$${ZZ}` 的转义写法对
        `$` 同样成立：文件写 `v=$${ZZ}`，进程收到逐字的 `v=${ZZ}`。
    `%X` 见 unit_pct_escape。两者都要处理：start_cmd 是用户写的 shell 命令，
    `${API_TOKEN}`、`${PORT:-3000}` 这类写法很常见，本该由 bash 在运行时展开。
    被 systemd 提前展开的后果不只是"和面板显示的不一样"——app 的 .panel.env
    里有数据库口令等密钥，systemd 会拿它去替换 start_cmd 里的 `${...}`，
    替换结果直接进 argv（/proc/<pid>/cmdline 全局可读，普通用户也能读 root
    进程的 cmdline），而 `${PORT:-3000}` 这种带默认值的写法还会被吞成空串。
    翻倍后 systemd 逐字下发，展开交回 bash，语义就是用户写这条命令时的语义。
    """
    return unit_pct_escape(str(s if s is not None else "")).replace("$", "$$")


def unit_env_value(v) -> str:
    r"""`Environment=KEY="..."` 双引号内的值：转义 `\` 与 `"`、压平换行、`%` 翻倍。

    与 apps_service._env_escape（写 .panel.env 用）的区别只有最后一步：
    EnvironmentFile 文件内容不做说明符展开，而 unit 里的 Environment= 行做，
    所以这里要多一道 `%%`。为什么必须加引号——实测不加引号时
    `Environment=CP_DATA_DIR=/tmp/sp dir/sub` 到进程里只剩 `/tmp/sp`，
    备份写到错目录，面板上看不出任何异常。
    `$` 不用处理：实测 `Environment=V=a$ZZ-trail`（加引号和不加引号两种写法）
    到进程里都是逐字的 `a$ZZ-trail`，systemd 只在 ExecStart= 的参数位展开
    `${VAR}`，见 unit_exec_arg_escape。
    """
    s = str(v if v is not None else "")
    s = s.replace(chr(92), chr(92) * 2)
    s = s.replace(chr(34), chr(92) + chr(34))
    s = s.replace(chr(10), " ").replace(chr(13), " ")
    return unit_pct_escape(s)


async def run(cmd: str, args: list, cwd=None, timeout=120, env=None) -> dict:
    """异步子进程执行，返回 {code, out}。

    超时时终止整个进程组（避免 npm/pip 子进程残留）；命令不存在返回 code=127。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            kill_group(proc)
            return {"code": 124, "out": f"timeout after {timeout}s"}
        return {"code": proc.returncode or 0, "out": out.decode(errors="replace").strip()}
    except FileNotFoundError as e:
        return {"code": 127, "out": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"code": 1, "out": str(e)}


def kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        with_subprocess(proc)


def with_subprocess(proc) -> None:
    with suppress(ProcessLookupError, AttributeError):
        proc.kill()


def run_shell(cmd: str, timeout=None) -> subprocess.CompletedProcess:
    """同步 shell 执行（backup runner / CLI 场景）。"""
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)


def parse_lines(raw: str | None, default: int = 200, maximum: int = 2000) -> int:
    """把查询串里的 lines 安全解析成 1..maximum。

    直接 `int(request.query_params.get("lines"))` 会让 `?lines=abc` 抛 ValueError
    → 500 + 堆栈；日志接口是 GET，viewer 账号也能打，等于一个免费的报错刷日志入口。
    非数字/超范围一律回落到默认值，不报错——这只是"看多少行"，不值得失败。
    """
    try:
        n = int(raw or "")
    except (TypeError, ValueError):
        return default
    return max(1, min(n if n else default, maximum))


def disk_usage_pct(path: str) -> int | None:
    """返回 path 所在分区的已用百分比（按非 root 可用空间算），取不到返回 None。

    必须用 `f_bavail`（普通进程真正能写的块）而不是 `f_bfree`：ext4 默认给 root
    预留 5%，用 f_bfree 会少报几个点，等 root 写满时面板自己先写不进 SQLite。
    告警与 doctor 共用这一份实现，避免两处口径不一致。
    """
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        if total <= 0:
            return None
        return round((total - st.f_bavail * st.f_frsize) * 100 / total)
    except OSError:
        return None


async def run_shell_async(cmd: str, timeout: float | None = 10) -> str:
    """在事件循环中安全地执行 shell 命令（避免阻塞）。返回 stdout；失败返回空串。"""
    def _sync() -> str:
        try:
            return run_shell(cmd, timeout=timeout).stdout
        except Exception:  # noqa: BLE001
            return ""

    return await asyncio.to_thread(_sync)
