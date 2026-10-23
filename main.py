"""Media download & trim API for LLM agents.

POST /api/process_media  -> downloads media via yt-dlp, optionally trims it with ffmpeg,
                            and returns a public URL: Supabase Storage if configured,
                            otherwise /downloads/ on this server.
"""

import base64
import binascii
import logging
import mimetypes
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import parse_qs, urlparse

from datetime import datetime, timedelta, timezone

import httpx
import yt_dlp
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

API_KEY = os.getenv("API_KEY", "")
# Public base URL, e.g. https://media.example.com. Render sets RENDER_EXTERNAL_URL itself.
# If both are empty, derived from the request.
PUBLIC_BASE_URL = (os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
# Netscape-format cookies.txt for yt-dlp; needed when YouTube blocks datacenter IPs
# ("Sign in to confirm you're not a bot"). On Render use a Secret File: /etc/secrets/cookies.txt
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
# Alternative: the whole cookies.txt base64-encoded in an env var (overrides COOKIES_FILE).
COOKIES_B64 = os.getenv("COOKIES_B64", "").strip()
# Log yt-dlp debug output (player clients, JS runtime, HTTP errors) to stdout.
YTDLP_VERBOSE = os.getenv("YTDLP_VERBOSE", "").lower() in ("1", "true", "yes")

# YouTube blocks most datacenter IPs. With an Apify token, YouTube links can be fetched
# via the streamers/youtube-video-downloader actor instead (paid per run, see apify.com).
APIFY_TOKEN = os.getenv("APIFY_TOKEN", "")
APIFY_ACTOR = os.getenv("APIFY_ACTOR", "streamers~youtube-video-downloader")
# "always": YouTube goes straight to Apify; "fallback": try yt-dlp first, Apify on failure.
YOUTUBE_VIA_APIFY = os.getenv("YOUTUBE_VIA_APIFY", "fallback").lower()
APIFY_VIDEO_QUALITY = os.getenv("APIFY_VIDEO_QUALITY", "720p")
# Audio is extracted from a video file; lower video quality = cheaper and faster, same audio.
APIFY_AUDIO_QUALITY = os.getenv("APIFY_AUDIO_QUALITY", "360p")
YOUTUBE_HOSTS = ("youtube.com", "youtu.be")
# Files older than this are deleted on each request (0 = never delete).
FILE_TTL_HOURS = float(os.getenv("FILE_TTL_HOURS", "24"))
# Hard limit for a single download, MB (0 = no limit).
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", "1024"))

# Supabase Storage. If URL and key are set, results are uploaded to a public bucket
# and the link points there (survives restarts of ephemeral hosts like Render).
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "media")
# Free plan rejects uploads over 50 MB.
SUPABASE_MAX_MB = float(os.getenv("SUPABASE_MAX_MB", "50"))
USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

PUBLIC_DIR = Path("./public_downloads").resolve()
WORK_DIR = Path("./tmp_work").resolve()
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-api")
# httpx logs every request URL at INFO; keep tokens and noise out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)

if not API_KEY:
    log.warning("API_KEY is not set - all requests to /api/process_media will be rejected.")

if COOKIES_B64:
    # Kept outside WORK_DIR so cleanup_old_files() doesn't delete it.
    _cookies_path = Path("./cookies_from_env.txt").resolve()
    try:
        _cookies_path.write_bytes(base64.b64decode(COOKIES_B64, validate=True))
        COOKIES_FILE = str(_cookies_path)
        log.info("Using cookies from COOKIES_B64")
    except (binascii.Error, ValueError):
        log.error("COOKIES_B64 is not valid base64 - cookies are ignored.")

