<p align="center">
  <img src="./assets/banner.svg" alt="choyeon-panel" width="100%" />
</p>

<p align="center">
  <a href="#界面预览">界面预览</a> ·
  <a href="#快速开始">快速开始</a> ·
  <a href="#首次访问与管理员账号">首次访问</a> ·
  <a href="#为什么选它">为什么选它</a> ·
  <a href="#功能">功能</a> ·
  <a href="#架构">架构</a> ·
  <a href="#命令行">命令行</a> ·
  <a href="#环境变量">环境变量</a> ·
  <a href="#faq">FAQ</a> ·
  <a href="CHANGELOG.md">更新日志</a> ·
  <a href="CONTRIBUTING.md">贡献指南</a> ·
  <a href="RUNBOOK.md">排障手册</a> ·
  <a href="AGENTS.md">AI 部署规则</a>
</p>

<p align="center">
  <a href="https://github.com/Choyeon/choyeon-panel/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/Choyeon/choyeon-panel/ci.yml?branch=main&label=CI&logo=githubactions&logoColor=white&color=6fdba4&cacheSeconds=3600" alt="CI 状态" /></a>
  <a href="https://github.com/Choyeon/choyeon-panel/releases"><img src="https://img.shields.io/github/v/release/Choyeon/choyeon-panel?label=release&logo=github&color=3d8eff&cacheSeconds=3600" alt="最新版本" /></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white&cacheSeconds=3600" alt="Python 3.11+" />
  <img src="https://img.shields.io/badge/Vue%203-TypeScript-42b883?logo=vuedotjs&logoColor=white&cacheSeconds=3600" alt="Vue 3 + TypeScript" />
  <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-9b7bff?cacheSeconds=3600" alt="MIT License" /></a>
  <img src="https://img.shields.io/badge/Docker-not%20required-8fd460?logo=docker&logoColor=white&cacheSeconds=3600" alt="无需 Docker" />
  <img src="https://img.shields.io/badge/OS-Ubuntu%20%C2%B7%20Debian%20%C2%B7%20Rocky%20%C2%B7%20Arch%20%C2%B7%20Alpine-5b5e6a?cacheSeconds=3600" alt="支持的发行版" />
</p>

> **原生 Linux 服务器管理面板：跑在宿主机上，不依赖 Docker。**
> 装完自带 `choyeonctl`：应用部署与回滚、systemd 服务、nginx 站点与证书、PostgreSQL/Redis、备份与异地对象存储、文件管理、Web 终端、防火墙与登录安全对账、22 项安全自检。

---

## 界面预览

> 以下截图取自真实运行实例（暗色主题，敏感信息已脱敏）。choyeon-panel 的网页端覆盖从部署、运维到安全的完整闭环：直接管理宿主机的 systemd / nginx / certbot / PostgreSQL / 文件与防火墙，不引入容器层。

### 总览与监控

| | |
| --- | --- |
| ![总览](assets/screenshots/dashboard.png) | ![系统服务](assets/screenshots/services.png) |

### 应用与站点

| | |
| --- | --- |
| ![应用管理](assets/screenshots/apps.png) | ![应用详情](assets/screenshots/app-detail.png) |
| ![网站与证书](assets/screenshots/sites.png) |  |

### 数据与备份

| | |
| --- | --- |
| ![数据库](assets/screenshots/db.png) | ![备份](assets/screenshots/backups.png) |
| ![备份文件](assets/screenshots/backups-files.png) | ![文件管理](assets/screenshots/files.png) |

### 运维与安全

| | |
| --- | --- |
| ![防火墙](assets/screenshots/firewall.png) | ![安全自检](assets/screenshots/doctor.png) |
| ![定时任务](assets/screenshots/cron.png) | ![Web 终端](assets/screenshots/terminal.png) |

### 日志与告警

| | |
| --- | --- |
| ![告警中心](assets/screenshots/alerts.png) | ![面板日志](assets/screenshots/logs.png) |
| ![日志中心](assets/screenshots/logsearch.png) |  |

### 面板管理与安全

| | |
| --- | --- |
| ![用户管理](assets/screenshots/users.png) | ![操作审计](assets/screenshots/audit.png) |
| ![设置](assets/screenshots/settings.png) | ![二次确认弹窗](assets/screenshots/confirm-dialog.png) |
| ![登录 / 初始化](assets/screenshots/login.png) |  |

---

## 为什么选它

买一台 VPS 之后，通常要同时在 SSH、systemctl、nginx 配置、certbot、pgAdmin、备份脚本之间来回切换。choyeon-panel 把这些收进一个页面，并且**直接管理宿主机的原生能力**——不引入容器层、不额外守护进程栈：

