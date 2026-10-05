# HANDOFF — choyeon-panel 优化交接

> 供下一个 session 直接接手。最后更新：本轮优化收尾。

## 【主线状态】

目标：把 `Choyeon/choyeon-panel`（原生 Linux 服务器管理面板）做成**参考一线开源面板（1Panel / Cockpit）的成熟形态** —— 前端可用、后端可靠、**CLI 可部署**、**规则文档让 AI 智能体能一次部署成功**。

当前状态：**两轮优化全部完成并通过验证**。代码在 `/workspace/choyeon-panel`（分支 `main`，基于 `7c9549b`），**尚未 commit**（有意留给使用者审阅后提交）。

- 第一轮：后端加固 + 前端 UI/UX + 部署脚本 + CI（见下）
- 第二轮（最近）：对标 1Panel（`1pctl` CLI）/ Cockpit 的**CLI 化、模板化、自检化**，并产出 AI 部署规则

## 【最近一轮新增（第二轮）】

| 交付 | 位置 | 要点 |
| --- | --- | --- |
| **CLI `choyeonctl`** | `backend/cli.py` + `bin/choyeonctl` | `deploy / upgrade / status / doctor / user / app / backup / start·stop·restart / logs / schema`；全命令支持 `--json`；退出码 `0/1/2/3`；`schema` 输出机器可读契约 |
| **部署模板** | `backend/app/templates.py` | `node-service`、`node-next`、`python-fastapi`、`static-site`；网页端与 CLI 共用，显式传参优先于模板 |
| **就绪探针 + 自检** | `backend/app/routers/meta.py` | `/api/ready`（就绪，库不可用返 503，免认证）、`/api/templates`、`/api/doctor`（9 项只读自检，每项带修复命令） |
| **自检页** | `web/src/views/Doctor.vue` | 圆环进度 + 逐项卡片 + 一键复制修复命令；stagger 入场动画、骨架屏 |
| **动效令牌** | `web/src/style.css` | `cp-rise`（stagger 上浮）、`cp-shimmer`（骨架微光）、`cp-pop`、`cp-press`；`prefers-reduced-motion` 自动降级 |
| **AI 部署规则** | `AGENTS.md` | 前置条件 → 三步部署（每步验收字段）→ 交付信息 → 命令契约 → 失败处置表 → 8 条硬性禁止事项 |
| 联动 | `apps.py`（创建应用支持 template）、`deps.py`（`/api/ready` 免认证）、`install.sh`（软链 `choyeonctl`）、`ci.yml`（CLI 冒烟） | |

## 【本 session 完成的事（第一轮摘要）】

### 1. 后端加固（`backend/`，Python/FastAPI）
| 文件 | 改动要点 |
| --- | --- |
| `config.py` | 新增 `.env` 解析与 `_int/_bool/_path/_paths` 类型化读取（带取值范围钳制）；新增 `CP_TOKEN_TTL_HOURS`、`CP_LOGIN_LIMIT`、`CP_MAX_UPLOAD_MB`、`CP_TERMINAL_MAX_SESSIONS`、`CP_AUDIT_KEEP`、`CP_HSTS` 等；`VERSION=1.1.0`、`as_dict()` |
| `database.py` | `busy_timeout=15s`、`synchronous=NORMAL`、外键开启；新增索引；`append_deploy_log` 裁剪（256KB）、`prune_audit`、`prune_deployments`、`deployments.commit_sha` 迁移 |
| `security.py` | JWT 增加 `ver`（按用户 epoch）+ `jti`；`bump_user_epoch` 实现改密/删号后旧 token 立即失效；限速次数改为读配置 |
| `deps.py` | 统一安全响应头：CSP、`nosniff`、`SAMEORIGIN`、`Referrer-Policy`、`Permissions-Policy`、COOP/CORP、`X-Request-Id`、可选 HSTS |
| `main.py` | lifespan 把遗留 `running` 部署标记 failed；启动同步备份 timer；`change_password` 改密后返回新 token（不踢当前会话）；setup 接口补 `None` 保护 |
| `util.py` | 子进程 `start_new_session` + 超时 `killpg` 杀进程组（返回 124）；新增 `run_shell_async` 避免阻塞事件循环 |
| `routers/apps.py`、`services/apps_service.py` | 路径参数强类型化；新增 **回滚能力**（`rollback_app`：`git reset --hard <sha>` + 重启）与 `commit_sha` 记录 |
| `services/nginx_ops.py` | `nginx -t` 失败自动回滚原配置；渲染增加 `http2 on;` 与安全响应头 |
| `routers/terminal.py` | 非管理员关闭 4403；并发会话上限（4503）；关闭时 `killpg` + wait + `kill -9` 兜底，消除僵尸进程 |
| `services/files_service.py`、`routers/files.py` | 上传/下载体积上限（`CP_MAX_UPLOAD_MB`）、`content-length` 预检返回 413、rename 禁止跨目录 |
| `routers/extras.py` | 改密/删用户后吊销 token；非管理员隐藏 Telegram/Webhook 密钥 |
| 新增 | `app/log.py`（统一日志前缀）、`requirements.txt` |