app = FastAPI(title="Media Downloader API", version="1.0.0")
app.mount("/downloads", StaticFiles(directory=PUBLIC_DIR), name="downloads")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def verify_api_key(key: Optional[str] = Security(api_key_header)) -> None:
    if not API_KEY or not key or not secrets.compare_digest(key, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class MediaRequest(BaseModel):
    url: str = Field(..., description="Link to the video (YouTube, TikTok, ...)")
    media_type: Literal["video", "audio"] = "video"
    start_time: Optional[str] = Field(None, description="HH:MM:SS, MM:SS or seconds")
    end_time: Optional[str] = Field(None, description="HH:MM:SS, MM:SS or seconds")
    precise: bool = Field(
        False,
        description="Re-encode when trimming for frame-accurate cuts (slower). "
        "Default -c copy snaps the start to the nearest keyframe.",
    )


class MediaResponse(BaseModel):
    status: str
    file_url: str


def parse_time(value: str) -> float:
    """Convert 'HH:MM:SS(.ms)', 'MM:SS' or '123.5' to seconds."""
    try:
        parts = [float(p) for p in value.strip().split(":")]
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid time format: {value!r}")
    if not 1 <= len(parts) <= 3 or any(p < 0 for p in parts):
        raise HTTPException(status_code=422, detail=f"Invalid time format: {value!r}")
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def cleanup_old_files() -> None:
    if FILE_TTL_HOURS <= 0:
        return
    cutoff = time.time() - FILE_TTL_HOURS * 3600
    for directory in (PUBLIC_DIR, WORK_DIR):
        for f in directory.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass


def _sb_headers() -> dict:
    return {"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"}


def supabase_ensure_bucket() -> None:
    """Create the public bucket if it doesn't exist yet."""
    with httpx.Client(timeout=30) as client:
        r = client.get(f"{SUPABASE_URL}/storage/v1/bucket/{SUPABASE_BUCKET}", headers=_sb_headers())
        if r.status_code == 200:
            if not r.json().get("public"):
                log.warning("Bucket %r is not public - returned links won't open.", SUPABASE_BUCKET)
            return
        r = client.post(
            f"{SUPABASE_URL}/storage/v1/bucket",
            headers=_sb_headers(),
            json={"id": SUPABASE_BUCKET, "name": SUPABASE_BUCKET, "public": True},
        )
        if r.status_code >= 400:
            log.error("Could not create bucket %r: %s %s", SUPABASE_BUCKET, r.status_code, r.text)
        else:
            log.info("Created public bucket %r", SUPABASE_BUCKET)


def supabase_upload(path: Path) -> str:
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    with open(path, "rb") as f, httpx.Client(timeout=600) as client:
        r = client.post(
            f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{path.name}",
            headers={**_sb_headers(), "Content-Type": content_type, "x-upsert": "true"},
            content=f,
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Supabase upload failed: {r.status_code} {r.text[:300]}")
    return f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{path.name}"


def supabase_cleanup() -> None:
    """Delete objects older than FILE_TTL_HOURS from the bucket."""
    if FILE_TTL_HOURS <= 0:
        return
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FILE_TTL_HOURS)
    try:
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{SUPABASE_URL}/storage/v1/object/list/{SUPABASE_BUCKET}",
                headers=_sb_headers(),
                json={"prefix": "", "limit": 1000, "sortBy": {"column": "created_at", "order": "asc"}},
            )
            r.raise_for_status()
            # created_at is UTC like "2026-10-23T12:34:56.789Z"; seconds precision is enough.
            old = [
                o["name"]
                for o in r.json()
                if o.get("created_at")
                and datetime.strptime(o["created_at"][:19], "%Y-%m-%dT%H:%M:%S").replace(
                    tzinfo=timezone.utc
                )
                < cutoff
            ]
            if old:
                client.request(
                    "DELETE",
                    f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}",
                    headers=_sb_headers(),
                    json={"prefixes": old},
                ).raise_for_status()
                log.info("Deleted %d expired file(s) from Supabase", len(old))
    except Exception:
        log.exception("Supabase cleanup failed")


class YtdlpLogCollector:
    """Keeps yt-dlp warnings/errors so a failure can explain itself in the API response."""

    def __init__(self) -> None:
        self.lines: list = []

    def debug(self, msg: str) -> None:
        if YTDLP_VERBOSE and not msg.startswith("[download]"):
            log.info("yt-dlp: %s", msg)

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)
        self.lines.append(msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)
        self.lines.append(msg)


class DownloadFailed(Exception):
    pass


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in YOUTUBE_HOSTS)


def canonical_youtube_url(url: str) -> str:
    """Strip playlist/mix params: Apify hangs on '&list=RD...' links."""
    parsed = urlparse(url)
    video_id = None
    if (parsed.hostname or "").endswith("youtu.be"):
        video_id = parsed.path.lstrip("/").split("/")[0]
    elif parsed.path == "/watch":
        video_id = (parse_qs(parsed.query).get("v") or [None])[0]
    else:
        parts = parsed.path.strip("/").split("/")
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v"):
            video_id = parts[1]
    return f"https://www.youtube.com/watch?v={video_id}" if video_id else url


