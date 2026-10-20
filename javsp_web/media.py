from __future__ import annotations

import time
import os
import socket
import struct
import re
import hashlib
import threading
from pathlib import Path
from urllib.parse import quote
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from .storage import list_media_servers, list_path_mappings


def _docker_host_gateway() -> str | None:
    """Return Docker's current bridge gateway without relying on a DNS alias."""
    try:
        with open("/proc/net/route", encoding="ascii") as routes:
            for line in routes.readlines()[1:]:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<L", int(fields[2], 16)))
    except (OSError, ValueError, struct.error):
        return None
    return None


def _service_url(server: dict) -> str:
    """Resolve Docker-local localhost to the container's host bridge gateway."""
    value = str(server.get("url") or "").strip().rstrip("/")
    in_docker = os.path.exists("/.dockerenv") or os.environ.get("JAVSP_WEB_DOCKER") == "1"
    parsed = urlsplit(value)
    if not in_docker or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return value
    host = _docker_host_gateway() or "host.docker.internal"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)).rstrip("/")


def _headers(server: dict) -> dict[str, str]:
    key = str(server.get("api_key") or "").strip()
    headers = {"Accept": "application/json"}
    if key:
        headers["X-Emby-Token"] = key
        if server.get("type") == "jellyfin":
            headers["Authorization"] = ('MediaBrowser Client="JavSP Web", Device="Server", '
                                        'DeviceId="javsp-web", Version="1.0", Token="' + key.replace('"', '') + '"')
    return headers


def _params(server: dict, **params: object) -> dict[str, object]:
    return dict(params)


_PLAYBACK_USER_CACHE: dict[str, str] = {}
_PLAYBACK_LOCK = threading.Lock()

# 让 Emby 把不兼容的容器/编码转成「渐进式 MP4（H.264 + AAC）」，
# 这样浏览器用原生 <video> 就能播，不需要额外引入 HLS 播放库。
_PLAYBACK_DEVICE_PROFILE = {
    "Name": "JavSP Web",
    "MaxStreamingBitrate": 40_000_000,
    "MaxStaticBitrate": 40_000_000,
    "MusicStreamingTranscodingBitrate": 192_000,
    "DirectPlayProfiles": [],
    "TranscodingProfiles": [
        # HLS 分片：浏览器可以随意拖动进度条；放不出来时下面的渐进式 MP4 兜底
        {"Container": "ts", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac",
         "Protocol": "Hls", "Context": "Streaming", "MaxAudioChannels": "2", "CopyTimestamps": False},
        {"Container": "mp4", "Type": "Video", "VideoCodec": "h264", "AudioCodec": "aac",
         "Protocol": "Http", "Context": "Streaming", "MaxAudioChannels": "2", "CopyTimestamps": False},
    ],
    "CodecProfiles": [],
    "ContainerProfiles": [],
    "ResponseProfiles": [],
}


def _playback_user_id(server: dict) -> str:
    """取一个可用的 Emby/Jellyfin 用户 id（用于 PlaybackInfo），按服务器缓存。"""
    cache_key = f"{server.get('id')}:{server.get('url')}"
    with _PLAYBACK_LOCK:
        cached = _PLAYBACK_USER_CACHE.get(cache_key)
    if cached:
        return cached
    user_id = ""
    try:
        me = requests.get(f"{_service_url(server)}/Users/Me", headers=_playback_headers(server), timeout=15)
        if me.status_code == 200:
            payload = me.json()
            if isinstance(payload, dict):
                user_id = str(payload.get("Id") or "")
    except (requests.RequestException, ValueError):
        user_id = ""
    if not user_id:
        try:
            response = requests.get(f"{_service_url(server)}/Users", headers=_playback_headers(server), timeout=15)
            response.raise_for_status()
            users = response.json() or []
        except (requests.RequestException, ValueError):
            users = []
        for user in users:
            user_id = str(user.get("Id") or "")
            if user.get("Policy", {}).get("IsAdministrator"):
                break
    if user_id:
        with _PLAYBACK_LOCK:
            _PLAYBACK_USER_CACHE[cache_key] = user_id
    return user_id