| | choyeon-panel | 常见 Docker 化面板 |
| --- | --- | --- |
| 运行时依赖 | Python 3.11+ / Node（仅构建期） | Docker Engine + 镜像仓库 + 容器网络 |
| 被管应用 | 宿主机 systemd unit，进程可见可 `journalctl` | 容器编排层，日志与端口多一跳 |
| 内存常驻 | 单 Uvicorn 进程（psutil 进程内采集，无 fork） | 面板 + docker daemon + compose 容器 |
| 安装 | `bash scripts/install.sh` 一行，安装阶段就把管理员建好 | 先装 Docker，再装面板 |
| 升级 | 备份 → 构建 → 健康检查 → 失败自动 git 回滚 | 拉镜像，失败需手工排障 |
| 数据落点 | 一个 SQLite 文件 + `.env`，可直接备份/迁移 | 分散在卷与容器内 |
| 面向自动化 | 全命令 `--json` + 固定退出码 + [AGENTS.md](AGENTS.md) | 多为网页优先 |

适合把一到几个站点跑在裸机上、又希望每次改动都能回滚、能追责的人：个人 VPS、小团队共用机器、独立开发者的交付机。

---

## 快速开始

### 支持的发行版

安装脚本自动识别发行版并**自动安装全部依赖**（含 Python 3.11+ 与 Node 20+，实际装的是 NodeSource 22 LTS）：

| 系 | 发行版 | 包管理 |
| --- | --- | --- |
| Debian | Debian 12+ / Ubuntu 22.04+ / Linux Mint / Kali | apt |
| RHEL | CentOS Stream 9+ / Rocky / AlmaLinux / Fedora / Amazon Linux | dnf/yum |
| Arch | Arch / Manjaro / EndeavourOS / CachyOS | pacman |
| openSUSE | Leap 15.6+ / Tumbleweed | zypper |
| Alpine | 3.20+（musl；无 systemd 时服务需手动启动） | apk |

其它发行版：脚本不会中断，但会跳过依赖安装并提示自行确认 `git / nginx / sqlite3`。

### 一键部署

```bash
git clone https://github.com/Choyeon/choyeon-panel.git
cd choyeon-panel && sudo bash scripts/install.sh
```

脚本依次完成：

1. 装系统依赖、确认 Python / Node；
2. 拉取或复用代码，`env_sync_all` 对齐 `.env`（缺失则按 `.env.example` 生成）并建好 `CP_DATA_DIR` / `CP_BACKUP_DIR`（0700）；
3. 建 Python venv、构建前端产物到 `web/dist`；
4. **安装阶段创建管理员账号**（见下节；这一步在 systemd 服务首次启动之前，避免零用户窗口被暴露）；
5. 渲染并安装 systemd unit → `systemd-analyze verify` → 重启服务 → 健康检查（30 秒内）；
6. 软链 `/usr/local/bin/choyeonctl`；
7. **可选**交互询问 nginx 反代域名、写引导 vhost、`nginx -t` 校验后 reload、可选 certbot 签发证书。

全新机器 3–8 分钟，大头在装依赖和前端构建。`choyeonctl deploy --json` 是**非交互等价版本**（强制 `CP_SKIP_NGINX=1`，不问域名、不创建管理员），超时预算 15 分钟；它适合 CI，不替代上面这条交互式安装。

<p align="center">
  <img src="./assets/deploy-flow.svg" alt="部署流程：依赖 → 配置 → 构建 → 建管理员 → systemd → 健康检查，失败自动回滚" width="100%" />
</p>

安装完成后验收：

```bash
sudo choyeonctl status --json     # "healthy": true
sudo choyeonctl doctor --json     # "counts": {"fail": 0}
```

### 首次访问与管理员账号

**这是全新安装最容易踩空的一步。** 面板的数据库初始是零用户态，`/api/auth/status` 会返回 `needsSetup`，登录页据此在「初始化面板」和「欢迎回来」之间切换。建号有且只有以下三条路径，**二选一即可，做过一次另一条就关闭**：

| | 做法 | 适用 |
| --- | --- | --- |
| **A · 默认** | 直接跑 `scripts/install.sh`，交互时回答 `Y`，填用户名与两次密码 | 有人在终端里装；推荐 |
| **B · 网页** | 跳过 A，打开安装结束时打印的「访问」地址，首次访问出现「初始化面板 · 创建管理员账号开始使用」，创建成功后**自动登录** | 无终端交互、但能打开本机/同机反代页面 |
| **C · CLI** | `printf '%s' "$PW" \| sudo choyeonctl user create admin --password-stdin --json` | 自动化脚本、无浏览器；也是 A 失败后的补救 |

**方式 A 的细节**：用户名 3–32 位字母、数字、下划线或连字符（`^[A-Za-z0-9_-]{3,32}$`），密码 8–128 位，两次必须一致。密码用 `read -s` 静默读取、只经 stdin 传给 CLI，不会回显、不进 argv、不进 shell 历史。脚本会先查数据库，已有账号则直接跳过并提示「直接登录即可」。

**非交互自动化**：stdin 已被脚本本体占用，所以凭据走一个仅属主可读的文件，不要写进环境变量或命令行参数：

