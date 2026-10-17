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
        long_target = self.media / 'ABC-123' / ('超长文件名' * 40 + '.mp4')
        long_target.write_bytes(VIDEO_BYTES)
        self.assertGreater(len(str(long_target)), 240)
        with patch.object(self.server, '_windows_short_path', return_value=r'C:\T\LONG~1.MP4'):
            command = self.server._reveal_command(long_target)
        self.assertEqual(command, ['explorer', '/select,C:\\T\\LONG~1.MP4'])
        # 没有短路径时不再调用 explorer（否则会打开“文档”），而是打开父目录
        with patch.object(self.server, '_windows_short_path', return_value=''):
            self.assertIsNone(self.server._reveal_command(long_target))
            opened = []
            with patch.object(self.server, '_focus_opened_folder'), \
                    patch.object(self.server.subprocess, 'Popen') as popen, \
                    patch.object(self.server.os, 'startfile', side_effect=lambda p: opened.append(p), create=True):
                result = self.server._reveal_in_file_manager(long_target)
            self.assertFalse(popen.called, '没有短路径时不应再交给 explorer')
            self.assertEqual(opened, [str(self.media / 'ABC-123')])
            self.assertEqual(result, self.media / 'ABC-123')

    def test_reveal_keeps_short_paths_untouched(self):
        library = self.create_library()
        film = self.film_by_dvdid('ABC-123')
        target = self.media / 'ABC-123' / 'ABC-123.mp4'
        with patch.object(self.server, '_windows_short_path') as short:
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
