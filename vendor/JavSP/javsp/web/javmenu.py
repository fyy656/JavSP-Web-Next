"""从JavMenu抓取数据

站点已改为 /<语言>/<番号> 形式（如 https://javmenu.com/zh/URE-123），
且详情不再放在固定的 div 结构里，而是集中在 meta 描述与封面图的 data-src 中。
找不到影片时站点会返回“猜你喜欢”页面，需要据此抛出 MovieNotFoundError。
"""
import logging
import re

from javsp.web.base import Request, resp2html
from javsp.web.exceptions import *
from javsp.datatype import MovieInfo


request = Request()

logger = logging.getLogger(__name__)
base_url = 'https://javmenu.com'
# 找不到资源时站点返回的页面标题
NOT_FOUND_TITLES = ('猜你喜欢', 'あなたは好きかもしれません', 'You may also like')


def _first(html, xpath, default=''):
    values = html.xpath(xpath)
    if not values:
        return default
    value = values[0]
    return (value.strip() if isinstance(value, str) else value) or default


def _split_items(text):
    return [item.strip() for item in re.split(r'[、,，]', text or '') if item.strip()]


def parse_meta(description: str) -> dict:
    """从站点的一句话描述里取出日期、女优、时长与主题"""
    data = {}
    match = re.search(r'发[佈布]\s*日期为\s*(\d{4}-\d{2}-\d{2})', description or '')
    if match:
        data['publish_date'] = match.group(1)
    match = re.search(r'主演女[优優]是(.+?)(?:，|。|$)', description or '')
    if match:
        data['actress'] = _split_items(match.group(1))
    match = re.search(r'影片时[长長]\s*(\d+)\s*分钟', description or '')
    if match:
        data['duration'] = match.group(1)
    match = re.search(r'主题为(.+?)(?:。|$)', description or '')
    if match:
        data['genre'] = _split_items(match.group(1))
    return data


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据

    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    url = f'{base_url}/zh/{movie.dvdid}'
    r = request.get(url, delay_raise=True)
    html = resp2html(r)
    page_title = _first(html, '//meta[@property="og:title"]/@content')
    if not page_title or any(marker in page_title for marker in NOT_FOUND_TITLES):
        raise MovieNotFoundError(__name__, movie.dvdid)

    description = _first(html, '//meta[@name="description"]/@content')
    title = page_title
    for suffix in ('  | JAV目錄大全 | 每日更新', ' 免費在線看', ' 免費AV在線看'):
        title = title.replace(suffix, '')
    prefix = f'{movie.dvdid} '
    if title.startswith(prefix):
        title = title[len(prefix):]
    # 标题里偶尔会重复番号，清掉后再校验
    title = title.replace(movie.dvdid, '').strip()
    if not title:
        raise WebsiteError('javmenu: 页面缺少影片标题，无法确认有效资料')

    covers = [value.strip() for value in html.xpath('//img/@data-src')
              if isinstance(value, str) and ('/covers/' in value or '/cover/' in value)]
    covers = [value for value in covers if value.startswith('http')]
    if not covers:
        covers = [value.strip() for value in html.xpath('//meta[@property="og:image"]/@content')
                  if isinstance(value, str) and value.startswith('http') and 'logo' not in value]

    movie.url = url
    movie.title = title
    movie.cover = covers[0] if covers else None
    meta = parse_meta(description)
    movie.publish_date = meta.get('publish_date')
    movie.actress = meta.get('actress') or None
    movie.duration = meta.get('duration')
    movie.genre = meta.get('genre') or []


if __name__ == "__main__":
    movie = MovieInfo('URE-123')
    try:
        parse_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