def _with_api_key(url: str, key: str) -> str:
    """把 api_key 拼进查询串；URL 里已经有 api_key 时不再重复添加（重复会让 Emby 返回 401）。"""
    if not url or not key:
        return url
    parsed = urlsplit(url)
    query = parsed.query
    if any(name == "api_key" for name, _ in parse_qsl(query, keep_blank_values=True)):
        return url
    extra = urlencode({"api_key": key})
    query = f"{query}&{extra}" if query else extra
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


def _playback_headers(server: dict) -> dict:
    headers = _headers(server)
    headers.update({
        "X-Emby-Client": "JavSP Web",
        "X-Emby-Client-Version": "1.0",
        "X-Emby-Device-Id": "javsp-web-player",
        "X-Emby-Device-Name": "JavSP Web",
    })
    return headers


# 浏览器（Chrome/Edge/Safari/手机）能直接解码的容器；其余交给 Emby 转码
# 默认所有视频容器都先走本地直连；浏览器解不了时再退回媒体服务器转码
LOCAL_PLAYABLE_SUFFIXES = {
    ".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".wmv", ".asf",
    ".mpg", ".mpeg", ".m2ts", ".mts", ".ts", ".flv", ".rmvb", ".rm",
    ".3gp", ".ogv", ".divx", ".vob",
}


def local_playable(film: dict) -> bool:
    """这个文件能否由浏览器直接播放（容器层面判断，不看编码）。"""
    path = Path(str(film.get("video_path") or ""))
    return path.suffix.lower() in LOCAL_PLAYABLE_SUFFIXES and path.is_file()


_RESOLVED_TTL = 3600
_RESOLVED_CACHE: dict[tuple[str, str], tuple[float, dict, dict]] = {}
_PATH_INDEX_TTL = 600
_PATH_INDEX_CACHE: dict[str, tuple[float, dict[str, dict]]] = {}
_PLAYBACK_LOCK = threading.Lock()


def _normalize_path(value) -> str:
    return str(value or "").replace("\\", "/").rstrip("/").casefold()


def _items_by_path(server: dict, path: str) -> list[dict]:
    """按绝对路径精确查条目；Emby 的标题常常不含番号，按名称搜索会漏。"""
    try:
        response = requests.get(f"{_service_url(server)}/Items", headers=_playback_headers(server),
                                params={"Path": path, "Recursive": "true",
                                        "Fields": "Path,MediaSources", "Limit": 10}, timeout=25)
        response.raise_for_status()
        return [item for item in (response.json() or {}).get("Items", []) if item.get("Id")]
    except (requests.RequestException, ValueError):
        return []


def _server_libraries(server: dict) -> list[str]:
    """媒体服务器上所有库的 id（用于遍历条目建立路径索引）。"""
    try:
        response = requests.get(f"{_service_url(server)}/Library/MediaFolders", headers=_playback_headers(server),
                                timeout=20)
        response.raise_for_status()
        return [str(item.get("Id")) for item in (response.json() or {}).get("Items", []) if item.get("Id")]
    except (requests.RequestException, ValueError):
        return []


def _library_path_index(server: dict, force: bool = False) -> dict[str, dict]:
    """拉一份「文件路径 → 条目」的索引（按服务器缓存 10 分钟），用于路径/文件名匹配。"""
    key = str(server.get("id") or server.get("url") or "")
    with _PLAYBACK_LOCK:
        cached = _PATH_INDEX_CACHE.get(key)
    if cached and not force and time.monotonic() - cached[0] < _PATH_INDEX_TTL:
        return cached[1]
    index: dict[str, dict] = {}
    for library_id in _server_libraries(server):
        start = 0
        while True:
            params = {"ParentId": library_id, "Recursive": "true", "IncludeItemTypes": "Movie,Video",
                      "Fields": "Path", "Limit": 200, "StartIndex": start,
                      "EnableTotalRecordCount": "false"}
            try:
                response = requests.get(f"{_service_url(server)}/Items", headers=_playback_headers(server),
                                        params=params, timeout=30)
                response.raise_for_status()
                items = (response.json() or {}).get("Items", [])
            except (requests.RequestException, ValueError):
                break
            if not items:
                break
            for item in items:
                item_path = _normalize_path(item.get("Path"))
                if item_path and item.get("Id"):
                    index.setdefault(item_path, item)
            if len(items) < 200:
                break
            start += 200
    with _PLAYBACK_LOCK:
        _PATH_INDEX_CACHE[key] = (time.monotonic(), index)
    return index


