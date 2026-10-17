"""Offline regressions for multipart discovery, task manifests and organization."""
import ast
import copy
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Dict
from unittest.mock import Mock, patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor/JavSP'))

from javsp.config import Cfg
from javsp.datatype import Movie, MovieInfo
from javsp.file import replace_illegal_chars, scan_movies
from javsp.multipart import group_files, part_number
from javsp_web import tasks


class MultipartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = yaml.safe_load((ROOT / 'vendor/JavSP/config.yml').read_text(encoding='utf-8'))
        self.data['scanner'].update(minimum_size=10, input_files=None, skip_nfo_dir=False)
        self.config = Cfg.model_validate(self.data)
        for module in ('javsp.avid', 'javsp.file'):
            self.enterContext(patch(f'{module}.Cfg', side_effect=lambda: self.config))

    def files(self, *names):
        result = []
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'x' * 20)
            result.append(path)
        return result

    def test_numeric_parts_and_aliases(self):
        paths = self.files('FC2PPV-4953812-10.mp4', 'FC2-PPV-4953812-02.mkv', 'FC2-4953812-1.mp4')
        self.assertEqual(group_files(paths, self.config.scanner), [[paths[2], paths[1], paths[0]]])
        movies = scan_movies(str(self.root))
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0].part_numbers, [1, 2, 10])

    def test_parts_before_quality_suffix(self):
        paths = self.files('masex.tv@aqumam00046_1_8k.strm', 'masex.tv@aqumam00046_2_8k.strm')
        self.assertEqual([part_number(path, self.config.scanner) for path in paths], [1, 2])
        self.assertEqual(group_files(paths, self.config.scanner), [paths])

    def test_identifiers_are_not_parts(self):
        for name in ('ABC-123.mp4', 'FC2-4953812.mp4', '080826_01.mp4', 'HEYDOUGA-1234-123.mp4'):
            with self.subTest(name=name):
                self.assertIsNone(part_number(self.root / 'ABC-123' / name, self.config.scanner))
        for name, number in [('ABC-123-CD12.mp4', 12), ('ABC-123-part2.mp4', 2), ('ssni00123-10.mp4', 10), ('ssni00123-CD12.mp4', 12), ('ssni00123-pt2.mp4', 2)]:
            with self.subTest(name=name):
                self.assertEqual(part_number(name, self.config.scanner), number)

    def test_duplicates_rejected_and_directories_separate(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2PPV-4953812-01.mkv')
        with self.assertRaisesRegex(ValueError, '重复'):
            group_files(paths, self.config.scanner)
        separate = self.files('other/FC2-4953812-2.mp4')
        self.assertEqual(len(group_files([paths[0], separate[0]], self.config.scanner)), 2)

    def test_letter_part_markers_match_the_engine_scanner(self):
        paths = self.files('FCDSS-027/FCDSS-027-B.MP4', 'FCDSS-027/FCDSS-027-A.MP4')
        self.assertEqual([[path.name for path in group] for group in group_files(paths, self.config.scanner)],
                         [['FCDSS-027-A.MP4', 'FCDSS-027-B.MP4']])
        movies = scan_movies(str(self.root))
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0].dvdid, 'FCDSS-027')
        self.assertEqual([Path(path).name for path in movies[0].files], ['FCDSS-027-A.MP4', 'FCDSS-027-B.MP4'])

    def test_letter_parts_with_quality_suffix(self):
        paths = self.files('FCDSS-027/FCDSS-027-A 4K.MP4', 'FCDSS-027/FCDSS-027-B 4K.MP4')
        self.assertEqual([[path.name for path in group] for group in group_files(paths, self.config.scanner)],
                         [['FCDSS-027-A 4K.MP4', 'FCDSS-027-B 4K.MP4']])

    def test_discontinuous_letter_parts_are_rejected(self):
        paths = self.files('FCDSS-027/FCDSS-027-A.MP4', 'FCDSS-027/FCDSS-027-C.MP4')
        with self.assertRaisesRegex(ValueError, '分 P 编号不明确或重复'):
            group_files(paths, self.config.scanner)
        self.assertEqual(scan_movies(str(self.root)), [])

    def test_unrelated_same_identifier_files_are_still_rejected(self):
        paths = self.files('FCDSS-027/FCDSS-027-A.MP4', 'FCDSS-027/FCDSS-027-花絮.MP4')
        with self.assertRaisesRegex(ValueError, '分 P 编号不明确或重复'):
            group_files(paths, self.config.scanner)

    def test_preset_identifier_rules_do_not_change_global_config(self):
        data = copy.deepcopy(self.data)
        data['scanner']['media_types'] = [
            {'id': 'custom', 'name': 'Custom', 'priority': 100, 'pattern': r'CUSTOM(?P<avid>\d+)', 'avid_format': 'X-{avid}'},
            {'id': 'normal', 'name': 'Normal', 'detector': 'fallback'},
        ]
        scanner = Cfg.model_validate(data).scanner
        paths = self.files('CUSTOM123-1.mp4', 'CUSTOM123-2.mp4')
        self.assertEqual(group_files(paths, scanner), [paths])
        from javsp.multipart import media_identity
        self.assertEqual(media_identity(paths[0], scanner), ('dvdid', 'x-123'))
        self.assertNotEqual(media_identity(paths[0], self.config.scanner), ('dvdid', 'x-123'))

    def test_single_video_and_hard_link_parts(self):
        single = self.files('ABC-123.mp4')[0]
        movie = scan_movies(str(single))[0]
        output = self.root / 'organized'
        output.mkdir()
        movie.save_dir, movie.basename = str(output), movie.dvdid
        movie.rename_files()
        self.assertTrue((output / 'ABC-123.mp4').is_file())
        paths = self.files('FC2-4953812-1.strm', 'FC2-4953812-2.strm')
        self.data['scanner']['input_files'] = list(map(str, paths))
        self.config = Cfg.model_validate(self.data)
        movie = scan_movies(str(paths[0]))[0]
        movie.save_dir, movie.basename = str(output), movie.dvdid
        movie.rename_files(use_hardlink=True)
        self.assertTrue(all(path.exists() for path in paths))
        for number, original in enumerate(paths, 1):
            self.assertTrue(os.path.samefile(original, output / f'FC2-4953812-CD{number}.strm'))

    def test_manifest_excludes_siblings_and_keeps_small_tail(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4', 'ABC-123.mp4')
        paths[1].write_bytes(b'x')
        self.data['scanner']['input_files'] = list(map(str, paths[:2]))
        self.config = Cfg.model_validate(self.data)
        movies = scan_movies(str(paths[0]))
        self.assertEqual(len(movies), 1)
        self.assertEqual(movies[0].files, list(map(str, paths[:2])))
        paths[1].unlink()
        with self.assertRaises(FileNotFoundError):
            scan_movies(str(paths[0]))

    def create_web_tasks(self):
        with patch.object(tasks, '_preset_config_data', return_value=(copy.deepcopy(self.data), {'name': 'Test'})), \
             patch.object(tasks, '_preset_task_concurrency', return_value=2), \
             patch.object(tasks, 'DATA_DIR', self.root / 'state'), \
             patch.object(tasks, '_logs', {}), \
             patch.object(tasks, '_persist'), patch('javsp_web.task_store.upsert_many'), \
             patch.object(tasks, '_enqueue_task') as thread:
            result = tasks.create_tasks(str(self.root), source='schedule', schedule_id='schedule-test')
        return result, thread

    def test_web_creates_one_task_per_movie_with_persisted_manifest(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4', 'ABC-123.mp4')
        paths[0].write_bytes(b'x')
        created, thread = self.create_web_tasks()
        self.assertEqual(len(created), 2)
        self.assertEqual(thread.call_count, 2)
        task = next(item for item in created if len(item['input_files']) == 2)
        self.assertEqual(task['size_bytes'], 21)
        self.assertEqual(task['source'], 'schedule')
        self.assertEqual(task['schedule_id'], 'schedule-test')
        config = yaml.safe_load(Path(task['config_path']).read_text(encoding='utf-8'))
        self.assertEqual(config['scanner']['input_files'], list(map(str, paths[:2])))
        self.config = Cfg.model_validate(config)
        self.assertEqual(scan_movies(task['input_directory'])[0].part_numbers, [1, 2])

    def test_web_ambiguity_does_not_start_any_tasks(self):
        self.files('ABC-123.mp4', 'FC2-4953812-1.mp4', 'FC2PPV-4953812-1.mkv')
        with patch.object(tasks, 'create_task') as create:
            with self.assertRaises(ValueError):
                self.create_web_tasks()
            create.assert_not_called()

    def test_remaining_part_keeps_number_and_does_not_overwrite(self):
        paths = self.files('FC2-4953812-1.mp4')
        movie = scan_movies(str(paths[0]))[0]
        output = self.root / 'organized'
        output.mkdir()
        movie.save_dir, movie.basename = str(output), movie.dvdid
        existing = output / 'FC2-4953812-CD2.mp4'
        existing.write_bytes(b'existing')
        movie.rename_files()
        self.assertEqual((output / 'FC2-4953812-CD1.mp4').read_bytes(), b'x' * 20)
        self.assertEqual(existing.read_bytes(), b'existing')

    def test_collision_checked_before_any_part_moves(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4')
        movie = scan_movies(str(self.root))[0]
        output = self.root / 'organized'
        output.mkdir()
        movie.save_dir, movie.basename = str(output), movie.dvdid
        (output / 'FC2-4953812-CD2.mp4').write_bytes(b'existing')
        movie.rename_files()
        self.assertTrue(all(path.exists() for path in paths))
        self.assertFalse((output / 'FC2-4953812-CD1.mp4').exists())
        self.assertEqual((output / 'FC2-4953812-CD2.mp4').read_bytes(), b'existing')

    def test_existing_movie_policies_and_equal_versions(self):
        for policy, size, newer, replaced in [('never', 5, True, False), ('always', 40, False, True),
                                              ('larger', 5, False, True), ('larger', 40, True, False),
                                              ('larger', 20, True, False), ('newer', 40, True, True),
                                              ('newer', 5, False, False)]:
            with self.subTest(policy=policy, size=size, newer=newer):
                source = self.files('incoming/ABC-123.mp4')[0]
                movie = scan_movies(str(source))[0]
                output = self.root / 'organized'
                output.mkdir(exist_ok=True)
                target = output / 'ABC-123.mp4'
                target.write_bytes(b'o' * size)
                os.utime(source, (200, 200))
                os.utime(target, (100 if newer else 300, 100 if newer else 300))
                movie.save_dir, movie.basename = str(output), 'ABC-123'
                movie.rename_files(policy=policy)
                self.assertEqual(target.read_bytes(), b'x' * 20 if replaced else b'o' * size)
                self.assertEqual(source.exists(), not replaced)

    def test_overwrite_failure_restores_all_parts_and_existing_videos(self):
        paths = self.files('incoming/FC2-4953812-1.mp4', 'incoming/FC2-4953812-2.mp4')
        movie = scan_movies(str(paths[0].parent))[0]
        output = self.root / 'organized'
        output.mkdir()
        targets = [output / f'FC2-4953812-CD{number}.mp4' for number in (1, 2)]
        for target in targets:
            target.write_bytes(b'original')
        movie.save_dir, movie.basename = str(output), movie.dvdid
        import shutil
        real_move = shutil.move
        def move(src, dst):
            if str(src) == str(paths[1]):
                raise OSError('simulated failure')
            return real_move(src, dst)
        with patch('javsp.datatype.shutil.move', side_effect=move), self.assertRaises(OSError):
            movie.rename_files(policy='always')
        self.assertTrue(all(path.read_bytes() == b'x' * 20 for path in paths))
        self.assertTrue(all(path.read_bytes() == b'original' for path in targets))
        self.assertFalse(list(output.glob('.javsp-backup-*')))

    def test_never_preserves_existing_artwork_before_downloads(self):
        source = self.files('incoming/ABC-123.mp4')[0]
        movie = scan_movies(str(source))[0]
        output = self.root / 'organized'
        output.mkdir()
        poster = output / 'poster.jpg'
        poster.write_bytes(b'original image')
        movie.save_dir, movie.basename, movie.poster_file = str(output), 'ABC-123', str(poster)
        self.assertFalse(movie.prepare_organization('never'))
        self.assertEqual(poster.read_bytes(), b'original image')
        self.assertTrue(source.exists())

    def test_existing_version_with_different_extension_is_compared(self):
        source = self.files('incoming/ABC-123.mp4')[0]
        movie = scan_movies(str(source))[0]
        output = self.root / 'organized'
        output.mkdir()
        old = output / 'ABC-123.mkv'
        old.write_bytes(b'o' * 40)
        movie.save_dir, movie.basename = str(output), 'ABC-123'
        movie.rename_files(policy='larger')
        self.assertTrue(source.exists())
        self.assertFalse((output / 'ABC-123.mp4').exists())
        movie.rename_files(policy='always')
        self.assertFalse(old.exists())
        self.assertEqual((output / 'ABC-123.mp4').read_bytes(), b'x' * 20)

    def test_never_preserves_alternate_cover_formats_and_stills(self):
        source = self.files('incoming/ABC-123.mp4')[0]
        for asset in ('poster.png', 'extrafanart/0.png'):
            with self.subTest(asset=asset):
                movie = scan_movies(str(source))[0]
                output = self.root / asset.replace('/', '-')
                existing = output / asset
                existing.parent.mkdir(parents=True)
                existing.write_bytes(b'original image')
                movie.save_dir, movie.basename = str(output), 'ABC-123'
                movie.poster_file = str(output / 'poster.jpg')
                self.assertFalse(movie.prepare_organization('never'))
                self.assertEqual(existing.read_bytes(), b'original image')
                self.assertTrue(source.exists())

    def stills_config(self, *, output_pattern=None, folder_pattern=None, base_pattern='{num}', move_files=False, **extra_fanarts):
        """Build a preset for stills/naming tests.

        ``move_files`` must stay false when ``generate_names`` is mocked (the stills folder is
        then taken from ``movie.save_dir``); pass true when the real naming rules must run,
        because the engine only applies ``output_folder_pattern`` when files are organized.
        """
        data = copy.deepcopy(self.data)
        data['summarizer']['move_files'] = move_files
        data['translator']['engine'] = None
        data['summarizer']['extra_fanarts'].update({'enabled': True, **extra_fanarts})
        pattern = folder_pattern or (str(output_pattern) if output_pattern else None)
        if pattern:
            data['summarizer']['path']['output_folder_pattern'] = pattern
        if pattern or base_pattern != '{num}':
            data['summarizer']['path']['basename_pattern'] = base_pattern
        return Cfg.model_validate(data)

    def stills_movie(self, source, output, *, extrafanart_dir=None):
        movie = scan_movies(str(source))[0]
        movie.save_dir, movie.basename = str(output), 'ABC-123'
        movie.nfo_file = str(output / 'movie.nfo')
        movie.fanart_file = str(output / 'fanart.jpg')
        movie.poster_file = str(output / 'poster.jpg')
        movie.extrafanart_dir = extrafanart_dir
        movie.info = MovieInfo(movie.dvdid)
        movie.info.title = 'Test'
        movie.info.big_covers, movie.info.covers = [], []
        movie.info.preview_pics = ['https://example.test/image.png']
        return movie

    def run_normal_mode(self, movie, config):
        """Run the vendored RunNormalMode body with mocked naming, downloads and NFO writes."""
        events = []
        tree = ast.parse((ROOT / 'vendor/JavSP/javsp/__main__.py').read_text(encoding='utf-8'))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'RunNormalMode')
        namespace = dict(os=os, time=Mock(), Cfg=lambda: config, logger=logging.getLogger('multipart-test'),
                         tqdm=lambda *args, **kwargs: args[0] if args else Mock(), progress_enabled=lambda: True,
                         progress_event=lambda stage, **kwargs: events.append((stage, kwargs)),
                         fallback_media_type_id=lambda: 'normal', parallel_crawler=lambda *args: [movie.info],
                         info_summary=lambda *args: True, generate_names=lambda *args: None,
                         download_cover=lambda *args, **kwargs: None, download=lambda *args: {},
                         valid_pic=lambda *args: True, get_fmt_size=lambda *args: '1 B',
                         get_pic_size=lambda *args: (1, 1), write_nfo=Mock())
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'RunNormalMode', 'exec'), namespace)
        return namespace['RunNormalMode']([movie]), events

    def naming_helpers(self, config):
        """Extract the vendored naming helpers so file-name rules are tested directly."""
        tree = ast.parse((ROOT / 'vendor/JavSP/javsp/__main__.py').read_text(encoding='utf-8'))
        wanted = ('extra_fanart_dir', 'generate_names')
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
        namespace = dict(os=os, Cfg=lambda: config, logger=logging.getLogger('multipart-test'), Dict=Dict,
                         split_by_punc=lambda value: [value], replace_illegal_chars=replace_illegal_chars,
                         get_remaining_path_len=lambda path: 100, Movie=Movie, MovieInfo=MovieInfo)
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'stills-naming', 'exec'), namespace)
        return namespace

    def test_naming_templates_can_use_the_original_file_name(self):
        output = self.root / 'organized'
        config = self.stills_config(folder_pattern=str(output / '{filename}'), base_pattern='{filename}', move_files=True)
        namespace = self.naming_helpers(config)
        movie = self.stills_movie(self.files('hhd800.com@SSIS-247.mp4')[0], output)
        namespace['generate_names'](movie)
        self.assertEqual(Path(movie.save_dir), output / 'hhd800.com@SSIS-247')
        self.assertEqual(movie.basename, 'hhd800.com@SSIS-247')

    def test_stills_folder_name_follows_file_name_variables(self):
        output = self.root / 'organized'
        namespace = self.naming_helpers(self.stills_config(output_pattern=output, move_files=True, folder_prefix='{num}-'))
        movie = self.stills_movie(self.files('ABC-123.mp4')[0], output)
        namespace['generate_names'](movie)
        self.assertEqual(Path(movie.extrafanart_dir), output / 'ABC-123-extrafanart')

        default_prefix = self.stills_config().summarizer.extra_fanarts.folder_prefix
        self.assertEqual(default_prefix, '')
        self.assertEqual(namespace['extra_fanart_dir']('/out', default_prefix, {'num': 'ABC-123'}), os.path.join('/out', 'extrafanart'))

    def test_stills_prefix_keeps_a_direct_child_folder(self):
        namespace = self.naming_helpers(self.stills_config(folder_prefix='../{num}/'))
        sanitized = namespace['extra_fanart_dir']('/out', '../{num}/', {'num': 'ABC-123'})
        self.assertEqual(Path(sanitized).parent, Path('/out'))
        self.assertTrue(Path(sanitized).name.endswith('extrafanart'))
        self.assertNotIn('..', Path(sanitized).name)

    def test_stills_are_written_into_the_configured_folder(self):
        output = self.root / 'organized'
        movie = self.stills_movie(self.files('ABC-123.mp4')[0], output, extrafanart_dir=str(output / 'ABC-123-extrafanart'))
        returned, events = self.run_normal_mode(movie, self.stills_config(folder_prefix='{num}-'))
        self.assertEqual(returned, [movie])
        self.assertTrue((output / 'ABC-123-extrafanart').is_dir())
        self.assertFalse((output / 'extrafanart').exists())
        reported = [event.get('stills_dir') for stage, event in events if stage == 'output' and event.get('stills_dir')]
        self.assertEqual([Path(value) for value in reported], [output / 'ABC-123-extrafanart'])

    def test_stills_fall_back_to_the_legacy_folder(self):
        output = self.root / 'organized'
        movie = self.stills_movie(self.files('ABC-123.mp4')[0], output)
        returned, _ = self.run_normal_mode(movie, self.stills_config())
        self.assertEqual(returned, [movie])
        self.assertTrue((output / 'extrafanart').is_dir())

    def test_stills_are_ordered_numerically(self):
        output = self.root / 'organized'
        folder = output / 'extrafanart'
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('0.png', '1.png', '2.png', '10.png', '11.png'):
            (folder / name).write_bytes(b'x' * 20)
        self.assertEqual([path.name for path in tasks._fanart_paths('', {'save_dir': str(output)})],
                         ['0.png', '1.png', '2.png', '10.png', '11.png'])

    def test_numbered_stills_with_prefix_are_ordered_numerically(self):
        output = self.root / 'organized'
        folder = output / 'extrafanart'
        folder.mkdir(parents=True, exist_ok=True)
        for name in ('fanart1.png', 'fanart10.png', 'fanart2.png'):
            (folder / name).write_bytes(b'x' * 20)
        self.assertEqual([path.name for path in tasks._fanart_paths('', {'save_dir': str(output)})],
                         ['fanart1.png', 'fanart2.png', 'fanart10.png'])

    def test_web_client_reads_prefixed_and_legacy_stills(self):
        output = self.root / 'organized'
        for directory in ('extrafanart', 'ABC-123-extrafanart', 'extrafanart-notes', 'Custom-stills'):
            folder = output / directory
            folder.mkdir(parents=True, exist_ok=True)
            (folder / '0.png').write_bytes(b'x' * 20)
        (output / 'extrafanart' / '1.png').write_bytes(b'x' * 20)
        found = [path.parent.name for path in tasks._fanart_paths('', {'save_dir': str(output)})]
        self.assertEqual(found, ['ABC-123-extrafanart', 'extrafanart', 'extrafanart'])
        # 引擎上报的目录优先，即使目录名不以 extrafanart 结尾
        reported = tasks._fanart_paths('', {'save_dir': str(output), 'stills_dir': str(output / 'Custom-stills')})
        self.assertIn(output / 'Custom-stills' / '0.png', reported)
        # 没有 save_dir 时按输入目录递归查找，兼容升级前的任务记录
        recursive = [path.parent.name for path in tasks._fanart_paths(str(output), None)]
        self.assertIn('ABC-123-extrafanart', recursive)
        self.assertEqual(tasks._stills_target({'save_dir': str(output)}), output / 'extrafanart')
        self.assertEqual(tasks._stills_target({'save_dir': str(output), 'stills_dir': str(output / 'Custom-stills')}), output / 'Custom-stills')
        self.assertIsNone(tasks._stills_target({}))

    def test_existing_extrafanart_reaches_organization(self):
        paths = self.files('FC2-4953812-1.mp4', 'FC2-4953812-2.mp4')
        movie = scan_movies(str(self.root))[0]
        output = self.root / 'organized'
        (output / 'extrafanart').mkdir(parents=True)
        sentinel = output / 'extrafanart/keep.txt'
        sentinel.write_text('keep', encoding='utf-8')
        movie.save_dir, movie.basename = str(output), movie.dvdid
        movie.fanart_file, movie.poster_file, movie.nfo_file = [str(output / name) for name in ('fanart.jpg', 'poster.jpg', 'movie.nfo')]
        movie.info = MovieInfo(movie.dvdid)
        movie.info.title = 'Test'
        movie.info.big_covers = []
        movie.info.covers = []
        movie.info.preview_pics = ['https://example.test/image.png']
        self.data['translator']['engine'] = None
        self.data['summarizer']['extra_fanarts']['enabled'] = True
        self.data['summarizer']['move_files'] = True
        self.data['summarizer']['path']['hard_link'] = False
        config = Cfg.model_validate(self.data)
        returned, events = self.run_normal_mode(movie, config)
        self.assertEqual(returned, [movie])
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'keep')
        self.assertTrue((output / 'FC2-4953812-CD1.mp4').exists())
        self.assertTrue((output / 'FC2-4953812-CD2.mp4').exists())
        self.assertTrue(any(stage == 'movie' and event.get('status') == 'completed' for stage, event in events))


if __name__ == '__main__':
    unittest.main()
