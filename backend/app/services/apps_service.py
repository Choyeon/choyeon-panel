import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path

from .. import aio_db
from .. import config
from .. import database as dbm
from ..util import (
    clean_abs_path,
    is_branch,
    is_domain,
    is_git_url,
    is_name,
    is_port,
    redact_creds,
    run,
    run_shell_async,
    sub_port,
    unit_exec_arg_escape,
)
from . import nginx_ops, systemd_ops
from .systemd_ops import is_active, service_action

APP_ROOT = config.APP_ROOT
UNIT_DIR = config.UNIT_DIR

_DEPLOY_TASKS: set = set()

TEMPLATE_TOKENS = ("{{name}}", "{{path}}", "{{start_cmd}}", "{{port}}")

_VERB_CN = {"start": "启动", "stop": "停止", "restart": "重启"}


def unit_name(app: dict) -> str:
    return app.get("unit_override") or f"panel-{app['name']}.service"


def render_unit_from(tpl: str, app: dict) -> str:
    return (
        tpl.replace("{{name}}", app["name"])
        .replace("{{path}}", app["path"])
        # 同 render_unit：自定义模板里 {{start_cmd}} 展开出来的行一样会被 systemd
        # 处理，`%X`/`${VAR}` 的展开风险与默认模板没有区别，所以这里同样转义。
        .replace("{{start_cmd}}", json.dumps(unit_exec_arg_escape(app["start_cmd"])))
        .replace("{{port}}", str(app["port"] if app["port"] is not None else ""))
    )


def render_unit(app: dict) -> str:
    if app.get("unit_template"):
        return render_unit_from(app["unit_template"], app)
    return (
        f"[Unit]\n"
        f"Description=choyeon-panel app {app['name']}\n"
        f"After=network-online.target\n"
        f"Wants=network-online.target\n"
        f"\n"
        f"[Service]\n"
        f"Type=simple\n"
        f"WorkingDirectory={app['path']}\n"
        f"EnvironmentFile=-{app['path']}/.panel.env\n"
        # 实测：ExecStart 整行（引号内也一样）会被 systemd 展开 `%X` 与 `${VAR}`。
        # 启动命令里出现 `%b` 时，`/bin/bash -lc "printf tag=%b"` 真的会执行成
        # `printf tag=/bin/sh`（unit 文本原样落进 /etc/systemd/system 起服务测出）；
        # 出现 `${DB_PASSWORD}` 时 systemd 会先拿 unit 自己的环境（含本 app 的
        # .panel.env）替换掉它，替换结果直接进 argv——/proc/<pid>/cmdline 是全局
        # 可读的，等于把密钥暴露给机器上任何用户，而 `${PORT:-3000}` 这类写法
        # 更会被吞成空串，进程 rc=0，面板只显示"启动成功"。
        # 所以这里用 unit_exec_arg_escape 把 `$`、`%` 各翻倍，展开交回 bash。
        # 路径不用处理：clean_abs_path 的白名单不含 `%` 和 `$`。
        f"ExecStart=/bin/bash -lc {json.dumps(unit_exec_arg_escape(app['start_cmd']))}\n"
        f"Restart=always\n"
        f"RestartSec=3\n"
        f"User=root\n"
        f"\n"
        f"[Install]\n"
        f"WantedBy=multi-user.target\n"
    )


def _env_escape(v) -> str:
    r"""把环境变量值写成 systemd EnvironmentFile 的双引号内容。

    EnvironmentFile 里双引号内的 `\` 是转义符，所以必须先转义 `\` 再转义 `"`。
    旧写法只转义了 `"`，实测后果比"值显示不对"严重得多：
      值 `abc\\`（口令、Windows 路径结尾极常见）写成 `TRAILBS="abc\"`，
      结尾的 `\` 把右引号吃掉，字符串不闭合，systemd 一路读到下一行的引号才停——
      journal 里 TRAILBS 的实际值变成 `abc"\nDOUBLEBS=a\b"`，
      而下一行的 DOUBLEBS 直接被吞掉、根本没有被定义。
    也就是说前一个变量的值会吃掉后一个变量：应用拿到错乱的配置，
    而部署日志显示成功，排查时完全看不出是环境变量出了问题。
    `\n`/`\r` 换成空格：换行本身也会截断这一行。
    `$` 不需要处理——已实测 systemd 不做变量展开（`cost $HOME` 原样传入）。
    """
    s = str(v if v is not None else "")
    s = s.replace(chr(92), chr(92) * 2)
    s = s.replace(chr(34), chr(92) + chr(34))
    return s.replace(chr(10), " ").replace(chr(13), " ")


def write_env_file(app: dict):
    try:
        vars_ = json.loads(app.get("env") or "[]")
    except (ValueError, TypeError):
        vars_ = []
    body = "\n".join(
        f'{x["k"]}="{_env_escape(x.get("v", ""))}"'
        for x in vars_
        if isinstance(x, dict) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", x.get("k") or "")
    )
    target = Path(app["path"]) / ".panel.env"
    if not target.parent.is_dir():
        # 应用还没部署过（目录都不存在）时不要顺手把目录建出来：git clone 拒绝往
        # "已存在且非空"的目录里写，而这里正是那份非空内容，用户会看到"部署失败"却
        # 完全不知道和刚才保存配置有关。目录会由部署自己创建，届时再写这个文件。
        return
    try:
        target.write_text(body + "\n", encoding="utf-8")
        os.chmod(target, 0o600)  # 环境变量常含密钥，禁止同机其他用户读取
    except OSError as e:
        raise RuntimeError(f"写入 .panel.env 失败：{e}") from e


async def ensure_unit(app: dict):
    if app.get("unit_override"):
        return
    Path(f"{UNIT_DIR}/{unit_name(app)}").write_text(render_unit(app))
    r = await run("systemctl", ["daemon-reload"])
    if r["code"] != 0:
        raise RuntimeError(r["out"])


