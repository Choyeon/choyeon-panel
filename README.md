# choyeon-panel

原生服务器管理面板：在一台 Linux 服务器上集中管理部署的应用（Node/Python）、systemd 服务、PostgreSQL/Redis、备份、文件、SSL 证书与告警。前端 Vue 3 + Naive UI，后端 Python FastAPI + SQLite，全部功能仅用标准库与少量优质三方库（FastAPI/uvicorn/PyJWT/websockets）。

## 功能

- **应用管理**：Git 拉取 → 安装依赖 → systemd 单元托管部署 Node/Python 应用，支持环境变量、自定义启动命令与 unit 模板、一键回滚部署记录
- **Nginx 集成**：按应用域名/端口生成反代配置，快捷开关 WebSocket 升级、请求体大小、重定向；`nginx -t` 校验后热加载
- **SSL**：certbot 签发/续期，到期天数告警
- **系统**：CPU/内存/磁盘/网速实时监控与历史曲线（`source` 字段标记数据来源：`python` 深采集 / `node` 降级快照）、systemd 服务启停、journalctl 实时日志（SSE）
- **数据库**：PostgreSQL 库/角色管理 + SQL 控制台；Redis INFO 面板
- **备份**：pg_dump / 目录打包，daily/weekly 调度、保留份数、cron 内嵌调度器
- **文件管理**：限定根目录内的浏览/编辑/上传/下载/重命名/删除
- **Web 终端**：Python 标准库 pty + xterm.js，仅管理员可用
- **用户与审计**：admin/viewer 两级角色（viewer 只读），全量操作审计日志
- **告警**：磁盘水位、SSL 到期、服务掉线 → Telegram / Webhook

## 目录结构

```
backend/           FastAPI 后端（Python 3.11+，uv 管理依赖）
  app/             routers/（apps、system、terminal、dbops、backups、files、extras）+ services/（nginx/systemd/pg/备份/告警…）
  app/backup_runner.py  备份 CLI（systemd timer 调用）
  python/          sys_helper.py —— 服务器本地系统指标采集（仅标准库）
  pyproject.toml   依赖声明（fastapi/uvicorn/websockets/PyJWT）；uv sync 生成 .venv
  tests/           unittest 套件（校验器/unit 渲染/nginx 解析/路径守卫/认证/sys_helper 解析）
data/              SQLite 数据库、jwt 密钥（运行时生成，已 gitignore）
web/               Vue 3 + Naive UI 前端（Vite 构建）
deploy/            systemd unit 与 nginx 反代示例
```

## 部署（Ubuntu 22.04+/Debian 12，root 或等权用户）

要求：[uv](https://docs.astral.sh/uv/)（`curl -LsSf https://astral.sh/uv/install.sh | sh` 或 pipx install uv）、Python ≥ 3.11、Node.js ≥ 20（仅构建前端）、nginx、（可选）certbot、PostgreSQL、Redis。

```bash
git clone <repo> /root/choyeon-panel && cd /root/choyeon-panel

# 构建前端
cd web && npm ci && npm run build && cd ..

# 后端依赖：uv 一键建虚拟环境并锁定
cd backend && uv sync && cd ..

# 注册 systemd 服务（按需修改 .env.example 中的变量后写入 unit）
cp deploy/choyeon-panel.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now choyeon-panel

# 首次访问 http://127.0.0.1:3210 创建管理员；公网访问请套用 deploy/nginx-panel.conf.example
```

面板自身续命：`systemctl status choyeon-panel`、`journalctl -u choyeon-panel -f`。

## 环境变量

见 [.env.example](.env.example)。要点：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CP_HOST` / `CP_PORT` | `127.0.0.1` / `3210` | 监听地址；反代场景保持回环 |
| `CP_TRUST_PROXY` | 关 | 置于反代后设 `1`，登录限速按真实 IP 计数 |
| `CP_DATA_DIR` | `data/`（仓库根） | SQLite 与密钥目录，需随备份保留 |
| `CP_FILE_ROOTS` | `/root/www,/etc/nginx,/root/backups/panel` | 文件管理器可访问的根目录白名单 |
| `CP_PYTHON` | `python3` | 系统指标采集的解释器 |
| `CP_LOG` | 关 | 设置任意值开启 uvicorn info 日志并回显错误详情 |

## 开发（Windows 本机亦可）

```bash
cd backend && uv sync                              # 首次建 .venv（或改依赖后重新同步）
cd backend && uv run python -m app.main            # 后端（CP_LOG=1 可看请求日志）
cd web     && npm i && npm run dev                 # vite dev server（proxy 见 vite.config.ts）
```

Windows 上 systemd/journalctl/nginx 等 Linux 专属能力会降级为错误提示，核心 API、页面与测试均可运行。

## 测试

```bash
cd backend && uv run python -m unittest discover -s tests  # 后端全套（23 项，含 sys_helper 解析器）
cd web     && npm run build                                # vue-tsc 类型检查 + vite 生产构建
```

## 安全设计

- 密码 scrypt(N=16384, 64B) + 随机盐存储（与旧 Node 版哈希互通）；JWT 12h 过期，密钥首启随机生成并存于 `CP_DATA_DIR`
- 登录限速（每 IP 10 次/分钟）+ 失败审计；viewer 角色全局禁写（认证中间件拦截）
- 所有 exec 走 `asyncio.create_subprocess_exec` 参数数组，配合名称/域名/unit 正则白名单，仅备份 runner 的管道命令使用受控 shell 转义
- 文件管理 realpath 约束在 `CP_FILE_ROOTS` 白名单内，拒绝穿越
- 统一安全响应头（nosniff、SAMEORIGIN frame、Referrer-Policy 等）；错误响应不回显堆栈
- SSE/终端经由同源反代访问；建议仅通过 HTTPS 暴露（nginx 示例已含 HSTS）

## 备份与恢复

面板数据 = `CP_DATA_DIR/panel.db` + 应用本体。恢复：

```bash
systemctl stop choyeon-panel
cp panel.db /root/choyeon-panel/data/panel.db
systemctl start choyeon-panel
```

业务库/目录备份由面板「备份」页调度，产物默认在 `/root/backups/panel`。
