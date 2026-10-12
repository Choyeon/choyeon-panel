# AGENTS.md — choyeon-panel 部署与运维规则

> 面向 AI 智能体 / 自动化脚本。**按本文顺序执行即可完成一次可验收的部署**，无需阅读源码。
> 所有命令都支持 `--json`，请以 JSON 字段而非终端文本做判断；退出码见下表。

| 退出码 | 含义 | 智能体应做的动作 |
| --- | --- | --- |
| `0` | 成功 | 继续下一步 |
| `1` | 执行失败 | 读 JSON 的 `error` 字段，按下文「失败处置」处理 |
| `2` | 用法错误 | 参数写错，修正后重试（不要重试第三次，先 `schema` 核对参数） |
| `3` | 前置条件不满足 | 缺 root / 缺文件 / 无 systemd，先补条件 |

---

## 0. 三十秒版（推荐路径）

```bash
# 1) 部署（root，全新机器约 3-8 分钟，前端构建较慢，超时给到 15 分钟）
choyeonctl deploy --port 3210 --json

# 2) 创建管理员（口令自己生成，不要使用示例口令；用 stdin 传，不要写进命令行）
PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
printf '%s' "$PW" | choyeonctl user create admin --password-stdin --json

# 3) 验收：两项都必须为 true
choyeonctl status --json      # healthy == true
choyeonctl doctor --json      # counts.fail == 0
```

若机器上还没有 CLI（未安装 / 未软链），用仓库内入口（`<安装目录>` 就是代码所在目录，默认就地部署）：

```bash
cd <安装目录> && ./bin/choyeonctl deploy --json
```

---

## 1. 前置条件（先检查，不满足就不要开始）

| 条件 | 检查命令 | 不满足时 |
| --- | --- | --- |
| root 权限 | `id -u` 输出 `0` | 用 `sudo -i` 或换 root 用户；面板必须 root（要管理 systemd/nginx） |
| 发行版 | `cat /etc/os-release` | 支持 Debian 系（Ubuntu/Debian）与 RHEL 系（Rocky/AlmaLinux）；其它需手工装依赖 |
| 端口空闲 | `ss -ltn \| grep :3210` | 换端口：`choyeonctl deploy --port 3211` |
| 磁盘 ≥ 2GB | `df -h /` | 清理后再装 |
| 网络可达 | `curl -fsS https://github.com` | 装依赖与克隆代码需要外网 |

**不要**在已有面板实例的机器上随意重复 `deploy`：它会重新渲染 systemd unit，并把本次生效的端口 / 数据目录 / 备份目录写回 `.env`。已有实例请走第 5 节升级（改配置用 `upgrade --no-pull`）。

---

## 2. 部署步骤（逐步验收，失败即停）

### 步骤 1：部署

```bash
choyeonctl deploy --port 3210 --json
```

返回示例：

```json
{ "ok": true, "url": "http://127.0.0.1:3210", "healthy": true,
  "prefix": "/root/www/choyeon-panel", "dataDir": "/root/www/choyeon-panel/data",
  "adminCreated": false }
```

验收：`ok == true` **且** `healthy == true`。后续命令要在返回的 `prefix` 目录下执行（`install.sh` 会把
`/usr/local/bin/choyeonctl` 指向它）；若 `prefix` 与当前 CLI 所在仓库不同，结果里会有 `cliHint`。
若 `healthy == false`，转「失败处置」。

### 步骤 2：创建管理员

```bash
PW="$(openssl rand -base64 24 | tr -d '/+=' | head -c 20)"
printf '%s' "$PW" | choyeonctl user create admin --password-stdin --json
echo "$PW"   # 记录到交付信息里，不要写进仓库
```

验收：返回 `{"ok": true, "username": "admin", "role": "admin"}`。
口令规则：≥ 8 位；用户名规则 `^[A-Za-z0-9_-]{3,32}$`。

口令**必须**走 `--password-stdin`：`--password` 的写法会把它留在 argv 里，同机任何用户
`ps -ef` 都能看到、`/proc/<pid>/cmdline` 世界可读、root 的 `~/.bash_history` 再存一条，
而面板这台机器上还跑着别的业务应用。`--password` 只保留给手工排障。

### 步骤 3：自检

```bash
choyeonctl doctor --json
```

验收：`counts.fail == 0`。`counts.warn > 0` 允许交付，但必须在交付说明里逐条列出（典型 warn：未装 nginx、防火墙未启用、尚无备份）。

