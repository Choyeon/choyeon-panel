# choyeon-panel 运维手册（RUNBOOK）

> 面向"线上出事了怎么办"。安装与开发请看 [README.md](./README.md)。
> 默认安装路径 `/root/choyeon-panel`，服务名 `choyeon-panel`，后端监听 `127.0.0.1:3210`。

---

## 0. 三十秒速查

| 我想做的事 | 命令 |
| --- | --- |
| 看服务在不在 | `systemctl status choyeon-panel` |
| 看实时日志 | `journalctl -u choyeon-panel -f` |
| 一键健康自检 | `choyeonctl doctor`（等价：`/root/choyeon-panel/scripts/healthcheck.sh`） |
| 面板状态一览 | `choyeonctl status --json` |
| 重启 | `systemctl restart choyeon-panel` |
| 备份 | `choyeonctl backup run` |
| 升级（失败自动回滚） | `choyeonctl upgrade` |
| 重置管理员口令 | `choyeonctl user reset admin --password '<新口令>'` |
| 卸载 | `/root/choyeon-panel/scripts/uninstall.sh` |

---

## 1. 架构与关键路径

```
浏览器 ──HTTPS──> nginx(443) ──反代──> uvicorn(127.0.0.1:3210) ──> SQLite
                                          │
                                          ├─ systemctl / journalctl（systemd_ops）
                                          ├─ nginx -t / certbot（nginx_ops）
                                          ├─ pg_dump / psql（pg_service）
                                          └─ bash pty（terminal，WebSocket）
```

| 路径 | 用途 |
| --- | --- |
| `/root/choyeon-panel/backend` | Python/FastAPI 后端，`.venv` 虚拟环境 |
| `/root/choyeon-panel/backend/app` | 应用代码 |
| `/root/choyeon-panel/web/dist` | 前端构建产物（后端以 StaticFiles 挂载在 `/`） |
| `/root/choyeon-panel/data` | `panel.db`、JWT 密钥、运行时数据（**唯一必须备份的目录**） |
| `/root/backups/panel` | 备份输出（面板备份 + `backup.sh` 手动备份） |
| `/etc/systemd/system/choyeon-panel.service` | systemd 单元 |
| `/etc/nginx/conf.d/choyeon-panel.conf` | nginx 反代（如配置了） |

**端口与协议**：3210/TCP 仅本机回环；443 由 nginx 对外；SSE（部署日志）与 WebSocket（终端）都走同一个 3210，nginx 必须关闭 `proxy_buffering` 否则日志会卡住。

---

## 1.5 命令行（choyeonctl）与自检

`choyeonctl` 与网页端共用同一套后端逻辑，适合 SSH 下操作与自动化调用。

```bash
choyeonctl schema --json    # 输出完整命令契约（参数 + 退出码），脚本/AI 读这一份就够
choyeonctl doctor           # 只读自检：监听地址/管理员/服务/前端产物/nginx/防火墙/备份/磁盘/权限/全局 CLI
choyeonctl status --json    # 版本、监听、服务状态、健康、自检汇总
choyeonctl user create|reset|list|delete
choyeonctl app templates|list|create|deploy|rollback|logs
choyeonctl backup run|list
choyeonctl start|stop|restart|logs|upgrade
```

约定：

- 任何命令都能加 `--json`（写在子命令前后都行），输出结构化结果；
- 退出码：`0` 成功、`1` 执行失败、`2` 用法错误、`3` 前置条件不满足；
- 破坏性操作需显式 `--yes`（如 `user delete`）；
- 改密与删号都会吊销该用户旧 token（`security.bump_user_epoch`），这是预期行为。

**就绪探针**（反代/容器用）：`/api/health` 是存活探针，`/api/ready` 是就绪探针（会查库，库不可用时返回 503）。两者都在认证白名单里，不要给它们加鉴权。

---

## 2. 日常操作

```bash
# 启停与自启
systemctl start|stop|restart choyeon-panel
systemctl enable choyeon-panel          # 开机自启（install.sh 已默认开启）

# 日志（按时间过滤）
journalctl -u choyeon-panel --since "10 min ago" --no-pager
journalctl -u choyeon-panel -p err -n 100 --no-pager

# 改配置后生效（例如改了 .env）
systemctl restart choyeon-panel
systemctl show choyeon-panel -p Environment   # 确认 Environment= 是否注入成功
```

> `.env` 由后端自己读取（`backend/app/config.py` 的 `_load_env`），**优先级低于 systemd 的 `Environment=`**。
> 因此在 unit 里写死的值会覆盖 `.env`，排障时先看 `systemctl show`。

---

## 3. 故障排查矩阵

### 3.1 面板打不开（502 / 503）

