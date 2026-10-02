"""Unit tests for sys_helper parsers (run: uv run python -m unittest discover -s tests)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from sys_helper import (
    parse_loadavg,
    parse_meminfo,
    parse_netdev,
    parse_stat,
    parse_uptime,
)

STAT = """cpu  1000 200 300 5000 100 50 25 0 0 0
cpu0 500 100 150 2500 50 25 12 0 0 0
intr 12345
"""

MEMINFO = """MemTotal:       16384000 kB
MemFree:         2048000 kB
MemAvailable:   10240000 kB
Buffers:          512000 kB
"""

NETDEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:111111     100    0    0    0     0          0         100    111111     100    0    0    0     0       0
  eth0:2000000    9000    0    0    0     0          0          9000 3000000    8000    0    0    0     0       0
  eth1:500000     500    0    0    0     0          0           500  250000     400    0    0    0     0       0
"""


class ParserTest(unittest.TestCase):
    def test_parse_stat(self):
        got = parse_stat(STAT)
        self.assertEqual(got["idle"], 5000 + 100)
        self.assertEqual(got["total"], 1000 + 200 + 300 + 5000 + 100 + 50 + 25)

    def test_parse_stat_missing_cpu_line(self):
        with self.assertRaises(ValueError):
            parse_stat("intr 1\n")

    def test_parse_stat_short_line(self):
        got = parse_stat("cpu  1 2 3 4\n")
        self.assertEqual(got, {"idle": 4, "total": 10})

    def test_parse_meminfo(self):
        got = parse_meminfo(MEMINFO)
        self.assertEqual(got["total"], 16384000 * 1024)
        self.assertEqual(got["available"], 10240000 * 1024)

    def test_parse_meminfo_no_memtotal(self):
        with self.assertRaises(ValueError):
            parse_meminfo("MemFree: 1 kB\n")

    def test_parse_netdev_skips_loopback(self):
        got = parse_netdev(NETDEV)
        self.assertEqual(got["rx"], 2000000 + 500000)
        self.assertEqual(got["tx"], 3000000 + 250000)

    def test_parse_loadavg(self):
        self.assertEqual(parse_loadavg("0.52 0.48 0.33 1/234 5678"), [0.52, 0.48, 0.33])

    def test_parse_uptime(self):
        self.assertEqual(parse_uptime("12345.67 999.0"), 12345.67)


if __name__ == "__main__":
    unittest.main()
