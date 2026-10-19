"""本地直连播放的分段流：Range 解析与客户端断开时停止读盘。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from javsp_web import streaming  # noqa: E402


class RangeParseTests(unittest.TestCase):
    def test_common_range_forms(self):
        self.assertEqual(streaming.parse_range("bytes=0-99", 1000), (0, 99))
        self.assertEqual(streaming.parse_range("bytes=100-", 1000), (100, 999))
        self.assertEqual(streaming.parse_range("bytes=-50", 1000), (950, 999))
        self.assertEqual(streaming.parse_range("bytes=0-100000", 1000), (0, 999))

    def test_invalid_ranges(self):
        for value in ("", "items=0-1", "bytes=abc", "bytes=1000-", "bytes=-0", "bytes=5-1"):
            self.assertIsNone(streaming.parse_range(value, 1000), value)


class FakeRequest:
    """只实现流需要的两个属性。"""

    def __init__(self, header: str, disconnect_after: int | None = None):
        self.headers = {"range": header} if header else {}
        self._disconnect_after = disconnect_after
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._disconnect_after is not None and self._checks > self._disconnect_after

    def header(self, name, default=None):
        return self.headers.get(name.lower(), default)


class StreamResponseTests(unittest.TestCase):
    def test_streams_only_the_requested_range(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.mp4"
            target.write_bytes(b"0123456789" * 100)
            response = streaming.file_stream_response(FakeRequest("bytes=100-199"), target, tag="t1")
            self.assertEqual(response.status_code, 206)
            self.assertEqual(response.headers["content-range"], "bytes 100-199/1000")
            self.assertEqual(response.headers["content-length"], "100")

            async def collect():
                return b"".join([chunk async for chunk in response.body_iterator])

            data = asyncio.run(collect())
            self.assertEqual(len(data), 100)
            self.assertEqual(data, target.read_bytes()[100:200])

    def test_stops_reading_when_client_disconnects(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.mp4"
            target.write_bytes(b"x" * (streaming.CHUNK_SIZE * 4))
            request = FakeRequest("", disconnect_after=1)
            response = streaming.file_stream_response(request, target, tag="t2")

            async def collect():
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                return chunks

            chunks = asyncio.run(collect())
            self.assertLessEqual(len(chunks), 2, '客户端断开后应立刻停止读盘')
            self.assertLess(sum(len(chunk) for chunk in chunks), target.stat().st_size)

    def test_stop_stream_cancels_an_running_stream(self):
        """/playback/stop 能立刻叫停正在进行的本地流。"""
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.mp4"
            target.write_bytes(b"x" * (streaming.CHUNK_SIZE * 8))
            response = streaming.file_stream_response(FakeRequest(""), target, tag="cancel-1")

            async def drain():
                chunks = []
                async for chunk in response.body_iterator:
                    chunks.append(chunk)
                    if len(chunks) == 1:
                        self.assertTrue(streaming.stop_stream("cancel-1"))
                return chunks

            chunks = asyncio.run(drain())
            self.assertLessEqual(len(chunks), 2, '收到停止请求后应立刻结束')
            self.assertFalse(streaming.stop_stream("cancel-1"), '流结束后注册表应清理')

    def test_rejects_unsatisfiable_range(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "sample.mp4"
            target.write_bytes(b"x" * 10)
            response = streaming.file_stream_response(FakeRequest("bytes=100-200"), target, tag="t3")
            self.assertEqual(response.status_code, 416)


if __name__ == "__main__":
    unittest.main()
