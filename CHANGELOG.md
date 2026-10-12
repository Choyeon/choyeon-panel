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
- `test_batch15a.py`（78 条）锁住这些方向：路径含空格的映射行整串保留（maps 的 path 可以带空格，
  按 `split()[5]` 取会只剩前半段）、扫描中途退出的进程整条跳过（它名下的 deleted 映射还在，
  报出来只会让人去查一个已经不存在的进程）、映射 inode 与当前文件一致时丢弃、orphan 单元排到表尾并写明
  「systemctl 管不到」、版本比较按 dpkg 的退出码而非替身的布尔值（`returncode=False` 时 `False == 0` 成立，
  替身自己就能把旧内核说成新内核）、元包不计入待重启、`rc` 残留行忽略、判定不了时不落成「要重启」、
  nginx 参照用 worker（回归 master 写法会误报还没 reload）、sshd 的相对 Include 按 /etc/ssh 补基准并展开 glob、
  非包路径不起子进程、所有 spawn 出去的 argv 字面量里不许出现 restart / reload / daemon-reload / reboot
  （用 AST 扫，grep 会被 findings 里那些本该给人执行的命令误伤）、读不到的节只许产 info 级判词、
  CLI 文本 CJK 列按显示宽度补齐、schema / argparse / AGENTS.md / README / SECURITY 五处的命令与自检项数一致。

### 修复
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
