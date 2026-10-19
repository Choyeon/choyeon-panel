import asyncio
import os
import re
import threading
import time
import weakref
from contextlib import suppress
from pathlib import Path

from .. import config
from ..log import warn
from ..util import PROBE_TIMEOUT, is_domain, is_email, run

CONF_DIR = config.NGINX_CONF_DIRS[0]
TLS_DIR = config.TLS_DIR

RE_SERVER_NAME = re.compile(r"server_name\s+([^;]+);")
# 链路对账要知道反代目标"是谁"，光有端口不够：同一个 3100 指在 127.0.0.1 上是本机应用，
# 指在别的机器上，本机的监听表就完全不是它的证据。
RE_PROXY_TARGET = re.compile(r"proxy_pass\s+https?://([^:/;\s]+)(?::(\d{1,5}))?\s*;")
RE_UP_TARGET = re.compile(r"\bserver\s+([^:/;\s]+)(?::(\d{1,5}))?")
RE_LISTEN_LINE = re.compile(r"^\s*listen\s+([^;]+);", re.M)
# `$` 也排除：certbot 写的 `return 301 https://$host$request_uri` 是自己跳自己，
# 主机名位置是变量，跳去哪个域名读不出来；而末尾的 `$request_uri` 必须留在捕获之外，
# 否则捕获到的是 `choyeon.cc$request_uri` 这种带变量的串，跟任何 server_name 都对不上。
RE_RETURN_HTTPS = re.compile(r"return\s+30[123]\s+https?://([^/:;$\s]+)")
# 跳转把请求方带来的 Host 原样拼进 Location（`return 301 https://$host$request_uri`，
# certbot 与面板模板都这么写）。写在具名域名块里是自己跳自己，无害；
# 一旦这个块成了端口的默认档（未知 Host 也进得来，见 pick_default_servers），
# 任何解析到本机的名字都会被送到 `https://<那个名字>/`——一条 DNS 记录就换来一次
# 「看着像本站发起」的跨站跳转。所以 Host 位置是变量这件事本身要单独成为一类事实。
# `(?![\w.-])` 保证只认 Host 位恰好是 `$host` 的写法，`$hostname` 之类变量名不误伤。
RE_HOST_REFLECT = re.compile(r"\breturn\s+30[123]\s+https?://\$host(?![\w.-])")

# 反代目标是"本机另一个端口"还是"别的机器"，决定了本机监听表能不能拿来对账，
# 也决定了域名归属能不能按端口落。两处（链路对账、纳管判定）共用这一份判据。
LOCAL_TARGETS = ("127.0.0.1", "localhost", "::1")

# 「写文件 → nginx -t → reload」必须整体串行：nginx -t 校验的是 /etc/nginx 全目录，
# 两个并发写会让其中一方在校验时读到另一方刚截断的空文件（把好配置判成语法错误，
# 回滚掉的还是别人的改动），或者 reload 时加载半份 server 块——一次 reload 失败
# 是全站域名同时不可用。面板的这些写路径都是低频接口，串行没有可感代价。
# 锁按事件循环建：一个进程里通常只有服务那一个循环，但 CLI 与测试会各自
# asyncio.run(...) 开一个新循环，跨循环复用同一把 asyncio.Lock 会直接抛
# "is bound to a different event loop"（不同循环之间本来也不存在并发，换新锁即可）。
_nginx_locks: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _nginx_section() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _nginx_locks.get(loop)
    if lock is None:
        lock = _nginx_locks[loop] = asyncio.Lock()
    return lock


