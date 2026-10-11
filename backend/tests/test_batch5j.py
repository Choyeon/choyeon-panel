"""批次⑤J：/proc 兜底、数据浏览器与 nginx 站点列表的口径。

这一批的缺陷都不是"接口 500"，而是面板很自信地说了一件不成立的事：
1. /proc/diskstats 的 6/10 号字段是**毫秒**（读/写耗时），不是数据量。旧代码把它们当扇区
   乘 512，兜底路径的磁盘读写速度直接错两个数量级；顺带 `末尾是数字就算分区` 会把
   NVMe 整盘（nvme0n1）自己也滤掉，在那类机器上恒为 0。
2. 凭据打码只认 `https://user:pass@host` 一种形态：pg 失败正文里的 `password=…`、
   部署日志回显的 `NEXTAUTH_SECRET=…`、应用日志里的 `Bearer eyJ…` 都原样落库、
   原样出接口，而日志检索接口对只读账号开放。反向的误伤同样实测过：sudo 的会话日志
   每一条都带 `PWD=当前目录`，所以整词 pwd 明确**不**当口令键名。
3. PG 标识符只认小写，于是 Prisma/TypeORM 那种 "UserProfile" 驼峰表在表列表里
   列得出来、点不开（永远一句"表名不合法"）；建库时不带引号又会被 Postgres 折叠成
   小写，用户按自己输入的驼峰名再去查就变成"库不存在"。
4. redis_del 自己另写了一个 `len(k) <= 512` 的过滤器，把 513~2048 的 key 静默丢掉：
   界面选 3 个删、只回 `deleted: 1`，那个长 key 根本没被送去执行，也不报错。
   而 SCAN/详情用的是另一份上限（2048），等于"看得见、删不掉"。
5. nginx 的站点列表/配置读取按目录全量扫，把 sites-available 里**没链接进
   sites-enabled、因此根本没生效**的草稿也算成一个站点；更糟的是
   find_app_configs 会把"这个域名的配置"解析到那份草稿上，用户改它、nginx -t 通过、
   reload 成功，线上一个字都没变（⑤H 已经把同一结论用在写入路径，读的一侧漏了）。
"""

import asyncio
import os
import shutil
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from unittest import mock

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_5j_ut_"))

from app import config, errors  # noqa: E402
from app.services import nginx_ops, pg_service as P, system_metrics as M  # noqa: E402
from app.util import redact_creds  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- diskstats

# shutil.disk_usage 返回的是带字段名的元组，代码里读 du.total / du.used；
# 用普通三元组顶替会让用例死在 AttributeError 上，而不反映被测代码。
_Du = namedtuple("_Du", "total used free")


