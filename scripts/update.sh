#!/usr/bin/env bash
# choyeon-panel 升级脚本：拉代码 → 备份 → 重装依赖 → 构建前端 → 重启 → 健康探测
# 健康检查失败时自动回滚到升级前的 commit 并重启。
#
#   bash scripts/update.sh            # 升级到 origin/main 最新
#   CP_BRANCH=v1.1.x bash scripts/update.sh
#   bash scripts/update.sh --no-pull  # 不拉代码，仅重建并重启（改配置后常用）
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

DO_PULL=1
for a in "$@"; do [ "$a" = "--no-pull" ] && DO_PULL=0; done

require_root
detect_os
[ -d "$PREFIX/.git" ] || die "$PREFIX 不是 git 仓库，请先执行 scripts/install.sh"

OLD_SHA="$(current_sha)"
log "升级开始：当前 commit ${OLD_SHA:0:8}"

# ---------- 1. 备份（回滚依据） ----------
step "备份数据"
BK="$(backup_data "pre-update")"
prune_backups
ok "备份：$BK"

# ---------- 2. 拉代码 ----------
if [ "$DO_PULL" = "1" ]; then
  step "拉取 $BRANCH"
  git -C "$PREFIX" fetch --depth 50 origin "$BRANCH" 2>/dev/null || git -C "$PREFIX" fetch origin "$BRANCH"
  git -C "$PREFIX" checkout "$BRANCH" >/dev/null 2>&1 || true
  git -C "$PREFIX" reset --hard "origin/$BRANCH"
else
  warn "--no-pull：跳过代码拉取"
fi
NEW_SHA="$(current_sha)"
[ "$NEW_SHA" = "$OLD_SHA" ] && log "代码无变化，仍执行依赖与前端重建"
ok "目标 commit ${NEW_SHA:0:8}"

# ---------- 3. 依赖与构建 ----------
NEED_DEPS=1
if [ "$DO_PULL" = "1" ] && [ "$NEW_SHA" != "$OLD_SHA" ]; then
  if ! git -C "$PREFIX" diff --name-only "$OLD_SHA" "$NEW_SHA" | grep -qE '^(backend/(requirements\.txt|pyproject\.toml)|web/package-lock\.json)'; then
    NEED_DEPS=0
  fi
fi

if [ "$NEED_DEPS" = "1" ]; then
  ensure_python
  venv_create
  ensure_node
  (cd "$WEB_DIR" && npm ci --no-audit --fund=false 2>/dev/null || npm install --no-audit --fund=false)
else
  log "依赖清单未变化，跳过依赖安装"
fi
web_build

# ---------- 4. 重启与校验 ----------
if have_systemd; then
  render_unit 2>/dev/null || true
  systemctl daemon-reload
fi

restart_and_check() {
  unit_restart
  if wait_health 40; then
    ok "健康检查通过"
    return 0
  fi
  return 1
}

if ! restart_and_check; then
  err "升级后健康检查失败，开始回滚"
  err "最近日志："
  have_systemd && journalctl -u "$SERVICE_NAME" -n 20 --no-pager || true
  if [ "$DO_PULL" = "1" ] && [ "$NEW_SHA" != "$OLD_SHA" ]; then
    step "回滚代码到 ${OLD_SHA:0:8}"
    git -C "$PREFIX" reset --hard "$OLD_SHA"
    web_build
    unit_restart
    if wait_health 40; then ok "已回滚到 ${OLD_SHA:0:8} 且服务恢复正常"; else err "回滚后仍未就绪，请人工介入（数据备份：$BK）"; fi
  fi
  exit 1
fi

ok "升级完成：${OLD_SHA:0:8} -> ${NEW_SHA:0:8}"
have_systemd && systemctl --no-pager -l status "$SERVICE_NAME" | head -n 6 || true
