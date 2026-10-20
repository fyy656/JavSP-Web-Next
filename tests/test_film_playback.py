"""媒体库播放：媒体服务器条目解析、播放地址生成与停止播放。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from javsp_web import media  # noqa: E402


SERVER = {"id": "s1", "name": "客厅 Emby", "type": "emby", "url": "http://192.168.1.9:8096/emby",
          "api_key": "KEY-123", "libraries": []}

FILM = {"id": "f1", "dvdid": "ABC-123", "dir_path": "D:/media/ABC-123",
        "video_path": "D:/media/ABC-123/ABC-123.mp4"}

ITEM = {"Id": "9", "Name": "ABC-123", "Path": "D:/media/ABC-123/ABC-123.mp4",
        "MediaSources": [{"Id": "src-9", "Container": "mp4"}]}


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


def patch_resolve(item=None, server=None):
    return patch.object(media, "resolve_playback_item",
                        return_value=(server or dict(SERVER), dict(item or ITEM)))


class FilmPlaybackTests(unittest.TestCase):
    def test_returns_transcoding_url_with_api_key(self):
        playback = {"MediaSources": [{"Id": "src-1", "Container": "mkv",
                                      "TranscodingUrl": "/videos/9/master.mp4?PlaySessionId=ps-1&MediaSourceId=src-1"}],
                    "PlaySessionId": "ps-1"}
        with patch_resolve(), \
                patch.object(media, "_playback_user_id", return_value="u1"), \
                patch.object(media.requests, "post", return_value=FakeResponse(playback)) as post:
            result = media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"))
        self.assertEqual(result["mode"], "emby")
        self.assertTrue(result["transcoding"])
        self.assertIn("api_key=KEY-123", result["url"])
        self.assertTrue(result["url"].startswith("http://192.168.1.9:8096/emby/videos/9/master.mp4?"))
        body = post.call_args.kwargs["json"]
        self.assertFalse(body["EnableDirectPlay"])
        self.assertFalse(body["EnableDirectStream"])
        # 第一档是 HLS(ts)，可拖动；第二档是渐进式 MP4 兜底
        self.assertEqual(body["DeviceProfile"]["TranscodingProfiles"][0]["Container"], "ts")
        self.assertEqual(body["DeviceProfile"]["TranscodingProfiles"][1]["Container"], "mp4")

    def test_falls_back_to_direct_stream_when_no_transcoding_needed(self):
        playback = {"MediaSources": [{"Id": "src-2", "Container": "mp4",
                                      "DirectStreamUrl": "/videos/9/stream?static=true&MediaSourceId=src-2"}]}
        with patch_resolve(), \
                patch.object(media, "_playback_user_id", return_value="u1"), \
                patch.object(media.requests, "post", return_value=FakeResponse(playback)):
            result = media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"))
        self.assertFalse(result["transcoding"])
        self.assertIn("/videos/9/stream", result["url"])
        self.assertIn("api_key=KEY-123", result["url"])

    def test_falls_back_to_static_stream_without_playback_info_urls(self):
        with patch_resolve(), \
                patch.object(media, "_playback_user_id", return_value=""), \
                patch.object(media.requests, "post", return_value=FakeResponse({"MediaSources": [{"Id": "src-3"}]})):
            result = media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"))
        self.assertIn("/videos/9/original.mp4", result["url"])
        self.assertIn("MediaSourceId=src-3", result["url"])
        self.assertIn("api_key=KEY-123", result["url"])

    def test_local_direct_play_for_browser_friendly_containers(self):
        """mp4/mov 这类容器直接本地直连，不调用 Emby。"""
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "ABC-123.mp4"
            video.write_bytes(b"x" * 16)
            film = dict(FILM, video_path=str(video))
            with patch.object(media, "resolve_playback_item") as resolve:
                result = media.film_playback(film)
        self.assertEqual(result["mode"], "local")
        self.assertFalse(result["transcoding"])
        self.assertEqual(result["url"], "/api/library/films/f1/stream")
        self.assertEqual(result["fallback_mode"], "emby")
        self.assertFalse(resolve.called, '本地能播时不应请求 Emby')

    def test_all_video_containers_try_direct_first(self):
        """avi/mkv 等也先直连，由浏览器决定能不能播，播不了再转码。"""
        for suffix in (".mkv", ".avi", ".wmv", ".m2ts"):
            with tempfile.TemporaryDirectory() as folder:
                video = Path(folder) / f"ABC-123{suffix}"
                video.write_bytes(b"x" * 16)
                film = dict(FILM, video_path=str(video))
                result = media.film_playback(film)
            self.assertEqual(result["mode"], "local", suffix)
            self.assertEqual(result["fallback_mode"], "emby", suffix)

    def test_prefer_emby_transcodes_any_container(self):
        playback = {"MediaSources": [{"Id": "s", "TranscodingUrl": "/videos/9/master.m3u8?api_key=K"}]}
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "ABC-123.mkv"
            video.write_bytes(b"x" * 16)
            film = dict(FILM, video_path=str(video))
            with patch_resolve(), \
                    patch.object(media, "_playback_user_id", return_value="u1"), \
                    patch.object(media.requests, "post", return_value=FakeResponse(playback)) as post:
                result = media.film_playback(film, "emby")
        self.assertEqual(result["mode"], "emby")
        self.assertTrue(result["transcoding"])
        self.assertTrue(result["hls"], 'HLS 地址要标记出来，播放器才能拖动进度条')
        # 转码优先 HLS，其次渐进式 MP4
        profiles = post.call_args.kwargs["json"]["DeviceProfile"]["TranscodingProfiles"]
        self.assertEqual(profiles[0]["Protocol"], "Hls")
        self.assertEqual(profiles[1]["Protocol"], "Http")

    def test_start_seconds_becomes_start_time_ticks(self):
        playback = {"MediaSources": [{"Id": "s", "TranscodingUrl": "/videos/9/master.m3u8?api_key=K"}]}
        with patch_resolve(), \
                patch.object(media, "_playback_user_id", return_value="u1"), \
                patch.object(media.requests, "post", return_value=FakeResponse(playback)) as post:
            result = media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"), "emby", 125)
        self.assertEqual(post.call_args.kwargs["json"]["StartTimeTicks"], 125 * 10_000_000)
        self.assertEqual(result["start_seconds"], 125)

    def test_prefer_local_forces_direct(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "ABC-123.mkv"
            video.write_bytes(b"x" * 16)
            film = dict(FILM, video_path=str(video))
            result = media.film_playback(film, "local")
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["fallback_mode"], '', '手动指定直连时不再自动跳转')

    def test_raises_when_no_media_server_item(self):
        with patch.object(media, "resolve_playback_item", return_value=None):
            with self.assertRaises(ValueError):
                media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"))

    def test_does_not_duplicate_api_key_from_server_url(self):
        playback = {"MediaSources": [{"Id": "src-4", "Container": "mp4",
                                      "TranscodingUrl": "/videos/9/stream.mp4?api_key=KEY-123&PlaySessionId=ps"}]}
        with patch_resolve(), \
                patch.object(media, "_playback_user_id", return_value="u1"), \
                patch.object(media.requests, "post", return_value=FakeResponse(playback)):
            result = media.film_playback(dict(FILM, video_path="D:/media/ABC-123/ABC-123.mkv"))
        self.assertEqual(result["url"].count("api_key="), 1, result["url"])

    def test_keeps_existing_query_parameters(self):
        url = media._with_api_key("http://h/emby/videos/1/master.mp4?PlaySessionId=ps", "K")
        self.assertIn("PlaySessionId=ps", url)
        self.assertTrue(url.endswith("&api_key=K"))

    def test_stop_playback_cancels_the_transcode_session(self):
        with patch.object(media, "list_media_servers", return_value=[dict(SERVER)]), \
                patch.object(media.requests, "delete", return_value=FakeResponse({}, 204)) as delete, \
                patch.object(media.requests, "post", return_value=FakeResponse({})) as post:
            result = media.stop_playback("s1", "ps-9")
        self.assertTrue(result["ok"])
        self.assertEqual(delete.call_args.kwargs["params"]["PlaySessionId"], "ps-9")
        self.assertTrue(str(delete.call_args.args[0]).endswith("/Videos/ActiveEncodings"))
        self.assertTrue(str(post.call_args.args[0]).endswith("/Sessions/Playing/Stopped"))


class ResolvePlaybackItemTests(unittest.TestCase):
    """播放定位只认文件路径与文件名：标题里没有番号也能命中。"""

    def setUp(self):
        media._RESOLVED_CACHE.clear()
        media._PATH_INDEX_CACHE.clear()
        self.addCleanup(media._RESOLVED_CACHE.clear)
        self.addCleanup(media._PATH_INDEX_CACHE.clear)

    def _resolve(self, by_path, index=None, film=None, servers=None):
        with patch.object(media, "list_media_servers", return_value=servers or [dict(SERVER)]), \
                patch.object(media, "_items_by_path", return_value=list(by_path)) as by_path_mock, \
                patch.object(media, "_library_path_index", return_value=dict(index or {})) as index_mock:
            result = media.resolve_playback_item(film or dict(FILM))
        return result, by_path_mock, index_mock

    def test_path_hit_works_even_when_title_has_no_dvdid(self):
        item = {"Id": "1", "Name": "NATURAL BEAUTY", "Path": "D:/media/ABC-123/ABC-123.mp4"}
        result, by_path, index = self._resolve([item])
        self.assertIsNotNone(result)
        self.assertEqual(result[1]["Id"], "1")
        self.assertTrue(by_path.called)
        self.assertFalse(index.called, '路径命中时不需要建索引')

    def test_falls_back_to_index_path_when_query_misses(self):
        item = {"Id": "2", "Name": "无番号标题", "Path": "D:/media/ABC-123/ABC-123.mp4"}
        index = {media._normalize_path(item["Path"]): item}
        result, _, index_mock = self._resolve([], index=index)
        self.assertIsNotNone(result)
        self.assertEqual(result[1]["Id"], "2")
        self.assertTrue(index_mock.called)

    def test_matches_by_filename_across_drives(self):
        item = {"Id": "3", "Name": "whatever", "Path": "F:/media/ABC-123/ABC-123.mp4"}
        index = {media._normalize_path(item["Path"]): item}
        result, _, _ = self._resolve([], index=index)
        self.assertIsNotNone(result)
        self.assertEqual(result[1]["Id"], "3")

    def test_ambiguous_filename_is_ignored(self):
        first = {"Id": "4", "Name": "a", "Path": "F:/a/ABC-123.mp4"}
        second = {"Id": "5", "Name": "b", "Path": "F:/b/ABC-123.mp4"}
        index = {media._normalize_path(first["Path"]): first, media._normalize_path(second["Path"]): second}
        result, _, _ = self._resolve([], index=index)
        self.assertIsNone(result)

    def test_no_candidates(self):
        result, _, _ = self._resolve([])
        self.assertIsNone(result)

    def test_result_is_cached(self):
        item = {"Id": "6", "Name": "x", "Path": "D:/media/ABC-123/ABC-123.mp4"}
        with patch.object(media, "list_media_servers", return_value=[dict(SERVER)]), \
                patch.object(media, "_items_by_path", return_value=[item]) as by_path:
            media.resolve_playback_item(dict(FILM))
            media.resolve_playback_item(dict(FILM))
        self.assertEqual(by_path.call_count, 1, '重复播放同一部影片应命中缓存')


if __name__ == "__main__":
    unittest.main()