class TestDiskStatsUnits(unittest.TestCase):
    """兜底路径的磁盘 IO：字段取错就是错两个数量级，而且只在没装 psutil 时露出来。"""

    # 本机 207.57.127.6 抓的真实行（不要凭记忆编：⑤I 的教训就是夹具跟着错误假设写，
    # 结果单测全绿而线上是坏的）。字段序号：
    #   3=读次数 4=读合并 5=读扇区 6=读ms 7=写次数 8=写合并 9=写扇区 10=写ms
    # sda 的 p[9]*512 与 psutil.disk_io_counters().write_bytes 逐位相同。
    SDA = ("   8       0 sda 1074524 365183 29493706 294452 2581828 4147637 "
           "83555331 2542994 0 663944 3036619 129793 1 791787840 12544 150592 186627")
    SDA1 = ("   8       1 sda1 912635 364936 24533854 256452 2579587 4147637 "
            "83555330 2541579 0 1606732 2810576 129790 0 791552696 12544 0 0")
    SR0 = "  11       0 sr0 1496 0 748 8 0 0 0 0 0 0 0 0 0 0 0 0 0"

    READ_SECTORS = 29493706
    WRITE_SECTORS = 83555331
    READ_MS = 294452
    WRITE_MS = 2542994
    PART_READ_SECTORS = 24533854

    def _raw(self, diskstats: str) -> dict:
        files = {
            "/proc/stat": "cpu  1 2 3 4 5 6 7 8 9 10\ncpu0 1 2 3 4 5 6 7 8 9 10\n",
            "/proc/meminfo": "MemTotal: 1000 kB\nMemAvailable: 500 kB\n",
            "/proc/net/dev": " Inter-|   Receive\n face |bytes packets errs\n  eth0:  10 0 0 0 0 0 0 0 20 0 0 0 0 0 0 0\n",
            "/proc/diskstats": diskstats,
        }
        with mock.patch.object(M, "_read", lambda p: files.get(p, "")), \
             mock.patch.object(M.os, "listdir", lambda p: ["1", "2"]), \
             mock.patch.object(M.shutil, "disk_usage", lambda p: _Du(1, 1, 1)):
            return M._proc_raw()

    def test_sectors_not_milliseconds(self):
        got = self._raw(self.SDA)
        self.assertEqual(got["ioRead"], self.READ_SECTORS * 512)
        self.assertEqual(got["ioWrite"], self.WRITE_SECTORS * 512)
        self.assertNotEqual(got["ioRead"], self.READ_MS * 512, "6 号字段是读耗时毫秒，不是字节")
        self.assertNotEqual(got["ioWrite"], self.WRITE_MS * 512, "10 号字段是写耗时毫秒，不是字节")

    def test_partitions_are_not_double_counted(self):
        got = self._raw("\n".join([self.SDA, self.SDA1]))
        self.assertEqual(got["ioWrite"], self.WRITE_SECTORS * 512, "sda1 不能和整盘一起加")
        self.assertEqual(got["ioRead"], self.READ_SECTORS * 512)
        self.assertNotEqual(got["ioRead"], (self.READ_SECTORS + self.PART_READ_SECTORS) * 512)

    def test_only_disk_prefixes_count(self):
        """光驱 sr0 不算磁盘吞吐（psutil 会把它计进 read_bytes，这是有意的口径差）。"""
        got = self._raw("\n".join([self.SDA, self.SR0]))
        self.assertEqual(got["ioRead"], self.READ_SECTORS * 512)

    def test_nvme_whole_disk_survives(self):
        """nvme0n1 末尾就是数字，但它不是分区——按老规则滤掉后这类机器恒为 0。"""
        line = " 259       0 nvme0n1 1 2 300 4 5 6 700 8 9 10 11"
        got = self._raw(line)
        self.assertEqual(got["ioRead"], 300 * 512)
        self.assertEqual(got["ioWrite"], 700 * 512)
        self.assertEqual(self._raw(line.replace("nvme0n1", "nvme0n1p2"))["ioRead"], 0)

    def test_whole_disk_names(self):
        for n in ("sda", "vdb", "xvda", "nvme0n1", "nvme1n1", "mmcblk0"):
            self.assertTrue(M._is_whole_disk(n), n)
        for n in ("sda1", "vdb2", "xvda3", "nvme0n1p1", "mmcblk0p1"):
            self.assertFalse(M._is_whole_disk(n), n)

    def test_legacy_ten_field_line_still_counts(self):
        """老内核（4.18 前）只有 10 个统计字段，p[9] 仍是写扇区：不能因为字段少就整块丢掉。"""
        got = self._raw("   8       0 sda 1 2 300 4 5 6 700 8 9 10")
        self.assertEqual(got["ioRead"], 300 * 512)
        self.assertEqual(got["ioWrite"], 700 * 512)

    def test_short_line_is_ignored(self):
        """截断行（读到一个只写了一半的 /proc 片段）不能让整次采样炸掉。"""
        got = self._raw("   8       0 sda 1 2 300 4")
        self.assertEqual(got["ioWrite"], 0)
        self.assertEqual(got["ioRead"], 0)


# ---------------------------------------------------------------- 凭据打码


