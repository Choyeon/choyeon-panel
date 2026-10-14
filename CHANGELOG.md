# 更新日志

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循
[语义化版本](https://semver.org/lang/zh-CN/)。版本号同时登记在 `backend/app/config.py`
的 `VERSION` 与 `web/package.json`，两者必须一致。

## [Unreleased]

### 新增
- 防火墙页（运维工具 → 防火墙，仅管理员）。此前面板只能告诉你"防火墙开着/没开着"，
  改规则要另开一个 SSH 会话。现在 ufw 与 firewalld 的规则都在表里：编号、动作、来源、
  端口区间、地址族、备注，窄屏（<900px）换成卡片列表。添加走 `ufw allow` /
  `firewall-cmd --add-*`（firewalld 同时写 permanent 并重载），删除按编号但必须带上
  前一次列表里读到的签名——编号会随任何一次删除前移，签名对不上就拒绝执行而不是盲删。
  `ufw delete 3` 不带 `--force` 会打印 "Proceed with operation (y|n)? Aborted" 而退出码仍是 0，
  所以删除除了带 flag 还要重新列表确认那条真的没了。
- 自锁保护：面板拒绝任何会让这台机器进不去的写操作。受保护端口取 sshd 实际监听的端口
  （含 `sshd_config` 的 Include 片段）、反代到面板的 nginx 入口端口、面板自身在非 loopback
  地址上的监听端口，以及你当前这个浏览器实际连进来的端口（Origin / Referer / X-Forwarded-Host）。
  只有当被删的这条是该地址族唯一的放行来源时才拦——`22/tcp` 和 `22:25/tcp` 同时存在时删掉前者
  仍然通，照单全拦会让人以为面板在胡闹；IPv6 的孪生规则不替 IPv4 顶岗，管理员大概率走 v4。
  没有"我已确认风险，继续执行"这类绕过参数：这条守卫的意义就是不给误操作留后门。
- 设置页的防火墙卡片原先写着"面板对防火墙只读，开启/改规则请 SSH 手动操作，以免误锁 SSH"，
  现在改为指向新页并说明锁死风险由面板承担。
- `backend/tests/test_batch8a.py`（64 条）与 `test_batch8d.py`（17 条 CLI 用例）锁定上述语义：ufw / firewalld 规则表解析、
  `LIMIT IN` 是带限速的 accept 因而算放行、自锁判定的同族冗余例外、v4/v6 签名互不相同、
  删除前签名比对、firewalld 运行期与 permanent 两份配置的差异、删掉 permanent 最后一条 ssh
  放行时跳过 `--reload`、非管理员一律 403，以及只读的 ufw/firewalld 探测服务。
- `choyeonctl firewall list|allow|deny|remove|on|off`：终端里拿到的是同一套守卫与同一份判定，
  不再有"页面拦住了、SSH 却能一把梭"的缝隙。`list` 的文本模式带编号、族、受保护标记和签名，
  签名就是 `remove --expect` 要回传的那个值；`off` 必须 `--yes`，`remove` 必须带 `--expect`。
  没有"我确认风险，继续"这类参数——守卫的意义就是不给出路。命令契约三处（`cmd_schema` 输出、
  argparse 定义、AGENTS.md §4 表格）由 `test_deploy_contract` 的一致性用例锁在一起，
  改一处漏改另两处会在测试里失败。
- 删掉一条放行后，如果同一端口的另一个地址族规则还在放行，服务、CLI 与页面三处都会点名它
  （`still_open`）：ufw 的一次 `allow` 写 v4+v6 两条，而 `delete N` 只删一条编号，只报"已删除"
  等于骗人说端口关了。孪生按端口身份认（端口、区间、协议、来源、动作、方向），不能按状态表
  解析出的 `token`——那条字段只有 firewalld 才有，ufw 的规则全是 None，第一版就是这么把
  22/80/443 全报成"仍在放行 3399"的。
- 防火墙页窄屏（<900px）的删除按钮原先实测 28x44，两个字的按钮点不准；补齐横向内边距到 52x44。
  端口输入框限长 5 位，避免粘进整段文字后才发现报错。

- 系统补丁可见性：`choyeonctl updates`、`GET /api/updates`、自检项「系统补丁」与服务页的「系统补丁」页签共用一份数据。
  此前面板连"这台机器还差几个安全更新"都答不出来，而未打的安全更新正是最常见的横向跳板。探测严格只读：不跑
  `apt update`（会写索引、和 dpkg 抢锁），不装任何包，升级命令只准备好供管理员自己执行——装包会顺带重启别的服务、
  可能换内核，这些不该由"刷新一下页面"触发。发行版先按 `ID`/`ID_LIKE` 判族，判不出来再看包管理器在不在；
  apt / dnf / pacman / zypper / apk 五套输出各配一个解析器，`dnf check-update` 有待装时退出码就是 100（那是约定不是失败）。
- 判不出安全更新子集的发行版如实报 `null`，页面显示"判不出"而不是 0：把"不知道"写成"没有"，管理员就不会再看第二眼，
  而这一行恰恰是唯一催他去装补丁的地方。同理，Arch/Alpine 上根本没有 `unattended-upgrades` 这类单元，自动更新状态报
  「该发行版不适用」，不再对着 systemctl 空跑五趟再断言"未开启"。
- 「系统补丁」页签给出待装 / 安全更新 / 自动安全更新 / 需重启四颗状态 chip、带「安全」标记的包列表、可复制的升级命令，
  以及 apt 索引过期提醒（索引旧于 24 小时就说明结论可能偏旧）。有待装安全更新时页签点名一颗红点——这一页平时没人看，
  而它最需要被看见。
- `backend/tests/test_batch9a.py` 锁住五套解析器、"判不出≠没有"的口径、只读探测（`apt update`/`upgrade`/`install`
  一次都不许被调用）、30 分钟缓存与 `--refresh` 回源、CLI 在未识别发行版时按前置条件不满足退 3，
  以及 AGENTS.md 写的自检项数必须等于 `_CHECKS` 的真实长度（文档口径由 `test_repo_hygiene` 一并校验）。
- 端口暴露面对账：`choyeonctl ports`、`GET /api/ports`、自检项「端口暴露面」与服务页的同名页签共用一份判定。
  自检里原先有"面板自己绑在哪"和"防火墙开没开着"两项，两者都对，合起来却答不出管理员真正怕的那个问题——
  我有没有在不知情的情况下开了个口子。做法是把三份本就躺在机器上的事实拼起来：`ss -tlnp` 的实际监听表、
  防火墙此刻放行的端口、面板登记的应用端口与域名，再给每个端口一档判词（internal 仅本机 / expected 预期对外 /
  blocked 绑在全网但防火墙未放行 / exposed 外部可达且不在预期清单）。只读：不改绑定、不动防火墙——把端口收回
  回环要改的是那个服务自己的启动参数，面板替不了它，猜错了就是一个正常服务被关掉。
- 两处判定不按"看起来安全"写。未启用防火墙时非回环端口一律算可达：关掉 ufw 就是没有过滤，把它们标成 unknown
  等于把最该看的那台机器标成看不了。docker 发布的端口也算可达，哪怕 ufw 表里没放行——它走的是 docker 自己在
  PREROUTING 里的 DNAT，压根不经过那张表，"防火墙没放行"对它是假安慰。
- 配了域名反代却绑在 `0.0.0.0` 上的源站是这一页最该抓出来的一条：任何人都能绕开 TLS 和限速直连 `IP:端口`，
  判词会点名到具体应用。nginx 的 80/443、sshd、反代到面板的入口端口则先认下来——第一条就是假警报的页面，
  没人会看第二次。链路本地的 mDNS/LLMNR 也一样放行，它们绑全网是设计如此。
- `backend/tests/test_batch10a.py`（40 条）锁住 ss 解析（v4/v6 按端口合并、`127.0.0.53%lo` 归回环、非 root 时
  没有进程名、ESTAB 行不算监听）、四档判词的边界、这一层只执行 `ss` 一条命令、5 分钟缓存与 `--refresh` 回源、
  读不到监听表时按前置条件不满足退 3，以及自检项数与 AGENTS.md 的口径一致。
- 登录安全对账：`choyeonctl ssh-audit`、`GET /api/ssh-audit`、自检项「登录安全」与服务页的同名页签共用一份判定。
  端口那一层答的是"外面进得来吗"，这一层答的是"进来之后凭什么站得住"：`sshd -T` 取有效配置（拿不到就退回解析
  `sshd_config` 与 Include 片段，并在报告里写明读的是哪一份、因此哪些值没能生效）、authorized_keys 只列指纹和
  权限/属主/是否符号链接（公钥正文不进面板，少一处复制粘贴的出口）、`/etc/shadow` 找空口令与多余 uid=0、
  过去 24h 的 journalctl 认证日志统计失败次数与来源 IP 排行、fail2ban/sshguard 看有没有人在拦，最后和端口那一层
  对个账：sshd 在听的这个端口外面到底可达不可达。判词分三档——要紧 / 值得处理 / 知道就好，每条附可复制的修复命令。
- 这一层写死了只读：不以任何写模式打开 sshd 配置、不 reload sshd、不碰 authorized_keys。面板写错一个 nginx 站点
  顶多网站打不开，写错一行 SSH 配置就是把人锁在自己机器外面。修复一律以"回去自己执行"的形式给出，
  `test_batch11a.py` 里有一条用例直接扫源码，出现写模式 `open(` 或改配置的动词就红。
- 来源排行按两种日志形态取 IP。爆破行有一半长成 `Connection closed by authenticating user root <ip> [preauth]`，
  不带 `from`；原先只按 `from` 取，这批来源会从排行里整体消失，报告成"没几个人在打"。
- 自检状态和判词分档是两件事：空口令、多余 uid=0 意味着"这台机器可能已经被人进来了"，算 fail；关口令、
  装 fail2ban 属于加固建议，算 warn。混成 fail 会让人以为部署坏了去看错地方，也让 `doctor fail == 0` 这条
  交付门禁失去意义。
- `backend/tests/test_batch11a.py`（138 条）锁住配置解析（`sshd -T` 缺失时的降级、Match 块不算全局值、重复键取值、
  注释行不变成一个设置）、authorized_keys 只出指纹不出正文、只读承诺、两种日志形态的来源统计、判词分档与排序、
  5 分钟缓存与 `--refresh` 回源、读不到 sshd 配置时按前置条件不满足退 3，以及 AGENTS.md/README 与命令契约的口径一致。

- 存储占用对账：`choyeonctl storage`、`GET /api/storage`、自检第 16 项。磁盘那一栏原先只说"还剩多少"，
  答不出"被谁占掉了"——真出事时人还是得 SSH 上去一层层 `du`。现在一次扫描给出分区余量、journald
  落盘量、面板数据/备份/登记根各自的总量与一级子目录榜、全场最大的 8 个文件，并只在三类情况下出声：
  分区已用 ≥90%（唯一给到"要紧"的一档）、journald ≥2GiB、备份目录 ≥4GiB（会说清攒了几份、跨了多久）。
  严格只读：不删文件、不改配额、不动 keep，清理动作一律给可复制的命令，删旧备份指向 `choyeonctl backup`
  而不是 rm。两处局限如实标注：每个根最多扫 2 秒/4 层，没扫完的行写着"数字是下界"；登记的根可以互相
  包含（数据目录就落在应用根里面），合计只算最外层那一份，否则同一批数据会被算两遍。
  这一项刻意永不 fail、也不进一键修复白名单：磁盘占用是趋势不是故障，"一键修复存储"只会删错东西。
- `backend/tests/test_batch12b.py`（60 条）锁住遍历语义（符号链接只按链接本身记账、跨设备跳过、深度与
  预算两种没扫完都算下界、根级散落文件归根不归分支）、判词阈值边界 ±1、journal 读不到时报 None 而不是 0、
  5 分钟缓存与 `--refresh` 回源、合计排除嵌套根，以及只读承诺与"不在 FIXABLE 里"。
- 运维工具 → 服务管理的「存储占用」页签把这份对账画出来：分区余量、journald、合计、三档计数、
  备份产物各一行 chip；下面每个目录一行给出总量条、一级子目录构成（超过八个只列最大的，其余写
  「另有 N 个」）和这一行的可信度（没扫完 → 数字是下界；读不到 → 几处读不到；和别的目录是同一份数据 →
  不重复计入合计）。判词默认收起「知道就好」那几档，要看就点展开；带命令的条目给复制按钮，点了只把命令
  送进剪贴板，并写明「面板不会替你执行，请 SSH 进去自己跑」。这一页刻意一个修复按钮都没有：删掉的文件回不来。
  376px 实测整页不横向溢出，长目录名与清理命令折行而不是截断——截了就没法照着敲，这一页只剩下好看。

- 存储占用对账补上挂载点这一层（`choyeonctl storage` 与 `GET /api/storage` 的 `mounts`）。原先面板只认得
  数据目录所在的那块盘，于是同一台机器上三种真实故障全都看不见：别的分区写满、inode 用尽、文件系统被内核
  重挂成只读。inode 那一类最难发现——ext4 的 inode 表在 mkfs 时就定死了，用尽之后新建任何文件都报 No space
  left on device，而容量列还能显示剩几十 GB，面板会一路报"盘很空"。现在逐块挂载点给出容量、inode 使用率与
  是否只读，statvfs 读不到的仍列出来并标"挂着却读不到"（设备掉线、网络存储断了最容易长成这样）。判词分三类：
  容量吃紧、inode 用尽（≥90 值得处理、≥97 要紧，标题直接写"容量才用 X%"）、被重挂成只读（附
  `dmesg --level=err,warn | tail -50`，不给 `mount -o remount,rw`——先把错误读出来再决定要不要解冻）。
  伪文件系统不进表；snap 的 squashfs 与光驱镜像照列但不判——它们天生显示 100% 已用，那是只读镜像的
  性质；overlay 也不进，它报的是下层目录那块盘的数字，多列一行会让人以为是
  两块盘各吃掉一半；只读的 RAM 盘（本机 `/run/credentials/*` 五个）不进表——它写不进东西，永远不会满。
  百分比与磁盘余量那一栏同口径：写不进去的都算已用（含 ext4 预留块），同一页上两个百分比才说的是同一块盘的
  同一件事；`df` 的 Use% 会低几个点，它把预留块既不算 Used 也不算 Avail。
- `backend/tests/test_batch13a.py`（36 条）：`/proc/mounts` 夹具是从生产机原样抓回的 28 行（带
  `errors=remount-ro` 的 ext4 根、没有 inode 表的 vfat ESP、五个只读 tmpfs），自己编样例行造不出这些坑。
  另锁八进制转义还原、同挂载点取最后一次挂载、f_bavail 口径、空文件系统不报百分比、读不到仍列出、
  inode 阈值 89/90/97 三档、面板自己那块盘不重复报容量、快照式文件系统只列不判、判词 id 不重复，
  以及 CLI 挂载点段的文案与"输出里不许出现 None"。
- 挂载点表补上「这块盘是谁」，并把同一层信息搬进页面（存储占用页签第四列）。同一块文件系统挂在两处时，
  两行的容量、inode 会一模一样，读起来像"两块盘都用到 21%"，管理员会去找一块并不存在的盘。面板自己就踩在
  这条上：unit 里开着 `PrivateTmp=true`，本进程的挂载命名空间因此比终端多一个 `/var/tmp`，它就是根分区
  （实测两个路径的 `st_dev` 都是 2049）。做法是标注而不是隐藏——`os.stat().st_dev` 相同的行给
  `same_dev_as`，CLI 与页面都写成"与 / 是同一块盘"，终端里少看到的那一行在页面里会出现，反过来也一样，
  两边对数才对得上；`os.stat` 失败（挂载点权限被改、网络存储断了）不标注，不拿猜测填这一格。
  只读镜像的判定也从判词层收回 `_parse_mounts`：解析时就落 `snapshot` 标志，列表与判词消费同一个结论，
  不再一处进表一处重查 `SNAPSHOT_FSTYPES`。
- 页面这一侧新增 4 列挂载点表（挂载点 / 容量 / inode / 这块盘）：容量条与 inode 条并排，≥90 才染红，
  和后端判词的 `DISK_TIGHT_PCT` / `INODE_WARN_PCT` 是同一条线，不会出现"条是绿的、判词说紧"；
  vfat 这类没有 inode 表的给 `—` 并写明"这类文件系统没有 inode 表"，而不是 0%。表头按吃紧排序，
  超过 24 个挂载点时最紧的先进来并在标题说明另有多少；`/proc/mounts` 读不到时整表换成原因而不是空表。
  376px 实测：整页不横向溢出，表格在卡片内部横滚（可见 286px / 内容 666px，能滚到底），
  8 行路径与标记无截断。
- `test_batch13a.py` 扩到 40 条：`_parse_mounts` 的 snapshot 标志（squashfs 为真、带 `errors=remount-ro`
  的 ext4 为假）、同一 st_dev 的第二行被标注、不同 st_dev 不标注、`os.stat` 抛 OSError 时不标注。
- 内存压力与 OOM 对账：`choyeonctl memory`、`GET /api/memory`、自检第 17 项。概览的内存仪表只答「占了
  多少」，答不出管理员紧接着要问的三句：机器有没有因为缺内存变慢、内存被谁拿着、内核有没有已经杀过
  进程。这台机器实测过 55 万次直接回收（`pgscan_direct` 550363）、累计换出约 12GB、PSI some 自启动
  阻塞 82 秒，而已用只有 33%——这三段经历在总量曲线上一条都不存在。
  速率取自 0.8 秒内的两个 vmstat 快照（换入 / 换出 / 直接回收 / allocstall / 主缺页），测不出给 None
  而不是 0：「这个窗口没采到」折成 0 就等于宣布「这台机器没有在换页」；计数器倒退同样只能 None，
  倒退填不出负速率。allocstall 按 zone 拆成几项，老内核只有其中几个，把所有存在的相加，一个都没有才
  给 None，不猜那个叫 normal 的。
  归属走 cgroup v2 的 `memory.current` / `pids.current`，面板管理的应用单元认领回应用名；`.slice` 是
  分组、那个数字已经含组内所有子单元，CLI 与判词都写明「分组，含子单元」，免得有人把它和自己的子单元
  再加一次。面板自己那一行无条件留在表里（当前 40.1MB，是自己 `MemoryMax=512M` 的 8%）：它被上限杀掉
  就是整个面板没了，不该因为排不进前 12 名而隐身。
  OOM 先读 vmstat 的 `oom_kill` 一个整数，非 0 才起一次 journalctl 取受害者；计数说杀过而当前 boot 的
  内核日志里没有 OOM 行（日志已轮转）就标 `counter-only` 并写明原因，不交一份看着完整其实空的名单。
  受害者认两种内核写法（`Killed process 1234 (node)` 与 `task:python,pid=1234`），认不出的行只留原文
  不填名字——猜错的 pid 比没有 pid 更容易把人引到错方向。
  严格只读：不 kill 进程、不改 sysctl、不动 unit 的 MemoryMax/MemoryHigh，也不进一键修复白名单。上限
  该设多少是容量决策，只有管理员知道业务峰值；页面给的处置一律是可当场复制的只读命令
  （`journalctl -k -b | grep -iE 'out of memory|oom-kill'`、`cat <单元>/memory.peak`、
  `systemctl show <单元> -p TasksMax -p NRestarts`）。OOM 也是这一项唯一会报 fail 的分支：其它项最多
  warn，而内核中断过业务进程是事故，不是提示。
- `test_batch14a.py`（155 条）锁住这些方向：HugePages_* 是页数不许按 kB 折算、PSI 缺 full 行的老内核
  仍算读到、`memory.max` 写着 `max` 时不折成数字、速率倒退给 None、allocstall 多 zone 求和、cgroup v1
  形态不做兼容猜测、`.slice` 与 session scope 的归属、面板自己那行不被 top-N 挤掉、阈值边界（已用
  79/80/90、swap 17/50/80、PSI 1%/5%、上限 80%/95%、线程配额 80%、换出 2MB/秒、直接回收 256 页/秒）、
  `oom_kill=0` 时一个子进程都不起、journalctl 超时降级成 counter-only、两种受害者写法与认不出只留原文、
  判词 id 不重复且每条都带「为什么」、所有处置命令扫过禁止写入的词表、CLI 文本不出现 None、
  schema / argparse / AGENTS.md / README / SECURITY 五处的命令与自检项数一致。

- 系统服务页新增「内存压力」页签，把上面那份探测画成可核对的四层：结论条 + 判词表 +
  「谁拿着这些内存」（cgroup 按 `memory.current` 排序，面板自己那一行无条件在表里，
  `.slice` 标出「分组，别把它和自己的子单元再加一次」）+ 按 RSS 的前几名进程，另加
  swap 设备与 OOM 名单。条子的颜色一律反查后端判词的 id（`sevOfFinding`），前端不重抄
  一遍阈值——两处各写一份 80%，改一处就会悄悄分叉。读不到的节（PSI 未编译、cgroup v1、
  速率窗口没采到）画「没采到」而不是折成 0，后者会被读成「没有在换页」。页签红点只认
  「要紧」：天天都有的小波动长期亮着就等于没亮。swappiness / overcommit_memory /
  min_free_kbytes / panic_on_oom 只展示不修改，处置列全是只读命令 + 复制按钮。

- 改动未生效对账：`choyeonctl pending`、`GET /api/pending`、自检第 18 项。这一页管的是「装上去的东西
  没进正在跑的进程」这一层，此前面板所有页面都看不见它：`updates` 说没有待装补丁、服务页全绿、
  `doctor` 一路 pass，而生产机上 postgres 的 26 个后端从 10-08 起还映射着 liblzma5 5.8.1-1+deb13u1
  的旧 inode（u2 由 unattended-upgrade 当天装上），同一份映射还挂在 unattended-upgrades 自己身上。
  判据是 inode：映射行末尾的 `(deleted)` 只说明路径没了，同路径当前文件 inode 相同就不算被替换，
  `/dev/zero`、`/SYSV*`、memfd、匿名映射这些天生带 `(deleted)` 的噪声必须滤掉，否则整机每一条都是假阳性；
  dpkg 安装时会把包内记录的 mtime 原样落到盘上，所以 mtime 回答不了「这文件什么时候装上的」，
  只有 ctime 能，而 ctime 晚于进程启动才算「装上时它还在跑旧的」。元包 `linux-image-cloud-amd64`
  的名字后面接架构、自己的版本号一直在变新，拿它当新内核会把每台正常机器判成待重启，
  所以只认 `linux-image-` 后面紧跟数字的包。nginx 的参照时刻取最晚启动的 worker 而不是 master，
  `nginx -s reload` 只换 worker。`/run/reboot-required` 来自 update-notifier-common，服务器镜像默认不装它，
  只认这个标记的实现在这台机器上永远显示绿灯。
  严格只读：不 restart、不 reload、不 daemon-reload、不 reboot，也不进一键修复白名单——这些动作会断连接、
  会杀业务，时机只有管理员知道。fix 全是给人复制走的命令，并且带上前置确认（`systemctl status <unit>`
  先看会断多少连接、`nginx -t` 先过语法再 reload）。安全库（libc6 / libssl / openssh / sudo / systemd /
  liblzma / zlib / libxml2 / libcurl / nginx / postfix / dovecot 一类的包名前缀表）还踩着旧映射时报 fail
  而不是 warn，这是自检里唯一一个由外部包状态触发 fail 的项，宁可响一次让人查一次。
  进程扫描实测 142 个进程 40 毫秒，上限 4000 个进程或 3 秒预算，超了就如实标 budget_hit 并说明
  只扫了前多少个；dpkg 归属按路径缓存、整轮最多查 12 个不同路径（`dpkg -S` 一次 0.2-0.5 秒）。
  整轮约 1.5 秒，缓存 120 秒。
- `test_batch15a.py`（85 条）锁住这些方向：路径含空格的映射行整串保留（maps 的 path 可以带空格，
  按 `split()[5]` 取会只剩前半段）、扫描中途退出的进程整条跳过（它名下的 deleted 映射还在，
  报出来只会让人去查一个已经不存在的进程）、映射 inode 与当前文件一致时丢弃、orphan 单元排到表尾并写明
  「systemctl 管不到」、版本比较按 dpkg 的退出码而非替身的布尔值（`returncode=False` 时 `False == 0` 成立，
  替身自己就能把旧内核说成新内核）、元包不计入待重启、`rc` 残留行忽略、判定不了时不落成「要重启」、
  nginx 参照用 worker（回归 master 写法会误报还没 reload）、sshd 的相对 Include 按 /etc/ssh 补基准并展开 glob、
  非包路径不起子进程、所有 spawn 出去的 argv 字面量里不许出现 restart / reload / daemon-reload / reboot
  （用 AST 扫，grep 会被 findings 里那些本该给人执行的命令误伤）、读不到的节只许产 info 级判词、
  CLI 文本 CJK 列按显示宽度补齐、schema / argparse / AGENTS.md / README / SECURITY 五处的命令与自检项数一致、
  时序字段只许给 ISO 且读不到留 None（不许折成 1970 年）、判词文案里的时刻不带 `T` 和时区、
  格子类名从 `style.css` 或 `:deep(` 选择器可达（新类漏掉时用例必须红，用反例验过一次）。

- 服务器页新增「改动未生效」页签，把上面那份对账画出来：结论条 + 判词表 +「还在跑旧文件的进程」
  （单元 / 几个进程 / 已经跑了 / 踩着的是哪份文件；路径整行给出不省略号，node_modules 里那几个同名文件
  要靠整行才分得开）+「配置和 unit 改过之后」（nginx / sshd / systemd 三层，各自写明拿什么比的）。
  颜色与文案一律反查后端判词的 id，前端不重抄安全库名单和阈值；处置列的命令只从后端那条判词里取，
  同一件事两处各写一遍迟早分叉。
  时刻一律由后端给 ISO 字段（`ref_start_iso` / `started_iso` / `newest_mtime_iso` / `boot_iso`）：
  前端的 `fmtDateTime` 只吃字符串，把 epoch 喂给它得到空串；判词文案那一路没人接手，所以后端自己
  把它缩成 `2026-10-08 06:50:51` 再放进句子——同一页出现两种时间写法会被读成两件不同的事。
  读不到的字段留 null 并画「读不到」，不折成 1970 年。
  这一页一个按钮都不按：restart / reload / daemon-reload / reboot 都会断连接或杀业务，
  每条处置只是一个复制按钮，toast 里写明「面板不会替你执行，请 SSH 进去自己跑」。
  页签红点同样只认「要紧」：安全库的旧映射要重启才算补上，天天亮着等于没亮；
  配置改过还没重载走页内告警说话。
  同一类坑又踩回来一次：`.pd-file + .pd-file` 那条分隔线写在 `<style scoped>` 里，`h()` 造出的单元格
  没有 `data-v-*`，构建、类型检查、接口与页面文本断言全绿，线上就是没有那条线——只有真 DOM 量得出
  `border-top-style: none`。改成 `.cp-tbl :deep(...)` 之外，新增 `StyleReachabilityTests` 把
  「render 函数里用过的类必须样式可达」固化为用例。
- 负载与 CPU 归因对账：`choyeonctl load`、`GET /api/load`、自检第 19 项「负载与CPU」与服务层
  `backend/app/services/load_service.py` 共用同一次 1 秒采样。立项依据是这台机器自己的读数：面板 unit 带着
  `CPUQuota=80%`（cgroupfs 里就是 `cpu.max 80000 100000`，0.8 核），`cpu.stat` 累计 `nr_throttled 84` /
  `throttled_usec 3694975`，而 load 长期在 0.1 上下。被限流的进程既不在 run queue 也不进 D 状态，
  此前的任何页面都看不见这类「机器不忙但活干不完」，只会给出"负载正常"这个相反结论。判据一律除以核数
  （每核 2 倍算要紧、1.5 倍算值得处理）：同一句 load 4 在 2 核上是过载，在 64 核上半载都不到。
  「CPU 不够」与「磁盘跟不上」分两条判词走，后者只由 `/proc/stat` 的 `procs_blocked` 触发。
- 冒烟时踩到的一处真误读，已固化成用例：`/proc/loadavg` 第 4 字段是「可运行/已注册任务总数」，第一版把它当成了
  blocked，2 核机器 load 0.05 时页面写出「227 个任务正卡在等 IO」——那是整机线程数。现在 blocked 只有
  `/proc/stat` 一个来源，`_loadavg()` 刻意不产出同名键，解析、判词、集成三层各钉一遍。
- 「谁在占」只列窗口增量大于 0 的进程：空闲机器上 /proc 里有上百个 0ms 的内核线程，全排进表等于让人去查十几个
  kworker；一个都没有时明说「这一秒没有任何进程消耗 CPU」，不把空白读成采样坏了。
- 处置一律是给人复制走的只读命令，且只用这台机器上真装了的工具（`pidstat` / `mpstat` / `sar` / `iostat`，
  这台机器没装 `iotop` 也没装 `atop`）。面板不改 `CPUQuota=` / `cpu.weight`、不 renice、不 kill、不 restart：
  放宽配额是容量决策（这台机器的配额本来就是拿来保业务进程的），杀进程和重启会直接断业务。这条同时写进
  AGENTS.md §7 第 10 项。自检项数 18→19，AGENTS.md / README / SECURITY 三处同步。
- `backend/tests/test_batch16a.py`（129 条）：loadavg 段不许出现同名 blocked 键、核数换算在 2 核与 64 核上给出
  相反结论、blocked 为 0 时不许走 IO 分支、guest / guest_nice 不重复计账、限流窗口必须两次快照相减
  （`80000/100000` 折成 0.8 核）、被限流的单元不在备查清单里重复出现、读不到的节只许出 info 并如实带原因、
  每条 fix 都是本机存在的只读命令，以及 AST 级守卫（本服务不 import 子进程模块、不以写模式 open 任何文件）。
  6 条变异反证（把 227 当 blocked、列出 0ms 进程、窗口不相减、guest 算两遍、要紧项不折 fail、按第一个括号切
  comm）全部被对应用例拦住。
- 服务器页新增「负载与 CPU」页签：结论条 + 16 颗 chip（「累计停顿」只在 PSI 有累计停时时出现，所以平时是 15 到 16 颗） + 一条 CPU 时间分解堆叠条 + 判词表
  +「这一秒谁在占 CPU」/「哪些任务不在 CPU 上（D / R）」/「配额与限流」三张表。页签红点只认「要紧」，
  iowait 偏高、被宿主抢走时间片这类用页内告警说话——天天亮就等于没亮。
- 这一页的颜色一律反查后端判词的 id（`ldSevOf('load-io', 'load-cpu', 'load-warm')`），不写第二套阈值：
  前端抄一份 2.0 / 25 / 20 / 40，服务层下次调常量就是颜色先于判词变红，同一件事在页面上给出两个档位。
  `test_batch16a.py` 里新增 `LoadFrontendWiringTests`（11 条）钉住三层口径：`/load` 路径三处一致、
  `refresh` 必须是数字（⑯A 冒烟时前端按惯例传 `true` 得到 422）、前端 `maxAge` 与服务层 TTL 同值、
  页面上用到的判词 id 必须存在于服务层（含 `throttle:` 这一族）、脚本段不许出现与阈值常量的比较、
  负载区不许出现任何写接口调用、堆叠条不含 guest 段、`一 tick = 1000/hz` 必须用后端给的 hz，
  以及页签不许被塞进 admin 分支（那一路由 viewer 可读）。附一条反证用例，证明阈值检测器不是空转。
- 「这一秒没有周期结束」与「这一秒没被限流」在限流列里是两句话，不是一个 0：cgroup 的 `cpu.stat`
  只在周期收尾时更新，把读不到折成 0 等于告诉管理员配额很宽松。

- 负载页的说明文案去掉了 markdown 反引号（`systemctl show -p CPUQuota` 这类）。同一份 NOTES 既喂 CLI
  也喂网页，CLI 里反引号只是怪，网页里会**原样画出来**——线上实测那一条就写成了带反引号的一句话。
  改完补了两条用例：NOTES 与负载页模板里不许再出现反引号。

- 自检加了第 20 项「配置来源」：`.env` 在不在，以及它和 unit 里那六行 `Environment=` 是不是同一套部署参数。
  这一项管的是"改配置的正规入口"本身——`.env` 不存在时 AGENTS.md §5 让人去编辑一个根本没有的文件，
  面板却全绿；两边不一致时健康检查探的是 `.env` 那份、服务听的是 unit 那份，一次成功的部署在文档推荐的
  验收命令上看起来像失败。两种情况现在都会被说出来：缺文件是 warn（fix 就是 `choyeonctl upgrade --no-pull`，
  它会把文件补齐），漂移是 fail 并逐键列出 `.env=… unit=…`；读不到 unit（无 systemd 的机器）也如实 warn，
  不冒充 pass。对账只看那六个会同时写进两边的键，`CP_TOKEN_TTL_HOURS` 之类各管各的不参与，免得假阳性。
  这一项不进 `FIXABLE`：它是只读对账，修复是给人复制走的那条命令。文档三处项数（AGENTS.md §4、README、
  SECURITY.md）与 `len(_CHECKS)` 由用例对齐，改代码忘了改文档会红。

- 面板登录的第二因子（TOTP，RFC 6238）。设置页「账号安全 → 两步验证」生成密钥并出二维码，
  扫进验证器后拿一枚当前口令 `enable`，验中才写库——"能扫上码"不等于"绑定了"。`users` 表加
  `totp_secret TEXT NOT NULL DEFAULT ''`（走 database.py 的列检查补 ALTER，不是 schema 本体，
  已有库直接升级），空串就是"没开"的唯一判据，没有第二份 enabled 标志可以跟它分叉。
  密钥只在 setup 响应里出现一次：状态接口、用户管理页、`choyeonctl user list` 三处都只报开没开，
  任何一条读路径都不回显密钥本身。
- 实现只用标准库（`backend/app/totp.py`，hmac/hashlib/base64/struct/secrets）：这一层坐在登录路径上，
  多一个依赖就多一分供应链面，而算法本身只有几十行。`STEP_SECONDS` / `DIGITS` / `VERIFY_WINDOW`
  三个数字只有这一处出处：既进口令算式，也经 `provisioning_uri()` 写进 otpauth URI（验证器 App 读的
  是 URI 里的 period/digits，不是它自己猜的），还随状态、setup 和每一条 401 响应经 `totp_params()`
  下发——登录页输入框的位数上限和"每多少秒换一枚"的提示都取自响应，前端不抄数字。
  时钟容差取 ±1 步：手机与服务器的钟不可能秒级同步，0 步就是"快了 20 秒的人登不进"；再宽就是
  拿安全换方便，一步 30 秒，±1 已经是 90 秒的接受窗口。
- 第二因子只在**口令已经对了**之后才开口要。不存在的账号、存在的账号、开了 2FA 的账号在口令这一关
  给出逐字相同的响应，否则 `/api/auth/login`（全面板唯一免认证入口）就把账号名单念给撞库的人听。
  口令对但动态口令错同样要消耗账号限速桶——动态口令只有 10^6 种，不限速的话它就是那条"每次失败
  不花钱"的旁路。已用步数按账号记在进程内存（`_used`），同一窗口内重复提交同一枚一律拒绝，抓到一枚
  口令的人不再有 90 秒的重复使用窗口；口径是步数严格递增（用过 99 之后 100/101 仍然可以），超窗的旧
  步数由 `cleanup_used()` 回收，删账号时连带清掉那个桶里的键，不让已删除的用户名把字典撑大。
  登录失败动作从一条扩到三条（`login_failed` / `login_totp_missing` / `login_totp_failed`），
  `security.py` 的注册表与登录路由实际用到的动作由用例对着写，加一档漏记一档会红。
- 两处只有实测才会撞见的坑：`_CODE_RE` 用 `[0-9]` 而不是 `\d`——Python 的 `\d` 是 Unicode 意义上的
  十进制数字，全角 `１２３４５６`、天城文 `१२३४५६` 都会放行，随后 `hmac.compare_digest` 对非 ASCII
  字符串直接抛 TypeError，粘一枚全角口令就是一个 500，偏偏落在唯一免认证的入口上；otpauth URI 的 label
  把冒号转义成 `%3A`（Google Authenticator 的 key-uri 规范写法），裸冒号主流 App 也认，但 issuer 与账号
  的分界只该由这一处决定，账号名里带冒号时原样进去就会切错。口令归一化去掉空格、连字符，以及
  `str.isspace()` 不认的零宽残留（BOM、ZWSP、行/段分隔符）——粘贴自二维码文本或验证器详情页时
  带进来的正是这几类。
- `choyeonctl user 2fa-disable <name> --yes` 是验证器丢失时的逃生口，只给 root：网页端"关闭两步验证"
  要求再输一枚当前有效的动态口令，而需要这条命令的人恰好输不出。它拿 root 权限换掉一个账号的第二因子，
  不是拿口令换。本来就没开的账号一律如实失败（不静默 ok），关完连带吊销该账号全部会话——来跑这条命令
  的原因是"手机不在手边"，而那种时候并不确定手里有票的是不是本人。网页端关闭不吊销自己的会话：
  那是本人拿着口令和当前口令的正常操作，把正在用的票撕掉只会在重新绑定那一步把人踢回登录页。
- 登录安全对账多了两判：一个账号都没开 2FA 时是一条 note（提醒可以加第二因子），开了的和没开的混在
  一起时点名 `with_totp/accounts`——半开状态最容易被误读成全站已开。viewer 的只读放行清单
  （`SELF_SERVICE_PREFIXES`）加上 `/api/auth/2fa`：开关的都是调用者自己的第二因子，与改密、改资料同形；
  这份清单只能显式扩张，`test_batch5c.py` 用精确集合钉住。账号取自 token 而不是请求体里的名字，
  五条路由都验过这一点。
- 前端两处按实测收敛。设置页抽屉分步走：第一步只核口令，"动态口令"输入框和"绑定后别关这个抽屉"的提示
  要等密钥生成完（`twStep === 'scan'`）或走关闭流程时才出现——早出现就是让人在验证器还没有这个账号的时候
  填一枚填什么都错的口令，而在关闭流程里那句提示本身是错的。二维码库只在开启流程里 `await import('qrcode')`，
  并在 `manualChunks` 里单独成块（实测 `qrcode-*.js` 25.84 kB）：只写动态 import 而让它跟 `@vicons`
  挤在同一个 vendor 里，下载时机一点没变，"懒"只停在写法上。登录页口令框的 `maxlength` 挂在 `NInput`
  而不是 `:input-props` 里——后一种写法线上实测既没落成 attribute、也没落成 `el.maxLength`（一直是 -1），
  位数上限静默失效。用户管理页新增「两步验证」只读列（表头实测 `用户 / 角色 / 两步验证 / 权限说明 / 操作`），
  管理员在这里只看得见状态，不能替别人开关。
- `backend/tests/test_batch23.py`（60 条 + 30 组子测试）：口令算式对 RFC 6238 附录 B 的已知向量、
  窗口恰好一步、一枚口令只用一次、步数往前走不卡死、`normalize_secret` 的宽松粘贴形态与
  ValueError 边界（太短/太长/非 base32 一律 400 而不是 500）、URI 与 `totp_params()` 和算式三者一致、
  账号名转义、`cleanup_used` 只丢不可能再被接受的步数、密钥长度取 RFC 4226 推荐的 SHA1 分组大小；
  登录网关侧断"没有口令时问口令、口令错时不暴露 2FA 状态、坏口令也抬账号桶、IP 桶仍然先于这一切"；
  迁移侧断那一列来自 `migrate()` 而非 schema 本体，并在一份旧库上往返升级；另有 CLI 逃生口的
  `--yes`/账号不存在/本来没开三种失败、吊销会话与审计留痕，以及源码级用例钉住"状态响应与 `user list`
  里搜不到密钥本身""位数来自响应""qrcode 走动态 import 且单独成块""输入框只在能填的时候出现"。


### 修复

- 「这个服务活着吗」全站只剩一个出处。这条判词此前在仓库里被抄了四遍，各有各的偏差：
  应用列表把 `reloading`（`systemctl reload` 期间就是这个态）当成没在跑，把 `activating`
  （Type=oneshot 的定时任务、正在启动的 JVM 应用）直接写成「已停止」，而同一时刻定时任务中心
  显示「正在执行」；cron 那侧另有一份 `systemctl show` 解析，只看 `active`，于是会对一个正在
  reload 的 unit 补发 restart；概览与详情页的端口探测也排在 `active` 后面，`activating` 期间
  端口其实已经监听，页面却报「未运行」。现在 `systemd_ops.unit_state/unit_states` 一次
  `systemctl show -pId -pLoadState -pActiveState -pMainPID` 派生出三个字段——`active`（能干活）、
  `busy`（别去打扰它，含 activating/deactivating/reloading）、`missing`（本机没有这个 unit）——
  四处判词改成调用同一个函数，`is_active`/`is_busy` 保留为它的薄封装。
- 「探测失败」不再被粉饰成绿灯，也不再被粉饰成红灯。`systemctl` 没回话时（超时、被安全模块拦、
  输出被截）`unit_state` 返回 `state="unknown"`：自检项报 warn「查不到……（systemctl 没有回话）」
  而不是 pass，列表与详情显示「状态未知」而不是「已停止」。
- 覆盖式恢复在探测不出状态时按最坏情况处理。`_stop_for_overwrite` 原先靠一次 `is-active` 的输出
  决定要不要停服务，探测本身失败就被当成「没在跑」，然后直接往一个正在写的文件上覆盖——服务
  其实在跑时，写坏半个数据库再启动就是数据损坏。现在 `unknown` 一律先停再恢复；`not-found`
  明确说「本机没有这个 unit」（首次恢复或改名之后）而不是无声跳过；`failed` 恢复完先
  `reset-failed` 再 start，并在日志里写明为什么多这一步；没有 systemd 的容器/WSL 走单独一条
  判词，不再对不存在的服务发命令。每个分支都必须留下一句话，用户点完恢复不该只看到进度条。
- 前端的状态说法收敛成一张表（`ui.ts` 的 `UNIT_TAG` + `unitTag()`）。应用列表与详情页此前各自把
  一个布尔渲染成「运行中/已停止」两选一：正在启动的应用在列表是「已停止」，点进详情还是
  「已停止」，三秒后它才变成运行中；unit 根本不存在时同样是「已停止」，管理员于是去点「启动」，
  收到一条和 nginx、和端口都无关的错误。现在列表与详情共用同一份措辞，「单元不存在」还会把
  「启动/重启」置灰并写明「本机没有这个 systemd 单元，请重新立即部署」。
- `backend/tests/test_batch32.py`（55 条 + 38 子用例）钉住上述语义：`parse_show` 只认带 `Id` 的
  块、缺 unit 时补 `UNKNOWN_STATE` 而不是漏 key、`_stop_for_overwrite` 六个分支各自的文案与
  「返回 (stopped, restart)」真值、恢复后 start 失败只记日志不抛（否则用户看不到已经写回去的
  文件）、自检项对 unknown/missing/activating 三态不给 pass。另有源码级守卫，防止判词再次分叉：
  `apps_service`/`backup_service`/`cron_service`/三个 router 里不得再出现 `get("ActiveState")`、
  `== "active"` 这类字面比较，每个文件至多一处 `parse_show` 定义，`UNIT_TAG` 全站只有一处，
  两个视图必须从 `'../ui'` 引 `unitTag`。前端文案另用 jsdom + SSR 渲染实测九个状态的可见中文
  （运行中/正在启动/正在停止/单元不存在/启动失败/重载中/已停止/状态未知，以及旧接口字段缺失时
  的兜底），构建通过不再被当成界面写对了的证据。
- SQL 浏览与行编辑的超时改成由服务端执行。此前面板这侧只有 `wait_for + kill_group`，杀掉的是
  psql 客户端：postgres 后端那条语句（`pg_sleep`、深 OFFSET 的 `count(*)`、被行锁卡住的
  `UPDATE`）感觉不到连接断开，会一路跑到自然结束，期间占着一个 `max_connections` 名额和它已经
  拿到的锁；关掉标签页再点几次，库是被自己拖死的，而面板早就回了「超时」。现在
  `CP_PG_STATEMENT_TIMEOUT_MS`（默认 20000）和 `CP_PG_LOCK_TIMEOUT_MS`（默认 5000）经 PGOPTIONS
  下发给服务端，与 `standard_conforming_strings` 走同一条通道；面板的墙钟取服务端上界 + 10 秒，
  保证永远是服务端先取消。等锁超时如实报「拿不到锁」，而不是让工作线程排在锁后面直到墙钟。
- 两条读外部进程输出的路补了字节上界，都拦在真正的开销发生之前。RESP 客户端读大 key 有
  `MAX_REPLY_BYTES`，而它失败之后转的 redis-cli 回退用的是 `communicate()`：集合类型只按 200 条
  截，每条 1MB 就是 200MB，几百 MB 整个搬进 root 进程内存，后面还跟着一份同量级的 JSON。现在
  回退自己按同一个 `MAX_REPLY_BYTES` 边读边限，超限先杀进程组再报错，正常回包照旧 reap。psql 的
  stdout 有 512KB 守卫，stderr 却是 `.read()` 到底：`DO $$/RAISE NOTICE` 这类循环的通知全走
  stderr，现在改成读满但不留满（保留开头 512KB 并标注已截断），而且继续读到 EOF——提前停会让
  管道填满、psql 卡在下一条错误上，用户只剩一句没头没尾的「psql 超时」。
- 恢复 SQLite 备份前扫描应用目录（找候选库）加了条目与时间上界。uploads/、media/ 不在排除名单里
  （它们确实可能装着业务库），几百万元素的树会把一次点击变成几分钟的 scandir 风暴，而文件页、
  备份列表、概览从同一个线程池取 worker。命中上界是报错而不是返回半份结果：调用方靠「目录里有没有
  和备份同名的库」（旧格式靠「最大的那个库」）定位恢复目标，半份名单会认错库、把备份覆盖到另一个
  库上，那是数据丢失，不是慢一点。
- 行定位的 ctid 改用 `fullmatch`。`CTID_RE.match` 配上 `$` 允许末尾多一个换行过关，`"(1,1)\n"`
  会被当成合法 tid 直接拼进 `ctid = '...'::tid`，与 `is_ident` 那处是同一类缺陷。
- 文件页 caption 里「entries.length > 200 才虚拟滚动」删掉了：表格的 `virtual-scroll` 是无条件
  开的，那句话描述了一个代码里不存在的阈值。
- `backend/tests/test_batch31.py`（23 条）锁定上述语义：两个超时都取自配置、`0` 时不注入、
  PGOPTIONS 整串只有两个单引号且注入值只能是数字、墙钟恒大于服务端上界、stderr 读到 EOF 但只保留
  头部、回退路线不再出现 `communicate()`、超限回包先杀进程组、正常回包一定被 reap、strip 与原样
  两种语义不变（编辑 string key 回填不能静默吃掉末尾换行）、五种畸形 ctid 全拒、扫描上界数的是
  「扫过的条目」而不是「命中的库」、新旋钮在 `.env.example` 有登记。

- 文件页补上两个资源上限，都拦在"省下成本的那一步"之前。列目录原本是给目录里每一项做一次
  `stat` 再整份返回：node_modules、构建缓存这类目录动辄几十万个 inode，一次请求就是几十万次
  syscall 加一份几十 MB 的 JSON，而这个接口走线程池的同步 I/O，多开几个标签页能把线程占满、
  浏览器当场卡死。现在截断排在 `stat` 之前（先 `stat` 完再截等于什么都没拦），上限是
  `CP_MAX_DIR_ENTRIES`（默认 5000，与解压条目上限同一量级），`total/truncated/maxEntries` 一并
  回传，页面那句「当前 N 项」说的是目录里真有多少项、不是表格里渲染了几行，前端不再自己抄一份
  上限。打包原本没有落盘预检：临时文件和产物都写在**源目录所在的同一个文件系统**上，而面板以
  root 运行，"打包整个应用目录"写满盘的代价是这台机器上所有业务一起停摆，不是这一次请求失败。
  现在按 gzip 的上界（源内容总字节数）先问一次剩余空间，装不下就在写下第一个字节之前拒掉，
  目录里不留临时文件；这趟 `walk` 数到超过剩余额度就返回，已经确定装不下时不必把几百万个 inode
  数完，符号链接按 `tar` 的口径只算链接本身（一个指向大文件的链接不会被算成几 GB）。真算漏了
  还有原来的 `OSError` 分支兜着——它会删掉临时文件并把 ENOSPC 原样报出来。
- 前端写操作入口加了源码级不变量测试，并把查出来的 6 处重复提交补齐。连点两次「保存/部署/恢复/
  执行」不是"多做两次"：第二次带的是第一次还没写回前端的旧字段，等于把刚保存的改动盖回去，或者
  两个进程在同一份 working tree 上互相踩。`backend/tests/test_batch29.py` 从 `web/src` 反推
  "哪些函数真的会写"（`api.ts` 里方法为 POST/PUT/PATCH/DELETE、或只给了 `body` 走默认 POST 的都算，
  Firewall/Sites 那种页面内自建 `const api = { add: () => req(...) }` 也顺着找一层），再看每个挂在
  模板事件、下拉 `run:`、`inp.onchange` 上的处理函数：第一个 `await` 之前必须有查在途标志的早退，
  标志必须在 finally 里被置回假值（漏了这一步按钮会永久置灰，比重复提交更难发现），而且得有绑了
  同一标志的 `:disabled`。目前钉住 17 个页面/组件里的 71 个写入口，它替我查出 6 处：AppDetail
  的 `save`/`deploy`/`sync`/`saveUnitFile`、
  Settings 的 `saveChan`、Doctor 的 `fix`（那一条修复是重启面板自身，重启窗口里的第二次请求只会以
  一句网络错误收场）。确认弹窗、`useCachedFetch` 的在途合并、Apps 的跨行并发、终端 PTY 输入四条
  是有意豁免的，豁免理由写死在测试里，不是让它闭嘴。
- 阈值类"改了就存"的输入框不能早退：连点步进器时丢掉最后一次恰好是用户想要的值。Settings 的
  `saveOne` 改成按键串行的队列——同一个键在途时，同值的直接丢掉，不同值的记成"这条写完还要再写
  一次"，写完取队列里那一条继续，回滚基准换成此刻显示的值；于是既不并发 PUT 同一个键（后端会
  按到达顺序落，先前那条把后一次盖掉），也不会把最后一次改动吞掉。
- 含口令的配置落盘改走唯一的原子写入口，0600 从创建那一刻生效。`.panel.env`、systemd unit、
  drop-in、PG/Redis 配置文件此前都是「先覆盖写、写完再 chmod」：目标文件不存在时先按
  `0o666 & ~umask` 创建（面板进程的 umask 不是 077，落地就是 0644 一类），再补 0600——中间
  那段时间，同一台机器上任一个被攻破的业务进程都能把明文口令读走；写一半被 kill 或磁盘满，
  留在盘上的就是半截 unit，而且当场看不出来（没 daemon-reload 之前 systemd 不会去读它），
  要等下一次 reload 才把坏文件当真。现在统一走 `util.write_secret_atomic`：同目录
  `O_CREAT|O_EXCL` 直接带 0600 → fsync → `os.replace` 整份替换，失败清掉临时文件并如实报错，
  旧内容原样留着。线上 `.venv` 实跑（临时目录，不碰任何在跑的服务）：把 umask 放到 0 再存
  环境变量，落盘 mode `0o600`、含空格的值照旧加引号、非法 KEY 被丢、二次保存返回 unchanged、
  目录里没有 `.tmp` 残留。
- 备份恢复的三条口子：同目标并发、落点穿过已有软链、正在部署的应用被盖。恢复全程没有互斥，
  双击「恢复」或两个人同时点同一份备份，两个流程会交错地停服务、清目录、解包、回写
  `.panel.env`，后一个把前一个写到一半的结果再盖一次；现在按 `kind:target` 上锁，第二个请求
  直接收到「同一目标（xxx）的恢复正在进行」。`_tar_checked` 逐条管的是归档里的条目类型，
  管不了目标目录里本来就躺着的软链——安装脚本把可变数据挪出仓库（`data -> /var/lib/app`）
  是常见写法，而解包按名字落盘，于是归档里的 `data/x.json` 写进了目录之外；面板以 root
  运行，一份手工上传的归档就能借这条链接往任意位置落文件。解包前按每一级路径 lstat 走查
  （含文件本身：现存的 `config.json` 若是软链，写它就是写链接目标），命中就整份拒绝，
  宁可拒一份可疑产物也不要「解了一半」的目录；门禁排在停服务之前，不会服务停了才发现恢复不了。
  恢复数据库/应用目录前补 `_ensure_no_deployment`，正在部署的应用不会再被恢复盖掉。
  同一批收口的是锚定校验的 `$`：`re.match(r"^…$")` 允许一个尾部换行通过，pg_service 的标识符与
  口令、`.panel.env` 的 KEY、pg_dump 库名、nginx `client_max_body_size`、Telegram bot token
  都可能被 `名字\n` 钻过去——库名里那道换行会原样混进 `sh -c 'pg_dump -Fc …'` 的命令串。
  五个模块全改 `fullmatch`，并用源码级用例钉住这些模块里不再出现 `re.match(`。并发互斥与
  部署守卫由单测覆盖（不拿生产数据做恢复演练）：断言同目标并发时峰值为 1、第二次请求报
  「恢复正在进行」、门禁调用排在 `is_active(unit)` 之前。

- 写死的像素宽度在 375px 手机档的三种死法，一次收口。登录态实测（旧产物）：JSON 预览浮层
  `.json-tip` 的 `max-width: 420px` 不跟视口缩，顶出屏幕右侧约 45px，而 tooltip 里没法横滚，
  开头几列字符直接看不着；概览进程表是 `56px 1fr 72px 64px 72px` 的五列硬 grid，固定四列 +
  4×8 gap 吃掉 296px，进程名只剩 ~20px、整列白搭；应用详情的环境变量 KEY 框写死 220px，
  同排两枚圆形按钮再占 ~64px，`flex: 1` 的值栏实测仅剩 ~30px——不溢出，但被兄弟挤死，
  手机上根本没法编辑环境变量。另有 Redis 统计格 `cols="2 s:4"` 最小档仍两列，~150px 小卡里
  「standalone / 面板密码」折得没法读。修法：浮层封顶 `min(420px, calc(100vw - 24px))`；
  进程表 ≤640px 藏「用户」列（PID 已能定位进程），把 72px+gap 还给进程名；KEY 框改
  `clamp(110px, 38%, 220px)` 随容器缩放；统计格补一列档 `cols="1 s:2 m:4"`。
  全站 52 处 `style="width: NNNpx"` 不带封顶的工具条/表单控件按 Apps.vue 既有范式统一补
  `max-width: 100%`。新产物 375px 登录态离屏复验：`.json-tip` 规则解析 351px 封顶、
  进程行实测 `56px 81px 60px 64px`（用户列 display:none、进程名 20→81px）、
  KEY/值栏 114/87px、Redis 统计格 335px 单列、备份任务选择器 230→140px，
  五个走查场景横向溢出元素计数全部为 0。
- `backend/tests/test_batch27.py`（9 条）钉住形状：凡 `style="width: NNNpx"` 的内联块必须同带
  max-width（100% 与浮层的 94vw 都算封顶）、`.json-tip` 只认 min() 视口写法、进程表 ≤640px
  降档恰好一处且桌面五列原样保留、KEY 框认 clamp 不许回像素字面量、Redis 统计每一档 span=1，
  构建产物里新旧档位（`1 s:2 m:4` 在、`2 s:4` 清零）与各守卫同源核对。

- 手机上的抽屉集体没法用：宽度写死的方式全错。六处 `<NDrawer>` 里五处用裸百分比（naive 把它
  原样写成行内样式，视口多窄它就跟着多窄：375px 登录态实测备份任务 188px、上传产物 150px、
  通知渠道 112.5px、两步验证 120px，备份任务里 230px 的「选择数据库」下拉顶穿右边缘 67px），
  剩一处是 PG 行编辑器的 `:width="560"`——比手机屏还宽 185px，整张表单直接开在屏幕外。
  恢复弹窗是另一种死法：`{ width: '40vw', minWidth: '420px', maxWidth: '94vw' }`，CSS 里
  min-width 压过 max-width，同一口径下实测 420px、顶出屏幕 45px。收口成一个出处：
  `ui.ts` 的 `useDrawerWidth(fraction)`（≤640px 一律全屏，宽屏取比例并夹在 [420, 860]），
  七处抽屉全接；弹窗的桌面下限改写进 `width: max(40vw, 420px)`，让 `max-width: 94vw` 真的封顶；
  TOTP 二维码补 `max-width: 100%`。新旧产物在同一个离屏表格里对照复验（375px）：抽屉
  188/150/112.5/120 → 全部 375，弹窗 420（minWidth 生效）→ 352.5（=94vw、minWidth:auto），
  新建用户与防火墙新增规则两个本来就合规的弹窗保持 352.5 不受牵连。
- `backend/tests/test_batch26.py`（9 条）钉住形状：抽屉的 `:width` 只准引用共享出处的变量名
  （百分比与像素字面量都算回潮，五文件锚点防止哪处悄悄不接）、每个 `<NModal` 必须带 vw 封顶
  且 style 里不许出现 min-width、`minWidth:"420px"` 单独出现在产物里是 naive-ui 自己的
  preset-dialog 样式这件事写进注释（守卫认的是 `40vw"+minWidth` 那个组合，不误伤库）；
  构建产物里旧形状（`width:"50%"` 等）确认清零。

- 全站被裁掉的全文只剩一个入口（hover），而五处「声明了要换行」的格子其实一个字都没换。
  375px 登录态同源离屏实测（2026-10-14）：告警内容最长的一条在 192px 的列里被裁掉 155px，
  承载它的 `.n-ellipsis` 节点连 `title` 都没有；同一形状分布在告警、审计、网站、用户、
  Redis 数据浏览器五处，手机上没有 hover，那半句结论就永久看不见。
  `PopoverTrigger` 是单值（不接受数组），hover 与 click 挂不到同一个 ellipsis 上，所以触屏入口另开一档：
  `ui.ts` 导出共享的 `tapEllipsis = { tooltip: { trigger: 'click' } }`，审计详情与 Redis 数据格改用它；
  整句话的格子换成新的 `.cp-prose`（三行封顶，折行本身看得见，`title` 退成桌面端补充）——自检判词、
  告警内容、证书发行方与到期时间、nginx 配置文件路径、权限说明、应用副标题、备份任务标签与生成时间、
  服务描述与套件、库表的 owner/schema、定时任务的上次执行；数据浏览的 PG 行不加浮层，
  点整行本来就打开行详情，全文和编辑都在那里。线上复验（有数据挂载的七个页面）：告警页 43 行里
  7 条判词折成两行，自检页最长的一条实测要 8 行、收到 3 行，两处都做到 `scrollWidth-clientWidth == 0`，
  而被裁的 `.n-ellipsis` 从 155px 归零。
- `.cp-break` 与 `.mono-dim`/`.cell-sub` 权重相同、靠样式块先后顺序决胜，而它写在前面：
  `white-space: nowrap` 赢——cron 的命令行与表达式、日志页的 unit 名、设置页的单元名、告警页的来源码，
  注释里都写着「这里必须换行」，实测一行都没换。告警页的来源码还多一种死法：内联
  `wordBreak: 'break-all'` 只赢 word-break 这一条属性，nowrap 仍由类提供。两条规则都改成双类名选择器
  （`.cp-break.cp-break`、`.cp-prose.cp-prose`，0-2-0 与顺序无关），`.cp-break` 把 `white-space`
  定成 `pre-wrap`（cron 表达式与命令行里的空格有意义，不能合并）；内联那份 break-all 删掉，
  告警的展开区改用全局类；批次㉔留在自检页的 `.cell-sub.wrap` 副本并进来，折行只剩一个出处。
  构建产物形状未变（`index-*.css` 仍是两条双类名）。
- `backend/tests/test_batch25.py`（18 条）把这些钉成源码级不变量：打赢 nowrap 的写法只有一个出处、
  内联 `wordBreak: 'break-all'` 不许再冒充换行声明、hover-only 的 `tooltip: true` 只允许出现在真的写了
  `:row-props=` 的那张表（豁免与整行点击通道绑在同一条断言上，通道一旦被删立刻变红）、`tapEllipsis`
  全站一份不许就地重抄、`virtual-scroll` 的表里不许出现三行封顶的格子。禁的是 `.cp-prose` 而不是折行，
  出处在库里：naive-ui 的 `Body.mjs` 传的是 `itemResizable: !virtualScrollX`，纵向虚拟列表会实测每行真实
  高度，长 unit 名偶发折一行裁不到字，但同一列混着 1 行和三行的句子会让行高成倍抖动。
- 自检项判词里「数了几项」和「点名了哪几条」原先是两套口径，三条分支漏了后半句：`load` 的 fail、
  `pending` 的 fail 和 `login` 的合并判词都写着「N 项要紧的…」，却只把第一条名字拼进 detail，
  第二条以后既没列也没说还剩几条。本机实测被吞掉的那条正是 `postgresql@17-main.service`——它排在
  第 2 位，页面上一个字没出现，只有 `counts` 里那个数字暗示有事。句式收成 `util.tallied()` 一个出处：
  报数在前、点名在后，load / pending / memory / storage / login 五个服务的 risk 与 warn 分支都改用它，
  各自手抄的 `more` 变量删掉。
- 「数字与补数站在冒号之前」是量出来的，不是审美：自检列表把 detail 放在项目名下面的副标题行，
  窄屏那一列实测 187px，本机这条 pending 的 fail 整句要 5 行。先落地的是「点第一条名字 + 句尾另有 N 项」，
  被裁掉的正好是句尾那半句，而「不止这一条」才是这条判词的结论。改成
  `2 项改动没进正在跑的进程（另有 1 项）：unattended-upgrades.service…` 之后，被裁的是名字，
  补数留在第一行。页面侧同时把这一行放开成三行折行（`.cell-sub.wrap`），行高有上限、一屏仍看得见几项，
  且只有自检这一列用它，别的副标题行还是单行。
- `tallied` 的 `total` 参数不是装饰。登录项前半句的总数来自 `_summary(findings)`（要紧的与值得处理的
  各算一档），比点名的那一列长；不传 total、直接按列长度算「另有」，会出现前半句说「3 项要紧」、
  补数说「另有 1 项」，加起来只有 2 项，读的人以为系统漏报了一项。给了 total 就用
  `max(total, len(点名列))` 兜住，补数恒等于总数减点名的那一条。
- 量词跟着被数的那一半走：存储那句数的是「处」，补数却写「另有 N 项」，读起来像后半句换了件事在数。
  `unit` 交给调用方传（存储「处」、登录「项要紧」），其余默认「项」。空标题既不计数也不点名——
  一条 `title` 为空的 finding 会让补数写成「另有 1 项」，而那条判词根本没有文字，点不出名也读不出内容。
- `backend/tests/test_batch24.py`（25 条 + 72 个子用例）把这类算错钉死。不变式用例从 detail 里正则抓出
  「N 项/处」和「另有 M 项/处」，对五个服务的 risk / warn 分支逐一断言 `N == 1 + M`、前后半句量词一致，
  并要求「另有」的落点在冒号之前（窄屏被裁的是句尾）；被点名的那条必须属于被计数的那一档，
  不能拿一条加固建议冒充「3 项要紧」的第一项。单源用例在剥掉注释与 docstring 的源码里断言
  `另有 {len(` 和 `[0]['title']` 在这六个文件里为零、`def tallied(` 与 `"（另有 {n - 1}"` 各只有一处、
  五个服务都真的从 `..util` 引进了它。反证用例把改之前的两种手抄写法喂给同一套检测，
  确认锚点抓得住——检测器空跑也算失败。

- Redis key 详情的两个读取边界原先在五个地方各写一遍：RESP 流水线的取值命令抄 `65535`/`200`，
  redis-cli 回退路线再抄一遍，页面再写一句「内容过长，仅读取前 64KB」。它们已经分叉了，而且是静默的那种：
  hash/list/set/zset 按**条目数**截断，那句 64KB 说的却是**字节**，看的人据此以为大集合也读了 64KB；
  后半句「截断状态下不开放编辑」只对 string 成立——集合类型没有"整值覆盖"这条路，条目该增删仍然可以，
  这句话让管理员以为百万元素的 key 在页面上什么都改不了。现在两个边界是 `pg_service` 的
  `REDIS_VALUE_BYTES` / `REDIS_RANGE_ITEMS`，所有命令参数（`GETRANGE 0..N-1`、`HSCAN/SSCAN COUNT`、
  `LRANGE/ZRANGE 0..N-1`）、两条路线各自的 `truncated` 判定、以及随详情下发的 `read_limits` 都由它们派生；
  页面那句按类型分别成句，数字取自响应而不是组件里的常数。
- 「一开始看多少条」在四个地方各有一份，而且彼此对不上：服务端默认 100、上限 500 藏在内联的
  `max(1, min(limit, 500))` 里；`Audit.vue` 与 `Alerts.vue` 各写一份默认 200 与档位 `[100,200,500]`，
  注释里还带着第五份（"服务端还把它 clamp 在 500"）；`api.ts` 里另有 `audit: (limit = 100)` 这个从没被触发过的
  默认值（两个页面总会显式传参，它只是在下次有人调 `api.audit()` 时突然把请求变成 100）。坏法和前几批同族：
  把 `LIST_CAP` 改小到 300，页面档位里仍然摆着 500 这一项，选完之后服务端悄悄截成 300，而回显写的是 500——
  用户看到的是一个从未存在过的读数。现在默认/上限/档位收进 `app/util.py`（`LIST_DEFAULT` / `LIST_CAP` /
  `parse_limit()` / `list_ladder()`），响应把**实际生效值**与档位一起带回（`/api/audit` 由裸数组改为信封
  `{rows, limit, ladder}`，唯一消费方就是这一页），两个页面把初值改成 `null`（不传参数，由服务端决定），
  下拉的选项与回显只认响应里那份，`api.ts` 不再自带默认行数。
- `/api/alerts/open` 的页面原先硬写 `?limit=500`（那是 500 这个数在仓库里的又一处副本）。现在不传参数，
  由服务端默认决定，而它的默认就是一档读到 `LIST_CAP`——用户看到的行数不变，抄本没了；响应形状也不动
  （仍是 `{rows, counts, total}`），不加没人消费的字段。
- 同一条 SQLite 教训原先一份写成注释、另一份连注释都没有：审计路由内联的夹取上方留着「`?limit=-1` 会把整张
  审计表吐出来（实测 601 条全返回）」，而告警历史 `_limit()` 里同一句 `max(1, min(n, HISTORY_CAP))` 什么都没写——
  下界看着像多余的防御，删掉它的人不会看到任何提醒。两份实现收进 `parse_limit()` 一处，理由写进它的 docstring，
  并由 `test_batch22b` 用 `-1` / `0` / `99999` / 非数字四种输入钉住：夹下界不是洁癖，是这条接口曾经漏过整表。
- `backend/tests/test_batch22b.py`（23 条）：数字同源与"副本不许复活"都断在剥掉注释的源码上（`code_of()`），
  否则「这份副本已经不在了」这句注释自己就会命中自己。三条反证证明参数是算出来的而不是恰好相等——
  把两个 Redis 边界打成 1024/50，命令必须跟着变成 `GETRANGE 0..1023`、`LRANGE 0..49`、`HSCAN COUNT 50`；
  边界改成 10 时长度 11 的 string 必须判成截断、长度 10 的必须判成全值；redis-cli 回退路线必须与流水线路线
  报同一份 `read_limits`，否则连不上 TCP（只开 unix socket、强制 TLS、bind 到别的地址）时结论就换了。
  `LIST_DEFAULT`/`LIST_CAP` 挪动时档位同步重算，`list_ladder()` 剔除落在上限之外的档位。
- 截断文案验到了 DOM：jsdom 整页挂 `RedisKeyPanel`，9 组 fixture、21 项断言（按字节成句 / 按条目成句 /
  数字随响应变化 / 缺 `read_limits` 时降级成不带数字的说法 / 未截断时整句不出现 / 截断值与二进制值都不给「编辑值」按钮）。
  反证同批做：组件回退到上一版重跑，7 项转红（三条是 hash/list 错说 64KB、两条是"数字跟着响应变"、两条是降级句）。
- 两处在本机没法端到端看到的，如实记下：这台机器的 Redis 里最大 string 是 38812 B、集合类 key 都不到 200 条，
  没有天然超边界的 key，而往业务实例写一个 70 KB 的测试 key 不在这批范围内，所以截断分支只有单测与 jsdom 探针为证，
  线上真实 key 只验到"正常态不再出现 64KB 那句"；审计页与告警页的表格用 `virtual-scroll`，内置浏览器面板处于关闭状态时
  （`visibilityState=hidden`）渲染帧不触发，虚拟化列表在探针里画 0 行——这是探针环境伪影不是缺陷，
  已由 `.v-vl-items` 的 `min-height`（200 行 × 40px ＝ 8000px）与页脚实际行数两处旁证确认数据到位。
- `.env` 有两个写入者，键集还不一样：install.sh 里另住着一份 `render_env`（逐行替换
  `CP_DATA_DIR` / `CP_PORT` / `CP_HOST` 三个键），common.sh 的 `env_sync_all` 同步的是六个键
  （外加 `CP_BACKUP_DIR` / `CP_APP_ROOT` / `CP_FILE_ROOTS`）。一次 `choyeonctl deploy` 先生成再对齐，
  看上去"配置写回来了"，实际是两份不同口径叠出来的——备份目录、应用根目录、文件白名单这三项只在其中
  一份里，而它们正是 `doctor` 与状态页要读的那几项。现在生成与对齐只剩 common.sh 一个出处：
  `ensure_env`（缺文件时按 `.env.example` 播种，0600）→ `sync_env` → `env_sync_all`，
  install.sh 既不再自己复制也不再自己 chmod。
- 播种整份示例必须行为中性，这条不靠人说：`test_batch22a` 把 `.env.example` 里每个未注释的键与
  `config.py` 的默认逐键比对。第一版只扫 `_int` / `_path` / `os.environ.get` 三种写法，
  `CP_TRUST_PROXY`（示例写 1、`_bool` 默认 0）正好从缝里滑过去；补上 `_bool` 之后它成了唯一豁免项，
  而豁免的前提——unit 模板把那一行写死、systemd 的 `Environment=` 压过 `.env`——本身也是一条用例。
  模板那行哪天被删掉，`.env` 里那份就真能决定"要不要采信 X-Real-IP"，测试会红而不是静默。
- 没有 `.env.example` 的机器（不完整检出）原先停在"warn 一句、什么都不写"，可紧接着 `sync_env` 就要读
  `.env`：common.sh 是 `set -euo pipefail`，读一个不存在的文件会让部署在第一次回写时当场断掉。
  现在 `ensure_env` 两条支路都以"文件存在且 0600"收尾，写不进去一律 `die` 说清是哪个目录不可写，
  不再留半截状态。
- 生产机自己就是这个洞的现场：`/root/www/choyeon-panel` 已经跑了两个多星期，`.env` 从来不存在，配置只住在 unit
  里，而 AGENTS.md §5 写着"改配置请编辑 `<安装目录>/.env`"。这批上线后跑 `choyeonctl upgrade --no-pull`，
  `.env` 按示例生成、六个部署参数与 unit 逐项相同，自检「配置来源」给出
  `.env 与 unit 的 6 项部署参数一致（监听 127.0.0.1:3210）`——此前这一项会因为文件不存在而 warn。
- `backend/tests/test_batch22a.py`（18 条）：脚本里 `render_env` 不许复活（断言做在剥掉注释的源码上，
  否则"这句话已经不在了"的注释自己会命中）、`ensure_env` 必须排在第一次 `sync_env` 之前、install.sh
  不许再 chmod、播种中性逐键比对、豁免键必须真被模板写死，以及「配置来源」的九种判词（无 unit、无 `.env`、
  一致、端口漂移、unit 缺键不比、`.env` 缺键、引号、无关键漂移、已注册且不在 `FIXABLE`）。
  文档项数一处对齐：AGENTS.md 写的项数必须等于 `len(_CHECKS)`。
- `test_deploy_contract.py` 里"没有 `.env` 时 `env_sync_all` 必须什么都不做"那条旧用例锁的正是这个 bug
  ——契约写错了，不是实现走偏。换成两条：缺 `.env` 要播种并把六个部署参数写成生效值（顺带钉 0600 与
  `set -e` 下的退出码），有示例时整份复制但 `CP_PORT` 仍按本次覆盖。
- `test_metrics_store.py` 的采样循环用例从"睡 60ms 再数轮次"改成等条件（上限 2s）：`loop()` 起来先要
  `await` 一次真实的后补采样（`asyncio.to_thread(_backfill_sync)`），这台 2 核机器上同机有构建在跑时它
  自己就吃掉几十毫秒，于是一次成功运行的第二轮落在 60ms 之外——断言量的是机器不是代码，与上一批
  `test_review_fixes` 那条同族，这次连根一起去掉。
- 「一开始看多少行」接着上一批再收四处：日志中心的档位还写死 `[200,500,1000,2000]`、默认值另写一份 500，
  nginx 页签那句说明写死「末尾 512KB」，实时日志组件抄了 200 与 2000，`api.ts` 里还有第三份 200，服务端
  SSE 的兜底写着第二个 `min(lines, 2000)`。坏法与上一批同族，只是这回是静默的那种：把 `HTTP_LOG_LINES_MAX`
  改小，前端输入框照样允许填到旧上限，请求发出去后被服务端悄悄截断，画面看着像"日志少了"而不是"你填超了"。
  现在档位由 `app/util.py` 的 `log_ladder()` 从 `LOG_LINES_DEFAULT` 与 `HTTP_LOG_LINES_MAX` 派生，经
  `/api/system/log-options`（仅管理员）下发；日志中心用它填两个下拉与那句 KB 说明，实时日志组件把初值留空
  （`null` = 不传 `lines`，按服务端自己的默认来）、上限交给输入框的 `:max`，`logUrl()` 不再自带默认行数，
  SSE 兜底改引用同一个常量。清单拿不到的 viewer 只是看不见上限，不报错也不崩。
- 下拉在没有可选项时会画一个孤零零的 `0`。naive-ui 对"`:value` 有值但选项里没有这一项"是直接把值当文本画出来，
  不会回落到 placeholder，于是行数清单还没到、候选文件还没取时框里是一个数字而不是提示语。改 `|| null` 之后
  文件那个框要另判：序号 `0` 本身就是合法的"第一个候选"，只能按"有没有候选"判，不能按真假判。
- 应用详情页环境变量编辑态的两颗图标按钮此前只有图标没有名字，读屏用户听到的是两个"按钮"。改由
  `NDynamicInput` 的 `#action` 插槽显式挂出删除/新增，各带 `aria-label`；实测 3 行配置就是 3×2 颗带名字的按钮。
- 上一批的提交（`d6ae079`）把 `### 修复` 里 ⑲B 与 ⑲A 两段整块覆盖掉了：写 CHANGELOG 时按行号替换，没比对被
  替换区间里是否还住着别人的条目。已按行级三方合并找回，两批条目重新在文件里，措辞未改。
- `backend/tests/test_batch21.py`（20 条）锁两件事：数字同源，与副本不许复活。源码断言在剥掉注释的文本上做
  （`code_of()`），否则"这份副本已经不在了"这句叙述本身就会被自己的注释命中。含反证：把 `LOG_LINES_DEFAULT`
  与 `HTTP_LOG_LINES_MAX` 打成 300/1200，档位必须跟着变成 `[300,600,1200]`；默认值落在上限之外时要被剔除而不是
  留在档位里。端点侧钉 `nginx_tail_bytes == system.TAIL_BYTES`、`log_ladder` 被委托、viewer 403、匿名 401。
- 两处在本机没法端到端看到的，如实记下：nginx 尾读那句"最多读末尾 512KB"只在有候选文件时才出现，而这台机器的
  `CP_FILE_ROOTS` 不含 `/var/log/nginx`，接口回的是白名单提示，所以那句说明目前只有接口值与单测为证；`NSelect`
  的下拉菜单在这台嵌入浏览器里（`visibilityState=hidden`）从不挂载 DOM，能证的是已选项 label（"200 行"）确实
  来自接口，而不是整张下拉清单。
- 资源告警原先不论越过多远都记 `warn`，注释写的是「crit 留给连不上 / 单元 failed /
  证书过期」。同一件事磁盘早就按两档走了，于是「CPU 82%」和「CPU 98%」在告警列表里
  长得一模一样：按 level 过滤的 Telegram / webhook 渠道要么两条都推、要么把要紧的那条
  一起滤掉；概览仪表按同一对数（配置线再加一档）早就变红，页面说"要出事"、列表说
  "值得处理"。现在 CPU / 内存读 `resource_thresholds()` 里的要紧档、负载读自检用的
  `load_service.LOAD_RISK_PER_CORE`（每核排队 2 个），服务层不再自己写第二个数字。
  默认配置下 `alert_load1_x` 就是 2，等于要紧档本身，所以负载一超标即 crit——这不是漏了
  中间档，而是"每核排俩"本来就是这台机器给出的要紧判词；要更早提醒该动的是那条配置。
- 「一次尾读多少行」在四个地方各写各的数：`cli._tail()` 把 `--lines` 夹到 5000，
  `cron_service` 的白名单正则只放行 1..1000（注释还写着"与 CLI 的 _tail() 同口径"），
  HTTP 侧另有一档 2000，定时任务页再把 1000 / 200 /「4KB」抄成第三份。坏法是同一个旗标
  两个答案：`choyeonctl logs --lines 2000` 在终端里正常，登记成定时任务被 400 拒掉，
  而拒它的理由那句根本不成立；页面上填不进终端里能用的值。现在三个数字住在 `app/util.py`
  （`CLI_LOG_LINES_MAX` / `HTTP_LOG_LINES_MAX` / `LOG_LINES_DEFAULT`），CLI、白名单校验、
  HTTP 解析共用，`/api/cron/options` 连同输出留存量（`output_keep_bytes`）一起下发，
  页面读接口不再抄常量。
- 全量跑 `test_review_fixes` 的 scrypt「循环是否空闲」断言又红过一次（实测最大间隔
  0.062 对 0.06 的阈值，单独跑永远绿）。那条断言量的是机器不是代码：同机负载能把一次 5ms
  的 sleep 顶到 60ms，跟哈希在不在事件循环上毫无关系。改成看中位数——哈希真压在循环上算，
  ticker 每 150ms 才轮得到一次，负载怎么抖都翻不回来。反证是同机上把哈希放回循环：中位数
  1.22 秒、整段只跑到 1 个 tick，两条断言（中位数与 tick 数量）都拒绝。
- `backend/tests/test_batch19b.py`（13 条）锁定这批：白名单放行 CLI 接受的每一个值
  （含 `--lines N` 与 `--lines=N` 两种写法）、`0` 与超界与非数字一律 400、`_LINES_RE`
  不许复活、HTTP 档严格小于 CLI 档、`/api/cron/options` 与三个常量同源、viewer 读不到
  白名单，以及 `CronTasks.vue` 里那份常量副本和那句 KB 不许回来。档位判定另由
  `test_alerts`（85% 是 warn、95% 是 crit、负载跟着自检那个数走）与 `test_hardening`
  （两档互不引用）钉住。

- 由外部 unit 纳管的应用现在真的能改环境变量。此前详情页里「环境变量」照样能编辑，PATCH 照样回 200、
  界面照样弹「已保存」，附一句「外部 unit：unit 与环境变量文件不由面板写入，本次只改了记录」——然后
  什么都没写。生产机上的 choyeon-video 就是这句话的现场：记录里 ADMIN_PASSWORD 早已换过新值，
  `/root/www/choyeon-video/.panel.env` 这个文件从来不存在，外部 unit 里也没有 EnvironmentFile 那行，
  进程读到的还是老配置，「改了没反应」于是变成玄学。unit 归外部管是真的，`.panel.env` 在应用目录里、
  面板写它是自己分内的事，两件事被一句早退捆在一起放弃了。现在写文件、并在 systemd 的
  `<unit>.d/panel-env.conf` 补一份 drop-in 引用它（不改别人的 unit 本体，删掉这个文件就恢复原样；
  drop-in 里的 EnvironmentFile 排在 unit 本体 `Environment=` 之后，面板的值优先），服务在跑且内容
  确实变了才重启一次，内容没变就一句「环境变量没有变化，服务未重启」，不再每次点保存抖一遍线上进程。
  确实活在 unit 本体的那三样（启动命令、端口、安装目录）照旧如实说明「要在 unit 里同步后重启才生效」。
- 同一个洞还有第二个入口：自定义 `unit_template` 漏写 EnvironmentFile 时，面板报「已生效」而 systemd
  读的是另一份配置。现在部署和编辑都会先问一句「这个 unit 到底读不读 `.panel.env`」——查 unit 本体和
  drop-in 两处，按整条路径比对（换过安装目录后，指向旧目录的引用不算生效），不读就用 drop-in 补上并在
  note 里写清楚补了什么。删除应用连带清掉面板自己那份 drop-in（目录空了才删，别人放的 `vendor.conf`
  不动）；托管与纳管互切时残留的 unit / drop-in 都有对应提示，不再只改记录就宣称生效。
- 新增回归测试：`backend/tests/test_batch20.py`（24 条）。含 unit 本体与 drop-in 两种引用都被认出、
  注释行与别的 app 的同名文件不算数、非法 unit 名（`../../etc/evil.service`）拒绝挂载、只有内容真变了
  才 daemon-reload、停止态不擅自启动、外部 unit 下改 start_cmd 仍要说「只改了记录」、应用目录还没建时
  提示下次部署写入、`.panel.env` 文件名只有一个出处（unit 模板、写入路径、drop-in 内容三处必须指同一个
  文件）。反证过不是空转：把 `apps_service.py` 退回上一版，24 条全红。线上实测：在 choyeon-video 的
  环境变量页加一个键保存，`.panel.env` 与 drop-in 落地、`systemctl show -p EnvironmentFiles` 读到该文件、
  跑着的 node 进程 `/proc/<pid>/environ` 里有这个键（外部 unit 自带的 `Environment=PORT` 一个没丢）、
  站点仍 200；删掉该键再保存，进程里跟着消失；内容不变再点一次保存，PID 不变并报「服务未重启」。
- 「还剩几天算快到期」原先有两个出处：`cert_ops.EXPIRING_DAYS = 21`（证书页标"即将到期"用它）与告警配置
  `alert_ssl_days`（默认 14，巡检决定发不发通知用它）。两份数字没有任何机制一起改，坏法是镜像的两侧都觉得自己对：
  把配置调到 30，第 30 天通知已经响了、证书页上还是"正常"，人以为面板没当回事；调到 7，证书页满屏"即将到期"
  却一条通知都不发，红牌变成装饰；调成 0（关掉证书提醒），页面照样按 21 天标黄，看起来像提醒还在生效。
  现在 `status_of(days_left, threshold, invalid)` 只认入参，线由调用方给：`cert_ops` 里那份常量已经删掉，
  certbot 与 openssl 两条来源都从 `setting_int("alert_ssl_days")` 拿线，`list_certs()` 随载荷带出
  `expiring_days`，站点页把这条线直接写给人看（「剩余 ≤ N 天算即将到期」），前端不再自己抄数字；
  `/api/ssl/certs` 也删掉了路由自己重拼的那份 `"expiring_days"`（⑰B 的老毛病：服务层算好了、路由再拼一遍，
  两份数字在响应里打架）。配置为 0 时页面只判有没有过期，巡检整段跳过、连 certbot 都不查。
- 第二个缺陷更隐蔽：天数读不到被说成"没事"，而且是被推送说成没事。certbot 的输出里少数证书读不到剩余天数
  （旧版格式、异常行），`parse_certbot` 老实地给 `days=None`，而巡检的分支只有 `invalid / days<=线 / else`，
  None 一路落进 else，于是往历史里写一行「证书 X 有效期正常」；上一轮这张证书真报过警的话，还会补推一条
  「已恢复：证书 X 有效期正常」——同一张证书在站点页显示的是"未知"。读不到就只记一条 warn 日志、不动档位、
  不发恢复，同时把 `notified` 标记留着（不留的话这张证书下次真恢复时也不发了）；天数能读出来且回到线上时
  照旧落 ok 并补「已恢复」，恢复通知没被一起掐死。
- 新增 `backend/tests/test_batch19a.py`（24 条）：档位只随入参走（21 天配 25 是 expiring、配 20 是 ok、
  负数优先 expired、None 是 unknown 而不是 ok、0 只关 expiring 不关 expired）、四条源码级守卫
  （`cert_ops` 里不许再出现 `EXPIRING_DAYS`；每个 `status_of(` 调用必须带线；sites 路由不许重拼
  `expiring_days` 且必须由 `**data` 带出；巡检段不许有 `low =` 这类第二跳板）、None 不落 ok 历史也不发恢复且留标记、
  后续可读轮仍补恢复并清标记、阈值同时进正文与 meta、线调到 7 就不发、invalid 恒 crit、一张读不到不带崩整轮
  （防止 continue 写成 return）、0 时不查 certbot，以及"同一条配置同时决定页面档位与巡检通知"的三档对照
  （25→页面 expiring + 1 条通知；7→ok + 0 条；0→ok + 0 条）。`test_sites.py` 里原来那条
  `test_expiring_boundary_is_21_days` 换成四条边界用例。反证：把四只产品文件（`cert_ops` / `alerts_service` /
  sites 路由 / `Sites.vue`）退回上一版，本批 24 条里 17 条红；仍绿的 7 条是
  `test_the_negative_days_still_wins_over_the_line`、`test_a_cert_above_the_line_is_not_alerted`、
  `test_breach_uses_the_configured_line_in_the_message`、`test_invalid_cert_is_crit_regardless_of_days`、
  `test_the_round_still_finishes_when_a_cert_is_unreadable`、`test_zero_skips_the_certificate_check`、
  `test_sites_page_has_no_hardcoded_day_count`——它们守的是旧代码本来就对的行为（负数、invalid、整轮不中断、
  前端没有硬编码天数），不是这批的收敛。
- 线上实测（生产机 3210，跑的是这批代码）：`alert_ssl_days` 原值 20，certbot 三张证书剩 76/77 天，
  `/api/ssl/certs` 带出 `expiring_days: 20`、页面文案「剩余 ≤ 20 天算即将到期」、徽章全"正常"；把它存成 45、
  再存成 60，同一个接口的线跟着变、页面数字跟着变，复位 20 后文案回到「≤ 20 天」；配成 0 时页面切到
  「该提醒已关闭（配 0），这里只判有没有过期」；999 判 400，报错句子里带的区间就是登记表那条，库里也没落这个值。
  浏览器端把 `/api/ssl/certs` 的载荷改写（线改成 80、其中一张的 `status` 改成 expiring）后重挂载站点页：
  说明行跟着写「≤ 80 天」、那张徽章变成"即将到期"，证明徽章取的是后端 `status` 而不是前端拿 `days_left` 再算一遍。
  新说明行在 375px 宽下换行不撑破容器（`scrollWidth == 375`），console 无报错。
- 概览的「磁盘」原先写死读根分区，而「存储占用」页签读的是数据目录所在的那块盘。数据目录放到独立
  挂载盘上之后，两处会同时给出相反的判断（数据盘 100%、根盘 20%：一处是要紧项，一处是"占用健康"），
  而面板真正会写满的是数据盘。现在两条采样路径都走同一个 `_disk_sample()`，按数据目录取盘，并把
  **实际读到的那条路径**一路带到接口与页面：仪表盘的仪表标题不再是写死的「磁盘 /」，存储页那句
  「没有异常占用」也会说清是哪块盘没有异常。数据目录还没建（新装、被移走）时退回根分区，退回后的
  路径同样如实标出来——换了盘不能还谎称是同一块。这一列的历史曲线从这批起跟着数据盘走。
- 分区警戒线原先前端自己抄了一个 90：把阈值调成 85，同一条「分区已用 87%」在自检里是要紧项、
  在页签上却是绿灯。阈值现在跟着 `/api/storage` 的返回一起给出（`tight_pct`、`crit_pct`、
  `inode_warn_pct`、`inode_risk_pct`），前端只用载荷判紧不紧；读不到时按 0 处理，宁可不判也不猜。
  挂载表的 inode 那一列不再借用容量的线——inode 90% 与磁盘 90% 说的是两件不同的事。
- 「改动未生效」的内核判定原先只认 dpkg 状态 `ii`，`iU`（已解包、未配置）与 `iF`（配置失败）被静默
  丢掉：apt 停在 configure 阶段、新内核文件已经在盘上时，面板报的是「不需要重启」。这两种状态现在
  收进 `partial` 并如实判成「判不出」，要人先修 dpkg 再回来看这一项；`rc`（已卸载只剩配置文件）
  照旧忽略——盘上没有可启动的内核，把它算进来每台机器都成了待重启。
- 应用详情页的内存占用带的是第二套字节格式化器（GB 留两位、MB 取整、没有 TB 一档），和全站唯一出处
  `useFormat.fmtBytes` 在同一屏里并排出现两种精度。删掉，改用同一个函数。
- 新增回归测试：`backend/tests/test_batch17b.py`（20 条）。内核那六条按生产机实拍的 `dpkg-query`
  输出造样例，`iU`/`iF`/`rc`/`ii` 四种状态分别断言，并钉住「半装的包不许同时出现在已装列表里」
  （否则前端拿它当生效版本比新旧）与「发行版挂了重启标记时优先于半装态」。磁盘那几条断言两条采样
  路径都走同一个出处、快照带出实际路径、仪表盘不再写死 `/`。阈值那三条打在 `audit()` 的返回上，
  夹具故意不给走盘结果塞 `tight_pct`——上一版就是把键加在 `_collect()` 里，用例全绿而线上前端读到
  null，出口错位只有打在真出口上才抓得住；另两条断言缓存命中仍带阈值、刚到警戒线那一档只出声不算要紧项。
  剩下的源码级守卫：前端读载荷而不是自己抄数字、路由不许重拼字段、应用详情不许再留
  第二套格式化器。
  反证过不是空转：把六只产品文件退回上一版，20 条里 16 条红。
- 「这块盘要满了」在同一条设置项上写着四个互相矛盾的答案：告警巡检读设置页的 `alert_disk_pct`
  （默认 85、可配 50-99）、`choyeonctl doctor` 的磁盘项写死 90/75 两个字面量、存储对账另立一个
  `DISK_TIGHT_PCT = 90`、前端仪表又抄了一份 80/90 且条形的热档再来一个 90。生产机上这条配置是 80，
  也就是说这台机器的盘到 87% 时告警会响、概览仪表仍是品牌蓝、`doctor` 报 pass、存储页签写
  「没有异常占用」。现在四条出口都引用 `alerts_service.disk_thresholds()`：警戒线就是那条生效配置，
  要紧线比它高 10 个点（封顶 99）。分两档是因为「该发通知了」和「页面上要红」本来不该是同一个数——
  合成一个的话，把线设到 60% 的机器会天天红。自检按这两档给出 pass/warn/fail 并在文案里带上线值，
  存储判词按两档分 severity，巡检越过要紧线时把 level 升成 crit（`record()` 的同级同文去重会因此
  新落一行历史，告警列表看得见「从该处理变成要出事」），`/api/system/stats` 随采样带出
  `diskWarnPct` / `diskCritPct`，概览磁盘仪表的配色与存储页的 chip、条形热档、挂载点筛选全部读载荷；
  载荷里的线是 0 或缺字段时一律不染色，宁可中性也不要一个平白的红仪表。+10 这一步只在
  `alerts_service` 里算一次（⑱B 起这个常量叫 `CRIT_STEP`，磁盘与 CPU/内存共用），别处再自己算
  一遍或自己读那条配置都会被用例拦住。
- 新增 `backend/tests/test_batch18a.py`（27 条）：配置线驱动 `disk_thresholds()`（默认 85/95、配 70→70/80、
  配 99 封顶、脏值回落、越界钳制）、存储判词两档与 inode 仍用它自己的线、`/api/storage` 载荷四键、
  `doctor` 磁盘项三档且文案带线、`sys_stats` 附两键且不破坏采样结果、巡检 warn→crit 升档必落一行历史而
  同档两轮不重复落。反证：把四只产品文件退回上一版，本批连同 ⑰B、⑫B 的 107 条里 27 条红。
  ⑰B 那两条阈值守卫随之反转——「`storage_service` 里 `DISK_TIGHT_PCT` 引用不少于 3 处」钉的本来就是一个
  第二出处，现在改为断言那个名字必须消失、判定与载荷只能引用同一个 `dt`。
- 上一条把「线是多少」收成一个出处之后，「这条配置默认是多少、能填多少」还是四份账：巡检里
  `_int_setting(key, 默认, 下限, 上限)` 的实参、`/api/settings/alerts` 保存校验用的 `extras.ALERT_INT_RANGE`、
  设置页 `ALERT_DEFAULTS` 的前端副本、模板里逐个写死的 `:min="50" :max="99"`。四份会各自漂移，
  而线上已经在漂移：`alert_mem_pct` 生产机根本没存过（读空），设置页靠前端副本把它显示成 90，
  接口回来的却是空串；哪天后端把默认改成 85，页面还画 90，保存一次才被发现。现在八条告警配置的
  `(默认, 下限, 上限)` 只写在 `alerts_service.ALERT_LIMITS` 一张表里，巡检读它（新增 `setting_int(key)`，
  按下限上限钳制、脏值回落默认；底层 `_int_setting` 只剩它和内部 `streak:` 计数在用），`extras` 的区间表
  由这张表派生（不再是第二处字面量），GET `/api/settings/alerts` 额外带出 `limits`（逐项默认/下限/上限）与
  `crit_step`，设置页拿到什么就显示什么：空值键用 `limits` 的默认补齐、`NInputNumber` 的 `:min/:max`
  直接取登记表、磁盘与 CPU/内存三行下面多出一句「警戒线就是这条配置（当前 N%），要紧档在它上面 10 个点」
  ——这句话以前只能靠人自己把两个数相加。
- CPU 与内存的告警线此前也只到「通知」为止：概览仪表的 CPU 环写死 70/85、内存环写死 80/90，
  于是把 `alert_cpu_pct` 调到 60 以后，同一台机器上巡检已经在 60% 发通知，页面上那只环却要等到 70%
  才黄、85% 才红。现在 `/api/system/stats` 随采样带出 `cpuWarnPct` / `cpuCritPct` / `memWarnPct` /
  `memCritPct`（同样出自 `alerts_service.resource_thresholds()`，与磁盘那两个键同一批口径），两只环的
  配色读载荷；载荷缺字段或线为 0 时不染红。负载环刻意不并入配置：`alert_load1_x` 决定的是「什么时候
  该给人发一条消息」，而环上那格要表达的是「机器此刻排没排满」，后者的分母是核数、跟配置无关——
  这条理由写在 Dashboard 的注释里，用例同时钉住「`resource_thresholds()` 的返回里不许出现负载」。
- 新增 `backend/tests/test_batch18b.py`（20 条）：登记表就是唯一出处（默认/钳制/脏值回落、
  `alert_limits()` 形状、`extras.ALERT_INT_RANGE` 必须由它派生且源码里不许再留区间字面量）、
  两档算术只在 `alerts_service`（`CRIT_STEP` 出现四次：定义加磁盘/CPU/内存三处引用，其它文件出现即红；
  99 封顶；extras 只许引用 `alerts_service.CRIT_STEP`、不许自己写 `crit_step = 10`）、`/api/system/stats`
  四键且不改动采样值、GET `/api/settings/alerts` 带出八键 limits 且越界值（`alert_load1_x=999`）判 400 不落库、
  巡检推送文案带「阈值 60%、要紧档 70%」两个数而历史行仍只写警戒线、meta 里存 crit、改配置后下一轮文案
  跟着变。另有两条源码级守卫盯着前端：设置页不许再留 `ALERT_DEFAULTS` 与 `:min="50"` 这类字面量、
  概览不许再留 `band(cpuVal.value, 70, 85)`。⑱A 那条「`CRIT_STEP` 不许出现在别处」的名单随之去掉
  `extras.py`——从这批起转交合法、自己算不合法，改由本批的正向断言守住。反证：把五只产品文件退回
  上一版，`test_batch18b.py` 20 条里 20 条红；还绿的那一条是「越界值判 400」，旧的区间表本来就拦得住它，
  它守的是别把校验改松，不是这批的收敛。
- 线上实测（生产机 3210，跑的是这批代码）：默认态 `/api/system/stats` 给 disk 80/90、cpu 80/90、
  mem 90/99；把 `alert_cpu_pct` 存成 66，同一个接口的两条线立刻跟着变 66/76，清空后回落 80/90；
  `alert_load1_x=999` 判 400，报错句子里带的「1-32」就是登记表那一条，库里也没落下这个值。
  浏览器端把 `/api/system/stats` 的载荷改写后按仪表顺序取环色：CPU 75% 配要紧档 70 时环是红、
  内存 95% 配警戒 90 时是黄、CPU 65%（两线之间）也是黄，把载荷改回 30%/10% 两只环回品牌色；
  设置页那三句说明在 375px 宽下换行不撑破容器（`scrollWidth == 375`），console 无报错。
- 补丁自检的假绿：`apt list --upgradable` 因为 dpkg 锁、超时（退出码 124）或命令不存在（127）而
  失败时，旧代码只往 notes 里塞一句话，然后照旧 `security_known = True`——清单是空的，
  `pending_total=0` 于是顺流走到自检项，输出「没有待装安全更新」。这台机器最怕的就是没打的安全更新，
  而探测失败把它粉饰成干净。现在 `_probe_error` 只认一件事：非零退出码 **且** 一行都没解析出来
  （dnf/zypper 的 100 是约定不是失败；非零但解析出了行，数量就是真的）。命中即 `pending_total=None`
  并带出 `probe_error`，自检项只能是 warn、fix 给刷新索引那条（每族新增 `refresh_index`，未知族回空串），
  CLI 与页签把 None 渲染成「读不到」而不是「0 个」，空表也写明「这一轮读不到，空表不代表没有更新」。
- 同源的第二条假绿是过旧的本地索引：`apt list` 读的是 /var/lib/apt/lists，索引是 30 天前的，那句
  「没有待装安全更新」说的就是 30 天前那个世界。现在探测成功也带出 `index_age_hours`，超过
  `STALE_TRUST_HOURS`（168 小时）时自检项从 pass 降为 warn 并点名年龄，给不出 warn 的 pass 文案里
  必须带上「索引 N 小时前刷新」；阈值只写在服务层，CLI 引用同一个常量、页签只认后端给的 `index_stale`。
- 「要不要重启」原先两个页签互相打脸：补丁页签读 `/run/reboot-required`，而那个标记由
  update-notifier-common 在 apt 事件里写、服务器镜像默认不装它，于是恒报「不用重启」，同一屏的
  `pending` 自检项却说「装了新内核、进程还踩着旧映射」。现在 Debian 走 `pending_service.kernel_reboot_verdict()`
  （新导出的公开入口，只跑一次 dpkg-query，不扫 /proc），RHEL 保留它自己的标记，其它族不判；
  判不出时页面与 CLI 写「判不出：原因」，不再假称不用重启。
- CPU 总账把 guest 算了两遍：内核已把 guest 时间计在 user/nice 里，`/proc/stat` 整行求和以及
  psutil `cpu_times` 全字段求和都会把虚拟化租户的那份再加一次，分母偏大 → 占用率偏低。
  `sys_helper.parse_stat` 与 `system_metrics` 统一改成只取前八项，与 `load_service.CPU_FIELDS` 同口径。
  生产机 guest=0，所以这条在本机是潜伏缺陷，不是当前故障。
- ctxt/forks 的每秒速率被抬高两三倍：增量是 stat1→stat2 之间攒的，中间夹着配额扫描和整轮 /proc
  扫描，而分母写死标称 1 秒、页面同时写着「窗口 1 秒」。现在 `window_sec` 是单调时钟实测跨度，
  负载区标「采样 N 秒」；进程表自己的窗口仍读 `procs.window_sec`（「这一轮扫了」与「每个数字都是
  这 N 秒里的增量」那两句说的是进程表，不是总窗口）。
- 端口暴露面有一条红灯会被白名单盖掉：判定顺序原先是「链路本地端口」在「面板应用配了域名反代却
  绑全网」之前，于是把一个带域名的应用登记在 5353/5355 上时，报的是「mDNS 报文出不了这一条网段」，
  而真实情况是任何人都能绕开 TLS 和限速直连源站。现在应用白名单先判。
- `backend/tests/test_batch17a.py`（46 条 + 9 组子测试）逐条钉住以上行为，含三条源码级守卫：补丁页签
  不许自己再算一遍重启判定、psutil 路径必须经过 `_cpu_total`、168 这个阈值只许出现在服务层。
  顺带更新两处旧断言：`commands_for(UNKNOWN)` 现在带 `refresh_index`；`test_batch16a` 的速率夹具换成
  可控时钟——分母改成真实跨度之后，速率不该再跟跑测试那台机器的快慢有关。
- `test_batch15a` 原先把「改动未生效」断言成自检清单的最后一项，第 19 项一加上去它就红：那条真正要锁的是
  「它排在内存压力之后」。清单是往尾部追加的，写成「必须是最后一项」等于给后面每一批都埋一个假报警，
  报的还是「上一批坏了」。改成按前一项锚定，⑯A 的新用例照同一写法（`load` 紧跟 `pending`）。
- 服务器页「自启」列原先把 systemctl 的英文状态直接画进 96px 的胶囊，375px 上实测截成
  "enabled-ru…"，而它是那一页唯一还在说英文的一列（左边的「状态」列早就是运行中/已停止）。
  现在按档位给短标签（已启用 / 临时启用 / 按需触发 / 生成式…），原始值留在悬浮提示里供排障对照；
  enabled-runtime 单列一档标黄：重启之后它就没了，最容易被人当成"已经设好开机自启"。
- 表格装不满一页时，表底下会孤零零挂出一个「1」——那是 naive 的分页条，只有一页可翻。
  判词表在「没有异常」时（0 行）也会挂出它，看着像坏了。`Services.vue` 现在走一个
  `pager(count, cfg)`：不足一页返回 `false`，超过才返回那个常量对象。返回的必须是同一个
  对象，模板里现造新对象会让 naive 每次重渲染都把用户翻到的页码重置回第一页。
  实测覆盖六个页签：130 行的服务表照旧分页，补丁/端口/登录/存储/内存的判词与大文件表不再挂噪声。
- 端口暴露面在 375px 上把绑定地址与 PID 截断（0.0.0.0:2… / pid 6507、601…），而这两样
  恰好是"这个端口绑在哪儿、是谁在听"的判据。改成折行，窄屏不再丢信息（与判词表同一套 ls-wrap）。
- `<style scoped>` 里有一类静默失效：作用域属性 `data-v-*` 只打在模板写出的元素上，表格列用
  `h()` 现造的节点没有它，于是页面里那些只作用于单元格的样式一条都不生效。实测到的后果是
  日志检索的命中高亮没有底色、日志正文丢了 `pre-wrap`（缩进与换行被折成一行）、计划任务
  "最近一次"列的图标和时间不能收缩。现在凡走 render 函数进格子的类都从模板上的表级类走
  `:deep(...)`（服务器页四个页签的单元格样式一并收进来），并删掉 Databases.vue 里一份没人用的
  `.mono-pre`。顺带把登录安全表在 375px 上从 978px 收到 760px：判词折行、指纹放开截断，
  不再逼手机横滚一张被 nowrap 顶宽的表。
- 单体仓库（monorepo）里的子目录组件现在能通过面板部署，也能回滚。原先三处按
  `{path}/.git` 判"是不是仓库"的代码统一改为共用 `resolve_repo_dir`（`git rev-parse --show-toplevel`）：
  没有自己的 `.git` 的组件目录（`<仓库>/frontend`）会被判成"没有仓库"，于是部署转去
  `git clone` 一个已存在且非空的目录（git 直接拒绝，该组件永远无法上线）、回滚在入口守卫处
  就报"应用目录不是 Git 仓库"、部署记录永远不写 commit（回滚查不到候选），安装失败时
  "盘上已是新代码、服务还在跑旧代码"的提示也永不触发。一键同步恰好内联过一份 rev-parse
  兜底，一直是好的——它同时掩盖了前面几条。git 操作落在工作树根，装依赖/构建/启动仍落在
  应用自己的目录（unit 的 `WorkingDirectory` 就是它）。
- 面板写完 unit 现在会 `systemctl enable` 它。此前只 start 不 enable，而 systemd 不会自动把
  新写的 unit 纳入开机目标，"部署成功"于是只保证这一次：机器重启后应用不会自己起来。
  enable 失败（自定义 `unit_template` 里没有 [Install] 段）作为提醒写进部署日志与 PATCH 的
  note，不把一次已经上线的部署判成失败；note 同时改成追加而不是覆盖，"没设上自启"不会再被
  后写的"服务没能重启成功"顶掉。
- 重启后的端口复核窗口从 5 秒放宽到 20 秒，并把"没探到任何监听端口"与"监听在别的端口"
  分成两句话。实测 `uvicorn --workers 2` 的应用 5 秒内还没 bind（FastAPI 的 import、连接池与
  路由注册都在启动期），每次部署都固定附一句"域名反代可能 502"的假警报——真出错时反而没人信。
- 新增回归测试：`backend/tests/test_batch7a.py`（真 git 夹具下的组件部署/回滚/同步、
  仓库根与组件目录同时更新、构建跑在组件目录、失败时的版本漂移提示）、
  `backend/tests/test_batch7b.py`（unit 写入后 enable、纳管模式不发任何 systemctl 命令、
  enable 失败进部署日志与 note、慢 bind 不再报端口警报）。
- 「同一个应用上的写操作不能重叠」这条规矩，此前只在单个进程里成立。部署、同步、回滚、删除
  各握一把 `asyncio.Lock`，而真正会改线上现场的入口有三个：界面的 HTTP 请求、`choyeonctl`
  （它 `import` 服务层自己起一个进程）、systemd timer / oneshot 拉起的子进程。三个进程各自
  一把锁，等于这条规矩在跨进程时从来不存在——同一秒里两个 `choyeonctl app deploy` 与一次
  页面点击，会各自 stop 同一个 unit、往同一个目录 `git reset` 两遍、再各自 start，后写完的
  那份部署记录把前一份的现场覆盖掉，而两条记录都写着成功。新增 `backend/app/locks.py`：
  按目标在 `DATA_DIR/locks/<key>.lock` 上 `flock`，非阻塞抢占，锁文件里写持有者留痕
  （动作、操作者、PID、开始时间），抢不到时报的错误直接点名"谁在跑、跑的是哪件事"。
  六个入口（部署、同步、回滚、删除、恢复、备份任务）共用同一套键，都按解析后的 app id 分，
  所以"这个应用正在被谁动"全局只有一个答案。`DATA_DIR` 现取不做模块常量，CLI 与测试换目录时
  立刻生效。选 `flock` 而不是 `deployments.status='running'` 做判据：后者在面板被 OOM 杀掉后
  会留在原地，把该应用之后的所有动作都挡到超时，而前者由内核随进程回收。
- 恢复的两条备份路径此前对同一个应用拿到两把锁。锁的键直接取自备份行上写的字符串，而目录类
  备份的 target 存应用名、SQLite 类存 app_id，同一个应用因此是 `app:vid` 与 `sqlite:1` 两个
  键：两次恢复能同时 stop 同一个服务、同时覆盖同一个目录和同一个库，先返回的那句「恢复完成」
  被后一次整份换掉。现在两条路径都先把 target 解析成 app id 再挑键，与部署用的是同一把锁。
  解析用不会抛异常的 `_restore_target_app`：它只是给锁挑键、给守卫看一眼有没有部署，不是恢复的
  前置条件。原先用会抛的那个挑键，应用记录已被删除时锁还没拿到就报"应用目录不存在"，
  报错的人以为自己在看恢复正文，其实连锁都没上，而"按名字回退"那条分支永远走不到。
  真正的目标校验仍由干活的 `_restore_app`/`_sqlite_app` 说（「备份目标应用已不存在」）。
- 恢复被杀不再等于现场什么都不留下。恢复的动作序列是"停服务 → 覆盖内容 → 起服务"，面板在
  中间被 kill/OOM/升级重启打断时，留下的就是一个被面板停掉的服务加半新半旧的目录，而界面上
  没有任何一处说它为什么停着。每次恢复现在先在 settings 里落一条"正在覆盖现场"的标记（记下
  目标，以及这次恢复是否由面板把 unit 停的），进程启动时 `resume_interrupted_restores` 读它
  并对账：先删标记（这条记录只该被说起一次，留着等于每次重启都报同一件事），再按 unit 此刻的
  状态分别给判词——本机没有这个 unit / 它正在跑但内容可能只写了一半 / systemctl 查不到状态
  （不擅自启动，沿用批次㉜ 的口径）/ failed 先 `reset-failed` 再拉起，每一行都进 journal 与
  审计。正常收尾时顺序是先删标记再放锁：反过来会开一个"锁已被下一次恢复拿走、上一次的标记
  还在"的窗口。锁不能替代这条标记——内核在进程死亡时自动释放锁，恰好意味着死后的现场无痕。
- 备份任务的三路入口共用一把锁，锁拿在进程入口而不是任务正文里。定时器的 `ExecStart=`、
  界面上的"立即备份"和 `choyeonctl backup run` 都是 `python -m app.backup_runner <id>` 起
  独立进程，只共用这一个入口；`run_job` 在这一层按 `backup-<id>` 抢占，抢不到就打印跳过原因
  并返回 0。返回 0 而不是失败：同一任务同时跑两次时，放弃后到那一次是正确行为（前一份马上
  就落盘），把它报成失败会让 timer 的 journal 与 unit 状态长期挂着一条假故障，而真正该失败的
  （`pg_dump` 崩了、磁盘满）就被淹没在假故障里。
- 归档文件的占名从"先看存不存在"改成 `O_CREAT|O_EXCL` 的原子创建。秒级时间戳本来就可能撞，
  旧写法检查完存在性就把名字交出去：定时器与手动备份在同一瞬间都看到"这个名字还空着"，于是
  其中一个把另一个刚写好的归档原地覆盖，两边都报告成功——目录里只剩一份，那个时间点的一份
  历史无声消失。存在性检查挡不住两个进程同时通过，只有创建本身原子才行。补号循环加了上界：
  备份目录里若已堆了一个同秒的目录，无限递增会把调用方挂死，不如如实报错。
- 恢复时暂存的 `.panel.env` 副本现在参与保留轮替。`_restore_app` 会把应用当前的
  `bk<id>-<时间戳>-pre-restore.env` 存进备份目录（含口令），正常路径回写后就地删除，进程被杀
  在半程时它就留下了；而轮替的识别正则只认 `.gz`/`.tgz`，于是每一次崩掉的恢复都在备份目录里
  存下一份永不淘汰的凭据副本，既占磁盘又扩大口令的可读面。认进形状里，它就和真备份一起按
  keep 淘汰。
- 定时执行补上了和手动执行同一把锁，方向反过来时原先没人挡。计划任务的"立即执行"早就
  `flock` 了任务自己的锁文件，所以"手动 + 手动"挡得住；但管理员点下立即执行、锁在面板进程手里
  时 timer 恰好到点，systemd 只知道同一个 unit 不能重叠，看不见那把进程内的锁，于是两份
  `choyeonctl backup run`、两个 `pg_dump` 同时打同一个目标。`ExecStart=` 现在套
  `flock -n -E 0 <锁文件> -- <命令>`：`-n` 不排队（排队的 oneshot 会一直占着这个 unit，而延后
  那一轮跑的已经不是它该看的现场）；`-E 0` 只在抢不到锁时按成功退出，因为 oneshot 非 0 退出会
  把 unit 留在 failed 名单里直到有人 `reset-failed`，而告警项 failed-units 是 crit——一次正常
  的"跳过重叠的一轮"不该把 crit 长挂在管理员手机上。实测 util-linux 2.41：`-E 0` 只改抢不到锁
  这一种情况，命令自身失败仍原样传出（`flock -n -E 0 f sh -c 'exit 5'` 返回 5），没有把失败
  粉饰成绿灯。机器上没有 `flock`（BusyBox 等）时退回裸命令，宁可少一把锁，也不要让 ExecStart
  指向一个不存在的二进制、每一轮退出 127。
- 计划任务的 unit 现在在面板启动时按库里的行重渲一遍。unit 只在任务增删改时落地，所以升级
  面板不会碰到存量任务的 `ExecStart=`——上面那条 flock 对装机时就存在的任务就成了"代码里写了、
  盘上没有"，而这正是本批要挡的那个方向。新增 `sync_all_units` 挂在 lifespan 里（备份那侧的
  `sync_all_timers` 早就是这个形状）。只处理启用中的行：禁用任务的 unit 本就不该在盘上，对它
  重跑一遍同步等于朝不存在的 unit 发 disable/stop/reset-failed 四趟命令，还会把管理员在面板外
  手工启用的 timer 按库里的禁用状态压回去。逐行 try，一个坏任务（argv 不在白名单、表达式解析
  不了）不该把其余任务的对账一起停掉。
- 部署的后台任务没建起来时，不再留下一条假"进行中"。原先 `INSERT` 写完 `status='running'`
  之后才 `create_task`，那一步抛错（事件循环正在关）就既不放锁也不收尾：一条谁都不会结束的
  running 记录把那把锁钉死，此后该应用的删除、回滚、改配置全被挡住，直到回收器满
  `DEPLOY_BUDGET_SEC` 才认它是孤儿。现在异常分支当场放锁并把记录收尾成 failed 并写日志，
  让"没启动起来的部署"在当场就留下答案。
- 锁的留痕里要写操作者，所以部署/回滚/删除/同步/恢复五个服务函数收下了 `actor`，路由传当前
  登录用户、CLI 传 `cli`。被拒绝的那第二次点击因此能说"admin 正在部署"，而不是所有人都是
  `system`。顺带补上测试告警的审计（`settings:alert-test`）：它往管理员的邮箱/飞书里真发一条
  消息，而旁边同样会发通知的 `run-checks` 早就记了审计——事后查不到是谁发的，等于"谁在半夜把
  告警发到老板手机上"这个问题没有答案。
- 新增回归测试：`backend/tests/test_batch33.py`（52 条 + 4 子用例）用真实子进程跑锁
  （`flock` 的语义不能在同一进程里自证：同进程重复 `open` 再 flock 是另一套规则），覆盖
  六个入口共用同一键、名字与 id 两种 target 解析到同一把锁、恢复标记的落地与启动对账四个分支
  的判词、`_free_path` 的原子占名与补号上界、`run_job` 抢不到锁时返回 0、cron 的
  `flock -n -E 0` 与没有 flock 时的回退、以及部署任务未起来时锁与记录的当场收尾。
  `test_batch28` 的恢复夹具改为按 SQL 分流 `query_one`——把 `query_one` 整体打桩会让每次恢复都
  凭空读到一条"正在跑的部署"，守卫测的就不是守卫了。

## [1.1.10] - 2026-10-12

本轮的改动集中在三处：认证与会话的安全边界、移动端触控与窄屏版式、仓库门面（品牌资产与 README）。

### 新增
- GitHub 门面资产：`assets/` 下 logo、banner、架构图、部署流程图与界面示意图（SVG，随仓库渲染）
- `LICENSE`（MIT，此前 README 已声明 MIT 但仓库缺少该文件）、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`
- `.github/`：Issue 模板（缺陷 / 功能 / 部署求助）、PR 模板、Dependabot（Actions / npm / pip）
- `backend/tests/test_repo_hygiene.py`：门面一致性回归测试（社区文件齐全、README 引用的本地文件存在、
  版本号三处一致、`assets/` 无孤儿且为合法 SVG、doctor 项数与文档口径一致、Issue 模板字段完整、
  `.env.example` 与 `config.py` 双向对账、banner 与 social 卡版式几何契约、
  品牌标记四处同源且图标不使用 `<text>`、界面品牌位只走 `LogoMark`、徽章查询串形态与缓存时长、
  入口图标链接与 `theme-color` 对账）
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
  渲染成空白，缓存后重复加载不再破图；其中 `license` 与 `OS` 两枚原先被拼成 `…-颜色&cacheSeconds=…`
  （没有 `?`），shields 仍回 200 但把 `&…` 当作颜色解析失败，自定义色和缓存时长一起丢失
- 后端为 `.webmanifest` 注册 `application/manifest+json`：部分发行版的 MIME 表里没有这个扩展名，
  回落到 `application/octet-stream` 会让浏览器忽略清单
- 文档与代码一致性核对：`choyeonctl doctor` 项数、CLI 参数表与 `choyeonctl schema --json` 对齐
- `.gitignore` 补齐工具缓存目录（`.pytest_cache/`、`.mypy_cache/`、`.ruff_cache/`、`*.egg-info/`）
- `.env.example` 补全 `CP_TRUSTED_PROXIES`、`CP_MAX_EXTRACT_MB/FILES`、`CP_AVATAR_MAX_KB`、
  `CP_TERMINAL_OUT_BUFFER_KB` 五个此前只在源码里存在的键，并修正 `CP_TRUST_PROXY` 的适用场景说明；
  `test_repo_hygiene` 新增双向对账，config 与样例不会再各说各话
- Release 正文改为就地部署口径（`choyeonctl upgrade` / `--no-pull`），并列出 tarball 产物与 SHA256
- 移动端触控目标收紧：`≤900px` 档把按钮、输入框、选择器、页签、分页、勾选框、抽屉导航补到 44px 高，
  纯图标按钮与输入框后缀的显隐口令按钮同时补到 44px 宽，表格里的文字操作按钮补横向内边距（列宽固定，
  多出的宽度在 `.cp-ops` 内换行，卡片头部的工具条不补——实测补宽会把最右按钮顶出视口）；开关与清除按钮
  只外扩命中区、视觉尺寸不变。375/768 两档 × 20 路由共 40 组量测里，按「高 < 40 或宽 < 24」计数的可点
  目标归零；被 44px 外层包住的输入框内层元素、以及靠伪元素外扩的三处（清除按钮、仓库外链、开关）改用
  `elementFromPoint` 逐点验证命中
- 全站 14 处宽表的「操作」列加 `fixed: 'right'`：375 档实测操作按钮原先落在视口外 272-770px，现在常驻右侧
- 概览格子由 `grid auto-fit` 改为 `flex-grow`：格子数（管理员 5 / 只读 4）与列数除不尽时，末段会露出容器底色，
  实测 375/640/1100 档都是一块灰色空洞；改法让最后一行的格子自己摊满剩余宽度，任何档数都不留洞
- 卡片头部（标题 + header-extra）`≤900px` 允许换行、`≤640px` 标题与工具条各占一行：375 档实测「监控」标题被压成
  0 宽一字一行、「应用（3/3 运行中）」被折成两行。naive 在 `.n-card > .n-card-header .n-card-header__main` 上写了
  `flex: 1 1 0%`（0,3,0），运行时注入的样式又排在我们的 `<link>` 之后，同特异度它会赢，所以这里改用 column +
  `align-items: stretch` 绕开竞争，并用 `:not(:has(.n-card-header__close))` 把带关闭按钮的对话框卡头排除在外
- 概览 hero 的右侧动作组去掉 `:wrap="false"`：375 档「快捷操作」原先顶出视口 10px
- 监控卡头部的网卡选择器在 `≤640px` 收到 140px：200px 选择器 + 120px 段排在 301px 内宽里放不下，工具条折成两行
  会把卡片头撑到 150px 高

### 安全

- `/api/auth/setup` 收口：判据从「有没有 admin」改成「整张表为空」，插入放进 `create_first_admin`
  的单锁临界区。此前管理员被 CLI 删光而机器上还留着 viewer 时，setup 会对公网重新开放，谁先
  POST 谁就是 root；两个并发未登录请求也能同时通过检查、各拿一枚 admin token。撞名改回 400，
  不再把 `IntegrityError` 的栈抛进 journal。
- 登录限速加第二道按账号的桶（`acct:<name>`）：IP 桶挡不住代理池慢爆——几百个 IP 各打 9 次/分钟，
  落在同一个 admin 账号上就是 9×N。账号检查放在 scrypt 之前（拦截不再顺手烧 16MB 哈希），
  窗口起点固定不随失败顺延（慢 drip 不会把锁变成永久封禁），只给真实存在的账号记账。
- CSP 的 `script-src` 收紧为 `'self'`：首屏防闪主题脚本从 `web/index.html` 的内联块挪到
  `web/public/theme-boot.js`。`style-src` 仍留 `'unsafe-inline'`，naive-ui 运行时注入样式必需它。
- 长连接不再只在握手时验一次身份：网页终端与日志 SSE 每 60 秒复验（库里的角色 + token epoch +
  到期时间），改密、降权、删号、票过期都会立刻停流并留一条审计；终端另加 180 秒空闲回收，
  半死连接不会再把 `TERMINAL_MAX_SESSIONS` 的名额占满到重启。
- 终端帧守卫：JSON 合法但不是对象的一帧（`1`、`"x"`、`[]`）原先会走到 `.get` 抛
  `AttributeError`，把会话和 pty 一起打死，而任何已登录 admin 发一条消息就够了。
- 校验函数统一 `fullmatch`：正则写成 `^…$` 配 `match` 时 `"example.com\n"` 也算合法，结尾那个
  换行会造出「看起来不存在、却又占着端口」的域名/库名/文件名；管理员用户名同理改用 `\Z`。
- 落盘权限：`CP_DATA_DIR` 收到 0700、`panel.db` 收到 0600（里面有 `pass_hash` 与 `jwt_secret`，
  签名密钥外泄等于任何人都能签一枚 admin token）；打包临时文件改 `O_CREAT|O_EXCL|O_NOFOLLOW`
  + 0600，`_keep_owner` 复制权限位时不再把 setuid/setgid 一起抄过去。
- 恢复应用归档后收平 setuid/setgid/sticky 位：`filter="tar"` 按归档里的 mode 原样落盘，
  root 解包一个手工上传的 4755 二进制就是给同机任何被攻破的应用留一个提权落点；收平了几个条目
  会以 `!!` 行如实报出来，不静默改人家的文件。
- 面板自己的 nginx 入口不再混进应用配置列表：应用的 port 允许 1024-65535，填成面板端口时
  归属判定会把 `choyeon-panel.conf` 认成「我的配置」，保存并 reload 一次就把面板的 HTTPS 入口
  换成指向那个应用，而 `remove_vhost` 只认 `panel-*.conf`，救不回来。
- 出口脱敏：审计的 detail 与 action、错误日志、`app:ssl` 回执、备份执行日志、恢复结果统一过
  `redact_creds`（子进程回显里的 `PGPASSWORD=`、Bearer 串、连接串）；`user:update` 审计写明改了
  角色/口令/昵称/头像中的哪几项，不再只留一个光秃秃的用户名。
- doctor 的权限项从只看 `.env` 扩到 `.env` + 数据目录 + `panel.db`，并把 `user create` 的修复
  命令与 CLI `deploy` 的下一步提示改成 `--password-stdin` 写法（口令不再进 argv、`ps` 与 shell 历史）。

### 测试

- `backend/tests/test_batch6a.py`：覆盖上面这批改动（首管理员建号临界区与整表还原、账号桶的
  锁定/清零、`--password-stdin` 的 CLI 解析、SSE 与终端的复验停流与空闲回收）。
- `test_batch5g`、`test_batch5k`、`test_deploy_contract`、`test_repo_hygiene` 跟随新
  `request.state.cp_exp` 与首屏主题脚本的新落点；`theme-color` 一致性断言改看 index.html +
  `theme-boot.js` 拼出的首屏那一帧，并要求初值等于某一档 `--cp-bg`。
- `backend/tests/test_repo_hygiene.py` 的 README 口径断言同步：CI 实为 4 个 job、
  `Ran 1080 tests … OK`、引用文件与 `.env.example` 覆盖继续由 CI 把守。

### 文档

- README 去掉宣传腔与重复句：删掉英文回声段、Star 煽情段和社交徽章；「60 秒部署」改成按实测
  的 3-8 分钟口径；常驻内存改成带测量口径的 ≈50 MB（`MemoryCurrent`）而非区间猜测；架构三小标题
  从对仗句式改回直陈；SSL 行并入「网站与证书」；安全设计补上本轮加固，命令行段指向
  `choyeonctl schema --json` 作为契约权威。

## [1.1.8] - 2026-10-11

本轮的主线是**让面板说的和机器上发生的一致**，以及把部署做成任意发行版都能一行跑通。

### 新增
- **多发行版一键安装**：Debian 系（Ubuntu/Debian/Mint/Kali）、RHEL 系（CentOS Stream/Rocky/AlmaLinux/Fedora/Amazon Linux）、Arch 系、openSUSE、Alpine（musl）自动识别并安装全部依赖，含 Python 3.11+ 与 Node 22 LTS
- **`choyeonctl doctor` 安全自检**当时扩展到 12 个检查项（监听地址、管理员、服务、前端产物、nginx、防火墙、备份、磁盘、文件权限、全局 CLI、证书续期托管、timer 与任务记录对账），每项附可复制的修复命令；网页「安全自检」页同源。项数随版本增加，当前口径以 AGENTS.md 为准
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

[Unreleased]: https://github.com/Choyeon/choyeon-panel/compare/v1.1.10...HEAD
[1.1.10]: https://github.com/Choyeon/choyeon-panel/releases/tag/v1.1.10
[1.1.8]: https://github.com/Choyeon/choyeon-panel/releases/tag/v1.1.8
