from __future__ import annotations

import ipaddress
import os
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yt_dlp
try:
    import imageio_ffmpeg
    FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG_PATH = None
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.2.0"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
MAX_YT_SOURCE_BYTES = 350 * 1024 * 1024
MAX_YT_FRAMES = 500
MAX_YT_EXPORT_FRAMES = 300
YT_FRAME_TTL_SECONDS = 30 * 60
JOB_TTL_SECONDS = 45 * 60
MAX_JOBS_PER_HOUR = 5
DOWNLOAD_ROOT = Path(os.getenv("FREX_TEMP_DIR", "/tmp/frex-downloads"))
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

FRONTEND_ORIGINS = [
    x.strip()
    for x in os.getenv(
        "FREX_ALLOWED_ORIGINS",
        "https://pavankumar7893.github.io,http://localhost:5500,http://127.0.0.1:5500",
    ).split(",")
    if x.strip()
]

app = FastAPI(
    title="Frex API",
    version=APP_VERSION,
    description="Temporary yt-dlp download service for Frex Web Studio.",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frex-download")
lock = threading.RLock()
jobs: dict[str, dict] = {}
recent_requests: dict[str, list[float]] = {}
active_job_id: str | None = None
active_youtube_frame_id: str | None = None
yt_frame_sessions: dict[str, dict] = {}


class InfoRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    quality: Literal["best", "1080", "720", "480", "audio"] = "720"


class YTFrameStartRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    fps: float = Field(default=1, ge=0.1, le=5)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)


class YTFrameExportRequest(BaseModel):
    id: str = Field(min_length=16, max_length=64)
    indices: list[int] = Field(default_factory=list, max_length=MAX_YT_EXPORT_FRAMES)
    all_frames: bool = False
    format: Literal["png", "jpg"] = "jpg"


def validate_public_url(raw: str) -> str:
    parsed = urlparse(raw.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise HTTPException(400, "Enter a valid public http/https media URL.")
    if parsed.username or parsed.password:
        raise HTTPException(400, "URLs containing embedded credentials are not accepted.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal", ".localhost")):
        raise HTTPException(400, "Local and private network addresses are not allowed.")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
            }
        except OSError:
            raise HTTPException(400, "The URL host could not be resolved.")
    if not addresses or any(not address.is_global for address in addresses):
        raise HTTPException(400, "Only publicly routable media URLs are accepted.")
    return raw.strip()


def client_key(request: Request) -> str:
    # Render supplies X-Forwarded-For from its proxy. Fall back to the ASGI peer.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[-1].strip() if forwarded else (request.client.host if request.client else "unknown")


def enforce_rate_limit(key: str) -> None:
    now = time.time()
    with lock:
        calls = [stamp for stamp in recent_requests.get(key, []) if now - stamp < 3600]
        if len(calls) >= MAX_JOBS_PER_HOUR:
            raise HTTPException(429, "Free demo limit: five download requests per IP per hour. Try again later.")
        calls.append(now)
        recent_requests[key] = calls


def cleanup_old_jobs() -> None:
    now = time.time()
    with lock:
        expired = [job_id for job_id, item in jobs.items() if now - item["created_at"] > JOB_TTL_SECONDS and item["status"] not in ("queued", "downloading", "processing")]
        for job_id in expired:
            shutil.rmtree(DOWNLOAD_ROOT / job_id, ignore_errors=True)
            jobs.pop(job_id, None)
        expired_frames = [
            session_id for session_id, item in yt_frame_sessions.items()
            if now - item["created_at"] > YT_FRAME_TTL_SECONDS
            and item.get("status") not in ("downloading", "previewing", "exporting")
            and (item.get("export") or {}).get("status") != "exporting"
        ]
        for session_id in expired_frames:
            shutil.rmtree(DOWNLOAD_ROOT / session_id, ignore_errors=True)
            yt_frame_sessions.pop(session_id, None)
        for key, calls in list(recent_requests.items()):
            retained = [stamp for stamp in calls if now - stamp < 3600]
            if retained:
                recent_requests[key] = retained
            else:
                recent_requests.pop(key, None)


def extract_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 18,
        "retries": 1,
    }
    with yt_dlp.YoutubeDL(opts) as downloader:
        info = downloader.extract_info(url, download=False)
    if not info:
        raise RuntimeError("The source did not return video information.")
    duration = info.get("duration")
    return {
        "title": info.get("title") or "Untitled media",
        "uploader": info.get("uploader") or info.get("channel") or "Unknown creator",
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
        "extractor": info.get("extractor_key") or info.get("extractor"),
        "webpage_url": info.get("webpage_url") or url,
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": info.get("fps"),
    }


