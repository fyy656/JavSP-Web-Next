"""本地直连播放的 Range 流。

Starlette 的 `FileResponse` 在客户端断开时依赖 send 抛异常收尾，Windows + Proactor 下
经常收不干净（磁盘/网络继续跑、日志里刷 WinError 10054）。这里自己做分段读取：

* 支持单段 Range（`bytes=a-b` / `a-` / `-n`），返回 206 与 `Content-Range`
* 每读一段前检查 `request.is_disconnected()`，客户端一断就立刻停止读盘
* 无论正常结束、客户端断开还是写入失败，都会关闭文件并记录发送量
"""
from __future__ import annotations

import logging
from pathlib import Path

import anyio
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse

logger = logging.getLogger("javsp_web.library")
if not logger.handlers:
    # uvicorn 的日志配置不会带上应用自己的 logger，这里单独挂一个控制台 handler
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

CHUNK_SIZE = 256 * 1024

# 每部影片同一时刻只保留一条流；关闭播放器时通过 stop_stream 立即叫停
_ACTIVE_STREAMS: dict[str, "asyncio.Event"] = {}


def _stream_event(key: str):
    import asyncio

    event = _ACTIVE_STREAMS.get(key)
    if event is None:
        event = asyncio.Event()
        _ACTIVE_STREAMS[key] = event
    return event


def stop_stream(key: str) -> bool:
    """叫停某部影片正在进行的本地直连流，返回是否真的停了一条。"""
    event = _ACTIVE_STREAMS.pop(key, None)
    if event is None:
        return False
    event.set()
    logger.info("已叫停本地直连播放: %s", key)
    return True
_MEDIA_TYPES = {
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
    ".mkv": "video/x-matroska", ".avi": "video/x-msvideo", ".wmv": "video/x-ms-wmv",
    ".asf": "video/x-ms-asf", ".mpg": "video/mpeg", ".mpeg": "video/mpeg",
    ".m2ts": "video/mp2t", ".mts": "video/mp2t", ".ts": "video/mp2t",
    ".flv": "video/x-flv", ".rmvb": "application/vnd.rn-realmedia-vbr", ".rm": "application/vnd.rn-realmedia",
    ".3gp": "video/3gpp", ".ogv": "video/ogg", ".divx": "video/x-msvideo", ".vob": "video/mpeg",
}


def media_type_for(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """解析单段 Range；返回 (start, end) 闭区间，无法满足时返回 None。"""
    value = (header or "").strip()
    if not value.lower().startswith("bytes="):
        return None
    spec = value[6:].split(",")[0].strip()
    if "-" not in spec:
        return None
    start_text, _, end_text = spec.partition("-")
    try:
        if start_text:
            start = int(start_text)
            end = int(end_text) if end_text else size - 1
        else:
            length = int(end_text)
            if length <= 0:
                return None
            start = max(0, size - length)
            end = size - 1
    except ValueError:
        return None
    if start < 0 or start >= size:
        return None
    end = min(end, size - 1)
    if end < start:
        return None
    return start, end


def file_stream_response(request: Request, path: Path, tag: str = "") -> StreamingResponse:
    """按 Range 返回文件流，客户端断开时立即停止读盘。"""
    size = path.stat().st_size
    start, end = 0, size - 1
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, max-age=0",
    }
    parsed = parse_range(request.headers.get("range", ""), size) if request.headers.get("range") else None
    if request.headers.get("range") and parsed is None:
        return PlainTextResponse("Range 无效", status_code=416,
                                 headers={"Content-Range": f"bytes */{size}"})
    if parsed is not None:
        start, end = parsed
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    length = end - start + 1
    headers["Content-Length"] = str(length)

    import asyncio

    cancel_event = _stream_event(tag or str(path))

    async def stream():
        sent = 0
        reason = "正常结束"
        handle = await anyio.to_thread.run_sync(path.open, "rb")
        try:
            await anyio.to_thread.run_sync(handle.seek, start)
            remaining = length
            while remaining > 0:
                if cancel_event.is_set():
                    reason = "收到停止请求"
                    break
                if await request.is_disconnected():
                    reason = "客户端断开"
                    break
                data = await anyio.to_thread.run_sync(handle.read, min(CHUNK_SIZE, remaining))
                if not data:
                    reason = "文件结束"
                    break
                remaining -= len(data)
                sent += len(data)
                yield data
        except (ConnectionResetError, ConnectionAbortedError, OSError) as exc:
            reason = f"写入中断（{type(exc).__name__}）"
        finally:
            await anyio.to_thread.run_sync(handle.close)
            if _ACTIVE_STREAMS.get(tag or str(path)) is cancel_event:
                _ACTIVE_STREAMS.pop(tag or str(path), None)
            logger.info("本地直连播放结束: %s 发送 %.1f MB（%s）", tag, sent / 1024 / 1024, reason)

    logger.info("开始本地直连播放: %s Range=%s（%s 字节）", tag, request.headers.get("range", "无"), length)
    return StreamingResponse(stream(), status_code=status_code, headers=headers,
                             media_type=media_type_for(path))