| 步骤 | 命令 | 判断 |
| --- | --- | --- |
| 1 | `systemctl is-active choyeon-panel` | `inactive`/`failed` → 服务没起来，跳 3.2 |
| 2 | `ss -ltnp \| grep 3210` | 无输出 → 监听没起来 |
| 3 | `curl -fsS http://127.0.0.1:3210/api/health` | 返回 `{"ok":true,...}` → 后端正常，问题在 nginx |
| 4 | `nginx -t && systemctl status nginx` | nginx 配置错误 → 跳 3.4 |

后端正常但页面 404：说明 `web/dist` 不存在或未构建。

```bash
ls /root/choyeon-panel/web/dist/index.html || echo "前端未构建"
cd /root/choyeon-panel/web && npm run build && systemctl restart choyeon-panel
```

### 3.2 服务起不来 / 反复重启

```bash
journalctl -u choyeon-panel -n 100 --no-pager   # 看真实报错
```

常见原因：

| 报错特征 | 根因 | 修复 |
| --- | --- | --- |
| `Address already in use` | 3210 被占用（旧进程残留） | `ss -ltnp \| grep 3210` → `kill <pid>`；或改 `CP_PORT` |
| `No such file or directory: .../backend/app/main.py` | `WorkingDirectory` 与实际安装路径不符 | 重新渲染 unit：`/root/choyeon-panel/scripts/update.sh --no-pull` |
| `Permission denied` 写 `/etc/nginx` | 服务不是 root 运行 | unit 必须保持 root（面板要管理 nginx/systemd） |
| `ModuleNotFoundError` | `.venv` 依赖缺失 | `cd backend && .venv/bin/pip install -r requirements.txt` |
| `starlette.testclient ... requires the httpx2 package`（只在 CI 红、本地绿） | 测试依赖没装：starlette 1.x 的 `TestClient` 硬依赖 `httpx2`，本地 venv 里躺着旧 `httpx` 只会降级成 deprecation warning | `cd backend && .venv/bin/pip install -r requirements-dev.txt`（CI 已自动装，见 `ci.yml`） |
| 启动 5 次后停止重试 | 触发了 `StartLimitBurst=5` | 先修根因，再 `systemctl reset-failed choyeon-panel && systemctl start choyeon-panel` |
| `Failed at step SECCOMP spawning` | unit 里加了 `SystemCallFilter=` | 删除该行（本项目 unit 刻意不启用 seccomp） |

```bash
# 手动前台启动，能直接看到 traceback（排障最有效的一招）
cd /root/choyeon-panel/backend
CP_LOG=1 CP_PORT=3999 .venv/bin/python -m app.main
```

### 3.3 能登录但操作报 500 / 超时

```bash
CP_LOG=1 journalctl -u choyeon-panel -n 200 --no-pager | grep -iE 'unhandled|error|traceback'
```

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| 终端一直转圈 | 并发会话达 `CP_TERMINAL_MAX_SESSIONS` 上限 | 关掉多余标签页；调大 `CP_TERMINAL_MAX_SESSIONS`（默认 4，上限 32） |
| 部署日志刷不出 | nginx 反代缓冲未关 | 确认 `proxy_buffering off;`（见 `deploy/nginx-panel.conf.example`） |
| 上传大文件 413 | nginx `client_max_body_size` 小于后端 `CP_MAX_UPLOAD_MB` | 两边对齐（默认后端 8MB，示例 nginx 20MB） |
| 改密后被踢下线 | 正常行为：改密会吊销旧 token | 重新登录；前端已对接新 token 自动续签 |
| 部署卡在 running | 上次进程被杀遗留的状态 | 重启服务即会自动标记为 failed（lifespan 里已处理） |

### 3.4 nginx 配置错误

```bash
nginx -t                       # 必须先校验
systemctl reload nginx         # 平滑重载（不要 restart，会断 WebSocket）
```

改坏了想回滚：

```bash
cp /etc/nginx/conf.d/choyeon-panel.conf /tmp/broken.conf   # 留现场
cp /root/choyeon-panel/deploy/nginx-panel.conf.example /etc/nginx/conf.d/choyeon-panel.conf
# 替换域名后
nginx -t && systemctl reload nginx
```

`certbot` 续期失败（80 端口被拦）：确认 80 端口 server 块放行 `/.well-known/acme-challenge/`。

### 3.5 磁盘与数据库

```bash
df -h /root/choyeon-panel/data
du -sh /root/backups/panel/*
sqlite3 /root/choyeon-panel/data/panel.db 'PRAGMA integrity_check;'
```