async def remove_unit(app: dict):
    if app.get("unit_override"):
        return
    await run("systemctl", ["disable", "--now", unit_name(app)], timeout=30)
    p = f"{UNIT_DIR}/{unit_name(app)}"
    if os.path.exists(p):
        os.unlink(p)
    await run("systemctl", ["daemon-reload"])


_ss_cache: tuple[float, list[str]] | None = None


async def _listen_lines(force: bool = False) -> list[str]:
    """读取监听端口表（5 秒内复用），异步执行避免阻塞事件循环。"""
    global _ss_cache
    if not force and _ss_cache and time.time() - _ss_cache[0] < 5:
        return _ss_cache[1]
    out = await run_shell_async("ss -tlnp", timeout=4)
    _ss_cache = (time.time(), out.split("\n"))
    return _ss_cache[1]


async def detect_port(unit: str, force: bool = False):
    r = await run("systemctl", ["show", unit, "-p", "MainPID", "--value"], timeout=5)
    try:
        main = int(r["out"].strip())
    except ValueError:
        main = 0
    if main <= 0:
        return None
    return await _port_for_pid(main, await _listen_lines(force))


def _pid_tree(main: int) -> set:
    pids = {main}
    queue = [main]
    while queue:
        p = queue.pop(0)
        try:
            for t in os.listdir(f"/proc/{p}/task"):
                try:
                    kids = Path(f"/proc/{p}/task/{t}/children").read_text().split()
                except OSError:
                    continue
                for k in kids:
                    try:
                        kid = int(k)
                    except ValueError:
                        continue
                    if kid and kid not in pids:
                        pids.add(kid)
                        queue.append(kid)
        except OSError:
            pass
    return pids


async def _port_for_pid(main: int, lines: list) -> int | None:
    pids = await asyncio.to_thread(_pid_tree, main)
    ports = set()
    for line in lines:
        pm = re.search(r"pid=(\d+)", line)
        if not pm or int(pm.group(1)) not in pids:
            continue
        parts = line.strip().split()
        addr = parts[3] if len(parts) > 3 else ""
        port = re.search(r":(\d+)$", addr)
        if port:
            ports.add(int(port.group(1)))
    return min(ports) if ports else None


async def list_apps() -> list:
    rows = await aio_db.query("SELECT * FROM apps ORDER BY name")
    units = [unit_name(a) for a in rows]
    # 一次 systemctl show 取代每 app 两次子进程；一次 deployments 查询取代 N 次
    states, dom_map, deploying = await asyncio.gather(
        systemd_ops.unit_states(units),
        asyncio.to_thread(nginx_ops.domains_by_port),
        aio_db.query("SELECT app_id FROM deployments WHERE status='running'"),
    )
    busy = {r["app_id"] for r in deploying}

    async def _one(a: dict):
        st = states.get(unit_name(a)) or {"active": False, "mainPid": 0}
        running = st["active"]
        detected = None
        if a["port"] is None and running and st["mainPid"] > 0:
            detected = await _port_for_pid(st["mainPid"], await _listen_lines())
        port = a["port"] if a["port"] is not None else detected
        domains = [a["domain"]] if a["domain"] else (dom_map.get(port, []) if port is not None else [])
        out = {k: v for k, v in a.items() if k != "env"}
        out.update(
            running=running,
            unit=unit_name(a),
            port=port,
            portAuto=a["port"] is None and detected is not None,
            domains=domains,
            deploying=a["id"] in busy,
        )
        return out

    return list(await asyncio.gather(*(_one(a) for a in rows)))


def get_app(app_id: int):
    return dbm.query_one("SELECT * FROM apps WHERE id=?", (app_id,))


def _app_path(i: dict) -> str:
    """取应用安装目录：先补默认值，再统一校验 + 规范化。

    规范化必须在写库之前做，否则 `/root/www/a/../../www/a` 与 `/root/www/a`
    是两个不同的字符串，却指向同一个目录——重复检查按字符串比较就会放行，
    两个应用共用一个工作目录，后部署的那个覆盖前一个的 unit 与 .panel.env。
    """
    return clean_abs_path(i.get("path") or f"{APP_ROOT}/{i['name']}", "安装目录")


def _stored_path_key(raw) -> str:
    """把库里已存的 path 折成一个可比较的键。

    历史数据里可能有旧校验放行过的 `..` 写法，clean_abs_path 会直接拒绝它们；
    如果因此跳过比对，等于"老数据写法的同一目录"继续躲得过重复检查——
    新应用照样能和它共用工作目录。这里对老数据放宽到只做 normpath：
    只用来比较，不用来拼命令或写盘，所以把 .. 折掉比忽略更安全。
    """
    if not isinstance(raw, str) or not raw:
        return ""
    try:
        return clean_abs_path(raw, "路径")
    except RuntimeError:
        return os.path.normpath(raw) if raw.startswith("/") else ""


