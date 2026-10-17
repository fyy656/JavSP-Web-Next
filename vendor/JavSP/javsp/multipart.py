"""Shared, configuration-aware multipart recognition for Web and CLI scans."""
import os
import re
from pathlib import Path

from javsp.avid import configured_media_type, get_cid, get_id


_PART_NUMBER_RE = re.compile(
    r'(?:[-_ .]+(?:cd|part|pt|p)?\s*|(?:cd|part|pt|p)\s*)'
    r'(\d{1,3})(?:(?:[-_ .]+)(?:\d{3,4}p|\d{1,2}k))?$',
    re.I,
)

# 兼容旧命名时，分片标记必须从 0、1 或 a 开始（同时要求连续）
_MARKER_START = ('0', '1', 'a')


def media_identity(path, scanner=None):
    configured = configured_media_type(str(path), scanner)
    if configured:
        return configured[1], configured[2].casefold()
    cid = get_cid(str(path), scanner)
    return ('cid', cid.casefold()) if cid else ('dvdid', get_id(str(path), scanner).casefold())


def part_number(path, scanner=None):
    path = Path(path)
    match = _PART_NUMBER_RE.search(path.stem)
    if not match:
        return None
    base = path.with_name(path.stem[:match.start()] + path.suffix)
    identity = media_identity(path, scanner)
    # ABC-123 and date-based identifiers must not lose their actual number.
    if identity[1] and media_identity(base.name, scanner) == identity:
        return int(match.group(1))
    return None


def legacy_part_order(paths):
    """Order files that differ only by one trailing part marker (letter or digit).

    Handles the legacy/Emby naming style (``FCDSS-027-A`` / ``FCDSS-027-B``, or
    ``moviename#.ext`` where # is A-D): take the common file-name prefix, read a single
    ``[a-z\\d]`` marker from each name, and require the remaining text to be identical,
    the markers to be unique, and the sequence to start at 0/1/a and be continuous.

    Returns the paths in part order, or ``None`` when the names do not describe one
    unambiguous sequence; the caller then reports a duplicate instead of guessing.
    """
    names = [Path(path).name for path in paths]
    if not all(names):
        return None
    prefix = os.path.commonprefix(names)
    try:
        pattern = re.compile(re.escape(prefix) + r'\s*([a-z\d])\s*', flags=re.I)
    except re.error:
        return None
    remaining = [pattern.sub(r'\1', name).lower() for name in names]
    markers = [value[0] for value in remaining]
    tails = [value[1:] for value in remaining]
    # 有文件不符合前缀规则，或分片标记重复时，无法确定分片顺序
    if len(set(tails)) != 1 or len(markers) != len(set(markers)):
        return None
    ordered = sorted(markers)
    first, last = ordered[0], ordered[-1]
    if first not in _MARKER_START or ord(last) != ord(first) + len(ordered) - 1:
        return None
    return [paths[markers.index(marker)] for marker in ordered]


def group_files(files, scanner=None):
    """Group only one directory's copies of an ID; reject ambiguous duplicates."""
    groups = {}
    for path in files:
        path = Path(path)
        kind, avid = media_identity(path, scanner)
        key = (str(path.parent), kind, avid) if avid else (str(path),)
        groups.setdefault(key, []).append(path)
    result = []
    for paths in groups.values():
        if len(paths) == 1:
            result.append(paths)
            continue
        numbers = [part_number(path, scanner) for path in paths]
        if None not in numbers and len(set(numbers)) == len(numbers):
            result.append([path for _, path in sorted(zip(numbers, paths))])
            continue
        # 数字编号不明确时退回单字符分片规则（与 CLI 扫描器保持一致）
        ordered = legacy_part_order(paths)
        if ordered is not None:
            result.append(ordered)
            continue
        names = '、'.join(path.name for path in paths)
        raise ValueError(f'同番号文件的分 P 编号不明确或重复，请检查后重试: {names}')
    return result
