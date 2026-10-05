#!/usr/bin/env bash
# choyeon-panel 部署脚本公共函数库
# 用法：在各脚本中 `source "$(dirname "$0")/common.sh"`

set -euo pipefail

# ---------- 可覆盖的变量（均可通过环境变量注入） ----------
PREFIX="${CP_PREFIX:-/root/choyeon-panel}"
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
# 渲染 unit：把 deploy/choyeon-panel.service 里的占位路径替换为实际安装路径
render_unit() {
  local src="$PREFIX/deploy/choyeon-panel.service"
  [ -r "$src" ] || die "缺少 $src"
  step "安装 systemd unit -> $UNIT_FILE"
  sed -e "s#/root/choyeon-panel#$PREFIX#g" \
      -e "s#^Environment=CP_PORT=.*#Environment=CP_PORT=$PORT#" \
      -e "s#^Environment=CP_HOST=.*#Environment=CP_HOST=$HOST#" \
      -e "s#^Environment=CP_DATA_DIR=.*#Environment=CP_DATA_DIR=$DATA_DIR#" \
      "$src" > "$UNIT_FILE.tmp"
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
  if [ -f "$db" ]; then
    # SQLite 在线备份必须用 backup API，直接 cp 可能拷到写了一半的页。
    # 优先用 Python 内置 sqlite3（后端就是 Python，必然可用），其次 sqlite3 CLI，最后才退化到 cp。
    local py="${PY:-python3}"
    if "$py" -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()' \
        "$db" "$dest/panel-${ts}.db" 2>/dev/null; then
      :
    elif have_cmd sqlite3 && sqlite3 "$db" ".backup '$dest/panel-${ts}.db'" 2>/dev/null; then
      :
    else
      warn "无法使用在线备份 API，退化为 cp（请确保此时无写入）"
      cp "$db" "$dest/panel-${ts}.db"
    fi
    tar -czf "$out" -C "$dest" "panel-${ts}.db" 2>/dev/null || true
    rm -f "$dest/panel-${ts}.db"
  fi
  [ -f "$PREFIX/.env" ] && tar -rzf "$out" -C "$PREFIX" .env 2>/dev/null || true
  # 既无 db 也无 .env 时 tar 从未被创建，补一个空包，避免下游 du/ls 拿到不存在的路径
  [ -f "$out" ] || tar -czf "$out" -T /dev/null
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