| 现象 | 处理 |
| --- | --- |
| `database is locked` | 已开启 WAL + `busy_timeout=15000`；仍报错说明有长事务，查 `fuser data/panel.db*` |
| `integrity_check` 非 ok | 立即停服务 → 用最近备份覆盖 → 起服务（见第 4 节） |
| 备份目录撑爆磁盘 | `scripts/backup.sh` 默认保留最近 14 份（`CP_KEEP_BACKUPS` 可调） |
| 审计表过大 | 后台任务按 `CP_AUDIT_KEEP`（默认 5000 条）自动裁剪 |

---

## 4. 备份与恢复

### 备份

```bash
/root/choyeon-panel/scripts/backup.sh          # → /root/backups/panel/manual/panel-<tag>-<ts>.tar.gz
```

内含：`panel.db`（用 `sqlite3 .backup` 在线安全拷贝，不是直接 `cp`）+ `.env`。
定时执行建议：

```cron
0 4 * * * /root/choyeon-panel/scripts/backup.sh >> /var/log/choyeon-backup.log 2>&1
```

### 恢复

```bash
systemctl stop choyeon-panel
mkdir -p /tmp/restore && tar -xzf /root/backups/panel/manual/panel-*.tar.gz -C /tmp/restore
cp /root/choyeon-panel/data/panel.db /root/choyeon-panel/data/panel.db.broken   # 留现场
cp /tmp/restore/panel-*.db /root/choyeon-panel/data/panel.db
systemctl start choyeon-panel
/root/choyeon-panel/scripts/healthcheck.sh
```

> ⚠️ 恢复会覆盖用户表与审计日志。操作前务必先 `cp` 一份当前 db 作为回退点（上面已做）。

---

## 5. 升级与回滚

```bash
/root/choyeon-panel/scripts/update.sh        # 拉代码 → 备份 → 依赖 → 构建 → 重启 → 健康检查
```

内置保护：
1. 升级前自动备份到 `/root/backups/panel/manual/`；
2. 依赖清单未变化时跳过 `pip`/`npm ci`，加速；
3. 重启后 40 秒内 `/api/health` 不通 → **自动 `git reset --hard` 回上一个 commit、重建、重启**。

手动回滚：

```bash
cd /root/choyeon-panel
git log --oneline -5                 # 找到目标 commit
git reset --hard <old-sha>
cd web && npm run build && cd ..
systemctl restart choyeon-panel
scripts/healthcheck.sh
```

改配置后只想重建重启（不拉代码）：

```bash
/root/choyeon-panel/scripts/update.sh --no-pull
```

---

## 6. 安全基线检查清单

上线前逐项确认：

- [ ] 面板仅监听 `127.0.0.1:3210`，**没有**直接对外暴露（防火墙只放行 80/443）
- [ ] 已启用 HTTPS，`CP_HSTS` 保持 0（由 nginx 统一发 HSTS）
- [ ] `/root/choyeon-panel/.env` 权限为 `0600`（install.sh 已设置）
- [ ] `data/` 权限为 `0700`
- [ ] 管理员口令 ≥ 12 位；已删除默认/测试账号
- [ ] `CP_FILE_ROOTS` 只包含必要目录，**不含** `/`、`/etc`（不含子路径时不要写 `/etc`）
- [ ] nginx 登录接口已配置 `limit_req`（示例配置里有可选项）
- [ ] 备份落盘位置与主机分离（至少不在同一块盘）
- [ ] `journalctl -u choyeon-panel -p err` 无持续报错

防火墙（只放行 Web 端口）：

```bash
# Debian 系
ufw allow 80/tcp && ufw allow 443/tcp && ufw deny 3210/tcp && ufw reload
# RHEL 系
firewall-cmd --permanent --add-service=http --add-service=https && firewall-cmd --reload
```

---

## 7. 应急响应速查

| 场景 | 第一动作 | 兜底 |
| --- | --- | --- |
| 面板服务完全起不来 | `journalctl -u choyeon-panel -n 100` | 回退到上一个已知好的 commit（第 5 节） |
| 怀疑被入侵 / 口令泄露 | `systemctl stop choyeon-panel` | 停服务 → 恢复备份 → 全量改密 → 查 `audit` 表 |
| 数据库损坏 | 停服务 → `integrity_check` | 用最近备份恢复（第 4 节），不要用 `--repair` 硬修 |
| nginx 挂了导致全站 502 | `nginx -t` → 回滚配置 | `systemctl reload nginx`；仍不行则临时用 `CP_PORT` 直连排查 |
| 磁盘 100% | `du -sh` 找大头 | 清 `/root/backups/panel` 旧备份 + `journalctl --vacuum-size=200M` |

审计日志查询（谁在什么时候做了什么）：

```bash
sqlite3 /root/choyeon-panel/data/panel.db \
  "SELECT created_at, username, action, detail FROM audit ORDER BY id DESC LIMIT 50;"
```