### 步骤 4：（可选）接入 HTTPS 域名

```bash
# 只在用户给了域名且该域名已解析到本机时执行。
# 注意：choyeonctl deploy 是非交互的（会置 CP_SKIP_NGINX=1），nginx 询问只发生在终端里手工跑脚本时。
bash scripts/install.sh                    # 在终端中按提示配置反代 + 申请证书，或手工套用：
#   deploy/nginx-panel.conf.example -> /etc/nginx/conf.d/choyeon-panel.conf
#   nginx -t && systemctl reload nginx
#   certbot --nginx -d <域名> --non-interactive --agree-tos -m admin@<域名>
```

验收：`curl -I https://<域名>` 返回 200/30x，且 `choyeonctl doctor --json` 的 nginx 项为 `pass`。

---

## 3. 交付前必须收集的信息

```
面板地址：http://127.0.0.1:<端口>（或 https://<域名>）
管理员：admin / <口令>
健康状态：choyeonctl status --json -> healthy
自检结果：choyeonctl doctor --json -> status / counts
数据目录：<CP_DATA_DIR，默认 <安装目录>/data>
备份目录：<CP_BACKUP_DIR，默认 /root/backups/panel>
```

---

## 4. 命令契约（完整）

`choyeonctl schema --json` 是本契约的权威来源，下表与它同步；若出现不一致，以命令输出为准（并把它当作缺陷上报）。

| 命令 | 关键参数 | 说明 |
| --- | --- | --- |
| `deploy` | `--prefix --port --branch --skip-nginx --no-deps` | 一键部署（依赖→venv→前端构建→systemd→健康检查）；非交互，不会询问 nginx 反代 |
| `upgrade` | `--no-pull` | 拉代码→备份→重建→重启，健康检查失败自动回滚；`--no-pull` 只重建并重新渲染 unit |
| `status` | — | 版本/监听/服务/健康/自检汇总 |
| `doctor` | — | 只读安全与健康自检（当前 14 项，以 JSON 的 `items` 为准），每项带修复命令 |
| `user list\|create\|reset\|revoke\|delete` | `<name> --password --password-stdin --role --yes` | 账号管理；create/reset 用 `--password-stdin` 传口令。改密与 `revoke` 都会吊销旧 token：`revoke <name>` 只踢这个账号，`revoke all` 踢全部（`delete` 必须加 `--yes`） |
| `app templates\|list\|create\|deploy\|rollback\|logs` | `--id --name --template --type --repo --branch --path --port --domain --start-cmd --install-cmd --lines` | 应用全生命周期；`create` 需 `--template` 或 `--start-cmd` 之一 |
| `backup run\|list` | — | 面板数据备份（SQLite 在线备份 API） |
| `firewall list\|allow\|deny\|remove\|on\|off` | `--port --port-to --proto --source --comment --rate --num --expect --yes` | 本机 ufw/firewalld 读写；会把自己锁在门外的操作一律拒绝 |
| `updates` | `--refresh` | 待装系统补丁与安全更新（只读探测）；面板不代跑 apt update/upgrade，升级命令只准备好供管理员执行 |
| `ports` | `--refresh` | 监听端口与对外可达性对账（只读）：每个端口给 internal/expected/blocked/exposed 一档判词，不改绑定也不动防火墙 |
| `start\|stop\|restart` | — | systemd 服务控制 |
| `logs` | `--lines` | journalctl 日志 |
| `schema` | — | 输出命令契约 |

应用模板（`choyeonctl app templates`）：`node-service`、`node-next`、`python-fastapi`、`static-site`。

`deploy --prefix` 默认取 CLI 所在的仓库目录（结构完整时就地部署），只有仓库不完整（无 `.git`）时才退回 `/root/choyeon-panel`；
`deploy --branch` 仅在全新克隆时生效——安装目录已是 git 仓库时会打印 warn 并保持当前分支，切分支请执行 `CP_BRANCH=<分支> bash scripts/update.sh`。

创建示例：

```bash
choyeonctl app create my-api --template python-fastapi --repo https://github.com/u/r.git --port 8000 --json
choyeonctl app deploy --id 1 --json
choyeonctl app logs --id 1 --lines 80
```

---

## 5. 升级、改配置与回滚

```bash
choyeonctl upgrade --json     # 拉代码→备份→重建→重启，健康检查失败自动 git reset 回上一个 commit
choyeonctl upgrade --no-pull --json   # 不拉代码：改完 .env / 手工改过 unit 后，重新渲染 unit 并重启
```