def _validate(i: dict, ignore_id: int = -1):
    if not is_name(i.get("name") or ""):
        raise RuntimeError("名称只能包含小写字母、数字、连字符，且以字母开头")
    if i.get("type") not in ("node", "python"):
        raise RuntimeError("类型必须是 node 或 python")
    if i.get("repo_url") and not is_git_url(i["repo_url"]):
        raise RuntimeError("仓库地址不合法")
    if i.get("branch") and not is_branch(i["branch"]):
        raise RuntimeError("分支名不合法")
    if i.get("port") is not None and not is_port(i["port"]):
        raise RuntimeError("端口必须在 1024-65535")
    if i.get("domain") and not is_domain(i["domain"]):
        raise RuntimeError("域名不合法")
    if not i.get("start_cmd") or len(i["start_cmd"]) > 500:
        raise RuntimeError("启动命令必填且不超过 500 字符")
    if i.get("path"):
        # 拒绝 `..` 并要求绝对路径；见 clean_abs_path 的说明
        clean_abs_path(i["path"], "安装目录")
        _check_not_protected(i["path"])
    if i.get("unit_template"):
        if len(i["unit_template"]) > 4000:
            raise RuntimeError("unit 模板过长")
        if re.search(r"\$\{|\$\(|`", i["unit_template"]):
            raise RuntimeError("unit 模板不允许 shell 替换语法")
    if i.get("unit_override") and not re.match(r"^[A-Za-z0-9@:._-]{1,64}\.service$", i["unit_override"]):
        raise RuntimeError("unit 名不合法")
    if i.get("db_names"):
        for d in i["db_names"].split(","):
            if d and not re.match(r"^[a-z0-9_]{1,63}$", d):
                raise RuntimeError(f"关联数据库名不合法: {d}")
    path = _app_path(i)
    dup = dbm.query_one(
        "SELECT id FROM apps WHERE id IS NOT ?"
        " AND (name=? OR path=? OR (domain IS NOT NULL AND domain=? AND domain!=''))",
        (ignore_id, i["name"], path, i.get("domain") or ""),
    )
    if dup:
        raise RuntimeError("名称/路径/域名已被其他应用占用")
    # 已经存在的记录里可能有本次改动之前写入的、写法不同的同一路径（带尾斜杠、带 ..），
    # 这里按规范化结果再比一次，避免"两条不同字符串指向同一目录"混过检查。
    for row in dbm.query("SELECT id, path FROM apps WHERE id IS NOT ?", (ignore_id,)):
        if _stored_path_key(row["path"]) == path:
            raise RuntimeError(f"安装目录与应用 #{row['id']} 相同")
    if i.get("port"):
        busy = dbm.query_one("SELECT id FROM apps WHERE port=? AND id IS NOT ?", (i["port"], ignore_id))
        if busy:
            raise RuntimeError(f"端口 {i['port']} 已被应用 #{busy['id']} 使用")


def _ensure_no_deployment(app_id: int, action: str):
    """删除之外，回滚与改配置同样不能和进行中的部署并发。

    部署任务在创建时就拿到了 app 字典的快照，回滚却在同一目录上跑
    `git reset --hard`：两边会互相把 HEAD 拧到不同 commit，
    部署结束时写回 unit 的还是旧快照，现场无法解释。
    旧代码只给 delete_app 加了这道守卫。
    """
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError(f"部署进行中，不能{action}")


def create_app(i: dict) -> dict:
    _validate(i)
    path = _app_path(i)
    parent_id = None
    if i.get("parent_id"):
        parent_id = int(i["parent_id"])
        if not get_app(parent_id):
            raise RuntimeError("组合项目的父应用不存在")
    res = dbm.execute(
        "INSERT INTO apps(name,type,repo_url,branch,path,port,domain,install_cmd,"
        "start_cmd,env,unit_override,db_names,unit_template,parent_id,component_role)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            i["name"],
            i["type"],
            i.get("repo_url") or "",
            i.get("branch") or "main",
            path,
            i.get("port"),
            i.get("domain") or None,
            i.get("install_cmd") or None,
            i["start_cmd"],
            json.dumps(i.get("env") or []),
            i.get("unit_override") or None,
            i.get("db_names") or None,
            i.get("unit_template") or None,
            parent_id,
            str(i.get("component_role") or "")[:16],
        ),
    )
    return get_app(res)


def update_app(app_id: int, i: dict) -> dict:
    cur = get_app(app_id)
    if not cur:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "修改配置")
    merged = dict(cur)
    merged.update({k: v for k, v in i.items() if k != "env"})
    _validate(merged, app_id)
    vars_ = i["env"] if "env" in i and i["env"] is not None else json.loads(cur["env"] or "[]")
    # 改端口时同步 start_cmd / env 里写死的旧端口，与建号期的模板替换同款。
    # 不这样做的话面板记录的是新端口、进程仍监听旧端口，套上 nginx 就是 502，
    # 而 UI 上一切正常。显式改了 start_cmd 或 env 时以用户输入为准，不再猜。
    old_port, new_port = cur.get("port"), merged.get("port")
    if None not in (old_port, new_port) and str(old_port) != str(new_port):
        if "start_cmd" not in i or i.get("start_cmd") in (None, ""):
            merged["start_cmd"] = sub_port(merged["start_cmd"], old_port, new_port)
        if "env" not in i or i["env"] is None:
            vars_ = [
                {**e, "v": sub_port(e.get("v", ""), old_port, new_port)} if isinstance(e, dict) else e
                for e in vars_
            ]
    dbm.execute(
        "UPDATE apps SET name=?,type=?,repo_url=?,branch=?,path=?,port=?,domain=?,install_cmd=?,start_cmd=?,env=?,"
        "unit_override=?,db_names=?,unit_template=?,parent_id=?,component_role=?,updated_at=datetime('now') WHERE id=?",
        (
            merged["name"],
            merged["type"],
            merged.get("repo_url") or "",
            merged.get("branch") or "main",
            # 写规范化后的路径：与 _validate 里重复检查用的值保持一致，
            # 否则检查用 /etc、入库存 /root/www/a/../../etc，下一次比对又错开。
            _app_path(merged),
            merged.get("port"),
            merged.get("domain") or None,
            merged.get("install_cmd") or None,
            merged["start_cmd"],
            json.dumps(vars_),
            merged.get("unit_override") or None,
            merged.get("db_names") or None,
            merged.get("unit_template") or None,
            merged.get("parent_id") or None,
            str(merged.get("component_role") or "")[:16],
            app_id,
        ),
    )
    return get_app(app_id)


def _check_not_protected(path: str):
    """安装目录不能指向面板自己或它的要害目录。

    部署流程会对 path 执行 `git fetch` + `git reset --hard origin/<branch>`，
    purge 还会 rmtree 它。把某个应用的 path 填成面板仓库（通常就在 APP_ROOT 之下，
    如 /root/www/choyeon-panel）后点部署，等于让面板把自己的代码切走甚至删掉，
    随后 systemd 重启失败，整台机器失去管理入口。
    """
    real = clean_abs_path(path, "安装目录")
    protected = {
        "面板程序目录": str(config.BASE),
        "面板数据目录": str(config.DATA_DIR),
        "systemd 配置目录": str(config.UNIT_DIR),
        "nginx 配置目录": "/etc/nginx",
        "证书目录": "/etc/letsencrypt",
    }
    for label, p in protected.items():
        try:
            root = clean_abs_path(p, label)
        except RuntimeError:
            continue
        if real == root or real.startswith(root + "/"):
            raise RuntimeError(f"安装目录不能位于{label}（{root}）之内或与之相同")


