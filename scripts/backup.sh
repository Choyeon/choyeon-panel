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
