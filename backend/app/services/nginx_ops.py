import os
import re
from pathlib import Path

from .. import config
from ..util import is_domain, run, run_shell

CONF_DIR = "/etc/nginx/conf.d"
TLS_DIR = config.TLS_DIR

RE_SERVER_NAME = re.compile(r"server_name\s+([^;]+);")
RE_PROXY_PORT = re.compile(r"proxy_pass\s+https?://[\w.:-]*?:(\d{2,5})\s*;")
RE_UPSTREAM = re.compile(r"upstream\s+([\w.-]+)\s*\{([^}]*)\}")
RE_UP_SERVER = re.compile(r"server\s+[\w.:-]*?:(\d{2,5})")
RE_PROXY_NAME = re.compile(r"proxy_pass\s+https?://([\w.-]+)\s*;")


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
    return (
        f"# managed by choyeon-panel ({spec['name']}) — do not edit by hand\n"
        f"{redirect}server {{\n"
        f"    listen {'443 ssl' if ssl else '80'};\n"
        f"    listen {'[::]:443 ssl' if ssl else '[::]:80'};\n"
        f"{ssl_lines}    server_name {domain};\n"
        "    client_max_body_size 100m;\n"
        "\n"
        "    location / {\n"
        f"{proxy_headers}\n"
        "    }\n"
        "}\n"
    )


def vhost_path(name: str) -> str:
    return f"{CONF_DIR}/panel-{name}.conf"


async def apply_vhost(spec: dict):
    Path(CONF_DIR).mkdir(parents=True, exist_ok=True)
    path = vhost_path(spec["name"])
    Path(path).write_text(render_vhost(spec))
    check = await run("nginx", ["-t"])
    if check["code"] != 0:
        os.unlink(path)
        raise RuntimeError(f"nginx config test failed, rolled back: {check['out']}")
    reload = await run("systemctl", ["reload", "nginx"])
    if reload["code"] != 0:
        raise RuntimeError(f"nginx reload failed: {reload['out']}")


async def remove_vhost(name: str):
    path = vhost_path(name)
    if os.path.exists(path):
        os.unlink(path)
    check = await run("nginx", ["-t"])
    if check["code"] == 0:
        await run("systemctl", ["reload", "nginx"])


async def issue_cert(domain: str, email: str | None = None) -> str:
    if not is_domain(domain):
        raise RuntimeError(f"invalid domain: {domain}")
    args = ["--nginx", "-d", domain, "--non-interactive", "--redirect", "--agree-tos", "--no-eff-email"]
    extra = ["-m", email] if email else ["--register-unsafely-without-email"]
    args = args[:4] + extra + args[4:]
    r = await run("certbot", args, timeout=300)
    if r["code"] != 0:
        raise RuntimeError(f"certbot failed: {r['out']}")
    return r["out"]


async def renew_certs() -> str:
    r = await run("certbot", ["renew", "--non-interactive"], timeout=600)
    return r["out"]


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
    seen = set()
    uniq_ports = [p for p in proxy_ports if not (p in seen or seen.add(p))]
    body = re.search(r"client_max_body_size\s+([^;]+);", content)
    return {
        "serverNames": names,
        "ssl": bool(re.search(r"listen\s+443\s+ssl|ssl_certificate\s", content)),
        "websocket": bool(re.search(r"proxy_set_header\s+Upgrade", content)),
        "bodySize": body.group(1) if body else None,
        "httpsRedirect": bool(re.search(r"return\s+30[123]\s+https", content)),
        "proxyPorts": uniq_ports,
    }


def _iter_conf_files():
    for d in config.NGINX_CONF_DIRS:
        try:
            for f in os.listdir(d):
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


def domains_by_port() -> dict:
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


def _safe_conf_path(p: str) -> str:
    real = os.path.realpath(p) if os.path.exists(p) else p
    if not real.startswith("/etc/nginx/"):
        raise RuntimeError("只能编辑 /etc/nginx 下的配置文件")
    if not re.match(r"^/etc/nginx/(conf\.d|sites-available)/", real):
        raise RuntimeError("仅支持 conf.d / sites-available 下的文件")
    if re.search(r"(\.sw.|~)$", real):
        raise RuntimeError("非法文件名")
    return real


async def save_app_config(path: str, content: str) -> dict:
    real = _safe_conf_path(path)
    if not content.strip():
        raise RuntimeError("内容不能为空")
    if len(content) > 200000:
        raise RuntimeError("文件过大")
    old = Path(real).read_text(errors="replace") if os.path.exists(real) else None
    Path(real).write_text(content)
    check = await run("nginx", ["-t"])
    if check["code"] != 0:
        if old is not None:
            Path(real).write_text(old)
        raise RuntimeError(f"nginx -t 校验失败，已回滚：\n{check['out']}")
    reload = await run("systemctl", ["reload", "nginx"])
    if reload["code"] != 0:
        raise RuntimeError(f"nginx reload 失败：{reload['out']}")
    return {"ok": True, "file": real}


async def quick_edit_config(path: str, kind: str, value: str | None = None) -> dict:
    real = _safe_conf_path(path)
    c = Path(real).read_text(errors="replace")
    if kind == "ws":
        if re.search(r"proxy_set_header\s+Upgrade", c):
            raise RuntimeError("该配置已包含 WebSocket 头")
        m = re.search(r"^([ \t]*)proxy_pass[^;]+;", c, re.M)
        if not m:
            raise RuntimeError("未找到 proxy_pass，无法插入")
        indent = m.group(1)
        c = c.replace(
            m.group(0),
            f"{m.group(0)}\n{indent}proxy_set_header Upgrade $http_upgrade;\n{indent}proxy_set_header Connection \"upgrade\";",
        )
    elif kind == "body":
        if not re.match(r"^\d{1,4}[km]?$", value or "", re.I):
            raise RuntimeError("大小格式应如 50m / 1g")
        if re.search(r"client_max_body_size", c):
            c = re.sub(r"client_max_body_size\s+[^;]+;", f"client_max_body_size {value};", c, count=1)
        else:
            m = re.search(r"^([ \t]*)server_name[^;]+;", c, re.M)
            if not m:
                raise RuntimeError("未找到 server_name，无法插入")
            c = c.replace(m.group(0), f"{m.group(0)}\n{m.group(1)}client_max_body_size {value};")
    else:
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
