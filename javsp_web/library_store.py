"""Filesystem-first media library index.

The library treats the files on disk (video, NFO, artwork) as the source of
truth.  Task records only describe how a film was processed; deleting them must
never remove a film from the library.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET

from . import series_scan
from .storage import DATA_DIR, VENDOR_DIR, read_config
from .timeutils import now_iso

LIBRARY_DB_FILE = DATA_DIR / "library.db"
# 重新刮削前的 NFO 快照：引擎写 NFO 是整份重建，用它把旧资料补回去
NFO_SNAPSHOT_DIR = DATA_DIR / "library-nfo-snapshots"

VIDEO_EXTENSIONS = {'.3gp', '.avi', '.f4v', '.flv', '.iso', '.m2ts', '.m4v', '.mkv', '.mov',
                    '.mp4', '.mpeg', '.mpg', '.rm', '.rmvb', '.ts', '.vob', '.webm', '.wmv', '.strm'}
IMAGE_EXTENSIONS = ('.jpg', '.jpeg', '.png', '.webp')
IGNORED_DIRS = {'.git', '.svn', '#recycle', '#整理完成', '#不要扫描', '@eadir', 'system volume information',
                '$recycle.bin', 'lost+found'}
PART_PATTERN = re.compile(r'(?i)[\s._-]*(?:cd|part|p|disc|disk)[\s._-]*\d{1,2}$')
HARD_FIELDS = ('dvdid', 'title', 'cover')

_lock = threading.RLock()
_ready: set[str] = set()
_scanner_cache: dict[str, object] = {}
_scan_state: dict[str, dict] = {}
_scan_lock = threading.Lock()


@contextmanager
def connection():
    LIBRARY_DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(LIBRARY_DB_FILE, timeout=30)
    db.row_factory = sqlite3.Row
    # 与 task_store 保持一致：SQL 里用 casefold() 做不区分大小写的匹配
    db.create_function('casefold', 1, lambda value: str(value or '').casefold(), deterministic=True)
    db.execute('PRAGMA foreign_keys=ON')
    key = str(LIBRARY_DB_FILE.resolve())
    try:
        with _lock:
            if key not in _ready:
                _ensure_schema(db)
                _ready.add(key)
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def ensure_ready() -> None:
    """启动时先把 schema 迁移跑完。

    老库第一次打开要补列（kind / parent_id / season / episode），如果拖到用户点「扫描」
    时才做，扫描线程会和迁移抢同一把写锁，偶发 "attempt to write a readonly database"。
    """
    with connection():
        pass


def _ensure_schema(db) -> None:
    key = str(LIBRARY_DB_FILE.resolve())
    if key in _ready:
        return
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript("""
    CREATE TABLE IF NOT EXISTS libraries (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, preset_id TEXT NOT NULL DEFAULT 'default',
        kind TEXT NOT NULL DEFAULT 'movie',
        created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS roots (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL,
        preset_id TEXT NOT NULL DEFAULT 'default', recursive INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, last_scan_at TEXT, scan_status TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS library_roots (
        library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,
        PRIMARY KEY (library_id, root_id)
    );
    CREATE TABLE IF NOT EXISTS films (
        id TEXT PRIMARY KEY,
        root_id TEXT NOT NULL REFERENCES roots(id) ON DELETE CASCADE,
        kind TEXT NOT NULL DEFAULT 'movie',
        parent_id TEXT NOT NULL DEFAULT '',
        season INTEGER NOT NULL DEFAULT 0,
        episode INTEGER NOT NULL DEFAULT 0,
        dir_path TEXT NOT NULL, video_path TEXT NOT NULL, files TEXT NOT NULL DEFAULT '[]',
        size_bytes INTEGER NOT NULL DEFAULT 0, mtime REAL NOT NULL DEFAULT 0,
        dvdid TEXT NOT NULL DEFAULT '', dvdid_source TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '', original_title TEXT NOT NULL DEFAULT '',
        plot TEXT NOT NULL DEFAULT '', release_date TEXT NOT NULL DEFAULT '',
        studio TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '',
        director TEXT NOT NULL DEFAULT '', runtime INTEGER NOT NULL DEFAULT 0,
        actress_text TEXT NOT NULL DEFAULT '',
        nfo_path TEXT NOT NULL DEFAULT '', nfo_mtime REAL NOT NULL DEFAULT 0,
        cover_path TEXT NOT NULL DEFAULT '', fanart_count INTEGER NOT NULL DEFAULT 0,
        extrafanart_dir TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'unscraped', missing_fields TEXT NOT NULL DEFAULT '[]',
        scrape_state TEXT NOT NULL DEFAULT 'idle', last_task_id TEXT NOT NULL DEFAULT '',
        last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS film_actress (
        film_id TEXT NOT NULL REFERENCES films(id) ON DELETE CASCADE,
        name TEXT NOT NULL, ord INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (film_id, name)
    );
    CREATE TABLE IF NOT EXISTS film_progress (
        film_id TEXT PRIMARY KEY REFERENCES films(id) ON DELETE CASCADE,
        position REAL NOT NULL DEFAULT 0,
        duration REAL NOT NULL DEFAULT 0,
        finished INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY, root_id TEXT NOT NULL, status TEXT NOT NULL,
        discovered INTEGER NOT NULL DEFAULT 0, added INTEGER NOT NULL DEFAULT 0,
        updated INTEGER NOT NULL DEFAULT 0, removed INTEGER NOT NULL DEFAULT 0,
        error TEXT NOT NULL DEFAULT '', started_at TEXT NOT NULL, finished_at TEXT
    );
    CREATE INDEX IF NOT EXISTS films_root ON films(root_id, updated_at DESC, id);
    CREATE INDEX IF NOT EXISTS films_status ON films(root_id, status);
    CREATE INDEX IF NOT EXISTS films_dvdid ON films(dvdid);
    CREATE INDEX IF NOT EXISTS films_release ON films(release_date);
    CREATE INDEX IF NOT EXISTS actress_name ON film_actress(name, film_id);
    CREATE INDEX IF NOT EXISTS library_roots_root ON library_roots(root_id);
    """)
    _ensure_scan_library_column(db)
    _ensure_scan_skipped_column(db)
    _ensure_film_detached_column(db)
    _ensure_library_kind_column(db)
    _ensure_film_series_columns(db)
    _migrate_legacy_roots(db)
    _ready.add(key)


def _table_columns(db, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})').fetchall()}


def _ensure_scan_library_column(db) -> None:
    """扫描记录归属到媒体库（旧库按文件夹扫描，这里追加列并保留旧行）"""
    if 'library_id' in _table_columns(db, 'scans'):
        return
    db.execute("ALTER TABLE scans ADD COLUMN library_id TEXT NOT NULL DEFAULT ''")


def _ensure_scan_skipped_column(db) -> None:
    if 'skipped' in _table_columns(db, 'scans'):
        return
    db.execute('ALTER TABLE scans ADD COLUMN skipped INTEGER NOT NULL DEFAULT 0')


def _ensure_film_detached_column(db) -> None:
    """整理把影片移出库目录后，索引会跟着搬家；detached 标记这类“已整理”的影片，
    避免后续扫描把它当作丢失。"""
    if 'detached' in _table_columns(db, 'films'):
        return
    db.execute('ALTER TABLE films ADD COLUMN detached INTEGER NOT NULL DEFAULT 0')


def _ensure_library_kind_column(db) -> None:
    """媒体库类型：movie（一个目录一部片）/ series（底层目录一部剧，按季集拆分）。"""
    if 'kind' not in _table_columns(db, 'libraries'):
        db.execute("ALTER TABLE libraries ADD COLUMN kind TEXT NOT NULL DEFAULT 'movie'")
    # 早期版本写过 'adult' 之类的值，统一归一到 movie（读取时也会归一，这里顺手把库里的值理顺）
    db.execute("UPDATE libraries SET kind='movie' WHERE kind NOT IN ('movie','series')")
    db.commit()


def _ensure_film_series_columns(db) -> None:
    """剧集库需要的列：条目类型、所属剧集、季、集。老库补列，默认值等于“普通影片”。

    注意：`films_parent` 索引必须等列补齐之后再建，否则老库（还没有 parent_id 列）
    会在建索引这一步直接报 "no such column"，整个媒体库都打不开。
    """
    columns = _table_columns(db, 'films')
    for name, ddl in (('kind', "TEXT NOT NULL DEFAULT 'movie'"),
                      ('parent_id', "TEXT NOT NULL DEFAULT ''"),
                      ('season', 'INTEGER NOT NULL DEFAULT 0'),
                      ('episode', 'INTEGER NOT NULL DEFAULT 0')):
        if name in columns:
            continue
        db.execute(f'ALTER TABLE films ADD COLUMN {name} {ddl}')
    db.execute('CREATE INDEX IF NOT EXISTS films_parent ON films(parent_id, season, episode)')
    db.commit()


def _migrate_legacy_roots(db) -> None:
    """旧版本里一个文件夹就是一个“媒体库”，迁移成“库 + 单个文件夹”。

    用文件夹 id 作为库 id，已有影片、任务和外链都不需要改。
    """
    rows = db.execute('SELECT * FROM roots WHERE id NOT IN (SELECT root_id FROM library_roots)').fetchall()
    for row in rows:
        existing = db.execute('SELECT id FROM libraries WHERE id=?', (row['id'],)).fetchone()
        if existing is None:
            db.execute('INSERT INTO libraries(id,name,preset_id,created_at,last_scan_at,scan_status) '
                       'VALUES(?,?,?,?,?,?)',
                       (row['id'], row['name'], row['preset_id'], row['created_at'],
                        row['last_scan_at'], row['scan_status']))
        db.execute('INSERT OR IGNORE INTO library_roots(library_id,root_id,added_at) VALUES(?,?,?)',
                   (row['id'], row['id'], now_iso()))


def _reset_ready() -> None:
    """Tests swap the database path; production never calls this."""
    _ready.clear()
    _scanner_cache.clear()


def _normalize_path(value: str) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(value or '').strip())))


def _video_extensions() -> set[str]:
    try:
        scanner = (read_config() or {}).get('scanner') or {}
        values = scanner.get('filename_extensions') or []
        configured = {str(item).lower() for item in values if str(item).startswith('.')}
        return configured or VIDEO_EXTENSIONS
    except Exception:
        return VIDEO_EXTENSIONS


def _ignored_dir(name: str) -> bool:
    return name.casefold() in IGNORED_DIRS or name.startswith('.')


def _part_key(stem: str) -> str:
    return PART_PATTERN.sub('', stem).strip() or stem


def _scanner_config():
    """刮削引擎的分片规则；校验后的 scanner 模型会被缓存（整库扫描逐目录复用）"""
    with _lock:
        if 'scanner' in _scanner_cache:
            return _scanner_cache['scanner']
    value = None
    try:
        import yaml
        from javsp.config import Cfg
        config = yaml.safe_load(read_config()) or {}
        value = Cfg.model_validate(config).scanner
    except Exception:  # noqa: BLE001 - 配置不可用或不合法时退回简单的后缀规则
        value = None
    with _lock:
        _scanner_cache['scanner'] = value
    return value


# 剧集不套用电影的「最小匹配文件大小」：一集几十 MB 很常见，电影级阈值会把整部剧过滤光
SERIES_MIN_SIZE = 1 << 20  # 1 MiB，只挡掉明显是垃圾的文件

_preset_scanner_cache: dict[str, tuple[dict, int]] = {}


def invalidate_preset_cache() -> None:
    """预设改了最小文件大小/分片规则后，下一次扫描必须重新读取。"""
    with _lock:
        _preset_scanner_cache.clear()


def preset_scan_limits(preset_id: str) -> tuple[dict, int]:
    """媒体库所属预设的扫描器配置与最小文件大小（字节），与新建刮削任务口径一致。"""
    key = str(preset_id or '')
    with _lock:
        if key in _preset_scanner_cache:
            return _preset_scanner_cache[key]
    scanner = None
    try:
        from . import storage
        preset = storage.get_preset(key or 'default') or {}
        form = preset.get('form') if isinstance(preset.get('form'), dict) else {}
        candidate = form.get('scanner') if isinstance(form, dict) else None
        if isinstance(candidate, dict):
            scanner = dict(candidate)
        elif preset.get('content'):
            import yaml
            loaded = yaml.safe_load(str(preset['content'])) or {}
            scanner = dict(loaded.get('scanner') or {}) if isinstance(loaded, dict) else {}
    except Exception:  # noqa: BLE001 - 预设缺失或损坏时退回基础配置
        scanner = None
    if scanner is None:
        try:
            import yaml
            base = yaml.safe_load(read_config()) or {}
            scanner = dict(base.get('scanner') or {}) if isinstance(base, dict) else {}
        except Exception:  # noqa: BLE001
            scanner = {}
    try:
        from .tasks import _minimum_size_bytes
        minimum = _minimum_size_bytes({'scanner': scanner})
    except Exception:  # noqa: BLE001
        minimum = 0
    value = (scanner, max(0, minimum))
    with _lock:
        _preset_scanner_cache[key] = value
    return value


def _passes_size(path: Path, minimum: int, scanner: dict) -> bool:
    """按预设的最小文件大小过滤；STRM 可依预设豁免。"""
    if minimum <= 0:
        return True
    try:
        from .tasks import _passes_minimum_size
        return _passes_minimum_size(path, minimum, {'scanner': scanner})
    except Exception:  # noqa: BLE001
        try:
            return path.stat().st_size >= minimum
        except OSError:
            return False


def _group_videos(paths: list[Path]) -> list[list[Path]]:
    """同一目录内的视频按引擎规则分组：数字分片、CD/part 与字母分片都合并成一部影片"""
    scanner = _scanner_config()
    if scanner is not None:
        try:
            from javsp.multipart import group_files
            groups = group_files(paths, scanner)
            if groups:
                return groups
        except Exception:  # noqa: BLE001 - 编号有歧义时退回逐文件分组，不中断整库扫描
            pass
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        grouped.setdefault(_part_key(path.stem).casefold(), []).append(path)
    return list(grouped.values())


def _film_id(root_id: str, dir_path: str, part_key: str) -> str:
    digest = hashlib.sha1(f'{root_id}|{dir_path.casefold()}|{part_key.casefold()}'.encode('utf-8')).hexdigest()
    return digest[:12]


# ---------------------------------------------------------------------------
# Libraries（一个媒体库 = 一个名字 + 多个文件夹）
# ---------------------------------------------------------------------------


def _root_rows(db, library_id: str) -> list[sqlite3.Row]:
    # 分集不算「条目」：剧集库的 film_count 是剧的数量
    return db.execute(
        """SELECT r.*, (SELECT count(*) FROM films f WHERE f.root_id=r.id AND f.kind<>'episode') AS film_count
           FROM roots r JOIN library_roots lr ON lr.root_id=r.id
           WHERE lr.library_id=? ORDER BY lr.added_at, r.path""", (library_id,)).fetchall()


LIBRARY_KINDS = ('movie', 'series')


def _normalize_kind(value) -> str:
    text = str(value or '').strip().lower()
    return text if text in LIBRARY_KINDS else 'movie'


def _library_from_row(db, row: sqlite3.Row) -> dict:
    folders = [{'id': item['id'], 'path': item['path'], 'name': item['name'],
                'recursive': bool(item['recursive']), 'film_count': item['film_count'],
                'last_scan_at': item['last_scan_at'], 'scan_status': item['scan_status']}
               for item in _root_rows(db, row['id'])]
    kind = _normalize_kind(row['kind'] if 'kind' in row.keys() else 'movie')
    return {
        'id': row['id'], 'name': row['name'], 'preset_id': row['preset_id'], 'kind': kind,
        'created_at': row['created_at'], 'last_scan_at': row['last_scan_at'],
        'scan_status': row['scan_status'], 'folders': folders,
        'film_count': sum(item['film_count'] for item in folders),
        'minimum_size': preset_scan_limits(row['preset_id'])[1],
        'stats': _stats(db, library_id=row['id']),
    }


def list_libraries() -> list[dict]:
    with connection() as db:
        rows = db.execute('SELECT * FROM libraries ORDER BY created_at, name').fetchall()
        return [_library_from_row(db, row) for row in rows]


def get_library(library_id: str) -> dict | None:
    with connection() as db:
        row = db.execute('SELECT * FROM libraries WHERE id=?', (library_id,)).fetchone()
        return _library_from_row(db, row) if row else None


def add_library(name: str, preset_id: str = 'default', folders: list | None = None,
                kind: str = 'movie') -> dict:
    clean_name = str(name or '').strip()[:80]
    if not clean_name:
        raise ValueError('请填写媒体库名称')
    with connection() as db:
        duplicate = db.execute('SELECT id FROM libraries WHERE lower(name)=lower(?)', (clean_name,)).fetchone()
        if duplicate:
            raise ValueError('已有同名媒体库')
        library_id = uuid.uuid4().hex[:12]
        db.execute('INSERT INTO libraries(id,name,preset_id,kind,created_at) VALUES(?,?,?,?,?)',
                   (library_id, clean_name, preset_id or 'default', _normalize_kind(kind), now_iso()))
    for folder in folders or []:
        add_folder(library_id, folder.get('path') or '', bool(folder.get('recursive', True)))
    return get_library(library_id)


def update_library(library_id: str, *, name: str | None = None, preset_id: str | None = None,
                   kind: str | None = None) -> dict | None:
    fields, params = [], []
    if name is not None:
        clean_name = name.strip()[:80]
        if not clean_name:
            raise ValueError('媒体库名称不能为空')
        fields.append('name=?')
        params.append(clean_name)
    if preset_id is not None:
        fields.append('preset_id=?')
        params.append(preset_id or 'default')
    if kind is not None:
        fields.append('kind=?')
        params.append(_normalize_kind(kind))
    if fields:
        with connection() as db:
            db.execute('UPDATE libraries SET ' + ','.join(fields) + ' WHERE id=?', [*params, library_id])
    return get_library(library_id)


def delete_library(library_id: str) -> bool:
    """删除媒体库及其索引（只删数据库记录，不动磁盘文件）"""
    with connection() as db:
        folders = [row['root_id'] for row in db.execute(
            'SELECT root_id FROM library_roots WHERE library_id=?', (library_id,)).fetchall()]
        db.execute('DELETE FROM libraries WHERE id=?', (library_id,))
        for root_id in folders:
            shared = db.execute('SELECT 1 FROM library_roots WHERE root_id=?', (root_id,)).fetchone()
            if not shared:
                db.execute('DELETE FROM roots WHERE id=?', (root_id,))
        return bool(folders) or True


# ---------------------------------------------------------------------------
# Folders（库里的文件夹）
# ---------------------------------------------------------------------------


def list_folders(library_id: str = '') -> list[dict]:
    with connection() as db:
        if library_id:
            rows = _root_rows(db, library_id)
        else:
            rows = db.execute(
                """SELECT r.*, (SELECT count(*) FROM films f WHERE f.root_id=r.id) AS film_count
                   FROM roots r ORDER BY r.created_at, r.path""").fetchall()
    return [{'id': row['id'], 'path': row['path'], 'name': row['name'],
             'recursive': bool(row['recursive']), 'film_count': row['film_count'],
             'last_scan_at': row['last_scan_at'], 'scan_status': row['scan_status']} for row in rows]


def get_folder(root_id: str) -> dict | None:
    with connection() as db:
        row = db.execute('SELECT * FROM roots WHERE id=?', (root_id,)).fetchone()
    return {**dict(row), 'recursive': bool(row['recursive'])} if row else None


def add_folder(library_id: str, path: str, recursive: bool = True, name: str = '') -> dict:
    """把文件夹加入媒体库；同一个文件夹可以属于多个库，但同一个库内不重复"""
    with connection() as db:
        if not db.execute('SELECT id FROM libraries WHERE id=?', (library_id,)).fetchone():
            raise ValueError('媒体库不存在')
    normalized = _normalize_path(path)
    if not os.path.isdir(normalized):
        raise ValueError('文件夹不存在或不可访问')
    with connection() as db:
        row = db.execute('SELECT * FROM roots WHERE lower(path)=lower(?)', (normalized,)).fetchone()
        if row is None:
            root_id = uuid.uuid4().hex[:12]
            db.execute('INSERT INTO roots(id,name,path,preset_id,recursive,created_at) VALUES(?,?,?,?,?,?)',
                       (root_id, (name or Path(normalized).name or '文件夹').strip()[:80], normalized,
                        'default', 1 if recursive else 0, now_iso()))
        else:
            root_id = row['id']
            if db.execute('SELECT 1 FROM library_roots WHERE library_id=? AND root_id=?',
                          (library_id, root_id)).fetchone():
                raise ValueError('该文件夹已经在这个媒体库里')
            db.execute('UPDATE roots SET recursive=? WHERE id=?', (1 if recursive else 0, root_id))
        db.execute('INSERT OR IGNORE INTO library_roots(library_id,root_id,added_at) VALUES(?,?,?)',
                   (library_id, root_id, now_iso()))
    return get_folder(root_id)


def update_folder(root_id: str, *, recursive: bool | None = None, name: str | None = None) -> dict | None:
    fields, params = [], []
    if recursive is not None:
        fields.append('recursive=?')
        params.append(1 if recursive else 0)
    if name is not None:
        fields.append('name=?')
        params.append(name.strip()[:80] or '文件夹')
    if fields:
        with connection() as db:
            db.execute('UPDATE roots SET ' + ','.join(fields) + ' WHERE id=?', [*params, root_id])
    return get_folder(root_id)


def remove_folder(library_id: str, root_id: str) -> bool:
    """从媒体库移除文件夹；没有其他库引用时连同索引一起删除（不动磁盘）"""
    with connection() as db:
        removed = db.execute('DELETE FROM library_roots WHERE library_id=? AND root_id=?',
                             (library_id, root_id)).rowcount > 0
        shared = db.execute('SELECT 1 FROM library_roots WHERE root_id=?', (root_id,)).fetchone()
        if removed and not shared:
            db.execute('DELETE FROM roots WHERE id=?', (root_id,))
        return removed


# ---------------------------------------------------------------------------
# NFO / artwork discovery


def _folder_videos(directory: Path) -> list[Path]:
    extensions = _video_extensions()
    try:
        return [item for item in directory.iterdir() if item.is_file() and item.suffix.lower() in extensions]
    except OSError:
        return []


def canonical_nfo_path(directory: Path, video: Path) -> Path:
    """每部影片唯一的 NFO 路径。

    多视频目录用 `<视频文件名>.nfo`（Emby/Jellyfin 只认这个，movie.nfo 会互相打架）；
    单视频目录用 movie.nfo（引擎默认命名，Emby 也认），但目录里已经有同名 NFO 时沿用。
    """
    named = directory / f'{video.stem}.nfo'
    if len(_folder_videos(directory)) > 1:
        return named
    return named if named.is_file() else directory / 'movie.nfo'


def canonical_nfo_basename(directory: Path, video: Path) -> str:
    """同一规则的文件名（不含扩展名），用于让引擎写到同一个 NFO。"""
    return canonical_nfo_path(directory, video).stem


def _nfo_candidates(directory: Path, video: Path) -> list[Path]:
    canonical = canonical_nfo_path(directory, video)
    candidates = [canonical] if canonical.is_file() else []
    # 多视频目录里的 movie.nfo 是多部影片共用的，必须忽略（Emby 也不会用它）
    if len(_folder_videos(directory)) <= 1:
        other = directory / ('movie.nfo' if canonical.name != 'movie.nfo' else f'{video.stem}.nfo')
        if other.is_file() and other != canonical:
            candidates.append(other)
    return candidates


def _parse_nfo(path: Path) -> dict:
    tree = ET.parse(path)
    root = tree.getroot()
    if str(root.tag).lower() != 'movie':
        raise ValueError('NFO 根节点不是 movie')

    def text(tag: str) -> str:
        element = root.find(tag)
        return (element.text or '').strip() if element is not None else ''

    dvdid = ''
    for item in root.findall('uniqueid'):
        if (item.get('type') or '').lower() in ('num', 'dvdid') and (item.text or '').strip():
            dvdid = item.text.strip()
            break
    actress = []
    for actor in root.findall('actor'):
        name = (actor.findtext('name') or '').strip()
        if name:
            actress.append(name)
    runtime = text('runtime')
    return {
        'title': text('title'), 'original_title': text('originaltitle'), 'plot': text('plot'),
        'release_date': text('premiered'), 'studio': text('studio'), 'publisher': text('publisher'),
        'director': text('director'), 'dvdid': dvdid, 'actress': list(dict.fromkeys(actress)),
        'runtime': int(runtime) if runtime.isdigit() else 0,
    }


def _find_image(directory: Path, stems: list[str], names: list[str]) -> str:
    for name in names:
        for stem in stems:
            for extension in IMAGE_EXTENSIONS:
                candidate = directory / f'{stem}{extension}' if stem else directory / f'{name}{extension}'
                if candidate.is_file():
                    return str(candidate)
    return ''


def _find_cover(directory: Path, stem: str) -> str:
    return (_find_image(directory, ['poster', f'{stem}-poster', 'folder', 'cover'], ['poster'])
            or _find_image(directory, [stem], [f'{stem}-poster']))


def _find_fanart(directory: Path, stem: str) -> tuple[str, int]:
    candidates = ['fanart', f'{stem}-fanart', 'backdrop', f'{stem}-backdrop']
    for name in candidates:
        for extension in IMAGE_EXTENSIONS:
            if (directory / f'{name}{extension}').is_file():
                count = len([item for item in directory.iterdir() if item.is_file()
                             and item.stem.casefold().startswith('fanart') and item.suffix.lower() in IMAGE_EXTENSIONS])
                return str(directory / f'{name}{extension}'), max(1, count)
    return '', 0


def _find_extrafanart(directory: Path) -> str:
    try:
        for item in directory.iterdir():
            if item.is_dir() and item.name.casefold().endswith('extrafanart'):
                return str(item)
    except OSError:
        pass
    return ''


def _guess_dvdid(path: Path) -> str:
    try:
        if str(VENDOR_DIR) not in sys.path:
            sys.path.insert(0, str(VENDOR_DIR))
        from javsp.avid import get_id
        return str(get_id(str(path)) or '').strip().upper()
    except Exception:
        match = re.search(r'(?i)\b(FC2)[-_ ]?(?:PPV[-_ ]?)?(\d{5,7})\b', path.name)
        if match:
            return f'FC2-{match.group(2)}'
        match = re.search(r'(?i)\b([A-Z]{2,10})[-_ ](\d{2,8})\b', path.name)
        return f'{match.group(1).upper()}-{match.group(2)}' if match else ''


def _status_for(dvdid: str, title: str, cover: str, has_nfo: bool, nfo_error: str,
                actress: list[str], studio: str, release_date: str) -> tuple[str, list[str]]:
    missing = []
    if not dvdid:
        missing.append('dvdid')
    if not title:
        missing.append('title')
    if not cover:
        missing.append('cover')
    if not actress:
        missing.append('actress')
    if not studio:
        missing.append('studio')
    if not release_date:
        missing.append('release_date')
    if nfo_error:
        return 'conflict', missing
    if dvdid and title and cover:
        return 'complete', missing
    if has_nfo or dvdid or title or cover:
        return 'partial', missing
    return 'unscraped', missing


def _record_for_files(root: dict, directory: Path, files: list[Path], single_group: bool) -> dict:
    files = sorted(files, key=lambda item: str(item).casefold())
    video = files[0]
    stem = _part_key(video.stem)
    nfo_path = ''
    nfo_error = ''
    metadata = {}
    best_score = -1
    for candidate in _nfo_candidates(directory, video):
        try:
            parsed = _parse_nfo(candidate)
        except (OSError, ET.ParseError, ValueError) as exc:
            nfo_error = f'NFO 解析失败：{exc}'
            nfo_path = str(candidate)
            break
        # 多个 NFO 都在时挑资料最全的那个，避免被只写了番号的 stub 抢走
        score = sum(1 for key in ('title', 'dvdid', 'studio', 'release_date', 'runtime')
                    if parsed.get(key)) + (1 if parsed.get('actress') else 0)
        if score > best_score:
            best_score, metadata, nfo_path = score, parsed, str(candidate)
    cover = _find_cover(directory, video.stem)
    _, fanart_count = _find_fanart(directory, video.stem)
    dvdid = str(metadata.get('dvdid') or '')
    source = 'nfo' if dvdid else ''
    if not dvdid:
        dvdid = _guess_dvdid(video)
        source = 'filename' if dvdid else ''
    title = str(metadata.get('title') or '')
    actress = metadata.get('actress') or []
    status, missing = _status_for(dvdid, title, cover, bool(nfo_path), nfo_error,
                                  actress, str(metadata.get('studio') or ''), str(metadata.get('release_date') or ''))
    mtime = 0.0
    size = 0
    for item in files:
        try:
            stat = item.stat()
            size += stat.st_size
            mtime = max(mtime, stat.st_mtime)
        except OSError:
            continue
    nfo_mtime = Path(nfo_path).stat().st_mtime if nfo_path and Path(nfo_path).exists() else 0
    return {
        'id': _film_id(root['id'], str(directory), stem),
        'root_id': root['id'], 'dir_path': str(directory), 'video_path': str(video),
        'files': [str(item) for item in files],
        'size_bytes': size,
        'mtime': mtime,
        'dvdid': dvdid, 'dvdid_source': source,
        'title': title, 'original_title': str(metadata.get('original_title') or ''),
        'plot': str(metadata.get('plot') or ''), 'release_date': str(metadata.get('release_date') or ''),
        'studio': str(metadata.get('studio') or ''), 'publisher': str(metadata.get('publisher') or ''),
        'director': str(metadata.get('director') or ''), 'runtime': int(metadata.get('runtime') or 0),
        'actress': actress, 'actress_text': '、'.join(actress),
        'nfo_path': nfo_path, 'nfo_mtime': nfo_mtime,
        'cover_path': cover, 'fanart_count': fanart_count, 'extrafanart_dir': _find_extrafanart(directory),
        'status': status, 'missing_fields': missing, 'nfo_error': nfo_error,
    }


def _series_record(root: dict, series_dir: Path, entries: list[dict]) -> dict:
    """一个剧集目录 → series 条目。它本身没有视频，播放的是它的分集。"""
    show_nfo = series_dir / 'tvshow.nfo'
    meta = series_scan.parse_tvshow_nfo(show_nfo) if show_nfo.is_file() else {}
    title = str(meta.get('title') or '') or series_scan.clean_series_title(series_dir.name) or series_dir.name
    cover = series_scan.find_poster(series_dir)
    if not cover:
        poster = str(meta.get('poster_path') or '')
        cover = poster if poster and not poster.startswith(('http://', 'https://')) else ''
    size = 0
    mtime = 0.0
    for entry in entries:
        try:
            stat = entry['video'].stat()
            size += stat.st_size
            mtime = max(mtime, stat.st_mtime)
        except OSError:
            continue
    actors = [item['name'] for item in meta.get('actors') or []]
    if title and cover:
        status = 'complete'
    elif title or cover or meta:
        status = 'partial'
    else:
        status = 'unscraped'
    return {
        'id': _film_id(root['id'], str(series_dir), 'series'),
        'root_id': root['id'], 'kind': 'series', 'parent_id': '', 'season': 0, 'episode': 0,
        'dir_path': str(series_dir), 'video_path': '',
        'files': [str(entry['video']) for entry in entries],
        'size_bytes': size, 'mtime': mtime,
        'dvdid': '', 'dvdid_source': '',
        'title': title,
        'original_title': str(meta.get('original_title') or ''),
        'plot': str(meta.get('plot') or ''),
        'release_date': str(meta.get('premiered') or meta.get('year') or ''),
        'studio': str(meta.get('studio') or ''), 'publisher': '', 'director': '',
        'runtime': 0,
        'actress': actors, 'actress_text': '、'.join(actors),
        'nfo_path': str(show_nfo) if meta else '',
        'nfo_mtime': show_nfo.stat().st_mtime if meta and show_nfo.exists() else 0,
        'cover_path': cover,
        'fanart_count': 1 if series_scan.find_fanart(series_dir) else 0,
        'extrafanart_dir': '',
        'status': status, 'missing_fields': [], 'nfo_error': '',
    }


def _episode_record(root: dict, series_record: dict, entry: dict) -> dict:
    """一集 → episode 条目（季集号、剧照、NFO 资料都来自本地）。"""
    video: Path = entry['video']
    meta = entry.get('meta') or {}
    nfo = entry.get('nfo')
    title = str(meta.get('title') or '') or video.stem
    thumb = str(meta.get('thumb_path') or '') or series_scan.find_thumb(video)
    size = 0
    mtime = 0.0
    try:
        stat = video.stat()
        size, mtime = stat.st_size, stat.st_mtime
    except OSError:
        pass
    actors = [item['name'] for item in meta.get('actors') or []]
    if meta.get('title') and thumb:
        status = 'complete'
    elif title or meta:
        status = 'partial'
    else:
        status = 'unscraped'
    return {
        'id': _film_id(root['id'], str(video), 'episode'),
        'root_id': root['id'], 'kind': 'episode', 'parent_id': series_record['id'],
        'season': int(entry.get('season') or 0), 'episode': int(entry.get('episode') or 0),
        'dir_path': str(video.parent), 'video_path': str(video), 'files': [str(video)],
        'size_bytes': size, 'mtime': mtime,
        'dvdid': '', 'dvdid_source': '',
        'title': title,
        'original_title': '',
        'plot': str(meta.get('plot') or ''),
        'release_date': str(meta.get('premiered') or ''),
        'studio': str(meta.get('studio') or ''),
        'publisher': '',
        'director': '、'.join(meta.get('directors') or []),
        'runtime': int(meta.get('runtime') or 0),
        'actress': actors, 'actress_text': '、'.join(actors),
        'nfo_path': str(nfo) if nfo else '',
        'nfo_mtime': nfo.stat().st_mtime if nfo and nfo.exists() else 0,
        'cover_path': thumb,
        'fanart_count': 0,
        'extrafanart_dir': '',
        'status': status, 'missing_fields': [], 'nfo_error': '',
    }


def _series_records_for(root: dict, series_dir: Path, scanner: dict, minimum: int) -> tuple[list[dict], int]:
    """单个剧集目录 → [series, episode...]。刷新单部剧时复用。

    剧集的最小文件大小取 `min(预设, SERIES_MIN_SIZE)`：预设的阈值是按整部影片定的
    （默认 232MiB），而一集短剧/番剧只有几十 MB，直接套上去会把整部剧过滤光。
    """
    extensions = _video_extensions()
    limit = min(int(minimum or 0), SERIES_MIN_SIZE)
    entries = []
    skipped = 0
    for entry in series_scan.episode_entries(series_dir, extensions):
        if _passes_size(entry['video'], limit, scanner):
            entries.append(entry)
        else:
            skipped += 1
    series_record = _series_record(root, series_dir, entries)
    records = [series_record, *(_episode_record(root, series_record, entry) for entry in entries)]
    return records, skipped


def _series_records(root: dict, scanner: dict, minimum: int) -> tuple[list[dict], int]:
    """把一个剧集库文件夹展开成 series + episode 记录。"""
    extensions = _video_extensions()
    records: list[dict] = []
    skipped = 0
    for series_dir in series_scan.series_dirs(Path(root['path']), extensions):
        part, part_skipped = _series_records_for(root, series_dir, scanner, minimum)
        records.extend(part)
        skipped += part_skipped
    return records, skipped


def _write_film(db, record: dict, existing: sqlite3.Row | None, preserve_manual: bool = True,
                detached: int = 0) -> bool:
    kind = str(record.get('kind') or 'movie')
    if existing is not None:
        if preserve_manual and kind == 'movie' and existing['dvdid_source'] == 'manual':
            if record.get('dvdid') and record['dvdid'].casefold() == existing['dvdid'].casefold():
                record['dvdid_source'] = 'nfo'
            else:
                record['dvdid'] = existing['dvdid']
                record['dvdid_source'] = 'manual'
        # NFO 被 Emby/Jellyfin 改写成只剩标题的 stub 时，别让一次重扫把已经刮好的资料冲掉
        if kind == 'movie' and not record.get('title') and existing['title']:
            record['title'] = existing['title']
            for field in ('original_title', 'plot', 'release_date', 'studio', 'publisher', 'director'):
                if not record.get(field):
                    record[field] = existing[field]
            if not record.get('runtime'):
                record['runtime'] = existing['runtime']
            if not record.get('actress'):
                kept = [item['name'] for item in db.execute(
                    'SELECT name FROM film_actress WHERE film_id=? ORDER BY rowid', (record['id'],)).fetchall()]
                if kept:
                    record['actress'] = kept
                    record['actress_text'] = '、'.join(kept)
            record['status'], record['missing_fields'] = _status_for(
                record.get('dvdid') or '', record.get('title') or '', record.get('cover_path') or '',
                bool(record.get('nfo_path')), '', record.get('actress') or [],
                str(record.get('studio') or ''), str(record.get('release_date') or ''))
        record['scrape_state'] = existing['scrape_state'] if existing['scrape_state'] in ('queued', 'running') else (
            'failed' if existing['scrape_state'] == 'failed' and record['status'] != 'complete' else 'idle')
        record['last_task_id'] = existing['last_task_id']
        record['last_error'] = existing['last_error']
    record.setdefault('scrape_state', 'idle')
    record.setdefault('last_task_id', '')
    record.setdefault('last_error', '')
    record['kind'] = kind
    record.setdefault('parent_id', '')
    record.setdefault('season', 0)
    record.setdefault('episode', 0)
    record['detached'] = detached
    record['updated_at'] = now_iso()
    changed = existing is None or any(existing[key] != record.get(key) for key in (
        'video_path', 'dvdid', 'title', 'status', 'cover_path', 'nfo_path', 'nfo_mtime', 'mtime',
        'kind', 'parent_id', 'season', 'episode'))
    db.execute("""INSERT INTO films(id,root_id,kind,parent_id,season,episode,dir_path,video_path,files,
        size_bytes,mtime,dvdid,dvdid_source,
        title,original_title,plot,release_date,studio,publisher,director,runtime,actress_text,nfo_path,nfo_mtime,
        cover_path,fanart_count,extrafanart_dir,status,missing_fields,scrape_state,last_task_id,last_error,detached,updated_at)
        VALUES(:id,:root_id,:kind,:parent_id,:season,:episode,:dir_path,:video_path,:files,
        :size_bytes,:mtime,:dvdid,:dvdid_source,:title,
        :original_title,:plot,:release_date,:studio,:publisher,:director,:runtime,:actress_text,:nfo_path,
        :nfo_mtime,:cover_path,:fanart_count,:extrafanart_dir,:status,:missing_fields,:scrape_state,
        :last_task_id,:last_error,:detached,:updated_at)
        ON CONFLICT(id) DO UPDATE SET root_id=excluded.root_id,kind=excluded.kind,
        parent_id=excluded.parent_id,season=excluded.season,episode=excluded.episode,
        dir_path=excluded.dir_path,
        video_path=excluded.video_path,files=excluded.files,size_bytes=excluded.size_bytes,mtime=excluded.mtime,
        dvdid=excluded.dvdid,dvdid_source=excluded.dvdid_source,title=excluded.title,
        original_title=excluded.original_title,plot=excluded.plot,release_date=excluded.release_date,
        studio=excluded.studio,publisher=excluded.publisher,director=excluded.director,runtime=excluded.runtime,
        actress_text=excluded.actress_text,nfo_path=excluded.nfo_path,nfo_mtime=excluded.nfo_mtime,
        cover_path=excluded.cover_path,fanart_count=excluded.fanart_count,extrafanart_dir=excluded.extrafanart_dir,
        status=excluded.status,missing_fields=excluded.missing_fields,scrape_state=excluded.scrape_state,
        last_task_id=excluded.last_task_id,last_error=excluded.last_error,detached=excluded.detached,
        updated_at=excluded.updated_at""",
               {**record, 'files': json.dumps(record['files'], ensure_ascii=False),
                'missing_fields': json.dumps(record['missing_fields'], ensure_ascii=False)})
    db.execute('DELETE FROM film_actress WHERE film_id=?', (record['id'],))
    db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                   [(record['id'], name, index) for index, name in enumerate(record['actress'])])
    return changed


def _film_from_row(db, row: sqlite3.Row, progress: dict | None = None) -> dict:
    item = dict(row)
    item['files'] = json.loads(item.get('files') or '[]')
    item['missing_fields'] = json.loads(item.get('missing_fields') or '[]')
    item['actress'] = [entry['name'] for entry in db.execute(
        'SELECT name FROM film_actress WHERE film_id=? ORDER BY ord', (row['id'],))]
    item['progress'] = (progress or {}).get(row['id']) or _empty_progress()
    return item


# ---------------------------------------------------------------------------
# 播放进度（电影与分集共用一张表，跨设备共享）


def _empty_progress() -> dict:
    return {'position': 0.0, 'duration': 0.0, 'finished': False, 'percent': 0.0, 'updated_at': ''}


def _progress_from_row(row: sqlite3.Row) -> dict:
    position = float(row['position'] or 0)
    duration = float(row['duration'] or 0)
    return {
        'position': position, 'duration': duration, 'finished': bool(row['finished']),
        'percent': round(position / duration * 100, 1) if duration > 0 else 0.0,
        'updated_at': row['updated_at'] or '',
    }


def _progress_map(db, film_ids: list[str]) -> dict[str, dict]:
    ids = [item for item in film_ids if item]
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute(f'SELECT * FROM film_progress WHERE film_id IN ({marks})', ids).fetchall()
    return {row['film_id']: _progress_from_row(row) for row in rows}


def save_progress(film_id: str, position: float, duration: float = 0) -> dict:
    """记录播放进度；看到 95% 以上（或离结尾不足 60 秒）就算看完。"""
    with connection() as db:
        if not db.execute('SELECT 1 FROM films WHERE id=?', (film_id,)).fetchone():
            raise ValueError('条目不存在')
        position = max(0.0, float(position or 0))
        duration = max(0.0, float(duration or 0))
        finished = 1 if duration > 0 and (position >= duration * 0.95 or duration - position <= 60) else 0
        db.execute(
            'INSERT INTO film_progress(film_id,position,duration,finished,updated_at) VALUES(?,?,?,?,?) '
            'ON CONFLICT(film_id) DO UPDATE SET position=excluded.position,duration=excluded.duration,'
            'finished=excluded.finished,updated_at=excluded.updated_at',
            (film_id, position, duration, finished, now_iso()))
        row = db.execute('SELECT * FROM film_progress WHERE film_id=?', (film_id,)).fetchone()
    return _progress_from_row(row)


def clear_progress(film_id: str) -> bool:
    with connection() as db:
        return db.execute('DELETE FROM film_progress WHERE film_id=?', (film_id,)).rowcount > 0


def series_progress(series_id: str) -> dict:
    """整部剧的观看情况：已看几集、下一集从哪开始（给剧集页的「继续观看」用）。"""
    with connection() as db:
        rows = db.execute(
            "SELECT f.id, f.season, f.episode, f.title, p.position, p.duration, p.finished "
            "FROM films f LEFT JOIN film_progress p ON p.film_id=f.id "
            "WHERE f.parent_id=? AND f.kind='episode' ORDER BY f.season, f.episode",
            (series_id,)).fetchall()
    total = len(rows)
    watched = sum(1 for row in rows if row['finished'])
    in_progress = next((row for row in rows if (row['position'] or 0) > 0 and not row['finished']), None)
    unwatched = in_progress or next((row for row in rows if not row['finished']), None)
    resume = None
    if unwatched is not None:
        resume = {'film_id': unwatched['id'], 'season': int(unwatched['season'] or 0),
                  'episode': int(unwatched['episode'] or 0), 'title': unwatched['title'] or '',
                  'position': float(unwatched['position'] or 0),
                  'duration': float(unwatched['duration'] or 0)}
    return {'total': total, 'watched': watched, 'resume': resume}


# ---------------------------------------------------------------------------
# Scanning


def _collect(root: dict, scanner: dict | None = None, minimum: int = 0) -> tuple[dict[tuple[str, str], list[Path]], int]:
    """枚举目录里的视频并分片分组；小于预设最小文件大小的文件会被跳过。"""
    extensions = _video_extensions()
    scanner = scanner or {}
    groups: dict[tuple[str, str], list[Path]] = {}
    skipped = 0
    base_depth = root['path'].rstrip('\\/').count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root['path']):
        dirnames[:] = [name for name in dirnames if not _ignored_dir(name)]
        if not root.get('recursive') and dirpath.rstrip('\\/').count(os.sep) > base_depth:
            dirnames[:] = []
            continue
        videos = []
        for name in sorted(filenames):
            if Path(name).suffix.lower() not in extensions:
                continue
            candidate = Path(dirpath) / name
            if _passes_size(candidate, minimum, scanner):
                videos.append(name)
            else:
                skipped += 1
        if not videos:
            continue
        directory = Path(dirpath)
        for files in _group_videos([directory / name for name in videos]):
            key = _part_key(Path(files[0]).stem).casefold()
            groups.setdefault((dirpath, key), []).extend(files)
    return groups, skipped


def _update_scan(db, scan: dict, **values) -> None:
    """先写库再更新内存：客户端看到「已完成」时，数据一定已经落盘。"""
    fields = ','.join(f'{key}=?' for key in values)
    db.execute(f'UPDATE scans SET {fields} WHERE id=?', [*values.values(), scan['id']])
    db.commit()
    scan.update(values)


def _run_scan(scan: dict, library: dict) -> None:
    started = time.monotonic()
    try:
        with connection() as db:
            _update_scan(db, scan, status='running')
            folders = [{**dict(row), 'recursive': bool(row['recursive'])} for row in _root_rows(db, library['id'])]
            scanner, minimum = preset_scan_limits(library.get('preset_id') or 'default')
            added = updated = removed = discovered = skipped = 0
            for root in folders:
                # 文件夹本身不可访问时（例如移动硬盘未挂载）跳过清点，避免误删整库记录
                if not os.path.isdir(root['path']):
                    db.execute('UPDATE roots SET scan_status=? WHERE id=?', ('failed', root['id']))
                    continue
                if library.get('kind') == 'series':
                    # 剧集库：一个目录一部剧，再拆成分集；不分「分片」，每集独立一条
                    records, root_skipped = _series_records(root, scanner, minimum)
                    skipped += root_skipped
                else:
                    groups, root_skipped = _collect(root, scanner, minimum)
                    skipped += root_skipped
                    records = [
                        _record_for_files(root, Path(directory), files,
                                          sum(1 for key in groups if key[0] == directory) == 1)
                        for (directory, _), files in groups.items()
                    ]
                seen = set()
                for record in records:
                    existing = db.execute('SELECT * FROM films WHERE id=?', (record['id'],)).fetchone()
                    if _write_film(db, record, existing):
                        if existing is None:
                            added += 1
                        else:
                            updated += 1
                    seen.add(record['id'])
                    discovered += 1
                    if discovered % 25 == 0 or time.monotonic() - started > 0.5:
                        _update_scan(db, scan, discovered=discovered, added=added, updated=updated)
                        started = time.monotonic()
                rows = (db.execute('SELECT id,video_path FROM films WHERE root_id=? AND detached=0 '
                                   f'AND id NOT IN ({",".join("?" for _ in seen)})',
                                   [root['id'], *seen]).fetchall()
                        if seen else
                        db.execute('SELECT id,video_path FROM films WHERE root_id=? AND detached=0',
                                   (root['id'],)).fetchall())
                # 这次没扫到、又没有“已整理”标记的影片：源文件已被删除或不再符合扫描条件，
                # 直接从索引移除（用户期望媒体库与磁盘保持一致，而不是留一堆“文件已丢失”）。
                for row in rows:
                    db.execute('DELETE FROM films WHERE id=?', (row['id'],))
                    removed += 1

            # 已整理（文件搬到整理目录）的影片：文件被删掉时也要从库里移除
            stale = db.execute('SELECT id,files FROM films WHERE root_id IN '
                               '(SELECT root_id FROM library_roots WHERE library_id=?) AND detached=1',
                               (library['id'],)).fetchall()
            for row in stale:
                files = [Path(item) for item in json.loads(row['files'] or '[]')]
                if files and all(not item.is_file() for item in files):
                    db.execute('DELETE FROM films WHERE id=?', (row['id'],))
                    removed += 1
                db.execute('UPDATE roots SET last_scan_at=?, scan_status=? WHERE id=?',
                           (now_iso(), 'succeeded', root['id']))
            _update_scan(db, scan, status='succeeded', discovered=discovered, added=added,
                         updated=updated, removed=removed, skipped=skipped, finished_at=now_iso())
            db.execute('UPDATE libraries SET last_scan_at=?, scan_status=? WHERE id=?',
                       (now_iso(), 'succeeded', library['id']))
    except Exception as exc:
        with connection() as db:
            _update_scan(db, scan, status='failed', error=str(exc), finished_at=now_iso())
            db.execute('UPDATE libraries SET scan_status=? WHERE id=?', ('failed', library['id']))
    finally:
        with _scan_lock:
            _scan_state.pop(scan['id'], None)


def start_scan(library_id: str) -> dict:
    library = get_library(library_id)
    if not library:
        raise ValueError('媒体库不存在')
    if not library['folders']:
        raise ValueError('媒体库还没有文件夹')
    unavailable = [folder['path'] for folder in library['folders'] if not os.path.isdir(folder['path'])]
    if len(unavailable) == len(library['folders']):
        raise ValueError('媒体库中的文件夹都不可访问')
    with _scan_lock:
        for item in _scan_state.values():
            if item.get('library_id') == library_id:
                return dict(item)
        scan = {'id': uuid.uuid4().hex[:12], 'library_id': library_id, 'root_id': '', 'status': 'queued',
                'discovered': 0, 'added': 0, 'updated': 0, 'removed': 0, 'error': '', 'started_at': now_iso()}
        _scan_state[scan['id']] = scan
    with connection() as db:
        db.execute('INSERT INTO scans(id,root_id,library_id,status,started_at) VALUES(?,?,?,?,?)',
                   (scan['id'], '', library_id, 'queued', scan['started_at']))
        db.execute('UPDATE libraries SET scan_status=? WHERE id=?', ('running', library_id))
    threading.Thread(target=_run_scan, args=(scan, library), name=f'library-scan-{library_id}', daemon=True).start()
    return scan


def get_scan(scan_id: str) -> dict | None:
    with _scan_lock:
        if scan_id in _scan_state:
            return dict(_scan_state[scan_id])
    with connection() as db:
        row = db.execute('SELECT * FROM scans WHERE id=?', (scan_id,)).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Querying


STATUS_FILTERS = {
    'complete': "status='complete'",
    'partial': "status IN ('partial','conflict')",
    'unscraped': "status='unscraped'",
    'failed': "scrape_state='failed'",
    'scraping': "scrape_state IN ('queued','running')",
    'missing': "status='missing'",
}


def _conditions(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
                group_by: str = '', group: str = '', kind: str = '', parent: str = '',
                season: int | None = None):
    conditions, params = [], []
    if folder_id:
        conditions.append('f.root_id=?')
        params.append(folder_id)
    elif library_id:
        conditions.append('f.root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)')
        params.append(library_id)
    if kind:
        conditions.append('f.kind=?')
        params.append(kind)
    elif not parent:
        # 分集不混进影片墙：只有明确按 parent 看某部剧时才带出来
        conditions.append("f.kind<>'episode'")
        if not library_id and not folder_id:
            # 「全部影片」是跨库的影片墙，剧集由各自的剧集库浏览
            conditions.append("f.kind<>'series'")
    if parent:
        conditions.append('f.parent_id=?')
        params.append(parent)
    if season is not None:
        conditions.append('f.season=?')
        params.append(int(season))
    if status and status in STATUS_FILTERS:
        conditions.append(STATUS_FILTERS[status])
    if query:
        needle = query.casefold()
        conditions.append("(instr(casefold(f.dvdid),?)>0 OR instr(casefold(f.title),?)>0 "
                          "OR instr(casefold(f.video_path),?)>0 OR instr(casefold(f.actress_text),?)>0)")
        params.extend([needle, needle, needle, needle])
    if group_by == 'actress' and group:
        conditions.append('EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id AND a.name=?)')
        params.append(group)
    elif group_by == 'actress' and group == '':
        conditions.append('NOT EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)')
    elif group_by == 'studio':
        conditions.append('f.studio=?')
        params.append(group)
    elif group_by == 'year':
        conditions.append("substr(f.release_date,1,4)=?")
        params.append(group)
    return conditions, params


def list_films(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
               group_by: str = '', group: str = '', sort: str = 'updated', direction: str = 'desc',
               limit: int = 48, offset: int = 0, kind: str = '', parent: str = '',
               season: int | None = None) -> dict:
    conditions, params = _conditions(library_id, folder_id, status, query, group_by, group,
                                     kind, parent, season)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    order = {
        'release': "coalesce(f.release_date,'')",
        'dvdid': 'f.dvdid',
        'title': 'f.title',
        'size': 'f.size_bytes',
    }.get(sort, 'f.updated_at')
    if parent:
        # 分集按季集号排，和影片墙的排序互不影响
        order = 'f.season, f.episode, f.title'
    order += ' ASC' if direction == 'asc' else ' DESC'
    with connection() as db:
        total = db.execute('SELECT count(*) FROM films f' + where, params).fetchone()[0]
        offset = min(offset, max(0, ((total - 1) // limit) * limit)) if total else 0
        rows = db.execute(f'SELECT f.* FROM films f{where} ORDER BY {order},f.id LIMIT ? OFFSET ?',
                          [*params, limit, offset]).fetchall()
        progress = _progress_map(db, [row['id'] for row in rows])
        items = [_film_from_row(db, row, progress) for row in rows]
        stats = _stats(db, library_id=library_id, folder_id=folder_id, query=query,
                       group_by=group_by, group=group, kind=kind, parent=parent, season=season)
    return {'items': items, 'total': total, 'limit': limit, 'offset': offset, 'stats': stats}


def _film_scope(library_id: str = '', folder_id: str = '') -> tuple[str, list]:
    if folder_id:
        return ' WHERE root_id=?', [folder_id]
    if library_id:
        return ' WHERE root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)', [library_id]
    return '', []


def _stats(db, library_id: str = '', folder_id: str = '', query: str = '',
           group_by: str = '', group: str = '', kind: str = '', parent: str = '',
           season: int | None = None) -> dict:
    """统计当前筛选范围（媒体库/文件夹/搜索/分类）的状态分布。

    状态本身是统计维度，所以这里不按 status 过滤；分类和搜索会跟随。
    """
    conditions, params = _conditions(library_id, folder_id, '', query, group_by, group,
                                     kind, parent, season)
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    rows = dict(db.execute('SELECT f.status,count(*) FROM films f' + where + ' GROUP BY f.status',
                           params).fetchall())
    scrape = dict(db.execute('SELECT f.scrape_state,count(*) FROM films f' + where +
                             ' GROUP BY f.scrape_state', params).fetchall())
    return {
        'total': sum(rows.values()),
        'complete': rows.get('complete', 0),
        'partial': rows.get('partial', 0) + rows.get('conflict', 0),
        'unscraped': rows.get('unscraped', 0),
        'missing': rows.get('missing', 0),
        'failed': scrape.get('failed', 0),
        'scraping': scrape.get('queued', 0) + scrape.get('running', 0),
    }


def series_stats(library_id: str = '', parent: str = '') -> dict[str, dict]:
    """每部剧的季数与集数（series id → {seasons, episodes}），用于剧集卡片。"""
    conditions, params = ["kind='episode'"], []
    if parent:
        conditions.append('parent_id=?')
        params.append(parent)
    elif library_id:
        conditions.append('root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)')
        params.append(library_id)
    where = ' WHERE ' + ' AND '.join(conditions)
    with connection() as db:
        rows = db.execute(
            f'SELECT parent_id, count(*), count(DISTINCT season) FROM films{where} GROUP BY parent_id',
            params).fetchall()
    return {str(row[0]): {'episodes': int(row[1]), 'seasons': int(row[2])} for row in rows if row[0]}


def series_seasons(series_id: str) -> list[dict]:
    """一部剧的季列表（季号 + 集数），用于剧集页的季切换标签。"""
    with connection() as db:
        rows = db.execute(
            "SELECT season, count(*) FROM films WHERE parent_id=? AND kind='episode' "
            'GROUP BY season ORDER BY season', (series_id,)).fetchall()
    return [{'season': int(row[0] or 0), 'episodes': int(row[1])} for row in rows]


def refresh_series(film_id: str) -> dict:
    """重新读取某部剧的 tvshow.nfo 与分集 NFO。

    剧集库不接在线元数据源（18+ 内容在公开库里也查不到），所以「刷新」= 按磁盘现状重建索引：
    补了新一集、改了目录名、加了 NFO，点一下即可同步。
    """
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            raise ValueError('条目不存在')
        if row['kind'] != 'series':
            raise ValueError('只有剧集条目可以刷新')
        root_row = db.execute('SELECT * FROM roots WHERE id=?', (row['root_id'],)).fetchone()
        if not root_row:
            raise ValueError('剧集所在文件夹已移除')
        root = {**dict(root_row), 'recursive': bool(root_row['recursive'])}
        library = db.execute('SELECT * FROM libraries WHERE id IN '
                             '(SELECT library_id FROM library_roots WHERE root_id=?)',
                             (root['id'],)).fetchone()
    series_dir = Path(row['dir_path'])
    if not series_dir.is_dir():
        raise ValueError('剧集目录不存在，无法刷新')
    scanner, minimum = preset_scan_limits((library['preset_id'] if library else 'default') or 'default')
    records, _ = _series_records_for(root, series_dir, scanner, minimum)
    keep = {record['id'] for record in records}
    with connection() as db:
        for record in records:
            existing = db.execute('SELECT * FROM films WHERE id=?', (record['id'],)).fetchone()
            _write_film(db, record, existing)
        stale = db.execute("SELECT id FROM films WHERE parent_id=? AND kind='episode'", (film_id,)).fetchall()
        for item in stale:
            if item['id'] not in keep:
                db.execute('DELETE FROM films WHERE id=?', (item['id'],))
        db.execute('UPDATE films SET updated_at=? WHERE id=?', (now_iso(), film_id))
    return get_film(film_id) or {}


def get_film(film_id: str, reconcile: bool = True) -> dict | None:
    if reconcile:
        _reconcile(film_id)
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            return None
        return _film_from_row(db, row, _progress_map(db, [film_id]))


def _refresh_film(film_id: str, preserve_manual: bool = True) -> None:
    with connection() as db:
        row = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        if not row:
            return
        root = db.execute('SELECT * FROM roots WHERE id=?', (row['root_id'],)).fetchone()
        if not root:
            return
        files = [Path(item) for item in json.loads(row['files'] or '[]') if Path(item).is_file()]
        if not files:
            db.execute("UPDATE films SET status='missing',updated_at=? WHERE id=?", (now_iso(), film_id))
            return
        single_group = len({_part_key(item.stem).casefold() for item in files}) == 1
        record = _record_for_files({**dict(root), 'recursive': bool(root['recursive'])},
                                   Path(row['dir_path']), files, single_group)
        record['id'] = film_id
        existing = db.execute('SELECT * FROM films WHERE id=?', (film_id,)).fetchone()
        # 刷新时保持原有的“已整理”标记：文件不在库目录是正常的
        _write_film(db, record, existing, preserve_manual=preserve_manual,
                    detached=int(existing['detached']) if existing is not None else 0)


def _reconcile(film_id: str) -> None:
    from . import task_store
    with connection() as db:
        row = db.execute('SELECT scrape_state,last_task_id FROM films WHERE id=?', (film_id,)).fetchone()
    if not row or row['scrape_state'] not in ('queued', 'running') or not row['last_task_id']:
        return
    state = task_store.statuses([row['last_task_id']]).get(row['last_task_id'])
    if state in ('queued', 'running'):
        return
    if state == 'succeeded':
        _refresh_film(film_id)
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif state in ('failed', 'cancelled'):
        from . import task_store as store
        record = store.record(row['last_task_id'], logs=False) or {}
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='failed',last_error=?,updated_at=? WHERE id=?",
                       (str(record.get('error') or ('任务已取消' if state == 'cancelled' else '刮削失败')), now_iso(), film_id))
    else:
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',updated_at=? WHERE id=?", (now_iso(), film_id))


def _organized_videos(task: dict, film: dict) -> list[Path]:
    """任务整理后的视频文件。

    只认引擎上报的 `organized_files`；退回扫描 save_dir 时必须文件名与本片一致，
    否则会把同一目录下的其它影片当成这部影片的分片（原地整理时尤其危险）。
    """
    extensions = _video_extensions()
    organizer = task.get('file_organizer') or {}
    candidates = [Path(item) for item in (organizer.get('organized_files') or []) if str(item)]
    videos = [path for path in candidates if path.is_file() and path.suffix.lower() in extensions]
    if videos:
        return videos
    save_dir = str(((task.get('progress') or {}).get('output') or {}).get('save_dir') or '')
    if not save_dir or not Path(save_dir).is_dir():
        return []
    stems = {Path(item).stem.casefold() for item in json.loads(film.get('files') or '[]')}
    current_stem = Path(str(film.get('video_path') or '')).stem.casefold()
    try:
        return sorted(path for path in Path(save_dir).iterdir()
                      if path.is_file() and path.suffix.lower() in extensions
                      and (path.stem.casefold() in stems
                           or (current_stem and path.stem.casefold() == current_stem)))
    except OSError:
        return []


def _relocate_film(film_id: str, task: dict) -> bool:
    """整理会把文件移出媒体库目录；把索引搬到新位置，影片就不会变成“文件已丢失”。

    只有在原文件确实不在时才搬家：整理目录和源目录重合（原地整理）时，
    绝不能把目录里的其它视频当成本片的文件。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        return False
    current = Path(str(film.get('video_path') or ''))
    if current.is_file():
        if film.get('detached'):
            with connection() as db:
                db.execute('UPDATE films SET detached=0 WHERE id=?', (film_id,))
        return False
    videos = _organized_videos(task, film)
    if not videos:
        return False
    video = videos[0]
    with connection() as db:
        db.execute("UPDATE films SET dir_path=?,video_path=?,files=?,detached=1,updated_at=? WHERE id=?",
                   (str(video.parent), str(video), json.dumps([str(path) for path in videos]), now_iso(), film_id))
    _refresh_film(film_id)
    return True


def _fill_from_task_metadata(film_id: str, task: dict) -> bool:
    """用任务里抓到的资料补影片的空字段，并写回它自己的 NFO。

    引擎抓到的资料可能被第三方（Jellyfin / Emby 打开“保存 NFO”时）用只含
    <title> 的 stub 覆盖掉；媒体库这时会显示“缺资料”，但任务记录里其实有数据。
    """
    meta = ((task.get('progress') or {}).get('metadata') or {})
    if not isinstance(meta, dict) or not meta:
        return False
    film = get_film(film_id, reconcile=False)
    if not film:
        return False

    def pick(key: str, incoming) -> str:
        # 任务刚抓到的资料优先（这才是刮削的结果）；抓取里没有的字段才沿用原值
        value = str(incoming or '').strip()
        return value or str(film.get(key) or '')

    crawled_actress = [str(item).strip() for item in (meta.get('actress') or []) if str(item).strip()]
    actress = crawled_actress or (film.get('actress') or [])
    values = {
        'dvdid': film.get('dvdid') or str(meta.get('dvdid') or ''),
        'title': pick('title', meta.get('title')),
        'original_title': film.get('original_title') or '',
        'plot': film.get('plot') or '',
        'release_date': pick('release_date', meta.get('publish_date')),
        'studio': pick('studio', meta.get('producer')),
        'publisher': pick('publisher', meta.get('publisher')),
        'director': pick('director', meta.get('director')),
        'runtime': film.get('runtime') or 0,
        'actress': actress,
    }
    changed = (
        values['title'] != (film.get('title') or '')
        or values['release_date'] != (film.get('release_date') or '')
        or values['studio'] != (film.get('studio') or '')
        or values['publisher'] != (film.get('publisher') or '')
        or values['director'] != (film.get('director') or '')
        or values['actress'] != (film.get('actress') or [])
    )
    if not changed:
        return False
    target = _nfo_target(film)
    expected = target.stat().st_mtime if target.is_file() else None
    try:
        _write_nfo(film, values, expected)
    except OSError:
        pass
    _refresh_film(film_id)
    return True


def refresh_film_after_task(film_id: str, status: str, error: str = '', task: dict | None = None) -> None:
    """任务结束后刷新影片索引：成功时重读磁盘（必要时跟随整理后的新路径），失败时保留状态与原因。"""
    if not film_id:
        return
    with connection() as db:
        row = db.execute('SELECT id FROM films WHERE id=?', (film_id,)).fetchone()
    if not row:
        return
    if status == 'succeeded':
        if not _relocate_film(film_id, task or {}):
            _refresh_film(film_id)
        merged = _merge_nfo_snapshot(get_film(film_id, reconcile=False) or {'id': film_id})
        if merged:
            _refresh_film(film_id)
        # NFO 被第三方改写时，用任务里的资料兜底，保证媒体库立刻显示最新结果
        _fill_from_task_metadata(film_id, task or {})
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',last_error='',updated_at=? WHERE id=?",
                       (now_iso(), film_id))
    elif status in ('failed', 'cancelled'):
        _drop_snapshot(film_id)
        with connection() as db:
            db.execute("UPDATE films SET scrape_state=?,last_error=?,updated_at=? WHERE id=?",
                       ('failed', error or ('任务已取消' if status == 'cancelled' else '刮削失败'), now_iso(), film_id))
    else:
        with connection() as db:
            db.execute("UPDATE films SET scrape_state='idle',updated_at=? WHERE id=?", (now_iso(), film_id))


def film_ids(library_id: str = '', folder_id: str = '', status: str = '', query: str = '',
             group_by: str = '', group: str = '', limit: int = 2000) -> list[str]:
    """按媒体库的筛选条件取影片 id，供批量刮削使用（与列表页同一套条件）。

    剧集库不联网刮削，所以这里只取普通影片。
    """
    conditions, params = _conditions(library_id, folder_id, status, query, group_by, group)
    conditions.append("f.kind='movie'")
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    with connection() as db:
        rows = db.execute(
            f'SELECT f.id FROM films f{where} ORDER BY f.updated_at DESC, f.id LIMIT ?',
            [*params, max(1, int(limit))]).fetchall()
    return [row['id'] for row in rows]


def groups(by: str, library_id: str = '', query: str = '', folder_id: str = '') -> dict:
    if by not in ('actress', 'studio', 'year'):
        raise ValueError('不支持的分类方式')
    if folder_id:
        films_where = ' WHERE f.root_id=?'
        params = [folder_id]
    elif library_id:
        films_where = ' WHERE f.root_id IN (SELECT root_id FROM library_roots WHERE library_id=?)'
        params = [library_id]
    else:
        films_where, params = '', []
    with connection() as db:
        if by == 'actress':
            rows = db.execute(f"""SELECT a.name AS key, count(*) AS count FROM film_actress a
                JOIN films f ON f.id=a.film_id{films_where}
                GROUP BY a.name ORDER BY count DESC, a.name""", params).fetchall()
            missing = db.execute(f"""SELECT count(*) FROM films f{films_where}
                {'AND' if films_where else 'WHERE'} NOT EXISTS (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)""",
                                 params).fetchone()[0]
        elif by == 'studio':
            rows = db.execute(f"""SELECT coalesce(nullif(f.studio,''),'') AS key, count(*) AS count
                FROM films f{films_where} GROUP BY key ORDER BY count DESC, key""", params).fetchall()
            missing = 0
        else:
            rows = db.execute(f"""SELECT substr(coalesce(f.release_date,''),1,4) AS key, count(*) AS count
                FROM films f{films_where} GROUP BY key ORDER BY key DESC""", params).fetchall()
            missing = 0
        # 封面用一次窗口函数查询取完，避免每个分类都单独查一遍（637 个女优时会慢到数秒）
        if by == 'actress':
            cover_key = 'a.name'
            cover_join = ' JOIN film_actress a ON a.film_id=f.id'
        elif by == 'studio':
            cover_key = "coalesce(nullif(f.studio,''),'')"
            cover_join = ''
        else:
            cover_key = "substr(coalesce(f.release_date,''),1,4)"
            cover_join = ''
        cover_rows = db.execute(
            f"""SELECT key, film_id, updated_at FROM (
                SELECT {cover_key} AS key, f.id AS film_id, f.updated_at AS updated_at,
                       ROW_NUMBER() OVER (PARTITION BY {cover_key} ORDER BY f.updated_at DESC, f.id) AS rn
                FROM films f{cover_join}{films_where}
                {'AND' if films_where else 'WHERE'} f.cover_path<>''
            ) WHERE rn<=9""", params).fetchall()
        covers_by_key = {}
        for item in cover_rows:
            covers_by_key.setdefault(item['key'] or '', []).append(
                {'id': item['film_id'], 'v': item['updated_at']})
        items = []
        for row in rows:
            key = row['key'] or ''
            if by == 'actress' and not key:
                continue
            covers = covers_by_key.get(key, [])
            items.append({'key': key, 'name': key or {'actress': '未知女优', 'studio': '未知片商', 'year': '未知年份'}[by],
                          'count': row['count'], 'covers': covers})
        if by == 'actress' and missing:
            covers = [{'id': item['id'], 'v': item['updated_at']} for item in db.execute(
                f"""SELECT f.id,f.updated_at FROM films f{films_where}
                {'AND' if films_where else 'WHERE'} f.cover_path<>'' AND NOT EXISTS
                (SELECT 1 FROM film_actress a WHERE a.film_id=f.id)""", params).fetchall()[:9]]
            items.append({'key': '', 'name': '未知女优', 'count': missing, 'covers': covers})
        if query:
            items = [item for item in items if query.casefold() in item['name'].casefold()]
        total = sum(item['count'] for item in items)
        # “全部影片”卡片：与概览一致，给出总数和封面拼贴素材
        all_count = db.execute('SELECT count(*) FROM films f' + films_where, params).fetchone()[0]
        all_covers = [{'id': item['id'], 'v': item['updated_at']} for item in db.execute(
            f"""SELECT f.id,f.updated_at FROM films f{films_where}
            {'AND' if films_where else 'WHERE'} f.cover_path<>''
            ORDER BY f.updated_at DESC LIMIT 9""", params).fetchall()]
        stats = _stats(db, library_id=library_id, folder_id=folder_id)
    return {'by': by, 'groups': items, 'total': total,
            'all': {'count': all_count, 'covers': all_covers}, 'stats': stats}


# ---------------------------------------------------------------------------
# Metadata / NFO writing


# 不同条目对应的 NFO 根节点：电影 <movie>、剧集 <tvshow>、分集 <episodedetails>
NFO_ROOT_BY_KIND = {'movie': 'movie', 'series': 'tvshow', 'episode': 'episodedetails'}


def nfo_root_tag(kind: str) -> str:
    return NFO_ROOT_BY_KIND.get(kind or 'movie', 'movie')


def _nfo_target(film: dict) -> Path:
    """写入目标：始终用唯一的规范 NFO（避免同一部影片出现两个 NFO）。"""
    directory = Path(film['dir_path'])
    # 剧集条目没有视频文件，资料写在剧集目录的 tvshow.nfo
    if film.get('kind') == 'series':
        return directory / 'tvshow.nfo'
    video = Path(film['video_path'])
    canonical = canonical_nfo_path(directory, video)
    if canonical.is_file():
        return canonical
    # 多视频目录绝不复用 movie.nfo，否则一部影片的写入会串到别的影片
    if len(_folder_videos(directory)) <= 1 and film.get('nfo_path') and Path(film['nfo_path']).is_file():
        return Path(film['nfo_path'])
    return canonical


def _replace_text(root: ET.Element, tag: str, value: str) -> None:
    matches = list(root.findall(tag))
    if value:
        target = matches[0] if matches else ET.SubElement(root, tag)
        target.text = value
        for extra in matches[1:]:
            root.remove(extra)
    else:
        for match in matches:
            root.remove(match)


def _snapshot_nfo(film: dict) -> Path | None:
    """刮削前把现有 NFO 备份到快照目录，任务结束后用来补齐被引擎覆盖掉的字段。"""
    source = _nfo_target(film)
    if not source.is_file():
        return None
    try:
        NFO_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot = NFO_SNAPSHOT_DIR / f"{film['id']}.nfo"
        snapshot.write_bytes(source.read_bytes())
        return snapshot
    except OSError:
        return None


def _snapshot_path(film_id: str) -> Path:
    return NFO_SNAPSHOT_DIR / f"{film_id}.nfo"


def _drop_snapshot(film_id: str) -> None:
    try:
        _snapshot_path(film_id).unlink(missing_ok=True)
    except OSError:
        pass


# 这些标签是“多值”结构（每个演员/分类/标签一个节点），缺失时整组补回
_MULTI_TAGS = {"actor", "genre", "tag"}
# 只在旧 NFO 有、新 NFO 完全没有时才补回的文本字段
_TEXT_TAGS = {"title", "originaltitle", "plot", "runtime", "rating", "premiered",
              "studio", "publisher", "director", "set", "country", "mpaa"}


def _merge_nfo_snapshot(film: dict) -> bool:
    """把刮削前的资料补回新 NFO：只补引擎没有写出的字段，不覆盖新抓到的值。"""
    snapshot = _snapshot_path(film['id'])
    if not snapshot.is_file():
        return False
    target = _nfo_target(film)
    if not target.is_file():
        _drop_snapshot(film['id'])
        return False
    # 只允许写回本片的 NFO：同名 NFO 或本片目录下的 movie.nfo
    video_dir = Path(str(film.get('video_path') or '')).parent
    stem = Path(str(film.get('video_path') or '')).stem.casefold()
    if target.parent != video_dir or target.stem.casefold() not in {stem, 'movie'}:
        _drop_snapshot(film['id'])
        return False
    try:
        old_root = ET.parse(snapshot).getroot()
    except (OSError, ET.ParseError):
        _drop_snapshot(film['id'])
        return False
    # 快照必须属于本片：番号对不上说明索引/路径错位，宁可放弃合并也不能串号
    film_dvdid = str(film.get('dvdid') or '').strip().upper()
    snapshot_dvdid = ''
    for node in old_root.findall('uniqueid'):
        if (node.get('type') or '').lower() == 'num' and (node.text or '').strip():
            snapshot_dvdid = node.text.strip().upper()
            break
    if film_dvdid and snapshot_dvdid and film_dvdid != snapshot_dvdid:
        _drop_snapshot(film['id'])
        return False
    try:
        new_root = ET.parse(target).getroot()
    except (OSError, ET.ParseError):
        _drop_snapshot(film['id'])
        return False
    changed = False
    for tag in _TEXT_TAGS:
        if list(new_root.findall(tag)):
            continue
        for node in old_root.findall(tag):
            if (node.text or '').strip():
                new_root.append(copy.deepcopy(node))
                changed = True
                break
    for tag in _MULTI_TAGS:
        if list(new_root.findall(tag)):
            continue
        for node in old_root.findall(tag):
            new_root.append(copy.deepcopy(node))
            changed = True
    # uniqueid(type=num/cid) 属于身份字段，新 NFO 缺失时补回
    existing_ids = {(node.get('type') or '').lower() for node in new_root.findall('uniqueid')}
    for node in old_root.findall('uniqueid'):
        kind = (node.get('type') or '').lower()
        if kind and kind not in existing_ids and (node.text or '').strip():
            new_root.append(copy.deepcopy(node))
            changed = True
    _drop_snapshot(film['id'])
    if not changed:
        return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        backup = target.with_suffix(target.suffix + '.bak')
        shutil.copy2(target, backup)
        temporary = target.with_suffix(target.suffix + '.tmp')
        tree = ET.ElementTree(new_root)
        ET.indent(tree, space='  ')
        tree.write(temporary, encoding='utf-8', xml_declaration=True)
        temporary.replace(target)
    except OSError:
        return False
    return True


def _write_nfo(film: dict, values: dict, expected_mtime: float | None) -> Path:
    kind = film.get('kind') or 'movie'
    root_tag = nfo_root_tag(kind)
    target = _nfo_target(film)
    if expected_mtime is not None and target.is_file():
        actual = target.stat().st_mtime
        if abs(actual - float(expected_mtime)) > 0.001:
            raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    head_comments = ''
    if target.is_file():
        try:
            raw = target.read_text(encoding='utf-8-sig', errors='replace')
        except OSError:
            raw = ''
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith('<?xml') or not stripped:
                continue
            if stripped.startswith('<!--'):
                head_comments += line.rstrip() + '\n'
                continue
            break
        root = ET.parse(target).getroot()
        if str(root.tag).lower() != root_tag:
            raise ValueError(f'现有 NFO 根节点不是 {root_tag}，已停止写入')
    else:
        root = ET.Element(root_tag)
    _replace_text(root, 'title', values.get('title', ''))
    _replace_text(root, 'originaltitle', values.get('original_title', ''))
    _replace_text(root, 'plot', values.get('plot', ''))
    if kind == 'series':
        # <tvshow>：没有番号、没有时长，年份与首播日期分开写
        _replace_text(root, 'showtitle', values.get('title', ''))
        _replace_text(root, 'premiered', values.get('release_date', ''))
        _replace_text(root, 'year', (values.get('release_date') or '')[:4])
        _replace_text(root, 'studio', values.get('studio', ''))
    elif kind == 'episode':
        # <episodedetails>：播出日期写 aired，季集号以索引为准
        _replace_text(root, 'aired', values.get('release_date', ''))
        _replace_text(root, 'season', str(film.get('season') or 0))
        _replace_text(root, 'episode', str(film.get('episode') or 0))
        _replace_text(root, 'runtime', str(values['runtime']) if values.get('runtime') else '')
    else:
        _replace_text(root, 'premiered', values.get('release_date', ''))
        _replace_text(root, 'studio', values.get('studio', ''))
        _replace_text(root, 'publisher', values.get('publisher', ''))
        _replace_text(root, 'director', values.get('director', ''))
        _replace_text(root, 'runtime', str(values['runtime']) if values.get('runtime') else '')
        for item in list(root.findall('uniqueid')):
            if (item.get('type') or '').lower() == 'num':
                root.remove(item)
        if values.get('dvdid'):
            ET.SubElement(root, 'uniqueid', {'type': 'num', 'default': 'true'}).text = values['dvdid']
    for item in list(root.findall('actor')):
        root.remove(item)
    for name in values.get('actress') or []:
        actor = ET.SubElement(root, 'actor')
        ET.SubElement(actor, 'name').text = name
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        backup = target.with_suffix(target.suffix + '.bak')
        shutil.copy2(target, backup)
    temporary = target.with_suffix(target.suffix + '.tmp')
    tree.write(temporary, encoding='utf-8', xml_declaration=True)
    if head_comments:
        text = temporary.read_text(encoding='utf-8')
        declaration, separator, rest = text.partition('\n')
        temporary.write_text(f'{declaration}{separator}{head_comments}{rest}', encoding='utf-8')
    temporary.replace(target)
    return target


def _status_for_update(film: dict, payload: dict, nfo_path: str) -> tuple[str, list[str]]:
    """保存资料后重算状态：电影沿用必需字段校验，剧集/分集按标题+封面判断。"""
    kind = film.get('kind') or 'movie'
    if kind == 'series':
        if payload['title'] and film.get('cover_path'):
            return 'complete', []
        if payload['title'] or film.get('cover_path') or nfo_path:
            return 'partial', []
        return 'unscraped', []
    if kind == 'episode':
        has_thumb = bool(film.get('cover_path'))
        if payload['title'] and has_thumb:
            return 'complete', []
        if payload['title'] or nfo_path:
            return 'partial', []
        return 'unscraped', []
    return _status_for(payload['dvdid'], payload['title'], film['cover_path'],
                       bool(nfo_path), '', payload['actress'], payload['studio'], payload['release_date'])


def update_film(film_id: str, values: dict, write_nfo: bool = True,
                expected_nfo_mtime: float | None = None) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    dvdid = str(values.get('dvdid') or '').strip().upper()
    payload = {
        'dvdid': dvdid, 'title': str(values.get('title') or '').strip(),
        'original_title': str(values.get('original_title') or '').strip(),
        'plot': str(values.get('plot') or '').strip(),
        'release_date': str(values.get('release_date') or '').strip(),
        'studio': str(values.get('studio') or '').strip(),
        'publisher': str(values.get('publisher') or '').strip(),
        'director': str(values.get('director') or '').strip(),
        'runtime': int(values.get('runtime') or 0),
        'actress': [str(item).strip() for item in values.get('actress') or [] if str(item).strip()],
    }
    nfo_path = film['nfo_path']
    if write_nfo:
        nfo_path = str(_write_nfo(film, payload, expected_nfo_mtime))
    status, missing = _status_for_update(film, payload, nfo_path)
    with connection() as db:
        db.execute("""UPDATE films SET dvdid=?,dvdid_source=?,title=?,original_title=?,plot=?,release_date=?,
            studio=?,publisher=?,director=?,runtime=?,actress_text=?,nfo_path=?,nfo_mtime=?,status=?,
            missing_fields=?,updated_at=? WHERE id=?""",
                   (payload['dvdid'], 'manual' if dvdid else film['dvdid_source'], payload['title'],
                    payload['original_title'], payload['plot'], payload['release_date'], payload['studio'],
                    payload['publisher'], payload['director'], payload['runtime'], '、'.join(payload['actress']),
                    nfo_path, Path(nfo_path).stat().st_mtime if nfo_path and Path(nfo_path).exists() else 0,
                    status, json.dumps(missing, ensure_ascii=False), now_iso(), film_id))
        db.execute('DELETE FROM film_actress WHERE film_id=?', (film_id,))
        db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                       [(film_id, name, index) for index, name in enumerate(payload['actress'])])
    return get_film(film_id, reconcile=False)


def read_nfo(film_id: str) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    target = _nfo_target(film)
    if not target.is_file():
        return {'content': '', 'path': str(target), 'mtime': 0}
    return {'content': target.read_text(encoding='utf-8', errors='replace'), 'path': str(target),
            'mtime': target.stat().st_mtime}


def _season_dir(film: dict, season: int) -> Path:
    """季目录：多季时 NFO 放在 Sxx 子目录，单季就放在剧集目录。"""
    base = Path(film['dir_path'])
    try:
        for child in base.iterdir():
            if child.is_dir() and series_scan.season_from_dir_name(child.name) == int(season):
                return child
    except OSError:
        pass
    return base


def read_season_nfo(series_id: str, season: int) -> dict:
    film = get_film(series_id, reconcile=False)
    if not film or film.get('kind') != 'series':
        raise ValueError('不是剧集条目')
    target = _season_dir(film, season) / 'season.nfo'
    if not target.is_file():
        return {'content': '', 'path': str(target), 'mtime': 0, 'season': int(season)}
    return {'content': target.read_text(encoding='utf-8', errors='replace'), 'path': str(target),
            'mtime': target.stat().st_mtime, 'season': int(season)}


def write_season_nfo(series_id: str, season: int, content: str,
                     expected_mtime: float | None = None) -> dict:
    """写入某一季的 season.nfo（根节点 season）。"""
    film = get_film(series_id, reconcile=False)
    if not film or film.get('kind') != 'series':
        raise ValueError('不是剧集条目')
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f'NFO XML 格式错误：{exc}') from exc
    if str(root.tag).lower() != 'season':
        raise ValueError('季 NFO 的根节点必须是 season')
    target = _season_dir(film, season) / 'season.nfo'
    if expected_mtime is not None and target.is_file() and \
            abs(target.stat().st_mtime - float(expected_mtime)) > 0.001:
        raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file():
        shutil.copy2(target, target.with_suffix(target.suffix + '.bak'))
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(target)
    return read_season_nfo(series_id, season)