```bash
install -m 600 /dev/null /root/.cp-admin-pw
printf '%s\n' "$PW" > /root/.cp-admin-pw
CP_ADMIN_USER=admin CP_ADMIN_PASSWORD_FILE=/root/.cp-admin-pw \
  CP_SKIP_NGINX=1 sudo -E bash scripts/install.sh
shred -u /root/.cp-admin-pw        # 用完立即删
```

**安全边界（重要）**：`/api/auth/setup` 是零用户态下唯一免认证的写端点，默认只接受本机 TCP 对端（`127.0.0.0/8`、`::1`，同机 nginx 反代也算），公网直连会被 403 拒绝。反向代理装在另一台机器上、又必须走方式 B 时，才在 `.env` 里设 `CP_SETUP_ALLOW_REMOTE=1`，并**先限制来源再开放**。方式 A/C 不需要这个开关。

打开安装结束时打印的「访问」地址（默认 `http://127.0.0.1:3210`；配好域名和证书时是 `https://<域名>`）。**面板默认只监听 `127.0.0.1`**，公网访问请套 nginx + TLS（模板见 `deploy/nginx-panel.conf.example`），不要直接把 3210 暴露出去。登录后立刻在「设置」里开启两步验证。

### 安装期环境变量

与运行期 `.env` 的配置项是两回事：下面这些只在跑 `install.sh` / `deploy` 时生效，脚本会把最终值写回 `.env` 并渲染进 unit。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CP_PREFIX` | 脚本所在的仓库目录 | 安装目录；仓库结构不完整时退回 `/root/choyeon-panel` |
| `CP_REPO` / `CP_BRANCH` | GitHub 仓库 / `main` | 代码来源 |
| `CP_PORT` / `CP_HOST` | `3210` / `127.0.0.1` | 监听端口与地址 |
| `CP_SERVICE` | `choyeon-panel` | systemd 单元名 |
| `CP_DATA_DIR` | `$PREFIX/data` | 数据目录（脚本会覆盖 `.env` 里的示例值） |
| `CP_BACKUP_DIR` / `CP_APP_ROOT` / `CP_FILE_ROOTS` | `/root/backups/panel` / `/root/www` / 三者拼起来的白名单 | 备份、应用与文件根目录 |
| `CP_KEEP_BACKUPS` | `14` | 手动备份保留份数（脚本变量，不是后端配置键） |
| `CP_SKIP_NGINX` / `CP_SKIP_DEPS` | `0` / `0` | 跳过 nginx 步骤 / 跳过系统依赖安装 |
| `CP_ADMIN_USER` / `CP_ADMIN_PASSWORD_FILE` | — | 非交互建号的用户名与 0600 口令文件 |

取值顺序统一为 **显式环境变量 > 安装目录的 `.env` > 内置默认**。改运行期配置请编辑 `.env` 后执行 `choyeonctl upgrade --no-pull`（见 AGENTS.md 第 5 节），不要手改 systemd unit。

### 手动安装

```bash
cd choyeon-panel
cp .env.example .env && chmod 0600 .env
# 必改：把 CP_DATA_DIR / CP_APP_ROOT / CP_BACKUP_DIR 换成实际路径。
# .env.example 里的 /root/choyeon-panel/data 只是示例值，装到别处会指空。

cd web && npm ci && npm run build && cd ..    # 前端产物 → web/dist（后端直接托管）
cd backend && python3 -m venv .venv && cd ..
backend/.venv/bin/pip install -r backend/requirements.txt

# 不要直接 cp deploy/choyeon-panel.service：模板里的路径写死 /root/choyeon-panel，
# 装到别处必然起不来。用 CLI 按实际安装目录渲染 unit：
sudo ./bin/choyeonctl deploy --json

