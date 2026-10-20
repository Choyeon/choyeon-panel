## 这个 PR 解决什么

一句话说清动机（修哪个 bug / 补哪个能力），并 `Closes #issue` 关联。

## 改了什么

- 

## 怎么验证的

**构建通过不等于功能修好了。** 请如实填写实际做过的验证，没做的项直接删掉，不要留空打勾。

- [ ] `make lint`（ruff 会阻断，mypy 是提示项，vue-tsc 会阻断——与 CI 各步一致）
- [ ] `make test`（后端 unittest；**不要带 `CP_DATA_DIR` 环境变量运行**）
- [ ] `cd web && npm run build`（前端改动）
- [ ] `bash -n` / shellcheck（脚本改动）
- [ ] 在跑起来的面板里点过一遍相关页面（UI 改动必填，附操作路径或截图）
- [ ] `choyeonctl status --json` → `healthy: true`
- [ ] `choyeonctl doctor --json` → `counts.fail == 0`

无法检查的项（例如本机无 systemd / 无 nginx）请写明，如实标为 warn 而不是当作通过。

## 影响面

| 方面 | 是否涉及 | 说明 |
| --- | --- | --- |
| 行为变更 / 破坏性 | 是 / 否 | 若有，迁移方式是什么 |
| 新增或修改配置项 | 是 / 否 | 同步 `.env.example` 与 README 环境变量表 |
| CLI 参数变化 | 是 / 否 | 同步 `choyeonctl schema` 与 AGENTS.md 命令表 |
| doctor 检查项增减 | 是 / 否 | 同步 README、AGENTS.md 里的项数表述 |
| 权限 / 安全边界 | 是 / 否 | exec 白名单、路径守卫、角色门禁、响应头 |
| 文档 | 是 / 否 | README / RUNBOOK / AGENTS / CHANGELOG |

## 自查

- [ ] 没有把口令、token、`.env` 内容、真实域名/公网 IP 写进代码、日志、测试数据或本描述
- [ ] exec 走参数数组 + 白名单，未新增字符串拼接
- [ ] 文件路径操作仍在 `CP_FILE_ROOTS` 守卫内
- [ ] 提交历史干净：没有 `--amend` 已推送的提交，没有把临时/探针脚本或产物提交进来
- [ ] CI 全绿
