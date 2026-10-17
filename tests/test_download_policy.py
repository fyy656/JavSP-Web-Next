"""Offline regressions for the qBittorrent takeover policy.

qBittorrent encodes share limits as -2 (use global), -1 (unlimited) and >=0 (threshold).
The takeover settings used to offer only -1, so enabling it silently rewrote every
matching torrent from `use global` to `unlimited`, i.e. it cancelled the user's seeding
policy. These tests pin the -2 option and its default.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

from pydantic import ValidationError
from javsp_web import qbittorrent, storage


class DownloadPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        old = storage.DATA_DIR
        for name, value in list(vars(storage).items()):
            if isinstance(value, Path) and value.is_relative_to(old):
                self.enterContext(patch.object(storage, name, self.folder / value.relative_to(old)))
        with patch('threading.Thread.start'):
            from javsp_web import server
        self.server = server

    def test_default_share_limits_keep_the_global_policy(self):
        public = self.server._public_qbittorrent_management({})
        self.assertEqual(public['ratio_limit'], -2)
        self.assertEqual(public['seeding_time_limit'], -2)
        self.assertEqual(public['inactive_seeding_time_limit'], -2)
        # 限速沿用 qBittorrent 的 0=不限速，保持 -1 语义即可
        self.assertEqual(public['download_limit_kib'], -1)
        self.assertEqual(public['upload_limit_kib'], -1)

    def test_share_limit_values_accept_global_unlimited_and_threshold(self):
        body = self.server.QbittorrentManagementBody(ratio_limit=-2, seeding_time_limit=-1, inactive_seeding_time_limit=120)
        self.assertEqual((body.ratio_limit, body.seeding_time_limit, body.inactive_seeding_time_limit), (-2, -1, 120))
        with self.assertRaises(ValidationError):
            self.server.QbittorrentManagementBody(ratio_limit=-3)

    def test_saved_global_choice_round_trips_and_reaches_qbittorrent(self):
        storage.save_qbittorrent_management({'takeover_enabled': True})
        for key in ('ratio_limit', 'seeding_time_limit', 'inactive_seeding_time_limit'):
            self.assertEqual(storage.get_qbittorrent_management().get(key, -2), -2)
        captured = {}

        def request(opener, url, headers, *, data=None):
            captured[url.rsplit('/', 1)[-1]] = data
            return b''

        settings = {'url': 'http://127.0.0.1:8080', 'username': 'u', 'password': 'p'}
        with patch.object(qbittorrent, '_open', return_value=('http://127.0.0.1:8080', object(), {})), \
             patch.object(qbittorrent, '_request', side_effect=request):
            qbittorrent.set_share_limits(settings, 'abc123', {'ratio_limit': -2, 'seeding_time_limit': -1, 'inactive_seeding_time_limit': 60})
        self.assertEqual(captured['setShareLimits'],
                         {'hashes': 'abc123', 'ratioLimit': -2, 'seedingTimeLimit': -1, 'inactiveSeedingTimeLimit': 60})

    def test_limits_match_recognizes_global_unlimited_and_threshold(self):
        management = {'download_limit_kib': -1, 'upload_limit_kib': -1, 'ratio_limit': -2,
                      'seeding_time_limit': -1, 'inactive_seeding_time_limit': 120}
        item = {'download_limit': 0, 'upload_limit': 0, 'ratio_limit': -2.0,
                'seeding_time_limit': -1.0, 'inactive_seeding_time_limit': 120.0}
        self.assertTrue(self.server._torrent_limits_match(item, management))
        self.assertFalse(self.server._torrent_limits_match(item | {'ratio_limit': -1.0}, management))
        self.assertFalse(self.server._torrent_limits_match(item | {'download_limit': 1024}, management))
        self.assertFalse(self.server._torrent_limits_match(item | {'inactive_seeding_time_limit': None}, management))

    def management_pass(self, downloads):
        management = {'takeover_enabled': True, 'takeover_tags': '', 'takeover_category': '',
                      'download_limit_kib': -1, 'upload_limit_kib': -1, 'ratio_limit': -2,
                      'seeding_time_limit': -2, 'inactive_seeding_time_limit': -2, 'auto_remove': False}
        with patch.object(self.server, 'get_qbittorrent_management', return_value=management), \
             patch.object(self.server, 'list_downloaders', return_value=[{'name': 'qB', 'url': 'http://127.0.0.1:8080'}]), \
             patch.object(self.server, 'list_downloads', return_value=downloads), \
             patch.object(self.server, '_auto_scrape_downloads'), \
             patch.object(self.server, 'set_torrent_transfer_limits') as speed, \
             patch.object(self.server, 'set_share_limits') as share:
            self.server._run_qbittorrent_management_once()
            return speed, share

    def test_unchanged_torrents_are_not_rewritten_every_minute(self):
        torrent = {'hash': 'aa', 'name': 'A', 'progress': 100, 'category': '', 'tags': '',
                   'download_limit': 0, 'upload_limit': 0, 'ratio_limit': -2,
                   'seeding_time_limit': -2, 'inactive_seeding_time_limit': -2}
        speed, share = self.management_pass([torrent])
        speed.assert_not_called()
        share.assert_not_called()

    def test_changed_torrent_limits_are_still_enforced(self):
        torrent = {'hash': 'aa', 'name': 'A', 'progress': 100, 'category': '', 'tags': '',
                   'download_limit': 0, 'upload_limit': 0, 'ratio_limit': -1,
                   'seeding_time_limit': 30, 'inactive_seeding_time_limit': -2}
        speed, share = self.management_pass([torrent])
        speed.assert_called_once()
        share.assert_called_once()

    def test_auto_remove_ignores_global_and_unlimited_limits(self):
        settings = {'url': 'http://127.0.0.1:8080', 'username': 'u', 'password': 'p'}
        item = {'hash': 'h', 'progress': 100, 'ratio': 5, 'seeding_time': 10 ** 9, 'inactive_seeding_time': 10 ** 9}
        with patch.object(self.server, 'delete_torrent') as delete:
            removed = self.server._auto_remove_downloads(
                settings, [item],
                {'auto_remove': True, 'ratio_limit': -2, 'seeding_time_limit': -1, 'inactive_seeding_time_limit': -2})
        self.assertEqual(removed, [])
        delete.assert_not_called()
        with patch.object(self.server, 'delete_torrent') as delete:
            removed = self.server._auto_remove_downloads(
                settings, [item],
                {'auto_remove': True, 'ratio_limit': 2, 'seeding_time_limit': -2, 'inactive_seeding_time_limit': -2})
        self.assertEqual(removed, ['h'])
        delete.assert_called_once()


if __name__ == '__main__':
    unittest.main()
