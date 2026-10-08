# choyeon-panel

原生（无 Docker）Linux 服务器管理面板：在一台服务器上集中管理应用部署、systemd 服务、nginx 站点、SSL 证书、PostgreSQL/Redis、备份、文件与 Web 终端。

- **后端**：Python 3.11+ / FastAPI + Uvicorn + SQLite（WAL）
- **前端**：Vue 3 `<script setup>` + TypeScript + Naive UI + Vite
- **运维**：systemd unit 直管，nginx 反代，一行脚本安装/升级/备份

> 运维排障手册见 [RUNBOOK.md](./RUNBOOK.md)，交接说明见 [HANDOFF.md](./HANDOFF.md)。
> **AI 智能体 / 自动化部署请直接读 [AGENTS.md](./AGENTS.md)** —— 含确定性步骤、验收字段与禁止事项。

---

## 快速开始

### 一键安装（推荐）

```bash
git clone https://github.com/Choyeon/choyeon-panel.git /root/choyeon-panel
cd /root/choyeon-panel && bash scripts/install.sh
```

脚本会完成：装系统依赖（Python 3.11+ / Node 22 / nginx / git / sqlite）→ 建 venv → 构建前端 → 渲染 systemd unit → 启动并做健康检查 → （可选）配置 nginx 反代与 certbot。

可用环境变量覆盖：`CP_PREFIX`（默认 `/root/choyeon-panel`）、`CP_PORT`（3210）、`CP_DATA_DIR`、`CP_BACKUP_DIR`、`CP_SKIP_NGINX=1`、`CP_SKIP_DEPS=1`。

### 命令行（等价路径）

装完后 `choyeonctl` 会软链到 `/usr/local/bin`，网页端能做的事在终端同样能做：

```bash
choyeonctl deploy                    # 与 scripts/install.sh 等价的一键部署
choyeonctl user create admin --password '<口令>'
choyeonctl app create my-api --template python-fastapi --repo <git-url> --port 8000
choyeonctl app deploy --id 1 && choyeonctl app logs --id 1
choyeonctl doctor                    # 9 项只读自检，每项带修复命令
choyeonctl status --json             # 供脚本/AI 解析
```

所有命令都支持 `--json`，退出码固定 `0 成功 / 1 失败 / 2 用法错误 / 3 前置条件不满足`。

### 手动安装

```bash
cd /root/choyeon-panel
cp .env.example .env && chmod 0600 .env       # 按需修改

cd web && npm ci && npm run build && cd ..    # 前端产物 → web/dist（后端直接托管）

cd backend && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # 或 uv pip install -r requirements.txt

cp deploy/choyeon-panel.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now choyeon-panel
```

打开 `http://127.0.0.1:3210` 创建管理员。**公网访问必须套 HTTPS**（见 `deploy/nginx-panel.conf.example`），不要直接暴露 3210。

### 升级 / 回滚

```bash
/root/choyeon-panel/scripts/update.sh          # 自动备份 → 构建 → 重启 → 健康检查，失败自动回滚
/root/choyeon-panel/scripts/update.sh --no-pull # 只改了配置时：跳过拉代码，仅重建重启
```

---

## 功能

| 模块 | 能力 |
| --- | --- |
| 应用管理 | Git 拉取 → 安装依赖 → systemd 托管部署 Node/Python 应用；环境变量、自定义启动命令、unit 模板、**一键回滚到上一次成功部署** |
| 一键模板 | 建应用时选预设（`node-service` / `node-next` / `python-fastapi` / `static-site`），自动填运行时、安装与启动命令、端口 |
| **安全自检** | 网页「安全自检」页与 `choyeonctl doctor`：监听地址、管理员、服务、前端产物、nginx、防火墙、备份、磁盘、文件权限 9 项只读检查，每项给可复制的修复命令 |
| Nginx 集成 | 按域名/端口生成反代配置，开关 WebSocket 升级、请求体大小、重定向；`nginx -t` 校验后热加载，**校验失败自动回滚原文件** |
| SSL | certbot 签发/续期，到期天数告警 |
| 系统监控 | CPU/内存/磁盘/网速实时曲线；systemd 服务启停；journalctl 实时日志（SSE） |
| 数据库 | PostgreSQL 库/角色管理 + SQL 控制台；Redis INFO 面板 |
| 备份 | pg_dump / 目录打包，daily/weekly 调度、保留份数 |
| 文件管理 | 白名单根目录内浏览/编辑/上传/下载/重命名/删除，带体积上限 |
| Web 终端 | pty + xterm.js，仅管理员，带并发会话上限 |
| 用户与审计 | admin/viewer 两级（viewer 只读），全量操作审计日志 |
| 告警 | 磁盘水位、SSL 到期、服务掉线 → Telegram / Webhook |

---

## 目录结构

```
backend/
  app/
    config.py          环境变量 → 类型化配置（含 .env 解析与取值范围钳制）
    templates.py       一键部署模板（网页端与 CLI 共用）
    routers/meta.py    /api/ready 就绪探针、/api/templates、/api/doctor 自检
    database.py        SQLite 封装：WAL、busy_timeout、审计与部署日志裁剪
    security.py        scrypt 口令、JWT（含按用户吊销版本号）、登录限速
    deps.py            认证中间件 + 统一安全响应头（CSP/HSTS/…）
    util.py            子进程封装：进程组超时清理，避免僵尸进程
  cli.py               choyeonctl：命令行部署/账号/应用/备份/自检，全命令支持 --json
  backup_runner.py     备份 CLI（systemd timer 调用）
  python/sys_helper.py 系统指标采集（仅标准库）
  tests/               unittest 套件
bin/choyeonctl         命令行入口（安装后软链到 /usr/local/bin）
web/                   Vue 3 + Naive UI 前端
  src/style.css        设计令牌（design tokens）、双主题变量与动效令牌
  src/App.vue          主题系统（dark/light/auto 跟随系统）
  src/views/Doctor.vue 安全自检页
deploy/                systemd unit + nginx 反代示例
scripts/               install / update / backup / healthcheck / uninstall
AGENTS.md              AI 智能体部署规则（确定性步骤 + 验收标准）
.github/workflows/     CI（后端测试 + 前端构建 + shellcheck）与 Release 打包
```