def _lookup_by_file(index: dict[str, dict], video: Path) -> dict | None:
    wanted = _normalize_path(video)
    if wanted in index:
        return index[wanted]
    name = video.name.casefold()
    matches = [item for path, item in index.items() if path.rsplit("/", 1)[-1] == name]
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_playback_item(film: dict) -> tuple[dict, dict] | None:
    """在媒体服务器的所有库里找这部影片，只以**文件路径和文件名**为准。

    1. 绝对路径精确查询（服务端与媒体服务器看到同一路径时最快）
    2. 库索引里按路径 / 文件名匹配（盘符或挂载点不同也能命中）
    标题与番号不参与匹配：Emby 里的标题经常不含番号，按名称搜索会漏片。
    """
    video = Path(str(film.get("video_path") or ""))
    if not video.name:
        return None
    film_id = str(film.get("id") or "")
    for server in list_media_servers():
        cache_key = (str(server.get("id") or ""), film_id)
        cached = _RESOLVED_CACHE.get(cache_key)
        if cached and time.monotonic() - cached[0] < _RESOLVED_TTL:
            return cached[1], cached[2]
        resolved = _resolve_on_server(server, video)
        if resolved is not None:
            if film_id:
                _RESOLVED_CACHE[cache_key] = (time.monotonic(), server, resolved)
            return server, resolved
    return None


def _resolve_on_server(server: dict, video: Path) -> dict | None:
    found = _items_by_path(server, str(video))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        exact = [item for item in found if _normalize_path(item.get("Path")) == _normalize_path(video)]
        if len(exact) == 1:
            return exact[0]
        return None
    item = _lookup_by_file(_library_path_index(server), video)
    if item is not None:
        return item
    # 索引可能过期：强制刷新一次再试
    return _lookup_by_file(_library_path_index(server, force=True), video)