def apify_download(url: str, media_type: str, file_id: str) -> Path:
    """Download a YouTube video through Apify; for audio, convert it to mp3 locally."""
    quality = APIFY_AUDIO_QUALITY if media_type == "audio" else APIFY_VIDEO_QUALITY
    payload = {
        "videos": [{"url": canonical_youtube_url(url)}],
        "storeInKVStore": True,
        "preferredQuality": quality,
        "preferredFormat": "mp4",
        "transcriptionAndSubtitle": "NONE",
    }
    with httpx.Client(
        timeout=httpx.Timeout(30, read=320),
        headers={"Authorization": f"Bearer {APIFY_TOKEN}"},
    ) as client:
        r = client.post(
            f"https://api.apify.com/v2/acts/{APIFY_ACTOR}/run-sync-get-dataset-items",
            params={"timeout": 300},
            json=payload,
        )
        if r.status_code >= 400:
            raise DownloadFailed(f"Apify run failed: {r.status_code} {r.text[:300]}")
        items = r.json()
        file_link = items[0].get("downloadedFileUrl") if items else None
        if not file_link:
            raise DownloadFailed(f"Apify returned no file: {str(items)[:300]}")

        video_path = WORK_DIR / f"{file_id}.mp4"
        with client.stream("GET", file_link, timeout=httpx.Timeout(30, read=120)) as resp:
            resp.raise_for_status()
            size = int(resp.headers.get("content-length") or 0)
            if MAX_FILESIZE_MB > 0 and size > MAX_FILESIZE_MB * 1024 * 1024:
                raise DownloadFailed(f"File is {size / 1048576:.0f} MB, limit is {MAX_FILESIZE_MB} MB")
            with open(video_path, "wb") as f:
                for chunk in resp.iter_bytes(1024 * 1024):
                    f.write(chunk)
        # The record would otherwise sit in Apify storage for ~3 days.
        try:
            client.delete(file_link)
        except httpx.HTTPError:
            pass

    if media_type != "audio":
        return video_path
    audio_path = WORK_DIR / f"{file_id}.mp3"
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path),
         "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(audio_path)],
        capture_output=True, text=True, timeout=600,
    )
    video_path.unlink(missing_ok=True)
    if result.returncode != 0:
        audio_path.unlink(missing_ok=True)
        raise DownloadFailed(f"ffmpeg audio extraction failed: {result.stderr.strip()[-300:]}")
    return audio_path


def download(url: str, media_type: str, file_id: str) -> Path:
    use_apify = bool(APIFY_TOKEN) and is_youtube(url)
    if use_apify and YOUTUBE_VIA_APIFY == "always":
        log.info("[%s] YouTube via Apify", file_id)
        return apify_download(url, media_type, file_id)
    try:
        return ytdlp_download(url, media_type, file_id)
    except DownloadFailed as e:
        if not use_apify:
            raise
        log.warning("[%s] yt-dlp failed, falling back to Apify: %s", file_id, str(e)[:200])
        return apify_download(url, media_type, file_id)


