"""Manual scans must stay responsive while a mounted filesystem is blocked."""
import copy
from collections import deque
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/JavSP'))
from javsp_web import storage, tasks, task_scans, task_store


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        old = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(old):
                self.enterContext(patch.object(storage, name, self.root / 'state' / value.relative_to(old)))
        self.enterContext(patch.object(tasks, 'DATA_DIR', self.root / 'state'))
        self.enterContext(patch.object(tasks, '_TASK_COVERS_DIR', self.root / 'state/task-covers'))
        self.enterContext(patch.object(tasks, '_logs', {}))
        self.enterContext(patch.object(tasks, '_cancelled_tasks', set()))
        self.enterContext(patch.object(tasks, '_deleted_tasks', set()))
        self.enterContext(patch.object(tasks, '_processes', {}))
        self.enterContext(patch.object(tasks, '_pending_tasks', deque()))
        self.enterContext(patch.object(tasks, '_worker_batches', {}))
        self.enqueue = self.enterContext(patch.object(tasks, '_enqueue_task'))
        self.config = yaml.safe_load((ROOT / 'vendor/JavSP/config.yml').read_text(encoding='utf-8'))
        self.config['scanner'].update(minimum_size=10, input_files=None, skip_nfo_dir=False)
        self.enterContext(patch.object(tasks, '_preset_config_data', side_effect=lambda _: (copy.deepcopy(self.config), {'name': 'Test', 'task_concurrency': 1})))
        with patch('threading.Thread.start'):
            from javsp_web import server
        self.server = server
        self.enterContext(patch.dict(server.app.dependency_overrides, {server.current_user: lambda: {'username': 'test', 'role': 'admin'}}))
        from fastapi.testclient import TestClient
        self.client = TestClient(server.app)
        self.addCleanup(self.client.close)

    def files(self, *names):
        folder = self.root / 'mount'
        folder.mkdir(exist_ok=True)
        paths = []
        for name in names:
            path = folder / name
            path.write_bytes(b'x' * 20)
            paths.append(path)
        return paths

    def submit(self, path, selected=None):
        body = dict(input_directory=str(path), preset_id='default')
        if selected is not None:
            body['input_files'] = list(map(str, selected))
        response = self.client.post('/api/tasks', json=body)
        self.assertEqual(response.status_code, 202, response.text)
        self.assertTrue(response.json()['scan'])
        return response.json()['tasks'][0]

    def children(self, scan):
        return [item for item in storage.load_tasks() if item['id'] != scan['id']]

    def test_submission_polling_and_cancel_never_stat_mount(self):
        mount = self.root / 'unresponsive-mount'
        original_stat = Path.stat

        def stat(path, *args, **kwargs):
            if path == mount:
                raise AssertionError('request touched media mount')
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, 'stat', stat), patch.object(tasks, '_task_name', side_effect=AssertionError('scanned while naming')):
            scan = self.submit(mount)
            response = self.client.get('/api/tasks?limit=20&view=manual')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['items'][0]['task_type'], 'scan')
            self.assertEqual(self.client.get('/api/tasks/' + scan['id']).status_code, 200)
            self.assertEqual(self.client.post('/api/tasks/' + scan['id'] + '/cancel').status_code, 200)

    def blocked_scan(self):
        files = self.files('ABC-123.mp4')
        mount = files[0].parent
        entered, release = threading.Event(), threading.Event()
        workers = []
        original_rglob = Path.rglob

        def rglob(path, *args, **kwargs):
            if path == mount:
                entered.set()
                if not release.wait(5):
                    raise TimeoutError('test scan was not released')
            return original_rglob(path, *args, **kwargs)

        def enqueue(task):
            if task.get('task_type') == 'scan':
                worker = threading.Thread(target=tasks._run_task, args=(task,), daemon=True)
                workers.append(worker)
                worker.start()

        self.enqueue.side_effect = enqueue
        self.enterContext(patch.object(Path, 'rglob', rglob))

        def cleanup():
            release.set()
            for worker in workers:
                worker.join(3)
                self.assertFalse(worker.is_alive())

        self.addCleanup(cleanup)
        start = time.monotonic()
        scan = self.submit(mount)
        self.assertLess(time.monotonic() - start, 1.5)
        self.assertTrue(entered.wait(2))
        self.assertFalse(release.is_set())
        return scan, release, workers

    def test_blocked_directory_returns_202_and_polling_works_before_io_finishes(self):
        scan, release, workers = self.blocked_scan()
        start = time.monotonic()
        detail = self.client.get('/api/tasks/' + scan['id']).json()
        self.assertEqual(detail['status'], 'running')
        self.assertLess(time.monotonic() - start, 1)
        self.assertFalse(release.is_set())
        release.set()
        workers[0].join(3)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'succeeded')
        self.assertEqual(len(self.children(scan)), 1)

    def test_cancel_and_delete_blocked_scan_never_enqueues_or_resurrects(self):
        scan, release, workers = self.blocked_scan()
        self.assertEqual(self.client.post('/api/tasks/' + scan['id'] + '/cancel').status_code, 200)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'cancelled')
        self.assertEqual(self.client.delete('/api/tasks/' + scan['id']).status_code, 200)
        release.set()
        workers[0].join(3)
        self.assertFalse(workers[0].is_alive())
        self.assertEqual(storage.load_tasks(), [])
        self.assertEqual(self.enqueue.call_count, 1)

    def test_multiselect_groups_parts_keeps_small_tail_and_shares_concurrency(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4', 'ABC-123.mp4')
        paths[1].write_bytes(b'x')
        scan = self.submit(paths[0], [*paths, paths[0]])
        tasks._run_task(scan)
        children = self.children(scan)
        self.assertEqual(len(children), 2)
        multipart = next(task for task in children if len(task['input_files']) == 2)
        self.assertEqual(multipart['input_files'], list(map(str, paths[:2])))
        self.assertEqual({task['batch_id'] for task in children}, {scan['id']})
        self.assertEqual({task['task_concurrency'] for task in children}, {1})
        tasks._pending_tasks.extend(children)
        first, batch = tasks._take_pending_task()
        self.assertIsNone(tasks._take_pending_task())
        tasks._worker_batches[batch] -= 1
        self.assertIsNotNone(tasks._take_pending_task())
        finished = storage.get_task_record(scan['id'])
        self.assertEqual(finished['scan']['created_tasks'], 2)
        self.assertEqual(finished['scan']['discovered_files'], 3)

    def test_multiselect_falls_back_when_mount_realpath_is_unsupported(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4')
        scan = self.submit(paths[0], paths)
        original_realpath = tasks.os.path.realpath

        def realpath(path, *args, **kwargs):
            if str(path).startswith(str(paths[0].parent)):
                raise OSError(1005, '底层设备不工作')
            return original_realpath(path)

        with patch.object(tasks.os.path, 'realpath', side_effect=realpath):
            tasks._run_task(scan)
        children = self.children(scan)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'succeeded')
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['input_files'], list(map(str, paths)))

    def mount_file(self, relative):
        path = self.root / 'mount' / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'x' * 20)
        return path

    def scan_children(self, scan):
        tasks._run_task(scan)
        return storage.get_task_record(scan['id']), self.children(scan)

    def test_skip_nfo_dir_ignores_folders_that_already_have_nfo(self):
        self.mount_file('ABC-123/ABC-123.mp4')
        self.mount_file('ABC-123/movie.nfo')
        fresh = self.mount_file('DEF-456/DEF-456.mp4')
        self.config['scanner']['skip_nfo_dir'] = True
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([child['input_files'] for child in children], [[str(fresh)]])

    def test_nfo_below_the_movie_folder_does_not_skip_it(self):
        video = self.mount_file('ABC-123/ABC-123.mp4')
        self.mount_file('ABC-123/subtitles/notes.nfo')
        self.config['scanner']['skip_nfo_dir'] = True
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([child['input_files'] for child in children], [[str(video)]])

    def test_ignored_folder_name_pattern_skips_output_folders(self):
        self.mount_file('#整理完成/ABC-123/ABC-123.mp4')
        fresh = self.mount_file('DEF-456/DEF-456.mp4')
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([child['input_files'] for child in children], [[str(fresh)]])

    def test_filename_extensions_follow_the_preset(self):
        self.mount_file('ABC-123.mp4')
        mkv = self.mount_file('DEF-456.mkv')
        self.config['scanner']['filename_extensions'] = ['.mkv']
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([child['input_files'] for child in children], [[str(mkv)]])

    def test_explicit_selection_bypasses_directory_filters(self):
        scraped = self.mount_file('ABC-123/ABC-123.mp4')
        self.mount_file('ABC-123/movie.nfo')
        self.config['scanner']['skip_nfo_dir'] = True
        result, children = self.scan_children(self.submit(scraped.parent, [scraped]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual([child['input_files'] for child in children], [[str(scraped)]])

    def test_all_skipped_folders_explain_the_reason(self):
        self.mount_file('ABC-123/ABC-123.mp4')
        self.mount_file('ABC-123/movie.nfo')
        self.config['scanner']['skip_nfo_dir'] = True
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(children, [])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('已有 NFO', result['error'])

    def test_empty_filename_extensions_report_a_clear_error(self):
        self.mount_file('ABC-123/ABC-123.mp4')
        self.config['scanner']['filename_extensions'] = []
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(children, [])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('视频文件扩展名为空', result['error'])

    def test_library_scan_skips_scraped_folders_and_keeps_letter_parts_together(self):
        """端到端复现用户库形态：已刮削目录跳过，字母分片合成一部影片。"""
        self.mount_file('ABF-055/ABF-055.MP4')
        self.mount_file('ABF-055/ABF-055.nfo')
        self.mount_file('ABF-055/movie.nfo')
        self.mount_file('FCDSS-027/FCDSS-027-A.MP4')
        self.mount_file('FCDSS-027/FCDSS-027-B.MP4')
        self.mount_file('SNOS-039-4K/SNOS-039-4K.mkv')
        self.mount_file('本編/本編_4k.mp4')
        self.config['scanner']['skip_nfo_dir'] = True
        self.config['scanner']['filename_extensions'] = ['.mp4', '.mkv']
        result, children = self.scan_children(self.submit(self.root / 'mount'))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(
            sorted(sorted(Path(path).name for path in child['input_files']) for child in children),
            sorted([['FCDSS-027-A.MP4', 'FCDSS-027-B.MP4'], ['SNOS-039-4K.mkv'], ['本編_4k.mp4']]),
        )

    def task_record(self, task_id, status):
        return {'id': task_id, 'created_at': '2026-09-23T12:00:00+00:00', 'status': status,
                'input_directory': str(self.root / f'{task_id}.mp4'), 'file_name': task_id, 'size_bytes': 1024}

    def test_cancel_all_stops_queued_and_running_tasks_only(self):
        storage.save_tasks([self.task_record('q1', 'queued'), self.task_record('q2', 'queued'),
                            self.task_record('r1', 'running'), self.task_record('ok', 'succeeded'),
                            self.task_record('bad', 'failed')])
        self.assertEqual(tasks.cancel_tasks(), 3)
        self.assertEqual({item['id']: item['status'] for item in storage.load_tasks()},
                         {'q1': 'cancelled', 'q2': 'cancelled', 'r1': 'cancelled', 'ok': 'succeeded', 'bad': 'failed'})

    def test_cancel_selected_tasks_leaves_other_queued_tasks_alone(self):
        storage.save_tasks([self.task_record('q1', 'queued'), self.task_record('q2', 'queued')])
        self.assertEqual(tasks.cancel_tasks({'q1'}), 1)
        self.assertEqual(storage.get_task_record('q1')['status'], 'cancelled')
        self.assertEqual(storage.get_task_record('q2')['status'], 'queued')

    def test_cancel_api_reports_the_stopped_count(self):
        storage.save_tasks([self.task_record('q1', 'queued'), self.task_record('q2', 'queued'), self.task_record('ok', 'succeeded')])
        response = self.client.post('/api/tasks/cancel', json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'ok': True, 'cancelled': 2})
        storage.save_tasks([self.task_record('q3', 'queued')])
        self.assertEqual(self.client.post('/api/tasks/cancel', json={'ids': ['q3', 'ok']}).json()['cancelled'], 1)
        self.assertEqual(storage.get_task_record('q3')['status'], 'cancelled')
        self.assertEqual(self.client.post('/api/tasks/cancel', json={'ids': ['ok']}).json()['cancelled'], 0)

    def test_worker_survives_a_broken_task_record(self):
        """A raising take-task step must not kill the worker: the queue would freeze."""
        ran = threading.Event()
        calls = {'count': 0}

        def take_pending():
            calls['count'] += 1
            if calls['count'] == 1:
                raise ValueError('损坏的任务记录')
            if calls['count'] == 2:
                tasks._worker_batches['probe'] = tasks._worker_batches.get('probe', 0) + 1
                return {'id': 'probe', 'batch_id': 'probe', 'task_concurrency': 1, 'status': 'queued'}, 'probe'
            raise SystemExit  # 结束本次测试线程

        with patch.object(tasks, '_take_pending_task', side_effect=take_pending), \
             patch.object(tasks, '_run_task', side_effect=lambda task: ran.set()), \
             patch.object(tasks, '_worker_batches', {}), \
             patch.object(tasks.logging, 'exception'):  # 这次取任务失败会被记录，测试里静音
            worker = threading.Thread(target=tasks._task_worker, daemon=True)
            worker.start()
            self.assertTrue(ran.wait(5), '工作线程被一次异常终止，队列会永远停住')
            worker.join(5)
            self.assertFalse(worker.is_alive())

    def test_media_type_filter_skips_unmatched_ad_files(self):
        paths = self.files('FC2-4953812-1.mp4', '台湾uu美少女直播 20年信誉保证服务全球.mp4', '社 区 最 新 情 報.mp4')
        self.config['scanner']['only_match_media_types'] = True
        scan = self.submit(paths[0].parent)
        tasks._run_task(scan)
        children = self.children(scan)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'succeeded')
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['input_files'], [str(paths[0])])

    def test_empty_scan_uses_no_scrape_tasks_error(self):
        empty = self.root / 'mount'
        empty.mkdir()
        scan = self.submit(empty)
        tasks._run_task(scan)
        result = storage.get_task_record(scan['id'])
        self.assertEqual(result['status'], 'failed')
        self.assertIn('输入目录中未找到符合预设最小匹配文件大小的影片文件', result['error'])

    def test_scheduled_empty_scan_is_not_saved_as_failed_run(self):
        schedule = {
            'id': 'empty-schedule', 'name': '空目录', 'enabled': True, 'cron': '* * * * *',
            'input_directory': str(self.root / 'mount'), 'preset_id': 'default',
            'created_at': '2026-10-06T12:00:00+08:00', 'updated_at': '2026-10-06T12:00:00+08:00',
            'last_run_key': '', 'last_run_at': '', 'last_result': '尚未执行', 'runs': [],
        }
        storage.save_auto_scrape_schedules([schedule])
        with patch.object(self.server, 'active_schedule_task_ids', return_value=[]), \
             patch.object(self.server, 'create_tasks', side_effect=tasks.NoScrapeTasksError('输入目录中未找到符合预设最小匹配文件大小的影片文件')):
            claimed, created = self.server._run_auto_scrape_schedule('empty-schedule', '2026-10-06T12:01')
        saved = storage.list_auto_scrape_schedules()[0]
        self.assertEqual(created, [])
        self.assertEqual(claimed['last_result'], '未发现符合条件的影片，已跳过本次运行')
        self.assertEqual(saved['runs'], [])
        self.assertEqual(saved['last_run_key'], '2026-10-06T12:01')

    def test_single_file_still_creates_one_scrape_task(self):
        video = self.files('ABC-123.mp4')[0]
        scan = self.submit(video)
        tasks._run_task(scan)
        children = self.children(scan)
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['input_directory'], str(video))
        self.assertEqual(children[0]['status'], 'queued')

    def test_missing_path_and_empty_directory_are_visible_failures(self):
        for folder in [self.root / 'missing', self.root / 'empty']:
            if folder.name == 'empty':
                folder.mkdir()
            scan = self.submit(folder)
            tasks._run_task(scan)
            result = storage.get_task_record(scan['id'])
            self.assertEqual(result['status'], 'failed')
            self.assertIn('扫描失败', result['error'])
            self.assertEqual(result['scan']['created_tasks'], 0)

    def test_network_enumeration_error_is_recorded_without_partial_batch(self):
        video = self.files('ABC-123.mp4')[0]
        scan = self.submit(video.parent)
        with patch.object(Path, 'rglob', side_effect=OSError('network disconnected')):
            tasks._run_task(scan)
        self.assertIn('network disconnected', storage.get_task_record(scan['id'])['error'])
        self.assertEqual(self.children(scan), [])

    def test_ambiguous_parts_fail_before_any_child_starts(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2PPV-4953812-01.mkv')
        scan = self.submit(paths[0], paths)
        tasks._run_task(scan)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'failed')
        self.assertEqual(self.children(scan), [])
        self.assertEqual(self.enqueue.call_count, 1)

    def test_cancellation_after_preparation_discards_config_and_children(self):
        video = self.files('ABC-123.mp4')[0]
        scan = self.submit(video)
        original = tasks._prepare_tasks

        def prepare(*args, **kwargs):
            prepared = original(*args, **kwargs)
            self.assertTrue(tasks.cancel_task(scan['id']))
            return prepared

        with patch.object(tasks, '_prepare_tasks', side_effect=prepare):
            tasks._run_task(scan)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'cancelled')
        self.assertEqual(self.children(scan), [])
        self.assertEqual(list((self.root / 'state/task-config').glob('*.yml')), [])

    def test_restart_resumes_uncommitted_scan_and_not_completed_scan(self):
        video = self.files('ABC-123.mp4')[0]
        scan = self.submit(video)
        scan['status'] = 'running'
        tasks._persist(scan)
        self.enqueue.reset_mock()
        self.assertEqual(tasks.recover_interrupted_tasks(), 1)
        restored = self.enqueue.call_args.args[0]
        self.assertEqual(restored['task_type'], 'scan')
        tasks._run_task(restored)
        self.assertEqual(storage.get_task_record(scan['id'])['status'], 'succeeded')
        self.enqueue.reset_mock()
        tasks.recover_interrupted_tasks()
        self.assertTrue(all(call.args[0].get('task_type') != 'scan' for call in self.enqueue.call_args_list))
        self.assertEqual(len(self.children(scan)), 1)

    def test_empty_selection_and_blank_paths_are_rejected(self):
        self.assertEqual(self.client.post('/api/tasks', json={'input_directory': 'x', 'input_files': []}).status_code, 422)
        self.assertEqual(self.client.post('/api/tasks', json={'input_directory': 'x', 'input_files': [' ']}).status_code, 400)

    def test_multiselect_dialog_cleanup_and_single_endpoint_compatibility(self):
        self.assertEqual(sum(getattr(route, 'path', None) == '/api/path/select-multi'
                             and 'POST' in getattr(route, 'methods', set())
                             for route in self.client.app.routes), 1)
        root = Mock()
        dialog = types.ModuleType('tkinter.filedialog')
        dialog.askopenfilenames = Mock(return_value=('X:/one.mp4', 'X:/two.mp4'))
        dialog.askopenfilename = Mock(return_value='X:/one.mp4')
        tk = types.ModuleType('tkinter')
        tk.Tk = Mock(return_value=root)
        tk.filedialog = dialog
        with patch.dict(sys.modules, {'tkinter': tk, 'tkinter.filedialog': dialog}), patch.object(self.server, 'IS_DOCKER', False):
            self.assertEqual(self.client.post('/api/path/select-multi', json={'kind': 'file'}).json()['paths'], ['X:/one.mp4', 'X:/two.mp4'])
            self.assertEqual(self.client.post('/api/path/select', json={'kind': 'file'}).json()['path'], 'X:/one.mp4')
            dialog.askopenfilenames.side_effect = RuntimeError('dialog failed')
            self.assertEqual(self.client.post('/api/path/select-multi', json={'kind': 'file'}).status_code, 500)
        self.assertEqual(root.destroy.call_count, 3)
        self.assertFalse(self.server._native_path_lock.locked())


if __name__ == '__main__':
    unittest.main()