async def apply_edited(old: dict, new_app: dict) -> dict:
    """PATCH 之后把改动真正落到 systemd / .panel.env / nginx，服务在跑就顺手重启。

    旧写法只有"改名"才会重建 unit 与站点（原来的 cleanup_renamed），其余字段只进
    数据库：改了启动命令、端口、域名或环境变量，界面显示"已保存"，进程却还按老
    配置跑着，要生效只能再点一次「部署」——而部署连依赖都要重装，几分钟起步，
    用户既不敢点也等不到，于是"改了没反应"成了常态。

    返回 {"applied","restarted","note"}，让路由如实说这次到底生效了没有：外部
    unit（unit_override）的 ExecStart 不归面板写，也不该擅自重启别人的服务，
    这种情况只能讲清"本次只改了记录"。
    """
    out = {"applied": True, "restarted": False, "note": ""}
    if not old:
        return out
    renamed = old["name"] != new_app["name"]
    # 改名前没在跑的应用不该被改名"顺手启动"，所以先记下状态再拆
    was_active = await is_active(unit_name(old))
    if renamed:
        # 只拆不建是一半问题，只建不拆是另一半：旧 unit 带 Restart=always 会继续
        # 占着端口、旧站点还在把域名转过去，而面板记录里已经没有这个名字，
        # 界面上再也管不到这两个孤儿。
        await remove_unit(old)
        await nginx_ops.remove_vhost(old["name"])
    if new_app.get("unit_override"):
        out["applied"] = False
        out["note"] = "外部 unit：unit 与环境变量文件不由面板写入，本次只改了记录"
        return out
    try:
        write_env_file(new_app)
        await ensure_unit(new_app)
        if new_app.get("domain") and new_app.get("port"):
            await nginx_ops.apply_vhost(
                {"name": new_app["name"], "domain": new_app["domain"], "port": new_app["port"]}
            )
        elif old.get("domain") and not renamed:
            # 域名被清空、或只剩域名没有端口：留着那个 server 块就是一个指向死端口的 502
            await nginx_ops.remove_vhost(new_app["name"])
    except Exception as e:  # noqa: BLE001
        out["applied"] = False
        out["note"] = f"配置已保存，但没能同步到 systemd/nginx：{str(e)[:160]}（下次部署会补上）"
        return out
    # daemon-reload 只改"下次启动用什么"，跑着的进程还是旧的 ExecStart，
    # 所以在服务本来就在跑的时候必须重启一次，否则这次编辑依旧不生效。
    if was_active and renamed or await is_active(unit_name(new_app)):
        try:
            await restart_and_verify(unit_name(new_app), new_app["port"])
            out["restarted"] = True
        except Exception as e:  # noqa: BLE001
            out["note"] = f"配置已写入，但服务没能重启成功：{str(e)[:160]}"
    else:
        # 应用本来就没在跑（包括"上一次保存写了坏命令、服务已经崩掉"这种）：
        # 不擅自启动，但必须说清这次改动要等下一次启动或部署才生效，
        # 否则界面只剩一句"已保存"，用户会以为新配置已经在跑了。
        out["note"] = "配置已写入，但服务当前没有运行，改动在下一次启动或部署时生效"
    return out


def _check_purge_target(path: str) -> str:
    """只做校验、不删，返回规范化后的目标目录。

    把"这个目录能不能删"的判断提前到拆除动作之前：unit 与 nginx 都拆了、
    记录也删了才发现目录不能删，就留下一个面板上看不见的孤儿目录。
    """
    try:
        real = clean_abs_path(path, "应用目录")
    except RuntimeError as e:
        raise RuntimeError(f"拒绝删除应用目录：{e}") from None
    root = clean_abs_path(APP_ROOT, "APP_ROOT")
    if real == root or not real.startswith(root + "/"):
        raise RuntimeError(f"拒绝删除 {real}：不在应用根目录 {root}/ 之下")
    return real


def _purge_app_dir(path: str):
    """删除应用目录，只在"确属 APP_ROOT 之下"时动手，失败必须抛错。

    两个都已实测的问题：
    1. `path.startswith(APP_ROOT)` 是纯字符串前缀，APP_ROOT=/root/www 时
       /root/www-other、/root/www2、/root/wwwbackups 全部判定为"在里面"，
       purge 会把邻居目录整个删掉（构造目录验证过，确实被 rmtree 删除）。
    2. `shutil.rmtree(..., ignore_errors=True)` 会把失败整段吞掉：
       目录还在，但应用记录已经删了，面板上再也看不到它，成了没人管的孤儿目录。
    历史数据里可能有带 .. 的 path，规范化失败时直接拒绝而不是猜测。
    """
    real = _check_purge_target(path)
    if not os.path.exists(real):
        return
    try:
        shutil.rmtree(real)
    except OSError as e:
        raise RuntimeError(f"删除应用目录失败：{e}") from e


async def delete_app(app_id: int, purge: bool):
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "删除")
    # purge 的目标先判定，避免 unit 与 nginx 都拆了才发现目录不能删、记录却已删掉
    if purge:
        _check_purge_target(app["path"])
    await remove_unit(app)
    if app.get("domain"):
        await nginx_ops.remove_vhost(app["name"])
    dbm.execute("DELETE FROM apps WHERE id=?", (app_id,))
    # 部署记录只属于这个应用，删掉应用后没有任何入口能读到它们（含完整日志文本）。
    dbm.execute("DELETE FROM deployments WHERE app_id=?", (app_id,))
    if purge:
        _purge_app_dir(app["path"])


