import asyncio
import os
import re
import threading
import time
import weakref
from contextlib import suppress
from pathlib import Path

from .. import config
from ..util import is_domain, is_email, run

CONF_DIR = config.NGINX_CONF_DIRS[0]
TLS_DIR = config.TLS_DIR

RE_SERVER_NAME = re.compile(r"server_name\s+([^;]+);")
RE_PROXY_PORT = re.compile(r"proxy_pass\s+https?://[\w.:-]*?:(\d{2,5})\s*;")
RE_UPSTREAM = re.compile(r"upstream\s+([\w.-]+)\s*\{([^}]*)\}")
RE_UP_SERVER = re.compile(r"server\s+[\w.:-]*?:(\d{2,5})")
RE_PROXY_NAME = re.compile(r"proxy_pass\s+https?://([\w.-]+)\s*;")

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


def _has_cert(domain: str) -> bool:
    return os.path.exists(f"{TLS_DIR}/{domain}/fullchain.pem")


def render_vhost(spec: dict) -> str:
    domain = spec["domain"]
    if not is_domain(domain):
        raise RuntimeError(f"invalid domain: {domain}")
    port = spec["port"]
    ssl = _has_cert(domain)
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
        f"    ssl_certificate {TLS_DIR}/{domain}/fullchain.pem;\n"
        f"    ssl_certificate_key {TLS_DIR}/{domain}/privkey.pem;\n"
        "    include /etc/letsencrypt/options-ssl-nginx.conf;\n"
        "    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;\n"
    ) if ssl else ""
    redirect = (
        f"server {{\n    listen 80;\n    listen [::]:80;\n    server_name {domain};\n"
        "    return 301 https://$host$request_uri;\n}\n\n"
    ) if ssl else ""
    security_headers = (
        "        add_header X-Content-Type-Options nosniff always;\n"
        "        add_header Referrer-Policy strict-origin-when-cross-origin always;\n"
        + ("        add_header Strict-Transport-Security \"max-age=31536000\" always;\n" if ssl else "")
    )
    return (
        f"# managed by choyeon-panel ({spec['name']}) — do not edit by hand\n"
        f"{redirect}server {{\n"
        f"    listen {'443 ssl' if ssl else '80'};\n"
        f"    listen {'[::]:443 ssl' if ssl else '[::]:80'};\n"
        f"    http2 on;\n"
        f"{ssl_lines}    server_name {domain};\n"
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
# nginx -T 实测 42ms，缓存 15 秒并写完即失效；拿不到时返回 None，让调用方
# 如实说"无法确认"，而不是把"查不了"当成"没冲突"照常写入。
_LOADED_TTL = 15.0
_loaded_cache: tuple[float, set | None] | None = None
_loaded_lock = threading.Lock()


async def loaded_conf_paths() -> set | None:
    global _loaded_cache
    now = time.time()
    with _loaded_lock:
        if _loaded_cache and now - _loaded_cache[0] < _LOADED_TTL:
            return _loaded_cache[1]
    r = await run("nginx", ["-T"], timeout=30)
    if r["code"] != 0:
        return None
    paths = {
        os.path.realpath(m)
        for m in re.findall(r"^# configuration file (.+?):", r["out"] or "", re.M)
    }
    data = paths or None
    with _loaded_lock:
        _loaded_cache = (now, data)
    return data


async def domain_owner(domain: str, target: str) -> str | None:
    """返回真正加载了该 domain、又不是本次要写的那个文件的配置路径。"""
    loaded = await loaded_conf_paths()
    if loaded is None:
        return None
    real_target = os.path.realpath(target)
    for p in sorted(loaded):
        if p == real_target:
            continue
        with suppress(OSError):
            if domain in analyze_config(Path(p).read_text(errors="replace"))["serverNames"]:
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
    content = render_vhost(spec)
    Path(CONF_DIR).mkdir(parents=True, exist_ok=True)
    path = vhost_path(spec["name"])
    async with _nginx_section():
        owner = await domain_owner(domain, path)
        if owner:
            raise DomainOwnedElsewhere(
                f"域名 {domain} 已由 {owner} 定义，面板没有生成第二份 server 块："
                "同一域名存在两份配置时 nginx 只按加载顺序认第一份（conf.d 先于 sites-enabled），"
                f"强行套用会让 {Path(owner).name} 里的手工配置静默失效。"
                f"要让面板托管这个域名，请删除那份文件，或把它改名为 {Path(path).name} 放到 {CONF_DIR}。"
            )
        old = Path(path).read_text(errors="replace") if os.path.exists(path) else None
        _write_conf(path, content)
        check = await run("nginx", ["-t"])
        if check["code"] != 0:
            # 回滚到原文件（新建的则删除），并恢复 nginx 配置状态
            if old is not None:
                _write_conf(path, old)
            else:
                os.unlink(path)
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
    r = await run("certbot", args, timeout=300)
    if r["code"] != 0:
        raise RuntimeError(f"certbot failed: {r['out']}")
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
    r = await run("certbot", ["renew", "--non-interactive"], timeout=600)
    if r["code"] != 0:
        # util.run 把 stderr 并进了 stdout，返回只有 {code, out}：这里再取 r["err"]
        # 就等于"报错正好发生在 out 为空的时候"必然 KeyError，把一条本该是 400 的
        # 失败变成 500（out 为空恰恰是 certbot 被 kill、磁盘写满这类情况）。
        raise RuntimeError(f"证书续期失败 (exit {r['code']}): {r['out'] or '(无输出)'}")
    after = await _expiries()
    renewed = sorted(n for n, e in after.items() if e and before.get(n) and e != before.get(n))
    reloaded = False
    if renewed:
        reloaded = (await run("systemctl", ["reload", "nginx"]))["code"] == 0
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


def analyze_config(content: str) -> dict:
    names: list[str] = []
    for m in RE_SERVER_NAME.finditer(content):
        for n in m.group(1).strip().split():
            if n and n != "_" and n not in names:
                names.append(n)
    proxy_ports = [int(m.group(1)) for m in RE_PROXY_PORT.finditer(content)]
    upstreams: dict[str, list[int]] = {}
    for m in RE_UPSTREAM.finditer(content):
        ports = [int(x.group(1)) for x in RE_UP_SERVER.finditer(m.group(2))]
        if ports:
            upstreams[m.group(1)] = ports
    for m in RE_PROXY_NAME.finditer(content):
        ups = upstreams.get(m.group(1))
        if ups:
            proxy_ports.extend(ups)
    uniq_ports: list[int] = []
    seen: set[int] = set()
    for p in proxy_ports:
        if p not in seen:
            seen.add(p)
            uniq_ports.append(p)
    body = re.search(r"client_max_body_size\s+([^;]+);", content)
    return {
        "serverNames": names,
        "ssl": bool(re.search(r"listen\s+443\s+ssl|ssl_certificate\s", content)),
        "websocket": bool(re.search(r"proxy_set_header\s+Upgrade", content)),
        "bodySize": body.group(1) if body else None,
        "httpsRedirect": bool(re.search(r"return\s+30[123]\s+https", content)),
        "proxyPorts": uniq_ports,
    }


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
    for p, f in _iter_conf_files():
        try:
            c = Path(p).read_text(errors="replace")
        except OSError:
            continue
        a = analyze_config(c)
        if (domain and domain in a["serverNames"]) or (port is not None and port in a["proxyPorts"]):
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
    # 15 秒内的旧集合会让下一次域名冲突判断看漏（或看多）刚动过的那个文件。
    with _loaded_lock:
        _loaded_cache = None


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


def app_conf_paths(port, domain) -> set:
    """该应用自己的 nginx 配置文件集合（realpath，便于与 _safe_conf_path 的结果比对）。"""
    out = set()
    for c in find_app_configs(port, domain):
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
                with suppress(OSError):
                    os.unlink(real)
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
        if not re.match(r"^\d{1,4}[kmg]$", value or "", re.I):
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
