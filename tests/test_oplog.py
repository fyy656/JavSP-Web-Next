"""运行日志：统一 handler、子 logger 命名与关键事件输出。"""
from __future__ import annotations

import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from javsp_web import media, oplog, streaming  # noqa: E402


class LoggerTests(unittest.TestCase):
    def test_child_loggers_share_one_handler(self):
        first = oplog.get("playback")
        second = oplog.get("scrape")
        self.assertEqual(first.name, "javsp_web.playback")
        self.assertEqual(second.name, "javsp_web.scrape")
        self.assertEqual(len(logging.getLogger("javsp_web").handlers), 1, '不应重复挂 handler')
        oplog.configure()  # 幂等
        self.assertEqual(len(logging.getLogger("javsp_web").handlers), 1)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


class PlaybackLogTests(unittest.TestCase):
    def test_transcode_start_is_logged(self):
        server = {"id": "s1", "name": "客厅 Emby", "type": "emby", "url": "http://h:8096", "api_key": "K"}
        item = {"Id": "9", "MediaSources": [{"Id": "src", "Container": "mkv"}]}
        playback = {"MediaSources": [{"Id": "src", "TranscodingUrl": "/videos/9/master.m3u8?api_key=K"}]}
        film = {"id": "f1", "dvdid": "ABC-123", "video_path": "D:/m/ABC-123/ABC-123.mkv"}
        with patch.object(media, "resolve_playback_item", return_value=(server, item)), \
                patch.object(media, "_playback_user_id", return_value="u1"), \
                patch.object(media.requests, "post", return_value=FakeResponse(playback)):
            with self.assertLogs("javsp_web.playback", level="INFO") as captured:
                media.film_playback(film, "emby", 30)
        text = "\n".join(captured.output)
        self.assertIn("开始转码播放", text)
        self.assertIn("ABC-123", text)
        self.assertIn("HLS", text)
        self.assertIn("起点=30", text)

    def test_stop_playback_is_logged(self):
        server = {"id": "s1", "name": "客厅 Emby", "type": "emby", "url": "http://h:8096", "api_key": "K"}
        with patch.object(media, "list_media_servers", return_value=[server]), \
                patch.object(media.requests, "delete", return_value=FakeResponse({}, 204)), \
                patch.object(media.requests, "post", return_value=FakeResponse({})):
            with self.assertLogs("javsp_web.playback", level="INFO") as captured:
                media.stop_playback("s1", "ps-1")
        self.assertIn("停止转码播放", "\n".join(captured.output))

    def test_direct_stream_logs_start_and_end(self):
        class FakeRequest:
            headers = {"range": "bytes=0-9"}
            client = type("C", (), {"host": "192.168.1.20"})()

            async def is_disconnected(self):
                return False

        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "ABC-123.mp4"
            target.write_bytes(b"0123456789" * 2)
            with self.assertLogs("javsp_web.playback", level="INFO") as captured:
                response = streaming.file_stream_response(FakeRequest(), target, tag="ABC-123")
                import asyncio

                async def drain():
                    return b"".join([chunk async for chunk in response.body_iterator])

                data = asyncio.run(drain())
        self.assertEqual(len(data), 10)
        text = "\n".join(captured.output)
        self.assertIn("开始本地直连播放", text)
        self.assertIn("客户端=192.168.1.20", text)
        self.assertIn("本地直连播放结束", text)


class ScrapeLogTests(unittest.TestCase):
    def test_scrape_logger_is_registered(self):
        from javsp_web import tasks

        self.assertEqual(tasks.scrape_logger.name, "javsp_web.scrape")


if __name__ == "__main__":
    unittest.main()