class TestCredentialRedaction(unittest.TestCase):
    SECRET = "ghp_SUpErSecret123"

    def test_url_credentials_still_masked(self):
        self.assertEqual(
            redact_creds(f"git clone https://octo:{self.SECRET}@github.com/a/b.git"),
            "git clone https://octo:***@github.com/a/b.git",
        )

    def test_keyword_value_is_masked(self):
        # psycopg/libpq 的失败正文，以前原样进日志接口
        msg = redact_creds('connection failed: password=Sup3rS3cr3t user="app"')
        self.assertNotIn("Sup3rS3cr3t", msg)
        self.assertIn("password=***", msg)

    def test_env_style_key_is_masked(self):
        """部署日志会回显 .panel.env；键名带前缀（NEXTAUTH_SECRET、DB_PASSWORD）也要遮。"""
        for s in ("NEXTAUTH_SECRET=abc123xyz", "DB_PASSWORD='p@ss word'", "csrf_token: aaaabbbbcccc"):
            out = redact_creds(s)
            self.assertNotIn("abc123xyz", out)
            self.assertNotIn("p@ss", out)
            self.assertNotIn("aaaabbbbcccc", out)
            self.assertIn("***", out)

    def test_multi_segment_key_is_masked(self):
        """⑤R：真实 SDK 的键名几乎都不是单个词。

        旧规则要求关键字整词结尾，`AWS_SECRET_ID=`、`AWS_ACCESS_KEY_ID=` 就这样带着
        明文出去了——而这类值正好写在应用的 start_cmd / .panel.env 里，对只读账号也开放。
        """
        for s, secret in (
            ("AWS_SECRET_ID=wjj thing", "wjj"),
            ("export AWS_SECRET_ACCESS_KEY=AKIAiosfj1030fj", "AKIAiosfj1030fj"),
            ("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
            ("SESSION_TOKEN_VALUE: ghp_abc123DEF", "ghp_abc123DEF"),
            ("PRIVATE_KEY=/root/.ssh/id_ed25519", "id_ed25519"),
        ):
            out = redact_creds(s)
            self.assertNotIn(secret, out, s)
            self.assertIn("***", out, s)

    def test_bearer_token_is_masked(self):
        out = redact_creds("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc")
        self.assertIn("Bearer ***", out)
        self.assertNotIn("eyJhbGciOiJIUzI1NiI", out)

    def test_ordinary_text_is_untouched(self):
        """误伤比漏遮更难查：这些必须一字不改。"""
        for s in (
            "https://github.com/a/b.git",
            "git@github.com:a/b.git",
            "secretary=42 status=200 api_calls=3",
            "GET /api/db/redis/key?k=token_list&page=2",
            "nginx reload ok, worker_processes auto",
            'psql -c "SELECT 1" -d rosetta',
            # ⑤J 线上探针在 80 行真日志里抓到的误伤：sudo 每条会话日志都带 PWD=当前目录，
            # 把整词 pwd 当口令键名会把运维要看的cwd 遮成 ***，所以这一形态明确不遮。
            "    root : PWD=/root/www/choyeon-panel/backend ; USER=postgres ; COMMAND=/usr/bin/psql",
            "cd $PWD && npm run build",
            # 后缀形态放宽之后仍然不能误伤：这些含关键字的普通词后面接的是字母，不是分隔符。
            "tokenizer_model=gpt2 max_tokens=8",
            "keyfile=/etc/nginx/keys/dhparam.pem",
            "npm run build --password-stdin",
            "",
            None,
        ):
            self.assertEqual(redact_creds(s), s, s)

    def test_separator_is_preserved(self):
        self.assertEqual(redact_creds("password: hunter2"), "password: ***")
        self.assertEqual(redact_creds("password=hunter2"), "password=***")

    def test_public_message_uses_the_same_rules(self):
        """errors.py 里那份重复的键值正则删掉了，能力不能跟着掉。"""
        msg = errors.public_message(RuntimeError("dump failed password=Sup3rS3cr3t at /root/backups/x"))
        self.assertNotIn("Sup3rS3cr3t", msg)
        self.assertIn("<路径>", msg)


# ---------------------------------------------------------------- PG 标识符


class TestPgIdentifiers(unittest.TestCase):
    def test_mixed_case_is_accepted(self):
        """驼峰表名以前"列得出来点不开"，校验现在放开大小写。"""
        for n in ("UserProfile", "MyTable", "audit_log2", "_priv", "Rosetta"):
            self.assertTrue(P.is_ident(n), n)

    def test_bad_shapes_rejected(self):
        for n in ("Ro-setta", "1abc", 'a"b', "a b", "a;DROP TABLE x", "a'b", "", None,
                  "postgresql://evil/db", "x" * 64):
            self.assertFalse(P.is_ident(n), repr(n))

    def _capture_psql(self, out="[]"):
        seen = []

        async def fake(sql, db=None, quiet=False):
            seen.append(sql)
            return out

        return seen, mock.patch.object(P, "psql", fake)

    def test_create_db_quotes_identifier(self):
        """不带引号建库会被 Postgres 折叠成小写，之后按用户输入的驼峰名查就"库不存在"。"""
        seen, patcher = self._capture_psql("")
        with patcher:
            _run(P.create_db("MyDb", owner="AppUser"))
        self.assertEqual(seen, ['CREATE DATABASE "MyDb" OWNER "AppUser";'])

    def test_role_ddl_quotes_identifier(self):
        seen, patcher = self._capture_psql("")
        with patcher:
            _run(P.create_role("AppUser", "Pw123456"))
            _run(P.drop_role("AppUser"))
            _run(P.grant("AppUser", "MyDb"))
        self.assertEqual(seen[0], 'CREATE ROLE "AppUser" LOGIN PASSWORD \'Pw123456\';')
        self.assertEqual(seen[1], 'DROP OWNED BY "AppUser"; DROP ROLE "AppUser";')
        self.assertEqual(seen[2], 'GRANT ALL PRIVILEGES ON DATABASE "MyDb" TO "AppUser";')

    def test_table_meta_reads_pascal_case_table(self):
        seen, patcher = self._capture_psql()
        with patcher:
            meta = _run(P.table_meta("rosetta", "public", "UserProfile"))
        self.assertEqual(meta, {"columns": [], "pk": []})
        self.assertIn("table_schema='public' AND table_name='UserProfile'", seen[0])
        self.assertIn("""'"public"."UserProfile"'::regclass""", seen[1])

    def test_table_data_quotes_the_relation(self):
        seen = []

        async def fake_meta(db, schema, table):
            return {"columns": [{"name": "id"}, {"name": "displayName"}], "pk": ["id"]}

        async def fake_psql(sql, db=None, quiet=False):
            seen.append(sql)
            return "0" if "count(*)" in sql else "[]"

        with mock.patch.object(P, "table_meta", fake_meta), \
             mock.patch.object(P, "psql", fake_psql), \
             mock.patch.dict(P._row_counts, {}):
            got = _run(P.table_data("MyDb", "public", "UserProfile", 1, 20))
        self.assertEqual(got["total"], 0)
        joined = "\n".join(seen)
        self.assertIn('"public"."UserProfile"', joined)
        self.assertNotIn('"{', joined, "不能把花括号留在标识符里")
        self.assertIn('ORDER BY "id"', joined)

    def test_row_predicate_quotes_mixed_case_columns(self):
        pred = P._row_predicate({"id": 7}, ["id"])
        self.assertEqual(pred, '"id" = \'7\'')
        self.assertEqual(P._row_predicate({"_ctid": "(1,2)"}, []), "ctid = '(1,2)'::tid")


# ---------------------------------------------------------------- redis_del


class TestRedisDel(unittest.TestCase):
    def _capture(self, reply=1):
        seen = []

        async def fake_rp(cmds, kinds=None):
            seen.extend(cmds)
            return [reply] * len(cmds)

        return seen, mock.patch.object(P, "_rp", fake_rp)

    def test_long_key_is_not_silently_dropped(self):
        """512 那道旧过滤器让"选 3 删 1"看起来像成功；上限统一到 _check_key 的 2048。"""
        long_key = "k" * 600
        seen, patcher = self._capture()
        with patcher:
            got = _run(P.redis_del(["a", long_key]))
        self.assertEqual([c[1] for c in seen], ["a", long_key])
        self.assertEqual(got, {"deleted": 2})

    def test_over_limit_raises_instead_of_skipping(self):
        with self.assertRaises(RuntimeError):
            _run(P.redis_del(["k" * 2049]))

    def test_batch_cap(self):
        with self.assertRaises(RuntimeError):
            _run(P.redis_del([str(i) for i in range(201)]))

    def test_empty_key_raises(self):
        with self.assertRaises(RuntimeError):
            _run(P.redis_del([""]))

    def test_failed_unlink_is_not_counted(self):
        """UNLINK 回 0（key 已被别人删掉）不能报成删成功了——计数就是界面的"已删除 N 个"。"""
        seen = []

        async def fake_rp(cmds, kinds=None):
            seen.extend(cmds)
            return [1, 0]

        with mock.patch.object(P, "_rp", fake_rp):
            got = _run(P.redis_del(["a", "b"]))
        self.assertEqual(len(seen), 2, "两条都要真的送出去")
        self.assertEqual(got, {"deleted": 1})


# ---------------------------------------------------------------- nginx 生效文件


class TestNginxLoadedFiles(unittest.TestCase):
    """站点列表/配置读取只认 nginx 真正加载的文件。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cp_5j_nginx_")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.avail = f"{self.root}/sites-available"
        self.enabled = f"{self.root}/sites-enabled"
        self.confd = f"{self.root}/conf.d"
        for d in (self.avail, self.enabled, self.confd):
            os.makedirs(d)
        live = f"{self.avail}/blog.conf"
        draft = f"{self.avail}/staging.conf"
        for p in (live, draft):
            Path(p).write_text("server { server_name a.example.com; }\n")
        os.symlink(live, f"{self.enabled}/blog.conf")
        Path(f"{self.confd}/panel-app.conf").write_text("server {}\n")
        Path(f"{self.confd}/backup.bak").write_text("server {}\n")
        self._dirs = config.NGINX_CONF_DIRS
        config.NGINX_CONF_DIRS = [self.confd, self.avail]
        self.addCleanup(setattr, config, "NGINX_CONF_DIRS", self._dirs)

    def _names(self):
        return sorted(f for _p, f in nginx_ops._iter_conf_files())

    def test_draft_config_is_not_a_site(self):
        names = self._names()
        self.assertIn("blog.conf", names, "链接进 sites-enabled 的必须还在")
        self.assertNotIn("staging.conf", names, "没启用的草稿不算一个站点")

    def test_conf_d_honours_the_include_glob(self):
        names = self._names()
        self.assertIn("panel-app.conf", names)
        self.assertNotIn("backup.bak", names, "nginx.conf include 的是 conf.d/*.conf")

    def test_find_app_configs_skips_draft(self):
        hits = nginx_ops.find_app_configs(None, "a.example.com")
        self.assertEqual([os.path.basename(h["file"]) for h in hits], ["blog.conf"])

    def test_no_sites_enabled_dir_means_no_filtering(self):
        """判不出生效与否（RHEL 布局、目录名不同）就当全部生效：
        把"查不了"当成"没生效"会让真在服务的站点从列表里消失，比多列一个文件严重。"""
        shutil.rmtree(self.enabled)
        self.assertIn("staging.conf", self._names())

    def test_assert_app_conf_rejects_draft_with_a_clear_reason(self):
        draft = f"{self.avail}/staging.conf"
        with self.assertRaises(RuntimeError) as cm:
            nginx_ops.assert_app_conf(draft, None, "a.example.com")
        self.assertIn("sites-enabled", str(cm.exception))

    def test_assert_app_conf_allows_the_live_file(self):
        nginx_ops.assert_app_conf(f"{self.avail}/blog.conf", None, "a.example.com")
