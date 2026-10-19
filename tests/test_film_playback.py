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
        self.assertEqual(body["DeviceProfile"]["TranscodingProfiles"][0]["Container"], "mp4")

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

    def test_non_browser_container_falls_back_to_emby(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "ABC-123.mkv"
            video.write_bytes(b"x" * 16)
            film = dict(FILM, video_path=str(video))
            playback = {"MediaSources": [{"Id": "s", "TranscodingUrl": "/videos/9/stream.mp4?api_key=K"}]}
            with patch_resolve(), \
                    patch.object(media, "_playback_user_id", return_value="u1"), \
                    patch.object(media.requests, "post", return_value=FakeResponse(playback)):
                result = media.film_playback(film)
        self.assertEqual(result["mode"], "emby")
        self.assertTrue(result["transcoding"])

    def test_prefer_emby_skips_local(self):
        with tempfile.TemporaryDirectory() as folder:
            video = Path(folder) / "ABC-123.mp4"
            video.write_bytes(b"x" * 16)
            film = dict(FILM, video_path=str(video))
            playback = {"MediaSources": [{"Id": "s", "TranscodingUrl": "/videos/9/stream.mp4?api_key=K"}]}
            with patch_resolve(), \
                    patch.object(media, "_playback_user_id", return_value="u1"), \
                    patch.object(media.requests, "post", return_value=FakeResponse(playback)):
                result = media.film_playback(film, "emby")
        self.assertEqual(result["mode"], "emby")
        self.assertIn("/api/library/films/f1/stream", result["fallback_url"])

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
    """播放查找不受“已勾选媒体库”限制，先路径、再番号。"""

    def _run(self, items, film=None):
        # 播放查找不应依赖路径映射：这里故意不让 media 去读映射配置
        with patch.object(media, "list_media_servers", return_value=[dict(SERVER)]), \
                patch.object(media, "list_path_mappings", side_effect=AssertionError("播放不应读取路径映射")), \
                patch.object(media, "_search_movies", return_value=items), \
                patch.object(media, "_playback_headers", return_value={}):
            return media.resolve_playback_item(film or dict(FILM))

    def test_matches_by_path_even_if_library_not_selected(self):
        items = [{"Id": "1", "Name": "ABC-123", "Path": "D:/media/ABC-123/ABC-123.mp4"},
                 {"Id": "2", "Name": "ABC-124", "Path": "D:/media/ABC-124/ABC-124.mp4"}]
        resolved = self._run(items)
        self.assertIsNotNone(resolved)
        server, item = resolved
        self.assertEqual(item["Id"], "1")
        self.assertEqual(server["id"], "s1")

    def test_matches_same_file_name_on_another_drive(self):
        """盘符/挂载点不同但文件名一致：直接认定是同一部影片，不需要路径映射。"""
        items = [{"Id": "3", "Name": "ABC-123", "Path": "F:/media/ABC-123/ABC-123.mp4"}]
        resolved = self._run(items)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[1]["Id"], "3")

    def test_matches_unique_dvdid_when_file_name_changed(self):
        items = [{"Id": "6", "Name": "ABC-123 标题", "Path": "F:/x/abc_hd_123_full.mp4"}]
        resolved = self._run(items)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[1]["Id"], "6")

    def test_same_name_parts_are_disambiguated_by_dvdid(self):
        items = [{"Id": "7", "Name": "ABC-123", "Path": "F:/a/ABC-123/ABC-123-A.avi"},
                 {"Id": "8", "Name": "XYZ-999", "Path": "F:/b/XYZ-999/ABC-123.mp4"}]
        film = dict(FILM, video_path="D:/media/ABC-123/ABC-123.mp4")
        resolved = self._run(items, film)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[1]["Id"], "8")

    def test_ambiguous_results_are_ignored(self):
        items = [{"Id": "4", "Name": "ABC-123", "Path": "F:/a/ABC-123.mp4"},
                 {"Id": "5", "Name": "ABC-123", "Path": "F:/b/ABC-123.mp4"}]
        self.assertIsNone(self._run(items))

    def test_no_results(self):
        self.assertIsNone(self._run([]))


if __name__ == "__main__":
    unittest.main()
