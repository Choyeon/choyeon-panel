#!/usr/bin/env bash
# choyeon-panel 一键安装脚本（原生部署，不使用 Docker）
#
#   git clone https://github.com/Choyeon/choyeon-panel.git /root/choyeon-panel
#   cd /root/choyeon-panel && bash scripts/install.sh
#   或：CP_PREFIX=/opt/choyeon-panel bash scripts/install.sh
#
# 注意：本脚本 source 同目录的 common.sh，不能 `curl | bash` 直跑——先 clone 再执行。
#
# 可注入环境变量：CP_PREFIX / CP_REPO / CP_BRANCH / CP_PORT / CP_HOST /
#                 CP_DATA_DIR / CP_BACKUP_DIR / CP_SKIP_NGINX=1 / CP_SKIP_DEPS=1
# 管理员账号（非交互自动化）：CP_ADMIN_USER=admin + CP_ADMIN_PASSWORD_FILE=/path/to/0600
#                 口令只从文件读，不要写进环境变量或命令行参数
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

trap 'err "安装中断（第 $LINENO 行）"' ERR

log "choyeon-panel 安装开始"
log "安装目录：$PREFIX"

require_root
detect_os
[ "${CP_SKIP_DEPS:-0}" = "1" ] || pkg_update

# ---------- 1. 系统依赖 ----------
if [ "${CP_SKIP_DEPS:-0}" = "1" ]; then
  warn "已设置 CP_SKIP_DEPS=1，跳过系统依赖安装"
else
  step "安装系统依赖"
  case "$OS_FAMILY" in
    debian) pkg_install ca-certificates curl git sqlite3 nginx rsync ;;
    rhel)   pkg_install ca-certificates curl git sqlite3 nginx rsync ;;
    arch)   pkg_install ca-certificates curl git sqlite nginx rsync ;;
    suse)   pkg_install ca-certificates curl git sqlite3 nginx rsync ;;
    alpine) pkg_install ca-certificates curl git sqlite nginx rsync ;;
    *)      warn "未识别发行版（$OS_ID），请自行确认已安装：git / nginx / sqlite3" ;;
  esac
fi

ensure_python
ensure_node

# ---------- 2. 代码 ----------
clone_or_die
cd "$PREFIX"

# ---------- 3. 配置文件 ----------
# 生成与对齐只留一个写入者：ensure_env / sync_env / env_sync_all 都在 common.sh。
# 这里过去另有一个 render_env，从 .env.example 复制并把 CP_DATA_DIR / CP_PORT / CP_HOST 三个键
# 换成本次的值——和 sync_env 干的是同一件事，键集却少三个（CP_BACKUP_DIR / CP_APP_ROOT /
# CP_FILE_ROOTS），两个写入者迟早给出两个答案。chmod 也归 sync_env 自己管（它每次都重写文件）。
step "对齐 .env（缺失则从 .env.example 生成）"
if [ -f "$PREFIX/.env" ]; then
  log ".env 已存在：保留其它配置，只同步本次生效的部署参数"
fi
env_sync_all

mkdir -p "$DATA_DIR" "$BACKUP_DIR"
chmod 0700 "$DATA_DIR"
# 备份归档里含 .env（数据库口令、Telegram token 等），目录权限必须与数据目录一致，
# 否则 CP_BACKUP_DIR 指到 /mnt/nas、/var/www 之类位置时，密钥就成了全局可读。
chmod 0700 "$BACKUP_DIR"

# ---------- 4. 构建 ----------
venv_create
web_build

# ---------- 4.5 管理员账号 ----------
# 放在服务第一次启动之前。/api/auth/setup 是零用户态下唯一免认证的写端点：谁先
# POST 谁就是 admin。装完再让用户「打开网页注册」，这个窗口会一直留到他手工开
# 浏览器为止，而下面的 nginx 步骤还可能先把面板暴露到公网。安装阶段就把账号
# 建好，服务起来的那一刻窗口已经关上。
ADMIN_STATE="skipped"          # created | existing | skipped
ADMIN_USER=""

admin_count() {
  # 不用 sqlite3 CLI：CP_SKIP_DEPS=1 时它可能不存在，缺命令会让「已有账号」误判成 0。
  # venv 里的 Python 一定有，且只读打开，绝不替服务抢先建表。
  "$VENV/bin/python" -c '
import sqlite3, sys
from pathlib import Path
p = Path(sys.argv[1])
try:
    if not p.is_file():
        raise sqlite3.Error("no database yet")
    con = sqlite3.connect("file:" + str(p) + "?mode=ro", uri=True)
    print(con.execute("SELECT COUNT(*) FROM users").fetchone()[0])
except Exception:
    print(0)
' "$DATA_DIR/panel.db"
}

