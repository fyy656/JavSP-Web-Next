"""从JavBus抓取数据"""
import logging


from javsp.web.base import *
from javsp.web.exceptions import *
from javsp.func import *
from javsp.config import Cfg, CrawlerID
from javsp.datatype import MovieInfo, GenreMap
from javsp.web.mirrors import get_with_mirror_fallback


logger = logging.getLogger(__name__)
genre_map = GenreMap('data/genre_javbus.csv')
permanent_url = 'https://www.javbus.com'
if Cfg().network.proxy_server is not None:
    base_url = permanent_url
else:
    base_url = str(Cfg().network.proxy_free[CrawlerID.javbus])

# JavBus 会按请求指纹决定是否返回“年龄验证”页：过旧的 UA、缺少 Accept-Language 时
# 只发回验证页（表现为解析器报“文档为空”），而浏览器式请求头可以直接拿到影片页。
JAVBUS_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Upgrade-Insecure-Requests': '1',
}
# 万一仍然命中验证页，就按浏览器的方式提交一次年龄确认表单
AGE_GATE_TITLE = 'Age Verification'
AGE_GATE_FORM = {'Submit': '確認'}


def new_request() -> Request:
    """带浏览器请求头的会话；保留代理与 CookieCloud cookie 处理"""
    request = Request()
    request.headers.update(JAVBUS_HEADERS)
    return request


def is_age_gate(resp) -> bool:
    """判断响应是否为 JavBus 的年龄验证页"""
    return AGE_GATE_TITLE in (getattr(resp, 'text', '') or '')


def confirm_age_verification(resp, request: Request | None = None):
    """命中年龄验证页时提交确认表单；其它情况原样返回"""
    if not is_age_gate(resp):
        return resp
    url = getattr(resp, 'url', '') or ''
    if not url:
        return resp
    logger.info('JavBus 返回年龄验证页，正在提交年龄确认：%s', url)
    confirmed = (request or new_request()).post(url, dict(AGE_GATE_FORM), delay_raise=True)
    if is_age_gate(confirmed):
        logger.warning('JavBus 年龄确认后仍是验证页，可能被站点风控拦截：%s', url)
        return resp
    return confirmed


def parse_data(movie: MovieInfo):
    """从网页抓取并解析指定番号的数据
    Args:
        movie (MovieInfo): 要解析的影片信息，解析后的信息直接更新到此变量内
    """
    url = f'{base_url.rstrip("/")}/{movie.dvdid}'
    request = new_request()
    resp = get_with_mirror_fallback(request.get, url, base_url, permanent_url, delay_raise=True)
    resp = confirm_age_verification(resp, request)
    if resp.status_code in (401, 403, 429) or resp.status_code >= 500:
        resp.raise_for_status()
    url = resp.url
    # 疑似JavBus检测到类似爬虫的行为时会要求登录，不过发现目前不需要登录也可以从重定向前的网页中提取信息
    if resp.history and resp.history[0].status_code == 302:
        html = resp2html(resp.history[0])
    else:
        html = resp2html(resp)
    # 引入登录验证后状态码不再准确，因此还要额外通过检测标题来确认是否发生了404
    page_title = html.xpath('/html/head/title/text()')
    if page_title and page_title[0].startswith('404 Page Not Found!'):
        raise MovieNotFoundError(__name__, movie.dvdid)

    container = html.xpath("//div[@class='container']")[0]
    title = container.xpath("h3/text()")[0]
    cover = container.xpath("//a[@class='bigImage']/img/@src")[0]
    preview_pics = container.xpath("//div[@id='sample-waterfall']/a/@href")
    info = container.xpath("//div[@class='col-md-3 info']")[0]
    dvdid = info.xpath("p/span[text()='識別碼:']")[0].getnext().text
    publish_date = info.xpath("p/span[text()='發行日期:']")[0].tail.strip()
    duration = info.xpath("p/span[text()='長度:']")[0].tail.replace('分鐘', '').strip()
    director_tag = info.xpath("p/span[text()='導演:']")
    if director_tag:    # xpath没有匹配时将得到空列表
        movie.director = director_tag[0].getnext().text.strip()
    producer_tag = info.xpath("p/span[text()='製作商:']")
    if producer_tag:
        text = producer_tag[0].getnext().text
        if text:
            movie.producer = text.strip()
    publisher_tag = info.xpath("p/span[text()='發行商:']")
    if publisher_tag:
        movie.publisher = publisher_tag[0].getnext().text.strip()
    serial_tag = info.xpath("p/span[text()='系列:']")
    if serial_tag:
        movie.serial = serial_tag[0].getnext().text
    # genre, genre_id
    genre_tags = info.xpath("//span[@class='genre']/label/a")
    genre, genre_id = [], []
    for tag in genre_tags:
        tag_url = tag.get('href')
        pre_id = tag_url.split('/')[-1]
        genre.append(tag.text)
        if 'uncensored' in tag_url:
            movie.uncensored = True
            genre_id.append('uncensored-' + pre_id)
        else:
            movie.uncensored = False
            genre_id.append(pre_id)
    # JavBus的磁力链接是依赖js脚本加载的，无法通过静态网页来解析
    # actress, actress_pics
    actress, actress_pics = [], {}
    actress_tags = html.xpath("//a[@class='avatar-box']/div/img")
    for tag in actress_tags:
        name = tag.get('title')
        pic_url = tag.get('src')
        actress.append(name)
        if not pic_url.endswith('nowprinting.gif'):     # 略过默认的头像
            actress_pics[name] = pic_url
    # 整理数据并更新movie的相应属性
    movie.url = f'{permanent_url}/{movie.dvdid}'
    movie.dvdid = dvdid
    movie.title = title.replace(dvdid, '').strip()
    movie.cover = cover
    movie.preview_pics = preview_pics
    if publish_date != '0000-00-00':    # 丢弃无效的发布日期
        movie.publish_date = publish_date
    movie.duration = duration if int(duration) else None
    movie.genre = genre
    movie.genre_id = genre_id
    movie.actress = actress
    movie.actress_pics = actress_pics


def parse_clean_data(movie: MovieInfo):
    """解析指定番号的影片数据并进行清洗"""
    parse_data(movie)
    movie.genre_norm = genre_map.map(movie.genre_id)
    movie.genre_id = None   # 没有别的地方需要再用到，清空genre id（暗示已经完成转换）


if __name__ == "__main__":
    import pretty_errors
    pretty_errors.configure(display_link=True)
    logger.root.handlers[1].level = logging.DEBUG

    movie = MovieInfo('NANP-030')
    try:
        parse_clean_data(movie)
        print(movie)
    except CrawlerError as e:
        logger.error(e, exc_info=1)
