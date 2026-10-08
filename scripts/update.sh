#!/usr/bin/env bash
# choyeon-panel 升级脚本：拉代码 → 备份 → 重装依赖 → 构建前端 → 重启 → 健康探测
# 健康检查失败时自动回滚到升级前的 commit 并重启。
#
#   bash scripts/update.sh            # 升级到 origin/main 最新
#   CP_BRANCH=v1.1.x bash scripts/update.sh
#   bash scripts/update.sh --no-pull  # 不拉代码，仅重建并重启（改配置后常用）
set -euo pipefail

# 本脚本会在第 2 步用 git reset --hard 把自己和 common.sh 原地换掉，两个后果：
# 1) bash 是按字节偏移边读边执行的，文件被替换后剩下的行取自新文件的同一偏移，
#    表现是莫名其妙的语法错误或半条命令；
# 2) common.sh 早就 source 进内存了，本次升级跑的还是旧函数——刚拉下来的修复要等
#    下一次升级才生效（实测：render_unit 新增的 CP_BACKUP_DIR/CP_APP_ROOT 这次没落地）。
# 所以先把自身和 common.sh 拷进临时目录，再从副本 exec，整轮用同一套代码。
if [ -z "${CP_UPDATE_SNAPSHOT:-}" ]; then
  _snap="$(mktemp -d "${TMPDIR:-/tmp}/choyeon-update-XXXXXX")"
  mkdir -p "$_snap/scripts"
  _here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cp "$_here/update.sh" "$_here/common.sh" "$_snap/scripts/"
  exec env CP_UPDATE_SNAPSHOT="$_snap" bash "$_snap/scripts/update.sh" "$@"
fi
trap 'rm -rf "$CP_UPDATE_SNAPSHOT"' EXIT

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
  git -C "$PREFIX" fetch --depth 50 origin "$BRANCH" 2>/dev/null || git -C "$PREFIX" fetch origin "$BRANCH" \
    || die "无法从 origin 拉取 $BRANCH（检查网络 / 分支名 / CP_REPO）"
  # 旧写法是 `git checkout "$BRANCH" || true`，吞掉切换失败后紧跟着
  # `git reset --hard origin/$BRANCH`——reset 动的是**当前所在分支的指针**。
  # 实测：停在 main 时切 v2 失败，reset 后 main 直接指到 v2 的 commit，
  # 分支关系被悄悄改写，回滚时的 `reset --hard $OLD_SHA` 又把 main 挪回去，
  # 现场彻底说不清。所以切换必须成功，失败就停，绝不带着未知 HEAD 继续。
  if ! git -C "$PREFIX" checkout "$BRANCH" >/dev/null 2>&1; then
    # 本地分支不存在时，基于刚 fetch 下来的远端 ref 建一个（不留 detached HEAD）
    git -C "$PREFIX" checkout -B "$BRANCH" "origin/$BRANCH" >/dev/null 2>&1 \
      || die "无法切换到分支 $BRANCH（工作区可能有未提交改动），升级中止"
  fi
  HEAD_BR="$(git -C "$PREFIX" rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
  [ "$HEAD_BR" = "$BRANCH" ] || die "HEAD 不在 $BRANCH 上（实际 $HEAD_BR），升级中止以免 reset 改错分支"
  git -C "$PREFIX" reset --hard "origin/$BRANCH" >/dev/null \
    || die "reset --hard origin/$BRANCH 失败，升级中止"
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
  # 前端依赖不在此处单独装：web_build 里的 npm ci 已带失败告警，重复一份只会多一份静默回退
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
    if [ "$NEED_DEPS" = "1" ]; then
      # pip 只升不卸：旧代码配上新版依赖包照样起不来，回滚得连依赖一起回，
      # 否则"自动回滚"只是把仓库指回旧 commit，健康检查仍然失败，只能人工介入。
      step "回滚后端依赖（按 ${OLD_SHA:0:8} 的 requirements 重装）"
      venv_create
      (cd "$WEB_DIR" && npm ci --no-audit --fund=false) \
        || warn "前端依赖回退失败，dist 可能与旧版本不完全匹配"
    fi
    web_build
    unit_restart
    if wait_health 40; then ok "已回滚到 ${OLD_SHA:0:8} 且服务恢复正常"; else err "回滚后仍未就绪，请人工介入（数据备份：$BK）"; fi
  fi
  exit 1
fi

ok "升级完成：${OLD_SHA:0:8} -> ${NEW_SHA:0:8}"
have_systemd && systemctl --no-pager -l status "$SERVICE_NAME" | head -n 6 || true
