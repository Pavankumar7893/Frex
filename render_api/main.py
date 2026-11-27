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
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_VERSION = "0.1.0"
MAX_DOWNLOAD_BYTES = 500 * 1024 * 1024
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


class InfoRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    quality: Literal["best", "1080", "720", "480", "audio"] = "720"


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
    return forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")


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
    return {"ok": True, "service": "frex-api", "version": APP_VERSION, "active_job": active_job_id is not None}


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
    elif quality == "best":
        format_selector = "best"
    else:
        max_height = int(quality)
        format_selector = f"best[height<={max_height}]/best"

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
        if active_job_id is not None:
            active = jobs.get(active_job_id)
            if active and active["status"] in ("queued", "downloading", "processing"):
                raise HTTPException(409, "The free demo is processing another download. Wait for it to finish.")
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


@app.get("/robots.txt")
def robots():
    return "User-agent: *\nDisallow: /\n"
