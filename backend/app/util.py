import asyncio
import os
import re
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# unit 名会以 argv 塞进 systemctl / journalctl，而面板以 root 运行 —— 这两个工具把以
# '-' 开头的参数当**选项**解析（`-x`、`--all`、`--force` 都能混进命令行），systemd 本身
# 也不接受这样的单元名。所以首字符必须是字母或数字，收紧在唯一的正则里，而不是每个调用点各补一层。
UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@:._-]{0,63}(\.service)?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
GIT_URL_RE = re.compile(r"^(https?://\S+|git@[\w.-]+:\S+)$")
# 邮箱只用于 certbot 注册 ACME 账户，但它以 root 身份进 argv，所以按"能安全落到 argv
# 与 letsencrypt 配置里"的最严口径收：单个 @、两侧无空白、总长有上限。
# 不做 RFC 5322 全量校验——那会放进引号、注释和反斜杠，正好是把参数解析玩坏的材料。
EMAIL_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63})+$")
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{2,200}$")
CRED_URL_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^/@:\s]*)(:[^@/\s]*)?@")
# 键值形态的口令：`password=…`（psql/libpq 的失败正文、mysqldump 命令行）、
# `NEXTAUTH_SECRET=…`（部署日志里回显的 env）、`token: …`（应用自己打的日志）。
# 键名允许带前缀（DB_PASSWORD、csrf_token），也允许带后缀（AWS_SECRET_ID、
# AWS_ACCESS_KEY_ID、SESSION_TOKEN_VALUE）——真实 SDK 的变量名几乎都不是单个词，
# 只认"整词结尾"会把最常见的一批凭据放过去。后缀必须以下划线/连字符/点起头，
# 所以 `secretary=…` 这类普通词仍然不算凭据键名（⑤J 的误伤反例）。
# 分隔符原样保留，只把值换成 ***。
# 不收 `pwd`：本机实测 sudo 每条会话日志都是 `PWD=/cwd ; USER=…`，把整词的 pwd 当键名
# 会把运维要看的当前目录遮成 ***（真实误伤，⑤J 线上探针在 80 行真日志里抓到），
# 而以 `pwd=` 传口令的工具在这里没有对照样本——宁可少遮一个少见形态，不弄脏每一条审计。
CRED_KV_RE = re.compile(
    r"(?i)\b([\w.-]*(?:password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"(?:[._-][A-Za-z0-9]+)*)"
    r"(\s*[=:]\s*)(\S+)"
)
# JWT 形态的口令只能按形态认：`Authorization: Bearer eyJhbGci…` 里没有任何键名。
# 长度下限 12 是把 "bearer 12345" 这类正常文本放过去。
CRED_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{12,})")


# 一律用 fullmatch 而不是 match：这些正则都写成 `^…$`，而 Python 的 `$` 允许结尾**多一个
# 换行**，所以 "example.com\n"、"mydb\n" 都能过 match。字符集里没有空格/分号/引号，
# 换行也只能落在最后，进 nginx 与 argv 不会改写命令词法——但它会让库名、server_name
# 与文件名的精确比较全部失配（`domain in idx`、find_app_configs 的域名集合），
# 造出一个"看起来不存在却又占着端口"的对象。fullmatch 要求吃掉整个字符串，堵住这一条。
def is_name(s): return bool(NAME_RE.fullmatch(s or ""))
def is_domain(s): return bool(DOMAIN_RE.fullmatch(s or ""))
def is_unit(s): return bool(UNIT_RE.fullmatch(s or ""))
def is_port(p): return isinstance(p, int) and 1024 <= p <= 65535
def is_branch(s): return bool(BRANCH_RE.fullmatch(s or ""))
def is_ident(s): return bool(IDENT_RE.fullmatch(s or ""))
def is_path(s): return bool(PATH_RE.fullmatch(s or ""))
def is_git_url(s): return bool(s) and (bool(GIT_URL_RE.fullmatch(s)) or s.startswith("/"))
def is_email(s): return bool(EMAIL_RE.fullmatch((s or "").strip()))


def sub_port(text, old, new):
    """把命令/env 文本里**独立成词**的旧端口换成新端口。

    模板把端口同时写进 `port` 字段和 `start_cmd`/`env`，两边一旦不同步，nginx 就反代到
    一个没人监听的端口（面板显示"部署成功"，实际 502）。用 `(?<!\\d)…(?!\\d)` 而不是
    `str.replace`：后者会把 `13456` 里的 `3456` 一起换掉，把好好的端口改成别的数字。
    """
    if not text or old in (None, "") or new in (None, "") or str(old) == str(new):
        return text
    return re.sub(rf"(?<!\d){re.escape(str(old))}(?!\d)", str(new), str(text))


