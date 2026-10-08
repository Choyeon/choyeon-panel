#!/usr/bin/env bash
# choyeon-panel 一键安装脚本（原生部署，不使用 Docker）
#
#   curl -fsSL https://raw.githubusercontent.com/Choyeon/choyeon-panel/main/scripts/install.sh | bash
#   或：CP_PREFIX=/opt/choyeon-panel bash scripts/install.sh
#
# 可注入环境变量：CP_PREFIX / CP_REPO / CP_BRANCH / CP_PORT / CP_HOST /
#                 CP_DATA_DIR / CP_BACKUP_DIR / CP_SKIP_NGINX=1 / CP_SKIP_DEPS=1
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
    *)      warn "未识别发行版，请自行确认已安装：git / nginx / sqlite3" ;;
  esac
fi

ensure_python
ensure_node

# ---------- 2. 代码 ----------
clone_or_die
cd "$PREFIX"

# ---------- 3. 配置文件 ----------
if [ ! -f "$PREFIX/.env" ]; then
  step "生成 .env（从 .env.example）"
  # 同 render_unit：用 bash 字符串替换而非 sed，避免 $DATA_DIR 里的 sed 元字符改坏内容
  render_env() {
    local line
    while IFS= read -r line || [ -n "$line" ]; do
      case "$line" in
        CP_DATA_DIR=*) line="CP_DATA_DIR=$DATA_DIR" ;;
        CP_PORT=*) line="CP_PORT=$PORT" ;;
        CP_HOST=*) line="CP_HOST=$HOST" ;;
      esac
      printf '%s\n' "$line"
    done < "$PREFIX/.env.example"
  }
  render_env > "$PREFIX/.env"
  chmod 0600 "$PREFIX/.env"
  ok "已生成 $PREFIX/.env（权限 0600）"
else
  log ".env 已存在，保留原配置"
fi

mkdir -p "$DATA_DIR" "$BACKUP_DIR"
chmod 0700 "$DATA_DIR"
# 备份归档里含 .env（数据库口令、Telegram token 等），目录权限必须与数据目录一致，
# 否则 CP_BACKUP_DIR 指到 /mnt/nas、/var/www 之类位置时，密钥就成了全局可读。
chmod 0700 "$BACKUP_DIR"

# ---------- 4. 构建 ----------
venv_create
web_build

# ---------- 5. systemd ----------
if have_systemd; then
  unit_install
  if [ -f "$UNIT_FILE" ] && command -v systemd-analyze >/dev/null 2>&1; then
    systemd-analyze verify "$UNIT_FILE" >/dev/null 2>&1 || warn "systemd-analyze 给出告警（不影响运行）：systemd-analyze verify $UNIT_FILE"
  fi
  unit_restart
  if wait_health 30; then
    ok "服务已启动并通过健康检查"
  else
    err "健康检查失败，查看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
    exit 1
  fi
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
ADMIN_HINT="请打开 $PANEL_URL 完成管理员注册（首次访问会引导创建）"
if [ -f "$DATA_DIR/panel.db" ] && have_cmd sqlite3; then
  if [ "$(sqlite3 "$DATA_DIR/panel.db" 'SELECT COUNT(*) FROM users;' 2>/dev/null || echo 0)" != "0" ]; then
    ADMIN_HINT="管理员已存在，直接登录即可"
  fi
fi

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
  1) 若未配置 HTTPS，务必执行 scripts/install.sh 的 nginx 步骤或手动接入 TLS；
  2) 用 ufw/firewalld 只放行 80/443，不要直接暴露 $PORT；
  3) 更新版本执行：$PREFIX/scripts/update.sh
EOF
