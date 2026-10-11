# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。版本号同时登记在 `backend/app/config.py`
的 `VERSION` 与 `web/package.json`，两者必须一致。

## [Unreleased]

### 新增
- GitHub 门面资产：`assets/` 下 logo、banner、架构图、部署流程图与界面示意图（SVG，随仓库渲染）
- `LICENSE`（MIT，此前 README 已声明 MIT 但仓库缺少该文件）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`
- `.github/`：Issue 模板（缺陷 / 功能 / 部署求助）、PR 模板、Dependabot（Actions / npm / pip）
- `backend/tests/test_repo_hygiene.py`：门面一致性回归测试（社区文件齐全、README 引用的本地文件存在、
  版本号三处一致、`assets/` 无孤儿且为合法 SVG、doctor 项数与文档口径一致、Issue 模板字段完整、
  `.env.example` 与 `config.py` 双向对账、banner 与 social 卡版式几何契约、
  品牌标记四处同源且图标不使用 `<text>`、入口图标链接与 `theme-color` 对账）
- `backend/run_tests.py`：unittest 入口，把 `conftest.py` 只为 pytest 提供的两道保护补齐——
  强制忽略外部 `CP_DATA_DIR`（带着它跑 discover 会让各模块的 `setdefault` 失效、用例直接读写线上库），
  并把本轮临时目录收进会话根目录、退出时整块删除；`make test` 与 CI 的单元测试步骤改用它
- 品牌图标体系：`web/public/favicon.svg` 重绘为与 README logo 同形的标记，补齐 `favicon-16/32/48.png`、
  `favicon.ico`（16+32+48 三张 PNG 容器）、`apple-touch-icon.png`、PWA `site-icon-192/512.png`
  与 `site.webmanifest`；新增 `web/src/components/LogoMark.vue` 统一侧栏、移动抽屉与登录页的品牌位
- `assets/social.svg`：GitHub「Social preview」的 1200×600 图源

### 变更
- README 重写为可传播的门面页：徽章行、目录、与 Docker 化面板的对比表、架构说明、FAQ
- `assets/banner.svg` 版式修正：特性胶囊行不再被右侧概览卡片压住（第 5 个胶囊原先越界），
  背景网格平铺加半格偏移，不再沿 banner 顶边/左边留一条贴边亮线
- 品牌标记统一：`assets/logo.svg`、`assets/banner.svg` 的 logo 组与 favicon 收敛成同一套 64 网格几何；
  原先 128 版里的侧栏分组、指标条与三颗窗口点被去掉——它们在 16px 标签页图标上会糊成噪点
- `<meta name="theme-color">` 与真实底色对齐：`#101014`/`#f5f6fa` → `--cp-bg` 的
  `#2e313d`/`#f4f4f4`，运行时改为直接读取该 token，不再在 JS 里抄一份色值
- README 的 9 枚 shields.io 徽章统一加 `cacheSeconds=3600`：GitHub 的图片代理冷缓存超时会把徽章
  渲染成空白，缓存后重复加载不再破图
- 后端为 `.webmanifest` 注册 `application/manifest+json`：部分发行版的 MIME 表里没有这个扩展名，
  回落到 `application/octet-stream` 会让浏览器忽略清单