def write_secret_atomic(target, text):
    """含凭据/布局信息的落盘文件统一走这里：0600 从创建起生效，且整份替换。

    旧写法是 write_text + chmod：O_CREAT 按 umask（root 常态 022 → 0644）建文件，
    chmod 排在写完之后，新 .panel.env 从出现的第一刻起就世界可读，直到那一下 chmod
    为止——同机任意用户在这个窗口 cat 一下，口令就漏了。而且就地截断重写时，
    写一半被磁盘满/断电打断，目标文件剩半截：.env 当场变成不可解析的配置，
    unit 则留到下一次 daemon-reload 才炸，报一句和这次保存无关的语法错误。
    同目录 O_EXCL 临时文件（0600）+ fsync + os.replace：要么旧内容要么新内容，
    新文件从建立起就是 0600，全程没有可读窗口，也没有半截态。
    """
    target = Path(str(target))
    parent = str(target.parent)
    tmp = os.path.join(parent, f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except OSError as e:
        with suppress(OSError):
            os.unlink(tmp)
        raise RuntimeError(f"写入 {target.name} 失败：{e}") from e


def redact_creds(s):
    """打码文本里能看到的凭据：URL 的 `user:password@`、`password=…` 这类键值、`Bearer …`。

    仓库地址允许写成 `https://user:token@host/x.git`（GIT_URL_RE 放行），
    部署日志会把整条 `git clone` 命令原文落库，而日志与列表接口对只读账号开放，
    所以凭据既不能落库、也不能出接口。仅用户名（无 `:`）不算密钥，原样保留。

    键值与 Bearer 两支是给**所有**日志出口用的（journal 检索能把整机日志读出来，
    定时任务与部署日志原样回显），所以放在这一处而不是各接口自己想起来才遮：
    `pg_dump` 失败正文里的 `password=…`、应用 env 回显里的 `*_SECRET=…` 以前是原样出接口的。
    """
    if not s:
        return s
    s = CRED_URL_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:***@" if m.group(3) else m.group(0), s)
    s = CRED_KV_RE.sub(lambda m: m.group(1) + m.group(2) + "***", s)
    return CRED_BEARER_RE.sub(lambda m: m.group(1) + "***", s)


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


# 「一次尾读多少行」有两个互不相干的上限，之前却各写各的数、还互相指认对方：
# - CLI_LOG_LINES_MAX：`choyeonctl logs --lines=N` 到底收多大的 N。同一个旗在终端里手打
#   和被登记成定时任务由面板代为校验，必须是同一个答案——原先 CLI 夹到 5000，
#   cron_service 的正则只放行 1..1000，而它的注释写着"与 CLI 的 _tail() 同口径"。
# - HTTP_LOG_LINES_MAX：一次 GET 最多返回多少行，保护的是响应体与内存。它和命令行那档
#   没关系，页面也不该照它写死（要给选择器用就得从接口读）。
CLI_LOG_LINES_MAX = 5000
HTTP_LOG_LINES_MAX = 2000
LOG_LINES_DEFAULT = 200


def parse_lines(raw: str | None, default: int = LOG_LINES_DEFAULT,
                maximum: int = HTTP_LOG_LINES_MAX) -> int:
    """把查询串里的 lines 安全解析成 1..maximum（HTTP 响应体上限，见 `HTTP_LOG_LINES_MAX`）。

    直接 `int(request.query_params.get("lines"))` 会让 `?lines=abc` 抛 ValueError
    → 500 + 堆栈；日志接口是 GET，viewer 账号也能打，等于一个免费的报错刷日志入口。
    非数字/超范围一律回落到默认值，不报错——这只是"看多少行"，不值得失败。
    """
    try:
        n = int(raw or "")
    except (TypeError, ValueError):
        return default
    return max(1, min(n if n else default, maximum))


def log_ladder() -> list[int]:
    """网页「看多少行」的档位，由上面那两个数派生，不再由前端抄一份。

    日志中心过去同时写着 200/500/1000/2000 与默认 500：默认值和 `LOG_LINES_DEFAULT`
    对不上，最大档和 `HTTP_LOG_LINES_MAX` 也只是恰好相等。上限一改，页面就整体失真，
    而且失真是安静的——下拉里那档还在，服务端已经按自己的上限截断了。
    """
    steps = {LOG_LINES_DEFAULT, HTTP_LOG_LINES_MAX // 4, HTTP_LOG_LINES_MAX // 2, HTTP_LOG_LINES_MAX}
    return sorted(v for v in steps if 1 <= v <= HTTP_LOG_LINES_MAX)


# 「列表一次取多少条」也是同一份性质，之前却有六处抄本：
# alerts.py 的默认 100 / 上限 500，extras.py /api/audit 里另一个默认 100 / 上限 500，
# Alerts.vue 与 Audit.vue 各写 ref(200) 加一份 [100,200,500] 档位，api.ts 的 audit(limit = 100)
# 是第七个。其中三条互相矛盾：UI 默认 200、接口默认 100——同一个 GET /api/audit，
# 从页面进是 200 条、从 curl 进是 100 条，两边都不算错，但对不上。
LIST_DEFAULT = 200
LIST_CAP = 500


def parse_limit(raw, default: int = LIST_DEFAULT, maximum: int = LIST_CAP) -> int:
    """把查询串里的 limit 解析成 1..maximum。

    下界必须夹住：SQLite 里 LIMIT 负数表示「不限制」，只写 `min(limit, CAP)` 的话
    `?limit=-1` 会把整张审计表吐出来（实测 601 条全返回）。
    `limit=0` 与非数字一样回落到默认值而不是 1——这一档只是「看多少条」，不值得失败，
    但也不值得为它返回一个空列表。
    """
    try:
        n = int(raw or "")
    except (TypeError, ValueError):
        return default
    return max(1, min(n or default, maximum))


def list_ladder() -> list[int]:
    """条数下拉的档位，由上面那两个数派生：{默认的一半, 默认, 上限}。

    上限那一档必须在列里，否则改了上限，下拉还能选到旧档而服务端已经在截断；
    低档取默认值的一半，保留原来「快速看一眼最近 100 条」这一档。
    """
    steps = {LIST_DEFAULT // 2, LIST_DEFAULT, LIST_CAP}
    return sorted(v for v in steps if 1 <= v <= LIST_CAP)


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


# 字节数折成人话：存储占用与内存压力两页原来各写一份，单位表一份到 TB、一份到 PB，
# 非数字时一个回 "—"、一个回 "0B"。同一个数量在两个页签里差一个数量级的写法，
# 用户对照着看就会怀疑是不是自己看错了盘。
def human_bytes(n) -> str:
    """None / 读不到的时候回 "—"，绝不回 "0B"：0 字节是一个结论，"—" 才是"没核对成"。"""
    try:
        val = float(n)
    except (TypeError, ValueError):
        return "—"
    if val < 0:
        val = 0.0
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if val < 1024 or unit == "PB":
            return f"{val:.0f}B" if unit == "B" else f"{val:.1f}{unit}"
        val /= 1024
    return "—"


# 自检判词里「一共几项」与「点名哪一项」是同一个事实的两半，以前却各服务手抄一份
# `，另有 N 项`，三条分支抄漏了（load 的 fail、pending 的 fail、login 的合并判词）：写着
# 「2 项改动没进正在跑的进程」却只点第一条名字，本机实测被吞掉的那条正是
# postgresql@17-main.service——最该看见的那一个。收成一条函数，漏写这一半就没法编译过用例。
def tallied(names, noun: str, unit: str = "项", total: int | None = None,
            lead: str = "", mid: str = "") -> str:
    """报数在前、点名在后，拼成自检的一句判词。

    形如 `3 处占用值得处理（另有 2 处）：分区已用 93%`。`noun`/`lead`/`mid` 是调用方自己的措辞
    （数字前后那几个字），这条函数只管数字、量词和点名的那一条。

    数字在前是有原因的：自检列表把判词放在项目名下面的副标题行，窄屏那一列实测 187px，
    三行折行仍放不下整句（本机那条 pending 的 fail 要 5 行，「另有」排在第 69 个字符）。
    「另有 N」挂在句尾时被裁掉的正好是它，而那半句是「不止这一条」的唯一凭证——
    它得站在第一行里。

    total 是前半句里那个总数，默认就是 names 的长度；只有总数另有出处时才显式给（登录项用的是
    `_summary(findings)`），给了就必须盖得住点名的这一列，否则前半句说 3 项、尾巴说另有 1 项，
    加起来只有 2 项。空标题不计，免得「另有 1 项」指的是一条没有文字的判词。
    """
    ts = [str(t) for t in (names or []) if t]
    n = len(ts) if total is None else max(int(total or 0), len(ts))
    rest = f"（另有 {n - 1} {unit}）" if n > 1 else ""
    head = f"{lead}{n}{noun}{rest}{mid}"
    return f"{head}：{ts[0]}" if ts else head


async def run_shell_async(cmd: str, timeout: float | None = 10) -> str:
    """在事件循环中安全地执行 shell 命令（避免阻塞）。返回 stdout；失败返回空串。"""
    def _sync() -> str:
        try:
            return run_shell(cmd, timeout=timeout).stdout
        except Exception:  # noqa: BLE001
            return ""

    return await asyncio.to_thread(_sync)
