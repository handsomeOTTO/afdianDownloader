import os
import sqlite3
import subprocess
import threading
import time
import uuid
import base64
from datetime import datetime
from pathlib import Path
from random import randint, random
from typing import Any, Callable

import eyed3
import feedparser
import requests
from flask import Flask, abort, flash, jsonify, make_response, redirect, render_template, request, send_file, url_for

from .app_logger import logger, setup_logging

PROJECT_ROOT = Path(__file__).resolve().parents[2]

AFDIAN_DOMAIN = "ifdian.net"
DEFAULT_SLEEP = 2
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "media" / "downloads"
DB_PATH = PROJECT_ROOT / "data" / "downloads.db"
TAG_EDITOR_TEMP_DIR = PROJECT_ROOT / "data" / "tag_editor_temp"
JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()

HEADERS = {
    "authority": AFDIAN_DOMAIN,
    "accept-language": "zh-CN,zh;q=0.9,en;q=0.8,zh-TW;q=0.7",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "referer": f"https://{AFDIAN_DOMAIN}/",
    "sec-ch-ua": '" Not A;Brand";v="99", "Chromium";v="100"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
}

app = Flask(
    __name__,
    template_folder=str(PROJECT_ROOT / "templates"),
    static_folder=str(PROJECT_ROOT / "static"),
)
app.secret_key = os.environ.get("APP_SECRET_KEY", "afdian-downloader-dev-key")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            author TEXT,
            album_name TEXT,
            description TEXT,
            source_type TEXT,
            source_id TEXT,
            audio_url TEXT,
            file_path TEXT,
            file_size INTEGER,
            status TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()
    logger.info("Database initialized at {}", DB_PATH)


def insert_record(
    *,
    title: str,
    author: str,
    album_name: str | None,
    description: str,
    source_type: str,
    source_id: str,
    audio_url: str,
    file_path: str | None,
    file_size: int | None,
    status: str,
    error_message: str | None,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO downloads (
            title, author, album_name, description, source_type, source_id,
            audio_url, file_path, file_size, status, error_message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            author,
            album_name,
            description,
            source_type,
            source_id,
            audio_url,
            file_path,
            file_size,
            status,
            error_message,
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ),
    )
    conn.commit()
    conn.close()


def query_recent_records(limit: int = 120) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, title, author, album_name, source_type, source_id, file_path,
               file_size, status, error_message, created_at
        FROM downloads
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    conn.close()

    results: list[dict[str, Any]] = []
    for row in rows:
        record = dict(row)
        file_path_raw = (record.get("file_path") or "").strip()
        local_file_exists = False
        if file_path_raw:
            file_path = Path(file_path_raw)
            local_file_exists = file_path.exists() and file_path.is_file()
        record["local_file_exists"] = local_file_exists
        results.append(record)
    return results