---

## 环境变量

完整说明见 [.env.example](.env.example)，常用项：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CP_HOST` / `CP_PORT` | `127.0.0.1` / `3210` | 监听地址；反代场景保持回环 |
| `CP_TRUST_PROXY` | 关 | 置于反代后设 `1`，登录限速按真实 IP 计数 |
| `CP_HSTS` | 关 | 面板直连 HTTPS 时才开；走 nginx 时由 nginx 统一发送 |
| `CP_DATA_DIR` | 仓库根 `data/` | SQLite 与密钥目录，**唯一必须备份** |
| `CP_FILE_ROOTS` | `/root/www,/etc/nginx,/root/backups/panel` | 文件管理根目录白名单（绝对路径） |
| `CP_TOKEN_TTL_HOURS` | `12` | 登录有效期（1-72） |
| `CP_LOGIN_LIMIT` | `10` | 每 IP 每分钟登录尝试上限 |
| `CP_MAX_UPLOAD_MB` | `8` | 单文件上限（nginx `client_max_body_size` 需 ≥ 此值） |
| `CP_TERMINAL_MAX_SESSIONS` | `4` | 并发终端会话上限 |
| `CP_AUDIT_KEEP` | `5000` | 审计日志保留条数 |
| `CP_PYTHON` | `python3` | 系统指标采集解释器 |

> 优先级：内置默认 < `.env` < 进程环境变量（systemd 的 `Environment=`）。

---

## 开发

```bash
make backend-dev     # 后端 127.0.0.1:3210
make web-dev         # 前端 5173，/api 代理到 3210
make test            # 后端 unittest
make lint            # ruff + mypy + vue-tsc
make build           # 前端生产构建
make help            # 查看全部命令
```

Windows 本机也可开发：systemd/journalctl/nginx 等 Linux 专属能力会降级为提示，API、页面与测试均可运行。

---

## 测试与质量

```bash
cd backend && python3 -m unittest discover -s tests   # 校验器、unit 渲染与转义、nginx 解析、
                                                      # 路径守卫、令牌吊销、限速、日志裁剪、超时清理
                                                      # 部署模板、就绪探针
cd web && npm run build                               # vue-tsc 类型检查 + vite 构建
bash scripts/healthcheck.sh                           # 线上健康检查
```

CI（`.github/workflows/ci.yml`）在每次 push/PR 上跑：Python 3.11/3.12 双版本后端测试 + 启动冒烟、前端类型检查与构建、shellcheck、systemd unit 校验。

---

## 安全设计

- 口令 scrypt(N=16384, 64B) + 随机盐；JWT 密钥首启随机生成并存于 `CP_DATA_DIR`
- **令牌吊销**：改密/删号后按用户自增 epoch，旧 token 立即失效（改密接口会同步返回新 token，不打断当前会话）
- 登录限速（每 IP 每分钟 N 次，可调）+ 失败审计；viewer 角色全局禁写
- 统一安全响应头：`Content-Security-Policy`、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、COOP/CORP、`X-Request-Id`；可选 HSTS
- 所有 exec 走参数数组 + 名称/域名/unit 正则白名单；仅受控场景使用 shell 转义
- 子进程统一 `start_new_session` 并在超时时 `killpg`，避免残留进程组与僵尸进程
- 文件管理 realpath 约束在白名单内，拒绝穿越、跨目录移动与超限读写
- nginx 配置变更前保存原文件，`nginx -t` 失败即回滚

---

## UI/UX

- 设计令牌（`src/style.css`）+ `dark / light / auto` 三态主题，`auto` 跟随系统且实时响应系统切换
- 首屏内联脚本预置主题，杜绝暗色模式下的白屏闪烁
- 响应式：桌面侧栏（可折叠）< 900px 自动切抽屉导航，表格开启横向滚动
- 无障碍：语义表单 + `label`/`aria-label`、`role="log"` 日志区、`:focus-visible` 焦点环、44px 触控目标、支持键盘触发的行操作
- 尊重 `prefers-reduced-motion`，动画自动降级
- ECharts 按需引入 + `ResizeObserver` 自适应；构建按变更频率分包（vue/naive/echarts/xterm）

---

## 备份与恢复

```bash
/root/choyeon-panel/scripts/backup.sh    # → /root/backups/panel/manual/panel-<tag>-<ts>.tar.gz
```

包含 `panel.db`（用 SQLite 在线 backup API 安全拷贝，非直接 `cp`）与 `.env`，默认保留最近 14 份（`CP_KEEP_BACKUPS`）。建议加定时任务：

```cron
0 4 * * * /root/choyeon-panel/scripts/backup.sh >> /var/log/choyeon-backup.log 2>&1
```

恢复：停服务 → 解压覆盖 `panel.db` → 起服务 → `scripts/healthcheck.sh`（详细步骤见 RUNBOOK 第 4 节）。

---

## License

MIT