- 文档与代码一致性核对：`choyeonctl doctor` 项数、CLI 参数表与 `choyeonctl schema --json` 对齐
- `.gitignore` 补齐工具缓存目录（`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`*.egg-info/`）
- `.env.example` 补全 `CP_TRUSTED_PROXIES`、`CP_MAX_EXTRACT_MB/FILES`、`CP_AVATAR_MAX_KB`、
  `CP_TERMINAL_OUT_BUFFER_KB` 五个此前只在源码里存在的键，并修正 `CP_TRUST_PROXY` 的适用场景说明；
  `test_repo_hygiene` 新增双向对账，config 与样例不会再各说各话
- Release 正文改为就地部署口径（`choyeonctl upgrade` / `--no-pull`），并列出 tarball 产物与 SHA256

## [1.1.8] - 2026-10-11

本轮的主线是**让面板说的和机器上发生的一致**，以及把部署做成任意发行版都能一行跑通。

### 新增
- **多发行版一键安装**：Debian 系（Ubuntu/Debian/Mint/Kali）、RHEL 系（CentOS Stream/Rocky/AlmaLinux/Fedora/Amazon Linux）、Arch 系、openSUSE、Alpine（musl）自动识别并安装全部依赖，含 Python 3.11+ 与 Node 22 LTS
- **`choyeonctl doctor` 安全自检**扩展到 12 项只读检查（监听地址、管理员、服务、前端产物、nginx、防火墙、备份、磁盘、文件权限、全局 CLI、证书续期托管、timer 与任务记录对账），每项附可复制的修复命令；网页「安全自检」页同源
- **指标历史持久化**：CPU/内存/网速实时曲线 + 1h/6h/24h/7d 历史（磁盘、负载、磁盘 IO），每网卡独立速率
- **告警历史与应用 HTTP 探活**、定时任务中心（cron 渲染为 systemd timer）、日志检索（unit/时间/级别/正则）、网站与证书总览页
- PostgreSQL 表/行浏览与编辑、Redis 键浏览器（string/hash/list/set/zset 含 TTL 与内存），读路径走进程内 RESP 客户端
- 用户头像与昵称、一键同步、SQLite 在线备份与恢复、审计日志记昵称

### 变更
- **部署契约对齐**：取值优先级统一为 `进程环境变量 > .env > 内置默认`，`upgrade --no-pull` 双向对齐 `.env` 与 systemd unit；`deploy --prefix` 默认就地部署（仓库结构不完整时才退回 `/root/choyeon-panel`）；`deploy --branch` 仅在全新克隆时生效并如实报警
- 应用编辑配置真正生效；"是否上线"改为看主进程实际状态而非期望状态
- 回滚判定改为看部署先后顺序；改名/删除会带走按名字编制的派生态（探活标记、备份任务跟随改名）
- 全站表格操作列收敛为文字按钮形态；侧栏分组与选中态重做；概览页按 1Panel 版式重构
- 依赖与产物安全：坏产物不再覆盖现库；保留名额按写入顺序裁剪，上传产物不挤占自动备份名额

### 修复（安全与正确性）
- 认证与会话：viewer 只读门禁、真正登出、令牌吊销开关、限速在反代后采信真实 IP、口令不再出现在命令行参数与日志
- 请求体上限全局收口（JSON body 不再无界进内存），答复前的收尾统一进中间件
- 文件与终端：原子写、大小限制、符号链接与越界路径拒绝、WebSocket 鉴权与会话复验
- nginx/域名/证书：写入串行化与原子替换、冲突域名遮蔽守卫、`nginx -t` 失败回滚、http2 与证书按实测判定
- 定时任务：执行留痕、oneshot `activating` 期间的重叠闸、执行中停用/删除不再留下 `failed` 幽灵
- 指标与数据浏览：diskstats 扇区单位、PG 混合大小写表名、凭据打码补漏（`pwd` 不再误伤 `PWD=`）
- 子进程统一 `start_new_session` + 超时 `killpg`，避免残留进程组与僵尸进程
- 事件循环：housekeeping prune、SQLite/应用恢复的重 I/O、redis 口令读取移出请求路径

### 工程
- CI：Python 3.11/3.12 双版本后端测试（非 root runner，与生产语义一致）、CLI 与启动冒烟、前端 `vue-tsc` + Vite 构建、shellcheck、systemd unit 校验；单元测试失败时输出摘要注解，无日志权限也能定位
- 测试：非 root 环境可运行（数据目录与文件根改为按测试模块自带默认值），`TestSafePath` 改为从 `config.FILE_ROOTS` 推导
- Release：打包产物写入被读目录导致的 `tar: file changed as we read it` 修复，改为 out-of-tree 打包后移回，SHA256 用 basename

[Unreleased]: https://github.com/Choyeon/choyeon-panel/compare/v1.1.8...HEAD
[1.1.8]: https://github.com/Choyeon/choyeon-panel/releases/tag/v1.1.8