改端口、数据目录、备份目录、文件根目录的正规做法：

```bash
vi <安装目录>/.env                    # 只改需要改的键
choyeonctl upgrade --no-pull --json   # 重新渲染 systemd unit 并重启
choyeonctl status --json              # dataDir / port 应与 .env 一致，healthy 必须为 true
```

`.env` 与 unit 的 `Environment=` 现在是同一套值（`upgrade --no-pull` 会双向对齐），优先级为
`进程环境变量 > .env > 内置默认`，与 `backend/app/config.py` 的文档一致。

手动回滚：

```bash
cd <安装目录> && git log --oneline -5
git reset --hard <旧 commit>
choyeonctl upgrade --no-pull --json   # 比手工 npm run build + restart 多一份健康检查与回滚保障
choyeonctl status --json
```

---

## 6. 失败处置（按症状查表，不要盲试）

| 症状 | 诊断 | 处置 |
| --- | --- | --- |
| `deploy` 返回 `healthy:false` | `journalctl -u choyeon-panel -n 50` | 多为端口占用或依赖缺失；改端口或 `./scripts/install.sh` 重跑 |
| `deploy` 报 unhealthy 但服务确实在跑 | 看返回 JSON 的 `url` / `prefix`，`ss -ltnp \| grep <端口>` | CLI 探测的端口/数据目录来自 `.env`，与 unit 不一致时先 `choyeonctl upgrade --no-pull` 把两边对齐再判失败 |
| `Address already in use` | `ss -ltnp \| grep :3210` | `kill <pid>` 或换 `--port` |
| 页面 404 | `ls web/dist/index.html` | 前端未构建：`cd web && npm run build` |
| `user create` 报「已存在」 | `choyeonctl user list` | 改走 `printf '%s' "$PW" \| choyeonctl user reset <name> --password-stdin` |
| nginx 项 fail | `nginx -t` | 修正配置后再 `systemctl reload nginx`；不要 restart（会断 WebSocket） |
| `choyeonctl: command not found` | `ls -l /usr/local/bin/choyeonctl` | 补软链接：`ln -sf <仓库目录>/bin/choyeonctl /usr/local/bin/choyeonctl`（只有 install.sh 会建这个链接） |
| 服务反复重启 | `systemctl status choyeon-panel` | 触发了 `StartLimitBurst=5`：先修根因，再 `systemctl reset-failed choyeon-panel` |
| 数据库锁 | `choyeonctl status` | 已开 WAL + busy_timeout；仍有错则停服务检查长事务 |

排障细节见 [RUNBOOK.md](./RUNBOOK.md)。

---

## 7. 硬性禁止事项

1. **禁止**把面板端口直接暴露公网——只监听 `127.0.0.1`，对外必须走 nginx + TLS。
2. **禁止**把口令、token、`.env` 内容写进仓库、日志或交付消息（口令只在终端生成并使用）。
   同样**禁止**把口令放在 `--password` 参数里传给 CLI——它会留在 `ps`、`/proc/<pid>/cmdline` 和 shell 历史里；
   一律用 `--password-stdin` 从标准输入传。
3. **禁止**跳过 `doctor` 就宣布部署成功。
4. **禁止**用 `rm -rf` 清理 `data/`、`/root/backups/panel`；需要空间先跑 `choyeonctl backup run` 再清理旧备份。
5. **禁止**手工编辑 `/etc/systemd/system/choyeon-panel.service`——它是 `render_unit` 从 `deploy/choyeon-panel.service` + `.env` 渲染出来的，下次升级会被覆盖。改配置请编辑 `<安装目录>/.env`，再执行 `choyeonctl upgrade --no-pull`（等价于 `bash scripts/update.sh --no-pull`）重新渲染。
6. **禁止**执行 `git reset --hard` 之外的破坏性 git 操作；`reset` 前确认工作区无未提交改动。
7. **禁止**在容器内使用 Docker 化部署——本项目是**原生部署**方案，设计前提就是不用 Docker。
8. **禁止**伪造自检结果：某项无法检查（如无 systemd）就如实标为 warn 并说明。

---

## 8. 新会话接手清单

1. `choyeonctl status --json` 确认实例存在且健康；
2. `choyeonctl doctor --json` 看当前 fail/warn；
3. 有 fail 先按 `fix` 字段处理，再继续新任务；
4. 改任何配置后重跑 `doctor`，并把结果放进结论。
