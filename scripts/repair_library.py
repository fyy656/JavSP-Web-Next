"""修复媒体库索引与 NFO：按磁盘重建索引，并恢复被写错的 NFO。

用法（先停止刮削任务 / 关闭程序，避免边修边写）：

    python scripts/repair_library.py --dry-run          # 只报告
    python scripts/repair_library.py                    # 备份并重建索引
    python scripts/repair_library.py --fix-nfo          # 同时用 .nfo.bak 恢复写错的 NFO
    python scripts/repair_library.py --library 女优合集  # 只修一个库

修复动作：
1. 备份 data/library.db；
2. 逐个媒体库重新扫描（索引以磁盘为准）——这会纠正被改错的 video_path/files/nfo_path，
   并删除磁盘上已经不存在的记录；
3. `--fix-nfo` 时，对“NFO 里的番号与文件名不一致”的文件，如果同名 `.nfo.bak` 的番号
   与文件名一致，就用备份覆盖回去。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'vendor' / 'JavSP'))

import os

os.environ.setdefault('JAVSP_WEB_DATA_DIR', str(ROOT / 'data'))

from javsp_web import library_store, storage  # noqa: E402

DVDID_RE = re.compile(r'(?i)\b([A-Z]{2,6})[-_]?(\d{2,5})\b')


def dvdid_of_text(text: str) -> str:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001
        return ''
    for node in root.findall('uniqueid'):
        if (node.get('type') or '').lower() == 'num' and (node.text or '').strip():
            return node.text.strip().upper()
    title = (root.findtext('title') or '').strip()
    match = DVDID_RE.match(title)
    return f'{match.group(1)}-{match.group(2)}'.upper() if match else ''


def dvdid_of_name(name: str) -> str:
    match = DVDID_RE.search(name)
    return f'{match.group(1)}-{match.group(2)}'.upper() if match else ''


def index_stats(db_file: Path) -> tuple[int, int, int, int]:
    db = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT video_path, files, detached FROM films').fetchall()
    finally:
        db.close()
    counts = Counter(row[0] for row in rows)
    duplicated = sum(1 for value in counts.values() if value > 1)
    big = sum(1 for row in rows if len(json.loads(row[1] or '[]')) > 5)
    detached = sum(1 for row in rows if row[2])
    return len(rows), duplicated, big, detached


def _same_volume_number(left: str, right: str) -> bool:
    """番号前缀写法常不同（如 714ANAN-042 vs ANAN-042），数字相同就当成同一部。"""
    def digits(value: str) -> str:
        return ''.join(char for char in value.split('-')[-1] if char.isdigit()).lstrip('0')

    return bool(digits(left)) and digits(left) == digits(right)


def fix_nfo_files(libraries: list[dict], dry_run: bool) -> None:
    restored = 0
    unfixable: list[str] = []
    seen: set[Path] = set()
    for library in libraries:
        for folder in library['folders']:
            base = Path(folder['path'])
            if not base.is_dir():
                continue
            for nfo in base.rglob('*.nfo'):
                if nfo in seen:
                    continue
                seen.add(nfo)
                try:
                    text = nfo.read_text(encoding='utf-8', errors='replace')
                except OSError:
                    continue
                file_id = dvdid_of_name(nfo.stem)
                nfo_id = dvdid_of_text(text)
                if not file_id or not nfo_id or file_id == nfo_id or _same_volume_number(file_id, nfo_id):
                    continue
                backup = nfo.with_suffix(nfo.suffix + '.bak')
                backup_id = ''
                if backup.is_file():
                    backup_id = dvdid_of_text(backup.read_text(encoding='utf-8', errors='replace'))
                if backup_id == file_id:
                    print(f'  恢复 {nfo.name[:70]} ← .bak（{backup_id}）')
                    if not dry_run:
                        shutil.copy2(backup, nfo)
                    restored += 1
                else:
                    unfixable.append(str(nfo))
    print(f'  NFO 恢复: {restored} 个' + ('（dry-run 未写入）' if dry_run else ''))
    if unfixable:
        print(f'  无法自动恢复: {len(unfixable)} 个（建议修复索引后重新刮削）')
        for path in unfixable[:10]:
            print(f'    {path}')


def prune_foreign_rows(db_file: Path, dry_run: bool) -> int:
    """删除“记录的 id 与它自己的文件名不匹配”的残留行（串号后的遗留）。

    扫描是按磁盘文件名生成 id 的，所以这类行对应的正确记录一定已经存在。
    """
    db = sqlite3.connect(f'file:{db_file}?mode=ro', uri=True)
    try:
        rows = db.execute('SELECT id, root_id, dir_path, video_path FROM films').fetchall()
    finally:
        db.close()
    stale = []
    for film_id, root_id, dir_path, video_path in rows:
        expected = library_store._film_id(root_id, dir_path,
                                          library_store._part_key(Path(video_path).stem))
        if expected != film_id:
            stale.append((film_id, video_path))
    if stale and not dry_run:
        db = sqlite3.connect(str(db_file))
        try:
            db.executemany('DELETE FROM film_actress WHERE film_id=?', [(film_id,) for film_id, _ in stale])
            db.executemany('DELETE FROM films WHERE id=?', [(film_id,) for film_id, _ in stale])
            db.commit()
        finally:
            db.close()
    print(f'  串号残留: {len(stale)} 条' + ('（dry-run 未删除）' if dry_run else '，已删除'))
    for film_id, video_path in stale[:5]:
        print(f'    {Path(video_path).name[:70]}')
    return len(stale)


def main() -> int:
    parser = argparse.ArgumentParser(description='修复媒体库索引与 NFO')
    parser.add_argument('--dry-run', action='store_true', help='只报告，不修改')
    parser.add_argument('--fix-nfo', action='store_true', help='用 .nfo.bak 恢复写错的 NFO')
    parser.add_argument('--library', default='', help='只修复指定名称的媒体库')
    args = parser.parse_args()

    db_file = Path(os.environ['JAVSP_WEB_DATA_DIR']) / 'library.db'
    if not db_file.is_file():
        print('找不到 library.db:', db_file)
        return 1
    libraries = library_store.list_libraries()
    if args.library:
        libraries = [item for item in libraries if item['name'] == args.library]
        if not libraries:
            print('没有名为', args.library, '的媒体库')
            return 1

    print('修复前: 影片 %d | 重复 video_path %d 部 | files>5 %d 部 | detached %d' % index_stats(db_file))
    if args.dry_run:
        print('dry-run：将重新扫描', len(libraries), '个媒体库:', [item['name'] for item in libraries])
    else:
        backup = db_file.parent / f'library.db.repair-{time.strftime("%Y%m%d-%H%M%S")}.bak'
        shutil.copy2(db_file, backup)
        for suffix in ('-wal', '-shm'):
            side = Path(str(db_file) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(backup) + suffix))
        print('已备份数据库:', backup.name)
        for library in libraries:
            if not library['folders']:
                continue
            print('重新扫描媒体库:', library['name'], '|', [folder['path'] for folder in library['folders']])
            job = library_store.start_scan(library['id'])
            deadline = time.time() + 1800
            while time.time() < deadline:
                scan = library_store.get_scan(job['id'])
                if scan and scan['status'] in ('succeeded', 'failed'):
                    print(f"  {scan['status']} | 发现 {scan['discovered']} | 更新 {scan['updated']} | 移除 {scan['removed']} | 跳过 {scan.get('skipped', 0)}")
                    break
                time.sleep(0.5)
            else:
                print('  扫描超时，仍在使用旧索引')
    if not args.dry_run:
        prune_foreign_rows(db_file, args.dry_run)
    if args.fix_nfo:
        print('检查 NFO 与文件名是否一致…')
        fix_nfo_files(libraries, args.dry_run)
    if not args.dry_run:
        print('修复后: 影片 %d | 重复 video_path %d 部 | files>5 %d 部 | detached %d' % index_stats(db_file))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
