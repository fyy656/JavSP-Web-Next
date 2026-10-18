"""Offline regressions for LAN access: settings, host binding, addresses and firewall rules."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from javsp_web import network_access, storage


def _redirect_storage(test):
    """Point every data path derived from storage.DATA_DIR at a temp directory."""
    directory = test.enterContext(tempfile.TemporaryDirectory(prefix='f03-network-'))
    previous = storage.DATA_DIR
    for name, value in list(vars(storage).items()):
        if isinstance(value, Path) and value.is_relative_to(previous):
            test.enterContext(patch.object(storage, name, Path(directory) / value.relative_to(previous)))
    test.enterContext(patch.dict(os.environ, {}, clear=False))
    os.environ.pop('JAVSP_WEB_PORT', None)


class NetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        _redirect_storage(self)
        storage.ensure_seed_data()

    def test_lan_access_defaults_off_and_binds_loopback(self):
        self.assertFalse(storage.get_network_settings()['lan_access'])
        self.assertEqual(network_access.effective_host(), '127.0.0.1')

    def test_enabling_lan_access_binds_all_interfaces(self):
        storage.save_network_settings({'lan_access': True})
        self.assertTrue(storage.get_network_settings()['lan_access'])
        self.assertEqual(network_access.effective_host(), '0.0.0.0')

    def test_saving_without_key_keeps_current_value(self):
        storage.save_network_settings({'lan_access': True})
        storage.save_network_settings({})
        self.assertTrue(storage.get_network_settings()['lan_access'])
        storage.save_network_settings({'lan_access': False})
        self.assertFalse(storage.get_network_settings()['lan_access'])

    def test_listen_port_prefers_environment_and_falls_back(self):
        os.environ['JAVSP_WEB_PORT'] = '9123'
        self.assertEqual(network_access.listen_port(), 9123)
        os.environ['JAVSP_WEB_PORT'] = 'not-a-number'
        self.assertEqual(network_access.listen_port(), network_access.DEFAULT_PORT)
        os.environ['JAVSP_WEB_PORT'] = '70000'
        self.assertEqual(network_access.listen_port(), network_access.DEFAULT_PORT)


class LanAddressTests(unittest.TestCase):
    def setUp(self):
        _redirect_storage(self)

    def test_addresses_exclude_loopback_and_link_local(self):
        rows = [(2, 1, 6, '', ('192.168.1.7', 0)), (2, 1, 6, '', ('127.0.0.1', 0)),
                (2, 1, 6, '', ('169.254.2.3', 0)), (2, 1, 6, '', ('10.0.0.5', 0))]
        with patch.object(network_access.socket, 'getaddrinfo', return_value=rows), \
             patch.object(network_access, '_address_via_udp', return_value=[]):
            self.assertEqual(network_access.lan_addresses(8090),
                             ['http://192.168.1.7:8090', 'http://10.0.0.5:8090'])

    def test_addresses_deduplicate_udp_fallback(self):
        rows = [(2, 1, 6, '', ('192.168.1.7', 0))]
        with patch.object(network_access.socket, 'getaddrinfo', return_value=rows), \
             patch.object(network_access, '_address_via_udp', return_value=['192.168.1.7']):
            self.assertEqual(network_access.lan_addresses(8090), ['http://192.168.1.7:8090'])

    def test_addresses_survive_resolution_failure(self):
        with patch.object(network_access.socket, 'getaddrinfo', side_effect=OSError('offline')), \
             patch.object(network_access, '_address_via_udp', return_value=['192.168.1.9']):
            self.assertEqual(network_access.lan_addresses(8090), ['http://192.168.1.9:8090'])


def fake_netsh(returncode=0, stdout=''):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr='')
    return run


class FirewallTests(unittest.TestCase):
    def setUp(self):
        _redirect_storage(self)

    def test_rule_name_includes_port(self):
        self.assertEqual(network_access.firewall_rule_name(8090), 'JavSP WEB LAN 8090')

    def test_exists_detects_rule_by_name(self):
        name = network_access.firewall_rule_name(8090)
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access.subprocess, 'run', side_effect=fake_netsh(stdout=f'规则名称: {name}')):
            self.assertTrue(network_access.firewall_rule_exists(8090))

    def test_exists_false_when_rule_missing(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access.subprocess, 'run', side_effect=fake_netsh(returncode=1)):
            self.assertFalse(network_access.firewall_rule_exists(8090))

    def test_add_rule_uses_netsh_arguments(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access.subprocess, 'run', side_effect=fake_netsh()) as run:
            network_access.firewall_add_rule(8090)
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ['netsh', 'advfirewall'])
        self.assertIn('dir=in', args)
        self.assertIn('action=allow', args)
        self.assertIn('localport=8090', args)

    def test_add_rule_raises_with_manual_guidance_without_admin(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access.subprocess, 'run', side_effect=fake_netsh(returncode=1, stdout='拒绝访问')):
            with self.assertRaises(network_access.FirewallError) as ctx:
                network_access.firewall_add_rule(8090)
        message = str(ctx.exception)
        self.assertIn('netsh advfirewall firewall add rule', message)
        self.assertIn('JavSP WEB LAN 8090', message)

    def test_non_windows_has_no_firewall_management(self):
        with patch.object(network_access, 'is_windows', return_value=False):
            self.assertFalse(network_access.firewall_rule_exists(8090))
            with self.assertRaises(network_access.FirewallError):
                network_access.firewall_add_rule(8090)
            status = network_access.firewall_status(8090)
        self.assertFalse(status['supported'])
        self.assertIn('ufw', status['message'])

    def test_apply_adds_rule_when_enabling(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access, 'firewall_rule_exists', return_value=False), \
             patch.object(network_access, 'firewall_add_rule') as add:
            status = network_access.apply_lan_access(True)
        add.assert_called_once_with(None)
        self.assertTrue(status['exists'])
        self.assertIn('已添加', status['message'])

    def test_apply_reports_failure_without_blocking(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access, 'firewall_rule_exists', return_value=False), \
             patch.object(network_access, 'firewall_add_rule',
                          side_effect=network_access.FirewallError('需要管理员权限')):
            status = network_access.apply_lan_access(True)
        self.assertFalse(status['exists'])
        self.assertEqual(status['error'], '需要管理员权限')
        self.assertIn('需要管理员权限', status['message'])

    def test_apply_removes_rule_when_disabling(self):
        with patch.object(network_access, 'is_windows', return_value=True), \
             patch.object(network_access, 'firewall_rule_exists', return_value=True), \
             patch.object(network_access, '_netsh', return_value=subprocess.CompletedProcess([], 0)):
            status = network_access.apply_lan_access(False)
        self.assertFalse(status['exists'])
        self.assertIn('已移除', status['message'])


class NetworkApiTests(unittest.TestCase):
    def setUp(self):
        _redirect_storage(self)
        with patch('threading.Thread.start'):
            from javsp_web import server
        self.server = server
        self.enterContext(patch.dict(server.app.dependency_overrides,
                                     {server.current_user: lambda: {'username': 'admin', 'role': 'admin'}}))
        from fastapi.testclient import TestClient
        self.client = self.enterContext(TestClient(server.app))
        self.addCleanup(self.client.close)
        storage.ensure_seed_data()

    def test_get_returns_settings_addresses_and_firewall(self):
        with patch.object(self.server.network_access, 'firewall_rule_exists', return_value=False), \
             patch.object(self.server.network_access, '_address_via_udp', return_value=['192.168.1.5']), \
             patch.object(self.server.network_access.socket, 'getaddrinfo',
                          return_value=[(2, 1, 6, '', ('192.168.1.5', 0))]):
            data = self.client.get('/api/network/settings').json()
        self.assertFalse(data['settings']['lan_access'])
        self.assertEqual(data['port'], 8090)
        self.assertEqual(data['addresses'], ['http://192.168.1.5:8090'])
        self.assertTrue(data['firewall']['supported'])

    def test_put_saves_setting_and_applies_firewall(self):
        with patch.object(self.server.network_access, 'is_windows', return_value=True), \
             patch.object(self.server.network_access, 'firewall_rule_exists', return_value=False), \
             patch.object(self.server.network_access, '_netsh', return_value=subprocess.CompletedProcess([], 0)):
            response = self.client.put('/api/network/settings', json={'lan_access': True})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body['settings']['lan_access'])
        self.assertTrue(body['firewall']['exists'])
        self.assertTrue(storage.get_network_settings()['lan_access'])

    def test_put_keeps_setting_when_firewall_fails(self):
        with patch.object(self.server.network_access, 'is_windows', return_value=True), \
             patch.object(self.server.network_access, 'firewall_rule_exists', return_value=False), \
             patch.object(self.server.network_access, 'firewall_add_rule',
                          side_effect=network_access.FirewallError('需要管理员权限')):
            response = self.client.put('/api/network/settings', json={'lan_access': True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['settings']['lan_access'])
        self.assertIn('需要管理员权限', response.json()['firewall']['error'])

    def test_put_requires_admin(self):
        self.server.app.dependency_overrides[self.server.current_user] = \
            lambda: {'username': 'operator', 'role': 'operator'}
        response = self.client.put('/api/network/settings', json={'lan_access': True})
        self.assertEqual(response.status_code, 403)


if __name__ == '__main__':
    unittest.main()
