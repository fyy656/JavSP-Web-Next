# 第三方组件与来源声明

本项目（`JavSP-Web-Next`）是上游开源项目的衍生修改版本，包含并修改了下列项目的代码与资源。所有原始著作权、许可证与署名均予保留。

## 衍生的上游项目

### JavSP（刮削核心，位于 `vendor/JavSP/`）

- 上游地址：<https://github.com/Yuukiy/JavSP>
- 版权：Copyright (C) Yuukiy 及 JavSP 贡献者
- 许可证：GNU General Public License v3.0（见 `vendor/JavSP/LICENSE`）
- 本项目对其做的修改：增加按任务注入番号（`JAVSP_FORCED_AVID`）的入口，其余行为沿用上游实现。

### JavSP-Web（网页端）

- 上游地址：<https://github.com/APecme/JavSP-Web>
- 版权：Copyright (C) APecme 及 JavSP-Web 贡献者
- 许可证：GNU General Public License v3.0
- 本项目对其做的修改：媒体库索引与批量刮削、失败任务重试、分类浏览与统计、NFO 统一与字段补齐、界面与文档调整、性能优化等。

## 主要第三方依赖

以下组件通过 `requirements.txt` 或前端直接引入（许可证以各自仓库为准）：

- FastAPI / Starlette / Uvicorn —— MIT / BSD
- lxml —— BSD
- Pillow —— MIT-CMU
- PyYAML —— MIT
- requests —— Apache-2.0
- cloudscraper —— MIT
- 前端无框架，图标为内联 SVG；图标风格参考 Material Symbols（Apache-2.0）
- ArtPlayer（`javsp_web/web/assets/artplayer.min.js`，v5.2.5）—— MIT，版权归 ArtPlayer 作者及贡献者所有，用于媒体库内置播放器，文件随仓库分发、不联网加载
- hls.js（`javsp_web/web/assets/hls.min.js`，v1.5.17）—— Apache-2.0，版权归 hls.js 贡献者所有，用于播放媒体服务器转码出的 HLS 流（可拖动进度条）

## 使用与分发提示

- 本项目整体按 GPL-3.0 分发。分发修改后的版本时，请同时保留本文件、`LICENSE` 以及 `vendor/JavSP/LICENSE`。
- 不得暗示本项目是上游项目的官方版本，也不得声称获得上游作者的背书。
- 上游项目名称与商标归各自所有者，本项目仅用于说明来源。
