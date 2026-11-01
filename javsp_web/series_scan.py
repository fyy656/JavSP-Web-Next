"""剧集目录识别：把「放影片的最底层文件夹」当一部剧，再按季目录与文件名拆成分集。

只做文件系统与 NFO 解析，不碰数据库；由 `library_store` 负责落库与索引。

识别顺序固定为 **NFO > 目录名 > 文件名**：

* 一部剧：含视频的目录；若它只有季子目录（`S01` / `第一季`），也算一部剧。
  不含视频的分类目录会被跳过，继续往下找。
* 季：`S01` / `Season 1` / `第一季` / `第十二季` → 1..12；`特别篇` / `SP` → 0。
* 集：`S01E02` / `EP02` / `第2集` / `第十二集` / `02` / `1x02` / `第二季第三集`。
* NFO：`tvshow.nfo` 提供剧名与简介；`<视频名>.nfo` 的 `<season>`/`<episode>` 覆盖文件名；
  `season.nfo` 的 `<seasonnumber>` 覆盖季目录名。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
_IGNORED_DIRS = {'.git', '.svn', '#recycle', '#整理完成', '#不要扫描', '@eadir',
                 'system volume information', '$recycle.bin', 'lost+found'}

_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_NUMBER = r"零〇一二三四五六七八九十百千两\d"

_SEASON_DIR_PATTERNS = (
    re.compile(r"(?i)^(?:s|season|series)[\s._-]*(\d{1,2})$"),
    re.compile(r"^第\s*([" + _CN_NUMBER + r"]{1,4})\s*季$"),
    re.compile(r"(?i)^(\d{1,2})(?:st|nd|rd|th)?[\s._-]*season$"),
)
_SPECIAL_DIR_PATTERN = re.compile(r"(?i)^(?:specials?|sp|ova|oad|特别篇|番外|花絮)$")

_EPISODE_PATTERNS = (
    # S01E02 / s1.e2 / S01 EP02 / S01-E02
    re.compile(r"(?i)(?:^|[^a-z0-9])s(\d{1,2})[\s._-]*(?:e|ep|episode)[\s._-]*(\d{1,4})"),
    # Season 1 Episode 2
    re.compile(r"(?i)season[\s._-]*(\d{1,2})[\s._-]*(?:episode|ep)[\s._-]*(\d{1,4})"),
    # 1x02
    re.compile(r"(?:^|[^0-9])(\d{1,2})x(\d{1,4})(?:[^0-9]|$)"),
    # 第二季第三集
    re.compile(r"第\s*([" + _CN_NUMBER + r"]{1,4})\s*季[\s._-]*第\s*([" + _CN_NUMBER + r"]{1,4})\s*[集话話期]"),
)
_EPISODE_ONLY_PATTERNS = (
    # EP02 / E02 / Episode 2
    re.compile(r"(?i)(?:^|[^a-z0-9])(?:e|ep|episode)[\s._-]*(\d{1,4})(?:[^0-9]|$)"),
    # 第2集 / 第二话 / 第03期
    re.compile(r"第\s*([" + _CN_NUMBER + r"]{1,4})\s*[集话話期]"),
    # 结尾的括号编号：《秘密教学》 (10)
    re.compile(r"[（(]\s*(\d{1,3})\s*[)）]\s*$"),
    # 中文括号编号：杨幂 xxx（二）旗袍变装…（可能出现在标题中间）
    re.compile(r"[（(]\s*([零〇一二三四五六七八九十]{1,3})\s*[)）]"),
    # 纯数字开头：01.mkv / 02 - 标题.mkv
    re.compile(r"^(\d{1,3})(?:[\s._-]|$)"),
)
_SEASON_TEXT_PATTERNS = (
    # Show.S02 / Show S2 —— 文件名只写了季
    re.compile(r"(?i)(?:^|[^a-z0-9])s(\d{1,2})(?:[^0-9]|$)"),
    re.compile(r"(?i)(?:^|[^a-z0-9])season[\s._-]*(\d{1,2})(?:[^0-9]|$)"),
    re.compile(r"第\s*([" + _CN_NUMBER + r"]{1,4})\s*季"),
)
_RELEASE_TAG = re.compile(
    r"(?i)\b(2160p|1080p|720p|480p|web-?dl|bluray|blu-ray|hdtv|x264|x265|h\.?264|h\.?265|"
    r"hevc|avc|aac|ac3|dts|ddp?\d?\.?\d?|complete|repack|proper|multi)\b")


def _cn_to_int(text: str) -> int:
    """中文数字转整数（一 / 十二 / 二十三 / 一百零五）；纯阿拉伯数字直接转。"""
    value = str(text or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    total = current = 0
    for char in value:
        if char in _CN_DIGITS:
            current = _CN_DIGITS[char]
        elif char in _CN_UNITS:
            total += (current or 1) * _CN_UNITS[char]
            current = 0
        else:
            return 0
    return total + current


def season_from_dir_name(name: str) -> int | None:
    """目录名里的季号：S01 / Season 1 / 第一季 → 1，特别篇 / SP → 0。

    返回 None 表示「这不是季目录」，调用方据此判断它是剧集根还是普通子目录。
    """
    text = str(name or "").strip()
    if not text:
        return None
    for pattern in _SEASON_DIR_PATTERNS:
        match = pattern.match(text)
        if match:
            value = _cn_to_int(match.group(1))
            return value if value > 0 else None
    return 0 if _SPECIAL_DIR_PATTERN.match(text) else None


def parse_episode_marker(text: str) -> tuple[int, int]:
    """从文件名解析 (季, 集)；解析不到返回 (0, 0)。"""
    raw = str(text or "")
    if not raw:
        return 0, 0
    for pattern in _EPISODE_PATTERNS:
        match = pattern.search(raw)
        if match:
            episode = _cn_to_int(match.group(2))
            if episode:
                return max(0, _cn_to_int(match.group(1))), episode
    for pattern in _EPISODE_ONLY_PATTERNS:
        match = pattern.search(raw)
        if match:
            episode = _cn_to_int(match.group(1))
            if episode:
                return 0, episode
    return 0, 0


def season_from_text(text: str) -> int:
    """文件名里只有季标记（S02 / 第二季）时取季号，取不到返回 0。"""
    raw = str(text or "")
    for pattern in _SEASON_TEXT_PATTERNS:
        match = pattern.search(raw)
        if match:
            value = _cn_to_int(match.group(1))
            if value > 0:
                return value
    return 0


def clean_series_title(name: str) -> str:
    """目录名 → 剧名：去掉季集标记、分辨率、发布组等噪声。"""
    text = str(name or "").replace(".", " ").replace("_", " ")
    text = re.sub(r"(?i)\bS\d{1,2}(E\d{1,3})?\b", " ", text)
    text = _RELEASE_TAG.sub(" ", text)
    text = re.sub(r"\[[^\]]*\]", " ", text)
    text = re.sub(r"-[A-Za-z0-9]+$", " ", text.strip())
    return re.sub(r"\s+", " ", text).strip(" -")


# ---------------------------------------------------------------- 目录遍历


def _subdirs(folder: Path) -> list[Path]:
    try:
        return sorted((item for item in folder.iterdir()
                       if item.is_dir() and not item.name.startswith(".")
                       and item.name.casefold() not in _IGNORED_DIRS),
                      key=lambda item: item.name.casefold())
    except OSError:
        return []


def _videos(folder: Path, extensions: set[str]) -> list[Path]:
    try:
        return sorted((item for item in folder.iterdir()
                       if item.is_file() and item.suffix.lower() in extensions),
                      key=lambda item: str(item).casefold())
    except OSError:
        return []


def _has_videos(folder: Path, extensions: set[str], depth: int = 2) -> bool:
    if _videos(folder, extensions):
        return True
    if depth <= 0:
        return False
    return any(_has_videos(child, extensions, depth - 1) for child in _subdirs(folder))


def series_dirs(root: Path, extensions: set[str], max_depth: int = 4) -> list[Path]:
    """找出库目录下的「剧集根目录」。

    往下逐层走：

    * 自己直接放视频、且子目录里没有视频 → 它就是一部剧，到此为止；
    * 自己放视频、但子目录里也有视频（例如 `AI短剧/` 里既有几个散装短剧，又有 400 多个剧集目录）
      → 自己那几个散文件单独算一部剧，同时继续往下拆子目录；
    * 只有季子目录（`S01` / `第一季`）→ 它就是一部剧，季交给 `episode_entries` 处理；
    * 什么都没有 → 当作分类目录继续往下找，这样 `国产剧/2024/某剧/S01` 也能落到 `某剧`。
    """
    found: list[Path] = []

    def walk(directory: Path, depth: int) -> None:
        if depth > max_depth:
            return
        videos = _videos(directory, extensions)
        children = _subdirs(directory)
        season_children = [child for child in children
                           if season_from_dir_name(child.name) is not None and _has_videos(child, extensions)]
        other_children = [child for child in children
                          if season_from_dir_name(child.name) is None and _has_videos(child, extensions)]
        if videos or season_children:
            found.append(directory)
        if season_children or (videos and not other_children):
            return
        for child in other_children:
            walk(child, depth + 1)

    walk(root, 0)
    return found


def _episode_entry(video: Path, season_hint: int) -> dict:
    season, episode = parse_episode_marker(video.stem)
    if not season:
        season = season_from_text(video.stem)
    return {"video": video, "season": season if season else season_hint,
            "episode": episode, "nfo": None, "meta": {}}


def _natural_key(value: str) -> tuple:
    """自然排序：(2) 排在 (10) 前面，而不是按字符串比较。"""
    parts = re.split(r"(\d+)", str(value).casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


_EXTRA_PATTERN = re.compile(
    r"(?i)(trailer|sample|preview|behind|featurette|interview|花絮|预告|特典|番外|片花|采访|制作)")

def _assign_episode_numbers(entries: list[dict]) -> list[dict]:
    """给没有集号的文件补序号。

    * 整季都没有集号：按文件名自然顺序编号（(2) 排在 (10) 前面）；
    * 季内已有集号：写明了集号的按原号，其余文件只有明确是花絮/预告/特典时才跳过，
      剩下的接着最大集号往后编号——宁可多一集，也不要把正片丢掉。
    """
    grouped: dict[int, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(int(entry.get("season") or 0), []).append(entry)
    result: list[dict] = []
    for season in sorted(grouped):
        items = grouped[season]
        numbered = sorted((item for item in items if item.get("episode")),
                          key=lambda item: (item["episode"], _natural_key(str(item["video"]))))
        result.extend(numbered)
        extras = [item for item in items
                  if not item.get("episode") and not _EXTRA_PATTERN.search(item["video"].stem)]
        # 没写集号的补到「最小的空闲集号」上，避免出现 E04、E05 却没有 E01 的空档
        used = {item["episode"] for item in numbered}
        candidate = 1
        for item in sorted(extras, key=lambda item: _natural_key(str(item["video"]))):
            while candidate in used:
                candidate += 1
            used.add(candidate)
            result.append({**item, "season": season, "episode": candidate})
            candidate += 1
    return sorted(result, key=lambda item: (item["season"], item["episode"]))


def episode_entries(series_dir: Path, extensions: set[str]) -> list[dict]:
    """列出剧集目录下的所有分集，并套用 NFO 与「无集号补序号」规则。

    只看这个目录自己的视频和它的季子目录；非季子目录由 `series_dirs` 当成另一部剧，
    这样 `AI短剧/` 那种「散文件 + 400 多个剧集目录」的结构不会被搅成一部几百集的剧。
    """
    entries = [_episode_entry(video, 1) for video in _videos(series_dir, extensions)]
    for child in _subdirs(series_dir):
        season_no = season_from_dir_name(child.name)
        if season_no is None:
            continue
        from_nfo = _season_number_from_nfo(child)
        if from_nfo is not None:
            season_no = from_nfo
        entries.extend(_episode_entry(video, season_no) for video in _videos(child, extensions))
    for entry in entries:
        nfo = entry["video"].with_suffix(".nfo")
        if not nfo.is_file():
            continue
        meta = parse_episode_nfo(nfo)
        if not meta:
            continue
        entry["nfo"], entry["meta"] = nfo, meta
        if meta.get("season") is not None:
            entry["season"] = int(meta["season"])
        if meta.get("episode") is not None:
            entry["episode"] = int(meta["episode"])
    return _assign_episode_numbers(entries)


def _season_number_from_nfo(folder: Path) -> int | None:
    for name in ("season.nfo", f"{folder.name}.nfo"):
        candidate = folder / name
        if not candidate.is_file():
            continue
        meta = parse_season_nfo(candidate)
        if meta.get("season") is not None:
            return int(meta["season"])
    return None


# ---------------------------------------------------------------- 图片


def find_poster(folder: Path, stem: str = "") -> str:
    """剧集/分集封面：poster / folder / cover / <名称>-poster。"""
    names = ["poster", f"{stem}-poster"] if stem else ["poster"]
    for name in [*names, "folder", "cover"]:
        for extension in IMAGE_EXTENSIONS:
            candidate = folder / f"{name}{extension}"
            if candidate.is_file():
                return str(candidate)
    return ""


def find_thumb(video: Path) -> str:
    """分集剧照：<视频名>-thumb.jpg 优先，其次同名图片。"""
    for suffix in ("-thumb", "-fanart"):
        for extension in IMAGE_EXTENSIONS:
            candidate = video.with_name(f"{video.stem}{suffix}{extension}")
            if candidate.is_file():
                return str(candidate)
    for extension in IMAGE_EXTENSIONS:
        candidate = video.with_suffix(extension)
        if candidate.is_file():
            return str(candidate)
    return ""


def find_fanart(folder: Path) -> str:
    for name in ("fanart", "backdrop"):
        for extension in IMAGE_EXTENSIONS:
            candidate = folder / f"{name}{extension}"
            if candidate.is_file():
                return str(candidate)
    return ""


# ---------------------------------------------------------------- NFO


def _parse_xml(path: Path, expected_tag: str) -> ET.Element | None:
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8-sig", errors="replace"))
    except (OSError, ET.ParseError):
        return None
    return root if str(root.tag).lower() == expected_tag else None


def _text(root: ET.Element, tag: str) -> str:
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _local_picture(value: str, folder: Path) -> str:
    """NFO 里的 <thumb> 常是相对文件名，换成磁盘上的真实图片；网络地址原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith(("http://", "https://")):
        return text
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = folder / text
    return str(candidate) if candidate.is_file() else ""


