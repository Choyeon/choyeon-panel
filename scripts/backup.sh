#!/usr/bin/env bash
# choyeon-panel 数据备份脚本
#   bash scripts/backup.sh                # 备份到 $BACKUP_DIR/manual
#   CP_BACKUP_DIR=/mnt/nas bash scripts/backup.sh
# 建议配合 systemd timer 或 crontab：
#   0 4 * * * /root/choyeon-panel/scripts/backup.sh >> /var/log/choyeon-backup.log 2>&1
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

require_root
mkdir -p "$BACKUP_DIR/manual"

OUT="$(backup_data "${1:-manual}")"
prune_backups

SIZE="$(du -h "$OUT" 2>/dev/null | cut -f1 || echo '?')"
ok "备份完成：$OUT（$SIZE）"
log "保留最近 $KEEP_BACKUPS 份；还原示例："
log "  mkdir -p /tmp/restore && tar -xzf $OUT -C /tmp/restore"
log "  systemctl stop $SERVICE_NAME && cp /tmp/restore/panel-*.db $DATA_DIR/panel.db && systemctl start $SERVICE_NAME"
# 归档里同时含 .env（CP_PREFIX/.env）。它不在上面的还原命令里，覆盖与否要人来决定，
# 所以明确提示位置，避免"备份包里有 .env 但没人知道"。
# 必须写成 if：脚本在 set -e 下运行，末行 `[ -f x ] && log ...` 条件为假时
# 退出码是 1，会让整个 backup.sh 以失败收场，choyeonctl backup run 误报"备份失败"。
if [ -f "$PREFIX/.env" ]; then
  log "  配置备份在 /tmp/restore/.env，需要时：cp /tmp/restore/.env $PREFIX/.env && chmod 600 $PREFIX/.env"
fi
