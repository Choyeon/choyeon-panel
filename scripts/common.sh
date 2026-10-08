#!/usr/bin/env bash
# choyeon-panel 部署脚本公共函数库
# 用法：在各脚本中 `source "$(dirname "$0")/common.sh"`

set -euo pipefail

# ---------- 可覆盖的变量（均可通过环境变量注入） ----------
# PREFIX 默认值必须能从"脚本自己所在的仓库"推出来。
# 实测：装在 /root/www/choyeon-panel 的实例执行 scripts/update.sh 时，
# 硬编码的 /root/choyeon-panel 不存在，脚本在第一行校验就 [fail] 退出，
# 于是"拉代码→备份→重建→重启→健康探测"整条流程从未执行，
# 面板还在跑旧构建，而 choyeonctl upgrade 报的却是同一个错。
_default_prefix() {
  local src
  src="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)" || src=""
  if [ -n "$src" ] && [ -d "$src/.git" ] && [ -f "$src/backend/main.py" ]; then
    printf '%s' "$src"
  else
    printf '%s' "/root/choyeon-panel"
  fi
}

PREFIX="${CP_PREFIX:-$(_default_prefix)}"
REPO="${CP_REPO:-https://github.com/Choyeon/choyeon-panel.git}"
BRANCH="${CP_BRANCH:-main}"
SERVICE_NAME="${CP_SERVICE:-choyeon-panel}"
PORT="${CP_PORT:-3210}"
HOST="${CP_HOST:-127.0.0.1}"
DATA_DIR="${CP_DATA_DIR:-$PREFIX/data}"
BACKUP_DIR="${CP_BACKUP_DIR:-/root/backups/panel}"
KEEP_BACKUPS="${CP_KEEP_BACKUPS:-14}"

BACKEND_DIR="$PREFIX/backend"
WEB_DIR="$PREFIX/web"
VENV="$BACKEND_DIR/.venv"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

# ---------- 日志 ----------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_RESET=$'\033[0m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'; C_BLUE=$'\033[34m'; C_DIM=$'\033[2m'
else
  C_RESET=''; C_RED=''; C_GREEN=''; C_YELLOW=''; C_BLUE=''; C_DIM=''
fi

log()  { printf '%s[info]%s %s\n'  "$C_BLUE"   "$C_RESET" "$*"; }
ok()   { printf '%s[ ok ]%s %s\n'  "$C_GREEN"  "$C_RESET" "$*"; }
warn() { printf '%s[warn]%s %s\n'  "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s[fail]%s %s\n'  "$C_RED"    "$C_RESET" "$*" >&2; }
step() { printf '%s==>%s %s\n'     "$C_DIM"    "$C_RESET" "$*"; }
die()  { err "$*"; exit 1; }

# ---------- 环境检测 ----------
require_root() {
  [ "$(id -u)" -eq 0 ] || die "需要 root 权限执行（面板本身要管理 systemd / nginx）"
}

# 输出：OS_FAMILY=debian|rhel|unknown，OS_ID，OS_VERSION
detect_os() {
  OS_ID="unknown"; OS_VERSION=""; OS_FAMILY="unknown"
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    OS_ID="${ID:-unknown}"; OS_VERSION="${VERSION_ID:-}"
    case "$OS_ID" in
      ubuntu|debian|raspbian|linuxmint) OS_FAMILY=debian ;;
      rhel|centos|rocky|almalinux|fedora|ol|amzn) OS_FAMILY=rhel ;;
    esac
  fi
  export OS_ID OS_VERSION OS_FAMILY
  log "系统：$OS_ID ${OS_VERSION:-?}（$OS_FAMILY 系）"
}

have_cmd() { command -v "$1" >/dev/null 2>&1; }

# ---------- 配置校验 ----------
# PREFIX / DATA_DIR / BACKUP_DIR 会流进 systemd unit 的 Environment=、.env、
# sqlite3 的 `.backup "..."` 点命令、tar 的 -C 参数等多处文本拼接场合。
# 与其在每一处小心转义，不如在入口用**白名单**收紧字符集（黑名单容易漏）。
# 允许字符与后端 app/util.py 的 PATH_RE 保持一致：字母 数字 . _ / -
# 实测反例：CP_PREFIX=/root/it's-panel 时，healthcheck 旧写法
# `bash -c "sqlite3 '$DATA_DIR/panel.db' ..."` 的引号被提前闭合，路径后半段变成待执行代码。
#
# 为什么用 case 而不是 `grep -qE '^[A-Za-z0-9._/-]+$'`：
# grep 是**按行**判断的，`/tmp/a\n/root/x;reboot` 里只要有一行合法就整串放行；
# 实测这种带换行的值 grep 版 PASS、case 版 REJECT。case 的模式匹配作用在整串上，
# 换行属于 [!A-Za-z0-9._/-] 集合，天然被拒——字符白名单必须整串判断，不能用行工具。