# 与 backend/app/security.py 的 USERNAME_RE / password_error 同一把尺子：
# 用户名 3-32 位 [A-Za-z0-9_-]，口令 8-128 位。这里先挡一道，CLI 还会再验一次。
valid_admin_name() { [[ "$1" =~ ^[A-Za-z0-9_-]{3,32}$ ]]; }
valid_admin_password() {
  local n=${#1}
  [ "$n" -ge 8 ] && [ "$n" -le 128 ] && [[ "$1" != *$'\n'* ]]
}

create_admin_now() {
  step "设置管理员账号"
  if [ "$(admin_count)" != "0" ]; then
    ok "数据库已有账号，跳过创建（直接登录即可）"
    ADMIN_STATE="existing"
    return 0
  fi

  local user="" pw="" pw2="" choice="n"
  # 非交互自动化的凭据只认 0600 文件：stdin 已被脚本本体占用，环境变量与命令行
  # 参数会把口令留在 /proc/<pid>/environ、ps 与 shell 历史里。
  local pwfile="${CP_ADMIN_PASSWORD_FILE:-}"
  if [ -n "$pwfile" ]; then
    if [ ! -r "$pwfile" ]; then
      warn "口令文件不可读：$pwfile；跳过创建"
      return 0
    fi
    local mode=""
    mode="$(stat -c '%a' "$pwfile" 2>/dev/null || true)"
    if [ -n "$mode" ] && [ "$mode" != "600" ] && [ "$mode" != "400" ]; then
      warn "口令文件 $pwfile 权限为 $mode，建议 chmod 600（仅属主可读）"
    fi
    user="${CP_ADMIN_USER:-admin}"
    pw="$(< "$pwfile")"
    pw2="$pw"
    choice="y"
  elif [ -t 0 ]; then
    read_value "现在创建管理员账号？(Y/n)" "Y"
    choice="${REPLY:-Y}"
    if [ "$choice" = "y" ] || [ "$choice" = "Y" ]; then
      read_value "管理员用户名（3-32 位字母、数字、下划线或连字符）" "admin"
      user="${REPLY:-admin}"
      # -s 静默：口令不回显、不进 argv、不进 shell history
      printf '设置密码（8-128 位，输入不回显）：' >&2
      IFS= read -rs pw || true
      printf '\n再输入一次：' >&2
      IFS= read -rs pw2 || true
      printf '\n' >&2
    fi
  fi

  if [ "$choice" != "y" ] && [ "$choice" != "Y" ]; then
    warn "跳过创建管理员。把面板暴露到公网之前必须建号，两种方式二选一："
    warn "  A) 打开面板地址，首次访问会出现「初始化面板」引导创建（仅本机/本机反代可用）"
    warn "  B) printf '%s' \"\$PW\" | choyeonctl user create admin --password-stdin --json"
    return 0
  fi

  if ! valid_admin_name "$user"; then
    warn "用户名不合法（只允许 3-32 位字母、数字、下划线、连字符）：跳过创建"
    return 0
  fi
  if ! valid_admin_password "$pw"; then
    warn "密码不合法（需要 8-128 位）：跳过创建"
    return 0
  fi
  if [ "$pw" != "$pw2" ]; then
    warn "两次输入的密码不一致：跳过创建"
    return 0
  fi

  local out=""
  if out="$(printf '%s\n' "$pw" | CP_DATA_DIR="$DATA_DIR" "$VENV/bin/python" \
        "$BACKEND_DIR/cli.py" user create "$user" --password-stdin --json 2>&1)"; then
    ok "管理员 $user 已创建（口令只经 stdin 传入，未写入命令行或日志）"
    ADMIN_STATE="created"
    ADMIN_USER="$user"
  else
    warn "创建管理员失败，安装继续；请在验收前手工执行："
    warn "  printf '%s' \"\$PW\" | choyeonctl user create admin --password-stdin --json"
    warn "  CLI 输出：$out"
  fi
  unset pw pw2
}
create_admin_now

# ---------- 5. systemd ----------
if have_systemd; then
  unit_install
  if [ -f "$UNIT_FILE" ] && command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$UNIT_FILE" >/dev/null 2>&1 || warn "systemd-analyze 给出告警（不影响运行）：systemd-analyze verify $UNIT_FILE"
  fi
  unit_restart
  rc=0
  wait_health 30 || rc=$?
  case "$rc" in
    0) ok "服务已启动并通过健康检查" ;;
    2) warn "服务已启动，但健康状态没核对成（无 curl/wget）：请安装 curl 后执行 choyeonctl status --json 确认 healthy" ;;
    *) err "健康检查失败，查看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
       exit 1 ;;
  esac