def query_record_by_id(record_id: int) -> dict[str, Any] | None:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT id, title, file_path, status
        FROM downloads
        WHERE id = ?
        """,
        (record_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return dict(row)


def sanitize_filename(filename: str) -> str:
    illegal_chars = '<>:"/\\|?*'
    for char in illegal_chars:
        filename = filename.replace(char, "_")
    filename = filename.strip(". ")
    return filename or "untitled.mp3"


class AfdianClient:
    def __init__(self, auth_token: str):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.session.cookies.set("auth_token", auth_token)

    def get_album_posts(self, album_id: str, last_rank: int, rank_order: str, rank_field: str) -> dict[str, Any]:
        resp = self.session.get(
            f"https://{AFDIAN_DOMAIN}/api/user/get-album-post",
            params={
                "album_id": album_id,
                "lastRank": last_rank,
                "rankOrder": rank_order,
                "rankField": rank_field,
            },
            timeout=25,
        )
        resp.raise_for_status()
        return resp.json()["data"]

    def get_post_detail(self, post_id: str) -> dict[str, Any]:
        resp = self.session.get(
            f"https://{AFDIAN_DOMAIN}/api/post/get-detail",
            params={"post_id": post_id},
            timeout=25,
        )
        resp.raise_for_status()
        return resp.json()["data"]["post"]

    def get_bytes(self, url: str) -> bytes:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def stream_download(
        self,
        url: str,
        target_path: Path,
        progress_callback: Callable[[int, int | None], None] | None = None,
    ) -> int:
        with self.session.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            total_size = int(resp.headers.get("Content-Length", "0") or "0")
            total_size = total_size if total_size > 0 else None
            downloaded = 0
            with target_path.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_callback:
                            progress_callback(downloaded, total_size)
        return target_path.stat().st_size


def set_job(job_id: str, **updates: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(updates)


def append_job_log(job_id: str, message: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        logs = job.setdefault("logs", [])
        logs.append(message)
        if len(logs) > 120:
            del logs[:-120]


def safe_parse_post_ids(raw: str) -> list[str]:
    tokens = []
    for piece in raw.replace("\n", ",").split(","):
        value = piece.strip()
        if value:
            tokens.append(value)
    return tokens


def detect_image_mime(image_bytes: bytes, source_url: str | None = None) -> str:
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"

    url_path = (source_url or "").split("?", 1)[0].lower()
    if url_path.endswith(".png"):
        return "image/png"
    if url_path.endswith(".gif"):
        return "image/gif"
    if url_path.endswith(".webp"):
        return "image/webp"
    return "image/jpeg"


def extract_afdian_cover_url(post: dict[str, Any]) -> str:
    candidate_keys = (
        "audio_thumb",
        "cover",
        "cover_url",
        "thumb",
    )
    for key in candidate_keys:
        value = (post.get(key) or "").strip()
        if value:
            return value

    audio_info = post.get("audio_info") or {}
    for key in ("audio_thumb", "cover", "cover_url"):
        value = (audio_info.get(key) or "").strip()
        if value:
            return value

    return ""


def load_audio_tag_info(file_path: Path) -> dict[str, Any]:
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError("音频文件不存在。")

    audio = eyed3.load(str(file_path))
    if audio is None or audio.tag is None:
        raise RuntimeError("无法读取音频标签。")

    year = ""
    if audio.tag.recording_date and audio.tag.recording_date.year:
        year = str(audio.tag.recording_date.year)

    description = ""
    if audio.tag.comments:
        description = audio.tag.comments[0].text or ""

    cover_data_url = ""
    if audio.tag.images:
        image = audio.tag.images[0]
        image_bytes = image.image_data or b""
        if image_bytes:
            mime_type = image.mime_type or detect_image_mime(image_bytes)
            encoded = base64.b64encode(image_bytes).decode("ascii")
            cover_data_url = f"data:{mime_type};base64,{encoded}"

    return {
        "file_path": str(file_path),
        "title": audio.tag.title or "",
        "artist": audio.tag.artist or "",
        "album": audio.tag.album or "",
        "year": year,
        "description": description,
        "cover_data_url": cover_data_url,
    }


def save_audio_tag_info(
    *,
    file_path: Path,
    title: str,
    artist: str,
    album: str,
    year: str,
    description: str,
    cover_file_bytes: bytes | None = None,
    cover_file_mime: str | None = None,
) -> None:
    if not file_path.exists() or not file_path.is_file():
        raise FileNotFoundError("音频文件不存在。")

    audio = eyed3.load(str(file_path))
    if audio is None:
        raise RuntimeError("无法加载音频文件。")
    if audio.tag is None:
        audio.initTag()

    audio.tag.title = title
    audio.tag.artist = artist
    audio.tag.album = album
    audio.tag.comments.set(description or "")

    year_value = (year or "").strip()
    if year_value:
        audio.tag.recording_date = eyed3.core.Date(int(year_value))
    else:
        audio.tag.recording_date = None

    if cover_file_bytes:
        audio.tag.images.set(3, cover_file_bytes, cover_file_mime or detect_image_mime(cover_file_bytes))

    audio.tag.save()


def apply_id3_tags(
    file_path: Path,
    title: str,
    author: str,
    album_name: str,
    description: str,
    cover: bytes | None,
    cover_mime: str | None,
) -> None:
    audio = eyed3.load(str(file_path))
    if audio is None and file_path.exists():
        ffmpeg = shutil_which("ffmpeg")
        if ffmpeg:
            output = file_path.with_suffix(".fixed.mp3")
            result = subprocess.run(
                [ffmpeg, "-y", "-i", str(file_path), str(output)],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and output.exists():
                file_path.unlink(missing_ok=True)
                output.rename(file_path)
                audio = eyed3.load(str(file_path))
    if audio is None:
        raise RuntimeError("Unable to load audio for tag writing.")

    if audio.tag is None:
        audio.initTag()
    audio.tag.artist = author
    audio.tag.title = title
    audio.tag.album = album_name
    audio.tag.comments.set(description or "")
    if cover:
        audio.tag.images.set(3, cover, cover_mime or "image/jpeg")
    audio.tag.save()


def apply_rss_id3_tags(
    file_path: Path,
    title: str,
    author: str,
    album_name: str,
    summary: str,
    track_number: int,
    cover: bytes | None,
    cover_mime: str | None,
) -> None:
    audio = eyed3.load(str(file_path))
    if audio is None:
        raise RuntimeError("Unable to load audio for tag writing.")

    if audio.tag is None:
        audio.initTag()
    audio.tag.artist = author
    audio.tag.title = title
    audio.tag.album = album_name
    audio.tag.track_num = track_number
    audio.tag.comments.set(summary or "")
    if cover:
        audio.tag.images.set(3, cover, cover_mime or "image/jpeg")
    audio.tag.save()


def shutil_which(command: str) -> str | None:
    for path in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(path) / command
        if candidate.exists():
            return str(candidate)
        candidate_exe = Path(path) / f"{command}.exe"
        if candidate_exe.exists():
            return str(candidate_exe)
    return None


def process_post(
    *,
    post: dict[str, Any],
    output_dir: Path,
    album_name: str | None,
    list_only: bool,
    source_type: str,
    source_id: str,
    client: AfdianClient,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> tuple[str, str]:
    title = post.get("title", "").strip() or "untitled"
    author = (post.get("user") or {}).get("name", "unknown")
    description = post.get("content", "") or ""
    audio_url = (post.get("audio") or "").strip()
    cover_url = extract_afdian_cover_url(post)
    final_album_name = album_name or title

    if not audio_url:
        logger.warning("Post skipped (no audio): {} ({})", title, source_id)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=description,
            source_type=source_type,
            source_id=source_id,
            audio_url=audio_url,
            file_path=None,
            file_size=None,
            status="skipped_no_audio",
            error_message="No audio URL in post.",
        )
        return "skipped_no_audio", f"Skip: {title}"

    if list_only:
        logger.info("Post listed only: {} ({})", title, source_id)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=description,
            source_type=source_type,
            source_id=source_id,
            audio_url=audio_url,
            file_path=None,
            file_size=None,
            status="listed",
            error_message=None,
        )
        return "listed", f"Listed: {title}"

    file_name = sanitize_filename(f"{title}.mp3")
    file_path = output_dir / file_name

    if file_path.exists():
        logger.info("Post skipped (exists): {}", file_path)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=description,
            source_type=source_type,
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
            status="skipped_exists",
            error_message=None,
        )
        return "skipped_exists", f"Exists: {title}"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_size = client.stream_download(audio_url, file_path, progress_callback=progress_callback)
        cover = None
        cover_mime = None
        if cover_url:
            try:
                cover = client.get_bytes(cover_url)
                cover_mime = detect_image_mime(cover, cover_url)
            except Exception:
                logger.warning("Cover download failed: {} ({})", cover_url, source_id)
                cover = None
                cover_mime = None
        apply_id3_tags(file_path, title, author, final_album_name, description, cover, cover_mime)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=description,
            source_type=source_type,
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=file_size,
            status="downloaded",
            error_message=None,
        )
        logger.info("Post downloaded: {} -> {}", title, file_path)
        return "downloaded", f"Downloaded: {title}"
    except Exception:
        logger.exception("Post download failed: {} ({})", title, source_id)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=description,
            source_type=source_type,
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=None,
            status="failed",
            error_message="download failed, check logs/app.log",
        )
        return "failed", f"Failed: {title}"


def extract_rss_audio_url(entry: dict[str, Any]) -> str:
    links = entry.get("links") or []
    for link in links:
        href = (link.get("href") or "").strip()
        link_type = (link.get("type") or "").lower()
        if href and (link_type.startswith("audio/") or link.get("rel") == "enclosure"):
            return href
    return ""


def extract_rss_cover_url(entry: dict[str, Any]) -> str:
    image = entry.get("image") or {}
    href = (image.get("href") or "").strip()
    if href:
        return href
    itunes_image = entry.get("itunes_image") or {}
    href = (itunes_image.get("href") or "").strip()
    if href:
        return href
    return ""


def stream_download_with_requests(
    url: str,
    target_path: Path,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> int:
    with requests.get(url, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total_size = int(resp.headers.get("Content-Length", "0") or "0")
        total_size = total_size if total_size > 0 else None
        downloaded = 0
        with target_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)
    return target_path.stat().st_size


def process_rss_entry(
    *,
    entry: dict[str, Any],
    output_dir: Path,
    album_name: str | None,
    author_name: str | None,
    list_only: bool,
    source_id: str,
    track_number: int,
    progress_callback: Callable[[int, int | None], None] | None = None,
) -> tuple[str, str]:
    title = (entry.get("title") or "").strip() or f"episode_{track_number}"
    author = (author_name or entry.get("author") or "unknown").strip()
    summary = (entry.get("summary") or "").strip()
    final_album_name = (album_name or entry.get("itunes_season") or "RSS Album").strip()
    audio_url = extract_rss_audio_url(entry)
    cover_url = extract_rss_cover_url(entry)

    if not audio_url:
        logger.warning("RSS entry skipped (no audio): {} ({})", title, source_id)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=summary,
            source_type="rss",
            source_id=source_id,
            audio_url=audio_url,
            file_path=None,
            file_size=None,
            status="skipped_no_audio",
            error_message="No audio URL in RSS entry.",
        )
        return "skipped_no_audio", f"Skip: {title}"

    if list_only:
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=summary,
            source_type="rss",
            source_id=source_id,
            audio_url=audio_url,
            file_path=None,
            file_size=None,
            status="listed",
            error_message=None,
        )
        return "listed", f"Listed: {title}"

    file_name = sanitize_filename(f"{title}.mp3")
    file_path = output_dir / file_name

    if file_path.exists():
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=summary,
            source_type="rss",
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=file_path.stat().st_size,
            status="skipped_exists",
            error_message=None,
        )
        return "skipped_exists", f"Exists: {title}"

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        file_size = stream_download_with_requests(audio_url, file_path, progress_callback=progress_callback)
        cover = None
        cover_mime = None
        if cover_url:
            try:
                cover = requests.get(cover_url, timeout=30).content
                cover_mime = detect_image_mime(cover, cover_url)
            except Exception:
                logger.warning("RSS cover download failed: {} ({})", cover_url, source_id)
                cover = None
                cover_mime = None
        apply_rss_id3_tags(
            file_path=file_path,
            title=title,
            author=author,
            album_name=final_album_name,
            summary=summary,
            track_number=track_number,
            cover=cover,
            cover_mime=cover_mime,
        )
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=summary,
            source_type="rss",
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=file_size,
            status="downloaded",
            error_message=None,
        )
        return "downloaded", f"Downloaded: {title}"
    except Exception:
        logger.exception("RSS entry download failed: {} ({})", title, source_id)
        insert_record(
            title=title,
            author=author,
            album_name=final_album_name,
            description=summary,
            source_type="rss",
            source_id=source_id,
            audio_url=audio_url,
            file_path=str(file_path),
            file_size=None,
            status="failed",
            error_message="download failed, check logs/app.log",
        )
        return "failed", f"Failed: {title}"


def run_rss_job(
    *,
    rss_url: str,
    latest_n: int,
    output_dir: str,
    album_name: str | None,
    author_name: str | None,
    list_only: bool,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if not rss_url:
        raise ValueError("rss_url is required in RSS mode.")
    if latest_n <= 0:
        raise ValueError("latest_n must be > 0 in RSS mode.")

    logger.info(
        "Run RSS job start: rss_url={}, latest_n={}, list_only={}, output_dir={}, album_name={}",
        rss_url,
        latest_n,
        list_only,
        output_dir,
        album_name,
    )

    output_path = Path(output_dir or str(DEFAULT_OUTPUT_DIR)).resolve()
    if album_name:
        output_path = output_path / sanitize_filename(album_name)

    feed = feedparser.parse(rss_url)
    entries = list(feed.entries or [])
    entries = entries[:latest_n]

    logs: list[str] = []
    stats = {
        "downloaded": 0,
        "failed": 0,
        "listed": 0,
        "skipped_no_audio": 0,
        "skipped_exists": 0,
    }

    total = len(entries)
    if status_callback:
        status_callback({"total": total, "processed": 0, "percent": 0.0, "current": ""})

    for index, entry in enumerate(entries, start=1):
        title = (entry.get("title") or "").strip() or f"episode_{index}"
        if status_callback:
            status_callback(
                {
                    "current": title,
                    "current_file_bytes": 0,
                    "current_file_total": None,
                    "current_file_percent": 0.0,
                }
            )

        def on_file_progress(downloaded: int, total_size: int | None) -> None:
            if not status_callback or list_only:
                return
            percent = (downloaded / total_size * 100.0) if total_size else 0.0
            status_callback(
                {
                    "current_file_bytes": downloaded,
                    "current_file_total": total_size,
                    "current_file_percent": percent,
                }
            )

        status, message = process_rss_entry(
            entry=entry,
            output_dir=output_path,
            album_name=album_name,
            author_name=author_name,
            list_only=list_only,
            source_id=rss_url,
            track_number=index,
            progress_callback=on_file_progress,
        )

        stats[status] = stats.get(status, 0) + 1
        logs.append(message)
        if status_callback:
            percent = (index / total * 100.0) if total else 100.0
            status_callback(
                {
                    "processed": index,
                    "total": total,
                    "percent": percent,
                    "last_message": message,
                    "stats": stats.copy(),
                    "current_file_percent": 100.0 if status != "failed" else 0.0,
                }
            )
        time.sleep(random() + (0 if list_only else 1))

    logger.info("Run RSS job done: {}", stats)
    return {"stats": stats, "logs": logs, "output_dir": str(output_path)}


def run_job(
    *,
    auth_token: str,
    mode: str,
    album_id: str,
    latest_n: int,
    post_ids_text: str,
    album_name: str | None,
    list_only: bool,
    output_dir: str,
    status_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    logger.info(
        "Run job start: mode={}, album_id={}, latest_n={}, list_only={}, output_dir={}, album_name={}",
        mode,
        album_id,
        latest_n,
        list_only,
        output_dir,
        album_name,
    )
    client = AfdianClient(auth_token)
    output_path = Path(output_dir or str(DEFAULT_OUTPUT_DIR)).resolve()
    if album_name:
        output_path = output_path / sanitize_filename(album_name)

    logs: list[str] = []
    stats = {
        "downloaded": 0,
        "failed": 0,
        "listed": 0,
        "skipped_no_audio": 0,
        "skipped_exists": 0,
    }

    tasks: list[tuple[dict[str, Any], str, str]] = []
    if mode == "all":
        if not album_id:
            raise ValueError("album_id is required in all mode.")
        last_rank = 0
        while True:
            data = client.get_album_posts(album_id, last_rank, "asc", "rank")
            posts = data.get("list") or []
            for post in posts:
                tasks.append((post, "album", album_id))
            if data.get("has_more") == 0:
                break
            if posts:
                last_rank = posts[-1].get("rank", last_rank)
            else:
                break
    elif mode == "latest":
        if not album_id:
            raise ValueError("album_id is required in latest mode.")
        if latest_n <= 0:
            raise ValueError("latest_n must be > 0.")
        posts: list[dict[str, Any]] = []
        has_more = True
        last_rank = 0
        while len(posts) < latest_n and has_more:
            data = client.get_album_posts(album_id, last_rank, "desc", "publish_sn")
            fetched = data.get("list") or []
            posts.extend(fetched)
            has_more = data.get("has_more") == 1
            if posts:
                last_rank = posts[-1].get("rank", 0)
            else:
                break
            time.sleep(random())
        for post in posts[:latest_n]:
            tasks.append((post, "album", album_id))
    elif mode == "posts":
        post_ids = safe_parse_post_ids(post_ids_text)
        if not post_ids:
            raise ValueError("post_ids is required in posts mode.")
        for post_id in post_ids:
            post = client.get_post_detail(post_id)
            tasks.append((post, "post", post_id))
    else:
        raise ValueError(f"Unknown mode: {mode}")

    total = len(tasks)
    if status_callback:
        status_callback({"total": total, "processed": 0, "percent": 0.0, "current": ""})

    for index, (post, source_type, source_id) in enumerate(tasks, start=1):
        title = post.get("title", "").strip() or "untitled"
        if status_callback:
            status_callback(
                {
                    "current": title,
                    "current_file_bytes": 0,
                    "current_file_total": None,
                    "current_file_percent": 0.0,
                }
            )

        def on_file_progress(downloaded: int, total_size: int | None) -> None:
            if not status_callback or list_only:
                return
            percent = (downloaded / total_size * 100.0) if total_size else 0.0
            status_callback(
                {
                    "current_file_bytes": downloaded,
                    "current_file_total": total_size,
                    "current_file_percent": percent,
                }
            )

        status, message = process_post(
            post=post,
            output_dir=output_path,
            album_name=album_name,
            list_only=list_only,
            source_type=source_type,
            source_id=source_id,
            client=client,
            progress_callback=on_file_progress,
        )

        stats[status] = stats.get(status, 0) + 1
        logs.append(message)

        if status_callback:
            percent = (index / total * 100.0) if total else 100.0
            status_callback(
                {
                    "processed": index,
                    "total": total,
                    "percent": percent,
                    "last_message": message,
                    "stats": stats.copy(),
                    "current_file_percent": 100.0 if status != "failed" else 0.0,
                }
            )

        time.sleep(random() + (0 if list_only else DEFAULT_SLEEP + randint(0, 1)))

    logger.info("Run job done: {}", stats)
    return {"stats": stats, "logs": logs, "output_dir": str(output_path)}


@app.route("/", methods=["GET"])
def index():
    records = query_recent_records()
    auth_token = (request.cookies.get("auth_token") or "").strip()
    active_job_id = (request.args.get("job_id") or "").strip()
    active_source = (request.args.get("source") or "afdian").strip()
    if active_source not in {"afdian", "rss", "editor"}:
        active_source = "afdian"
    return render_template(
        "index.html",
        records=records,
        auth_token=auth_token,
        active_job_id=active_job_id,
        active_source=active_source,
    )


@app.route("/run", methods=["POST"])
def run():
    provider = (request.form.get("provider") or "afdian").strip()
    auth_token = (request.form.get("auth_token") or "").strip()
    mode = (request.form.get("mode") or "latest").strip()
    album_id = (request.form.get("album_id") or "").strip()
    post_ids_text = request.form.get("post_ids") or ""
    album_name_raw = (request.form.get("album_name") or "").strip()
    album_name = album_name_raw or None
    output_dir = (request.form.get("output_dir") or str(DEFAULT_OUTPUT_DIR)).strip()
    list_only = request.form.get("list_only") == "on"
    rss_url = (request.form.get("rss_url") or "").strip()
    rss_latest_n_raw = (request.form.get("rss_latest_n") or "1").strip()
    rss_author_name = (request.form.get("rss_author_name") or "").strip() or None

    if provider == "afdian" and not auth_token:
        flash("auth_token 不能为空。", "error")
        return redirect(url_for("index", source="afdian"))
    if not album_name_raw:
        flash("album_name 不能为空。", "error")
        return redirect(url_for("index", source=provider))

    try:
        post_ids = safe_parse_post_ids(post_ids_text)
        if provider == "afdian":
            if post_ids:
                if album_id:
                    flash("已填写帖子 ID，专辑 ID 将被忽略。", "info")
                mode = "posts"
                post_ids_text = ",".join(post_ids)
            else:
                if not album_id:
                    flash("请填写专辑 ID，或改为填写帖子 ID。", "error")
                    return redirect(url_for("index", source="afdian"))
                if mode not in {"latest", "all"}:
                    mode = "latest"

        latest_n = int((request.form.get("latest_n") or "1").strip() or "1")
        rss_latest_n = int(rss_latest_n_raw or "1")
        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "id": job_id,
                "status": "running",
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "total": 0,
                "processed": 0,
                "percent": 0.0,
                "current": "",
                "current_file_bytes": 0,
                "current_file_total": None,
                "current_file_percent": 0.0,
                "stats": {
                    "downloaded": 0,
                    "failed": 0,
                    "listed": 0,
                    "skipped_no_audio": 0,
                    "skipped_exists": 0,
                },
                "last_message": "",
                "logs": [],
                "output_dir": "",
                "error": "",
            }

        def on_status(update: dict[str, Any]) -> None:
            set_job(job_id, **update)
            if "last_message" in update and update["last_message"]:
                append_job_log(job_id, str(update["last_message"]))

        def background_job() -> None:
            try:
                if provider == "rss":
                    result = run_rss_job(
                        rss_url=rss_url,
                        latest_n=rss_latest_n,
                        output_dir=output_dir,
                        album_name=album_name,
                        author_name=rss_author_name,
                        list_only=list_only,
                        status_callback=on_status,
                    )
                else:
                    result = run_job(
                        auth_token=auth_token,
                        mode=mode,
                        album_id=album_id,
                        latest_n=latest_n,
                        post_ids_text=post_ids_text,
                        album_name=album_name,
                        list_only=list_only,
                        output_dir=output_dir,
                        status_callback=on_status,
                    )
                set_job(
                    job_id,
                    status="completed",
                    percent=100.0,
                    current="",
                    output_dir=result["output_dir"],
                    stats=result["stats"],
                )
                for line in result["logs"][-20:]:
                    append_job_log(job_id, line)
            except Exception as exc:
                logger.exception("Run job failed")
                set_job(job_id, status="failed", error=str(exc), current="")

        worker = threading.Thread(target=background_job, daemon=True)
        worker.start()

        flash("任务已启动，可在页面下方查看进度。", "info")
        response = make_response(redirect(url_for("index", job_id=job_id, source=provider)))
        if provider == "afdian":
            response.set_cookie("auth_token", auth_token, max_age=60 * 60 * 24 * 30, httponly=False)
        return response
    except Exception as exc:
        logger.exception("Run job failed")
        flash(f"执行失败: {exc}", "error")
    return redirect(url_for("index", source=provider))


@app.route("/job/<job_id>", methods=["GET"])
def get_job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "job not found"}), 404
        return jsonify({"ok": True, "job": job})


@app.route("/tag-editor/load", methods=["POST"])
def load_tag_editor_info():
    file_path_raw = (request.form.get("file_path") or "").strip()
    if not file_path_raw:
        return jsonify({"ok": False, "error": "file_path 不能为空。"}), 400

    try:
        payload = load_audio_tag_info(Path(file_path_raw))
        return jsonify({"ok": True, "data": payload})
    except Exception as exc:
        logger.exception("Load tag editor info failed: {}", file_path_raw)
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/tag-editor/save", methods=["POST"])
def save_tag_editor_info():
    file_path_raw = (request.form.get("file_path") or "").strip()
    if not file_path_raw:
        return jsonify({"ok": False, "error": "file_path 不能为空。"}), 400

    year_raw = (request.form.get("year") or "").strip()
    if year_raw and (not year_raw.isdigit() or len(year_raw) != 4):
        return jsonify({"ok": False, "error": "year 必须是 4 位数字年份。"}), 400

    cover_file = request.files.get("cover_file")
    cover_bytes: bytes | None = None
    cover_mime: str | None = None
    if cover_file and (cover_file.filename or "").strip():
        cover_bytes = cover_file.read()
        if not cover_bytes:
            return jsonify({"ok": False, "error": "上传的封面图片为空。"}), 400
        cover_mime = detect_image_mime(cover_bytes, cover_file.filename or "")

    try:
        save_audio_tag_info(
            file_path=Path(file_path_raw),
            title=(request.form.get("title") or "").strip(),
            artist=(request.form.get("artist") or "").strip(),
            album=(request.form.get("album") or "").strip(),
            year=year_raw,
            description=(request.form.get("description") or "").strip(),
            cover_file_bytes=cover_bytes,
            cover_file_mime=cover_mime,
        )
        return jsonify({"ok": True, "message": "信息已保存。"})
    except Exception as exc:
        logger.exception("Save tag editor info failed: {}", file_path_raw)
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/tag-editor/upload-audio", methods=["POST"])
def upload_tag_editor_audio():
    audio_file = request.files.get("audio_file")
    if not audio_file or not (audio_file.filename or "").strip():
        return jsonify({"ok": False, "error": "请上传音频文件。"}), 400

    filename = sanitize_filename(Path(audio_file.filename).name)
    suffix = Path(filename).suffix.lower()
    if suffix != ".mp3":
        return jsonify({"ok": False, "error": "当前仅支持上传 mp3 文件。"}), 400

    TAG_EDITOR_TEMP_DIR.mkdir(parents=True, exist_ok=True)
    target_name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}_{filename}"
    target_path = TAG_EDITOR_TEMP_DIR / target_name

    try:
        audio_file.save(target_path)
        payload = load_audio_tag_info(target_path)
        payload["is_temp_file"] = True
        return jsonify({"ok": True, "data": payload})
    except Exception as exc:
        logger.exception("Upload tag editor audio failed: {}", filename)
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/download/<int:record_id>", methods=["GET"])
def download_record_file(record_id: int):
    record = query_record_by_id(record_id)
    if not record:
        abort(404, description="record not found")

    file_path_raw = (record.get("file_path") or "").strip()
    if not file_path_raw:
        abort(404, description="file path empty")

    file_path = Path(file_path_raw)
    if not file_path.exists() or not file_path.is_file():
        abort(404, description="file not found on server")

    download_name = sanitize_filename(file_path.name)
    return send_file(file_path, as_attachment=True, download_name=download_name)


def main() -> None:
    setup_logging()
    init_db()
    logger.info("Starting Web UI at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
