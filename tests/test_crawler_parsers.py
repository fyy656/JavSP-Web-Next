"""Offline regressions for crawler parsers whose sites changed layout.

JavMenu moved its detail pages to ``/<lang>/<dvdid>`` and now exposes the fields through
the page's meta description plus the cover image's ``data-src``; the old div-based parser
raised "页面缺少影片信息区域" for every title. These tests pin the new extraction without
touching the network.
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

from javsp.web import javmenu
from javsp.web.exceptions import MovieNotFoundError, WebsiteError
from javsp.datatype import MovieInfo

DESCRIPTION = (
    'URE-123 原作:サゲジョー 窓の椿 保護者だったはずの私はいま隣の子とスケベに身体を重ね合っている 小梅えな，'
    '发佈日期为2025-09-23，主演女优是小梅えな、日森一，影片时长120分钟，由拍摄的作品，由製作，'
    '主题为巨乳、已婚妇女、荡妇、处男、原作改编、单体作品。'
)


def page(title='URE-123 原作:サゲジョー 窓の椿 小梅えな', description=DESCRIPTION,
         cover='https://c0.jdbstatic.com/covers/bk/bKGpqB.jpg') -> str:
    meta = [f'<meta property="og:title" content="{title}">' if title else '',
            f'<meta name="description" content="{description}">' if description else '']
    image = f'<img data-src="https://javmenu.com/images/loading.gif" data-real="{cover}"><img data-src="{cover}">' if cover else ''
    return f'<html><head>{"".join(meta)}</head><body>{image}</body></html>'


def response_for(html: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = html.encode('utf-8')
    response.encoding = 'utf-8'
    response.url = 'https://javmenu.com/zh/URE-123'
    return response


class JavmenuParserTests(unittest.TestCase):
    def parse(self, html):
        movie = MovieInfo('URE-123')
        with patch.object(javmenu.request, 'get', return_value=response_for(html)):
            javmenu.parse_data(movie)
        return movie

    def test_meta_description_is_parsed_into_movie_fields(self):
        movie = self.parse(page())
        self.assertEqual(movie.title, '原作:サゲジョー 窓の椿 小梅えな')
        self.assertEqual(movie.cover, 'https://c0.jdbstatic.com/covers/bk/bKGpqB.jpg')
        self.assertEqual(movie.publish_date, '2025-09-23')
        self.assertEqual(movie.actress, ['小梅えな', '日森一'])
        self.assertEqual(movie.duration, '120')
        self.assertEqual(movie.genre[0], '巨乳')
        self.assertIn('javmenu.com/zh/URE-123', movie.url)

    def test_fallback_page_is_reported_as_not_found(self):
        with self.assertRaises(MovieNotFoundError):
            self.parse(page(title='猜你喜欢'))

    def test_page_without_detail_meta_is_reported_as_not_found(self):
        with self.assertRaises(MovieNotFoundError):
            self.parse(page(title=''))

    def test_title_only_containing_the_identifier_is_a_site_error(self):
        with self.assertRaises(WebsiteError):
            self.parse(page(title='URE-123'))

    def test_simplified_and_traditional_dates_are_supported(self):
        description = DESCRIPTION.replace('发佈日期', '发布日期')
        movie = self.parse(page(description=description))
        self.assertEqual(movie.publish_date, '2025-09-23')


if __name__ == '__main__':
    unittest.main()
