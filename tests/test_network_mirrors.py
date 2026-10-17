"""Offline regression for the engine's proxy / proxy_free routing.

The preset help text tells users that JavBus and JavDB switch to the official host as
soon as a proxy is configured, and that `proxy_free` only applies without one. That is
the engine's behaviour in `javsp/web/javbus.py`; pin it so the guidance cannot silently
rot, and so "mirror is the official host" misconfigurations stay explainable.
"""
import importlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

from javsp.config import CrawlerID


def reload_javbus(proxy_server, mirror):
    """Import javsp.web.javbus with one specific network configuration."""
    import javsp.web.javbus as javbus
    config = SimpleNamespace(network=SimpleNamespace(proxy_server=proxy_server,
                                                     proxy_free={CrawlerID.javbus: mirror}))
    with patch('javsp.config.Cfg', lambda: config):
        return importlib.reload(javbus)


class ProxyFreeRoutingTests(unittest.TestCase):
    def setUp(self):
        import javsp.web.javbus as javbus
        # 用真实配置重新加载，避免把测试用配置留给其它测试
        self.addCleanup(importlib.reload, javbus)

    def test_configured_proxy_switches_javbus_to_the_official_host(self):
        module = reload_javbus('http://127.0.0.1:7890', 'https://mirror.test')
        self.assertEqual(module.base_url, 'https://www.javbus.com')

    def test_without_a_proxy_javbus_uses_the_configured_mirror(self):
        module = reload_javbus(None, 'https://mirror.test')
        self.assertEqual(module.base_url, 'https://mirror.test')

    def test_shipped_default_is_a_reachable_mirror_not_the_official_host(self):
        import yaml

        from javsp_web.storage import VENDOR_DIR
        config = yaml.safe_load((VENDOR_DIR / 'config.yml').read_text(encoding='utf-8'))
        self.assertNotIn('javbus.com', str(config['network']['proxy_free']['javbus']))
        self.assertTrue(str(config['network']['proxy_free']['javbus']).startswith('https://'))


class JavbusAgeGateTests(unittest.TestCase):
    """JavBus 按请求指纹返回年龄验证页：旧 UA 只拿到验证页，浏览器头才能拿到影片页。"""

    def test_crawler_uses_browser_headers(self):
        import javsp.web.base as base
        import javsp.web.javbus as javbus

        headers = javbus.new_request().headers
        self.assertIn('Chrome/124', headers['User-Agent'])
        self.assertIn('Accept-Language', headers)
        self.assertIn('Accept', headers)
        self.assertNotEqual(headers['User-Agent'], base.headers['User-Agent'])

    def test_age_gate_page_is_confirmed_with_the_form(self):
        import javsp.web.javbus as javbus

        gate = SimpleNamespace(text='<title>Age Verification JavBus - JavBus</title>', url='https://www.javbus.com/SONE-994')
        movie = SimpleNamespace(text='<title>SONE-994</title><a class="bigImage">', url='https://www.javbus.com/SONE-994')
        calls = []

        class FakeRequest:
            def post(self, url, data, delay_raise=False):
                calls.append((url, data, delay_raise))
                return movie

        self.assertTrue(javbus.is_age_gate(gate))
        self.assertIs(javbus.confirm_age_verification(gate, FakeRequest()), movie)
        self.assertEqual(calls, [('https://www.javbus.com/SONE-994', {'Submit': '確認'}, True)])

    def test_movie_page_is_not_confirmed_again(self):
        import javsp.web.javbus as javbus

        movie = SimpleNamespace(text='<title>SONE-994</title><a class="bigImage">', url='https://www.javbus.com/SONE-994')

        class FakeRequest:
            def post(self, *args, **kwargs):
                raise AssertionError('普通影片页不应再提交年龄确认')

        self.assertFalse(javbus.is_age_gate(movie))
        self.assertIs(javbus.confirm_age_verification(movie, FakeRequest()), movie)

    def test_still_gated_response_keeps_the_original(self):
        import javsp.web.javbus as javbus

        gate = SimpleNamespace(text='<title>Age Verification JavBus - JavBus</title>', url='https://www.javbus.com/SONE-994')

        class FakeRequest:
            def post(self, url, data, delay_raise=False):
                return gate

        self.assertIs(javbus.confirm_age_verification(gate, FakeRequest()), gate)


if __name__ == '__main__':
    unittest.main()