@app.get("/")
def root():
    return {
        "service": "Frex API",
        "version": APP_VERSION,
        "status": "online",
        "docs": "/docs",
        "note": "Temporary download service. Downloads are not stored permanently.",
    }


@app.get("/health")
def health():
    return {"ok": True, "service": "frex-api", "version": APP_VERSION, "active_job": active_job_id is not None, "ffmpeg": bool(FFMPEG_PATH)}


@app.post("/api/info")
def get_info(payload: InfoRequest, request: Request):
    url = validate_public_url(payload.url)
    try:
        return extract_info(url)
    except Exception as exc:
        raise HTTPException(400, str(exc)[:700])


def run_download(job_id: str, url: str, quality: str) -> None:
    global active_job_id
    job_dir = DOWNLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    def update(**values):
        with lock:
            if job_id in jobs:
                jobs[job_id].update(values)

    def progress_hook(data: dict):
        status = data.get("status")
        downloaded = int(data.get("downloaded_bytes") or 0)
        if downloaded > MAX_DOWNLOAD_BYTES:
            raise yt_dlp.utils.DownloadError("Frex free demo limit: maximum download size is 500 MB.")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            pct = min(99.0, (downloaded / total * 100)) if total else None
            update(
                status="downloading",
                progress=round(pct, 1) if pct is not None else None,
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=data.get("speed"),
                eta=data.get("eta"),
                detail="Downloading media…",
            )
        elif status == "finished":
            update(status="processing", progress=99.0, detail="Finalizing downloaded file…")

    if quality == "audio":
        format_selector = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        height_limit = {"1080": 1080, "720": 720, "480": 480}.get(quality)
        if quality == "best":
            format_selector = "bestvideo+bestaudio/best" if FFMPEG_PATH else "best"
        elif FFMPEG_PATH:
            format_selector = f"bestvideo[height<={height_limit}]+bestaudio/best[height<={height_limit}]/best"
        else:
            # Without FFmpeg, choose a progressive format so audio is included.
            format_selector = f"best[height<={height_limit}]/best"

    options = {
        "format": format_selector,
        "outtmpl": str(job_dir / "%(title).100B [%(id)s].%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "socket_timeout": 18,
        "retries": 2,
        "fragment_retries": 2,
        "file_access_retries": 1,
        "continuedl": False,
        "max_filesize": MAX_DOWNLOAD_BYTES,
        "progress_hooks": [progress_hook],
        "overwrites": True,
    }
    if FFMPEG_PATH and quality != "audio":
        options["ffmpeg_location"] = FFMPEG_PATH
        options["merge_output_format"] = "mkv"
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            downloader.download([url])
        candidates = [
            p for p in job_dir.iterdir()
            if p.is_file() and not p.name.endswith((".part", ".ytdl", ".temp"))
        ]
        if not candidates:
            raise RuntimeError("The downloader finished but no output file was found.")
        output = max(candidates, key=lambda p: p.stat().st_size)
        size = output.stat().st_size
        if size > MAX_DOWNLOAD_BYTES:
            raise RuntimeError("The selected file exceeds the 500 MB demo limit.")
        update(
            status="completed",
            progress=100,
            detail="Ready to download.",
            filename=output.name,
            size_bytes=size,
            completed_at=time.time(),
        )
    except Exception as exc:
        update(status="failed", detail=str(exc)[:1000], error=str(exc)[:1000], completed_at=time.time())
    finally:
        with lock:
            if active_job_id == job_id:
                active_job_id = None


@app.post("/api/download")
def create_download(payload: DownloadRequest, request: Request):
    global active_job_id
    cleanup_old_jobs()
    url = validate_public_url(payload.url)
    key = client_key(request)
    enforce_rate_limit(key)
    with lock:
        if active_job_id is not None or active_youtube_frame_id is not None:
            active = jobs.get(active_job_id) if active_job_id else None
            if active and active["status"] in ("queued", "downloading", "processing"):
                raise HTTPException(409, "The free demo is processing another task. Wait for it to finish.")
            if active_youtube_frame_id is not None:
                raise HTTPException(409, "The YouTube frame extractor is busy with another job. Wait for it to finish.")
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "detail": "Queued…",
            "filename": None,
            "size_bytes": None,
            "created_at": time.time(),
            "client_key": key,
        }
        active_job_id = job_id
        executor.submit(run_download, job_id, url, payload.quality)
    return {"ok": True, "id": job_id, "status": "queued", "poll": f"/api/jobs/{job_id}"}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    cleanup_old_jobs()
    with lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "Job not found. It may have expired or the free service restarted.")
        public = {k: v for k, v in job.items() if k not in ("client_key",)}
    public["download_url"] = f"/api/jobs/{job_id}/file" if public["status"] == "completed" else None
    public["expires_in_seconds"] = max(0, JOB_TTL_SECONDS - int(time.time() - public["created_at"]))
    return public