def parse_tvshow_nfo(path: Path) -> dict:
    """解析 `<tvshow>`：剧名、简介、年份、演员、海报。"""
    root = _parse_xml(path, "tvshow")
    if root is None:
        return {}
    data = {
        "title": _text(root, "title"),
        "original_title": _text(root, "originaltitle"),
        "year": _text(root, "year"),
        "premiered": _text(root, "premiered"),
        "plot": _text(root, "plot"),
        "studio": _text(root, "studio"),
        "mpaa": _text(root, "mpaa"),
    }
    data["genres"] = [(node.text or "").strip() for node in root.findall("genre") if (node.text or "").strip()]
    data["actors"] = [{"name": (node.findtext("name") or "").strip(),
                       "role": (node.findtext("role") or "").strip()}
                      for node in root.findall("actor") if (node.findtext("name") or "").strip()]
    thumb = ""
    for node in root.findall("thumb"):
        if (node.text or "").strip():
            thumb = _local_picture(node.text.strip(), path.parent)
            break
    data["poster_path"] = thumb
    data["nfo_path"] = str(path)
    return {key: value for key, value in data.items() if value not in ("", [], None)}


def parse_episode_nfo(path: Path) -> dict:
    """解析 `<episodedetails>`：季集号、标题、简介、时长、剧照。"""
    root = _parse_xml(path, "episodedetails")
    if root is None:
        return {}
    data = {
        "title": _text(root, "title"),
        "plot": _text(root, "plot"),
        "premiered": _text(root, "premiered") or _text(root, "aired"),
        "studio": _text(root, "studio"),
    }
    runtime = _text(root, "runtime")
    if runtime.isdigit():
        data["runtime"] = int(runtime)
    for key in ("season", "episode"):
        value = _text(root, key)
        if value.isdigit():
            data[key] = int(value)
    directors = [(node.text or "").strip() for node in root.findall("director")]
    data["directors"] = [name for name in directors if name]
    actors = [{"name": (node.findtext("name") or "").strip(),
               "role": (node.findtext("role") or "").strip()}
              for node in root.findall("actor") if (node.findtext("name") or "").strip()]
    if actors:
        data["actors"] = actors
    thumb = ""
    for node in root.findall("thumb"):
        if (node.text or "").strip():
            thumb = _local_picture(node.text.strip(), path.parent)
            break
    data["thumb_path"] = thumb
    data["nfo_path"] = str(path)
    return {key: value for key, value in data.items() if value not in ("", [], None)}


def parse_season_nfo(path: Path) -> dict:
    """解析季目录的 `<season>`（tMM 会写 season.nfo）。"""
    root = _parse_xml(path, "season")
    if root is None:
        return {}
    data = {"title": _text(root, "title"), "plot": _text(root, "plot")}
    value = _text(root, "seasonnumber")
    if value.isdigit():
        data["season"] = int(value)
    return {key: item for key, item in data.items() if item not in ("", None)}