def film_playback(film: dict, prefer: str = "auto", start_seconds: int = 0) -> dict:
    """把媒体库影片映射到 Emby/Jellyfin 条目，返回浏览器可直接播放的 URL。

    B3 方案：URL 里带上服务器的 api_key（不做反向代理），由 Emby 决定直连还是转码。
    """
    film_id = str(film.get("id") or "")
    if prefer in ("auto", "local") and local_playable(film):
        return {
            "mode": "local",
            "server_id": "",
            "server_name": "",
            "item_id": "",
            "url": f"/api/library/films/{quote(film_id)}/stream",
            "fallback_url": "",
            "fallback_mode": "emby" if prefer == "auto" else "",
            "start_seconds": max(0, int(start_seconds or 0)),
            "container": Path(str(film.get("video_path") or "")).suffix.lower().lstrip("."),
            "transcoding": False,
            "play_session_id": "",
        }
    resolved = resolve_playback_item(film)
    if resolved is None:
        raise ValueError("没有在已连接的 Emby / Jellyfin 里找到这部影片：请确认媒体服务器已扫描到该文件（播放按文件路径与文件名匹配，不看标题）")
    server, item = resolved
    item_id = str(item.get("Id") or "")
    source_id = str(item.get("MediaSources") and (item["MediaSources"][0].get("Id") or "") or "") or item_id
    base = _service_url(server)
    key = str(server.get("api_key") or "").strip()
    user_id = _playback_user_id(server)
    params = {"UserId": user_id} if user_id else {}
    response = requests.post(f"{base}/Items/{item_id}/PlaybackInfo", headers=_playback_headers(server), params=params,
                             json={"DeviceProfile": _PLAYBACK_DEVICE_PROFILE,
                                   "MaxStreamingBitrate": _PLAYBACK_DEVICE_PROFILE["MaxStreamingBitrate"],
                                   "StartTimeTicks": max(0, int(start_seconds or 0)) * 10_000_000,
                                   # 强制转码：直连/直封装在浏览器里经常"能加载但放不出来"
                                   "EnableDirectPlay": False,
                                   "EnableDirectStream": False,
                                   "AllowVideoStreamCopy": True,
                                   "AllowAudioStreamCopy": True},
                             timeout=30)
    response.raise_for_status()
    data = response.json() or {}
    media_source = (data.get("MediaSources") or [{}])[0]
    source_id = str(media_source.get("Id") or source_id or item_id)

    def absolute(value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        raw = _with_api_key(raw, key)
        return base + raw if raw.startswith("/") else raw

    transcode_url = absolute(str(media_source.get("TranscodingUrl") or ""))
    direct_url = absolute(str(media_source.get("DirectStreamUrl") or ""))
    if not direct_url:
        # 强制转码时 Emby 不会给直连地址，这里自己拼一个静态流作为兜底
        direct_url = absolute(f"/videos/{item_id}/original.mp4?MediaSourceId={quote(source_id)}&DeviceId=javsp-web-player")
    url = transcode_url or direct_url
    if not url:
        raise ValueError("媒体服务器没有返回可播放的地址（请在 Emby 里确认该影片可以播放/允许转码）")
    local_url = f"/api/library/films/{quote(film_id)}/stream" if local_playable(film) else ""
    return {
        "mode": "emby",
        "server_id": str(server.get("id") or ""),
        "server_name": str(server.get("name") or ""),
        "item_id": item_id,
        "url": url,
        "fallback_url": local_url or (direct_url if direct_url != url else ""),
        "container": str((media_source.get("Container") or "")).lower(),
        "transcoding": bool(transcode_url),
        "hls": ".m3u8" in transcode_url,
        "start_seconds": max(0, int(start_seconds or 0)),
        "play_session_id": str(data.get("PlaySessionId") or ""),
    }


def stop_playback(server_id: str = "", play_session_id: str = "") -> dict:
    """结束一次播放：让 Emby/Jellyfin 立刻停掉转码任务，否则它会一直读到超时。

    - DELETE /Videos/ActiveEncodings 立即停止当前转码（磁盘/CPU 马上降下来）
    - POST /Sessions/Playing/Stopped 正常上报结束，顺带写入播放进度
    """
    targets = [server for server in list_media_servers() if not server_id or str(server.get("id")) == server_id]
    results = []
    for server in targets:
        base = _service_url(server)
        headers = _playback_headers(server)
        params = {"DeviceId": "javsp-web-player"}
        if play_session_id:
            params["PlaySessionId"] = play_session_id
        try:
            response = requests.delete(f"{base}/Videos/ActiveEncodings", headers=headers, params=params, timeout=15)
            results.append({"server_id": server.get("id"), "stopped": response.status_code < 400})
        except requests.RequestException as exc:
            results.append({"server_id": server.get("id"), "stopped": False, "error": str(exc)[:120]})
        if play_session_id:
            try:
                requests.post(f"{base}/Sessions/Playing/Stopped", headers=headers,
                              json={"PlaySessionId": play_session_id, "PositionTicks": 0}, timeout=10)
            except requests.RequestException:
                pass
    return {"ok": True, "results": results}


def connection_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        status = exc.response.status_code
        reason = {401: "认证未通过，请确认密钥属于当前服务器且未被撤销", 403: "密钥没有读取媒体库的权限"}.get(status, "请检查服务地址与权限")
        return f"HTTP {status}：{reason}"
    if isinstance(exc, requests.Timeout):
        return "请求超时，请检查服务地址与网络"
    return "无法读取媒体服务器，请检查地址、网络及返回数据"


def sync_media_server(server: dict) -> dict:
    libraries = [str(value) for value in (server.get("libraries") or []) if str(value).strip()]
    targets = libraries or [None]
    for library_id in targets:
        params = _params(server)
        if library_id:
            params["LibraryId"] = library_id
        response = requests.post(f"{_service_url(server)}/Library/Refresh", params=params, headers=_headers(server), timeout=30)
        response.raise_for_status()
    return {"id": server["id"], "name": server["name"], "ok": True, "message": "媒体库扫描已启动"}


def list_media_libraries(server: dict) -> list[dict]:
    response = requests.get(
        f"{_service_url(server)}/Library/VirtualFolders",
        params=_params(server),
        headers=_headers(server),
        timeout=20,
    )
    if response.status_code in (401, 403) and server.get("type") == "jellyfin":
        response = requests.get(f"{_service_url(server)}/Library/MediaFolders", headers=_headers(server), timeout=20)
    response.raise_for_status()
    libraries = []
    data = response.json() or []
    for item in data.get("Items", []) if isinstance(data, dict) else data:
        if not isinstance(item, dict):
            continue
        libraries.append({"id": str(item.get("ItemId") or item.get("Id") or item.get("Name") or ""), "name": str(item.get("Name") or item.get("CollectionType") or "未命名媒体库"),
                          "locations": list(item.get("Locations") or ([item["Path"]] if item.get("Path") else []))})
    return [item for item in libraries if item["id"]]


_LIBRARY_CACHE: dict[str, tuple[float, list[dict]]] = {}
_LIBRARY_CACHE_SECONDS = 300
_LIBRARY_FAILURE_SECONDS = 60


def _local_roots(root: str, mappings: list[dict]) -> list[str]:
    """Translate a media-server library path into the paths JavSP writes to."""
    normalize = lambda value: str(value or "").replace("\\", "/").rstrip("/")
    root = normalize(root)
    roots = [root] if root else []
    for mapping in mappings:
        remote, local = normalize(mapping.get("source_path")), normalize(mapping.get("target_path"))
        if remote and local and (root.casefold() == remote.casefold() or root.casefold().startswith(remote.casefold() + "/")):
            roots.append(local + root[len(remote):])
    return list(dict.fromkeys(roots))


def overview_libraries() -> list[dict]:
    """Selected media libraries with local folder roots, cached to keep overview polling cheap."""
    mappings = list_path_mappings()
    result = []
    for server in list_media_servers():
        if not server.get("url") or not server.get("api_key"):
            continue
        cache_key = "|".join([str(server.get("id")), str(server.get("url")), str(server.get("api_key")), ",".join(map(str, server.get("libraries") or []))])
        cached = _LIBRARY_CACHE.get(cache_key)
        if cached and cached[0] > time.monotonic():
            libraries = cached[1]
        else:
            try:
                libraries = list_media_libraries(server)
                _LIBRARY_CACHE[cache_key] = (time.monotonic() + _LIBRARY_CACHE_SECONDS, libraries)
            except (OSError, ValueError, requests.RequestException):
                libraries = cached[1] if cached else []
                _LIBRARY_CACHE[cache_key] = (time.monotonic() + _LIBRARY_FAILURE_SECONDS, libraries)
        selected = set(server.get("libraries") or [])
        for library in libraries:
            if selected and library["id"] not in selected:
                continue
            roots = [local for root in library.get("locations") or [] for local in _local_roots(root, mappings)]
            result.append({"key": f"{server['id']}:{library['id']}", "name": library["name"], "server": server.get("name", ""), "roots": roots})
    return result


def playback_links(task: dict, include_targets: bool = False) -> list[dict]:
    """Match output paths to selected libraries, then resolve the server's item page."""
    output = (task.get("progress") or {}).get("output") or {}
    folder = str(output.get("save_dir") or task.get("input_directory") or "")
    identifier = str(((task.get("progress") or {}).get("metadata") or {}).get("dvdid") or "")
    normalize = lambda value: str(value).replace("\\", "/").rstrip("/").casefold()
    paths = [normalize(folder)]
    for value in (task.get('file_organizer') or {}).get('organized_files') or []:
        parent = normalize(value).rsplit('/', 1)[0]
        if parent:
            paths.append(parent)
    for mapping in list_path_mappings():
        local, remote = normalize(mapping.get("target_path")), normalize(mapping.get("source_path"))
        if local and (paths[0] == local or paths[0].startswith(local + "/")):
            paths.append(remote + paths[0][len(local):])
    links = []
    for server in list_media_servers():
        external = urlsplit(str(server.get("external_url") or server.get('url') or "").strip())
        if external.scheme not in ("http", "https") or not external.netloc:
            continue
        base_path = external.path.rstrip("/")
        for suffix in ("/web/index.html", "/web"):
            if base_path.endswith(suffix):
                base_path = base_path[:-len(suffix)]
                break
        base = urlunsplit((external.scheme, external.netloc, base_path, "", ""))
        libraries = list_media_libraries(server)
        selected = set(server.get("libraries") or [])
        matched = [library for library in libraries if (not selected or library["id"] in selected)
                   and any(path == normalize(root) or path.startswith(normalize(root) + "/")
                           for path in paths for root in library.get("locations", []) if root)]
        reference = task.get('media_reference') or {}
        if reference and reference.get('server_id') != server['id']:
            continue
        if not matched and identifier:
            matched = [library for library in libraries if not selected or library['id'] in selected]
        if reference:
            matched = [library for library in libraries if library['id'] == reference.get('library_id') and (not selected or library['id'] in selected)]
        for library in matched:
            if reference:
                items = [{'Id': reference['item_id'], 'Path': folder + '/video'}]
            else:
                response = requests.get(f"{_service_url(server)}/Items", headers=_headers(server),
                                    params={"ParentId": library["id"], "Recursive": "true", "IncludeItemTypes": "Movie", "SearchTerm": identifier,
                                            "Fields": "Path,ProviderIds", "Limit": 100}, timeout=20)
                response.raise_for_status()
                items = response.json().get("Items", [])
            exact = [item for item in items if normalize(item.get("Path") or "").rsplit("/", 1)[0] in paths]
            if not exact and identifier:
                token = re.compile(r"(?<![a-z0-9])" + re.escape(identifier).replace(r'\-', r'[-_ ]?') + r"(?![a-z0-9])", re.I)
                exact = [item for item in items if token.search(str(item.get("Path") or '') + ' ' + str(item.get('Name') or ''))]
            if len(exact) != 1 or not exact[0].get("Id"):
                continue
            info = requests.get(f"{_service_url(server)}/System/Info", headers=_headers(server), timeout=20)
            info.raise_for_status()
            query = urlencode({"id": exact[0]["Id"], "serverId": info.json().get("Id", "")})
            if server.get("type") == "jellyfin":
                url = base + "/web/#/details?" + query
            else:
                url = base + "/web/index.html#!/item?" + query
            link = {"name": server["name"] + " · " + library["name"], "url": url}
            if include_targets:
                link.update({"server_id": server["id"], "session_server_id": info.json().get("Id", ""), "item_id": str(exact[0]["Id"]), "server": server})
            links.append(link)
    if not include_targets:
        return [{key: value for key, value in link.items() if key in {"name", "url"}} for link in links]
    return links


def media_sessions(task: dict) -> list[dict]:
    sessions = []
    seen = set()
    for target in playback_links(task, include_targets=True):
        server = target.pop('server')
        response = requests.get(f"{_service_url(server)}/Sessions", params={"activeWithinSeconds": 900}, headers=_headers(server), timeout=20)
        response.raise_for_status()
        for item in response.json() or []:
            capabilities = item.get('Capabilities') or {}
            if item.get('SupportsRemoteControl') is False or capabilities.get('SupportsMediaControl') is False:
                continue
            playable = capabilities.get('PlayableMediaTypes') or item.get('PlayableMediaTypes') or []
            if playable and 'Video' not in playable:
                continue
            key = (target["server_id"], item.get('Id'))
            if key in seen: continue
            seen.add(key)
            sessions.append({"server_id": target["server_id"], "item_id": target["item_id"], "server_name": target["name"],
                             "session_id": item.get('Id'), "client": item.get('Client') or '', "device_name": item.get('DeviceName') or '',
                             "user_name": item.get('UserName') or '', "last_activity": item.get('LastActivityDate')})
    return [item for item in sessions if item.get('session_id')]


def seek_media(task: dict, server_id: str, session_id: str, seconds: int) -> None:
    targets = [target for target in playback_links(task, include_targets=True) if target['server_id'] == server_id]
    targets = list({(target['server_id'], target['item_id']): target for target in targets}.values())
    if len(targets) != 1:
        raise ValueError('媒体库影片匹配不唯一')
    target = targets[0]; server = target.pop('server')
    sessions = media_sessions(task)
    if not any(item['server_id'] == server_id and item['session_id'] == session_id for item in sessions):
        raise ValueError('播放器会话不存在、已离线或无远程控制权限')
    if seconds < 0 or seconds > 7 * 24 * 3600:
        raise ValueError('播放时间超出允许范围')
    params = {'playCommand': 'PlayNow', 'itemIds': target['item_id'], 'startPositionTicks': seconds * 10_000_000}
    response = requests.post(f"{_service_url(server)}/Sessions/{quote(session_id, safe='')}/Playing", params=params, headers=_headers(server), timeout=20)
    response.raise_for_status()


_FILM_CACHE = {}
_FILM_LOCK = threading.RLock()


def clear_catalog_cache():
    with _FILM_LOCK:
        _FILM_CACHE.clear()
        _LIBRARY_CACHE.clear()


def _movie_summary(server, library, item):
    path = str(item.get('Path') or '').replace('\\', '/')
    title = str(item.get('Name') or '')
    identifier_text = path.rsplit('/', 1)[-1] + ' ' + title + ' ' + path.rsplit('/', 2)[-2] if '/' in path else path + ' ' + title
    match = re.search(r'(?<![A-Za-z0-9])([A-Za-z]{2,12})[-_ ]?(\d{2,8})(?!\d)', identifier_text)
    identifier = str((item.get('ProviderIds') or {}).get('JavSPDVDID') or ((match[1].upper() + '-' + match[2]) if match else ''))
    fc2 = re.search(r'\bFC2[-_ ]*(?:PPV[-_ ]*)?(\d{4,9})\b', identifier_text, re.I)
    if fc2 and not (item.get('ProviderIds') or {}).get('JavSPDVDID'):
        identifier = 'FC2-' + fc2[1]
    metadata = {'dvdid': identifier, 'title': title,
                'actress': [p['Name'] for p in item.get('People') or [] if p.get('Type') == 'Actor' and p.get('Name')],
                'director': ' / '.join(p['Name'] for p in item.get('People') or [] if p.get('Type') == 'Director' and p.get('Name')),
                'producer': ' / '.join(s['Name'] for s in item.get('Studios') or [] if s.get('Name')),
                'publisher': str((item.get('ProviderIds') or {}).get('JavSPPublisher') or ''), 'publish_date': str(item.get('PremiereDate') or item.get('ProductionYear') or '')[:10]}
    key = hashlib.sha256((str(server['id']) + ':' + str(item['Id'])).encode()).hexdigest()[:24]
    return {'id': 'media-' + key, 'name': title, 'title': title, 'file_name': path.rsplit('/', 1)[-1] or title,
            'input_directory': path, 'status': 'succeeded', 'source': 'media', 'task_type': 'media',
            'created_at': item.get('DateCreated') or '', 'updated_at': item.get('DateLastSaved') or '',
            'preset_name': server.get('name', '') + ' · ' + library['name'],
            'cover_count': int(bool((item.get('ImageTags') or {}).get('Primary'))),
            'fanart_count': len(item.get('BackdropImageTags') or []), 'log_entries': [], 'log_tail': [],
            'media_reference': {'server_id': server['id'], 'library_id': library['id'], 'item_id': str(item['Id'])},
            'library_keys': [str(server['id']) + ':' + library['id']],
            'progress': {'metadata': metadata, 'output': {'save_dir': path.rsplit('/', 1)[0] if '/' in path else ''}, 'images': {}}}


def catalog_movies():
    """Read every bound library, including films never scraped by this app."""
    movies, errors = [], []
    with _FILM_LOCK:
        for server in list_media_servers():
            if not server.get('url') or not server.get('api_key'):
                continue
            key = (server['id'], server['url'], server['api_key'], tuple(map(str, server.get('libraries') or [])))
            cached = _FILM_CACHE.get(key)
            if cached and cached[0] > time.monotonic():
                movies.extend(cached[1]); errors.extend(cached[2]); continue
            records, failures = [], []
            try:
                selected = set(map(str, server.get('libraries') or []))
                for library in list_media_libraries(server):
                    if selected and library['id'] not in selected:
                        continue
                    offset = 0
                    while True:
                        response = requests.get(f'{_service_url(server)}/Items', headers=_headers(server), timeout=30,
                            params={'ParentId': library['id'], 'Recursive': 'true', 'IncludeItemTypes': 'Movie',
                                    'Fields': 'Path,People,Studios,DateCreated,DateLastSaved,ProviderIds',
                                    'StartIndex': offset, 'Limit': 1000, 'EnableTotalRecordCount': 'true'})
                        response.raise_for_status()
                        data = response.json()
                        batch = data.get('Items') or []
                        records.extend(_movie_summary(server, library, item) for item in batch if item.get('Id'))
                        offset += len(batch)
                        if not batch or ('TotalRecordCount' in data and offset >= int(data['TotalRecordCount'])) or ('TotalRecordCount' not in data and len(batch) < 1000):
                            break
            except (OSError, ValueError, requests.RequestException) as exc:
                failures.append(server.get('name', '媒体服务器') + '：' + connection_error(exc))
                records = cached[1] if cached else []
            _FILM_CACHE[key] = (time.monotonic() + (60 if failures else 300), records, failures)
            movies.extend(records); errors.extend(failures)
    return movies, errors


def catalog_task(task_id):
    if not task_id.startswith('media-'):
        return None
    return next((item for item in catalog_movies()[0] if item['id'] == task_id), None)


def _catalog_target(task_id):
    task = catalog_task(task_id)
    if not task:
        raise ValueError('媒体库影片不存在或已取消绑定')
    ref = task['media_reference']
    server = next(item for item in list_media_servers() if item['id'] == ref['server_id'])
    return task, server, f"{_service_url(server)}/Items/{quote(ref['item_id'], safe='')}"


def catalog_image(task_id, kind, index=0, content=None, delete=False):
    task, server, base = _catalog_target(task_id)
    role = 'Primary' if kind == 'cover' else 'Backdrop'
    url = base + '/Images/' + role
    if content is None:
        url += '/' + str(index)
    headers = _headers(server)
    if delete:
        response = requests.delete(url, headers=headers, timeout=20)
    elif content is not None:
        headers['Content-Type'] = 'image/jpeg'
        response = requests.post(url + ('/' + str(index) if index >= 0 else ''), headers=headers,
                                 data=content, timeout=30)
    else:
        response = requests.get(url, headers=headers, params={'MaxWidth': 1200}, timeout=20)
    response.raise_for_status()
    if delete or content is not None:
        clear_catalog_cache()
    return response


def update_catalog_metadata(task_id, values):
    task, server, url = _catalog_target(task_id)
    response = requests.get(f'{_service_url(server)}/Items', headers=_headers(server), timeout=20,
                            params={'Ids': task['media_reference']['item_id'], 'Fields': 'People,Studios,ProviderIds,Overview,Genres,Tags,PremiereDate'})
    response.raise_for_status()
    items = response.json().get('Items') or []
    if len(items) != 1:
        raise ValueError('媒体库影片不存在')
    item = items[0]
    people = [p for p in item.get('People') or [] if p.get('Type') not in ('Actor', 'Director')]
    people += [{'Name': name, 'Type': 'Actor'} for name in values.get('actress') or []]
    if values.get('director'):
        people.append({'Name': values['director'], 'Type': 'Director'})
    item.update(Name=values.get('title', ''), People=people,
                Studios=[{'Name': values['producer']}] if values.get('producer') else [],
                PremiereDate=values.get('publish_date') or None)
    item.setdefault('ProviderIds', {}).update(JavSPDVDID=values.get('dvdid') or '', JavSPPublisher=values.get('publisher') or '')
    response = requests.post(url, headers=_headers(server), json=item, timeout=30)
    response.raise_for_status()
    clear_catalog_cache()
    return {'metadata': values, 'nfo_path': ''}


def auto_sync_media_servers() -> None:
    for server in list_media_servers():
        if not server.get("auto_scan"):
            continue
        try:
            delay = max(0, min(86400, int(server.get("auto_scan_delay", 0) or 0)))
            if delay:
                time.sleep(delay)
            sync_media_server(server)
        except (OSError, requests.RequestException):
            continue