def _apply_nfo_fields(film_id: str, content: str) -> None:
    """剧集/分集直接改原始 NFO 后，把常见字段回写到索引。

    电影走 `_refresh_film`（按文件重建条目），但剧集条目没有自己的视频，
    用那套逻辑会把 series 记录冲成电影记录，所以这里只按标签回填。
    """
    film = get_film(film_id, reconcile=False)
    if not film:
        return
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return

    def text(tag: str) -> str:
        return (root.findtext(tag) or '').strip()

    actors = []
    for node in root.findall('actor'):
        name = (node.findtext('name') or '').strip()
        if name:
            actors.append(name)
    payload = {
        'title': text('title') or film['title'],
        'original_title': text('originaltitle') or film['original_title'],
        'plot': text('plot') or film['plot'],
        'release_date': text('premiered') or text('aired') or text('year') or film['release_date'],
        'studio': text('studio') or film['studio'],
        'actress': actors or film['actress'],
    }
    target = _nfo_target(film)
    status, missing = _status_for_update(film, {**payload, 'dvdid': film['dvdid']}, str(target))
    with connection() as db:
        db.execute("""UPDATE films SET title=?,original_title=?,plot=?,release_date=?,studio=?,
            actress_text=?,nfo_path=?,nfo_mtime=?,status=?,missing_fields=?,updated_at=? WHERE id=?""",
                   (payload['title'], payload['original_title'], payload['plot'], payload['release_date'],
                    payload['studio'], '、'.join(payload['actress']), str(target),
                    target.stat().st_mtime if target.is_file() else 0,
                    status, json.dumps(missing, ensure_ascii=False), now_iso(), film_id))
        db.execute('DELETE FROM film_actress WHERE film_id=?', (film_id,))
        db.executemany('INSERT OR IGNORE INTO film_actress(film_id,name,ord) VALUES(?,?,?)',
                       [(film_id, name, index) for index, name in enumerate(payload['actress'])])


