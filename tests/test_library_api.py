"""Offline regressions for the filesystem-first media library (P0 API).

The library index treats videos, NFOs and artwork on disk as the source of truth;
task records are only history. These tests drive the HTTP API the same way the page
will: add a folder, scan it, edit metadata (which writes the NFO), start a scrape and
re-scan after files disappear.
"""
import copy
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

import yaml
from PIL import Image

from javsp_web import library_store, storage, tasks

VIDEO_BYTES = b'\x00' * 4096


def write_nfo(path: Path, dvdid: str, title: str) -> None:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f'<movie><title>{title}</title><uniqueid type="num" default="true">{dvdid}</uniqueid>'
        '<actor><name>Actor A</name></actor><studio>Studio X</studio><premiered>2024-01-02</premiered></movie>\n',
        encoding='utf-8')


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.media = self.root / 'media'
        self.media.mkdir()
        old = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(old):
                self.enterContext(patch.object(storage, name, self.root / 'state' / value.relative_to(old)))
        self.enterContext(patch.object(library_store, 'LIBRARY_DB_FILE', self.root / 'state' / 'library.db'))
        self.enterContext(patch.object(tasks, 'DATA_DIR', self.root / 'state'))
        self.enterContext(patch.object(tasks, '_TASK_COVERS_DIR', self.root / 'state/task-covers'))
        self.enterContext(patch.object(tasks, '_logs', {}))
        self.enterContext(patch.object(tasks, '_pending_tasks', deque()))
        self.enterContext(patch.object(tasks, '_processes', {}))
        self.enterContext(patch.object(tasks, '_deleted_tasks', set()))
        self.enterContext(patch.object(tasks, '_worker_batches', {}))
        self.enterContext(patch.object(tasks, '_cancelled_tasks', set()))
        self.enqueue = self.enterContext(patch.object(tasks, '_enqueue_task'))
        # 媒体库扫描会读取默认预设的最小文件大小；测试用的小文件需要把阈值设为 0
        storage.ensure_seed_data()
        default_preset = storage.get_preset('default') or {}
        preset_form = copy.deepcopy(default_preset.get('form') or {})
        preset_form.setdefault('scanner', {})['minimum_size'] = 0
        storage.save_preset({**default_preset, 'id': 'default', 'form': preset_form})
        library_store._preset_scanner_cache.clear()
        self.addCleanup(library_store._preset_scanner_cache.clear)
        config = yaml.safe_load((ROOT / 'vendor/JavSP/config.yml').read_text(encoding='utf-8'))
        config['scanner'].update(minimum_size=0, input_files=None, skip_nfo_dir=False)
        self.enterContext(patch.object(tasks, '_preset_config_data',
                                       side_effect=lambda _: (copy.deepcopy(config), {'name': 'Test', 'task_concurrency': 1})))
        with patch('threading.Thread.start'):
            from javsp_web import server
        self.server = server
        self.enterContext(patch.dict(server.app.dependency_overrides,
                                     {server.current_user: lambda: {'username': 'test', 'role': 'admin'}}))
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)
        self.make_media()

    # -- fixtures -----------------------------------------------------------
    def make_media(self):
        complete = self.media / 'ABC-123'
        complete.mkdir()
        (complete / 'ABC-123.mp4').write_bytes(VIDEO_BYTES)
        write_nfo(complete / 'ABC-123.nfo', 'ABC-123', 'Demo Title')
        Image.new('RGB', (24, 36), 'blue').save(complete / 'ABC-123.jpg')
        fresh = self.media / 'DEF-456'
        fresh.mkdir()
        (fresh / 'DEF-456.mp4').write_bytes(VIDEO_BYTES)
        parts = self.media / 'GHI-789'
        parts.mkdir()
        (parts / 'GHI-789-A.mp4').write_bytes(VIDEO_BYTES)
        (parts / 'GHI-789-B.mp4').write_bytes(VIDEO_BYTES)

    def create_library(self, name='媒体库', scan=True, folders=None):
        body = {'name': name, 'scan': scan}
        if folders is not None:
            body['folders'] = folders
        else:
            body['folders'] = [{'path': str(self.media), 'recursive': True}]
        response = self.client.post('/api/library/libraries', json=body)
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        if scan and payload.get('scan'):
            self.wait_scan(payload['scan']['id'])
        return payload['library']

    def add_folder(self, library_id, path, scan=True):
        response = self.client.post(f'/api/library/libraries/{library_id}/folders',
                                    json={'path': str(path), 'recursive': True, 'scan': scan})
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        if scan and payload.get('scan'):
            self.wait_scan(payload['scan']['id'])
        return payload['folder']

    def wait_scan(self, scan_id, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            scan = self.client.get('/api/library/scans/' + scan_id).json()['scan']
            if scan['status'] in ('succeeded', 'failed'):
                self.assertEqual(scan['status'], 'succeeded', scan)
                return scan
            time.sleep(0.05)
        self.fail('扫描未在超时内完成')

    def films(self, **params):
        response = self.client.get('/api/library/films', params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def film_by_dvdid(self, dvdid):
        return next(item for item in self.films(limit=200)['items'] if item['dvdid'] == dvdid)

    # -- tests --------------------------------------------------------------
    def test_create_library_scans_and_lists_every_film(self):
        library = self.create_library()
        self.assertTrue(library['id'])
        self.assertEqual(len(library['folders']), 1)
        payload = self.films(limit=200)
        self.assertEqual(payload['total'], 3)
        by_id = {item['dvdid']: item for item in payload['items']}
        self.assertEqual(by_id['ABC-123']['status'], 'complete')
        self.assertEqual(by_id['ABC-123']['title'], 'Demo Title')
        self.assertEqual(by_id['ABC-123']['actress'], ['Actor A'])
        self.assertEqual(by_id['DEF-456']['status'], 'partial')
        self.assertIn('title', by_id['DEF-456']['missing_fields'])
        self.assertIn('cover', by_id['DEF-456']['missing_fields'])
        self.assertEqual(len(by_id['GHI-789']['files']), 2, '多 P 应合并为一部影片')
        stats = payload['stats']
        self.assertEqual((stats['total'], stats['complete']), (3, 1))
        groups = self.client.get('/api/library/groups', params={'by': 'actress', 'library_id': library['id']}).json()
        keys = [item['key'] for item in groups['groups']]
        self.assertEqual(keys[0], 'Actor A')
        self.assertIn('', keys, '没有女优的影片应归入未知女优分组')
        self.assertEqual([item['name'] for item in groups['groups'] if item['key'] == ''], ['未知女优'])

    def test_one_library_holds_multiple_folders(self):
        second = Path(self.temp.name) / 'media2'
        second.mkdir()
        (second / 'JKL-321').mkdir()
        (second / 'JKL-321' / 'JKL-321.mp4').write_bytes(VIDEO_BYTES)
        library = self.create_library(name='合集库')
        self.add_folder(library['id'], second)
        refreshed = self.client.get('/api/library/libraries').json()['libraries'][0]
        self.assertEqual(refreshed['name'], '合集库')
        self.assertEqual(len(refreshed['folders']), 2)
        self.assertEqual(refreshed['film_count'], 4)
        self.assertEqual(self.films(library_id=library['id'], limit=200)['total'], 4)
        # 单个文件夹也能单独过滤
        folder = refreshed['folders'][1]
        self.assertEqual(self.films(folder_id=folder['id'], limit=200)['total'], 1)
        # 同一文件夹不能重复加入同一个库
        duplicate = self.client.post(f'/api/library/libraries/{library["id"]}/folders',
                                     json={'path': str(second), 'scan': False})
        self.assertEqual(duplicate.status_code, 400, duplicate.text)
        # 移除文件夹后影片随之离开该库
        removed = self.client.delete(f'/api/library/libraries/{library["id"]}/folders/{folder["id"]}')
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(self.films(library_id=library['id'], limit=200)['total'], 3)

    def test_delete_library_only_removes_the_index(self):
        library = self.create_library()
        response = self.client.delete(f'/api/library/libraries/{library["id"]}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get('/api/library/libraries').json()['libraries'], [])
        self.assertTrue((self.media / 'ABC-123' / 'ABC-123.mp4').is_file(), '不能删除磁盘文件')

    def test_reveal_opens_the_folder_on_a_local_deployment(self):
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        calls = []
        with patch.object(self.server, 'IS_DOCKER', False), \
                patch.object(self.server.os, 'name', 'nt'), \
                patch.object(self.server.sys, 'platform', 'win32'), \
                patch.object(self.server, '_focus_opened_folder') as focus, \
                patch.object(self.server.subprocess, 'Popen', side_effect=lambda args, *a, **k: calls.append(args)):
            response = self.client.post(f'/api/library/films/{film["id"]}/reveal')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['path'], str(self.media / 'ABC-123'))
        self.assertEqual(len(calls), 1)
        self.assertIn('/select,', calls[0][1])
        self.assertIn('ABC-123.mp4', calls[0][1])
        self.assertIn('ABC-123', focus.call_args.args, '打开后应把窗口提到前台')

    def test_reveal_uses_a_short_path_for_very_long_names(self):
        """explorer 对超长路径会失败并退回“文档”，这里改用 8.3 短路径。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        # 用多层目录把整体路径拉长；单个文件名保持较短，避免 Linux 的 255 字节上限
        deep = self.media
        for _ in range(5):
            deep = deep / ('深いディレクトリ' * 6)
        deep.mkdir(parents=True, exist_ok=True)
        long_target = deep / 'ABC-123.mp4'
        long_target.write_bytes(VIDEO_BYTES)
        self.assertGreater(len(str(long_target)), 240)
        self.assertLess(max(len(part.encode()) for part in long_target.parts), 255)
        with patch.object(self.server.os, 'name', 'nt'), \
                patch.object(self.server, '_windows_short_path', return_value=r'C:\T\LONG~1.MP4'):
            command = self.server._reveal_command(long_target)
        self.assertEqual(command, ['explorer', '/select,C:\\T\\LONG~1.MP4'])
        # 没有短路径时不再调用 explorer（否则会打开“文档”），而是打开父目录
        with patch.object(self.server.os, 'name', 'nt'), \
                patch.object(self.server, '_windows_short_path', return_value=''):
            self.assertIsNone(self.server._reveal_command(long_target))
            opened = []
            with patch.object(self.server, '_focus_opened_folder'), \
                    patch.object(self.server.os, 'name', 'nt'), \
                    patch.object(self.server.subprocess, 'Popen') as popen, \
                    patch.object(self.server.os, 'startfile', side_effect=lambda p: opened.append(p), create=True):
                result = self.server._reveal_in_file_manager(long_target)
            self.assertFalse(popen.called, '没有短路径时不应再交给 explorer')
            self.assertEqual(opened, [str(long_target.parent)], '应打开影片所在目录')
            self.assertEqual(result, long_target.parent)

    def test_reveal_keeps_short_paths_untouched(self):
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        target = self.media / 'ABC-123' / 'ABC-123.mp4'
        with patch.object(self.server.os, 'name', 'nt'), \
                patch.object(self.server, '_windows_short_path') as short:
            command = self.server._reveal_command(target)
        self.assertEqual(command, ['explorer', f'/select,{target}'])
        self.assertFalse(short.called, '短路径不需要查询 8.3 名称')

    def test_reveal_is_disabled_in_docker(self):
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        with patch.object(self.server, 'IS_DOCKER', True):
            response = self.client.post(f'/api/library/films/{film["id"]}/reveal')
        self.assertEqual(response.status_code, 400)
        self.assertIn('容器', response.json()['detail'])

    def test_reveal_reports_missing_files(self):
        library = self.create_library()
        film = self.film_by_dvdid('GHI-789')
        (self.media / 'GHI-789' / 'GHI-789-A.mp4').unlink()
        (self.media / 'GHI-789' / 'GHI-789-B.mp4').unlink()
        with patch.object(self.server, 'IS_DOCKER', False):
            response = self.client.post(f'/api/library/films/{film["id"]}/reveal')
        self.assertEqual(response.status_code, 404)
        self.assertIn('不存在', response.json()['detail'])

    def test_scan_follows_the_preset_minimum_size(self):
        """媒体库扫描与刮削任务共用预设的“最小匹配文件大小”。"""
        tiny = self.media / 'TINY-001'
        tiny.mkdir()
        (tiny / 'TINY-001.mp4').write_bytes(VIDEO_BYTES)  # 4 KB，低于阈值
        presets = self.client.get('/api/presets').json()
        form = copy.deepcopy(presets[0]['form'])
        form['scanner']['minimum_size'] = '8MiB'
        preset = self.client.post('/api/presets', json={'name': '最小 8MiB', 'mode': 'form', 'form': form,
                                                        'task_concurrency': 1}).json()
        library = self.create_library(name='阈值库', folders=[{'path': str(self.media), 'recursive': True}])
        # 建库时用的默认预设已索引 4 个文件；换成大阈值后重扫应跳过全部小文件
        self.client.put(f'/api/library/libraries/{library["id"]}', json={'preset_id': preset['id']})
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        scan = self.client.get('/api/library/scans/' + scan['id']).json()['scan']
        # skipped 统计的是“文件数”：ABC、DEF、GHI 的两个分片、TINY 共 5 个
        self.assertEqual(scan['skipped'], 5)
        self.assertEqual(self.films(library_id=library['id'], limit=200)['total'], 0)
        # 小于阈值的旧记录是被移除，而不是标成“文件已丢失”
        self.assertEqual(self.films(library_id=library['id'])['stats']['missing'], 0)
        # 库返回的阈值让前端能提示用户
        libraries = self.client.get('/api/library/libraries').json()['libraries']
        self.assertEqual(libraries[0]['minimum_size'], 8 * 1024 * 1024)

    def test_scan_without_minimum_size_indexes_small_files(self):
        library = self.create_library(name='不限制')
        movies = self.films(library_id=library['id'], limit=200)
        self.assertEqual(movies['total'], 3)
        tiny = self.media / 'TINY-002'
        tiny.mkdir()
        (tiny / 'TINY-002.mp4').write_bytes(VIDEO_BYTES)
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        self.assertEqual(self.films(library_id=library['id'], limit=200)['total'], 4)

    def test_batch_scrape_follows_the_current_filter(self):
        """批量刮削按当前筛选范围入队：只看“缺资料”时只入队这些影片。"""
        library = self.create_library()
        unscraped_before = self.films(folder_id='', library_id=library['id'])['stats']
        partial = self.films(library_id=library['id'], status='partial')
        self.assertGreater(partial['total'], 0)
        response = self.client.post('/api/library/films/scrape',
                                    json={'library_id': library['id'], 'status': 'partial'})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(len(payload['accepted']), partial['total'])
        self.assertEqual(payload['total'], partial['total'])
        for task_id in payload['accepted']:
            task = tasks.get_task(task_id)
            self.assertEqual(task['source'], 'library')
        queued = self.films(library_id=library['id'], status='scraping')
        self.assertEqual(queued['total'], partial['total'])

    def test_batch_scrape_can_include_complete_films(self):
        """资料完整的影片也能批量入队，用于补齐 NFO 里缺的字段。"""
        library = self.create_library()
        response = self.client.post('/api/library/films/scrape', json={'library_id': library['id']})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(len(payload['accepted']), 3, '全部 3 部都应入队')
        again = self.client.post('/api/library/films/scrape', json={'library_id': library['id']})
        self.assertEqual(again.status_code, 202, again.text)
        self.assertEqual(len(again.json()['accepted']), 0, '已在刮削中的影片不应重复入队')
        self.assertEqual(len(again.json()['skipped']), 3)

    def test_rescrape_keeps_fields_the_engine_did_not_write(self):
        """引擎写 NFO 是整份重建：重新刮削后要把旧 NFO 里的女优、片商等补回去。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        nfo_path = Path(film['nfo_path'])
        self.assertTrue(nfo_path.is_file())
        before = nfo_path.read_text(encoding='utf-8')
        self.assertIn('Actor A', before)
        # 入队时会产生快照
        response = self.client.post(f'/api/library/films/{film["id"]}/scrape', json={})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue((library_store.NFO_SNAPSHOT_DIR / f'{film["id"]}.nfo').is_file())
        # 模拟引擎只写了标题和番号（没有演员、片商）
        nfo_path.write_text(
            '<?xml version="1.0"?><movie><title>新标题</title><uniqueid type="num">ABC-123</uniqueid></movie>',
            encoding='utf-8')
        library_store.refresh_film_after_task(film['id'], 'succeeded')
        saved = nfo_path.read_text(encoding='utf-8')
        self.assertIn('新标题', saved)
        self.assertIn('Actor A', saved, '旧 NFO 里的演员应被补回')
        self.assertIn('Studio X', saved, '旧 NFO 里的片商应被补回')
        refreshed = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(refreshed['actress'], ['Actor A'])
        self.assertFalse((library_store.NFO_SNAPSHOT_DIR / f'{film["id"]}.nfo').exists(), '合并后清理快照')

    def test_in_place_organize_keeps_each_film_separate(self):
        """原地整理（save_dir 就是源目录）时，不能把同目录其它影片当成本片的分片。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        task = {
            'status': 'succeeded',
            'file_organizer': {'organized_files': [film['video_path']]},
            'progress': {'output': {'save_dir': str(self.media)}},
        }
        library_store.refresh_film_after_task(film['id'], 'succeeded', '', task)
        after = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(len(after['files']), 1, '只能包含本片自己的文件')
        self.assertEqual(after['video_path'], film['video_path'])
        self.assertEqual(after['detached'], 0, '文件没搬家就不该标记已整理')
        # 没有 organized_files 时同样不能乱认文件
        library_store.refresh_film_after_task(film['id'], 'succeeded', '',
                                              {'status': 'succeeded', 'progress': {'output': {'save_dir': str(self.media)}}})
        after = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(len(after['files']), 1)
        self.assertEqual(after['detached'], 0)
        # 其它影片不受影响
        for dvdid in ('DEF-456', 'GHI-789'):
            other = self.film_by_dvdid(dvdid)
            self.assertEqual(other['detached'], 0)
            self.assertNotEqual(other['video_path'], after['video_path'])

    def test_real_move_is_still_followed(self):
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        outside = Path(self.temp.name) / '#整理完成' / 'ABC-123'
        outside.mkdir(parents=True)
        target = outside / 'ABC-123.mp4'
        Path(film['video_path']).rename(target)
        task = {'status': 'succeeded', 'file_organizer': {'organized_files': [str(target)]},
                'progress': {'output': {'save_dir': str(outside)}}}
        library_store.refresh_film_after_task(film['id'], 'succeeded', '', task)
        after = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(Path(after['video_path']), target)
        self.assertEqual(after['detached'], 1)

    def fail_task(self, task_id: str) -> None:
        """把一个任务标记为失败（模拟刮削失败后的记录与影片状态）。"""
        from javsp_web import library_store, task_store

        record = task_store.record(task_id, logs=False)
        film_id = str(record.get('library_film_id') or '')
        record['status'] = 'failed'
        record['error'] = '抓取失败'
        task_store.upsert(record)
        if film_id:
            library_store.refresh_film_after_task(film_id, 'failed', '抓取失败')

    def test_retry_failed_task_uses_the_current_dvdid(self):
        """重新刮削失败任务时会用影片当前番号（编辑资料后就是新番号）。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        first = self.client.post(f'/api/library/films/{film["id"]}/scrape', json={}).json()['task_id']
        self.fail_task(first)
        self.client.patch(f'/api/library/films/{film["id"]}',
                          json={'dvdid': 'ZZZ-999', 'title': '新标题', 'actress': ['Actor A']})
        response = self.client.post(f'/api/tasks/{first}/retry')
        self.assertEqual(response.status_code, 202, response.text)
        new_id = response.json()['task_id']
        self.assertNotEqual(new_id, first)
        job = tasks.get_task(new_id)
        self.assertEqual(job['forced_avid'], 'ZZZ-999', '应使用编辑后的番号')
        self.assertEqual(job['library_film_id'], film['id'])
        # 列表接口也要带上关联影片，前端才能显示“资料”按钮
        listing = self.client.get('/api/tasks', params={'view': 'all', 'status': 'failed'}).json()
        self.assertTrue(listing, '应能查到失败任务')
        self.assertEqual(listing[0]['library_film_id'], film['id'])
        refreshed = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(refreshed['scrape_state'], 'queued')

    def test_retry_failed_tasks_batch(self):
        library = self.create_library()
        first = self.client.post('/api/library/films/scrape', json={'library_id': library['id']}).json()
        for task_id in first['accepted'][:2]:
            self.fail_task(task_id)
        response = self.client.post('/api/tasks/retry-failed', json={})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(len(payload['accepted']), 2)
        self.assertEqual(payload['total'], 2)
        self.assertTrue(all(isinstance(item, str) for item in payload['accepted']))
        again = self.client.post('/api/tasks/retry-failed', json={})
        self.assertEqual(again.status_code, 202)
        self.assertEqual(len(again.json()['accepted']), 0, '已在队列中的不再重复入队')
        self.assertEqual(len(again.json()['skipped']), 2)

    def test_per_file_nfo_wins_over_a_stub_movie_nfo(self):
        """目录里同时有同名 NFO 和只写了番号的 movie.nfo 时，要用资料更全的那个。"""
        folder = self.media / 'STUB-001'
        folder.mkdir()
        (folder / 'STUB-001.mp4').write_bytes(VIDEO_BYTES)
        (folder / 'movie.nfo').write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><uniqueid type="num" default="true">STUB-001</uniqueid></movie>',
            encoding='utf-8')
        (folder / 'STUB-001.nfo').write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><title>STUB-001 完整标题</title>'
            '<uniqueid type="num" default="true">STUB-001</uniqueid><studio>Studio S</studio>'
            '<premiered>2024-05-01</premiered><actor><name>Actor S</name></actor></movie>',
            encoding='utf-8')
        Image.new('RGB', (24, 36), 'red').save(folder / 'STUB-001.jpg')
        library = self.create_library()
        film = next(item for item in self.films(library_id=library['id'])['items'] if item['dvdid'] == 'STUB-001')
        self.assertTrue(film['nfo_path'].endswith('STUB-001.nfo'), '应指向同名 NFO')
        self.assertEqual(film['title'], 'STUB-001 完整标题')
        self.assertEqual(film['studio'], 'Studio S')
        self.assertEqual(film['release_date'], '2024-05-01')
        self.assertEqual(film['actress'], ['Actor S'])
        self.assertEqual(film['status'], 'complete', film['missing_fields'])

    def test_stats_follow_the_selected_group(self):
        """统计要跟着分类走：选中某个女优后只统计她的影片。"""
        extra = self.media / 'JKL-012'
        extra.mkdir()
        (extra / 'JKL-012.mp4').write_bytes(VIDEO_BYTES)
        write_nfo(extra / 'JKL-012.nfo', 'JKL-012', 'Second Title')
        Image.new('RGB', (24, 36), 'green').save(extra / 'JKL-012.jpg')
        library = self.create_library()
        whole = self.films(library_id=library['id'])
        self.assertEqual(whole['stats']['total'], 4)
        groups = self.client.get('/api/library/groups',
                                 params={'by': 'actress', 'library_id': library['id']}).json()
        actress = groups['groups'][0]
        page = self.films(library_id=library['id'], group_by='actress', group=actress['key'])
        self.assertEqual(page['total'], actress['count'])
        self.assertEqual(page['stats']['total'], actress['count'], '总数应等于该分类的影片数')
        self.assertEqual(page['stats']['complete'], page['total'], '都应有完整资料')
        # 搜索也影响统计
        searched = self.films(library_id=library['id'], query='JKL-012')
        self.assertEqual(searched['stats']['total'], 1)

    def test_groups_load_without_per_group_queries(self):
        """分组封面必须一次查完：以前每个女优查一次，637 个女优要 3 秒多。"""
        from contextlib import contextmanager

        library = self.create_library()
        statements: list[str] = []
        original = library_store.connection

        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db

        with patch.object(library_store, 'connection', traced):
            payload = library_store.groups('actress', library['id'])
        selects = [item for item in statements if item.strip().upper().startswith('SELECT')]
        self.assertLess(len(selects), 12, f'分组查询次数过多（{len(selects)} 次）：{selects[:5]}')
        self.assertTrue(payload['groups'])
        for group in payload['groups']:
            self.assertLessEqual(len(group['covers']), 9)

    def test_task_metadata_fills_fields_lost_from_nfo(self):
        """NFO 被第三方改写成只有标题时，用任务里抓到的资料补全并写回 NFO。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        nfo_path = Path(film['nfo_path'])
        # 模拟 Jellyfin/Emby 覆盖：只剩 sorttitle，没有 title/actor/studio/premiered
        nfo_path.write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><sorttitle>ABC-123</sorttitle>'
            '<lockdata>false</lockdata><runtime>115</runtime></movie>', encoding='utf-8')
        library_store.refresh_film_after_task(film['id'], 'succeeded', '')
        self.assertEqual(library_store.get_film(film['id'], reconcile=False)['title'], 'Demo Title',
                         'stub NFO 不应把已有标题冲掉')
        task = {'status': 'succeeded', 'progress': {'metadata': {
            'dvdid': 'ABC-123', 'title': '被抓到的标题', 'actress': ['Actor A'],
            'director': 'Director D', 'producer': 'Studio X', 'publisher': 'Publisher P',
            'publish_date': '2020-09-21'}}}
        library_store.refresh_film_after_task(film['id'], 'succeeded', '', task)
        refreshed = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(refreshed['title'], '被抓到的标题')
        self.assertEqual(refreshed['actress'], ['Actor A'])
        self.assertEqual(refreshed['studio'], 'Studio X')
        self.assertEqual(refreshed['release_date'], '2020-09-21')
        self.assertEqual(refreshed['status'], 'complete')
        saved = nfo_path.read_text(encoding='utf-8')
        self.assertIn('被抓到的标题', saved)
        self.assertIn('Actor A', saved)

    def test_rescan_keeps_metadata_when_nfo_is_a_stub(self):
        """NFO 被媒体服务器改写成 stub 后，重新扫描不能把已刮好的资料冲掉。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        nfo_path = Path(film['nfo_path'])
        nfo_path.write_text(
            '<?xml version="1.0" encoding="utf-8"?><movie><sorttitle>ABC-123</sorttitle></movie>',
            encoding='utf-8')
        job = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        for _ in range(200):
            scan = self.client.get(f'/api/library/scans/{job["id"]}').json()['scan']
            if scan['status'] in ('succeeded', 'failed'):
                break
            time.sleep(0.05)
        refreshed = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(refreshed['title'], 'Demo Title', '重扫不应把标题清空')
        self.assertEqual(refreshed['actress'], ['Actor A'])
        self.assertEqual(refreshed['status'], 'complete')

    def test_nfo_path_is_unique_per_film(self):
        """多视频目录必须用 <视频文件名>.nfo；单视频目录用 movie.nfo —— 每部影片只有一个 NFO。"""
        multi = self.media / 'MULTI-001'
        multi.mkdir()
        for suffix in ('A', 'B'):
            (multi / f'MULTI-001-{suffix}.mp4').write_bytes(VIDEO_BYTES)
        (multi / 'movie.nfo').write_text(
            '<?xml version="1.0"?><movie><title>共享 NFO</title></movie>', encoding='utf-8')
        single = self.media / 'SINGLE-002'
        single.mkdir()
        (single / 'SINGLE-002.mp4').write_bytes(VIDEO_BYTES)
        library = self.create_library()
        films = self.films(library_id=library['id'])
        multi_films = [item for item in films['items'] if item['dvdid'].startswith('MULTI-001')]
        self.assertTrue(multi_films)
        for item in multi_films:
            self.assertFalse(item['nfo_path'].endswith('movie.nfo'),
                             f'多视频目录不能共用 movie.nfo：{item["nfo_path"]}')
            self.assertEqual(library_store._nfo_target(item).name,
                             f'{Path(item["video_path"]).stem}.nfo')
        single_film = next(item for item in films['items'] if item['dvdid'] == 'SINGLE-002')
        self.assertEqual(single_film['nfo_path'], '', '还没有 NFO 时不应凭空指向 movie.nfo')
        # 写入目标是单视频目录的 movie.nfo（与引擎默认一致）
        target = library_store._nfo_target(single_film)
        self.assertEqual(target.name, 'movie.nfo')
        multi_target = library_store._nfo_target(multi_films[0])
        self.assertEqual(multi_target.name, f'{Path(multi_films[0]["video_path"]).stem}.nfo')

    def test_task_config_uses_the_library_nfo_name(self):
        """媒体库任务生成的任务配置要把引擎的 NFO 命名改成媒体库用的那个文件。"""
        multi = self.media / 'CONF-003'
        multi.mkdir()
        (multi / 'CONF-003.mp4').write_bytes(VIDEO_BYTES)
        (multi / 'CONF-003-2.mp4').write_bytes(VIDEO_BYTES)
        library = self.create_library()
        film = next(item for item in self.films(library_id=library['id'])['items']
                    if item['dvdid'] == 'CONF-003' and item['video_path'].endswith('CONF-003.mp4'))
        response = self.client.post(f'/api/library/films/{film["id"]}/scrape', json={})
        self.assertEqual(response.status_code, 202, response.text)
        task_id = response.json()['task_id']
        config = yaml.safe_load((self.root / 'state' / 'task-config' / f'{task_id}.yml').read_text(encoding='utf-8'))
        self.assertEqual(config['summarizer']['nfo']['basename_pattern'], 'CONF-003')

    def test_reveal_is_rejected_for_non_local_clients(self):
        """局域网其它设备不能触发宿主机弹文件管理器。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        calls = []
        with patch.object(self.server, '_is_local_client', return_value=False), \
                patch.object(self.server.subprocess, 'Popen', side_effect=lambda *a, **k: calls.append(a)):
            response = self.client.post(f'/api/library/films/{film["id"]}/reveal')
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(calls, [], '非本机请求不应启动任何进程')

    def test_runtime_reports_local_client(self):
        """运行时信息要告诉前端：当前浏览器是否在服务端主机上。"""
        with patch.object(self.server, '_is_local_client', return_value=False):
            remote = self.client.get('/api/runtime').json()
        self.assertFalse(remote['local_client'])
        local = self.client.get('/api/runtime').json()
        self.assertTrue(local['local_client'], '测试客户端默认视为本机')

    def test_native_path_picker_is_rejected_for_non_local_clients(self):
        """局域网设备不能弹出宿主机原生的文件夹选择框。"""
        with patch.object(self.server, '_is_local_client', return_value=False), \
                patch.object(self.server, '_select_native_paths') as native:
            single = self.client.post('/api/path/select', json={'kind': 'directory'})
            multi = self.client.post('/api/path/select-multi', json={'kind': 'directory'})
        self.assertEqual(single.status_code, 403, single.text)
        self.assertEqual(multi.status_code, 403, multi.text)
        self.assertFalse(native.called, '非本机请求不应调用系统对话框')
        # 本机仍然可用
        with patch.object(self.server, '_select_native_paths', return_value=['D:/media']):
            ok = self.client.post('/api/path/select', json={'kind': 'directory'})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.json()['path'], 'D:/media')

    def test_file_operations_are_local_only(self):
        """还原文件、删除图片这类操作会动宿主机文件，非本机请求必须 403。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        task_id = self.client.post(f'/api/library/films/{film["id"]}/scrape', json={}).json()['task_id']
        with patch.object(self.server, '_is_local_client', return_value=False), \
                patch.object(self.server, 'restore_task_files') as restore, \
                patch.object(self.server, 'edit_task_artwork') as artwork:
            remote_restore = self.client.post(f'/api/tasks/{task_id}/restore')
            remote_artwork = self.client.delete(f'/api/tasks/{task_id}/artwork/cover/0')
        self.assertEqual(remote_restore.status_code, 403, remote_restore.text)
        self.assertEqual(remote_artwork.status_code, 403, remote_artwork.text)
        self.assertFalse(restore.called, '非本机请求不应移动文件')
        self.assertFalse(artwork.called, '非本机请求不应删除图片')
        # 本机仍然可用
        with patch.object(self.server, 'restore_task_files', return_value=True) as restore:
            local = self.client.post(f'/api/tasks/{task_id}/restore')
        self.assertEqual(local.status_code, 200, local.text)
        self.assertTrue(restore.called)

    def test_library_film_playback_route(self):
        """播放接口：能解析到媒体服务器就返回地址，解析不到给出明确提示。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        payload = {'mode': 'emby', 'item_id': '9', 'url': 'http://emby/videos/9/master.mp4?api_key=K',
                   'transcoding': True, 'server_name': '客厅'}
        with patch.object(self.server, 'film_playback', return_value=payload):
            response = self.client.get(f'/api/library/films/{film["id"]}/playback')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['mode'], 'emby')
        with patch.object(self.server, 'film_playback', side_effect=ValueError('没有在已连接的 Emby 里找到这部影片')):
            missing = self.client.get(f'/api/library/films/{film["id"]}/playback')
        self.assertEqual(missing.status_code, 409, missing.text)
        self.assertIn('Emby', missing.json()['detail'])

    def test_local_stream_supports_range_requests(self):
        """本地直连：只允许索引里的文件，支持 Range（拖动进度）。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        full = self.client.get(f'/api/library/films/{film["id"]}/stream')
        self.assertEqual(full.status_code, 200, full.text)
        self.assertEqual(full.headers.get('content-type'), 'video/mp4')
        partial = self.client.get(f'/api/library/films/{film["id"]}/stream', headers={'Range': 'bytes=0-99'})
        self.assertEqual(partial.status_code, 206, partial.text)
        self.assertEqual(partial.headers.get('content-range', '').split('/')[0], 'bytes 0-99')

    def test_local_stream_rejects_unsupported_containers(self):
        """浏览器放不了的容器不让直连，交给 Emby 转码。"""
        folder = self.media / 'MKV-001'
        folder.mkdir()
        (folder / 'MKV-001.mkv').write_bytes(VIDEO_BYTES)
        write_nfo(folder / 'MKV-001.nfo', 'MKV-001', 'Matroska')
        library = self.create_library()
        film = self.film_by_dvdid('MKV-001')
        response = self.client.get(f'/api/library/films/{film["id"]}/stream')
        self.assertEqual(response.status_code, 415, response.text)

    def test_playback_prefers_local_for_mp4(self):
        """mp4 默认走本地直连，不需要 Emby。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        response = self.client.get(f'/api/library/films/{film["id"]}/playback')
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload['mode'], 'local')
        self.assertEqual(payload['url'], f'/api/library/films/{film["id"]}/stream')
        self.assertEqual(payload['fallback_mode'], 'emby')

    def test_playback_stop_route(self):
        """关闭播放器：接口把停止请求转发给媒体服务器。"""
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        with patch.object(self.server, 'stop_playback', return_value={'ok': True, 'results': []}) as stop:
            response = self.client.post(f'/api/library/films/{film["id"]}/playback/stop',
                                        json={'server_id': 's1', 'play_session_id': 'ps-1'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(stop.call_args.args, ('s1', 'ps-1'))

    def test_search_is_case_insensitive(self):
        library = self.create_library()
        for needle in ('def-456', 'DEF-456', 'Def-456'):
            found = self.films(library_id=library['id'], query=needle)
            self.assertEqual(found['total'], 1, needle)
            self.assertEqual(found['items'][0]['dvdid'], 'DEF-456')
        self.assertEqual(self.films(library_id=library['id'], query='不存在的番号')['total'], 0)

    def test_organized_move_follows_the_index_instead_of_losing_the_film(self):
        """预设开启整理时文件会移出库目录，索引要跟着搬家，影片不能变成“文件已丢失”。"""
        library = self.create_library()
        film = self.film_by_dvdid('DEF-456')
        outside = Path(self.temp.name) / '#整理完成' / '演员' / 'DEF-456'
        outside.mkdir(parents=True)
        source = self.media / 'DEF-456' / 'DEF-456.mp4'
        target = outside / 'DEF-456.mp4'
        source.rename(target)
        (outside / 'movie.nfo').write_text(
            '<?xml version="1.0"?><movie><title>整理后的标题</title>'
            '<uniqueid type="num">DEF-456</uniqueid><actor><name>Actor B</name></actor>'
            '<studio>Studio Z</studio><premiered>2025-03-04</premiered></movie>', encoding='utf-8')
        task = {
            'status': 'succeeded',
            'file_organizer': {'organized_files': [str(target)]},
            'progress': {'output': {'save_dir': str(outside)}},
        }
        library_store.refresh_film_after_task(film['id'], 'succeeded', '', task)
        moved = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(moved['status'], 'partial')  # 仍缺封面
        self.assertNotEqual(moved['status'], 'missing')
        self.assertEqual(Path(moved['video_path']), target)
        self.assertEqual(moved['detached'], 1)
        self.assertEqual(library_store.list_films(library_id=library['id'])['total'], 3)
        # 再次扫描库目录不应把它标成丢失
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        after = library_store.get_film(film['id'], reconcile=False)
        self.assertNotEqual(after['status'], 'missing')
        self.assertEqual(self.films(library_id=library['id'])['stats']['missing'], 0)

    def test_failed_task_keeps_the_film_in_the_incomplete_filter(self):
        library = self.create_library()
        film = self.film_by_dvdid('DEF-456')
        self.assertEqual(self.films(library_id=library['id'], status='partial')['total'], 2)
        library_store.refresh_film_after_task(film['id'], 'failed', '所有数据源均未取得封面')
        after = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(after['status'], 'partial')
        self.assertEqual(after['scrape_state'], 'failed')
        self.assertEqual(self.films(library_id=library['id'], status='partial')['total'], 2)

    def test_patch_writes_nfo_with_backup_and_detects_external_edit(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        response = self.client.patch('/api/library/films/' + film['id'],
                                     json={'dvdid': 'DEF-456', 'title': 'Filled In', 'actress': ['Actor B'],
                                           'studio': 'Studio Y', 'release_date': '2024-02-03'})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()['film']
        self.assertEqual(saved['title'], 'Filled In')
        nfo_path = Path(saved['nfo_path'])
        self.assertTrue(nfo_path.is_file())
        self.assertIn('Filled In', nfo_path.read_text(encoding='utf-8'))
        # 再改一次：应保留一份 .bak
        self.client.patch('/api/library/films/' + film['id'], json={'dvdid': 'DEF-456', 'title': 'Second Edit'})
        self.assertTrue(nfo_path.with_suffix(nfo_path.suffix + '.bak').is_file())
        # 外部改动后使用过期 mtime 保存 → 409
        nfo = self.client.get('/api/library/films/' + film['id'] + '/nfo').json()
        stale = nfo['mtime']
        time.sleep(0.01)
        nfo_path.write_text(nfo_path.read_text(encoding='utf-8') + '\n', encoding='utf-8')
        os.utime(nfo_path, None)
        conflict = self.client.patch('/api/library/films/' + film['id'],
                                     json={'dvdid': 'DEF-456', 'title': 'Clobber', 'expected_nfo_mtime': stale})
        self.assertEqual(conflict.status_code, 409, conflict.text)

    def test_raw_nfo_roundtrip_and_validation(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        raw = self.client.get('/api/library/films/' + film['id'] + '/nfo').json()
        self.assertFalse(raw['content'])
        good = '<movie><title>Raw Title</title><uniqueid type="num">DEF-456</uniqueid></movie>'
        written = self.client.put('/api/library/films/' + film['id'] + '/nfo',
                                  json={'content': good, 'expected_mtime': raw['mtime']})
        self.assertEqual(written.status_code, 200, written.text)
        self.assertIn('Raw Title', written.json()['nfo']['content'])
        bad = self.client.put('/api/library/films/' + film['id'] + '/nfo', json={'content': '<movie><title>'})
        self.assertEqual(bad.status_code, 400, bad.text)

    def test_scrape_creates_library_task_with_forced_avid(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        response = self.client.post('/api/library/films/' + film['id'] + '/scrape',
                                    json={'dvdid': 'ABC-999', 'preset_id': 'default'})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertTrue(payload['task_id'])
        task = storage.get_task_record(payload['task_id'])
        self.assertEqual(task['source'], 'library')
        self.assertEqual(task['forced_avid'], 'ABC-999')
        self.assertEqual(task['library_film_id'], film['id'])
        self.assertEqual(payload['film']['scrape_state'], 'queued')
        self.assertEqual(payload['film']['dvdid'], 'ABC-999')
        # 重复发起被拒绝
        again = self.client.post('/api/library/films/' + film['id'] + '/scrape', json={})
        self.assertEqual(again.status_code, 400, again.text)

    def test_batch_scrape_reports_skipped_films(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        response = self.client.post('/api/library/films/scrape', json={'ids': [film['id'], 'missing-id']})
        self.assertEqual(response.status_code, 202, response.text)
        payload = response.json()
        self.assertEqual(len(payload['accepted']), 1)
        self.assertEqual([item['id'] for item in payload['skipped']], ['missing-id'])

    def test_deleted_folders_are_removed_from_the_library(self):
        """源文件被删除后，扫描会把对应记录从媒体库移除，而不是留成“文件已丢失”。"""
        library = self.create_library()
        target = self.media / 'DEF-456'
        for path in sorted(target.rglob('*'), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        target.rmdir()
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        result = self.client.get('/api/library/scans/' + scan['id']).json()['scan']
        self.assertEqual(result['removed'], 1)
        payload = self.films(limit=200)
        self.assertEqual(payload['total'], 2, '被删除的文件应从索引移除')
        self.assertNotIn('DEF-456', [item['dvdid'] for item in payload['items']])
        self.assertEqual(payload['stats']['missing'], 0, '不应留下“文件已丢失”记录')
        self.assertTrue((self.media / 'ABC-123' / 'ABC-123.mp4').is_file())

    def test_deleted_single_file_removes_only_that_record(self):
        library = self.create_library()
        (self.media / 'ABC-123' / 'ABC-123.mp4').unlink()
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        remaining = [item['dvdid'] for item in self.films(library_id=library['id'], limit=200)['items']]
        self.assertNotIn('ABC-123', remaining)
        self.assertIn('DEF-456', remaining)

    def test_unavailable_folder_keeps_its_records(self):
        """整个文件夹不可访问（如移动硬盘未挂载）时不能清空它名下的影片。"""
        library = self.create_library()
        self.client.put(f'/api/library/libraries/{library["id"]}', json={'preset_id': 'default'})
        folder = self.client.get('/api/library/libraries').json()['libraries'][0]['folders'][0]
        moved = Path(self.temp.name) / 'offline-media'
        self.media.rename(moved)
        self.addCleanup(lambda: moved.rename(self.media) if moved.exists() and not self.media.exists() else None)
        refused = self.client.post(f'/api/library/libraries/{library["id"]}/scan')
        self.assertEqual(refused.status_code, 404, refused.text)
        self.assertIn('不可访问', refused.json()['detail'])
        self.assertEqual(self.films(library_id=library['id'], limit=200)['total'], 3, '不可访问时不能删记录')

    def test_deleted_organized_files_are_removed_too(self):
        """已整理（文件搬到整理目录）的影片，文件被删后同样会从库里移除。"""
        library = self.create_library()
        film = self.film_by_dvdid('DEF-456')
        outside = Path(self.temp.name) / '#整理完成' / 'DEF-456'
        outside.mkdir(parents=True)
        target = outside / 'DEF-456.mp4'
        (self.media / 'DEF-456' / 'DEF-456.mp4').rename(target)
        task = {'status': 'succeeded', 'file_organizer': {'organized_files': [str(target)]}}
        library_store.refresh_film_after_task(film['id'], 'succeeded', '', task)
        self.assertEqual(library_store.get_film(film['id'], reconcile=False)['detached'], 1)
        target.unlink()
        scan = self.client.post(f'/api/library/libraries/{library["id"]}/scan').json()['scan']
        self.wait_scan(scan['id'])
        self.assertIsNone(library_store.get_film(film['id'], reconcile=False))
        self.assertNotIn('DEF-456', [item['dvdid'] for item in self.films(library_id=library['id'], limit=200)['items']])

    def test_task_completion_refreshes_the_index_from_disk(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        # 模拟“任务已在刮削、引擎已写回 NFO 与封面”
        self.client.patch('/api/library/films/' + film['id'], json={'dvdid': 'DEF-456', 'title': 'Scraped'})
        Image.new('RGB', (24, 36), 'red').save(self.media / 'DEF-456' / 'poster.jpg')
        with library_store.connection() as db:
            db.execute("UPDATE films SET scrape_state='running' WHERE id=?", (film['id'],))
        library_store.refresh_film_after_task(film['id'], 'succeeded')
        refreshed = self.film_by_dvdid('DEF-456')
        self.assertEqual(refreshed['scrape_state'], 'idle')
        self.assertEqual(refreshed['status'], 'complete')
        self.assertTrue(refreshed['cover_path'].endswith('poster.jpg'))

    def test_failed_task_keeps_the_manual_number_and_the_reason(self):
        self.create_library()
        film = self.film_by_dvdid('DEF-456')
        self.client.patch('/api/library/films/' + film['id'], json={'dvdid': 'MANUAL-7', 'title': 'Manual'})
        library_store.refresh_film_after_task(film['id'], 'failed', '所有数据源均未取得封面')
        refreshed = library_store.get_film(film['id'], reconcile=False)
        self.assertEqual(refreshed['dvdid'], 'MANUAL-7')
        self.assertEqual(refreshed['dvdid_source'], 'manual')
        self.assertEqual(refreshed['scrape_state'], 'failed')
        self.assertIn('所有数据源均未取得封面', refreshed['last_error'])

    def test_cover_endpoint_serves_the_indexed_image(self):
        self.create_library()
        film = self.film_by_dvdid('ABC-123')
        response = self.client.get('/api/library/films/' + film['id'] + '/cover')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content)
        self.assertEqual(response.headers['content-type'], 'image/jpeg')
        cached = self.client.get('/api/library/films/' + film['id'] + '/cover',
                                 headers={'if-none-match': response.headers['etag']})
        self.assertEqual(cached.status_code, 304)


class ForcedAvidEngineTests(unittest.TestCase):
    def load(self):
        import javsp.__main__ as engine
        return engine

    def movie(self, files):
        from types import SimpleNamespace
        return SimpleNamespace(dvdid='OLD-001', files=[str(path) for path in files])

    def test_forced_avid_overrides_detected_number(self):
        engine = self.load()
        with patch.dict(os.environ, {'JAVSP_FORCED_AVID': 'new-999'}, clear=False):
            movie = engine.apply_forced_avid(self.movie(['C:/media/DEF-456.mp4']))
        self.assertEqual(movie.dvdid, 'NEW-999')

    def test_forced_avid_only_applies_to_the_target_file(self):
        engine = self.load()
        with patch.dict(os.environ, {'JAVSP_FORCED_AVID': 'NEW-999', 'JAVSP_FORCED_AVID_PATH': 'C:/media/OTHER.mp4'}, clear=False):
            other = engine.apply_forced_avid(self.movie(['C:/media/OTHER.mp4']))
            untouched = engine.apply_forced_avid(self.movie(['C:/media/DIFFERENT.mp4']))
        self.assertEqual(other.dvdid, 'NEW-999')
        self.assertEqual(untouched.dvdid, 'OLD-001')

    def test_no_env_keeps_the_detected_number(self):
        engine = self.load()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('JAVSP_FORCED_AVID', None)
            movie = engine.apply_forced_avid(self.movie(['C:/media/DEF-456.mp4']))
        self.assertEqual(movie.dvdid, 'OLD-001')


if __name__ == '__main__':
    unittest.main()