_validate_path() { # _validate_path <变量名> <值>
  local name="$1" val="$2"
  [ -n "$val" ] || die "$name 不能为空"
  case "$val" in
    /*) ;;
    *) die "$name 必须是绝对路径，收到：$val" ;;
  esac
  case "$val" in
    *[!A-Za-z0-9._/-]*)
      die "$name 含不安全字符（只允许字母、数字与 . _ / -），收到：$(printf '%q' "$val")"
      ;;
  esac
  # 字符白名单里有 . 和 /，所以 `..` 能通过上面两关。它在这里必须额外拒绝：
  # 实测 CP_PREFIX=/root/choyeon-panel/../.. 一路通过校验，最后 rm -rf 由内核解析
  # 成删除 / 。路径校验必须在入口就把 .. 挡掉，而不是指望每个使用点都规范化。
  case "/$val/" in
    */../*) die "$name 含 ..（收到：$val），请使用规范的绝对路径" ;;
  esac
}

validate_config() {
  _validate_path CP_PREFIX "$PREFIX"
  _validate_path CP_DATA_DIR "$DATA_DIR"
  _validate_path CP_BACKUP_DIR "$BACKUP_DIR"
  # 端口/主机名/服务名同样整串白名单，不用 grep -qE（换行绕过同理）
  case "$PORT" in
    ''|*[!0-9]*) die "CP_PORT 必须是数字，收到：$(printf '%q' "$PORT")" ;;
  esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "CP_PORT 必须在 1-65535，收到：$PORT"
  case "$HOST" in
    ''|*[!A-Za-z0-9:._-]*) die "CP_HOST 不合法（只允许字母数字与 : . _ -），收到：$(printf '%q' "$HOST")" ;;
  esac
  case "$SERVICE_NAME" in
    ''|*[!A-Za-z0-9@:._-]*) die "CP_SERVICE 不合法（只允许字母数字与 @ : . _ -），收到：$(printf '%q' "$SERVICE_NAME")" ;;
  esac
}

_norm_path() { # 去掉末尾多余的 /，让深度判断与路径比较稳定
  local v="$1"
  while [ "$v" != "/" ] && [ "${v%/}" != "$v" ]; do v="${v%/}"; done
  printf '%s' "$v"
}

_path_depth() { # 绝对路径的层级数（/root/choyeon-panel -> 2）
  local v
  v="$(_norm_path "$1")"
  [ "$v" = "/" ] && { printf '0'; return; }
  printf '%s' "$v" | tr -cd '/' | wc -c | tr -d ' '
}

# 卸载 --purge 会把这里的路径交给 rm -rf，字符白名单挡不住"路径本身填错"。
# 实测 CP_PREFIX=/ 时，输入 purge 确认后脚本执行 `rm -rf / /var`（已用 rm shim 验证）。
#
# 拦截规则：
#   1) 拒绝含 .. 的路径。字符白名单允许 `.` 与 `/`，所以
#      CP_PREFIX=/root/choyeon-panel/../.. 能通过 validate_config，
#      而 rm -rf 按内核解析后等于删 / —— 危险路径必须先看穿 .. 的把戏。
#   2) 根目录本身；层级 < 2：/root /etc /var /usr /tmp /opt 全是一次输入错误就能命中的目录。
#   3) CP_NEVER_INSIDE 里的目录：等于它、包含它、在它之内都拒绝（系统配置与运行时的地盘）。
#   4) CP_PROTECTED 里的目录：等于它或包含它时拒绝（这些是"可能有人把数据放这儿"的高价值目录，
#      但 /root/choyeon-panel 这类正常安装路径必须放行，所以不能按"在其内"来拒）。
CP_NEVER_INSIDE="/bin /boot /dev /etc /lib /lib64 /proc /run /sbin /sys /usr"
CP_PROTECTED="/opt /root /tmp /var/lib/mysql /var/lib/postgresql /var/www /var/log /srv/www /home"

_assert_deletable() { # _assert_deletable <变量名> <路径>
  local name="$1" v p
  case "/$2/" in
    */../*) die "$name（$2）含 ..，拒绝删除：请给出规范的绝对路径" ;;
  esac
  v="$(_norm_path "$2")"
  [ "$v" != "/" ] || die "$name 为根目录，拒绝删除"
  [ "$(_path_depth "$v")" -ge 2 ] \
    || die "$name（$v）层级太浅，不像是独立安装目录，拒绝删除；请把面板装在 /x/choyeon-panel 这类路径下"
  for p in $CP_NEVER_INSIDE; do
    # 清单条目全是 1 层目录，所以 v（已保证 >=2 层）既不可能等于它、也不可能成为它的祖先；
    # 这里只需判断"v 在 p 之内"。多层的受保护路径（如 /var/lib/mysql）由 CP_PROTECTED 负责，
    # 那边的祖先分支是可达的（/var/lib 就会被拦）。
    case "$v" in
      "$p"/*) die "$name（$v）位于系统目录 $p 之内，面板不应安装在 $p 下" ;;
    esac
  done
  for p in $CP_PROTECTED; do
    [ "$v" != "$p" ] || die "$name 命中受保护目录（$v），拒绝删除"
    case "$p" in
      "$v"/*) die "$name（$v）包含受保护目录 $p，拒绝删除" ;;
    esac
  done
  # 挂载点：rm -rf 会在里面报 busy，留下半个被删空的目录；NAS 备份盘尤其常见
  if [ -r /proc/mounts ] && awk -v p="$v" '$2==p{f=1} END{exit !f}' /proc/mounts; then
    die "$name（$v）是挂载点，拒绝直接删除，请先卸载或改指向具体子目录"
  fi
}

_assert_backup_outside() { # _assert_backup_outside <备份目录> <待删目录...>
  # "卸载前备份"刚写完就被同一条 rm -rf 删掉，脚本还打印"备份保留在 …"——
  # 实测确实如此，等于把最后的救命数据删了还不吭声。
  # 放在 common.sh 而不是就地写在 uninstall.sh 里：那两条断言在 require_root
  # 之后，非 root 的 CI 只能跳过，逻辑本身（纯路径比较）其实不需要权限。
  local bk p
  bk="$(_norm_path "$1")"
  shift
  for p in "$@"; do
    p="$(_norm_path "$p")"
    case "$bk" in
      "$p"|"$p"/*)
        die "CP_BACKUP_DIR（$bk）位于待删除目录之内（$p），purge 会连备份一起删掉；请改指到安装目录外再执行" ;;
    esac
  done
}

_require_tty() { # 危险操作前的交互确认必须有 tty
  # 定时任务、`curl | bash`、CI 里都可能带着参数跑到这一行；
  # 无 tty 时 read_value 只会拿到空串然后 die，报错还停在"未确认"，
  # 看不出真正原因，用户容易误以为脚本坏了而去绕过确认。
  [ -t 0 ] || die "${1:-该操作} 必须在交互终端执行（当前无 tty），确认无法进行；请手动在终端里运行"
}

need_cmd() { have_cmd "$1" || die "缺少命令：$1，请先安装"; }

version_ge() { # version_ge 当前 最低
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# ---------- 服务进程探测 ----------
# 返回：0=systemd 可用，1=无 systemd（容器/WSL 场景）
have_systemd() {
  have_cmd systemctl && [ -d /run/systemd/system ]
}

# ---------- 打包安装 ----------
pkg_install() { # pkg_install <pkg...>
  case "$OS_FAMILY" in
    debian) export DEBIAN_FRONTEND=noninteractive; apt-get install -y --no-install-recommends "$@" ;;
    rhel)   (dnf install -y "$@" 2>/dev/null || yum install -y "$@") ;;
    *)      die "未识别的发行版（$OS_ID），请手动安装：$*" ;;
  esac
}

pkg_update() {
  case "$OS_FAMILY" in
    debian) export DEBIAN_FRONTEND=noninteractive; apt-get update -qq ;;
    rhel)   dnf -q makecache 2>/dev/null || true ;;
    *)      warn "未识别的发行版，跳过包索引更新" ;;
  esac
}

# ---------- 运行时版本保障 ----------
ensure_python() { # 需要 >=3.11
  local py=""
  for c in python3.13 python3.12 python3.11 python3; do
    if have_cmd "$c"; then
      if "$c" -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' 2>/dev/null; then py="$c"; break; fi
    fi
  done
  if [ -z "$py" ]; then
    step "未找到 Python >= 3.11，尝试安装"
    case "$OS_FAMILY" in
      debian) pkg_install python3 python3-venv python3-pip ;;
      rhel)   pkg_install python3.11 python3.11-pip || pkg_install python3 python3-pip ;;
      *)      die "请手动安装 Python >= 3.11" ;;
    esac
    py=python3
  fi
  PY="$py"
  export PY
  ok "Python：$("$PY" -V 2>&1)"
}

# Node 20+：优先复用已安装版本，否则用 deb/rpm 官方源安装
ensure_node() {
  local need=20 cur=""
  if have_cmd node; then cur="$(node -v | sed 's/^v//')"; fi
  if [ -n "$cur" ] && version_ge "$cur" "$need"; then
    ok "Node：v$cur"
    return
  fi
  step "未找到 Node >= $need，开始安装 NodeSource 22 LTS"
  case "$OS_FAMILY" in
    debian)
      pkg_install ca-certificates curl gnupg
      curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
      pkg_install nodejs
      ;;
    rhel)
      curl -fsSL https://rpm.nodesource.com/setup_22.x | bash -
      pkg_install nodejs
      ;;
    *) die "请手动安装 Node.js >= 20" ;;
  esac
  need_cmd node
  ok "Node：$(node -v)"
}

# ---------- 代码 ----------
clone_or_die() {
  need_cmd git
  if [ -d "$PREFIX/.git" ]; then
    log "$PREFIX 已存在，跳过克隆（更新请执行 scripts/update.sh）"
    return
  fi
  step "克隆 $REPO -> $PREFIX"
  git clone --depth 50 -b "$BRANCH" "$REPO" "$PREFIX"
}

current_sha() {
  git -C "$PREFIX" rev-parse HEAD 2>/dev/null || echo "unknown"
}

# ---------- 后端依赖 ----------
venv_create() {
  if [ ! -x "$VENV/bin/python" ]; then
    step "创建虚拟环境 $VENV"
    "$PY" -m venv "$VENV"
  fi
  step "安装后端依赖"
  if have_cmd uv; then
    (cd "$BACKEND_DIR" && uv pip install --python "$VENV/bin/python" -r requirements.txt)
  else
    "$VENV/bin/python" -m pip install -q --upgrade pip wheel
    "$VENV/bin/python" -m pip install -q -r "$BACKEND_DIR/requirements.txt"
  fi
  ok "后端依赖就绪"
}

# ---------- 前端构建 ----------
web_build() {
  step "构建前端（npm）"
  (cd "$WEB_DIR" && npm ci --no-audit --fund=false 2>/dev/null || npm install --no-audit --fund=false)
  (cd "$WEB_DIR" && npm run build)
  ok "前端构建完成：$WEB_DIR/dist"
}

# ---------- systemd ----------
# 渲染 unit：把 deploy/choyeon-panel.service 里的占位路径替换为实际安装路径。
# 用 bash 自己的字符串替换而不是 sed：${var//pat/rep} 的替换段没有任何元字符语义，
# 路径里出现 `# & \ $ 反引号` 都不会改变结果。旧 sed 写法实测会被 CP_PREFIX 里的
# `#` 弄成语法错误（安装中断）、被 `&` 静默替换成整段匹配（unit 里写出
# /opt/panel/root/choyeon-panelv2 这种路径，服务起不来且毫无报错线索）。
render_unit() {
  local src="$PREFIX/deploy/choyeon-panel.service"
  [ -r "$src" ] || die "缺少 $src"
  step "安装 systemd unit -> $UNIT_FILE"
  local line
  : > "$UNIT_FILE.tmp" || die "无法写入 $UNIT_FILE.tmp"
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line//\/root\/choyeon-panel/$PREFIX}"
    case "$line" in
      Environment=CP_PORT=*) line="Environment=CP_PORT=$PORT" ;;
      Environment=CP_HOST=*) line="Environment=CP_HOST=$HOST" ;;
      Environment=CP_DATA_DIR=*) line="Environment=CP_DATA_DIR=$DATA_DIR" ;;
    esac
    printf '%s\n' "$line" >> "$UNIT_FILE.tmp"
  done < "$src"
  mv "$UNIT_FILE.tmp" "$UNIT_FILE"
  chmod 0644 "$UNIT_FILE"
}

unit_install() {
  have_systemd || { warn "未检测到 systemd（容器环境），跳过服务安装"; return; }
  render_unit
  systemctl daemon-reload
  systemctl enable "$SERVICE_NAME" >/dev/null 2>&1 || true
}

unit_restart() {
  have_systemd || { warn "无 systemd，跳过重启"; return; }
  systemctl restart "$SERVICE_NAME"
}

unit_is_active() {
  have_systemd || return 1
  systemctl is-active --quiet "$SERVICE_NAME"
}

# ---------- 健康检查 ----------
# 最多等待 $1 秒（默认 30），直到 /api/health 返回 ok
wait_health() {
  local timeout="${1:-30}" i=0
  local url="http://127.0.0.1:${PORT}/api/health"
  while [ "$i" -lt "$timeout" ]; do
    if have_cmd curl; then
      if curl -fsS --max-time 3 "$url" 2>/dev/null | grep -q '"ok":true'; then return 0; fi
    elif have_cmd wget; then
      if wget -qO- --timeout=3 "$url" 2>/dev/null | grep -q '"ok":true'; then return 0; fi
    else
      warn "无 curl/wget，跳过健康探测"; return 0
    fi
    i=$((i + 1)); sleep 1
  done
  return 1
}

# ---------- 数据备份 ----------
backup_data() { # backup_data <标签>
  local tag="${1:-manual}" ts
  ts="$(date +%Y%m%d-%H%M%S)"
  local dest="$BACKUP_DIR/manual"
  mkdir -p "$dest"
  local out="$dest/panel-${tag}-${ts}.tar.gz"
  local db="$DATA_DIR/panel.db"
  # 成员先收集到临时目录，最后**一次性**打包。
  # 旧写法是"先 tar -czf 出包，再 tar -rzf 追加 .env"，而 GNU tar 不能更新已压缩的归档：
  # 实测报 `tar: Cannot update compressed archives`（退出码 2），又被同一行的 `|| true` 吞掉，
  # 于是**每一份备份里都没有 .env**（CP_DATA_DIR / CP_PORT / CP_FILE_ROOTS 全丢），
  # 照 RUNBOOK 还原完才发现配置缺失。归档布局保持扁平（panel-*.db 在包根），
  # 与 backup.sh / RUNBOOK 里既有的还原命令一致。
  local stage dbfile
  stage="$(mktemp -d)" || die "无法创建临时目录（备份中止）"
  dbfile="panel-${ts}.db"
  local members=()
  if [ -f "$db" ]; then
    # SQLite 在线备份必须用 backup API，直接 cp 可能拷到写了一半的页。
    # 优先用 Python 内置 sqlite3（后端就是 Python，必然可用），其次 sqlite3 CLI，最后才退化到 cp。
    local py="${PY:-python3}"
    if "$py" -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' \
        "$db" "$stage/$dbfile" 2>/dev/null; then
      :
    elif have_cmd sqlite3 && sqlite3 "$db" ".backup \"$stage/$dbfile\"" 2>/dev/null; then
      :
    else
      warn "无法使用在线备份 API，退化为 cp（请确保此时无写入）"
      cp "$db" "$stage/$dbfile"
    fi
    members+=("$dbfile")
  fi
  if [ -f "$PREFIX/.env" ]; then
    cp "$PREFIX/.env" "$stage/.env"
    members+=(".env")
  fi
  if [ ${#members[@]} -eq 0 ]; then
    # 既无 db 也无 .env：补一个空包，避免下游 du/ls 拿到不存在的路径
    tar -czf "$out" -T /dev/null
  else
    tar -czf "$out" -C "$stage" "${members[@]}"
  fi || { rm -rf "$stage"; die "备份打包失败：$out"; }
  rm -rf "$stage"
  # 收尾自检：归档必须可读，且 .env 存在时必须真的在里面——不再靠"看起来成功"
  tar -tzf "$out" >/dev/null 2>&1 || die "备份归档不可读：$out"
  if [ -f "$PREFIX/.env" ] && ! tar -tzf "$out" | grep -qxF ".env"; then
    die "备份里缺 .env：$out"
  fi
  echo "$out"
}

prune_backups() { # 保留最近 $KEEP_BACKUPS 份
  local dir="$BACKUP_DIR/manual"
  [ -d "$dir" ] || return 0
  # 外层 || true 必需：开启 pipefail 时，ls 无匹配返回非 0 会让整条管道失败并中断调用方
  { ls -1t "$dir"/panel-*.tar.gz 2>/dev/null || true; } \
    | tail -n +$((KEEP_BACKUPS + 1)) \
    | while read -r f; do [ -n "$f" ] && rm -f "$f"; done
  return 0
}

# ---------- 交互 ----------
# read_value <提示> <默认值> -> 结果写入 REPLY
read_value() {
  local prompt="$1" default="$2" input=""
  if [ -t 0 ]; then
    read -r -p "$prompt${default:+ [$default]}: " input || true
  fi
  REPLY="${input:-$default}"
}

# 生成随机密码（32 位）
random_pass() {
  if have_cmd openssl; then openssl rand -base64 24 | tr -d '/+=' | head -c 32
  else tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32; fi
  printf '\n'
}

# ---------- 入口校验 ----------
# 放在 common.sh 末尾：五个脚本都 source 本文件，等于一处校验全部生效，
# 不必担心以后新增脚本时漏调用（漏掉一次就是静默的注入面）。
validate_config