# 然后建管理员（口令走 stdin，不要写在命令行参数里）
PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
printf '%s' "$PW" | sudo choyeonctl user create admin --password-stdin --json
```

### 升级 / 回滚

```bash
choyeonctl upgrade            # 拉代码 → 备份 → 构建 → 重启 → 健康检查，失败自动回滚上一个 commit
choyeonctl upgrade --no-pull  # 只改了配置时：跳过拉代码，仅重建并重新渲染 unit
```

---

## 架构

<p align="center">
  <img src="./assets/architecture.svg" alt="choyeon-panel 架构：入口层（浏览器 SPA / choyeonctl / nginx 反代）→ 面板核心（认证中间件、routers、services、SQLite 与指标库）→ 被管资源（systemd、nginx+certbot、PostgreSQL/Redis、文件白名单、备份与告警）" width="100%" />
</p>

1. **一个进程，两类入口。** 后端是单个 FastAPI/Uvicorn 进程，同时托管 SPA 静态产物与 `/api`；`choyeonctl` 复用同一套 service 层，所以 CLI 覆盖的是网页端的同一批操作，不存在「只有 UI 里有」的能力。
2. **直接调用宿主机命令。** systemd、nginx、certbot、pg_dump 都在这台机器上被执行：exec 一律走参数数组 + 名称/域名/unit 正则白名单，子进程统一 `start_new_session` 并在超时时 `killpg`。
3. **状态只有一个 SQLite 文件。** 面板自身数据是 WAL 模式的 `panel.db` + `.env`，都在 `CP_DATA_DIR` 下；备份走 SQLite 在线备份 API，不用 `cp`。指标历史进同一个库，`doctor` 查的就是这些事实是否自相一致。

## 功能

| 模块 | 能力 |
| --- | --- |
| 应用管理 | Git 拉取 → 安装依赖 → systemd 托管部署 Node/Python 应用；环境变量、自定义启动命令、unit 模板，可回滚到上一次成功部署 |
| 一键模板 | 建应用时选预设（`node-service` / `node-next` / `python-fastapi` / `static-site`），自动填运行时、安装与启动命令、端口 |
| 安全自检 | 网页「安全自检」页与 `choyeonctl doctor`：监听地址、管理员、服务、前端产物、nginx、防火墙、备份、磁盘、文件权限、全局 CLI、证书续期、定时器对账、待装补丁与安全更新、端口暴露面、登录安全（SSH 有效配置 / 口令认证 / 爆破日志 / 公钥）、存储占用、内存压力与 OOM、改动未生效、负载与 CPU 归因、配置来源、站点链路、应用环境变量落盘，共 22 项只读检查，每项给可复制的修复命令 |
| 网站与证书 | 按域名/端口生成反代配置，开关 WebSocket 升级、请求体大小、重定向；`nginx -t` 校验后热加载，失败自动回滚。certbot 签发/续期，vhost 总览能一键套用/移除与手动续期，并列剩余天数 |
| 站点纳管 | 把域名归属写进面板自己的登记表并认领对应 nginx 配置文件；`choyeonctl site claim\|unclaim\|claim-all`，不写 nginx、不 reload、不签证书 |
| 防火墙与封禁 | ufw / firewalld 规则增删、防火墙开关（会把自己锁在门外的操作一律拒绝）；`choyeonctl ban add\|rm` 封禁 sshd 爆破来源，写的是同一张表 |
| 系统监控 | psutil 进程内采集（无 fork）：CPU/内存/网速实时曲线，1h/6h/24h/7d 历史曲线、Top 进程；systemd 服务启停；journalctl 实时日志（SSE）与关键词检索 |
| 数据库 | PostgreSQL 库/角色管理 + SQL 控制台 + 表/行数据浏览（可编辑）；Redis 键浏览器（含 TTL 与内存），读路径走进程内 RESP 客户端 |
| 备份 | pg_dump / 目录打包 / SQLite 在线备份，daily/weekly 由 systemd timer 托管；支持在线恢复、上传下载；可配 S3 兼容异地目标（S3 / MinIO / OSS / COS），签名只用标准库 |
| 日志中心 | 系统日志按 unit/时间/级别/正则关键词检索，应用与面板日志尾读；`choyeonctl webstats` 解析 nginx access.log 给状态码分布、TOP 路径与每分钟请求数 |
| 文件管理 | 白名单根目录内浏览/编辑/多文件上传/下载/打包/解压/重命名/删除，带体积上限；解压拒绝越界路径与符号链接 |
| Web 终端 | pty + xterm.js，仅管理员，带并发会话上限与空闲回收 |
| 用户与审计 | admin/viewer 两级（viewer 只读），全量操作审计日志（自动裁剪与脱敏）；破坏性操作统一走确认弹窗 |
| 告警 | 磁盘水位、SSL 到期、服务掉线、CPU/内存/负载阈值、应用 HTTP 探活 → Telegram / Webhook / 邮件 SMTP；告警入库可追溯、可确认 |
| 定时任务 | cron 表达式在面板里登记（渲染成 systemd timer），命令走固定 argv 白名单，可立即执行并留下末段日志 |

---

## 命令行

装完后 `choyeonctl` 会软链到 `/usr/local/bin`。所有命令都支持 `--json`，退出码固定 `0 成功 / 1 失败 / 2 用法错误 / 3 前置条件不满足`，适合写进运维脚本；**`choyeonctl schema --json` 是参数契约的权威来源**，下面只是常用示例。

### 命令族全览

| 组 | 子命令 |
| --- | --- |
| 生命周期 | `deploy` · `upgrade` · `status` · `doctor` · `start` · `stop` · `restart` · `logs` · `schema` |
| 账号 | `user list\|create\|reset\|revoke\|2fa-disable\|delete` |
| 应用 | `app templates\|list\|create\|deploy\|rollback\|logs` |
| 备份 | `backup run\|list\|jobs\|targets\|add-target\|edit-target\|del-target\|test-target\|push\|link\|cloud-list\|cloud-pull` |
| 防火墙 / 封禁 | `firewall list\|allow\|deny\|remove\|on\|off` · `ban list\|add\|rm` |
| 站点 | `site list\|claim\|unclaim\|claim-all` · `site-audit` |
| 只读对账 | `updates` · `ports` · `ssh-audit` · `storage` · `memory` · `pending` · `load` · `webstats` |

### 常用示例

```bash
choyeonctl deploy --port 3210 --json                 # 非交互部署（跳过 nginx 询问、不建管理员）
choyeonctl status --json                             # 版本 / 监听 / 服务 / 健康 / 自检汇总
choyeonctl doctor --json                             # 22 项只读自检，每项带修复命令
choyeonctl ssh-audit --json                          # 登录安全只读对账：口令认证、root 登录、爆破来源、公钥权限
choyeonctl storage --json                            # 存储占用对账：面板数据、备份、journald、应用目录各占多少
choyeonctl memory --json                             # 内存压力对账：换页、直接回收、内核 OOM 杀过谁
choyeonctl pending --json                            # 改动未生效对账：补丁装了但进程还踩着旧库、新内核没重启
choyeonctl load --json                               # 负载归因：每核排队、CPU 不够还是在等 IO、谁被 CPUQuota 限流
choyeonctl site-audit --json                         # 站点链路对账：反代目标有没有人听、证书覆不覆盖、有没有跳 HTTPS
choyeonctl ports --json / choyeonctl updates --json  # 端口暴露面 / 待装补丁
choyeonctl webstats --list --json                    # 访问统计：状态码分布、TOP 路径 / 客户端、每分钟请求数