@app.get("/api/jobs/{job_id}/file")
def download_file(job_id: str):
    cleanup_old_jobs()
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "completed":
            raise HTTPException(404, "This download is not ready or has expired.")
        path = DOWNLOAD_ROOT / job_id / str(job["filename"])
        if not path.is_file():
            raise HTTPException(410, "The temporary file is no longer available. Start a new download.")
        media_type = "application/octet-stream"
        return FileResponse(path, media_type=media_type, filename=path.name, headers={"Cache-Control": "no-store"})




def yt_frame_state_payload(session: dict) -> dict:
    export = session.get("export")
    return {
        "id": session["id"],
        "title": session["title"],
        "uploader": session["uploader"],
        "duration": session["duration"],
        "duration_text": session["duration_text"],
        "thumbnail": session.get("thumbnail"),
        "width": session.get("width"),
        "height": session.get("height"),
        "fps": session["fps"],
        "start": session["start"],
        "end": session["end"],
        "estimated_frames": session["estimated_frames"],
        "frame_count": session.get("frame_count", 0),
        "status": session["status"],
        "stage": session.get("stage", ""),
        "progress": session.get("progress", 0),
        "detail": session.get("detail", ""),
        "error": session.get("error", ""),
        "export": {
            k: export.get(k)
            for k in ("id", "status", "progress", "detail", "error", "frame_count", "filename")
        } if export else None,
        "expires_in_seconds": max(0, YT_FRAME_TTL_SECONDS - int(time.time() - session["created_at"])),
    }


def run_yt_frame_preview(session_id: str, url: str, fps: float, start: float, end: float, info: dict) -> None:
    global active_youtube_frame_id
    job_dir = DOWNLOAD_ROOT / session_id
    preview_dir = job_dir / "preview"
    job_dir.mkdir(parents=True, exist_ok=True)
    preview_dir.mkdir(parents=True, exist_ok=True)

    def update(**values):
        with lock:
            if session_id in yt_frame_sessions:
                yt_frame_sessions[session_id].update(values)

    def hook(data: dict):
        if data.get("status") == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            if downloaded > MAX_YT_SOURCE_BYTES:
                raise yt_dlp.utils.DownloadError("YouTube frame extraction is limited to 350 MB source video on the free service.")
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            pct = min(58.0, downloaded / total * 58.0) if total else None
            update(
                progress=round(pct, 1) if pct is not None else 5,
                detail=f"Downloading YouTube source · {downloaded / (1024 * 1024):.1f} MB",
            )

    try:
        update(status="downloading", stage="download", progress=1, detail="Downloading source video from YouTube…")
        options = {
            "format": "bestvideo[height<=720]/best[height<=720]/bestvideo/best",
            "outtmpl": str(job_dir / "source.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 20,
            "retries": 2,
            "fragment_retries": 3,
            "continuedl": False,
            "max_filesize": MAX_YT_SOURCE_BYTES,
            "progress_hooks": [hook],
            "overwrites": True,
        }
        with yt_dlp.YoutubeDL(options) as downloader:
            result = downloader.download([url])
        if result not in (None, 0):
            raise RuntimeError("yt-dlp could not download this YouTube source.")
        sources = [
            p for p in job_dir.glob("source.*")
            if p.is_file() and p.stat().st_size > 0 and not p.name.endswith((".part", ".ytdl"))
        ]
        if not sources:
            raise RuntimeError("YouTube source download finished, but no video file was found.")
        source = max(sources, key=lambda p: p.stat().st_size)
        if source.stat().st_size > MAX_YT_SOURCE_BYTES:
            raise RuntimeError("The source exceeds the 350 MB free-service limit.")
        update(
            status="previewing",
            stage="preview",
            progress=62,
            detail="Generating lightweight frame previews…",
            source_path=str(source),
        )
        length = end - start
        count = min(MAX_YT_FRAMES, max(1, int((length * fps) + 0.999999)))
        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-ss", f"{start:.3f}", "-i", str(source), "-t", f"{length:.3f}",
            "-vf", f"fps={fps:.8f},scale=480:-2:flags=lanczos",
            "-frames:v", str(count), "-q:v", "5", "-start_number", "0",
            str(preview_dir / "frame_%05d.jpg"),
        ]
        proc = __import__("subprocess").Popen(cmd, stdout=__import__("subprocess").PIPE, stderr=__import__("subprocess").STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1)
        with lock:
            yt_frame_sessions[session_id]["process"] = proc
        for raw in proc.stdout or []:
            line = raw.strip()
            if line.startswith("out_time_ms="):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000
                    pct = min(99, 62 + seconds / max(0.1, length) * 37)
                    update(progress=round(pct, 1), detail=f"Generating frame previews · {pct:.0f}%")
                except ValueError:
                    pass
        code = proc.wait()
        with lock:
            if session_id in yt_frame_sessions:
                yt_frame_sessions[session_id]["process"] = None
        if code != 0:
            raise RuntimeError("FFmpeg could not decode this YouTube video. Try another video or a shorter range.")
        preview_count = len(list(preview_dir.glob("frame_*.jpg")))
        if preview_count <= 0:
            raise RuntimeError("No frames were generated. Try a shorter range or a lower FPS.")
        update(
            status="ready",
            stage="ready",
            progress=100,
            frame_count=preview_count,
            detail=f"Preview ready · {preview_count} frames available",
            error="",
            width=info.get("width"),
            height=info.get("height"),
        )
    except Exception as exc:
        update(status="failed", stage="failed", progress=100, detail="Frame extraction failed", error=str(exc)[:1400])
    finally:
        with lock:
            if active_youtube_frame_id == session_id:
                active_youtube_frame_id = None