else
  warn "无 systemd：请手动启动 -> $VENV/bin/python -m app.main（工作目录 $BACKEND_DIR）"
fi

# ---------- 5.5 CLI ----------
ln -sf "$PREFIX/bin/choyeonctl" /usr/local/bin/choyeonctl
ok "CLI 已安装：choyeonctl（先跑 choyeonctl doctor 看自检结果）"

# ---------- 6. nginx（可选） ----------
PANEL_URL="http://127.0.0.1:$PORT"
if [ "${CP_SKIP_NGINX:-0}" = "1" ] || ! have_cmd nginx; then
  log "跳过 nginx 配置（CP_SKIP_NGINX=1 或系统无 nginx）"
else
  read_value "是否为面板配置 nginx 反代？需要已解析到本机的域名（留空跳过）" ""
  DOMAIN="${REPLY:-}"
  if [ -n "$DOMAIN" ]; then
    # 先校验再落地：DOMAIN 直接进 nginx 配置。含 `#`（sed 分隔符）会让 sed 语法错误，
    # 含 `&` 会被替换成整段匹配、写出错误的 server_name——两种都表现为 "nginx -t 失败"
    # 却看不出原因。改成 bash 字符串替换后不再有元字符问题，这里只挡非法字符。
    case "$DOMAIN" in
      *[!A-Za-z0-9.-]*) die "域名不合法：${DOMAIN}（只允许字母、数字、点与连字符）" ;;
    esac
    step "写入 /etc/nginx/conf.d/choyeon-panel.conf"
    NGINX_CONF=/etc/nginx/conf.d/choyeon-panel.conf
    render_nginx() {
      local line
      while IFS= read -r line || [ -n "$line" ]; do
        line="${line//panel.example.com/$DOMAIN}"
        line="${line//127.0.0.1:3210/127.0.0.1:$PORT}"
        printf '%s\n' "$line"
      done < "$PREFIX/deploy/nginx-panel.conf.example"
    }
    # 模板的 443 段引用 /etc/letsencrypt/live/$DOMAIN/*.pem，而证书要 certbot 跑完才有，
    # 所以全新域名首次安装必须先落一份纯 80 的引导配置（含 ACME 校验路径），
    # nginx -t 才可能通过；随后 certbot --nginx 会把它原地升级成 TLS + 80→443 跳转。
    bootstrap_http() {
      printf '%s\n' \
        "server {" \
        "    listen 80;" \
        "    server_name $DOMAIN;" \
        "    client_max_body_size 20m;" \
        "    access_log /var/log/nginx/choyeon-panel.access.log;" \
        "    error_log  /var/log/nginx/choyeon-panel.error.log warn;" \
        "    location ^~ /.well-known/acme-challenge/ { root /var/www/html; }" \
        "    location ~ ^/api/(files/download|terminal|system/services/[^/]+/logs|apps/[0-9]+/logs)$ {" \
        "        access_log off;" \
        "        proxy_pass http://127.0.0.1:$PORT;" \
        "        proxy_set_header Host \$host;" \
        "        proxy_set_header X-Real-IP \$remote_addr;" \
        "        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;" \
        "        proxy_set_header X-Forwarded-Proto \$scheme;" \
        '        proxy_set_header Connection "upgrade";' \
        "        proxy_http_version 1.1;" \
        "        proxy_buffering off;" \
        "        proxy_read_timeout 24h;" \
        "    }" \
        "    location / {" \
        "        proxy_pass http://127.0.0.1:$PORT;" \
        "        proxy_set_header Host \$host;" \
        "        proxy_set_header X-Real-IP \$remote_addr;" \
        "        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;" \
        "        proxy_set_header X-Forwarded-Proto \$scheme;" \
        "        proxy_http_version 1.1;" \
        "        proxy_set_header Upgrade \$http_upgrade;" \
        '        proxy_set_header Connection "upgrade";' \
        "        proxy_buffering off;" \
        "        proxy_cache off;" \
        "        proxy_read_timeout 24h;" \
        "        proxy_send_timeout 24h;" \
        "    }" \
        "}"
    }
    load_nginx() {
      # 不能写成 `nginx -t && systemctl reload nginx`：AND-OR 列表里非末位命令失败
      # 是被 set -e 豁免的，nginx -t 挂了脚本照样往下跑，于是"配置已生效"是句假话。
      nginx -t || return 1
      # 只用 reload：restart 会掐断正在跑的终端 WebSocket
      systemctl reload nginx || return 1
    }
    if [ -f "$NGINX_CONF" ]; then
      # certbot --nginx 把 SSL 段直接写进这个文件；无条件重写等于把 HTTPS 打回裸模板
      cp -a "$NGINX_CONF" "$NGINX_CONF.bak-$(date +%Y%m%d%H%M%S)"
      warn "已存在 $NGINX_CONF：保留现有配置未覆盖（已备份 .bak-*）；确需重新生成请先删除该文件再重跑"
      if load_nginx; then ok "nginx 现有配置校验通过并已 reload"; else warn "nginx 配置校验未通过，请执行 nginx -t 排错后 systemctl reload nginx"; fi
    elif [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
      render_nginx > "$NGINX_CONF"
      if load_nginx; then ok "nginx 配置已生效（TLS：复用已有证书）"; PANEL_URL="https://$DOMAIN"
      else warn "nginx 配置校验未通过，已保留文件未 reload"; fi
    else
      bootstrap_http > "$NGINX_CONF"
      if load_nginx; then ok "nginx 已生效（当前 HTTP，申请证书后自动升级 HTTPS）"; PANEL_URL="http://$DOMAIN"
      else warn "nginx 配置校验未通过，请执行 nginx -t 排错后 systemctl reload nginx"; fi
    fi
    if have_cmd certbot && [ "$PANEL_URL" = "http://$DOMAIN" ]; then
      read_value "是否为 $DOMAIN 申请 Let's Encrypt 证书？(y/N)" "N"
      if [ "${REPLY:-N}" = "y" ] || [ "${REPLY:-N}" = "Y" ]; then
        if certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" && load_nginx; then
          PANEL_URL="https://$DOMAIN"
          ok "证书签发成功，面板走 https://$DOMAIN"
        else
          warn "证书申请失败（多为域名尚未解析到本机）；面板仍按 HTTP 提供服务，修复后手动执行：certbot --nginx -d $DOMAIN"
        fi
      else
        warn "未申请证书：$DOMAIN 将以明文 HTTP 暴露管理面板，建议尽快补 TLS"
      fi
    fi
  else
    log "跳过 nginx（面板仅监听 $HOST:$PORT，可后续用 scripts/ 下的示例配置接入）"
  fi
fi

# ---------- 7. 收尾提示 ----------
# 管理员状态只认上面那一步的结果：旧写法依赖 sqlite3 命令存在且 .db 恰好在
# $DATA_DIR/panel.db，缺命令或 CP_DATA_DIR 指到别处时会把「已有账号」误报成
# 「请打开网页注册」，用户对着登录页以为安装坏了。
case "$ADMIN_STATE" in
  created)  ADMIN_HINT="管理员 $ADMIN_USER 已在安装时创建，打开上面的地址直接登录" ;;
  existing) ADMIN_HINT="数据库已有管理员账号，打开上面的地址直接登录" ;;
  *)        ADMIN_HINT="尚未创建管理员：先按上面两种方式之一建号，再打开地址登录" ;;
esac

cat <<EOF

${C_GREEN}安装完成${C_RESET}
  目录：   $PREFIX
  数据：   $DATA_DIR
  备份：   $BACKUP_DIR
  服务：   systemctl status $SERVICE_NAME
  日志：   journalctl -u $SERVICE_NAME -f
  访问：   $PANEL_URL
  $ADMIN_HINT

后续建议：
  1) 若未配置 HTTPS，务必接入 TLS；面板默认只听 $HOST:$PORT，不要用明文 HTTP 暴露管理入口；
  2) 用 ufw/firewalld 只放行 80/443，不要直接暴露 $PORT；
  3) 登录后立刻在「设置」里开启两步验证，并跑一次 choyeonctl doctor --json 看 counts.fail 是否为 0；
  4) 更新版本执行：$PREFIX/scripts/update.sh
EOF