printf '%s' "$PW" | choyeonctl user create admin --password-stdin --json
choyeonctl app templates                             # node-service / node-next / python-fastapi / static-site
choyeonctl app create my-api --template python-fastapi \
  --repo https://github.com/you/repo.git --port 8000 --json
choyeonctl app deploy --id 1 --json
choyeonctl app logs --id 1 --lines 80
choyeonctl backup run                                # SQLite 在线备份 API
choyeonctl backup jobs --json                        # 定时任务与最近一次执行留痕
choyeonctl backup targets                            # 异地目标与上次失败原因
printf '%s' "$SK" | choyeonctl backup add-target --name offsite --endpoint https://minio.example.com:9000 \
  --bucket panel --access-key AKIA... --secret-stdin --json   # Secret Key 只走 stdin
choyeonctl backup link --id 1 --target 1 --json       # 任务跑完推给哪个目标；--target 0 取消
choyeonctl backup push --id 1 --json                  # 只补推没推过的产物
choyeonctl firewall allow --port 8080 --json
choyeonctl ban add 203.0.113.9 --json                # 只封单个公网地址
choyeonctl site claim-all --json
choyeonctl schema --json                             # 输出完整命令契约
```

完整参数与失败处置表见 [AGENTS.md](AGENTS.md)；排障细节见 [RUNBOOK.md](RUNBOOK.md)。

---

## 目录结构

```
backend/
  app/
    config.py          环境变量 → 类型化配置（含 .env 解析与取值范围钳制）
    main.py            认证入口（登录 / 零用户初始化 / 2FA / 自助改密）与静态托管
    deps.py            认证中间件 + 统一安全响应头（CSP/HSTS/…）
    security.py        scrypt 口令、JWT（含按用户吊销版本号）、登录限速
    database.py        SQLite 封装：WAL、busy_timeout、审计与部署日志裁剪
    routers/           13 个路由模块（apps / backups / system / sites / dbops / …）
    services/          业务逻辑；CLI 与 HTTP 共用同一层
  cli.py               choyeonctl：命令行部署/账号/应用/备份/自检，全命令支持 --json
  backup_runner.py     备份 CLI（systemd timer 调用）
  run_tests.py         测试入口（隔离数据目录 + 退出清理；make test 与 CI 都走它）
  tests/               unittest 套件，刻意平铺，见 backend/tests/README.md
  requirements-dev.txt 测试/开发依赖（不进生产 requirements.txt）
bin/choyeonctl         命令行入口（安装后软链到 /usr/local/bin）
web/                   Vue 3 + Naive UI 前端
  src/style.css        设计令牌（design tokens）、双主题变量与动效令牌
  src/validation.ts    账号规则唯一前端出处（用户名/口令校验与提示文案）
  src/App.vue          主题系统（dark/light/auto 跟随系统）
  src/views/           21 个页面；src/router.ts 定义路由与 admin-only 收口
  tests/               前端源码断言单测（node --test）
