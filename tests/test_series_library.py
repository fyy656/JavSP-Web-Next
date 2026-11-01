"""剧集库：目录模型、季集识别、NFO 优先、三级浏览接口。

剧集库与电影库共用 `films` 表（kind = movie / series / episode），
所以这里既验证纯解析函数，也验证扫描落库和 HTTP 接口。
"""
from __future__ import annotations

import copy
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

from javsp_web import library_store, series_scan, storage  # noqa: E402

VIDEO_BYTES = b'\x00' * 4096


def touch(path: Path, data: bytes = VIDEO_BYTES) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class SchemaMigrationTests(unittest.TestCase):
    """老库升级：main 上的 library.db 没有 kind/parent_id 等列，打开时必须自动补齐。"""

    OLD_FILMS = """
    CREATE TABLE films (
        id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        dir_path TEXT NOT NULL, video_path TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]',
        size_bytes INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
        dvdid TEXT NOT NULL DEFAULT '', dvdid_source TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '', original_title TEXT NOT NULL DEFAULT '',
        plot TEXT NOT NULL DEFAULT '', release_date TEXT NOT NULL DEFAULT '',
        studio TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
        director TEXT NOT NULL DEFAULT '', runtime INTEGER NOT NULL DEFAULT 0,
        actress_text TEXT NOT NULL DEFAULT '',
        nfo_path TEXT NOT NULL DEFAULT '', nfo_mtime REAL NOT NULL DEFAULT 0,
        cover_path TEXT NOT NULL DEFAULT '', fanart_count INTEGER NOT NULL DEFAULT 0,
        extrafanart_dir TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unscraped', missing_fields TEXT NOT NULL DEFAULT '[]',
        scrape_state TEXT NOT NULL DEFAULT 'idle', last_task_id TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    );
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_file = self.root / 'state' / 'library.db'
        self.db_file.parent.mkdir(parents=True)
        import sqlite3
        db = sqlite3.connect(self.db_file)
        db.executescript("""
        CREATE TABLE libraries (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, preset_id TEXT NOT NULL DEFAULT 'default',
            created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE roots (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
            preset_id TEXT NOT NULL DEFAULT 'default', recursive INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE library_roots (
            library_id TEXT NOT NULL, root_id TEXT NOT NULL, added_at TEXT NOT NULL,
            PRIMARY KEY (library_id, root_id)
        );
        CREATE TABLE film_actress (
            film_id TEXT NOT NULL, name TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (film_id, name)
        );
        CREATE TABLE scans (
            id TEXT PRIMARY KEY, root_id TEXT NOT NULL, status TEXT NOT NULL,
            discovered INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
        );
        """)
        db.executescript(self.OLD_FILMS)
        db.execute("INSERT INTO libraries(id,name,preset_id,created_at) VALUES('lib1','老库','default','2026-01-01')")
        db.execute("INSERT INTO roots(id,name,path,created_at) VALUES('root1','老目录','C:/media','2026-01-01')")
        db.execute("INSERT INTO library_roots(library_id,root_id,added_at) VALUES('lib1','root1','2026-01-01')")
        db.execute("INSERT INTO films(id,root_id,dir_path,video_path,files,updated_at,title,dvdid) "
                   "VALUES('f1','root1','C:/media/A','C:/media/A/A.mp4','[]','2026-01-01','老影片','ABC-123')")
        db.commit()
        db.close()
        self.enterContext(patch.object(library_store, 'LIBRARY_DB_FILE', self.db_file))

    def test_old_database_is_migrated_in_place(self):
        import sqlite3
        # 打开老库：补列 + 建索引，不能抛 "no such column: parent_id"
        libraries = library_store.list_libraries()
        self.assertEqual([item['name'] for item in libraries], ['老库'])
        self.assertEqual(libraries[0]['kind'], 'movie', '老库默认按电影库处理')

        db = sqlite3.connect(self.db_file)
        columns = {row[1] for row in db.execute('PRAGMA table_info(films)')}
        indexes = {row[1] for row in db.execute('PRAGMA index_list(films)')}
        db.close()
        self.assertTrue({'kind', 'parent_id', 'season', 'episode'} <= columns)
        self.assertIn('films_parent', indexes)

        # 老数据还在，且默认是普通影片
        page = library_store.list_films('lib1')
        self.assertEqual([item['title'] for item in page['items']], ['老影片'])
        self.assertEqual(page['items'][0]['kind'], 'movie')

    def test_migration_is_idempotent(self):
        library_store.list_libraries()
        library_store._reset_ready()
        self.assertEqual(library_store.list_libraries()[0]['name'], '老库')


class SeriesParseTests(unittest.TestCase):
    """季目录名与文件名里的季集识别。"""

    def test_season_dir_names(self):
        cases = {
            'S01': 1, 's2': 2, 'Season 1': 1, 'season.03': 3, '第1季': 1,
            '第一季': 1, '第十二季': 12, '2nd Season': 2, '特别篇': 0, 'SP': 0,
        }
        for name, expected in cases.items():
            self.assertEqual(series_scan.season_from_dir_name(name), expected, name)
        # 不是季目录：剧名里带 S01 时它本身就是剧集目录
        for name in ('Show.S01', '国产剧', '第一集', 'Season'):
            self.assertIsNone(series_scan.season_from_dir_name(name), name)

    def test_episode_markers(self):
        cases = {
            'Show.S01E02': (1, 2),
            'show.s1.e12': (1, 12),
            'Show.S02 EP03': (2, 3),
            'Show.Season 3 Episode 4': (3, 4),
            '某剧.第12集': (0, 12),
            '某剧 第十二集': (0, 12),
            '某剧 第二季第三集': (2, 3),
            '某剧 EP05': (0, 5),
            '01': (0, 1),
            '02 - 标题': (0, 2),
        }
        for name, expected in cases.items():
            self.assertEqual(series_scan.parse_episode_marker(name), expected, name)

    def test_episode_marker_ignores_unrelated_numbers(self):
        for name in ('Movie.2019.1080p', 'abc-123', '123456-001', '剧名'):
            self.assertEqual(series_scan.parse_episode_marker(name), (0, 0), name)

    def test_season_only_in_filename(self):
        self.assertEqual(series_scan.season_from_text('Show.S02.第05集'), 2)
        self.assertEqual(series_scan.season_from_text('剧名.S03'), 3)
        self.assertEqual(series_scan.season_from_text('第二季 第01集'), 2)
        self.assertEqual(series_scan.season_from_text('Movie.2019.1080p'), 0)

    def test_clean_series_title(self):
        self.assertEqual(series_scan.clean_series_title('Show.S01.1080p.WEB-DL.x265'), 'Show')
        self.assertEqual(series_scan.clean_series_title('风起长林'), '风起长林')


class SeriesScanTests(unittest.TestCase):
    """扫描落库：目录 → 剧 → 季 → 集。"""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        old = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(old):
                self.enterContext(patch.object(storage, name, self.root / 'state' / value.relative_to(old)))
        self.enterContext(patch.object(library_store, 'LIBRARY_DB_FILE', self.root / 'state' / 'library.db'))
        # 扫描会读预设的最小文件大小；测试用的小文件需要把阈值设为 0
        storage.ensure_seed_data()
        preset = storage.get_preset('default') or {}
        form = copy.deepcopy(preset.get('form') or {})
        form.setdefault('scanner', {})['minimum_size'] = 0
        storage.save_preset({**preset, 'id': 'default', 'form': form})
        library_store._preset_scanner_cache.clear()
        self.addCleanup(library_store._preset_scanner_cache.clear)

    def _library(self, kind: str = 'series', preset: str = 'default') -> dict:
        return library_store.add_library('测试库', preset, [{'path': str(self.media)}], kind)

    def _wait(self, scan_id: str, timeout: float = 10) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = library_store.get_scan(scan_id)
            if state and state['status'] in ('succeeded', 'failed'):
                # 状态落库后线程还要收尾（关闭连接），等它退出再让测试删临时目录
                while scan_id in library_store._scan_state and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(state['status'], 'succeeded', state)
                return state
            time.sleep(0.05)
        self.fail('扫描超时')

    def _scan(self, kind: str = 'series') -> dict:
        library = self._library(kind)
        self._wait(library_store.start_scan(library['id'])['id'])
        return library

    def _series(self, library_id: str) -> list[dict]:
        return library_store.list_films(library_id, kind='series', limit=200)['items']

    def _episodes(self, library_id: str, parent: str, season: int | None = None) -> list[tuple[int, int]]:
        items = library_store.list_films(library_id, parent=parent, season=season, limit=200)['items']
        return sorted((item['season'], item['episode']) for item in items)

    def test_flat_folder_is_one_series(self):
        for index in (1, 2, 3):
            touch(self.media / '剧A' / f'剧A.S01E0{index}.mkv')
        library = self._scan()
        series = self._series(library['id'])
        self.assertEqual([item['title'] for item in series], ['剧A'])
        self.assertEqual(self._episodes(library['id'], series[0]['id']), [(1, 1), (1, 2), (1, 3)])
        self.assertEqual(library_store.get_library(library['id'])['film_count'], 1,
                         '剧集库的条目数按“部”算，不把分集算进去')

    def test_season_folders_split_by_season(self):
        touch(self.media / '剧B' / 'S01' / 'B.S01E01.mkv')
        touch(self.media / '剧B' / 'S01' / 'B.S01E02.mkv')
        touch(self.media / '剧B' / 'S02' / 'B.S02E01.mkv')
        library = self._scan()
        series = self._series(library['id'])
        self.assertEqual(len(series), 1, '季目录不能各自变成一部剧')
        self.assertEqual(self._episodes(library['id'], series[0]['id']), [(1, 1), (1, 2), (2, 1)])
        self.assertEqual(library_store.series_seasons(series[0]['id']),
                         [{'season': 1, 'episodes': 2}, {'season': 2, 'episodes': 1}])

    def test_chinese_season_folders(self):
        touch(self.media / '剧C' / '第一季' / '第01集.mkv')
        touch(self.media / '剧C' / '第一季' / '第02集.mkv')
        touch(self.media / '剧C' / '第二季' / '第一集.mkv')
        library = self._scan()
        series = self._series(library['id'])
        self.assertEqual(self._episodes(library['id'], series[0]['id']), [(1, 1), (1, 2), (2, 1)])

    def test_nfo_wins_over_filename(self):
        touch(self.media / '剧D' / '随便起的名字.mkv')
        (self.media / '剧D' / '随便起的名字.nfo').write_text(
            '<episodedetails><title>NFO 标题</title><season>3</season><episode>7</episode>'
            '<plot>NFO 简介</plot><runtime>45</runtime></episodedetails>', encoding='utf-8')
        (self.media / '剧D' / 'tvshow.nfo').write_text(
            '<tvshow><title>剧D 正式名</title><year>2024</year><plot>剧集简介</plot></tvshow>', encoding='utf-8')
        library = self._scan()
        series = self._series(library['id'])[0]
        self.assertEqual(series['title'], '剧D 正式名')
        self.assertEqual(series['plot'], '剧集简介')
        self.assertEqual(series['release_date'], '2024')
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        self.assertEqual((episode['season'], episode['episode']), (3, 7))
        self.assertEqual(episode['title'], 'NFO 标题')
        self.assertEqual(episode['plot'], 'NFO 简介')
        self.assertEqual(episode['runtime'], 45)

    def test_season_nfo_number_overrides_folder_name(self):
        touch(self.media / '剧E' / '第一季' / 'E01.mkv')
        (self.media / '剧E' / '第一季' / 'season.nfo').write_text(
            '<season><seasonnumber>5</seasonnumber><title>第五季</title></season>', encoding='utf-8')
        library = self._scan()
        series = self._series(library['id'])[0]
        self.assertEqual(self._episodes(library['id'], series['id']), [(5, 1)])

    def test_unnumbered_files_get_sequential_numbers(self):
        for name in ('c', 'a', 'b'):
            touch(self.media / '剧F' / f'{name}.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        items = library_store.list_films(library['id'], parent=series['id'])['items']
        self.assertEqual([(item['season'], item['episode']) for item in items], [(1, 1), (1, 2), (1, 3)])
        self.assertEqual([item['title'] for item in items], ['a', 'b', 'c'], '按文件名顺序编号')

    def test_trailer_skipped_when_episode_numbers_exist(self):
        touch(self.media / '剧G' / 'G.S01E01.mkv')
        touch(self.media / '剧G' / 'G.trailer.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        self.assertEqual(self._episodes(library['id'], series['id']), [(1, 1)])

    def test_nested_category_folder_still_finds_series(self):
        touch(self.media / '分类X' / '剧H' / 'S01' / 'H.S01E01.mkv')
        touch(self.media / '分类X' / '剧I' / 'I.S01E01.mkv')
        library = self._scan()
        names = sorted(item['title'] for item in self._series(library['id']))
        self.assertEqual(names, ['剧H', '剧I'])

    def test_bracketed_numbers_are_episodes(self):
        """《秘密教学》 (10) 这种括号编号，且按自然顺序而不是字符串顺序排。"""
        for index in (1, 2, 10, 11):
            touch(self.media / '剧T' / f'《秘密教学》  ({index}).mp4')
        library = self._scan()
        series = self._series(library['id'])[0]
        items = library_store.list_films(library['id'], parent=series['id'])['items']
        self.assertEqual([(item['season'], item['episode']) for item in items],
                         [(1, 1), (1, 2), (1, 10), (1, 11)])

    def test_chinese_bracket_numbers_are_episodes(self):
        touch(self.media / '剧U' / '某系列（二）旗袍.mp4')
        touch(self.media / '剧U' / '某系列（三）情趣.mp4')
        touch(self.media / '剧U' / '某系列.mp4')
        library = self._scan()
        series = self._series(library['id'])[0]
        items = library_store.list_films(library['id'], parent=series['id'])['items']
        # （二）=2、（三）=3，没写集号的补到最小空闲号 1
        self.assertEqual([(item['episode'], item['title']) for item in items],
                         [(1, '某系列'), (2, '某系列（二）旗袍'), (3, '某系列（三）情趣')])

    def test_unnumbered_files_are_kept_next_to_numbered_ones(self):
        """季内已有集号时，没写集号的正片不能丢，花絮才跳过。"""
        touch(self.media / '剧V' / 'V.S01E01.mp4')
        touch(self.media / '剧V' / 'V.S01E02.mp4')
        touch(self.media / '剧V' / 'V 特别篇.mp4')
        touch(self.media / '剧V' / 'V trailer.mp4')
        touch(self.media / '剧V' / 'V 花絮.mp4')
        library = self._scan()
        series = self._series(library['id'])[0]
        items = library_store.list_films(library['id'], parent=series['id'])['items']
        self.assertEqual([item['episode'] for item in items], [1, 2, 3])
        self.assertEqual([item['title'] for item in items][-1], 'V 特别篇')

    def test_playback_progress_is_recorded(self):
        touch(self.media / '剧W' / 'W.S01E01.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        self.assertEqual(episode['progress']['position'], 0)

        saved = library_store.save_progress(episode['id'], 300, 1800)
        self.assertAlmostEqual(saved['position'], 300)
        self.assertFalse(saved['finished'])
        self.assertAlmostEqual(saved['percent'], 16.7, places=1)

        # 列表和详情都要带上进度，前端才能画进度条
        listed = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        self.assertAlmostEqual(listed['progress']['position'], 300)
        self.assertAlmostEqual(library_store.get_film(episode['id'])['progress']['position'], 300)

    def test_finished_when_near_the_end(self):
        touch(self.media / '剧X' / 'X.S01E01.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        self.assertTrue(library_store.save_progress(episode['id'], 1750, 1800)['finished'], '看到 97% 算看完')
        self.assertTrue(library_store.save_progress(episode['id'], 1740, 1800)['finished'], '离结尾 60 秒内也算看完')
        self.assertFalse(library_store.save_progress(episode['id'], 600, 1800)['finished'])

    def test_series_progress_counts_watched_and_points_at_next(self):
        for index in (1, 2, 3):
            touch(self.media / '剧Y' / f'Y.S01E0{index}.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        episodes = library_store.list_films(library['id'], parent=series['id'])['items']
        library_store.save_progress(episodes[0]['id'], 1800, 1800)   # 看完
        library_store.save_progress(episodes[1]['id'], 120, 1800)    # 看了一半

        progress = library_store.series_progress(series['id'])
        self.assertEqual(progress['total'], 3)
        self.assertEqual(progress['watched'], 1)
        self.assertEqual(progress['resume']['episode'], 2, '继续观看要指向没看完的那一集')
        self.assertAlmostEqual(progress['resume']['position'], 120)

    def test_progress_can_be_cleared(self):
        touch(self.media / '剧Z' / 'Z.S01E01.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        library_store.save_progress(episode['id'], 200, 1000)
        self.assertTrue(library_store.clear_progress(episode['id']))
        self.assertEqual(library_store.get_film(episode['id'])['progress']['position'], 0)
        self.assertFalse(library_store.clear_progress(episode['id']), '没有记录时返回 False')

    def test_progress_is_dropped_with_the_film(self):
        touch(self.media / '剧AA' / 'AA.S01E01.mkv')
        library = self._library()
        self._wait(library_store.start_scan(library['id'])['id'])
        series = self._series(library['id'])[0]
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        library_store.save_progress(episode['id'], 200, 1000)
        (self.media / '剧AA' / 'AA.S01E01.mkv').unlink()
        self._wait(library_store.start_scan(library['id'])['id'])
        import sqlite3
        db = sqlite3.connect(library_store.LIBRARY_DB_FILE)
        left = db.execute('SELECT count(*) FROM film_progress WHERE film_id=?', (episode['id'],)).fetchone()[0]
        db.close()
        self.assertEqual(left, 0, '影片从索引移除后，进度记录要跟着删掉')

    def test_rescan_removes_deleted_episodes(self):
        touch(self.media / '剧J' / 'S01' / 'J.S01E01.mkv')
        touch(self.media / '剧J' / 'S01' / 'J.S01E02.mkv')
        touch(self.media / '剧K' / 'K.S01E01.mkv')
        library = self._library()
        self._wait(library_store.start_scan(library['id'])['id'])
        (self.media / '剧J' / 'S01' / 'J.S01E02.mkv').unlink()
        import shutil
        shutil.rmtree(self.media / '剧K')
        self._wait(library_store.start_scan(library['id'])['id'])
        self.assertEqual([item['title'] for item in self._series(library['id'])], ['剧J'])
        self.assertEqual(self._episodes(library['id'], self._series(library['id'])[0]['id']), [(1, 1)])

    def test_movie_library_never_produces_episodes(self):
        touch(self.media / 'Movie.2024' / 'Movie.2024.mkv')
        touch(self.media / 'Show.S01' / 'Show.S01E01.mkv')
        touch(self.media / 'Show.S01' / 'Show.S01E02.mkv')
        library = self._scan('movie')
        items = library_store.list_films(library['id'], limit=50)['items']
        self.assertTrue(items)
        self.assertTrue(all(item['kind'] == 'movie' for item in items),
                        '电影库不会产生 series/episode 条目')
        self.assertEqual(library_store.series_stats(library['id']), {})

    def test_refresh_series_picks_up_new_episode_and_nfo(self):
        touch(self.media / '剧M' / 'M.S01E01.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        touch(self.media / '剧M' / 'M.S01E02.mkv')
        (self.media / '剧M' / 'tvshow.nfo').write_text('<tvshow><title>刷新后的名字</title></tvshow>',
                                                       encoding='utf-8')
        library_store.refresh_series(series['id'])
        self.assertEqual(library_store.get_film(series['id'])['title'], '刷新后的名字')
        self.assertEqual(len(library_store.list_films(library['id'], parent=series['id'])['items']), 2)

    def test_series_cannot_be_scraped(self):
        touch(self.media / '剧N' / 'N.S01E01.mkv')
        library = self._scan()
        series = self._series(library['id'])[0]
        episode = library_store.list_films(library['id'], parent=series['id'])['items'][0]
        for film_id in (series['id'], episode['id']):
            with self.assertRaises(ValueError):
                library_store.start_scrape(film_id)


class SeriesApiTests(SeriesScanTests):
    """走 HTTP 接口验证三级浏览：库 → 剧 → 季 → 集。"""

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(storage, 'DATA_DIR', self.root / 'state'))
        from javsp_web import server
        self.server = server
        self.enterContext(patch.dict(server.app.dependency_overrides,
                                     {server.current_user: lambda: {'username': 'test', 'role': 'admin'}}))
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    # 父类里的用例在 HTTP 层再跑一遍没有意义，这里只保留 API 专属用例
    test_flat_folder_is_one_series = None
    test_season_folders_split_by_season = None
    test_chinese_season_folders = None
    test_nfo_wins_over_filename = None
    test_season_nfo_number_overrides_folder_name = None
    test_unnumbered_files_get_sequential_numbers = None
    test_trailer_skipped_when_episode_numbers_exist = None
    test_nested_category_folder_still_finds_series = None
    test_rescan_removes_deleted_episodes = None
    test_movie_library_never_produces_episodes = None
    test_bracketed_numbers_are_episodes = None
    test_chinese_bracket_numbers_are_episodes = None
    test_unnumbered_files_are_kept_next_to_numbered_ones = None
    test_playback_progress_is_recorded = None
    test_finished_when_near_the_end = None
    test_series_progress_counts_watched_and_points_at_next = None
    test_progress_can_be_cleared = None
    test_progress_is_dropped_with_the_film = None
    test_refresh_series_picks_up_new_episode_and_nfo = None
    test_series_cannot_be_scraped = None

    def test_create_series_library_and_browse(self):
        for index in (1, 2):
            touch(self.media / '剧A' / 'S01' / f'A.S01E0{index}.mkv')
        touch(self.media / '剧A' / '第二季' / '第一集.mkv')
        created = self.client.post('/api/library/libraries', json={
            'name': '剧集库', 'kind': 'series',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        })
        self.assertEqual(created.status_code, 201, created.text)
        library = created.json()['library']
        self.assertEqual(library['kind'], 'series')
        self.scan_library(library['id'])

        payload = self.client.get(f"/api/library/films?library_id={library['id']}&kind=series").json()
        self.assertEqual(payload['total'], 1)
        series = payload['items'][0]
        self.assertEqual(series['season_count'], 2)
        self.assertEqual(series['episode_count'], 3)

        seasons = self.client.get(f"/api/library/series/{series['id']}/seasons").json()
        self.assertEqual([entry['season'] for entry in seasons['seasons']], [1, 2])
        self.assertEqual(seasons['episodes'], 3)

        season_one = self.client.get(
            f"/api/library/films?library_id={library['id']}&parent={series['id']}&season=1").json()
        self.assertEqual([item['episode'] for item in season_one['items']], [1, 2])
        self.assertEqual(season_one['total'], 2, '按季过滤后 total 也要跟着变')

        all_episodes = self.client.get(
            f"/api/library/films?library_id={library['id']}&parent={series['id']}").json()
        self.assertEqual([(item['season'], item['episode']) for item in all_episodes['items']],
                         [(1, 1), (1, 2), (2, 1)], '分集按季集号排序')

    def test_episode_can_be_played(self):
        touch(self.media / '剧P' / 'S01' / 'P.S01E01.mp4', b'\x01' * 65536)
        created = self.client.post('/api/library/libraries', json={
            'name': '播放库', 'kind': 'series',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        }).json()
        self.scan_library(created['library']['id'])
        series = self.client.get(
            f"/api/library/films?library_id={created['library']['id']}&kind=series").json()['items'][0]
        episode = self.client.get(
            f"/api/library/films?library_id={created['library']['id']}&parent={series['id']}").json()['items'][0]
        playback = self.client.get(f"/api/library/films/{episode['id']}/playback")
        self.assertEqual(playback.status_code, 200, playback.text)
        payload = playback.json()
        self.assertEqual(payload['mode'], 'local')
        self.assertEqual(payload['url'], f"/api/library/films/{episode['id']}/stream")
        stream = self.client.get(payload['url'], headers={'Range': 'bytes=0-1023'})
        self.assertIn(stream.status_code, (200, 206), stream.text)

    def test_library_kind_can_be_changed(self):
        touch(self.media / '剧Q' / 'Q.S01E01.mkv')
        created = self.client.post('/api/library/libraries', json={
            'name': '切类型', 'kind': 'movie',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        }).json()
        self.scan_library(created['library']['id'])
        library_id = created['library']['id']
        # 电影库把这部剧目录当成一部“影片”，不会产生分集
        before = self.client.get(f'/api/library/films?library_id={library_id}').json()
        self.assertEqual(before['total'], 1)
        self.assertEqual(before['items'][0]['kind'], 'movie')

        updated = self.client.put(f'/api/library/libraries/{library_id}', json={'kind': 'series'})
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()['library']['kind'], 'series')

        self.scan_library(library_id)
        payload = self.client.get(f'/api/library/films?library_id={library_id}&kind=series').json()
        self.assertEqual([item['title'] for item in payload['items']], ['剧Q'])
        self.assertEqual(payload['items'][0]['episode_count'], 1)

    def test_scrape_is_rejected_for_series(self):
        touch(self.media / '剧R' / 'R.S01E01.mkv')
        created = self.client.post('/api/library/libraries', json={
            'name': '剧集库', 'kind': 'series',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        }).json()
        self.scan_library(created['library']['id'])
        series = self.client.get(
            f"/api/library/films?library_id={created['library']['id']}&kind=series").json()['items'][0]
        response = self.client.post(f"/api/library/films/{series['id']}/scrape", json={})
        self.assertEqual(response.status_code, 400, response.text)
        # 批量刮削会跳过剧集，不会报错
        batch = self.client.post('/api/library/films/scrape', json={'library_id': created['library']['id']})
        self.assertEqual(batch.status_code, 202, batch.text)
        self.assertEqual(batch.json()['accepted'], [])

    def test_progress_api_round_trip(self):
        for index in (1, 2):
            touch(self.media / '剧AB' / f'AB.S01E0{index}.mkv')
        created = self.client.post('/api/library/libraries', json={
            'name': '进度库', 'kind': 'series',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        }).json()
        library_id = created['library']['id']
        self.scan_library(library_id)
        series = self.client.get(f'/api/library/films?library_id={library_id}&kind=series').json()['items'][0]
        episodes = self.client.get(f'/api/library/films?library_id={library_id}&parent={series["id"]}').json()['items']

        response = self.client.put(f"/api/library/films/{episodes[0]['id']}/progress",
                                   json={'position': 600, 'duration': 1800})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertAlmostEqual(response.json()['progress']['percent'], 33.3, places=1)

        # 分集列表和季接口都要能读到进度
        listed = self.client.get(f'/api/library/films?library_id={library_id}&parent={series["id"]}').json()['items']
        self.assertAlmostEqual(listed[0]['progress']['position'], 600)
        seasons = self.client.get(f"/api/library/series/{series['id']}/seasons").json()
        self.assertEqual(seasons['progress']['total'], 2)
        self.assertEqual(seasons['progress']['watched'], 0)
        self.assertEqual(seasons['progress']['resume']['episode'], 1)

        cleared = self.client.delete(f"/api/library/films/{episodes[0]['id']}/progress")
        self.assertEqual(cleared.status_code, 200, cleared.text)
        self.assertEqual(self.client.get(f"/api/library/films/{episodes[0]['id']}").json()['film']['progress']['position'], 0)

        missing = self.client.put('/api/library/films/not-exist/progress', json={'position': 1, 'duration': 2})
        self.assertEqual(missing.status_code, 404)

    def test_refresh_endpoint_rereads_nfo(self):
        touch(self.media / '剧S' / 'S.S01E01.mkv')
        created = self.client.post('/api/library/libraries', json={
            'name': '刷新库', 'kind': 'series',
            'folders': [{'path': str(self.media), 'recursive': True}], 'scan': False,
        }).json()
        self.scan_library(created['library']['id'])
        series = self.client.get(
            f"/api/library/films?library_id={created['library']['id']}&kind=series").json()['items'][0]
        (self.media / '剧S' / 'tvshow.nfo').write_text('<tvshow><title>新名字</title></tvshow>', encoding='utf-8')
        response = self.client.post(f"/api/library/films/{series['id']}/refresh")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['film']['title'], '新名字')

    def scan_library(self, library_id: str) -> None:
        self._wait(library_store.start_scan(library_id)['id'])

    def wait_scan(self, scan_id: str, timeout: float = 10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            scan = self.client.get('/api/library/scans/' + scan_id).json()['scan']
            if scan['status'] in ('succeeded', 'failed'):
                while scan_id in library_store._scan_state and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(scan['status'], 'succeeded', scan)
                return scan
            time.sleep(0.05)
        self.fail('扫描超时')


if __name__ == '__main__':
    unittest.main()
