"""静态产物的预压缩协商（批次④A）。

首屏四个文件原样传输是 956KB，构建期产出的 .gz 合计 273KB。这里锁住三件事：
命中 .gz、显式拒绝 gzip 时回原文件、以及两种编码共用同一份 ETag（否则客户端带旧
ETag 来验证会整份重传）。运行时压缩不能用于 /api 的 text/event-stream，所以压缩
只发生在构建期（web/scripts/gzip-assets.mjs），这里只测"读哪份文件"。
"""
import gzip
import os
import tempfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("CP_DATA_DIR", tempfile.mkdtemp(prefix="cp_static_gzip_data_"))
os.environ.setdefault("CP_FILE_ROOTS", tempfile.mkdtemp(prefix="cp_static_gzip_roots_"))
from app.main import DistFiles

RAW = b"RAW-JAVASCRIPT-" * 400
PACKED = b"GZIP-PAYLOAD-DIFFERENT-CONTENT-" * 90


class StaticEncodingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "assets").mkdir()
        (root / "assets" / "a.js").write_bytes(RAW)
        (root / "assets" / "a.js.gz").write_bytes(gzip.compress(PACKED, mtime=0))
        (root / "index.html").write_text("<html>ok</html>", encoding="utf-8")
        app = FastAPI()
        app.mount("/", DistFiles(directory=str(root), html=True), name="static")
        self.client = TestClient(app)

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self, url, accept):
        return self.client.get(url, headers={"Accept-Encoding": accept})

    def test_gzip_client_gets_the_precompressed_copy(self):
        r = self._get("/assets/a.js", "gzip, deflate, br")
        self.assertEqual(r.headers["content-encoding"], "gzip")
        # httpx 会替浏览器解掉 content-encoding，所以比正文等于 .gz 里装的东西
        self.assertEqual(r.content, PACKED)
        self.assertNotEqual(r.content, RAW)

    def test_media_type_stays_javascript(self):
        """不能因为文件是 a.js.gz 就回 octet-stream：浏览器会拒绝执行。"""
        r = self._get("/assets/a.js", "gzip")
        self.assertTrue(r.headers["content-type"].startswith("text/javascript"), r.headers["content-type"])

    def test_clients_without_gzip_get_the_raw_file(self):
        r = self._get("/assets/a.js", "identity")
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.content, RAW)

    def test_explicit_gzip_rejection_is_honoured(self):
        """`gzip;q=0` 是显式不要 gzip，只 grep 子串会把原文件错误地压给客户端。"""
        r = self._get("/assets/a.js", "deflate, gzip;q=0")
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.content, RAW)

    def test_vary_and_immutable_survive_the_switch(self):
        r = self._get("/assets/a.js", "gzip")
        self.assertIn("Accept-Encoding", r.headers["vary"])
        self.assertEqual(r.headers["cache-control"], "public, max-age=31536000, immutable")
        raw = self._get("/assets/a.js", "identity")
        self.assertIn("Accept-Encoding", raw.headers["vary"], "未压缩的那份也要 Vary，共享缓存才不会串")

    def test_etag_is_the_same_representation_version_for_both_encodings(self):
        with_gz = self._get("/assets/a.js", "gzip")
        without = self._get("/assets/a.js", "identity")
        self.assertEqual(with_gz.headers["etag"], without.headers["etag"])

    def test_conditional_request_still_answers_304(self):
        first = self._get("/assets/a.js", "gzip")
        again = self.client.get(
            "/assets/a.js", headers={"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]}
        )
        self.assertEqual(again.status_code, 304)

    def test_missing_copy_falls_back_to_raw(self):
        (Path(self.tmp.name) / "assets" / "a.js.gz").unlink()
        r = self._get("/assets/a.js", "gzip")
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.content, RAW)

    def test_html_is_never_precompressed(self):
        """HTML 是 no-cache 的，多一轮协商只增开销；构建脚本也不会给它产 .gz。"""
        (Path(self.tmp.name) / "index.html.gz").write_bytes(gzip.compress(b"SHOULD-NOT-BE-USED"))
        r = self.client.get("/", headers={"Accept-Encoding": "gzip"})
        self.assertNotIn("content-encoding", r.headers)
        self.assertEqual(r.headers["cache-control"], "no-cache")
        self.assertEqual(r.text, "<html>ok</html>")

    def test_requesting_the_gz_path_directly_does_not_double_wrap(self):
        r = self._get("/assets/a.js.gz", "gzip")
        self.assertNotIn("content-encoding", r.headers)
        # gzip 头里带 mtime：不锁 mtime 时，setUp 写入的那份与本行重新压缩的那份会因跨秒而字节不同
        # （CI 比本机慢，几乎每次都在断言里重压一次），把它做成"命中就逐字节等于盘上那份"的可重复断言。
        self.assertEqual(r.content, gzip.compress(PACKED, mtime=0))


if __name__ == "__main__":
    unittest.main()