deploy/                systemd unit + nginx 反代示例
scripts/               install / update / backup / healthcheck / uninstall / common.sh
assets/                门面与文档插图（logo / banner / 架构图 / 部署流程 / 界面示意）
AGENTS.md              AI 智能体部署规则（确定性步骤 + 验收标准）
RUNBOOK.md             运维排障手册（按症状查表）
CONTRIBUTING.md        贡献指南（环境、测试门槛、提交与 PR 约定）
SECURITY.md            漏洞上报通道与威胁模型
CHANGELOG.md           更新日志（Keep a Changelog）
LICENSE                MIT
.editorconfig          UTF-8 / LF / 缩进与末尾换行约定
.gitignore / .gitattributes
.github/               CI 与 Release 工作流、Issue/PR 模板、Dependabot
```

---

## 环境变量

完整说明见 [.env.example](.env.example)。后端共 32 个 `CP_*` 配置键，全部可选、非法值回落默认、越界钳到上下限。下表**列全**：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CP_HOST` | `127.0.0.1` | 监听地址；反代场景保持回环 |
| `CP_PORT` | `3210` | 监听端口（1-65535） |
| `CP_TRUST_PROXY` | 关 | 置于反代后设 `1`，登录限速按真实 IP 计数 |
| `CP_TRUSTED_PROXIES` | `127.0.0.1,::1` | 允许采信 `X-Real-IP` / `X-Forwarded-For` 的 TCP 对端 |
| `CP_HSTS` | 关 | 面板直连 HTTPS 时才开；走 nginx 时由 nginx 统一发送 |
| `CP_LOG` | 关 | 只开 uvicorn info 与访问日志；**不会**把异常详情回显到响应体 |
| `CP_SETUP_ALLOW_REMOTE` | 关 | 放开「非本机对端」调用首次初始化建号接口，见「首次访问与管理员账号」 |
| `CP_DATA_DIR` | 仓库根 `data/` | SQLite 与密钥目录，**唯一必须备份**；install.sh 会覆盖成 `$PREFIX/data` |
| `CP_APP_ROOT` | `/root/www` | 应用部署根目录 |
| `CP_BACKUP_DIR` | `/root/backups/panel` | 备份存放目录 |
| `CP_FILE_ROOTS` | `/root/www,/etc/nginx,/root/backups/panel` | 文件管理根目录白名单（绝对路径） |
| `CP_LOG_ROOTS` | `/var/log/nginx` | 访问/错误日志与流量统计能读到的目录（不放大文件管理白名单） |
| `CP_UNIT_DIR` | `/etc/systemd/system` | systemd 单元目录 |
| `CP_NGINX_CONF_DIRS` | `/etc/nginx/conf.d,/etc/nginx/sites-available` | nginx 配置发现目录 |
| `CP_TLS_DIR` | `/etc/letsencrypt/live` | 证书目录 |
| `CP_TOKEN_TTL_HOURS` | `12` | 登录有效期（1-72 小时） |
| `CP_LOGIN_LIMIT` | `10` | 每 IP 每分钟登录尝试上限（1-100）；按账号的失败桶复用同一常量 |
| `CP_AUDIT_KEEP` | `5000` | 审计日志保留条数 |
| `CP_MAX_UPLOAD_MB` | `8` | 单文件上限（1-512；nginx `client_max_body_size` 需 ≥ 此值） |
| `CP_MAX_BODY_MB` | `2` | 非上传接口的请求体上限（1-64；超出返回 413） |
| `CP_MAX_EXTRACT_MB` | `512` | 解压后体积上限（16-8192），防 zip bomb |
| `CP_MAX_EXTRACT_FILES` | `5000` | 解压文件数上限（10-200000） |
| `CP_MAX_DIR_ENTRIES` | `5000` | 文件页一次列目录的条目上限（100-200000），超出按名称截断并如实提示 |
| `CP_PG_STATEMENT_TIMEOUT_MS` | `20000` | 下发给 PostgreSQL 的语句超时（0 不注入） |
| `CP_PG_LOCK_TIMEOUT_MS` | `5000` | 行编辑等锁上限，超时如实报「拿不到锁」 |
| `CP_SQLITE_SCAN_MAX_ENTRIES` | `200000` | 恢复定位目标库时扫描应用目录的条目上界 |
| `CP_SQLITE_SCAN_BUDGET_SEC` | `20` | 同一次扫描的时间上界（1-300 秒） |
| `CP_CLOUD_PUSH_TIMEOUT_SEC` | `900` | 异地推送超时（30-7200 秒） |
| `CP_CLOUD_PROBE_TIMEOUT_SEC` | `20` | 异地目标连通性探测超时（3-300 秒） |
| `CP_AVATAR_MAX_KB` | `2048` | 头像上传上限（32-16384 KB） |
| `CP_TERMINAL_MAX_SESSIONS` | `4` | 并发终端会话上限（1-32） |
| `CP_TERMINAL_OUT_BUFFER_KB` | `4096` | 终端输出缓冲水位（256-65536 KB），超水位主动断开 |

> 优先级：内置默认 < `.env` < 进程环境变量（systemd 的 `Environment=`）。安装期变量（`CP_PREFIX`、`CP_SKIP_NGINX` 等）见上节，不属于这张表。

---

## 开发