async def app_action(app_id: int, verb: str) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if verb == "rollback":
        return await rollback_app(app_id)
    # 部署流程自己会在结束时 restart；这时用户再点一次停止/重启，
    # 两边抢同一个服务，出现"部署日志说成功、服务实际没起来"的现场。
    _ensure_no_deployment(app_id, f"{_VERB_CN.get(verb, verb)}服务")
    unit = unit_name(app)
    if verb in ("start", "restart"):
        # 同 restart_and_verify：崩过一次的应用会被 systemd 的启动上限直接拒掉
        # （"Start request repeated too quickly"），用户分不清是自己命令写错还是面板坏了。
        if not await is_active(unit):
            await run("systemctl", ["reset-failed", unit], timeout=30)
    await service_action(unit, verb)
    if verb in ("start", "restart"):
        # 只信 systemctl 的返回码会把"起来就崩"说成启动成功（见 restart_and_verify）。
        # 手动点「启动/重启」的正是等着看效果的用户，这里必须等到进程稳定存活。
        await _wait_stable(unit)
    # service_action 失败会抛异常，走到这里就代表命令确实执行成功了。
    # 旧实现把 is_active 的返回值同时当作 ok：stop 成功后服务不再是 active，
    # 于是对外报 ok:false（HTTP 200），而 AGENTS.md 的约定是失败走 4xx/5xx，
    # 成功响应里的 ok 必须为 true——否则 CLI/前端按字段判断会把正常停止当失败。
    return {"ok": True, "active": await systemd_ops.is_active(unit)}


def _append_log(dep_id: int, text: str):
    # 单一落库入口，凭据在这里挡掉：git 的命令行与 stderr 都可能回显带 token 的仓库地址
    dbm.append_deploy_log(dep_id, redact_creds(text))


# 重启后给进程留多少时间起立。不能只信 systemctl 的返回码：Type=simple +
# Restart=always 的单元"起来就崩"时 restart 照样返回 0，于是部署写着"完成"、
# 界面全绿，端口却空着、域名 502——用户只能靠访问失败反推部署其实没成功。
UNIT_START_GRACE_SEC = 15
# 判定"起来了"要在连续两次采样里看到同一个主进程活着。
UNIT_STABLE_SAMPLES = 2
# 采样间隔：生产上 1s 足够让 python/node 完成 bind；测试把它调小以免每个用例等两秒。
UNIT_SAMPLE_SEC = 1
# 刚重启完就查端口必然读到空值：进程已 fork 但还没 bind。给绑定的这几秒留出余量，
# 否则每次正常部署都配一句"域名反代可能 502"的假警报，真出错时反而没人信。
PORT_WAIT_TRIES = 5


async def _live_pid(unit: str) -> int:
    """服务处于 active 且主进程确实还活着时返回它的 pid，否则 0。

    只看 `systemctl is-active` 不够：实测 Restart=always 的单元在崩溃自动重启的
    间隙里也会回 active，"起来就崩"的应用于是照样被判部署成功。
    """
    st = (await systemd_ops.unit_states([unit])).get(unit) or {}
    pid = int(st.get("mainPid") or 0)
    return pid if st.get("active") and pid > 0 and os.path.exists(f"/proc/{pid}") else 0


async def _wait_stable(unit: str) -> None:
    """等主进程连续 UNIT_STABLE_SAMPLES 次采样存活（且是同一个 pid），超时则抛错。

    只看 `systemctl is-active` 或 restart 的返回码都不够：实测 Restart=always 的单元
    在崩溃自动重启的间隙里也会回 active，"起来就崩"的应用于是照样被判部署成功——
    界面全绿、端口空着、域名 502，用户只能靠访问失败反推。
    要求"连续两次是同一个进程"是因为崩溃循环每 3 秒换一个新 pid。
    """
    deadline = time.monotonic() + UNIT_START_GRACE_SEC
    last, stable = 0, 0
    while time.monotonic() < deadline:
        await asyncio.sleep(UNIT_SAMPLE_SEC)
        pid = await _live_pid(unit)
        stable = stable + 1 if pid and pid == last else (1 if pid else 0)
        last = pid
        if stable >= UNIT_STABLE_SAMPLES:
            return
    raise RuntimeError(f"{unit} 在 {UNIT_START_GRACE_SEC}s 内主进程没能稳定存活，请看运行日志")


async def restart_and_verify(unit: str, port=None, *, warn=None) -> None:
    """重启并确认主进程稳定活着；端口对不上只作提醒，不判失败。

    端口是否监听没法当判据：worker 型应用本就不监听，而填错端口的代价只是 nginx
    502，把它做成硬失败会把一次正常上线说成失败。
    """
    # 崩到 systemd 的启动上限（默认 10s 内 5 次）之后，restart 会被直接拒绝：
    # "Start request repeated too quickly"。最容易撞上它的正是"把坏启动命令改对了再保存"
    # 这一次——配置已经写对了，服务却起不来，用户看不出两者没关系。
    # 既然这次调用就是要主动把服务拉起来，先把失败计数清掉（对正常停止的服务是空操作）。
    if not await is_active(unit):
        await run("systemctl", ["reset-failed", unit], timeout=30)
    await service_action(unit, "restart")
    await _wait_stable(unit)

    if port and warn:
        listening = None
        for attempt in range(PORT_WAIT_TRIES):
            listening = await detect_port(unit, force=attempt > 0)
            if listening == port:
                return
            await asyncio.sleep(UNIT_SAMPLE_SEC)
        if listening is None:
            warn(f"!!> {unit} 未检测到监听端口，而面板记录的是 {port}，域名反代可能 502")
        else:
            warn(f"!!> {unit} 监听在 {listening}，与面板记录的 {port} 不一致，域名反代可能 502")


