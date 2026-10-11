# 贡献指南

先说结论：**能跑通 `make lint && make test`、CI 全绿、且改动在真实面板里验证过**，这个 PR 就基本可以合并了。下面解释每一步为什么这么要求。

## 环境准备

```bash
# 后端（需要 Python 3.11+）
cd backend && python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# 前端（需要 Node 20+）
cd ../web && npm ci
```

`requirements-dev.txt` 与生产依赖**刻意分开**：`scripts/common.sh` 的 `venv_create()` 只装 `requirements.txt`，测试包进生产环境会被 doctor 与备份一起带上机器。测试跑起来还硬依赖 `httpx2`（starlette 的 `TestClient`），漏装会在干净环境里直接 `RuntimeError`——本地 venv 常因为带着旧 httpx 而退化成一堆 warning，于是"本地绿 CI 红"。

常用命令：

```bash
make help          # 查看全部目标
make backend-dev   # 后端 http://127.0.0.1:3210
make web-dev       # 前端 :5173，/api 代理到 3210
make test          # 后端 unittest
make lint          # ruff + mypy + vue-tsc
make fmt           # ruff 格式化
make release-dry   # 本地模拟发布打包（不推送）
```

Windows 也能开发：systemd / journalctl / nginx 这类 Linux 专属能力会降级为提示，API、页面与测试均可运行。

## 跑测试要注意的三件事

1. **用 `backend/run_tests.py` 跑测试，不要直接 `python -m unittest discover`。** 每个测试模块必须在 `from app import …` **之前**设置 `CP_DATA_DIR` / `CP_FILE_ROOTS` 默认值——配置在 import 时就读取了。`unittest discover` 不读 `conftest.py`，缺了它那道"外部 `CP_DATA_DIR` 一律忽略 + 临时目录跑完整块删除"的保护：环境里带着 `CP_DATA_DIR`（生产机上很常见，`.env` 里就有）时各模块的 `setdefault` 全部失效，整轮用例会直接读写线上库。`make test` 与 CI 都走 `run_tests.py`。
2. **不要带 `CP_DATA_DIR` 环境变量跑测试**，尤其是生产机上；`run_tests.py` 会打印它忽略了这个变量并改用会话临时目录，看到这条提示说明你的调用方式不对。
3. **探测解释器的测试要遮位。** CI 的 3.11 job 的 `PATH` 里一定有 `python3.11`，测试若关心版本路由就必须把 3.13/3.12/3.11 全部用不合格替身遮住，否则测的是宿主机而不是脚本。

## 代码约定

- 后端：类型注解完整，`mypy` 非阻断但新增错误请修；`ruff` 阻断（含 import 排序 `I001`）。
- 所有对外 exec 走**参数数组 + 白名单正则**，不拼接 shell 字符串；子进程统一 `start_new_session`，超时 `killpg`。
- 文件路径操作必须过 `safe_path` 一类的 realpath 守卫，限制在 `CP_FILE_ROOTS` 内。
- CLI 的每个命令都要支持 `--json`，退出码只用 `0/1/2/3` 四态；改了参数就同步更新 `choyeonctl schema` 与 [AGENTS.md](AGENTS.md) 的命令表。
- 前端：`<script setup>` + TypeScript；样式与主题值走 `web/src/style.css` 的设计令牌，不要在组件里硬编码颜色；文案用中文，图标用 naive-ui / 内置 SVG，不用 emoji。

## 提 PR 的流程

1. 从 `main` 切分支，命名随意，但一个 PR 只做一件事。
2. 本地 `make lint && make test`，前端改动再跑 `cd web && npm run build`。
3. **UI 改动要在跑起来的面板里验证**——构建通过不等于界面修好了。截图或简述操作路径放在 PR 描述里。
4. 提交信息用中文，一句话说清"改了什么/为什么"，动词开头（`修复 …` / `新增 …` / `重构 …`）。一个提交对应一个可独立回滚的变更。
5. 打开 PR，CI 必须全绿（后端 3.11/3.12、前端构建、shellcheck、systemd unit 校验）。
6. 若改动了行为、配置项或命令契约，同步更新 `README.md`、`AGENTS.md`/`RUNBOOK.md` 与 `CHANGELOG.md`。