def _write_conf(path: str, content: str) -> None:
    """同目录临时文件 + os.replace 落盘，而不是 write_text 原地截断。

    nginx -t 与 reload 都会读这个文件，原地截断意味着读侧可能拿到空文件或半份配置；
    rename 在同一目录内是原子的，读到的要么是旧的完整文件要么是新的。
    临时文件后缀刻意用 `.tmp`：conf.d 的 include 规则是 `*.conf`，进程中途被杀
    留下的残留文件不会被 nginx 当配置读进去。
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with suppress(OSError):
            os.unlink(tmp)


# `http2 on;` 是 1.25.1 才有的写法，而旧写法 `listen 443 ssl http2;` 在新版上只是
# 一句 deprecation warn（本机 nginx 1.26.3 实测：`nginx -t` 仍然通过）。反过来把
# 新写法发给旧版（Ubuntu 22.04 的 1.18、Debian 12 的 1.22）就是
# `[emerg] unknown directive "http2"`，nginx -t 直接不过，面板在任何一台旧版机器上
# 都套不上 HTTPS 域名。所以按实测版本选写法，版本拿不到时用旧写法（覆盖面最广）。
_HTTP2_NEW_SYNTAX = (1, 25, 1)
_ver_cache: tuple | None = None


async def nginx_version() -> tuple | None:
    """`nginx -v` 的 (major, minor, patch)；拿不到返回 None，且失败不进缓存。"""
    global _ver_cache
    if _ver_cache is not None:
        return _ver_cache
    r = await run("nginx", ["-v"], timeout=PROBE_TIMEOUT)
    m = re.search(r"nginx/(\d+)\.(\d+)\.(\d+)", r["out"] or "")
    if m:
        _ver_cache = tuple(int(x) for x in m.groups())
    return _ver_cache


async def http2_uses_new_syntax() -> bool:
    ver = await nginx_version()
    return ver is not None and ver >= _HTTP2_NEW_SYNTAX


# 一张多 SAN 证书只躺在**第一个域名**的 lineage 目录里（certbot 的常规产物，
# 例如 example.com + www.example.com 共用 live/example.com/）。旧判定是
# `_has_cert(domain)`：只看 live/<domain>/ 存不存在，第二个 SAN 就被判成"没有证书"，
# 于是面板给一个本来能走 HTTPS 的域名写出明文 vhost，还把 certbot 已经配好的
# HTTPS 覆盖掉。这里按证书内容建 domain -> lineage 的索引。
_CERT_TTL = 60.0
_cert_cache: tuple[float, dict] | None = None
_cert_lock = threading.Lock()


def invalidate_cert_cache() -> None:
    global _cert_cache
    with _cert_lock:
        _cert_cache = None


async def cert_index() -> dict:
    """domain -> /etc/letsencrypt/live 下的 lineage 目录名。"""
    global _cert_cache
    now = time.time()
    with _cert_lock:
        if _cert_cache and now - _cert_cache[0] < _CERT_TTL:
            return _cert_cache[1]
    from . import cert_ops  # 延迟导入：cert_ops 会回头用 nginx_ops 的常量

    try:
        names = sorted(p.name for p in Path(TLS_DIR).iterdir() if (p / "fullchain.pem").is_file())
    except OSError:
        names = []
    idx: dict[str, str] = {}
    for name in names:
        meta = await cert_ops._openssl_x509(f"{TLS_DIR}/{name}/fullchain.pem")
        domains = meta.get("domains") or []
        if not domains:
            # SAN 读不出来（老 openssl、自签只写 CN）时按目录名兜底：
            # 把一个其实有证书的域名判成"没证书"会让 vhost 降级成明文，比误判成有证书严重。
            domains = [name]
        for d in domains:
            # 键一律小写：TLS 证书里的名字与 nginx 的 server_name 一样，比对前都得先折成小写
            # （见 _analyze_block 的说明）。留原样就会让同一个域名因为证书里写成 `Blog.Example.com`
            # 而查不到，把已经配好 HTTPS 的域名判成没证书。
            idx.setdefault(str(d).strip().lower(), name)
    with _cert_lock:
        _cert_cache = (now, idx)
    return idx


async def cert_lineage(domain: str) -> str | None:
    """覆盖这个域名的证书在 live 下的目录名；没有则 None。"""
    domain = (domain or "").strip().lower()
    idx = await cert_index()
    if domain in idx:
        return idx[domain]
    # 索引里没有但目录在：手工放的证书（目录名即域名）。保持旧行为，别把已配好的
    # HTTPS 判成没证书。
    return domain if os.path.isdir(f"{TLS_DIR}/{domain}") else None


def render_vhost(spec: dict, *, ssl_lineage: str | None = None, http2_new: bool = False) -> str:
    """渲染一个 vhost。

    `ssl_lineage` / `http2_new` 都要由调用方**实测**后传进来（见 apply_vhost）：
    在这里默认"没证书"或"新版写法"都会让面板在旧机器/多 SAN 域名上写出 nginx 认不下的配置。
    """
    domain = (spec["domain"] or "").strip().lower()
    if not is_domain(domain):
        raise RuntimeError(f"invalid domain: {domain}")
    port = spec["port"]
    ssl = bool(ssl_lineage)
    proxy_headers = (
        f"        proxy_pass http://127.0.0.1:{port};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_read_timeout 300s;"
    )
    if spec.get("proxyWebsocket", True) is not False:
        proxy_headers += (
            "\n        proxy_set_header Upgrade $http_upgrade;"
            "\n        proxy_set_header Connection \"upgrade\";"
        )
    ssl_lines = (
        f"    ssl_certificate {TLS_DIR}/{ssl_lineage}/fullchain.pem;\n"
        f"    ssl_certificate_key {TLS_DIR}/{ssl_lineage}/privkey.pem;\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
        "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n"
    ) if ssl else ""
    # 跳转目标写死成这个块自己的域名，不用 `$host`。
    # 差别的分量只有当这个块成了端口的默认档时才显出来：Debian 上 conf.d 先于 sites-enabled
    # 加载，而没有任何 default_server 时「第一个听 80 的块」就是默认档 —— 那时未知 Host
    # 也会进到这个块，`return 301 https://$host$request_uri` 等于把用户送到他请求头里
    # 自带的那个域名，谁能在 DNS 上指过来一条记录，谁就拿到一次「像是本站发起」的跳转。
    # certbot 手工改写的老配置仍可能是 `$host` 那种形状，由站点链路对账单独点名（见
    # site_audit_service 的 default-reflect）。
    redirect = (
        f"server {{\n    listen 80;\n    listen [::]:80;\n    server_name {domain};\n"
        f"    return 301 https://{domain}$request_uri;\n}}\n\n"
    ) if ssl else ""
    security_headers = (
        "        add_header X-Content-Type-Options nosniff always;\n"
        "        add_header Referrer-Policy strict-origin-when-cross-origin always;\n"
        + ("        add_header Strict-Transport-Security \"max-age=31536000\" always;\n" if ssl else "")
    )
    # http2 只在 443 上开：`http2 on;` 写在明文 server 里 nginx -t 也放行（本机 1.26.3 实测），
    # 但那等于对 :80 打开 h2c 前置知识连接；而它落到 1.25.1 之前更是
    # `[emerg] unknown directive "http2"`，直接让整份配置校验不过。
    if ssl:
        listen_v4 = "listen 443 ssl http2;" if not http2_new else "listen 443 ssl;"
        listen_v6 = "listen [::]:443 ssl http2;" if not http2_new else "listen [::]:443 ssl;"
        h2 = "    http2 on;\n" if http2_new else ""
    else:
        listen_v4, listen_v6, h2 = "listen 80;", "listen [::]:80;", ""
    return (
        f"# managed by choyeon-panel ({spec['name']}) — do not edit by hand\n"
        f"{redirect}server {{\n"
        f"    {listen_v4}\n"
        f"    {listen_v6}\n"
        f"{h2}{ssl_lines}    server_name {domain};\n"
        "    client_max_body_size 100m;\n"
        "\n"
        "    location / {\n"
        f"{proxy_headers}\n"
        f"{security_headers}"
        "    }\n"
        "}\n"
    )


def vhost_path(name: str) -> str:
    return f"{CONF_DIR}/panel-{name}.conf"


# Debian 的 nginx.conf 只 include `conf.d/*.conf` 与 `sites-enabled/*`，
# 而 conf.d 排在前面：同一个 server_name 写在这两处时，nginx 只认先加载的那一份
# （nginx -t 会给一句 conflicting server name 警告，然后照样通过）。
# 所以"这个域名有没有被别处定义"只能按**实际加载**的文件集合判断：
# 拿 sites-available 里那些没启用的文件算会误报，拿内存里的旧配置算又会漏报。
# nginx -T 实测 42ms，缓存 15 秒并写完即失效。返回的是**列表不是集合**：
# 谁先加载决定了同名配置谁赢、也决定了没人写 default_server 时默认档是谁（见 pick_default_servers）。
# 拿不到集合时返回 None，domain_owner 也就无法判断（返回值同样是 None）。
# **这是"查不了"不是"没冲突"**：面板照常写入（一次 nginx -t 不过的可解释故障，
# 比因为读不到 -T 就拒绝所有域名操作更容易恢复），所以必须 warn 一条到面板日志，
# 让"两份配置互相遮蔽"这种全站性后果有迹可查，而不是静默假定安全。
_LOADED_TTL = 15.0
_loaded_cache: tuple[float, list | None] | None = None
_loaded_lock = threading.Lock()


async def loaded_conf_paths() -> list[str] | None:
    """nginx 这次真正加载的配置文件，按它读进去的先后顺序给出；读不到返回 None。

    顺序是有意义的，别折成 set：同一端口上的「默认 server」是加载顺序里第一个听这个口的
    块（没人写 default_server 时），同一个 server_name 写了两份时赢的也是先加载的那一份。
    `nginx -T` 打出的 `# configuration file …:` 行正好就是先后顺序，直接拿来用，
    比按目录猜 include 规则可靠（nginx.conf 里改一行顺序，猜的那套就全错）。
    """
    global _loaded_cache
    now = time.time()
    with _loaded_lock:
        if _loaded_cache and now - _loaded_cache[0] < _LOADED_TTL:
            return _loaded_cache[1]
    r = await run("nginx", ["-T"])
    if r["code"] != 0:
        return None
    ordered: list[str] = []
    seen: set[str] = set()
    for m in re.findall(r"^# configuration file (.+?):", r["out"] or "", re.M):
        rp = os.path.realpath(m)
        if rp not in seen:
            seen.add(rp)
            ordered.append(rp)
    data = ordered or None
    with _loaded_lock:
        _loaded_cache = (now, data)
    return data


async def domain_owner(domain: str, target: str) -> str | None:
    """返回真正加载了该 domain、又不是本次要写的那个文件的配置路径。

    None 有两义：nginx -T 读不到，或确实没有别处定义。调用方用
    `loaded_conf_paths() is None` 区分并记日志，见上面那段说明。
    按加载顺序取第一个命中的：那一份才是真正在服务这个域名的（后面的会被 nginx 以
    conflicting server name 警告跳过）。
    """
    loaded = await loaded_conf_paths()
    if loaded is None:
        return None
    real_target = os.path.realpath(target)
    low = (domain or "").strip().lower()
    for p in loaded:
        if p == real_target:
            continue
        with suppress(OSError):
            if low in analyze_config(Path(p).read_text(errors="replace"))["serverNames"]:
                return p
    return None


class DomainOwnedElsewhere(RuntimeError):
    """这个域名的 server 块已经由**别的面板文件**定义，面板不生成第二份。

    单独一个类型是为了让调用方分清两种失败：
    部署跑了几分钟不该因为一次可解释的域名归属冲突被判成 failed（站点仍按现有配置
    服务，只是不由面板托管）；而 reload 失败、nginx -t 不过那类是真失败，必须照实失败。
    """


async def apply_vhost(spec: dict):
    domain = spec["domain"]
    Path(CONF_DIR).mkdir(parents=True, exist_ok=True)
    path = vhost_path(spec["name"])
    async with _nginx_section():
        # 证书与 nginx 版本都在锁内读：锁外读到的可能是几秒钟之前的状态
        # （并发的续期刚写好证书，或上一次 -T 结果还在 15 秒缓存里）。
        lineage = await cert_lineage(domain)
        http2_new = await http2_uses_new_syntax()
        content = render_vhost(spec, ssl_lineage=lineage, http2_new=http2_new)
        owner = await domain_owner(domain, path)
        if owner:
            raise DomainOwnedElsewhere(
                f"域名 {domain} 已由 {owner} 定义，面板没有生成第二份 server 块："
                "同一域名存在两份配置时 nginx 只按加载顺序认第一份（conf.d 先于 sites-enabled），"
                f"强行套用会让 {Path(owner).name} 里的手工配置静默失效。"
                f"要让面板托管这个域名，请删除那份文件，或把它改名为 {Path(path).name} 放到 {CONF_DIR}。"
            )
        if await loaded_conf_paths() is None:
            warn(f"nginx -T 不可读，本次域名套用（{domain}）未能确认该域名是否已被别处定义")
        old = Path(path).read_text(errors="replace") if os.path.exists(path) else None
        _write_conf(path, content)
        check = await run("nginx", ["-t"])
        if check["code"] != 0:
            # 回滚到原文件（新建的则删除），并恢复 nginx 配置状态
            if old is not None:
                _write_conf(path, old)
            else:
                try:
                    os.unlink(path)
                except OSError as e:
                    # 删不掉就是最坏的一种：坏配置留在 conf.d/ 里，之后每一次 nginx -t
                    # 都连带失败，面板上其它域名的套用与续期会全部不可用。
                    # 吞掉异常再报"已回滚"等于指着一颗地雷说地面是平的。
                    raise RuntimeError(
                        f"nginx -t 校验失败，且刚写入的 {path} 删除失败（{type(e).__name__}）："
                        "这份坏配置留在目录里会让后续所有 nginx 操作一起失败，"
                        "请手工删除该文件后重新校验。\n"
                        f"原始错误：{check['out']}"
                    ) from e
            raise RuntimeError(f"nginx config test failed, rolled back: {check['out']}")
        reload = await run("systemctl", ["reload", "nginx"])
        if reload["code"] != 0:
            raise RuntimeError(f"nginx reload failed: {reload['out']}")
    invalidate_domain_cache()


async def remove_vhost(name: str):
    path = vhost_path(name)
    if not os.path.exists(path):
        # 没有这个应用的文件就别去 nginx -t + reload：拆除/清理流程对从未配过域名的
        # 应用也会调到这里，空跑一次 reload 是无谓的全站配置重载。
        return
    async with _nginx_section():
        old = Path(path).read_text(errors="replace")
        os.unlink(path)
        check = await run("nginx", ["-t"])
        if check["code"] != 0:
            # 校验不过多半是因为别的文件引用了这个文件里的 upstream，或者机器上本来
            # 就有另一处坏配置。这时把文件放回去：站点照旧由原配置服务，
            # 用户不会因为一次"删除"而莫名丢站；失败原因仍然如实报出来。
            _write_conf(path, old)
            raise RuntimeError(
                "nginx 校验未通过，已把该域名的配置恢复回去（面板没有重载，站点仍按原配置提供服务）："
                f"{(check['out'] or '').strip()[:200]}"
            )
        reload = await run("systemctl", ["reload", "nginx"])
        if reload["code"] != 0:
            # 已经删除且校验通过，这里不回滚文件：nginx 仍在按内存里的旧配置服务，
            # 下一次成功 reload 就会断开。状态与磁盘不一致必须说清楚，
            # 静默返回 ok 会让界面显示"已移除"而域名照旧可访问。
            raise RuntimeError(
                "配置已删除，但 nginx 校验通过、重载失败（该域名可能仍在按旧配置提供服务）："
                f"{(reload['out'] or '').strip()[:200]}"
            )
    invalidate_domain_cache()


async def issue_cert(domain: str, email: str | None = None) -> str:
    if not is_domain(domain):
        raise RuntimeError(f"invalid domain: {domain}")
    # email 直接进 certbot 的 argv，而面板以 root 跑：certbot 会拿它注册 ACME 账户、
    # 写进 /etc/letsencrypt 的配置里。空串、带空白、以 `-` 开头的值会让参数解析错位
    # （`-m --no-redirect` 之类把后面的选项当邮箱吃掉），所以在这里一次收口，
    # 而不是在每个调用点各补一层。留空仍然是合法选择（走无邮箱注册）。
    email = (email or "").strip()
    if email and not is_email(email):
        raise RuntimeError(f"邮箱格式不正确：{email}")
    args = ["--nginx", "-d", domain, "--non-interactive", "--redirect", "--agree-tos", "--no-eff-email"]
    extra = ["-m", email] if email else ["--register-unsafely-without-email"]
    args = args[:4] + extra + args[4:]
    # --nginx 会自己改写 /etc/nginx 下的配置（server_name、跳转、有时整份 vhost），
    # 而⑤H 建立的规矩是「nginx 配置的写 → nginx -t → reload」整体串行：
    # certbot 绕开这把锁跑，面板这边就可能在校验时读到它刚写到一半的文件，
    # 把别人的改动当成坏配置回滚掉。所以申请证书也要占住同一个临界区。
    async with _nginx_section():
        r = await run("certbot", args, timeout=300)
        if r["code"] != 0:
            raise RuntimeError(f"certbot failed: {r['out']}")
        invalidate_cert_cache()
        invalidate_domain_cache()
    return r["out"]


async def renew_certs() -> dict:
    """跑一次 `certbot renew`，并用法器到期日的实际变化确认到底续到了没有。

    certbot 在「还没到期、所以什么都没做」时同样返回退出码 0，只看退出码就等于把
    「什么都没发生」报成「已续期」；证书文件更新了也不等于进程里换了证书，
    所以确实有更新时补一次 nginx reload（certbot 自带的 deploy-hook 不保证配了）。
    """
    from . import cert_ops  # 延迟导入：cert_ops 侧不需要知道 nginx_ops

    async def _expiries() -> dict:
        return {c["name"]: c.get("expiry") for c in (await cert_ops.list_certs())["certs"]}

    before = await _expiries()
    # 同 issue_cert：certbot renew 会改写 nginx 配置并（若配了 deploy-hook）重载服务，
    # 必须占住 ⑤H 那把「nginx 写入临界区」的锁，否则面板这边正写一半的 vhost
    # 会被它当成待校验配置一起读进去。续期本身要几分钟，同期排队的是低频接口，代价可接受。
    async with _nginx_section():
        r = await run("certbot", ["renew", "--non-interactive"], timeout=600)
        invalidate_cert_cache()
    if r["code"] != 0:
        # util.run 把 stderr 并进了 stdout，返回只有 {code, out}：这里再取 r["err"]
        # 就等于"报错正好发生在 out 为空的时候"必然 KeyError，把一条本该是 400 的
        # 失败变成 500（out 为空恰恰是 certbot 被 kill、磁盘写满这类情况）。
        raise RuntimeError(f"证书续期失败 (exit {r['code']}): {r['out'] or '(无输出)'}")
    after = await _expiries()
    renewed = sorted(n for n, e in after.items() if e and before.get(n) and e != before.get(n))
    reloaded = False
    if renewed:
        async with _nginx_section():
            reloaded = (await run("systemctl", ["reload", "nginx"]))["code"] == 0
        invalidate_domain_cache()
    return {
        "log": r["out"],
        "renewed": renewed,
        "unchanged": sorted(set(after) - set(renewed)),
        "reloaded": reloaded,
        "summary": (
            f"已续期 {len(renewed)} 张：" + "、".join(renewed) + ("，nginx 已重载" if reloaded else "，nginx 重载失败")
            if renewed else "没有证书到期，certbot 未做任何变更（这是正常结果，不是失败）"
        ),
    }


def _strip_comments(text: str) -> str:
    """抹掉行注释，保留引号内的原文。

    块扫描靠花括号配对，而注释里出现的花括号（`# 旧写法 listen 443 ssl http2; { }` 这类
    说明）会把配对算错，进而把半份配置当成一个 server 块解析出域名来。
    """
    out: list[str] = []
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _keyword_blocks(body: str, keyword: str) -> list[tuple[int, str, str]]:
    """`keyword 参数 { … }` 的块，返回 (起始深度, 花括号前的参数, 块体)。

    深度是相对传入 body 的：同一段文本里 `upstream x { server 127.0.0.1:8000; }` 的那行
    `server` 在深度 1，靠深度就能和真正的 server 块分开——旧的正则做不到这件事，
    它把整份文件当一个 server 块读，于是一个文件里三个域名的反代端口全被混成一份账。
    花括号不配对（配置本来就坏，nginx -t 会先拒绝）的那块直接跳过，不猜块体。
    """
    out: list[tuple[int, str, str]] = []
    for m in re.finditer(rf"\b{keyword}\b([^{{;]*)\{{", body):
        depth = body.count("{", 0, m.start()) - body.count("}", 0, m.start())
        i, open_count = m.end(), 1
        while i < len(body) and open_count:
            if body[i] == "{":
                open_count += 1
            elif body[i] == "}":
                open_count -= 1
            i += 1
        if open_count:
            continue
        out.append((depth, m.group(1).strip(), body[m.end(): i - 1]))
    return out


def _expand_targets(body: str, upstreams: dict) -> tuple[list[dict], list[int]]:
    """一个 server 块的反代目标与端口（upstream 名按它自己的 server 行展开）。"""
    targets: list[dict] = []
    ports: list[int] = []
    for m in RE_PROXY_TARGET.finditer(body):
        host = m.group(1).strip()
        port_text = m.group(2)
        if port_text is None and host in upstreams:
            # proxy_pass http://<upstream 名>; 展开成它自己的 server 行，与 proxyPorts 同一个口径：
            # 只算被真正引用到的 upstream，没被引用的块不该参与端口对账。
            for h, p in upstreams[host]:
                targets.append({"host": h, "port": p})
                if p is not None:
                    ports.append(p)
            continue
        port = int(port_text) if port_text else None
        targets.append({"host": host, "port": port})
        if port is not None:
            ports.append(port)
    uniq: list[int] = []
    for p in ports:
        if p not in uniq:
            uniq.append(p)
    return targets, uniq


def _default_target(body: str, upstreams: dict) -> dict | None:
    """`location /` 反代到哪儿——这个域名「主体」由谁服务。

    一个站常见形态是主站与接口分开反代（Rosetta：`/api`、`/media` → FastAPI，`/` → SSR）。
    整份文件的端口集合判不出归属（两个应用都在里面），而「打开这个域名看到的是什么」
    只由根路径那一跳决定，所以纳管判定看它。
    """
    for depth, args, inner in _keyword_blocks(body, "location"):
        if depth:
            continue
        parts = args.split()
        if not parts or parts[-1] != "/":
            continue
        targets, _ = _expand_targets(inner, upstreams)
        if targets:
            return targets[0]
    return None


def _analyze_block(body: str, upstreams: dict) -> dict:
    names: list[str] = []
    catch_all = False
    for m in RE_SERVER_NAME.finditer(body):
        for n in m.group(1).strip().split():
            # 一律折成小写：nginx 匹配 server_name 前把 Host 转小写，配置里写成
            # `Video.Example.com` 的块永远按名字匹配不上（只有当默认档时才接得到请求）。
            # 读侧不跟着折，「这个域名归谁」「有没有被别处定义」就会对同一份配置给出
            # 跟大小写有关的答案，而纳管登记表、链路对账与 /api/sites/{domain}/config
            # 早就都是小写口径 —— 差的那一侧就是「页面上看得见这个域名，查看配置却 404」。
            low = n.lower()
            if low == "_":
                # 通配名答不了「这个块归哪个域名」，所以不进 names；但它是一类事实：
                # 默认档写成 `server_name _` 是明摆着的「谁进来都给这一个响应」，
                # 写成具名域名则是「没登记的 Host 也当成这个站」，两者处置完全不同，
                # 丢掉它等于让这两种配置在对账里长得一模一样。
                catch_all = True
                continue
            if low and low not in names:
                names.append(low)
    targets, ports = _expand_targets(body, upstreams)
    listen, wildcard, defaults = _listen_detail(body)
    size = re.search(r"client_max_body_size\s+([^;]+);", body)
    # 跳转目标域名：别名块（www / blog 跳裸域）自己没有反代目标，归属只能跟随它跳去的那个域名。
    # 只认字面量主机名，见 RE_RETURN_HTTPS 的说明。
    redirect = RE_RETURN_HTTPS.search(body)
    return {
        "serverNames": names,
        "targets": targets,
        "ports": ports,
        "defaultTarget": _default_target(body, upstreams),
        "redirectTarget": redirect.group(1).lower() if redirect else "",
        "listenPorts": listen,
        "wildcardPorts": wildcard,
        "defaultPorts": defaults,
        "catchAll": catch_all,
        "hostReflect": bool(RE_HOST_REFLECT.search(body)),
        "ssl": bool(re.search(r"listen\s+443\s+ssl|ssl_certificate\s", body)),
        "websocket": bool(re.search(r"proxy_set_header\s+Upgrade", body)),
        "httpsRedirect": bool(re.search(r"return\s+30[123]\s+https", body)),
        "bodySize": size.group(1) if size else None,
    }


def parse_blocks(content: str) -> list[dict]:
    """按 server 块解析一份配置；每个块给出自己的域名、反代目标、监听口与 SSL/跳转形态。

    analyze_config 是它的文件级聚合，两处不各写一份解析：站点列表、域名映射、链路对账
    过去都读同一份「整文件当一个块」的近似，于是同一个文件里 80 跳转块与 443 主块
    共用一份端口账，域名归属只能靠猜。
    只取文件里最浅一层的 server 块（Debian 的站点文件是裸 server 块，
    少数把整份 http{} 塞进 conf.d 的写法也照样能解析），upstream 取同一层。
    """
    body = _strip_comments(content or "")
    servers = _keyword_blocks(body, "server")
    if not servers:
        return []
    depth = min(d for d, _a, _b in servers)
    upstreams: dict[str, list[tuple[str, int | None]]] = {}
    for d, args, inner in _keyword_blocks(body, "upstream"):
        if d != depth or not re.fullmatch(r"[\w.-]+", args):
            continue
        pairs = [(s.group(1).strip(), int(s.group(2)) if s.group(2) else None)
                 for s in RE_UP_TARGET.finditer(inner)]
        if pairs:
            upstreams[args] = pairs
    return [_analyze_block(b, upstreams) for d, _a, b in servers if d == depth]


def analyze_config(content: str) -> dict:
    """文件级聚合（字段口径与历史一致）：解析本身只有一处，见 parse_blocks。"""
    blocks = parse_blocks(content)
    names: list[str] = []
    ports: list[int] = []
    listen: list[int] = []
    targets: list[dict] = []
    size = None
    for b in blocks:
        for n in b["serverNames"]:
            if n not in names:
                names.append(n)
        for p in b["ports"]:
            if p not in ports:
                ports.append(p)
        for p in b["listenPorts"]:
            if p not in listen:
                listen.append(p)
        targets.extend(b["targets"])
        if size is None:
            size = b["bodySize"]
    return {
        "serverNames": names,
        "ssl": any(b["ssl"] for b in blocks),
        "websocket": any(b["websocket"] for b in blocks),
        "bodySize": size,
        "httpsRedirect": any(b["httpsRedirect"] for b in blocks),
        "proxyPorts": ports,
        "listenPorts": sorted(listen),
        "proxyTargets": targets,
    }


def pick_default_servers(rows: list[tuple[str, str]]) -> dict:
    """按 nginx 的加载顺序算出「每个端口上的默认 server 是谁」。

    默认档就是 Host 没匹配上任何 server_name 时接住请求的那个块，规则只有两条：
    ① 有块显式写了 `default_server` → 第一个那样的块；② 一个都没写 → **加载顺序里第一个**
    听这个口的块。所以这道题只吃顺序，顺序取自 `nginx -T`（见 loaded_conf_paths）。

    这件事必须显出来，因为它不是「一个域名的事」：Debian 上 conf.d 先于 sites-enabled 加载，
    于是给新站写一份 `conf.d/panel-xxx.conf` 就把全站未知 Host 的归属换到了这个新站身上，
    改的是本站的文件，动的是所有人的默认档，而且 nginx 一声不响。
    反过来，摘掉一个 `default_server` 标记的人以为自己在删默认档，实际只是把默认档
    让给了加载顺序里的下一个 —— 那下一个若正写着 `return 301 https://$host…`，
    未知 Host 就从此有了出口（见 RE_HOST_REFLECT）。

    只看绑全部地址的 listen（`_listen_detail`）；没写 listen 的 server 块在 nginx 里默认听 80，
    这类块这里算不到 —— 本机的站点配置都会写 listen，宁可漏判也不猜。
    """
    first: dict[int, dict] = {}
    explicit: dict[int, dict] = {}
    for path, content in rows:
        for blk in parse_blocks(content):
            for port in blk["wildcardPorts"]:
                if port in explicit or (port in first and port not in blk["defaultPorts"]):
                    # 已经定了就不换：nginx 对第二个 default_server 只给一句警告，
                    # 隐式默认档也是先到先得。
                    continue
                row = {
                    "port": port,
                    "file": os.path.basename(path),
                    "domains": list(blk["serverNames"]),
                    "catchAll": bool(blk["catchAll"]),
                    "hostReflect": bool(blk["hostReflect"]),
                    "ssl": bool(blk["ssl"]),
                    "targets": [f"{t['host']}:{t['port']}" if t.get("port") is not None
                                else str(t.get("host") or "") for t in blk["targets"]],
                }
                if port in blk["defaultPorts"]:
                    explicit[port] = row
                else:
                    first[port] = row
    ports = []
    for port in sorted(set(first) | set(explicit)):
        row = explicit.get(port) or first[port]
        row["explicit"] = port in explicit
        ports.append(row)
    return {"ok": True, "reason": "", "ports": ports}


async def default_servers() -> dict:
    """默认档对账的取数入口：`nginx -T` 读不到就如实回 ok=False（判不了 ≠ 没有默认档）。"""
    paths = await loaded_conf_paths()
    if paths is None:
        return {"ok": False, "reason": "nginx -T 这一轮读不到，默认 server 判不了", "ports": []}

    def _read() -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        for p in paths:
            with suppress(OSError):
                out.append((p, Path(p).read_text(errors="replace")))
        return out

    return pick_default_servers(await asyncio.to_thread(_read))


def _listen_detail(content: str) -> tuple[list[int], list[int], list[int]]:
    """这个 server 块的 (听着哪些端口, 其中绑全部地址的, 其中显式写了 default_server 的)。

    `listen` 的写法五花八门（`80`、`443 ssl http2`、`[::]:443`、`listen 80 default_server`、
    `listen 127.0.0.1:80`），取每行第一个 token 再按最后一个冒号切端口，与 exposure_service.parse_ss
    同一套取数法；解析不出数字的那行直接跳过，不猜。
    后两列只看没写地址的 listen（`80`、`[::]:80`、`*:80`）：要回答的是
    「外网随便一个 Host 打进来落到哪个 server」，那只可能发生在绑全部地址的监听口上，
    把 `listen 127.0.0.1:80 default_server` 算成默认档会判错（它只对回环生效）。
    """
    ports: list[int] = []
    wildcard: list[int] = []
    defaults: list[int] = []
    for m in RE_LISTEN_LINE.finditer(content):
        head = m.group(1).strip().split()
        if not head:
            continue
        first = head[0]
        text = first.rpartition(":")[2]
        if not text.isdigit() or not 1 <= int(text) <= 65535:
            continue
        port = int(text)
        if port not in ports:
            ports.append(port)
        if first.isdigit() or first.startswith("[::]:") or first.startswith("*:"):
            if port not in wildcard:
                wildcard.append(port)
            if "default_server" in head[1:] and port not in defaults:
                defaults.append(port)
    return sorted(ports), sorted(wildcard), sorted(defaults)


def config_dirs_present() -> bool:
    """nginx 装没装：只看配置目录是否存在（不存在就别把空列表伪装成「没有站点」）。"""
    return any(Path(d).is_dir() for d in config.NGINX_CONF_DIRS)


def loaded_configs() -> list[dict]:
    """nginx 实际会加载的每份配置与它的解析结果。

    站点列表、域名→端口映射和站点链路对账都必须读这一份：两处各扫一遍目录，
    迟早会在「sites-available 里没启用的草稿算不算一个站点」上分叉。
    """
    out: list[dict] = []
    for p, f in _iter_conf_files():
        try:
            c = Path(p).read_text(errors="replace")
        except OSError:
            continue
        out.append({"file": p, "name": f, "analysis": analyze_config(c), "blocks": parse_blocks(c)})
    return out


def _enabled_targets(available_dir: str):
    """sites-available 里**已启用**的文件 realpath 集合；判不出来时返回 None（=不过滤）。

    没有 sites-enabled 目录说明本机不是 Debian 那套双目录布局，此时没有可靠判据，
    把"查不了"当成"没生效"会让真正在服务的站点从列表里消失——那比多列一个文件严重得多。
    """
    enabled = os.path.join(os.path.dirname(available_dir.rstrip("/")), "sites-enabled")
    if not os.path.isdir(enabled):
        return None
    try:
        names = os.listdir(enabled)
    except OSError:
        return None
    out = set()
    for n in names:
        with suppress(OSError):
            out.add(os.path.realpath(f"{enabled}/{n}"))
    return out


def _iter_conf_files():
    """列出 nginx 实际会加载的配置文件（不是"这些目录里躺着的所有文件"）。

    Debian 的 nginx.conf 只 include `conf.d/*.conf` 与 `sites-enabled/*`，所以：
    - conf.d 里不以 .conf 结尾的文件（手工备份、README）不在那个 glob 里；
    - sites-available 只是存放处，一份配置只有被 sites-enabled 的符号链接指到才生效
      （链接名可以跟文件名不同，所以比 realpath）。

    之前按目录全量扫，把没启用的草稿也算成一个站点。本模块对这件事早有结论——
    "拿 sites-available 里那些没启用的文件算会误报"（见 loaded_conf_paths 上方注释），
    只是当时只把结论用在了写入路径上，读的一侧还是旧的。后果不只是列表多一行：
    find_app_configs 会把"这个域名的配置"解析到那份不生效的文件上，用户在应用详情里
    改它、nginx -t 通过、reload 成功，线上却一个字都没变，而 assert_app_conf 又按同一份
    匹配结果放行——编辑就此指到幽灵文件上。
    """
    for d in config.NGINX_CONF_DIRS:
        base = os.path.basename(d.rstrip("/"))
        enabled = _enabled_targets(d) if base == "sites-available" else None
        try:
            for f in os.listdir(d):
                # 原子写留下的 `.面板.conf.<pid>.tmp` 正常不存在（写完就 rename），
                # 只有进程被杀在中间才会残留；它不是 nginx 会 include 的文件，
                # 也不该出现在站点列表里。
                if f.endswith(".tmp"):
                    continue
                if base == "conf.d" and not f.endswith(".conf"):
                    continue
                if (enabled is not None
                        and base == "sites-available"
                        and os.path.realpath(f"{d}/{f}") not in enabled):
                    continue
                yield f"{d}/{f}", f
        except OSError:
            continue


def find_app_configs(port, domain) -> list:
    out = []
    low = str(domain or "").strip().lower()
    for p, f in _iter_conf_files():
        try:
            c = Path(p).read_text(errors="replace")
        except OSError:
            continue
        a = analyze_config(c)
        if (low and low in a["serverNames"]) or (port is not None and port in a["proxyPorts"]):
            out.append({"file": p, "name": f, "content": c, "analysis": a})
    return out


def _scan_domains_by_port() -> dict:
    mapping: dict[int, list[str]] = {}
    for p, _f in _iter_conf_files():
        try:
            c = Path(p).read_text(errors="replace")
        except OSError:
            continue
        a = analyze_config(c)
        for pt in a["proxyPorts"]:
            arr = mapping.setdefault(pt, [])
            for n in a["serverNames"]:
                if n not in arr:
                    arr.append(n)
    return {k: v for k, v in mapping.items() if v}


# 应用列表每次刷新都会读端口→域名映射，而它来自全量 nginx 配置扫描（十几个文件 + 正则）。
# 配置只在面板的几个写路径里变化，所以做 TTL 缓存 + 写完即失效：
# 失效点漏掉时 10 秒也会自愈，用户不会看到永久过期的域名。
_DOMAIN_TTL = 10.0
_domain_cache: tuple[float, dict] | None = None
_domain_lock = threading.Lock()


def invalidate_domain_cache() -> None:
    global _domain_cache, _loaded_cache
    with _domain_lock:
        _domain_cache = None
    # "nginx 到底加载了哪些文件"也是同一批事实：面板刚写完/删掉配置，
    # 15 秒内的旧集合会让下一次域名冲突判断看漏（或看多）刚动过的那个文件，
    # 默认档是谁同样会跟着变。
    with _loaded_lock:
        _loaded_cache = None
    # 站点链路对账读的也是这一批配置（它有自己的 60 秒缓存）。留着不失效就等于
    # 「同一个事实两个新鲜度」：刚套用完域名，站点页与自检还拿旧的一份配置说没事，
    # 而 CLI 那几条纳管命令早就在动手后清它了。
    # 延迟导入：site_audit_service 顶层 import 了本模块，正向导入会成环。
    from . import site_audit_service

    site_audit_service.reset_cache()


def domains_by_port() -> dict:
    global _domain_cache
    now = time.time()
    with _domain_lock:
        if _domain_cache and now - _domain_cache[0] < _DOMAIN_TTL:
            return _domain_cache[1]
    data = _scan_domains_by_port()
    with _domain_lock:
        _domain_cache = (now, data)
    return data


def _safe_conf_path(p: str) -> str:
    """把配置路径收进已配置的 nginx conf 目录，且必须先归一化再判断。

    旧实现只在"文件已存在"时 realpath，并对**未归一化**的原始串做前缀与正则匹配，
    于是 `/etc/nginx/conf.d/../../../../tmp/x.conf` 既以 /etc/nginx/ 开头、
    又能匹配 conf.d 那条前缀规则，守卫全部放行，
    实际写入却被内核解析到 /tmp（实测越界创建成功）。
    新建文件本来就不存在，不能依赖 realpath 的存在性分支，所以统一先
    abspath 归一化 `..`；已存在时再 realpath 一次，防符号链接逃到目录外。
    """
    if not p or not p.startswith("/"):
        raise RuntimeError("只能使用绝对路径")
    real = os.path.abspath(p)
    if os.path.exists(real):
        real = os.path.realpath(real)
    # 允许目录取自 CP_NGINX_CONF_DIRS，避免这里再硬编码一份与 config 不同步的路径
    if not any(real == d or real.startswith(d + "/") for d in config.NGINX_CONF_DIRS):
        raise RuntimeError("仅支持 conf.d / sites-available 下的文件")
    if re.search(r"(\.sw.|~)$", real):
        raise RuntimeError("非法文件名")
    return real


def is_panel_proxy(c: dict) -> bool:
    """这份配置反代的是面板自己的端口，那它就是面板的对外入口，不是任何应用的 vhost。

    应用的 port 只受 `is_port`（1024-65535）约束，填成面板端口照样存得进去，而归属判定
    是按"反代端口"匹配的：于是一个 port=3210 的应用会把 choyeon-panel.conf 认成
    "我的配置"，保存 + reload 一次就把面板的 HTTPS 入口换成指向那个应用的反代，
    而 remove_vhost 只认 panel-*.conf，救不回来。
    """
    return config.PORT in (c.get("analysis", {}).get("proxyPorts") or [])


def app_conf_paths(port, domain) -> set:
    """该应用自己的 nginx 配置文件集合（realpath，便于与 _safe_conf_path 的结果比对）。"""
    out = set()
    for c in find_app_configs(port, domain):
        if is_panel_proxy(c):
            continue
        with suppress(OSError):
            out.add(os.path.realpath(c["file"]))
    return out


def assert_app_conf(path: str, port, domain) -> None:
    """应用详情页的编辑器只能改这个应用自己的 nginx 配置。

    _safe_conf_path 只保证"落在 conf.d / sites-available 里面"，而这两个接口的
    file 参数直接来自请求体：从应用 #1 的页面可以把 #2 的 vhost，甚至把面板自己的
    choyeon-panel.conf（面板的对外反代 + TLS）当成"我的配置"整份改掉，
    nginx -t 通过就立刻 reload——写坏面板那一份，全站再也进不来。
    前端本来就只把 GET 返回的 configs 列表里的文件交给保存接口，所以这里按
    同一份匹配结果（域名或反代端口）收口，不改变正常使用。
    """
    real = _safe_conf_path(path)
    allowed = app_conf_paths(port, domain)
    if real not in allowed:
        raise RuntimeError(
            "只能编辑该应用自己的 nginx 配置（按域名/反代端口匹配到的、且 nginx 实际加载的文件）。"
            "若刚改过域名或端口，请回到本页重新加载配置列表后再编辑；"
            "sites-available 里没有对应 sites-enabled 链接的文件不生效，也不会出现在可编辑列表里。"
        )


async def save_app_config(path: str, content: str) -> dict:
    real = _safe_conf_path(path)
    if not content.strip():
        raise RuntimeError("内容不能为空")
    if len(content) > 200000:
        raise RuntimeError("文件过大")
    async with _nginx_section():
        old = Path(real).read_text(errors="replace") if os.path.exists(real) else None
        _write_conf(real, content)
        check = await run("nginx", ["-t"])
        if check["code"] != 0:
            # 有原文件就恢复；没有原文件（本次是新建）必须删掉，
            # 否则坏配置会留在 conf.d/ 里，之后每次 nginx -t / reload 都连带失败，
            # 面板上其它应用的域名与 SSL 操作也会全部不可用。
            if old is not None:
                _write_conf(real, old)
            else:
                try:
                    os.unlink(real)
                except OSError as e:
                    # 消息里那句「已回滚」此时是假的：坏配置留在了目录里。
                    raise RuntimeError(
                        f"nginx -t 校验失败，且新建的 {real} 删除失败（{type(e).__name__}）："
                        "这份坏配置留在目录里会让之后每次 nginx -t / reload 一起失败，"
                        "请手工删除该文件。\n原始错误：\n" + (check["out"] or "")
                    ) from e
            raise RuntimeError(f"nginx -t 校验失败，已回滚：\n{check['out']}")
        reload = await run("systemctl", ["reload", "nginx"])
        if reload["code"] != 0:
            raise RuntimeError(f"nginx reload 失败：{reload['out']}")
    invalidate_domain_cache()
    return {"ok": True, "file": real}


async def quick_edit_config(path: str, kind: str, value: str | None = None) -> dict:
    # kind 来自请求体，且会拼进审计动作名。旧写法把"其它值"一律当 HTTPS 跳转处理，
    # 于是一个拼错的 kind 也会改配置、审计里出现 f"app:nginx-{任意字符串}"。
    if kind not in ("ws", "body", "redirect"):
        raise RuntimeError("未知的快捷操作类型")
    real = _safe_conf_path(path)
    c = Path(real).read_text(errors="replace")
    if kind == "ws":
        if re.search(r"proxy_set_header\s+Upgrade", c):
            raise RuntimeError("该配置已包含 WebSocket 头")
        m = re.search(r"^([ \t]*)proxy_pass[^;]+;", c, re.M)
        if not m:
            raise RuntimeError("未找到 proxy_pass，无法插入")
        indent = m.group(1)
        upgrade_headers = (
            f"{indent}proxy_set_header Upgrade $http_upgrade;"
            f"\n{indent}proxy_set_header Connection \"upgrade\";"
        )
        c = c.replace(m.group(0), f"{m.group(0)}\n{upgrade_headers}")
    elif kind == "body":
        if not re.fullmatch(r"\d{1,4}[kmg]", value or "", re.I):
            raise RuntimeError("大小格式应如 50m / 1g")
        if re.search(r"client_max_body_size", c):
            c = re.sub(r"client_max_body_size\s+[^;]+;", f"client_max_body_size {value};", c, count=1)
        else:
            m = re.search(r"^([ \t]*)server_name[^;]+;", c, re.M)
            if not m:
                raise RuntimeError("未找到 server_name，无法插入")
            c = c.replace(m.group(0), f"{m.group(0)}\n{m.group(1)}client_max_body_size {value};")
    elif kind == "redirect":
        a = analyze_config(c)
        if not a["ssl"]:
            raise RuntimeError("该配置尚未启用 HTTPS，请先在「域名 / SSL」申请证书")
        if a["httpsRedirect"]:
            raise RuntimeError("已存在 HTTPS 强制跳转")
        if not a["serverNames"]:
            raise RuntimeError("无法确定 server_name")
        name = a["serverNames"][0]
        block = (
            f"server {{\n    listen 80;\n    listen [::]:80;\n    server_name {name};\n"
            "    return 301 https://$host$request_uri;\n}\n\n"
        )
        m = re.search(r"^server\s*\{", c, re.M)
        c = block + c if not m else c[: m.start()] + block + c[m.start() :]
    return await save_app_config(real, c)