def write_raw_nfo(film_id: str, content: str, expected_mtime: float | None = None) -> dict | None:
    film = get_film(film_id, reconcile=False)
    if not film:
        return None
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError(f'NFO XML 格式错误：{exc}') from exc
    expected = nfo_root_tag(film.get('kind') or 'movie')
    if str(root.tag).lower() != expected:
        raise ValueError(f'NFO 根节点必须是 {expected}')
    target = _nfo_target(film)
    if expected_mtime is not None and target.is_file() and abs(target.stat().st_mtime - float(expected_mtime)) > 0.001:
        raise FileExistsError('NFO 已被其他程序修改，请重新读取后再保存')
    if target.is_file():
        shutil.copy2(target, target.with_suffix(target.suffix + '.bak'))
    temporary = target.with_suffix(target.suffix + '.tmp')
    temporary.write_text(content, encoding='utf-8')
    temporary.replace(target)
    if film.get('kind') in ('series', 'episode'):
        _apply_nfo_fields(film_id, content)
    else:
        _refresh_film(film_id, preserve_manual=False)
    return read_nfo(film_id)


# ---------------------------------------------------------------------------
# Scrape integration


def start_scrape(film_id: str, dvdid: str | None = None, preset_id: str | None = None) -> dict:
    from . import tasks
    film = get_film(film_id)
    if not film:
        raise ValueError('影片不存在')
    if film.get('kind') in ('series', 'episode'):
        raise ValueError('剧集库不联网刮削，请用「刷新」重新读取 NFO')
    if film['scrape_state'] in ('queued', 'running'):
        raise ValueError('该影片正在刮削')
    video = Path(film['video_path'])
    if not video.is_file():
        raise ValueError('视频文件不存在或不可访问')
    if dvdid is not None:
        value = str(dvdid).strip().upper()
        if value:
            with connection() as db:
                db.execute("UPDATE films SET dvdid=?,dvdid_source='manual',updated_at=? WHERE id=?",
                           (value, now_iso(), film_id))
            film['dvdid'] = value
    forced = film['dvdid'] or None
    preset = preset_id or library_preset_for_folder(film['root_id']) or 'default'
    # 引擎写 NFO 是整份重建：先留一份旧资料，任务结束后补回没被抓到的字段
    _snapshot_nfo(film)
    task = tasks.create_task(str(video), preset, source='library', forced_avid=forced,
                             library_film_id=film_id)
    with connection() as db:
        db.execute("UPDATE films SET scrape_state='queued',last_task_id=?,last_error='',updated_at=? WHERE id=?",
                   (task['id'], now_iso(), film_id))
    return {'task_id': task['id'], 'film': get_film(film_id, reconcile=False)}


def library_preset_for_folder(root_id: str) -> str:
    """影片所属媒体库的默认预设（一个文件夹可能属于多个库，取最早创建的那个）"""
    with connection() as db:
        row = db.execute(
            """SELECT l.preset_id FROM libraries l JOIN library_roots lr ON lr.library_id=l.id
               WHERE lr.root_id=? ORDER BY l.created_at LIMIT 1""", (root_id,)).fetchone()
    return (row['preset_id'] if row else '') or ''


def library_for_folder(root_id: str) -> dict | None:
    with connection() as db:
        row = db.execute(
            """SELECT l.* FROM libraries l JOIN library_roots lr ON lr.library_id=l.id
               WHERE lr.root_id=? ORDER BY l.created_at LIMIT 1""", (root_id,)).fetchone()
        return _library_from_row(db, row) if row else None