async def deploy_app(app_id: int) -> int:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if dbm.query_one("SELECT id FROM deployments WHERE app_id=? AND status='running'", (app_id,)):
        raise RuntimeError("该应用已有部署任务在运行")
    res = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
    dep_id = res
    task = asyncio.create_task(_run_deployment(app, dep_id))
    _DEPLOY_TASKS.add(task)
    task.add_done_callback(_DEPLOY_TASKS.discard)
    return dep_id


async def wait_deployment(dep_id: int, app_id: int, timeout: int = 1800) -> dict:
    """等一次部署真正结束。

    CLI 与脚本都在自己的事件循环里调用 deploy_app；`asyncio.run` 退出时会取消
    尚未跑完的后台任务，部署停在 status='running'，而 _ensure_no_deployment
    会让这个应用此后无法删除/回滚/改配置，直到面板重启才被 main.py 的自愈逻辑纠正。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = dbm.query_one("SELECT status FROM deployments WHERE id=?", (dep_id,))
        if row and row["status"] != "running":
            return row
        await asyncio.sleep(1)
    raise RuntimeError(f"部署 #{dep_id} 超过 {timeout}s 仍未结束（应用 #{app_id}）")


async def _current_sha(path: str) -> str | None:
    r = await run("git", ["rev-parse", "--short", "HEAD"], cwd=path, timeout=30)
    return r["out"] if r["code"] == 0 else None


async def rollback_app(app_id: int) -> dict:
    """回滚到**上一个**成功部署的版本，并重启服务。

    旧实现取"最近一条成功记录"的 commit，而部署成功时记下的正是当前正在运行的
    commit，于是 `git reset --hard <当前 commit>` 变成空操作——用户点了"回滚"却什么都没变
    （只有"最近一次部署失败"这种场景才恰好正确）。
    现在以工作区实际 commit 为基准，往前找第一个不同的成功版本。
    """
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "回滚")
    if not os.path.isdir(f"{app['path']}/.git"):
        raise RuntimeError("应用目录不是 Git 仓库，无法回滚")
    rows = dbm.query(
        "SELECT id,commit_sha FROM deployments WHERE app_id=? AND status='success' AND commit_sha IS NOT NULL"
        " ORDER BY id DESC LIMIT 20",
        (app_id,),
    )
    if not rows:
        raise RuntimeError("没有可回滚的成功部署记录")
    current = await _current_sha(app["path"])
    target = None
    seen: set[str] = set()
    for row in rows:
        sha = row["commit_sha"]
        if not sha or sha in seen:
            continue
        seen.add(sha)
        if sha != current:
            target = row
            break
    if not target:
        raise RuntimeError("没有更早的成功版本可回滚（当前已是最近一次成功部署）")
    r = await run("git", ["reset", "--hard", target["commit_sha"]], cwd=app["path"], timeout=120)
    if r["code"] != 0:
        raise RuntimeError(f"回滚失败：{r['out']}")
    try:
        await restart_and_verify(unit_name(app), app["port"])
    except RuntimeError as e:
        # 工作区已经切到目标版本了，只说"回滚失败"会让人以为再点一次就好，
        # 实际要处理的是那个起不来的进程。
        raise RuntimeError(f"代码已回滚到 {target['commit_sha']}，但服务没起来：{e}") from None
    return {"ok": True, "commit": target["commit_sha"], "deployment": target["id"], "from": current}


async def sync_app(app_id: int) -> dict:
    """拉取远端最新代码并重启服务——比部署短的路径：不装依赖、不写 env/unit/nginx。

    代码改完想立刻上线时，走 deploy 要等 npm/pip 几分钟，sync 只有 fetch/reset/restart
    三步，通常在几秒内完成。仍然落一条 deployments 记录（status=success + commit_sha），
    这样同步上去的版本也能被「回滚」选中，不会出现"同步完发现坏了、回滚列表里找不到上一版"。
    """
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    _ensure_no_deployment(app_id, "同步")
    if not app["repo_url"]:
        raise RuntimeError("应用未配置 Git 仓库地址，无法同步")
    if not is_git_url(app["repo_url"]):
        raise RuntimeError("Git 仓库地址不合法")
    branch = app["branch"] or "main"
    if not is_branch(branch):
        raise RuntimeError("分支名不合法")
    path = app["path"]
    # 单体仓库（monorepo）里前后端组件常常各占一个子目录，子目录没有 .git。
    # 用 rev-parse 找到真正的工作树根，同步才能覆盖"组件目录在同一个仓库内"的场景。
    repo_dir = path
    if not os.path.isdir(f"{path}/.git"):
        top = await run("git", ["-C", path, "rev-parse", "--show-toplevel"], cwd=None, timeout=30)
        cand = (top["out"] or "").strip().splitlines()[-1] if top["code"] == 0 else ""
        if cand and os.path.isdir(f"{cand}/.git"):
            repo_dir = cand
    if not os.path.isdir(f"{repo_dir}/.git"):
        raise RuntimeError(f"{path} 下没有 Git 仓库，请先执行一次完整部署")

    dep_id = dbm.execute("INSERT INTO deployments(app_id,status) VALUES(?,'running')", (app_id,))
    before = await _current_sha(repo_dir)
    out: list[str] = []

    def log(text: str):
        out.append(text)
        _append_log(dep_id, text)

    try:
        if repo_dir != path:
            log(f"==> 仓库根 {repo_dir}（应用目录是其子目录，本次会更新整个仓库）")
        log(f"==> git fetch origin {branch}")
        r = await run("git", ["fetch", "origin", branch], cwd=repo_dir, timeout=300)
        log(redact_creds(r["out"]))
        if r["code"] != 0:
            raise RuntimeError(f"拉取远端失败（exit {r['code']}）")

        r = await run("git", ["checkout", branch], cwd=repo_dir, timeout=120)
        if r["code"] != 0:
            # 分支可能只存在于刚拉取的远端（本地从未 checkout 过）：
            # checkout 失败先试一次跟踪分支创建，仍失败才是真问题。
            log(redact_creds(r["out"]))
            r = await run("git", ["checkout", "-B", branch, f"origin/{branch}"], cwd=repo_dir, timeout=120)
            if r["code"] != 0:
                raise RuntimeError(f"切换分支失败（exit {r['code']}）")
        log(f"==> git reset --hard origin/{branch}")
        st = await run("git", ["status", "--porcelain"], cwd=repo_dir, timeout=60)
        if st["code"] == 0 and st["out"].strip():
            # reset --hard 只会丢已跟踪文件的本地改动，未跟踪目录（构建备份、缓存）不受影响，
            # 所以警告只统计跟踪改动，别把一堆 "??" 说成"要被覆盖"吓到人。
            dirty = [ln for ln in st["out"].splitlines() if ln.strip() and not ln.startswith("??")]
            untracked = [ln for ln in st["out"].splitlines() if ln.startswith("??")]
            if dirty:
                log(f"!!! 工作区有 {len(dirty)} 处未提交改动，将被覆盖：{'; '.join(dirty[:6])}")
            if untracked:
                log(f"==> 另有 {len(untracked)} 个未跟踪路径（不受本次同步影响）")
        r = await run("git", ["reset", "--hard", f"origin/{branch}"], cwd=repo_dir, timeout=120)
        log(redact_creds(r["out"]))
        if r["code"] != 0:
            raise RuntimeError(f"更新工作区失败（exit {r['code']}）")

        after = await _current_sha(repo_dir)
        log(f"==> 当前 commit {after or '未知'}" + (f"（同步前 {before}）" if before else ""))
        if before and after and before != after:
            log(f"==> 版本变化 {before} -> {after}")
        elif before and after == before:
            log("==> 远端无新提交，工作区版本未变化")

        log(f"==> 重启并确认服务 {unit_name(app)}")
        await restart_and_verify(unit_name(app), app["port"], warn=log)
        dbm.execute(
            "UPDATE deployments SET status='success', commit_sha=?, finished_at=datetime('now') WHERE id=?",
            (after, dep_id),
        )
        dbm.execute("UPDATE apps SET updated_at=datetime('now') WHERE id=?", (app_id,))
        log("==> 同步完成")
        return {"ok": True, "commit": after, "from": before, "changed": bool(before and after and before != after), "deployment": dep_id, "out": "\n".join(out)}
    except Exception as e:  # noqa: BLE001
        # 服务没起来却把记录留在 running，前端会一直显示"部署中"，
        # 且 _ensure_no_deployment 会让这个应用此后无法再同步/回滚。
        dbm.execute("UPDATE deployments SET status='failed', finished_at=datetime('now') WHERE id=?", (dep_id,))
        log(f"!!! 同步失败: {e}")
        raise


async def _run_deployment(app: dict, dep_id: int):
    def finish(ok: bool):
        dbm.execute(
            "UPDATE deployments SET status=?, finished_at=datetime('now') WHERE id=?",
            ("success" if ok else "failed", dep_id),
        )
        dbm.execute("UPDATE apps SET updated_at=datetime('now') WHERE id=?", (app["id"],))

    # 动工作区之前先记下当前 commit，失败时才能说清"盘上的代码和服务跑的代码是不是两份"
    before = await _current_sha(app["path"]) if os.path.isdir(f"{app['path']}/.git") else None
    try:
        if app["repo_url"]:
            if os.path.exists(f"{app['path']}/.git"):
                _append_log(dep_id, f"==> git fetch/checkout {app['branch']}")
                r = await run("git", ["fetch", "origin", app["branch"]], cwd=app["path"], timeout=300)
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run("git", ["checkout", app["branch"]], cwd=app["path"])
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                r = await run("git", ["reset", "--hard", f"origin/{app['branch']}"], cwd=app["path"])
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
            else:
                _append_log(dep_id, f"==> git clone -b {app['branch']} {app['repo_url']} -> {app['path']}")
                r = await run(
                    "git", ["clone", "--depth", "1", "-b", app["branch"], app["repo_url"], app["path"]], timeout=600
                )
                if r["code"] != 0:
                    raise RuntimeError(r["out"])
                _append_log(dep_id, r["out"])
        else:
            _append_log(dep_id, f"==> 跳过拉取（未配置仓库，使用现有目录 {app['path']}）")
        if not os.path.exists(app["path"]):
            raise RuntimeError(f"目录不存在: {app['path']}")

        _append_log(dep_id, f"==> 安装依赖 ({app['type']})")
        default_install = (
            "npm install"
            if app["type"] == "node"
            else "python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
        )
        install = app["install_cmd"] or default_install
        ir = await run("/bin/bash", ["-lc", install], cwd=app["path"], timeout=1800)
        _append_log(dep_id, ir["out"] or "(no output)")
        if ir["code"] != 0:
            raise RuntimeError(f"安装依赖失败 (exit {ir['code']})")

        _append_log(dep_id, "==> 写入环境变量与 systemd unit")
        write_env_file(app)
        await ensure_unit(app)

        if app["domain"]:
            if app["port"]:
                _append_log(dep_id, f"==> 应用 nginx 反代 {app['domain']} -> 127.0.0.1:{app['port']}")
                await nginx_ops.apply_vhost({"name": app["name"], "domain": app["domain"], "port": app["port"]})
            else:
                # 反代必须知道往哪个端口转，缺端口就只能不配；不写这一行的话
                # 用户只看到"部署完成"，域名却始终打不开，无从猜起是证书还是应用的问题。
                _append_log(dep_id, f"!!> 已配置域名 {app['domain']} 但未填端口，跳过 nginx 反代（补上端口后重新部署）")

        _append_log(dep_id, f"==> 重启并确认服务 {unit_name(app)}")
        await restart_and_verify(unit_name(app), app["port"], warn=lambda t: _append_log(dep_id, t))
        sha = await _current_sha(app["path"])
        if sha:
            dbm.execute("UPDATE deployments SET commit_sha=? WHERE id=?", (sha, dep_id))
            _append_log(dep_id, f"==> 当前 commit {sha}（可在部署记录中回滚到此版本）")
        _append_log(dep_id, "==> 部署完成")
        finish(True)
    except Exception as e:  # noqa: BLE001
        _append_log(dep_id, f"!!! 部署失败: {e}")
        # 实测：git fetch/reset 之后安装依赖失败（exit 7）时，
        # 工作区已经变成新代码（CODE=v2），而服务进程还在跑旧代码，
        # 面板只写"部署失败"，看现场的人无法判断现在盘上到底是哪一份。
        # 这里不自动 reset 回去——用户可能正要在新代码上排查，静默改工作区更糟；
        # 只把差异写进日志，并指出可用的回滚目标。
        after = await _current_sha(app["path"]) if os.path.isdir(f"{app['path']}/.git") else None
        if before and after and before != after:
            _append_log(
                dep_id,
                f"!!! 工作区已从 {before} 切到 {after}，但服务未被重启，仍在运行旧代码",
            )
            _append_log(dep_id, "    如需回到部署前的版本，用「回滚」或 choyeonctl app rollback --id 部署记录")
        finish(False)


async def attach_ssl(app_id: int, email: str | None = None) -> str:
    app = get_app(app_id)
    if not app or not app["domain"]:
        raise RuntimeError("应用未配置域名")
    if app["port"] is None:
        raise RuntimeError("未填写端口且服务未运行，无法反代；请先在应用配置里填端口")
    out = await nginx_ops.issue_cert(app["domain"], email)
    await nginx_ops.apply_vhost({"name": app["name"], "domain": app["domain"], "port": app["port"]})
    return out


def read_unit(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    unit = unit_name(app)
    path = f"{UNIT_DIR}/{unit}"
    content = Path(path).read_text(errors="replace") if os.path.exists(path) else ""
    return {
        "unit": unit,
        "path": path,
        "managed": not app["unit_override"],
        "content": content,
        "template": app["unit_template"],
        "defaultTemplate": render_unit({**app, "unit_template": None}),
    }


async def save_unit(app_id: int, content: str) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    if app["unit_override"]:
        raise RuntimeError("纳管模式：unit 由外部管理，面板不写文件")
    if not content.strip():
        raise RuntimeError("unit 内容不能为空")
    if len(content) > 8000:
        raise RuntimeError("unit 内容过长")
    if re.search(r"\$\{|\$\(|`", content):
        raise RuntimeError("unit 不允许 shell 替换语法")
    if "[Service]" not in content:
        raise RuntimeError("unit 必须包含 [Service] 段")
    if not re.search(r"^ExecStart=", content, re.M):
        raise RuntimeError("unit 必须包含 ExecStart=")
    _ensure_no_deployment(app_id, "修改 unit")
    # 补一次换行得到 normalized，文件和库里都写它。
    # 旧代码文件写 normalized、库写 content + "\n"，末尾本来就有换行时库存下两个换行；
    # 前端读回的 template 又带这个多余空行，用户原样再保存一次就累积成三个，
    # 部署时 render_unit 用的正是这份 unit_template，于是每部署一次现场就更难对齐。
    normalized = content if content.endswith("\n") else content + "\n"
    unit = unit_name(app)
    path = f"{UNIT_DIR}/{unit}"
    old = Path(path).read_text(errors="replace") if os.path.exists(path) else None

    def _restore():
        """失败回滚：原来没有这个文件就把刚写进去的删掉。

        旧实现只在 old is not None 时恢复，首次保存（或 unit 文件已被删）失败后，
        这份没通过 verify 的 unit 会留在 UNIT_DIR 里——当下没 daemon-reload 所以还没加载，
        可之后任何一次 daemon-reload（别的应用部署、面板重启）都会把它读进去。
        """
        try:
            if old is None:
                os.unlink(path)
            else:
                Path(path).write_text(old)
        except OSError:
            pass

    Path(path).write_text(normalized)
    verify = await run("systemd-analyze", ["verify", path], timeout=30)
    if verify["code"] != 0:
        _restore()
        raise RuntimeError(f"systemd-analyze verify 失败，已回滚：\n{verify['out']}")
    reload = await run("systemctl", ["daemon-reload"])
    if reload["code"] != 0:
        _restore()
        await run("systemctl", ["daemon-reload"])
        raise RuntimeError(f"daemon-reload 失败，已回滚：\n{reload['out']}")
    dbm.execute(
        "UPDATE apps SET unit_template=?,updated_at=datetime('now') WHERE id=?", (normalized, app_id)
    )
    return {"ok": True, "unit": unit, "restartRequired": True}


PROC_FIELDS = [
    "MainPID",
    "MemoryCurrent",
    "NRestarts",
    "ActiveEnterTimestamp",
    "CPUUsageNSec",
    "ExecMainStartTimestamp",
    "SubState",
]


async def app_proc(app_id: int) -> dict:
    app = get_app(app_id)
    if not app:
        raise RuntimeError("应用不存在")
    unit = unit_name(app)
    args = ["show", unit] + [f"-p{f}" for f in PROC_FIELDS]
    r = await run("systemctl", args, timeout=8)
    got: dict[str, str] = {}
    for line in r["out"].split("\n"):
        i = line.find("=")
        if i > 0:
            got[line[:i]] = line[i + 1:].strip()

    def _int(key):
        try:
            return int(got.get(key, ""))
        except ValueError:
            return None

    def _ts(key):
        v = got.get(key)
        return v if v and v != "n/a" else None

    pid = _int("MainPID") or 0
    mem = _int("MemoryCurrent")
    cpu_ns = _int("CPUUsageNSec")
    return {
        "unit": unit,
        "subState": got.get("SubState") or "unknown",
        "running": pid > 0,
        "pid": pid or None,
        "memoryBytes": mem,
        "restarts": _int("NRestarts") or 0,
        "activeSince": _ts("ActiveEnterTimestamp"),
        "execStartAt": _ts("ExecMainStartTimestamp"),
        "cpuSeconds": round(cpu_ns / 1e6) / 1000 if cpu_ns is not None else None,
    }
