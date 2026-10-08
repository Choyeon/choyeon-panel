#!/usr/bin/env bash
# choyeon-panel 卸载脚本
#   bash scripts/uninstall.sh           # 停止并禁用服务（保留数据）
#   bash scripts/uninstall.sh --purge   # 同时删除安装目录与数据（危险）
#
# ⚠️ 风险提醒：--purge 会删除 $PREFIX 与 $DATA_DIR，请先执行 scripts/backup.sh。
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

# 参数解析必须显式拒绝未知选项。旧写法 `for a in "$@"; do [ "$a" = "--purge" ] && PURGE=1; done`
# 有两个问题：
#   1) `--purge=1`、`-purge`、`--purge2` 这类打错的形式被静默忽略，用户以为在执行危险删除，
#      实际只走了"保留数据"的分支（实测 --purge=1 后 $PREFIX 仍在）；
#   2) 反过来，确认步骤一旦通过就是 rm -rf，没有第二次拦截。
PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -h|--help)
      sed -n '2,6p' "$0"
      exit 0
      ;;
    *) die "未知参数：$a（可用：--purge / --help）" ;;
  esac
done

require_root
detect_os

if [ "$PURGE" = "1" ]; then
  # 删除前拦一道：validate_config 的字符白名单挡不住"路径本身填错"。
  # 实测 CP_PREFIX=/ 时确认后执行 rm -rf / /var（用 rm shim 验证，未真删）。
  _assert_deletable CP_PREFIX "$PREFIX"
  _assert_deletable CP_DATA_DIR "$DATA_DIR"
  _assert_backup_outside "$BACKUP_DIR" "$PREFIX" "$DATA_DIR"
  warn "即将删除：$PREFIX 与 $DATA_DIR"
  _require_tty "--purge"
  read_value "确认请输入 purge" ""
  [ "$REPLY" = "purge" ] || die "未确认，已取消"
  step "卸载前备份"
  BK="$(backup_data "pre-uninstall")"
  ok "备份：$BK"
fi

# 停止服务：`|| true` 会把"停不掉"一起吞掉，紧接着 rm 掉 unit 文件后
# systemd 再也管不到那个进程——实测进程仍在跑、端口仍被占用，
# 而面板显示"服务已移除"。这里要求真的停下来。
if have_systemd && systemctl list-unit-files --no-legend --no-pager "$SERVICE_NAME.service" 2>/dev/null | awk '{print $1}' | grep -qxF -- "$SERVICE_NAME.service"; then
  step "停止并禁用 $SERVICE_NAME"
  if ! systemctl stop "$SERVICE_NAME" 2>/dev/null; then
    LEFT="$(systemctl show -p MainPID --value "$SERVICE_NAME" 2>/dev/null || echo 0)"
    warn "systemctl stop 失败（主进程 PID=${LEFT:-0} 可能仍在运行）"
    warn "为避免数据被仍在写入的进程影响、且删掉 unit 后就再也停不下来，已中止卸载"
    warn "排查：journalctl -u $SERVICE_NAME -n 50 --no-pager"
    die "服务未能停止，卸载中止"
  fi
  systemctl disable "$SERVICE_NAME" 2>/dev/null || true
  rm -f "$UNIT_FILE"
  systemctl daemon-reload
  ok "服务已移除"
fi

# CLI 软链接：install.sh 建的是 /usr/local/bin/choyeonctl -> $PREFIX/bin/choyeonctl。
# 不删就是悬空链接（实测 `choyeonctl --help` 报 "No such file or directory"），
# 而且下次别人装别的东西时，这个名字已经"存在"了。
if [ -L /usr/local/bin/choyeonctl ]; then
  rm -f /usr/local/bin/choyeonctl
  ok "已移除 CLI 软链接 /usr/local/bin/choyeonctl"
fi

if [ -f /etc/nginx/conf.d/choyeon-panel.conf ]; then
  rm -f /etc/nginx/conf.d/choyeon-panel.conf
  have_cmd nginx && nginx -t >/dev/null 2>&1 && systemctl reload nginx 2>/dev/null || true
  ok "nginx 配置已移除"
fi

if [ "$PURGE" = "1" ]; then
  # 两个路径都要删：DATA_DIR 默认在 PREFIX 里，重复传没问题；
  # 但 CP_DATA_DIR 指到别处时必须都删到，否则"卸载"后数据库还在。
  rm -rf "$PREFIX"
  [ "$DATA_DIR" = "$PREFIX/data" ] || rm -rf "$DATA_DIR"
  ok "已删除 $PREFIX 与 $DATA_DIR"
  warn "备份保留在 $BACKUP_DIR"
else
  ok "已卸载服务，数据保留在 $DATA_DIR，代码保留在 $PREFIX"
fi

# 面板托管的应用单元（/etc/systemd/system/panel-*.service）不在这里自动删除：
# 它们指向的是 CP_APP_ROOT（默认 /root/www）下的应用目录，而卸载面板不该
# 顺手停掉用户正在对外服务的网站。但必须报告——实测 --purge 之后
# panel-myapp.service 依旧 active，而它的工作目录已随 $PREFIX 的假设一起消失时，
# systemd 会按 Restart= 无限重试一个不存在的东西。
if have_systemd; then
  # `|| true` 是必需的：list-unit-files 在没有匹配单元时退出码为 1（实测），
  # pipefail 下整条赋值语句会失败，卸载明明做完了却以非零码收场。
  APP_UNITS="$( { systemctl list-unit-files --no-legend --no-pager 'panel-*.service' 2>/dev/null || true; } \
    | awk '{print $1}' | tr '\n' ' ' )"
  if [ -n "${APP_UNITS// /}" ]; then
    warn "检测到面板托管的应用单元仍在运行，卸载不会自动停止它们（那是你的网站）："
    warn "  $APP_UNITS"
    warn "需要一并清理时：systemctl disable --now <单元> && rm -f /etc/systemd/system/<单元> && systemctl daemon-reload"
  fi
fi