```bash
make backend-dev     # 后端 127.0.0.1:3210（需要 Linux / WSL2）
make web-dev         # 前端 5173，/api 代理到 3210
make test            # 后端 unittest
make lint            # ruff（阻断）+ mypy（提示）+ vue-tsc（阻断）
make build           # 前端生产构建
make clean           # 只清可再生的构建产物与缓存，不动 .venv / data
make distclean       # 连 .venv 与 data 一起清（会删数据，先备份）
make help            # 查看全部命令
```

**Windows 本机只能开发前端**：后端在 import 期依赖 `fcntl` / `grp` 等 Unix 模块，Windows 上 `import app.main` 直接失败。全量后端测试必须跑在 Linux / WSL2 / 部署机上。前端类型检查、单测与构建在 Windows 可正常运行。

### 测试与质量

```bash
cd backend
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt   # httpx2 是 TestClient 硬需求
.venv/bin/python run_tests.py               # 全量 unittest；入口强制用临时数据目录，不碰线上库
cd ../web
npm ci
npm run typecheck                           # vue-tsc --noEmit
npm run test:unit                           # node --test，前端源码断言单测
npm run build                               # vue-tsc 类型检查 + vite 构建 + gzip 预压缩
bash scripts/healthcheck.sh                 # 线上健康检查
```

CI（[`.github/workflows/ci.yml`](.github/workflows/ci.yml)）在每次 push/PR 上跑四个 job：Python 3.11/3.12 双版本后端测试（非 root runner，含 CLI 与启动冒烟）、前端类型检查 + 单元测试 + 构建、shellcheck、systemd unit 校验。`run_tests.py` 的用例数随批次增长，不在文档里写死数字；增删用例后以本地/服务器实跑结果为准。仓库门面由 `backend/tests/test_repo_hygiene.py` 持续把守：README 引用的本地文件、版本号三处一致、`.env.example` 与 `config.py` 的配置键双向覆盖、`assets/banner.svg` 版式，以及全仓 LF 与「具名 export 不许零读者」。

---

## 安全设计

- 口令 scrypt(N=16384, 64B) + 随机盐；JWT 密钥首启随机生成并存于 `CP_DATA_DIR`，数据目录 0700、`panel.db` 与 `.env` 0600
- **零用户初始化收口**：`/api/auth/setup` 默认只接受本机 TCP 对端，`install.sh` 在服务启动前就把管理员建好；口令只经 stdin，不进 argv、ps、`/proc` 或 shell 历史
- **令牌吊销**：改密/删号/降权会 bump 按用户的 epoch，旧 token 立即失效（改密接口同步返回新 token，不打断当前会话）
- **长连接复验**：网页终端（WebSocket）与日志流（SSE）每 60 秒重新对一次身份与到期时间；终端另有 180 秒空闲回收与并发会话上限
- 登录限速双桶：每 IP 每分钟 N 次 + 每个账号每分钟 N 次（代理池轮换 IP 时后一道才管用），失败与吊销都进审计；初始化用独立桶，不挤占首次登录
- **两步验证（TOTP，RFC 6238）**：第二因子只在口令已经对了之后才开口要，登录接口不替人念账号名单；动态口令错一次同样消耗账号限速桶，同一枚不二次接受。密钥存 `users.totp_secret`，接口与 CLI 只回「开没开」。验证器丢失时由 root 执行 `choyeonctl user 2fa-disable <name> --yes`
- viewer 角色全局禁写：写接口在中间件按角色拒绝，只有自己的账号（改密/资料/头像/2FA/登出）例外
- 统一安全响应头：`Content-Security-Policy`（`script-src 'self'`）、`X-Content-Type-Options`、`X-Frame-Options`、`Referrer-Policy`、`Permissions-Policy`、COOP/CORP、`X-Request-Id`；可选 HSTS
- 请求体上限：非上传接口统一 413，上传按 `CP_MAX_UPLOAD_MB` 收口；`X-Real-IP`/`XFF` 只在 TCP 对端属于 `CP_TRUSTED_PROXIES` 时才采信
- 所有 exec 走参数数组 + 名称/域名/unit 正则白名单；子进程统一 `start_new_session` 并在超时时 `killpg`
- 文件管理 realpath 约束在白名单内，拒绝穿越与超限读写；原子写不跟随符号链接，解包拒绝越界路径并清掉 setuid/setgid 位
- nginx 配置变更前保存原文件，`nginx -t` 失败即回滚
- 默认只监听 `127.0.0.1`；审计、错误日志与恢复日志出面板前统一 `redact_creds` 脱敏

发现漏洞请走 [SECURITY.md](SECURITY.md) 里的私密渠道上报，不要开公开 Issue。

---

## UI/UX

