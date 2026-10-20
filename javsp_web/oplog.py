"""统一的运行日志。

服务端默认只把 uvicorn 的日志输出到控制台，应用自己的日志需要一个 handler。
这里集中配置 `javsp_web` 命名空间，并按用途给出子 logger：

* ``javsp_web.playback`` 播放（本地直连 / 转码 / 停止）
* ``javsp_web.scrape``   刮削任务（开始 / 结束 / 失败原因）
"""
from __future__ import annotations

import logging
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_CONFIGURED = False


def configure(level: int = logging.INFO) -> None:
    """给 javsp_web 命名空间挂一个控制台 handler（只做一次）。"""
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("javsp_web")
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    _CONFIGURED = True


def get(name: str) -> logging.Logger:
    """取得 ``javsp_web.<name>`` 子 logger（首次调用时完成配置）。"""
    configure()
    return logging.getLogger(f"javsp_web.{name}")