def ytdlp_download(url: str, media_type: str, file_id: str) -> Path:
    collector = YtdlpLogCollector()
    opts = {
        "quiet": True,
        "noprogress": True,
        "logger": collector,
        "verbose": YTDLP_VERBOSE,
        "noplaylist": True,
        "outtmpl": str(WORK_DIR / f"{file_id}.%(ext)s"),
    }
    if MAX_FILESIZE_MB > 0:
        opts["max_filesize"] = MAX_FILESIZE_MB * 1024 * 1024
    cookies_copy = None
    if COOKIES_FILE and Path(COOKIES_FILE).is_file():
        # yt-dlp writes back to the cookie file; Render secret files are read-only, so use a copy.
        cookies_copy = WORK_DIR / f"cookies_{file_id}.txt"
        shutil.copy(COOKIES_FILE, cookies_copy)
        opts["cookiefile"] = str(cookies_copy)

    if media_type == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    else:
        # Prefer H.264 (avc1/h264): AV1, VP9 and HEVC don't play in many clients
        # (Safari, Windows without codecs, older phones). TikTok serves combined
        # h264/h265 files, hence the best[...] fallbacks.
        opts["format"] = (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/best[ext=mp4][vcodec^=avc1]/best[ext=mp4][vcodec^=h264]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
    except yt_dlp.utils.DownloadError as e:
        warnings = [w for w in collector.lines if w not in str(e)][-5:]
        raise DownloadFailed(" | ".join([str(e), *warnings])) from e
    finally:
        if cookies_copy:
            cookies_copy.unlink(missing_ok=True)

    expected_ext = "mp3" if media_type == "audio" else "mp4"
    candidates = sorted(WORK_DIR.glob(f"{file_id}.*"))
    if not candidates:
        raise RuntimeError("yt-dlp finished but no output file was found")
    for c in candidates:
        if c.suffix == f".{expected_ext}":
            return c
    return candidates[0]


def trim(
    src: Path, dst: Path, start: Optional[float], end: Optional[float], precise: bool = False
) -> None:
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if start:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if end is not None:
        cmd += ["-t", str(end - (start or 0))]

    if not precise:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    elif dst.suffix == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-q:a", "2"]
    else:
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart",
        ]
    cmd.append(str(dst))

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not dst.exists():
        dst.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-500:]}")


@app.on_event("startup")
def on_startup() -> None:
    if USE_SUPABASE:
        log.info("Storage: Supabase bucket %r", SUPABASE_BUCKET)
        try:
            supabase_ensure_bucket()
        except Exception:
            log.exception("Supabase bucket check failed")
    else:
        log.info("Storage: local ./public_downloads")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "storage": "supabase" if USE_SUPABASE else "local",
        "cookies": bool(COOKIES_FILE and Path(COOKIES_FILE).is_file()),
        "js_runtime": shutil.which("deno") is not None,
        "youtube_via_apify": YOUTUBE_VIA_APIFY if APIFY_TOKEN else "off",
    }


# Sync `def` on purpose: FastAPI runs it in a threadpool, so blocking yt-dlp/ffmpeg
# calls don't freeze the event loop and concurrent requests are processed in parallel.
@app.post("/api/process_media", response_model=MediaResponse, dependencies=[Depends(verify_api_key)])
def process_media(req: MediaRequest, request: Request) -> MediaResponse:
    cleanup_old_files()
    if USE_SUPABASE:
        supabase_cleanup()

    start = parse_time(req.start_time) if req.start_time else None
    end = parse_time(req.end_time) if req.end_time else None
    if start is not None and end is not None and end <= start:
        raise HTTPException(status_code=422, detail="end_time must be greater than start_time")

    file_id = uuid.uuid4().hex
    log.info(
        "[%s] %s %s start=%s end=%s precise=%s",
        file_id, req.media_type, req.url, start, end, req.precise,
    )

    try:
        downloaded = download(req.url, req.media_type, file_id)
    except DownloadFailed as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")
    except Exception as e:
        log.exception("[%s] download error", file_id)
        raise HTTPException(status_code=500, detail=f"Download failed: {e}")

    final_path = PUBLIC_DIR / f"{file_id}{downloaded.suffix}"
    try:
        if start is not None or end is not None:
            trim(downloaded, final_path, start, end, req.precise)
            downloaded.unlink(missing_ok=True)
        else:
            shutil.move(str(downloaded), final_path)
    except Exception as e:
        log.exception("[%s] processing error", file_id)
        downloaded.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=str(e))

    if USE_SUPABASE:
        try:
            size_mb = final_path.stat().st_size / (1024 * 1024)
            if size_mb > SUPABASE_MAX_MB:
                raise HTTPException(
                    status_code=413,
                    detail=f"Result is {size_mb:.1f} MB, storage limit is {SUPABASE_MAX_MB:.0f} MB. "
                    "Use a shorter start_time/end_time range or media_type='audio'.",
                )
            file_url = supabase_upload(final_path)
        except HTTPException:
            raise
        except Exception as e:
            log.exception("[%s] upload error", file_id)
            raise HTTPException(status_code=502, detail=str(e))
        finally:
            final_path.unlink(missing_ok=True)
    else:
        base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        file_url = f"{base}/downloads/{final_path.name}"
    log.info("[%s] done -> %s", file_id, file_url)
    return MediaResponse(status="success", file_url=file_url)
