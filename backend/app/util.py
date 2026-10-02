import asyncio
import re
import subprocess

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,38}$")
DOMAIN_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
UNIT_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,64}(\.service)?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
GIT_URL_RE = re.compile(r"^(https?://\S+|git@[\w.-]+:\S+)$")
IDENT_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]{2,200}$")
UNIT_NAME_FILE_RE = re.compile(r"^[A-Za-z0-9@:._-]{1,64}\.service$")


def is_name(s): return bool(NAME_RE.match(s or ""))
def is_domain(s): return bool(DOMAIN_RE.match(s or ""))
def is_unit(s): return bool(UNIT_RE.match(s or ""))
def is_unit_file(s): return bool(UNIT_NAME_FILE_RE.match(s or ""))
def is_port(p): return isinstance(p, int) and 1024 <= p <= 65535
def is_branch(s): return bool(BRANCH_RE.match(s or ""))
def is_ident(s): return bool(IDENT_RE.match(s or ""))
def is_path(s): return bool(PATH_RE.match(s or ""))
def is_git_url(s): return bool(s) and (bool(GIT_URL_RE.match(s)) or s.startswith("/"))
def sh_escape(s): return "'" + s.replace("'", "'\\''") + "'"


async def run(cmd: str, args: list, cwd=None, timeout=120, env=None):
    """subprocess 调用，返回 {code, out}，行为与 Node 版 run() 对齐。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {"code": 1, "out": f"timeout after {timeout}s"}
        return {"code": proc.returncode or 0, "out": out.decode(errors="replace").strip()}
    except FileNotFoundError as e:
        return {"code": 127, "out": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"code": 1, "out": str(e)}


def run_shell(cmd: str, timeout=None):
    """同步 shell 执行（backup runner / psql 管道等场景），返回 CompletedProcess。"""
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)
