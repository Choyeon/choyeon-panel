#!/usr/bin/env bash
# choyeon-panel 卸载脚本
#   bash scripts/uninstall.sh           # 停止并禁用服务（保留数据）
#   bash scripts/uninstall.sh --purge   # 同时删除安装目录与数据（危险）
#
# ⚠️ 风险提醒：--purge 会删除 $PREFIX 与 $DATA_DIR，请先执行 scripts/backup.sh。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

PURGE=0
for a in "$@"; do [ "$a" = "--purge" ] && PURGE=1; done

require_root
detect_os

if [ "$PURGE" = "1" ]; then
  warn "即将删除：$PREFIX 与 $DATA_DIR"
  read_value "确认请输入 purge" ""
  [ "$REPLY" = "purge" ] || die "未确认，已取消"
  step "卸载前备份"
  BK="$(backup_data "pre-uninstall")"
  ok "备份：$BK"
fi

if have_systemd && systemctl list-unit-files 2>/dev/null | grep -q "^$SERVICE_NAME"; then
  step "停止并禁用 $SERVICE_NAME"
  systemctl stop "$SERVICE_NAME" 2>/dev/null || true
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  ok "服务已移除"
fi

if [ -f /etc/nginx/conf.d/choyeon-panel.conf ]; then
  rm -f /etc/nginx/conf.d/choyeon-panel.conf
  have_cmd nginx && nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  ok "nginx 配置已移除"
fi

if [ "$PURGE" = "1" ]; then
  rm -rf "$PREFIX" "$DATA_DIR"
  ok "已删除 $PREFIX 与 $DATA_DIR"
  warn "备份保留在 $BACKUP_DIR"
else
  ok "已卸载服务，数据保留在 $DATA_DIR，代码保留在 $PREFIX"
fi
