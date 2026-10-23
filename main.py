"""Media download & trim API for LLM agents.

POST /api/process_media  -> downloads media via yt-dlp, optionally trims it with ffmpeg,
                            and returns a public URL under /downloads/.
"""

import logging
import os
import secrets
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Literal, Optional

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
# Files older than this are deleted on each request (0 = never delete).
FILE_TTL_HOURS = float(os.getenv("FILE_TTL_HOURS", "24"))
# Hard limit for a single download, MB (0 = no limit).
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", "1024"))

PUBLIC_DIR = Path("./public_downloads").resolve()
WORK_DIR = Path("./tmp_work").resolve()
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
WORK_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("media-api")

if not API_KEY:
    log.warning("API_KEY is not set - all requests to /api/process_media will be rejected.")

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


def download(url: str, media_type: str, file_id: str) -> Path:
    opts = {
        "quiet": True,
        "no_warnings": True,
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
        # Prefer H.264 (avc1): AV1/VP9 in mp4 don't play in many clients (Safari, older phones).
        opts["format"] = (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )
        opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
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


@app.get("/health")
def health():
    return {"status": "ok"}


# Sync `def` on purpose: FastAPI runs it in a threadpool, so blocking yt-dlp/ffmpeg
# calls don't freeze the event loop and concurrent requests are processed in parallel.
@app.post("/api/process_media", response_model=MediaResponse, dependencies=[Depends(verify_api_key)])
def process_media(req: MediaRequest, request: Request) -> MediaResponse:
    cleanup_old_files()

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
    except yt_dlp.utils.DownloadError as e:
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

    base = PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
    file_url = f"{base}/downloads/{final_path.name}"
    log.info("[%s] done -> %s", file_id, file_url)
    return MediaResponse(status="success", file_url=file_url)