- 设计令牌（`web/src/style.css`）+ `dark / light / auto` 三态主题，`auto` 跟随系统且实时响应系统切换
- 首屏外链脚本（`web/public/theme-boot.js`）先落地主题与 `theme-color`，避免暗/亮切换时首帧闪白
- 响应式：桌面侧栏（可折叠）< 900px 自动切抽屉导航；宽表在容器内横向滚动，「操作」列吸附在右侧
- 触屏：`≤900px` 档按钮、输入框、选择器、页签、分页、勾选框补到 44px 高
- 无障碍：语义表单 + `label`/`aria-label`、`role="log"` 日志区、`:focus-visible` 焦点环、支持键盘触发的行操作；破坏性操作统一走共享确认弹窗（默认聚焦「取消」，关闭后归还焦点，`error` 色确认按钮）
- 状态反馈：读取失败是持久错误条而非只有 3 秒 toast；加载中/空/失败三态分开画，刷新按钮转圈时禁用；登录与建号共用同一套用户名/口令校验和文案；原始 `fetch` 上传也复用统一的 401 登出与中文错误解析
- `prefers-reduced-motion` 下动画自动关闭
- ECharts 按需引入 + `ResizeObserver` 自适应；构建按变更频率分包（vue/naive/echarts/xterm）

---

## 备份与恢复

```bash
choyeonctl backup run          # 或 scripts/backup.sh → /root/backups/panel/manual/panel-<tag>-<ts>.tar.gz
choyeonctl backup list
choyeonctl backup jobs --json  # 定时任务 + 每条最近一次执行结果
```

包含 `panel.db`（用 SQLite 在线 backup API 安全拷贝，非直接 `cp`）与 `.env`，默认保留最近 14 份（`CP_KEEP_BACKUPS`，安装脚本变量）。定时备份由 systemd timer 托管，也可登记为 cron：

```cron
0 4 * * * <安装目录>/scripts/backup.sh >> /var/log/choyeon-backup.log 2>&1
```

本机归档落了盘才算这次备份成功；异地一份是附加副本，推不上去只记在目标的上次失败里，不会把成功的备份改判成失败。面板以 root 运行，而异地端点是填写的任意地址，因此拒绝元数据与内网保留地址；对象存储的凭据只走 stdin，接口与日志里只出现遮罩位。

恢复：停服务 → 解压覆盖 `panel.db` → 起服务 → `scripts/healthcheck.sh`（详细步骤见 RUNBOOK 第 4 节）。

---

## FAQ

**Q：首次打开面板显示「初始化面板」而不是登录框，是不是装坏了？**
不是。数据库里还没有任何账号时，登录页会切换成创建管理员表单，填完创建成功即自动登录。如果你已经用安装脚本或 `choyeonctl user create` 建过号，这个入口会关闭并显示登录框——两条路径二选一。

**Q：装完忘了建管理员怎么办？**
先 `choyeonctl user list` 看有没有账号。没有就用 `printf '%s' "$PW" | choyeonctl user create admin --password-stdin --json` 建一个；在**把面板暴露到公网之前**必须完成，否则零用户态的初始化端点会成为抢注窗口（默认已限制为本机对端，但仍不建议留着）。

**Q：为什么不打包成 Docker 镜像？**
面板的职责是管理这台机器上的 systemd、nginx、certbot 与文件。放进容器就要额外打通 docker socket、特权挂载与 host 网络，权限面反而更大，日志还多一跳，排障更慢。

**Q：需要独立数据库吗？**
不需要。面板自身状态存在一个 SQLite 文件（WAL 模式），迁移或备份只需带走 `CP_DATA_DIR`。PostgreSQL/Redis 是「被管理对象」，你有才需要。

**Q：会占用多少资源？**
一个 Uvicorn 进程。指标采集走 psutil 进程内读取，不 fork 子进程；历史曲线写在同一个 SQLite 里。这台交付机上 systemd 记的 `MemoryCurrent` 约 50 MB（被管应用自身的内存不计入）。

**Q：能同时管几台机器？**
一台实例管一台机器。多台就每台装一份，用同一个 Git 仓库和各自的 `.env`；面板本身不提供跨机聚合。

**Q：viewer 角色能做什么？**
只读。所有写接口在中间件层按角色拒绝，只有改自己的口令/资料/头像/两步验证例外，操作留痕在审计日志里。

**Q：升级失败怎么办？**
`choyeonctl upgrade` 在健康检查不通过时自动 `git reset` 回上一个 commit 并重启。注意回滚只覆盖代码，不恢复数据库——数据要回来得走备份文件（上节）。

---

## 参与贡献

改动请先读 [CONTRIBUTING.md](CONTRIBUTING.md)：环境准备、`make lint / make test` 门槛、CI 必须全绿、commit 与 PR 约定。

维护者偏好：先在真实环境验证再交付——构建通过不等于功能修好了，UI 改动要在跑起来的面板里点过一遍。

项目在别人机器上的样子我们看不到，所以最缺的信息是：你的发行版、被管应用的类型、以及哪一步和文档写的不一样。这些直接开 [Issue](https://github.com/Choyeon/choyeon-panel/issues/new/choose)。

## License

[MIT](./LICENSE) · © Choyeon · [提 Issue](https://github.com/Choyeon/choyeon-panel/issues/new/choose) · [提 PR](https://github.com/Choyeon/choyeon-panel/compare)