合并策略由维护者决定，_contributor 不要在本地 rebase 已推送的提交，也不要 `--amend`。_

## 发布

版本号同时存在于 `backend/app/config.py` 的 `VERSION` 与 `web/package.json`，发版时两处一起改。流程：提交 → 打 `vX.Y.Z` tag → 推 tag → `.github/workflows/release.yml` 自动打包并建 Release（产物：源码包、前端 dist 包、`SHA256SUMS.txt`）。本地可用 `make release-dry` 先看产物对不对。

## 门面资产（README 里的 SVG）

`assets/*.svg` 是项目在 GitHub 上的第一屏，改动后请用真实渲染验证，不要只看 XML 语法：

- `backend/tests/test_repo_hygiene.py` 已经把守三件事：资产合法且被文档引用、chips 行不压到右侧卡片、标签字宽不溢出胶囊（CJK 按 1em 的保守估算 + 10% 渲染器余量），以及平铺网格必须带 `patternTransform` 偏移——从 `(0,0)` 铺满画布的描边网格会在 banner 顶边留下一条贴边亮线。
- 像素级复验：用浏览器直接打开 `.svg` 文件（1:1 渲染，不套 markdown 容器），把 `getBoundingClientRect()` 按 `viewBox.width / svgRect.width` 换算回画布坐标再判重叠；朴素 `getBBox()` 会忽略祖先 `transform`，量出来是假重叠。
- CI 与多数开发机没有中文字体和 rsvg/inkscape，**不要**指望服务端把 SVG 转 PNG。
- `preview-light.svg` 由 `preview-dark.svg` 按设计令牌替换生成（`sed` 逐色映射，保留 `id="pd-*"` 不动），改深色版必须同步生成浅色版。
- 品牌标记是**四处同源**：`web/public/favicon.svg`、`assets/logo.svg`、`assets/banner.svg` 的 logo 组、`assets/social.svg` 的 logo 组共用同一套 64 网格几何（后两者按 ×2 摆放），测试 `test_品牌标记三处同源且不依赖字体` 会把分叉判出来。图标一律不许用 `<text>` 画字母——缺字体的渲染器会把它整个丢掉，退化成一块纯色方块。
- 改完标记要按 16/32px 复核：浏览器里把 SVG 序列化成 `data:` URL 交给 `<img>`，画进 canvas 后按最近邻放大，肉眼确认曲线没有糊成噪点（16px 下描边宽度换算不足 1.5px 就会断）。
- `web/public/` 下的 PNG/ICO 兜底图标由上述 canvas 路径导出（16/32/48、`apple-touch-icon` 180 直角满幅、PWA 192/512），`favicon.ico` 是 16+32+48 三张 PNG 拼的容器格式；不要手工维护，改 SVG 后重新导出。
- `assets/social.svg` 是 GitHub「Settings → Social preview」的图源，导出 1200×600 PNG 上传；GitHub 会缩到 640×320 显示，正文最小字号不要低于 22。
- README 的徽章走 GitHub 图片代理 `camo.githubusercontent.com`：**代理失败在 GitHub 页面表现为破图，直接 curl `img.shields.io` 却是 200**，所以要用浏览器打开仓库首页、数 `naturalWidth === 0` 的 `<img>` 才算验证。徽章 URL 的自定义参数一律从 `?` 起（`/badge/label-message-color?cacheSeconds=3600`）；`/badge/…-颜色&cacheSeconds=…` 这种写法 shields 仍回 200，但 `&…` 被并进颜色段解析失败，自定义色和缓存时长会一起丢掉——`test_readme_徽章链接形态` 拦的就是这个。

## 行为与沟通

- 项目讨论、Issue、提交信息默认中文；代码标识符用英文。
- 安全漏洞请按 [SECURITY.md](SECURITY.md) 私密上报，不要开公开 Issue。
- 维护者评审最看重两点：**是否真的在线上验证过**，以及**是否把线上事实（服务是否活着、备份是否落地）如实写进结论**。不能检查的项就标成 warn 并说明原因，不要伪造通过。
