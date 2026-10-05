"""轻量日志：统一前缀 + 时间，输出到 stdout（由 systemd journal 收集）。"""

import sys
import time


def log(*parts) -> None:
    msg = " ".join(str(p) for p in parts)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True, file=sys.stdout)


def warn(*parts) -> None:
    msg = " ".join(str(p) for p in parts)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARN {msg}", flush=True, file=sys.stderr)
