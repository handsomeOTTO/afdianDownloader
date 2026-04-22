# Afdian Downloader Web

用于下载与整理音频的本地 Web 工具，当前包含：

- 爱发电专辑/帖子下载（含历史记录）
- 播客 RSS 下载
- 信息编辑（音频标签、封面预览与替换）

## 运行环境

- Python 3.10+
- 可选：`ffmpeg`（用于部分格式转换）

## 快速启动

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python main.py
```

默认访问：`http://127.0.0.1:5000`

## 目录说明

- `main.py`: 兼容入口（加载 `src` 并启动 Web）
- `src/afdian_downloader/web.py`: Flask 主逻辑（下载、历史、信息编辑 API）
- `src/afdian_downloader/app_logger.py`: 日志初始化
- `templates/index.html`: 前端页面
- `data/downloads.db`: 下载记录数据库
- `media/downloads/`: 下载输出目录

## 合规与使用声明

- 本项目仅用于你拥有合法访问权限的内容管理与离线收听。
- 请遵守内容平台服务条款、版权法及你所在地法律法规。
- 付费内容、会员内容、私有内容等请确保已获得授权后再下载与使用。
- 开源代码许可不等于内容授权，媒体内容的版权归原权利人所有。

## 部署安全说明

- 默认仅监听 `127.0.0.1:5000`，用于本机访问。
- 不建议直接监听 `0.0.0.0` 或将服务直接暴露到公网。
- 如因自行公网暴露导致未授权访问、文件被删改、数据泄露等风险，由部署者自行承担。
- 若必须远程访问，请至少增加访问鉴权，并通过反向代理做访问控制（IP 白名单、TLS、限流等）。

## 第三方来源与许可

本项目部分早期实现参考了开源项目：

- `senventise/afdian_podcast_down`  
  <https://github.com/senventise/afdian_podcast_down>  
  License: MIT

详细信息见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
