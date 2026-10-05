import asyncio
import os
import re
import subprocess
from contextlib import suppress

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
def sh_escape(s): return "'" + str(s).replace("'", "'\\''") + "'"


async def run(cmd: str, args: list, cwd=None, timeout=120, env=None) -> dict:
    """异步子进程执行，返回 {code, out}。

    超时时终止整个进程组（避免 npm/pip 子进程残留）；命令不存在返回 code=127。
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            cmd, *args,
            cwd=cwd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            _kill_group(proc)
            return {"code": 124, "out": f"timeout after {timeout}s"}
        return {"code": proc.returncode or 0, "out": out.decode(errors="replace").strip()}
    except FileNotFoundError as e:
        return {"code": 127, "out": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"code": 1, "out": str(e)}


def _kill_group(proc) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except (ProcessLookupError, PermissionError, OSError):
        with_subprocess(proc)


def with_subprocess(proc) -> None:
    with suppress(ProcessLookupError, AttributeError):
        proc.kill()


def run_shell(cmd: str, timeout=None) -> subprocess.CompletedProcess:
    """同步 shell 执行（backup runner / CLI 场景）。"""
    return subprocess.run(["/bin/sh", "-c", cmd], capture_output=True, text=True, timeout=timeout)


async def run_shell_async(cmd: str, timeout: float | None = 10) -> str:
    """在事件循环中安全地执行 shell 命令（避免阻塞）。返回 stdout；失败返回空串。"""
    def _sync() -> str:
        try:
            return run_shell(cmd, timeout=timeout).stdout
        except Exception:  # noqa: BLE001
            return ""

    return await asyncio.to_thread(_sync)
