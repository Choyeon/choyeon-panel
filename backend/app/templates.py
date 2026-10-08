"""一键部署模板（对标 1Panel 应用商店的"预设"思路，但只在本机原生部署）。

模板只提供默认值，最终仍走 apps_service.create_app 的同一套校验，
因此网页端与 CLI 用同一份数据，不会出现两边行为不一致。
"""

from __future__ import annotations

# type 受 apps 表 CHECK 约束限制，只能是 node / python
# env 条目结构必须与 apps_service.write_env_file 一致：{"k": ..., "v": ...}。
# 用 {"key","value"} 的话 write_env_file 会静默过滤掉整条，
# 部署出来的应用拿不到 PORT/NODE_ENV，网页上却显示"部署成功"（实测 .panel.env 为空文件）。
TEMPLATES: list[dict] = [
    {
        "key": "node-service",
        "name": "Node 服务",
        "desc": "npm install 后执行 npm start，适合 Express / Koa / Nest 等常驻服务",
        "type": "node",
        "install_cmd": "npm install --omit=dev",
        "start_cmd": "npm start",
        "port": 3000,
        "env": [{"k": "PORT", "v": "3000"}, {"k": "NODE_ENV", "v": "production"}],
    },
    {
        "key": "node-next",
        "name": "Next.js 站点",
        "desc": "先 npm run build 再 npm start，适合 Next.js / Nuxt 等需要构建的前端框架",
        "type": "node",
        "install_cmd": "npm install && npm run build",
        "start_cmd": "npm start",
        "port": 3000,
        "env": [{"k": "PORT", "v": "3000"}, {"k": "NODE_ENV", "v": "production"}],
    },
    {
        "key": "python-fastapi",
        "name": "Python FastAPI",
        "desc": "建 venv 装 requirements.txt，用 uvicorn 拉起 main:app",
        "type": "python",
        "install_cmd": "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt",
        "start_cmd": ".venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000",
        "port": 8000,
        "env": [],
    },
    {
        "key": "static-site",
        "name": "静态站点",
        "desc": "用 Python 自带 http.server 托管纯静态目录，零依赖",
        "type": "python",
        "install_cmd": "",
        "start_cmd": "python3 -m http.server 8000 --bind 127.0.0.1",
        "port": 8000,
        "env": [],
    },
]

_BY_KEY = {t["key"]: t for t in TEMPLATES}


def list_templates() -> list[dict]:
    return [dict(t) for t in TEMPLATES]


def get_template(key: str) -> dict | None:
    """按 key 取模板；未知 key 返回 None，由调用方决定报错方式。"""
    t = _BY_KEY.get((key or "").strip())
    return dict(t) if t else None


def apply_template(i: dict) -> dict:
    """把模板默认值并入创建参数：显式传入的字段永远优先于模板。"""
    key = (i.get("template") or "").strip()
    if not key:
        return i
    tpl = get_template(key)
    if not tpl:
        raise RuntimeError(f"未知模板: {key}（可选：{', '.join(_BY_KEY)}）")

    merged = dict(i)
    merged.pop("template", None)
    for field in ("type", "install_cmd", "start_cmd", "port"):
        if merged.get(field) in (None, ""):
            merged[field] = tpl[field]
    if not merged.get("env"):
        merged["env"] = tpl["env"]
    return merged
