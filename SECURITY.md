# 安全策略

## 报告一个漏洞

**不要开公开 Issue。** 请通过 GitHub 的私密漏洞报告通道提交：

仓库页 → **Security** → **Report a vulnerability**
（或直接访问 <https://github.com/Choyeon/choyeon-panel/security/advisories/new>）

该通道只对你和维护者可见，且会留下一条可追踪的记录。GitHub 账号不便时，可先发一封仅含"存在漏洞、涉及哪个模块"的邮件到维护者的 GitHub 公开邮箱，等私密渠道建立后再补充细节——**不要在邮件正文里附带可直接利用的 PoC**。

报告里请尽量给出：影响版本、复现步骤（命令或请求）、默认配置下是否可复现、你判断的影响面。能给出修复思路更好，但不是必须。

## 处理节奏

- 维护者在**7 天内**回复确认，说明复现结果与初步评估。
- 修复与公告目标 **30 天内**完成；需要协调下游部署时会说明原因。
- 一经确认为漏洞，会通过 GitHub Security Advisory 公开披露（含 CVE 与否由维护者视情况申请），并在 [CHANGELOG.md](CHANGELOG.md) 标注。
- 修复发布前请勿公开细节。

## 这个项目的威胁模型

面板**默认设计为只监听 `127.0.0.1`**，以 root 运行并直接操作 systemd / nginx / certbot / 文件系统。因此：

- 一旦面板可被公网直连，等价于把整机 root 暴露出去。**"面板端口暴露公网"不算本项目的漏洞，而是部署错误**——请对照 `choyeonctl doctor` 的「监听地址」项与 AGENTS.md 第 7 节的禁止事项。
- 真正算漏洞的是：绕过认证/角色（viewer 能写）、绕过 `CP_FILE_ROOTS` 白名单的路径穿越、命令注入（exec 参数数组被拼接绕过）、令牌吊销失效（改密或删号后旧 token 仍可用）、敏感信息进日志或进仓库。
- 已知的、明确接受的设计边界（**不作为漏洞受理**）：admin 角色本身即等同于机器 root；Web 终端仅 admin 可用且计入并发上限；nginx 配置由 admin 直接生成，其正确性由 `nginx -t` 校验并失败回滚保障，而非沙箱隔离。

## 加固建议（部署侧）

```bash
choyeonctl doctor --json          # 21 项只读自检，fail 必须为 0
choyeonctl ssh-audit --json       # 登录方式对账（只读），见下文 SSH 那一段
choyeonctl site-audit --json      # 站点链路对账（只读）：反代目标没人听、证书不含这个域名、HTTPS 站点不跳转
```

- 面板之外还有正门：`choyeonctl ssh-audit` 核对 22 端口的登录方式（`PasswordAuthentication`、`PermitRootLogin`、
  是否装了 fail2ban/sshguard、authorized_keys 的权限与属主、过去 24h 的失败来源排行）。这一层面板**严格只读**——
  猜错一行 sshd_config 就是把管理员锁在机器外面，而唯一能改回来的入口正是 SSH 本身，所以判词只附修复命令，
  由你自己 SSH 进去执行
- 对外只走 nginx + TLS 反代，`CP_TRUST_PROXY=1` 让登录限速按真实 IP 计数
- `CP_HSTS` 只在面板直连 HTTPS 时开，走 nginx 时由 nginx 统一发送
- `.env` 权限 0600；`CP_DATA_DIR` 含 JWT 密钥与 SQLite，纳入备份但**不要**放进任何公开目录
- 口令一律 `--password-stdin` 传入，避免留在 `ps`、`/proc/<pid>/cmdline` 与 shell 历史
- 管理员请在设置页绑定验证器（TOTP，RFC 6238）：登录接口是全面板唯一免认证入口，会被持续撞库；口令那一段的响应是逐字相同的，第二因子才拦得住已经拿到口令的人。开没开在 `choyeonctl user list` 末列（`2fa` / `-`）、用户管理页与安全自检「登录方式」判词里都看得到。手机不在手边时由 root 执行 `choyeonctl user 2fa-disable <name> --yes`，它会连带吊销该账号全部会话
- 依赖更新由 `.github/dependabot.yml` 跟踪（Actions / npm / pip），安全类 bump 请优先合并

## 支持的版本

| 版本 | 是否获得安全更新 |
| --- | --- |
| 最新 `main` / 最近一个 `vX.Y.Z` | ✅ |
| 更早的 tag | ❌（请升级到最新后复现） |