@app.post("/api/yt-frames/start")
def start_yt_frame_extraction(payload: YTFrameStartRequest, request: Request):
    global active_youtube_frame_id
    cleanup_old_jobs()
    url = validate_public_url(payload.url)
    if not FFMPEG_PATH:
        raise HTTPException(503, "FFmpeg is unavailable on the Render service.")
    enforce_rate_limit(client_key(request))
    if payload.fps > 5:
        raise HTTPException(400, "The Render free service supports up to 5 FPS for YouTube frame extraction.")
    try:
        info = extract_info(url)
    except Exception as exc:
        raise HTTPException(400, str(exc)[:700])
    duration = float(info.get("duration") or 0)
    if duration <= 0:
        raise HTTPException(400, "Could not determine the source video's duration.")
    start = float(payload.start)
    end = float(payload.end) if payload.end is not None else duration
    if start >= duration or end <= start or end > duration + 0.5:
        raise HTTPException(400, f"Choose a valid range between 0 and {duration:.1f} seconds.")
    estimate = max(1, int((end - start) * float(payload.fps) + 0.999999))
    if estimate > MAX_YT_FRAMES:
        raise HTTPException(400, f"This range would produce {estimate:,} frames. The free service limit is {MAX_YT_FRAMES:,}. Lower FPS or shorten the range.")
    with lock:
        if active_job_id is not None or active_youtube_frame_id is not None:
            raise HTTPException(409, "The free Render service is already processing another download or frame task. Wait until it finishes.")
        session_id = uuid.uuid4().hex
        session = {
            "id": session_id,
            "url": url,
            "title": info.get("title") or "YouTube video",
            "uploader": info.get("uploader") or "Unknown creator",
            "thumbnail": info.get("thumbnail"),
            "duration": duration,
            "duration_text": str(int(duration // 60)) + ":" + str(int(duration % 60)).zfill(2),
            "width": None,
            "height": None,
            "fps": float(payload.fps),
            "start": start,
            "end": end,
            "estimated_frames": estimate,
            "frame_count": 0,
            "status": "queued",
            "stage": "queued",
            "progress": 0,
            "detail": "Queued for source download…",
            "error": "",
            "created_at": time.time(),
            "source_path": "",
            "process": None,
            "preview_dir": str(DOWNLOAD_ROOT / session_id / "preview"),
            "export": None,
        }
        yt_frame_sessions[session_id] = session
        active_youtube_frame_id = session_id
    executor.submit(run_yt_frame_preview, session_id, url, float(payload.fps), start, end, info)
    return yt_frame_state_payload(session)


@app.get("/api/yt-frames/state")
def get_yt_frame_state(id: str):
    cleanup_old_jobs()
    with lock:
        session = yt_frame_sessions.get(id)
        if not session:
            raise HTTPException(404, "YouTube frame session expired. Start the extraction again.")
        return yt_frame_state_payload(session)


@app.get("/api/yt-frames/page")
def get_yt_frame_page(id: str, offset: int = 0, limit: int = 72):
    with lock:
        session = yt_frame_sessions.get(id)
        if not session or session.get("status") != "ready":
            raise HTTPException(409, "YouTube frame previews are not ready yet.")
        count = int(session.get("frame_count", 0))
    offset = max(0, offset)
    limit = max(1, min(72, limit))
    end = min(count, offset + limit)
    frames = []
    for index in range(offset, end):
        frames.append({
            "index": index,
            "timestamp": round(float(session["start"]) + index / float(session["fps"]), 3),
            "timestamp_text": f"{(float(session['start']) + index / float(session['fps'])):0.2f}s",
            "image": f"/api/yt-frames/image?id={id}&index={index}",
        })
    return {"frames": frames, "offset": offset, "total": count, "has_more": end < count}


@app.get("/api/yt-frames/image")
def get_yt_frame_image(id: str, index: int):
    with lock:
        session = yt_frame_sessions.get(id)
        if not session or session.get("status") != "ready":
            raise HTTPException(404, "YouTube frame preview not found or expired.")
        count = int(session["frame_count"])
        directory = Path(session["preview_dir"])
    if index < 0 or index >= count:
        raise HTTPException(404, "Frame index is out of range.")
    path = directory / f"frame_{index:05d}.jpg"
    if not path.is_file():
        raise HTTPException(404, "Frame preview image is missing.")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=600"})


def run_yt_frame_export(session_id: str, export_id: str, indices: list[int], export_all: bool, image_format: str):
    global active_youtube_frame_id
    session = yt_frame_sessions[session_id]
    export_dir = DOWNLOAD_ROOT / session_id / ("export_" + export_id)
    export_dir.mkdir(parents=True, exist_ok=True)
    try:
        source_path = session["source_path"]
        if not Path(source_path).is_file():
            raise RuntimeError("Source video has expired. Run frame extraction again.")
        selected = list(range(session["frame_count"])) if export_all else sorted(set(indices))
        ext = "jpg" if image_format == "jpg" else "png"
        vf = f"fps={float(session['fps']):.8f}"
        if not export_all:
            expr = "+".join(f"eq(n\\,{i})" for i in selected)
            vf += f",select={expr}"
        pattern = str(export_dir / f"frame_%05d.{ext}")
        cmd = [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
            "-ss", f"{float(session['start']):.3f}", "-i", source_path,
            "-t", f"{float(session['end'] - session['start']):.3f}",
        ]
        if export_all:
            cmd += ["-vf", vf]
        else:
            filter_file = export_dir / "select_filter.txt"
            filter_file.write_text(vf, encoding="utf-8")
            cmd += ["-filter_script:v", str(filter_file)]
        cmd += ["-fps_mode", "vfr", "-frames:v", str(len(selected))]
        if ext == "jpg":
            cmd += ["-q:v", "2"]
        else:
            cmd += ["-compression_level", "3"]
        cmd += ["-start_number", "1", pattern]
        proc = __import__("subprocess").Popen(cmd, stdout=__import__("subprocess").PIPE,
            stderr=__import__("subprocess").STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1)
        with lock:
            yt_frame_sessions[session_id]["export"]["process"] = proc
        for raw in proc.stdout or []:
            line = raw.strip()
            if line.startswith("frame="):
                try:
                    current = int(line.split("=", 1)[1])
                    with lock:
                        export = yt_frame_sessions[session_id].get("export") or {}
                        export.update(progress=min(98, current / max(1, len(selected)) * 96),
                                      detail=f"Rendering full-resolution frames · {current}/{len(selected)}")
                except ValueError:
                    pass
        code = proc.wait()
        with lock:
            yt_frame_sessions[session_id]["export"]["process"] = None
        if code != 0:
            raise RuntimeError("FFmpeg failed while rendering full-resolution frame images.")
        outputs = sorted(export_dir.glob(f"frame_*.{ext}"))
        if len(outputs) != len(selected):
            raise RuntimeError(f"Expected {len(selected)} frames but FFmpeg produced {len(outputs)}.")
        zip_path = export_dir / "Frex_YouTube_Frames.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for sequence, (path, frame_index) in enumerate(zip(outputs, selected), 1):
                stamp = float(session["start"]) + frame_index / float(session["fps"])
                archive.write(path, f"frame_{sequence:05d}_{stamp:.2f}s.{ext}")
            archive.writestr("metadata.json", __import__("json").dumps({
                "title": session["title"], "url": session["url"], "fps": session["fps"],
                "start": session["start"], "end": session["end"],
                "exported_frames": len(selected), "created_at": time.time(),
            }, indent=2))
        filename = f"Frex_YouTube_Frames_{len(selected)}_{export_id[:6]}.zip"
        final_path = DOWNLOAD_ROOT / session_id / filename
        shutil.copy2(zip_path, final_path)
        with lock:
            exp = yt_frame_sessions[session_id]["export"]
            exp.update(status="complete", progress=100, detail=f"ZIP ready · {len(selected)} full-resolution frames",
                       filename=filename, zip_path=str(final_path), error="", process=None)
    except Exception as exc:
        with lock:
            if session_id in yt_frame_sessions:
                exp = yt_frame_sessions[session_id].get("export") or {}
                exp.update(status="failed", detail="Frame ZIP export failed", error=str(exc)[:1200], process=None)
    finally:
        with lock:
            if active_youtube_frame_id == session_id:
                active_youtube_frame_id = None


@app.post("/api/yt-frames/export")
def start_yt_frame_export(payload: YTFrameExportRequest):
    with lock:
        session = yt_frame_sessions.get(payload.id)
        if not session or session.get("status") != "ready":
            raise HTTPException(404, "Generate YouTube frame previews first.")
        if session.get("export") and session["export"].get("status") == "exporting":
            raise HTTPException(409, "A frame ZIP export is already running.")
        if active_job_id is not None or (active_youtube_frame_id is not None and active_youtube_frame_id != payload.id):
            raise HTTPException(409, "The Render service is processing another task. Wait for it to finish.")
        active_youtube_frame_id = payload.id
        frame_count = int(session["frame_count"])
        if payload.all_frames:
            selected = list(range(frame_count))
        else:
            selected = sorted(set(payload.indices))
            if not selected:
                raise HTTPException(400, "Select at least one frame.")
        if any(x < 0 or x >= frame_count for x in selected):
            raise HTTPException(400, "One or more selected frame indexes are out of range.")
        if len(selected) > MAX_YT_EXPORT_FRAMES:
            raise HTTPException(400, f"The Render free service can export up to {MAX_YT_EXPORT_FRAMES} full-resolution frames per ZIP.")
        export_id = uuid.uuid4().hex
        session["export"] = {
            "id": export_id, "status": "exporting", "progress": 0,
            "detail": f"Preparing {len(selected)} selected frames…", "error": "",
            "frame_count": len(selected), "filename": "", "zip_path": "", "process": None,
        }
    threading.Thread(
        target=run_yt_frame_export,
        args=(payload.id, export_id, selected, payload.all_frames, payload.format),
        daemon=True,
    ).start()
    return {"ok": True, "id": export_id, "status": "exporting", "frame_count": len(selected)}


@app.get("/api/yt-frames/download")
def download_yt_frame_zip(id: str):
    with lock:
        session = yt_frame_sessions.get(id)
        if not session:
            raise HTTPException(404, "YouTube frame session expired.")
        export = session.get("export") or {}
        if export.get("status") != "complete":
            raise HTTPException(409, "Frame ZIP is not ready.")
        path = Path(export.get("zip_path", ""))
        filename = export.get("filename") or "Frex_YouTube_Frames.zip"
    if not path.is_file():
        raise HTTPException(410, "The ZIP expired. Generate and export frames again.")
    return FileResponse(path, media_type="application/zip", filename=filename, headers={"Cache-Control": "no-store"})


@app.post("/api/yt-frames/cancel")
def cancel_yt_frame_task(id: str):
    global active_youtube_frame_id
    with lock:
        session = yt_frame_sessions.get(id)
        if not session:
            raise HTTPException(404, "Frame session not found.")
        proc = session.get("process") or (session.get("export") or {}).get("process")
        if session.get("status") in ("downloading", "previewing"):
            session["status"] = "cancelled"
            session["detail"] = "Cancellation requested."
        if session.get("export") and session["export"].get("status") == "exporting":
            session["export"]["status"] = "cancelled"
            session["export"]["detail"] = "Cancellation requested."
    if proc and proc.poll() is None:
        proc.terminate()
    with lock:
        if active_youtube_frame_id == id:
            active_youtube_frame_id = None
    return {"ok": True}



@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n"
