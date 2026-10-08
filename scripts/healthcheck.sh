#!/usr/bin/env bash
# choyeon-panel 健康检查脚本（供 crontab / 监控系统 / 升级后校验使用）
#   退出码：0=健康  1=不健康
#   bash scripts/healthcheck.sh           # 人类可读输出
#   bash scripts/healthcheck.sh --quiet   # 仅用退出码，不打印
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

QUIET=0
for a in "$@"; do [ "$a" = "--quiet" ] && QUIET=1; done

say() { [ "$QUIET" = "1" ] || printf '%s\n' "$*"; }

FAILED=0
check() { # check <描述> <命令...>
  local desc="$1"; shift
  if "$@" >/dev/null 2>&1; then
    say "  ${C_GREEN}PASS${C_RESET} $desc"
  else
    say "  ${C_RED}FAIL${C_RESET} $desc"
    FAILED=$((FAILED + 1))
  fi
}

say "choyeon-panel 健康检查（$PREFIX）"

# 1. 服务进程状态
if have_systemd; then
  check "systemd 单元 active" systemctl is-active --quiet "$SERVICE_NAME"
else
  say "  ${C_YELLOW}SKIP${C_RESET} 无 systemd"
fi

# 2. 端口监听
if have_cmd ss; then
  check "端口 $PORT 已监听" ss -ltn "( sport = :$PORT )"
elif have_cmd netstat; then
  # 不能写成 check "..." netstat -ltn：那只判断 netstat 能否运行（恒 0），
  # 端口没监听也会报 PASS，等于在最需要报警的机器上装死。
  netstat_listening() { netstat -ltn 2>/dev/null | grep -Eq "[:.]$PORT([[:space:]]|\$)"; }
  check "端口 $PORT 已监听" netstat_listening
else
  say "  ${C_YELLOW}SKIP${C_RESET} 无 ss/netstat"
fi

# 3. HTTP 健康接口
if have_cmd curl; then
  BODY="$(curl -fsS --max-time 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || echo '')"
  if printf '%s' "$BODY" | grep -q '"ok":true'; then
    say "  ${C_GREEN}PASS${C_RESET} /api/health -> $BODY"
  else
    say "  ${C_RED}FAIL${C_RESET} /api/health 无响应"
    FAILED=$((FAILED + 1))
  fi
else
  say "  ${C_YELLOW}SKIP${C_RESET} 无 curl"
fi

# 4. 数据库可写
if [ -f "$DATA_DIR/panel.db" ] && have_cmd sqlite3; then
  # 路径必须走参数传递，不能拼进 bash -c 的字符串：
  # 旧写法 `bash -c "sqlite3 '$DATA_DIR/panel.db' ..."` 在 CP_PREFIX 含单引号时
  # （例如 /root/it's-panel）引号被提前闭合，轻则误报 FAIL，重则拼出可执行片段。
  check "SQLite 完整性" bash -c 'sqlite3 "$1" "PRAGMA integrity_check;" | grep -q "^ok$"' \
    healthcheck "$DATA_DIR/panel.db"
else
  say "  ${C_YELLOW}SKIP${C_RESET} 无 sqlite3 或数据库未初始化"
fi

# 5. 磁盘余量（<5% 报警）
if [ -d "$DATA_DIR" ]; then
  USAGE="$(df -P "$DATA_DIR" | awk 'NR==2 {gsub("%","",$5); print $5}')"
  if [ -n "$USAGE" ] && [ "$USAGE" -ge 95 ]; then
    say "  ${C_RED}FAIL${C_RESET} 磁盘使用率 ${USAGE}%（阈值 95%）"
    FAILED=$((FAILED + 1))
  else
    say "  ${C_GREEN}PASS${C_RESET} 磁盘使用率 ${USAGE:-?}%"
  fi
fi

if [ "$FAILED" -gt 0 ]; then
  say "结论：${C_RED}不健康（$FAILED 项失败）${C_RESET}"
  exit 1
fi
say "结论：${C_GREEN}健康${C_RESET}"
exit 0