### 2. 前端 UI/UX（`web/`）
- `style.css` 全量设计令牌化；`App.vue` 三态主题（dark/light/auto，跟随系统）；`index.html` 内联防闪脚本
- `Shell.vue`：桌面侧栏可折叠，<900px 自动切抽屉；主题切换下拉；汉堡按钮与账号按钮补 `aria-label`
- 响应式与无障碍：表格 `scroll-x`、44px 触控目标、`:focus-visible` 焦点环、`prefers-reduced-motion`、语义表单 + `label`/`aria-label`、日志区 `role="log"`
- `api.ts` 重写：`AbortController` 超时、401 统一回调、角色缺省 `viewer`（最小权限）
- `router.ts` 新增 404 路由 + `NotFound.vue`；`Login.vue` 改为真表单 + 内联错误提示
- `Dashboard.vue`：ECharts 按需引入、`ResizeObserver` + `MutationObserver` 随主题重绘、骨架屏
- `vite.config.ts` 分包：入口 588KB → **18.6KB**（vue/vendor/xterm/echarts/naive 按变更频率拆分，路由懒加载）

### 3. 部署工程（新增）
- `scripts/`：`install.sh`（依赖→构建→systemd→健康检查→可选 nginx/certbot）、`update.sh`（备份→构建→重启→健康探测→**失败自动回滚**）、`backup.sh`（SQLite 在线 backup API）、`healthcheck.sh`、`uninstall.sh`、`common.sh`
- `deploy/choyeon-panel.service` 加固：重启风暴限制、内存/CPU 上限、`OOMPolicy=continue`、日志与命名空间限制（**刻意不启用 `SystemCallFilter`，注释已写明原因**）
- `deploy/nginx-panel.conf.example`：TLS 参数、`http2 on`、SSE 必须 `proxy_buffering off`、登录限速可选块
- `.github/workflows/ci.yml`（后端 3.11/3.12 测试 + 冒烟、前端类型检查与构建、shellcheck、unit 校验）、`release.yml`（tag 打包 + Release）
- `Makefile`、`RUNBOOK.md`、`.env.example` 补全

### 4. 质量保障
- 测试 **23 → 45 → 52 项**（新增 `tests/test_hardening.py`：配置解析、令牌吊销、登录限速、日志裁剪、文件守卫、命令超时、vhost 渲染、**部署模板、就绪探针**）
- **ruff 0 问题、mypy 0 问题**（修掉 13 处潜在 `None` 崩溃与类型混用，36 处刻意降级异常显式标注 `# noqa: BLE001`）
- 端到端冒烟：`/api/health` ✅、七项安全响应头 ✅、setup/重复 setup 403 ✅、前端构建无告警 ✅

## 【关键决定与规则】

1. **先诊断后修复**：所有改动都定位根因（如终端僵尸进程 → `killpg`；部署日志膨胀 → 裁剪），而非表面规避。
2. **回滚优先可验证**：`update.sh` 健康检查失败自动 `git reset --hard` 回上一 commit；nginx 配置变更先保存原文件，`nginx -t` 失败即回滚。
3. **安全默认值收敛**：角色缺省 `viewer`、非管理员不返回告警密钥、上传有体积上限、令牌可吊销。
4. **不启用 `SystemCallFilter`**：Python/uvicorn 调用面广，误配会导致进程被 SIGSYS 杀死且日志难查，收益不抵风险（unit 内已注释）。
5. **面板必须 root 运行**（要管理 systemd/nginx/certbot），因此 systemd 加固只选不影响写 `/etc`、`/root` 的选项。
6. **刻意吞异常要显式标注**，不靠全局 ignore 掩盖——36 处加了行内 `# noqa: BLE001` 而非配置级忽略。
7. **未做破坏性操作**：未 `git commit`、未 `rm -rf` 用户数据、未改动线上配置。

## 【下一步（可选）】

| 优先级 | 事项 | 说明 |
| --- | --- | --- |
| 高 | 提交与推送 | `git add -A && git commit -m "..."`（未提交，等你确认） |
| 中 | 真实机验证 `scripts/install.sh` | 沙箱内无 systemd/nginx 全链路，建议在一台干净 Ubuntu/Debian 上跑一遍 |
| 中 | ECharts 进一步瘦身 | 现 464KB 已路由懒加载；如需可只注册用到的图表类型 |
| 低 | 前端单元测试 | 目前只做类型检查与构建；可引入 Vitest 覆盖 `api.ts` 与工具函数 |
| 低 | i18n | 界面文案目前硬编码中文 |
| 低 | MCP Server | 对标 1Panel 的 MCP 接口，让 AI 客户端直接调用面板能力（当前靠 CLI + AGENTS.md） |

## 【快速恢复命令】

```bash
cd /workspace/choyeon-panel
git status --short                 # 看本轮改动清单
make help                          # 全部常用命令
cd backend && python3 -m unittest discover -s tests   # 52 项测试
./bin/choyeonctl doctor                                # 命令行自检
./bin/choyeonctl schema --json                         # AI 可读的命令契约
cd backend && ruff check . && mypy app                # 静态检查（均应为 0 问题）
cd web && npm run build                               # 前端构建
bash scripts/healthcheck.sh                           # 若已安装到本机
```

**收尾完成，session 可结束。**
