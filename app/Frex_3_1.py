#!/usr/bin/env python3
"""Frex 3.2.0 - local web UI, downloader, and batch frame extractor.

Run with Python 3.10+. The UI is served only on 127.0.0.1; no cloud service is used.
Install/update downloader: py -m pip install -U yt-dlp
Optional: install FFmpeg and put ffmpeg.exe on PATH or beside this script.
"""
from __future__ import annotations

import json
import csv
import io
import os
import re
import math
import uuid
import zipfile
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
import argparse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

APP_NAME = "Frex"
BASE_DIR = Path(__file__).resolve().parent
WEB_FILE = BASE_DIR / "Frex_3_1.html"
DEFAULT_FOLDER = Path.home() / "Videos" / "Frex"
APPDATA = Path(os.environ.get("APPDATA") or Path.home()) / "Frex"
# Use python.exe for command-line child processes even when Frex itself was
# launched invisibly with pythonw.exe. This preserves stdout for yt-dlp JSON/progress.
PYTHON_EXECUTABLE = str(Path(sys.executable).with_name("python.exe")) if Path(sys.executable).name.lower() == "pythonw.exe" else sys.executable
if not Path(PYTHON_EXECUTABLE).is_file():
    PYTHON_EXECUTABLE = sys.executable
SETTINGS_FILE = APPDATA / "settings.json"
MAX_BATCH = 100
MAX_FRAME_BATCH_URLS = 25
MAX_FRAME_BATCH_TOTAL = 50000
MAX_BODY = 2 * 1024 * 1024
MAX_FRAME_PREVIEWS = 12000
MAX_FRAME_EXPORT = 25000
MAX_SELECTED_FRAME_EXPORT = 2000
FRAME_CACHE_TTL_SECONDS = 48 * 60 * 60
FRAME_PAGE_SIZE = 72
FRAME_CACHE = APPDATA / "frame_cache"


def locate(*names: str) -> str | None:
    for name in names:
        candidate = BASE_DIR / name
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(name)
        if found:
            return found
    return None


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def seconds_from_time(value: str) -> int:
    value = str(value).strip()
    parts = value.split(":")
    if not 1 <= len(parts) <= 3 or not all(p.isdigit() for p in parts):
        raise ValueError("Use seconds, MM:SS, or HH:MM:SS for trim times.")
    nums = [int(p) for p in parts]
    if len(nums) == 3:
        if nums[1] >= 60 or nums[2] >= 60:
            raise ValueError("Minutes and seconds must be below 60.")
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("Seconds must be below 60.")
        return nums[0] * 60 + nums[1]
    return nums[0]


def fmt_time(seconds: float | int | None) -> str:
    if seconds is None:
        return "—"
    n = max(0, int(seconds))
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class FrexService:
    def __init__(self) -> None:
        self.ytdlp = locate("yt-dlp.exe", "yt-dlp")
        self.ytdlp_module = False
        if not self.ytdlp:
            try:
                subprocess.run([PYTHON_EXECUTABLE, "-m", "yt_dlp", "--version"], check=True,
                               capture_output=True, timeout=12,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.ytdlp_module = True
            except Exception:
                pass
        self.ffmpeg = locate("ffmpeg.exe", "ffmpeg")
        if not self.ffmpeg:
            # The desktop installer includes imageio-ffmpeg so frame extraction
            # works without asking the user to install FFmpeg manually.
            try:
                import imageio_ffmpeg
                self.ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                self.ffmpeg = None
        self.deno = locate("deno.exe", "deno")
        self.node = locate("node.exe", "node")
        self.lock = threading.RLock()
        self.jobs: list[dict] = []
        self.logs: list[dict] = []
        self.running = False
        self.cancel_requested = False
        self.process: subprocess.Popen | None = None
        self.cookie_file: str | None = None  # in-memory path only; contents are never read by Frex
        self.frame_sessions: dict[str, dict] = {}
        self.frame_batches: dict[str, dict] = {}
        self.frame_process: subprocess.Popen | None = None
        self.settings = self._load_settings()
        self.folder = self.settings.get("folder", str(DEFAULT_FOLDER))
        self.mode = self.settings.get("mode", "Video")
        self.quality = self.settings.get("quality", "Best available")
        self.container = self.settings.get("container", "MP4")

    def _load_settings(self) -> dict:
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings(self) -> None:
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
            data = {"folder": self.folder, "mode": self.mode, "quality": self.quality, "container": self.container}
            tmp = SETTINGS_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(SETTINGS_FILE)
        except OSError:
            pass

    def log(self, message: str) -> None:
        item = {"time": datetime.now().strftime("%H:%M:%S"), "message": str(message)[:500]}
        with self.lock:
            self.logs.append(item)
            self.logs = self.logs[-100:]

    def status(self) -> dict:
        with self.lock:
            active = self.running
        return {
            "app": APP_NAME,
            "ytdlp": self.ytdlp or ("Python module" if self.ytdlp_module else None),
            "ffmpeg": self.ffmpeg,
            "js_runtime": self.deno or self.node,
            "running": active,
            "folder": self.folder,
            "mode": self.mode,
            "quality": self.quality,
            "container": self.container,
        }

    def auth_args(self, options: dict) -> list[str]:
        # Authentication is impossible to activate unless Advanced mode is explicitly on.
        if not options.get("advanced"):
            return []
        source = options.get("auth_source", "No login")
        mapping = {
            "Chrome session": "chrome", "Edge session": "edge", "Firefox session": "firefox",
            "Brave session": "brave", "Vivaldi session": "vivaldi", "Opera session": "opera",
            "Chromium session": "chromium",
        }
        if source in mapping:
            return ["--cookies-from-browser", mapping[source]]
        if source == "Cookies file":
            selected = self.cookie_file
            if not selected or not Path(selected).is_file():
                raise ValueError("Choose a valid cookies.txt file in Advanced settings.")
            return ["--cookies", selected]
        return []

    def ytdlp_command(self) -> list[str]:
        if self.ytdlp_module:
            return [PYTHON_EXECUTABLE, "-m", "yt_dlp"]
        if self.ytdlp:
            return [self.ytdlp]
        raise RuntimeError("yt-dlp is not installed. Use the Install / update button in Frex.")

    def runtime_args(self) -> list[str]:
        if self.deno:
            return ["--js-runtimes", f"deno:{self.deno}"]
        if self.node:
            return ["--js-runtimes", f"node:{self.node}"]
        return []

    def inspect(self, url: str, options: dict) -> dict:
        if not valid_url(url):
            raise ValueError("Enter a valid http or https video URL.")
        if not (self.ytdlp or self.ytdlp_module):
            raise RuntimeError("yt-dlp was not found. Click Install / update yt-dlp in Frex or run: py -m pip install -U yt-dlp")
        args = self.ytdlp_command() + ["--dump-single-json", "--no-playlist", "--no-warnings", "--socket-timeout", "20"]
        args += self.runtime_args() + self.auth_args(options) + [url]
        self.log("Inspecting source metadata…")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=creationflags)
        if result.returncode != 0:
            raise RuntimeError(self.friendly_error((result.stderr or result.stdout or "Could not inspect this URL.")[-1800:]))
        info = json.loads(result.stdout)
        video_formats = [f for f in info.get("formats", []) if f.get("vcodec") != "none"]
        heights = sorted({int(f["height"]) for f in video_formats if f.get("height")}, reverse=True)
        best_format = max(video_formats, key=lambda f: (int(f.get("height") or 0), float(f.get("fps") or 0), float(f.get("tbr") or 0)), default={})
        source_fps = float(info.get("fps") or best_format.get("fps") or 0) or None
        return {
            "title": info.get("title") or "Untitled video",
            "uploader": info.get("uploader") or info.get("channel") or "Unknown creator",
            "duration": info.get("duration"),
            "duration_text": fmt_time(info.get("duration")),
            "thumbnail": info.get("thumbnail"),
            "heights": heights,
            "width": info.get("width") or best_format.get("width"),
            "height": info.get("height") or best_format.get("height"),
            "fps": source_fps,
            "extractor": info.get("extractor_key") or info.get("extractor") or "Source",
        }

    @staticmethod
    def friendly_error(detail: str) -> str:
        low = detail.lower()
        if "javascript runtime" in low or "js runtime" in low or "challenge" in low:
            return detail + "\n\nInstall Deno and restart Frex if this source requires a JavaScript runtime."
        if "ffmpeg" in low and ("not found" in low or "required" in low):
            return detail + "\n\nInstall FFmpeg and add ffmpeg.exe to PATH for merging, audio conversion, and trimming."
        if "members-only" in low or "members only" in low or "login required" in low or "sign in" in low:
            return detail + "\n\nUse only an account that already has permission to view this content. Advanced account access cannot unlock content your account cannot watch."
        if "unsupported url" in low:
            return detail + "\n\nThis URL is not supported by the installed yt-dlp extractor. Update yt-dlp or use the provider's official download option."
        if "http error 403" in low or "forbidden" in low:
            return detail + "\n\nThe source refused this request. Frex does not bypass DRM, access controls, or provider restrictions."
        return detail

    @staticmethod
    def _frame_time(seconds: float) -> str:
        ms = max(0, int(round(float(seconds) * 1000)))
        hours, rem = divmod(ms, 3_600_000)
        minutes, rem = divmod(rem, 60_000)
        secs, millis = divmod(rem, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    def _cleanup_frame_cache(self) -> None:
        """Remove abandoned frame sessions older than 48 hours; never touch active work."""
        try:
            FRAME_CACHE.mkdir(parents=True, exist_ok=True)
            cutoff = time.time() - FRAME_CACHE_TTL_SECONDS
            with self.lock:
                active_dirs = {
                    str(Path(v.get("session_dir", "")).resolve())
                    for v in self.frame_sessions.values()
                    if v.get("status") in ("downloading", "previewing")
                    or (v.get("export") or {}).get("status") == "exporting"
                }
                active_dirs.update(
                    str(Path(b.get("batch_dir", "")).resolve())
                    for b in self.frame_batches.values()
                    if b.get("status") in ("queued", "processing", "packaging") and b.get("batch_dir")
                )
            for child in FRAME_CACHE.iterdir():
                try:
                    if child.is_dir() and child.stat().st_mtime < cutoff and str(child.resolve()) not in active_dirs:
                        shutil.rmtree(child, ignore_errors=True)
                except OSError:
                    continue
        except OSError:
            pass

    def start_frame_preview(self, url: str, fps_value, start_value="", end_value="", options=None, allow_batch=False, direct_export=False, image_format="png") -> dict:
        """Download an authorized source video, then create a paged low-res frame contact sheet cache."""
        options = options or {}
        if not valid_url(url):
            raise ValueError("Paste a valid YouTube video URL first.")
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required for frame extraction. Install FFmpeg and restart Frex.")
        if not (self.ytdlp or self.ytdlp_module):
            raise RuntimeError("yt-dlp is missing. Use Install / update yt-dlp first.")
        with self.lock:
            if self.running:
                raise RuntimeError("Finish or cancel the current download queue before starting frame extraction.")
            if not allow_batch and any(b.get("status") in ("queued", "processing", "packaging") for b in self.frame_batches.values()):
                raise RuntimeError("A batch frame extraction is already running.")
            if any(s.get("status") in ("downloading", "previewing") or (s.get("export") or {}).get("status") == "exporting" for s in self.frame_sessions.values()):
                raise RuntimeError("A frame extraction task is already running.")

        try:
            fps = float(fps_value)
        except (TypeError, ValueError):
            raise ValueError("Enter a valid FPS value, for example 1, 2.5, or 30.")
        if not math.isfinite(fps) or fps < 0.1 or fps > 30:
            raise ValueError("Custom FPS must be between 0.1 and 30.")

        auth_args = self.auth_args(options)
        if options.get("advanced") and options.get("auth_source") not in (None, "No login") and not options.get("auth_confirmed"):
            raise ValueError("Confirm Advanced account access before using a saved browser session or cookies file.")

        self._cleanup_frame_cache()
        # Inspect once so the range, source cadence, and output estimate are anchored to the actual video.
        info = self.inspect(url, options)
        duration = float(info.get("duration") or 0)
        if duration <= 0:
            raise RuntimeError("Frex couldn't read this video's duration, so it can't estimate the frame count safely.")
        start = seconds_from_time(start_value) if str(start_value).strip() else 0
        end = seconds_from_time(end_value) if str(end_value).strip() else int(math.floor(duration))
        if start < 0 or end <= start or end > duration + 1:
            raise ValueError(f"Choose a valid range inside the video duration ({fmt_time(duration)}). Leave both fields blank for the full video.")
        requested_fps = fps
        source_fps = float(info.get("fps") or 0)
        # Do not manufacture duplicate samples when the requested rate exceeds the source cadence.
        if source_fps > 0:
            fps = min(fps, source_fps)
        fps = max(0.1, fps)
        estimate = max(1, int(math.ceil((end - start) * fps)))
        image_format = str(image_format).lower()
        if image_format not in ("png", "jpg"):
            raise ValueError("Choose PNG or JPG output.")
        if direct_export and estimate > MAX_FRAME_EXPORT:
            raise ValueError(
                f"This direct export would create about {estimate:,} frames. The direct ZIP limit is {MAX_FRAME_EXPORT:,}. "
                "Lower the FPS or narrow the time range."
            )
        if not direct_export and estimate > MAX_FRAME_PREVIEWS:
            raise ValueError(
                f"That FPS/range would create about {estimate:,} preview frames. The preview limit is {MAX_FRAME_PREVIEWS:,}. "
                "Lower the FPS or narrow the time range, or use Direct Download."
            )

        session_id = uuid.uuid4().hex
        session_dir = FRAME_CACHE / session_id
        preview_dir = session_dir / "preview"
        session_dir.mkdir(parents=True, exist_ok=True)
        if not direct_export:
            preview_dir.mkdir(parents=True, exist_ok=True)
        session = {
            "id": session_id, "url": url, "title": info.get("title") or "YouTube video",
            "uploader": info.get("uploader") or "Unknown channel", "thumbnail": info.get("thumbnail"),
            "duration": duration, "duration_text": fmt_time(duration), "fps": fps,
            "requested_fps": requested_fps, "source_fps": source_fps,
            "width": info.get("width"), "height": info.get("height"),
            "start": float(start), "end": float(end),
            "estimated_frames": estimate, "frame_count": 0, "status": "downloading", "stage": "download",
            "progress": 0.0, "detail": "Starting source download…", "error": "",
            "source_path": "", "session_dir": str(session_dir), "preview_dir": str(preview_dir),
            "export": None, "created_at": time.time(), "auth_args": list(auth_args),
            "direct_export": bool(direct_export), "image_format": image_format,
        }
        with self.lock:
            self.frame_sessions[session_id] = session
        mode_label = "direct export" if direct_export else "preview extraction"
        self.log(f"Frame extractor started · {fps:g} fps · estimated {estimate:,} frames · {mode_label}")
        threading.Thread(target=self._prepare_frame_session, args=(session_id,), daemon=True).start()
        return {k: session[k] for k in ("id", "title", "uploader", "thumbnail", "duration", "duration_text", "fps", "requested_fps", "source_fps", "width", "height", "start", "end", "estimated_frames", "status", "detail", "progress")}

    def _frame_update(self, session_id: str, **updates) -> None:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if session:
                session.update(updates)

    def _prepare_frame_session(self, session_id: str) -> None:
        with self.lock:
            session = self.frame_sessions.get(session_id)
        if not session:
            return
        session_dir = Path(session["session_dir"])
        url = session["url"]
        try:
            args = self.ytdlp_command() + [
                "--newline", "--progress", "--no-playlist", "--no-warnings", "--socket-timeout", "20",
                "--retries", "5", "--fragment-retries", "10", "--file-access-retries", "3",
                "-f", "bestvideo/best",
                "-o", str(session_dir / "source.%(ext)s"),
            ] + self.runtime_args() + list(session.get("auth_args") or []) + [url]
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            tail = []
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                                    universal_newlines=True, creationflags=flags)
            with self.lock:
                self.frame_process = proc
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                pct = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
                if pct:
                    value = min(60.0, float(pct.group(1)) * 0.60)
                    self._frame_update(session_id, progress=value, detail=f"Downloading source · {float(pct.group(1)):.1f}%")
                low = line.lower()
                if any(t in low for t in ("error", "failed", "unable to", "warning")):
                    tail.append(line[:500])
                    tail[:] = tail[-8:]
                with self.lock:
                    cancelled = bool(self.frame_sessions.get(session_id, {}).get("cancel_requested"))
                if cancelled:
                    self._terminate(proc)
                    break
            code = proc.wait()
            with self.lock:
                self.frame_process = None
                cancelled = bool(self.frame_sessions.get(session_id, {}).get("cancel_requested"))
            if cancelled:
                self._frame_update(session_id, status="cancelled", stage="cancelled", detail="Frame extraction cancelled.")
                return
            if code != 0:
                raise RuntimeError(self.friendly_error("\n".join(tail[-5:]) or f"yt-dlp exited with code {code} while fetching the video."))

            sources = [p for p in session_dir.glob("source.*") if p.is_file() and p.stat().st_size > 0]
            if not sources:
                raise RuntimeError("yt-dlp finished but Frex could not find the downloaded video file.")
            source_path = max(sources, key=lambda p: p.stat().st_size)
            if session.get("direct_export"):
                # Reuse the full-resolution ZIP renderer without creating preview images.
                self._frame_update(
                    session_id, source_path=str(source_path), status="ready", stage="direct_export",
                    progress=62.0, frame_count=int(session["estimated_frames"]),
                    detail="Source downloaded · rendering frames directly into ZIP…"
                )
                export = self.start_frame_export(
                    session_id, [], True, session.get("image_format", "png")
                )
                self.log(f"Direct frame export queued · {export['frame_count']:,} frames")
                return

            self._frame_update(session_id, source_path=str(source_path), stage="preview", status="previewing", progress=62.0,
                               detail="Generating frame previews…")

            fps = float(session["fps"])
            start = float(session["start"])
            length = float(session["end"] - session["start"])
            preview_dir = Path(session["preview_dir"])
            ffmpeg = self.ffmpeg or "ffmpeg"
            vf = f"fps={fps:.8f},scale=480:-2:flags=lanczos"
            cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
                   "-ss", f"{start:.3f}", "-i", str(source_path), "-t", f"{length:.3f}",
                   "-vf", vf, "-frames:v", str(int(session["estimated_frames"])),
                   "-q:v", "4", "-start_number", "0", str(preview_dir / "frame_%06d.jpg")]
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
            with self.lock:
                self.frame_process = proc
            assert proc.stdout is not None
            ffmpeg_tail = []
            for raw in proc.stdout:
                line = raw.strip()
                if line and not line.startswith(("frame=", "fps=", "stream_")) and "=" not in line:
                    ffmpeg_tail.append(line[:500])
                    ffmpeg_tail[:] = ffmpeg_tail[-10:]
                if line.startswith("out_time_ms="):
                    try:
                        out_sec = int(line.split("=", 1)[1]) / 1_000_000
                        ratio = min(1.0, out_sec / max(0.001, length))
                        self._frame_update(session_id, progress=62.0 + ratio * 38.0,
                                           detail=f"Generating previews · {ratio*100:.0f}%")
                    except ValueError:
                        pass
                with self.lock:
                    cancelled = bool(self.frame_sessions.get(session_id, {}).get("cancel_requested"))
                if cancelled:
                    self._terminate(proc)
                    break
            code = proc.wait()
            with self.lock:
                self.frame_process = None
                cancelled = bool(self.frame_sessions.get(session_id, {}).get("cancel_requested"))
            if cancelled:
                self._frame_update(session_id, status="cancelled", stage="cancelled", detail="Frame extraction cancelled.")
                return
            if code != 0:
                detail = "\n".join(ffmpeg_tail[-6:]) or f"No FFmpeg diagnostic was returned (exit code {code})."
                raise RuntimeError(f"FFmpeg could not generate frame previews (exit code {code}): {detail}")

            count = len(list(preview_dir.glob("frame_*.jpg")))
            if count <= 0:
                raise RuntimeError("No frames were generated. Try a different FPS or time range.")
            self._frame_update(session_id, status="ready", stage="ready", progress=100.0, frame_count=count,
                               detail=f"Ready · {count:,} frames available for preview and selection", error="")
            self.log(f"Frame preview ready · {count:,} frames · {session['title'][:100]}")
        except Exception as exc:
            with self.lock:
                self.frame_process = None
            self._frame_update(session_id, status="failed", stage="failed", detail="Frame extraction failed", error=str(exc)[:1800])
            self.log(f"Frame extraction failed · {str(exc)[:260]}")

    def frame_session_state(self, session_id: str) -> dict:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if not session:
                raise ValueError("Frame session not found. Generate the preview again.")
            export = session.get("export") or {}
            return {
                "id": session["id"], "title": session["title"], "uploader": session["uploader"],
                "duration": session["duration"], "duration_text": session["duration_text"],
                "fps": session["fps"], "requested_fps": session.get("requested_fps", session["fps"]),
                "source_fps": session.get("source_fps"), "width": session.get("width"), "height": session.get("height"),
                "thumbnail": session.get("thumbnail"), "start": session["start"], "end": session["end"],
                "estimated_frames": session["estimated_frames"], "frame_count": session["frame_count"],
                "direct_export": bool(session.get("direct_export")),
                "status": session["status"], "stage": session["stage"], "progress": session["progress"],
                "detail": session["detail"], "error": session["error"],
                "export": {k: export.get(k) for k in ("id", "status", "progress", "detail", "error", "frame_count", "filename")}
                          if export else None,
            }

    def frame_page(self, session_id: str, offset=0, limit=FRAME_PAGE_SIZE) -> dict:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if not session or session.get("status") != "ready":
                raise ValueError("Frame previews are not ready yet.")
            count = int(session["frame_count"])
            fps = float(session["fps"])
            start = float(session["start"])
        offset = max(0, int(offset))
        limit = max(1, min(FRAME_PAGE_SIZE, int(limit)))
        end = min(count, offset + limit)
        if offset >= count:
            return {"frames": [], "offset": offset, "total": count, "has_more": False}
        frames = []
        for index in range(offset, end):
            timestamp = start + index / fps
            frames.append({
                "index": index,
                "timestamp": round(timestamp, 3),
                "timestamp_text": self._frame_time(timestamp),
                "image": f"/api/frames/image?id={session_id}&index={index}",
            })
        return {"frames": frames, "offset": offset, "total": count, "has_more": end < count}

    def frame_image_path(self, session_id: str, index: int) -> Path:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if not session or session.get("status") != "ready":
                raise ValueError("Frame preview not found.")
            count = int(session["frame_count"])
            preview_dir = Path(session["preview_dir"])
        if index < 0 or index >= count:
            raise ValueError("Frame index is out of range.")
        path = preview_dir / f"frame_{index:06d}.jpg"
        if not path.is_file():
            raise ValueError("Frame preview file is missing.")
        return path

    def start_frame_export(self, session_id: str, indices=None, export_all=False, image_format="png", allow_batch=False) -> dict:
        indices = indices or []
        image_format = str(image_format).lower()
        if image_format not in ("png", "jpg"):
            raise ValueError("Choose PNG or JPG output.")
        with self.lock:
            if not allow_batch and any(b.get("status") in ("queued", "processing", "packaging") for b in self.frame_batches.values()):
                raise RuntimeError("A batch frame extraction is already running.")
            session = self.frame_sessions.get(session_id)
            if not session or session.get("status") != "ready":
                raise ValueError("Generate frame previews before exporting.")
            if (session.get("export") or {}).get("status") == "exporting":
                raise RuntimeError("A frame export is already running for this video.")
            total_available = int(session["frame_count"])
            fps = float(session["fps"])
            duration = float(session["end"] - session["start"])
            if export_all:
                selected = list(range(total_available))
            else:
                try:
                    selected = sorted({int(x) for x in indices})
                except (ValueError, TypeError):
                    raise ValueError("The selected frame list is invalid.")
                if not selected:
                    raise ValueError("Select at least one frame first.")
                if selected[0] < 0 or selected[-1] >= total_available:
                    raise ValueError("One or more selected frames are out of range.")
            if len(selected) > MAX_FRAME_EXPORT:
                raise ValueError(f"The export limit is {MAX_FRAME_EXPORT:,} frames per ZIP. Reduce FPS or narrow the time range.")
            if not export_all and len(selected) > MAX_SELECTED_FRAME_EXPORT:
                raise ValueError(
                    f"A custom selection is limited to {MAX_SELECTED_FRAME_EXPORT:,} frames. "
                    "Use Extract Full Video for a complete range at this FPS."
                )
            export_id = uuid.uuid4().hex
            export_dir = Path(session["session_dir"]) / f"export_{export_id}"
            export_dir.mkdir(parents=True, exist_ok=True)
            export = {
                "id": export_id, "status": "exporting", "progress": 0.0,
                "detail": f"Exporting {len(selected):,} full-resolution frame(s)…", "error": "",
                "frame_count": len(selected), "filename": "", "zip_path": "", "cancel_requested": False,
            }
            session["export"] = export
            snapshot = {
                "session_id": session_id, "export_id": export_id, "selected": selected,
                "export_all": bool(export_all), "format": image_format, "fps": fps,
                "direct_export": bool(session.get("direct_export")),
                "start": float(session["start"]), "length": duration,
                "source_path": session["source_path"], "export_dir": str(export_dir),
            }
        threading.Thread(target=self._run_frame_export, args=(snapshot,), daemon=True).start()
        self.log(f"Frame export started · {len(selected):,} frame(s) · {image_format.upper()}")
        return {"id": export_id, "frame_count": len(selected), "status": "exporting"}

    def _run_frame_export(self, job: dict) -> None:
        session_id, export_id = job["session_id"], job["export_id"]
        export_dir = Path(job["export_dir"])
        ext = job["format"]
        try:
            fps = float(job["fps"])
            vf = f"fps={fps:.8f}"
            selected = job["selected"]
            if not job["export_all"]:
                # n refers to frames after the fps filter, matching the preview index sequence.
                expr = "+".join(f"eq(n\\,{i})" for i in selected)
                vf += f",select={expr}"
            out_pattern = str(export_dir / f"frame_%06d.{ext}")
            filter_script = None
            cmd = [self.ffmpeg or "ffmpeg", "-hide_banner", "-loglevel", "error", "-progress", "pipe:1", "-nostats",
                   "-ss", f"{job['start']:.3f}", "-i", job["source_path"], "-t", f"{job['length']:.3f}"]
            if not job["export_all"]:
                filter_script = export_dir / "select_filter.txt"
                filter_script.write_text(vf, encoding="utf-8")
                cmd += ["-filter_script:v", str(filter_script)]
            else:
                cmd += ["-vf", vf]
            cmd += ["-fps_mode", "vfr"]
            if not job.get("direct_export"):
                cmd += ["-frames:v", str(len(selected))]
            if ext == "jpg":
                cmd += ["-q:v", "1"]
            else:
                cmd += ["-compression_level", "3"]
            cmd += ["-start_number", "1", out_pattern]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, encoding="utf-8", errors="replace", bufsize=1, creationflags=flags)
            with self.lock:
                self.frame_process = proc
            assert proc.stdout is not None
            ffmpeg_tail = []
            for raw in proc.stdout:
                line = raw.strip()
                if line and not line.startswith(("frame=", "fps=", "stream_")) and "=" not in line:
                    ffmpeg_tail.append(line[:500])
                    ffmpeg_tail[:] = ffmpeg_tail[-10:]
                if line.startswith("frame="):
                    try:
                        current = int(line.split("=", 1)[1])
                        with self.lock:
                            current_session = self.frame_sessions.get(session_id, {})
                            exp = current_session.get("export") or {}
                            if exp.get("id") == export_id:
                                exp["progress"] = min(98.0, current / max(1, len(selected)) * 96.0)
                                exp["detail"] = f"Rendering full-resolution frames · {current:,}/{len(selected):,}"
                    except ValueError:
                        pass
                with self.lock:
                    current_session = self.frame_sessions.get(session_id, {})
                    exp = current_session.get("export") or {}
                    cancelled = bool(exp.get("id") == export_id and exp.get("cancel_requested"))
                if cancelled:
                    self._terminate(proc)
                    break
            code = proc.wait()
            with self.lock:
                self.frame_process = None
                current_session = self.frame_sessions.get(session_id, {})
                exp = current_session.get("export") or {}
                cancelled = bool(exp.get("id") == export_id and exp.get("cancel_requested"))
            if cancelled:
                with self.lock:
                    if exp.get("id") == export_id:
                        exp.update(status="cancelled", detail="Frame export cancelled.")
                return
            if code != 0:
                detail = "\n".join(ffmpeg_tail[-6:]) or f"No FFmpeg diagnostic was returned (exit code {code})."
                raise RuntimeError(f"FFmpeg failed while rendering frames (exit code {code}): {detail}")
            outputs = sorted(export_dir.glob(f"frame_*.{ext}"))
            if job.get("direct_export"):
                if not outputs:
                    raise RuntimeError("FFmpeg did not produce any frames for the direct export.")
                if len(outputs) > MAX_FRAME_EXPORT:
                    raise RuntimeError(f"FFmpeg produced {len(outputs):,} frames, above the {MAX_FRAME_EXPORT:,}-frame ZIP limit.")
                # Direct mode does not have preview indexes; use the actual rendered count.
                selected = list(range(len(outputs)))
            elif len(outputs) != len(selected):
                raise RuntimeError(f"Expected {len(selected)} frame(s), but FFmpeg produced {len(outputs)}. Try again or reduce FPS.")
            with zipfile.ZipFile(export_dir / "frames.zip", "w", compression=zipfile.ZIP_STORED) as archive:
                for sequence, (path, frame_index) in enumerate(zip(outputs, selected), 1):
                    timestamp = job["start"] + frame_index / fps
                    label = self._frame_time(timestamp).replace(":", "-").replace(".", "_")
                    archive.write(path, f"frame_{sequence:06d}_{label}.{ext}")
            zip_path = export_dir / "frames.zip"
            filename = f"Frex_Frames_{len(selected)}_{export_id[:6]}.zip"
            # Move the finished archive into the session root so the download endpoint has a stable path.
            final_zip = Path(self.frame_sessions[session_id]["session_dir"]) / filename
            shutil.move(str(zip_path), str(final_zip))
            with self.lock:
                exp = self.frame_sessions[session_id].get("export") or {}
                if exp.get("id") == export_id:
                    exp.update(status="complete", progress=100.0, frame_count=len(outputs),
                               detail=f"Ready · {len(outputs):,} full-quality frames",
                               filename=filename, zip_path=str(final_zip), error="")
            self.log(f"Frame export ready · {len(selected):,} frame(s) · {filename}")
        except Exception as exc:
            with self.lock:
                self.frame_process = None
                session = self.frame_sessions.get(session_id, {})
                exp = session.get("export") or {}
                if exp.get("id") == export_id:
                    exp.update(status="failed", detail="Frame export failed", error=str(exc)[:1600])
            self.log(f"Frame export failed · {str(exc)[:250]}")

    @staticmethod
    def _safe_archive_name(value: str, fallback="video") -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
        value = re.sub(r"_+", "_", value)
        return (value[:72] or fallback)

    def start_frame_batch(self, urls, fps_value, start_value="", end_value="", image_format="png", options=None) -> dict:
        """Queue frame extraction sequentially and package one structured archive."""
        options = options or {}
        if not isinstance(urls, list):
            raise ValueError("Provide a list of video URLs, one per line.")
        clean, seen = [], set()
        for raw in urls:
            url = str(raw).strip()
            if not url:
                continue
            if not valid_url(url):
                raise ValueError(f"Invalid video URL: {url[:160]}")
            if url not in seen:
                clean.append(url)
                seen.add(url)
        if not clean:
            raise ValueError("Paste at least one video URL.")
        if not self.ffmpeg:
            raise RuntimeError("FFmpeg is required for batch frame extraction. Install FFmpeg or repair Frex's imageio-ffmpeg component.")
        if not (self.ytdlp or self.ytdlp_module):
            raise RuntimeError("yt-dlp is missing. Use Install / update yt-dlp first.")
        if len(clean) > MAX_FRAME_BATCH_URLS:
            raise ValueError(f"Batch frame extraction accepts up to {MAX_FRAME_BATCH_URLS} videos at a time.")
        image_format = str(image_format).lower()
        if image_format not in ("png", "jpg"):
            raise ValueError("Choose PNG or JPG output.")
        try:
            fps = float(fps_value)
        except (TypeError, ValueError):
            raise ValueError("Enter a valid FPS value.")
        if not math.isfinite(fps) or fps < 0.1 or fps > 30:
            raise ValueError("Custom FPS must be between 0.1 and 30.")
        if options.get("advanced") and options.get("auth_source") not in (None, "No login") and not options.get("auth_confirmed"):
            raise ValueError("Confirm Advanced account access before using a saved browser session or cookies file.")
        with self.lock:
            if self.running:
                raise RuntimeError("Finish or cancel the current download queue before starting frame extraction.")
            if any(s.get("status") in ("downloading", "previewing") or (s.get("export") or {}).get("status") == "exporting" for s in self.frame_sessions.values()):
                raise RuntimeError("Finish or cancel the current frame task before starting a batch.")
            if any(b.get("status") in ("queued", "processing", "packaging") for b in self.frame_batches.values()):
                raise RuntimeError("A batch frame extraction is already running.")
        batch_id = uuid.uuid4().hex
        batch_dir = FRAME_CACHE / f"batch_{batch_id}"
        batch_dir.mkdir(parents=True, exist_ok=True)
        batch = {
            "id": batch_id, "status": "queued", "progress": 0.0,
            "detail": f"Queued {len(clean)} video(s)", "error": "", "items": [
                {"index": i + 1, "url": url, "title": "", "status": "queued", "progress": 0.0,
                 "detail": "Waiting in queue", "frame_count": 0, "folder": "", "error": ""}
                for i, url in enumerate(clean)
            ], "total": len(clean), "completed": 0, "failed": 0, "cancel_requested": False,
            "fps": fps, "start": str(start_value or "").strip(), "end": str(end_value or "").strip(),
            "format": image_format, "options": dict(options), "batch_dir": str(batch_dir),
            "zip_path": "", "filename": "", "created_at": time.time(), "current_session_id": "",
        }
        with self.lock:
            self.frame_batches[batch_id] = batch
        threading.Thread(target=self._run_frame_batch, args=(batch_id,), daemon=True).start()
        self.log(f"Batch frame extraction queued · {len(clean)} video(s) · {fps:g} FPS")
        return {"id": batch_id, "total": len(clean), "status": "queued", "detail": batch["detail"]}

    def _batch_update(self, batch_id: str, **updates) -> None:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if batch:
                batch.update(updates)

    def _batch_item_update(self, batch_id: str, item_index: int, **updates) -> None:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if batch and 0 <= item_index < len(batch["items"]):
                batch["items"][item_index].update(updates)
                completed = sum(1 for x in batch["items"] if x.get("status") == "complete")
                failed = sum(1 for x in batch["items"] if x.get("status") in ("failed", "skipped", "cancelled"))
                batch["completed"], batch["failed"] = completed, failed

    def frame_batch_state(self, batch_id: str) -> dict:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if not batch:
                raise ValueError("Batch not found. Start the batch again.")
            return {
                "id": batch["id"], "status": batch["status"], "progress": batch["progress"],
                "detail": batch["detail"], "error": batch["error"], "total": batch["total"],
                "completed": batch["completed"], "failed": batch["failed"],
                "items": [dict({k: v for k, v in item.items() if k != "url"}) for item in batch["items"]],
                "filename": batch["filename"], "download_ready": bool(batch["zip_path"] and Path(batch["zip_path"]).is_file()),
            }

    def _run_frame_batch(self, batch_id: str) -> None:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if not batch:
                return
            batch["status"] = "processing"
        batch_dir = Path(batch["batch_dir"])
        archive_tmp = batch_dir / "batch.tmp.zip"
        manifest_items = []
        total_frames = 0
        any_success = False
        try:
            with zipfile.ZipFile(archive_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                for item_index in range(batch["total"]):
                    with self.lock:
                        batch = self.frame_batches[batch_id]
                        if batch.get("cancel_requested"):
                            break
                        item = batch["items"][item_index]
                    url = item["url"]
                    self._batch_item_update(batch_id, item_index, status="processing", detail="Inspecting source and preparing previews")
                    self._batch_update(batch_id, detail=f"Processing video {item_index + 1} of {batch['total']}")
                    try:
                        session_info = self.start_frame_preview(
                            url, batch["fps"], batch["start"], batch["end"], batch.get("options"), allow_batch=True
                        )
                        session_id = session_info["id"]
                        self._batch_update(batch_id, current_session_id=session_id)
                        while True:
                            with self.lock:
                                cancel = bool(self.frame_batches[batch_id].get("cancel_requested"))
                            session = self.frame_session_state(session_id)
                            self._batch_item_update(batch_id, item_index, title=session.get("title", ""),
                                progress=float(session.get("progress") or 0), detail=session.get("detail") or session.get("status"))
                            overall = (item_index + min(0.95, float(session.get("progress") or 0) / 100.0 * 0.75)) / batch["total"] * 100
                            self._batch_update(batch_id, progress=round(overall, 1))
                            if cancel:
                                self.cancel_frame_task(session_id)
                                self._batch_item_update(batch_id, item_index, status="cancelled", detail="Cancelled by user")
                                break
                            if session["status"] in ("failed", "cancelled"):
                                raise RuntimeError(session.get("error") or session.get("detail") or "Frame preparation failed")
                            if session["status"] == "ready":
                                break
                            time.sleep(0.4)
                        with self.lock:
                            cancelled = bool(self.frame_batches[batch_id].get("cancel_requested"))
                        if cancelled:
                            break
                        frame_count = int(session.get("frame_count") or 0)
                        if frame_count <= 0:
                            raise RuntimeError("No frames were generated for this video.")
                        if total_frames + frame_count > MAX_FRAME_BATCH_TOTAL:
                            raise RuntimeError(
                                f"Batch safety cap reached ({MAX_FRAME_BATCH_TOTAL:,} frames total). "
                                "Reduce FPS or shorten the time ranges, then split the batch."
                            )
                        self._batch_item_update(batch_id, item_index, status="exporting", frame_count=frame_count,
                                                 detail=f"Exporting {frame_count:,} source-resolution frames")
                        export = self.start_frame_export(session_id, [], True, batch["format"], allow_batch=True)
                        export_id = export["id"]
                        while True:
                            with self.lock:
                                cancel = bool(self.frame_batches[batch_id].get("cancel_requested"))
                            session = self.frame_session_state(session_id)
                            exp = session.get("export") or {}
                            self._batch_item_update(batch_id, item_index, progress=float(exp.get("progress") or 0),
                                detail=exp.get("detail") or "Exporting frames")
                            overall = ((item_index + 0.75 + min(1.0, float(exp.get("progress") or 0) / 100.0) * 0.25) / batch["total"]) * 100
                            self._batch_update(batch_id, progress=round(overall, 1))
                            if cancel:
                                self.cancel_frame_task(session_id)
                                self._batch_item_update(batch_id, item_index, status="cancelled", detail="Cancelled by user")
                                break
                            if exp.get("status") in ("failed", "cancelled"):
                                raise RuntimeError(exp.get("error") or exp.get("detail") or "Frame export failed")
                            if exp.get("status") == "complete":
                                break
                            time.sleep(0.4)
                        with self.lock:
                            cancelled = bool(self.frame_batches[batch_id].get("cancel_requested"))
                        if cancelled:
                            break
                        frame_zip, _ = self.frame_download_path(session_id, export_id)
                        session_full = self.frame_sessions[session_id]
                        folder = f"{item_index + 1:02d}_{self._safe_archive_name(session_full.get('title'), 'video')}"
                        with zipfile.ZipFile(frame_zip, "r") as per_video:
                            for member in per_video.infolist():
                                if member.is_dir():
                                    continue
                                archive.writestr(f"{folder}/frames/{Path(member.filename).name}", per_video.read(member.filename))
                        metadata = {
                            "title": session_full.get("title"), "uploader": session_full.get("uploader"),
                            "source_url": url, "duration_seconds": session_full.get("duration"),
                            "duration": session_full.get("duration_text"), "source_width": session_full.get("width"),
                            "source_height": session_full.get("height"), "requested_fps": session_full.get("requested_fps"),
                            "effective_fps": session_full.get("fps"), "start_seconds": session_full.get("start"),
                            "end_seconds": session_full.get("end"), "frame_count": frame_count,
                            "image_format": batch["format"].upper(), "folder": folder,
                        }
                        archive.writestr(f"{folder}/metadata.json", json.dumps(metadata, ensure_ascii=False, indent=2))
                        manifest_items.append({"index": item_index + 1, "status": "complete", **metadata})
                        total_frames += frame_count
                        any_success = True
                        self._batch_item_update(batch_id, item_index, status="complete", progress=100,
                            title=session_full.get("title"), frame_count=frame_count, folder=folder,
                            detail=f"Complete · {frame_count:,} frames")
                    except Exception as exc:
                        with self.lock:
                            cancelled = bool(self.frame_batches[batch_id].get("cancel_requested"))
                        label = "cancelled" if cancelled else "failed"
                        self._batch_item_update(batch_id, item_index, status=label, error=str(exc)[:1000], detail=str(exc)[:180])
                        manifest_items.append({"index": item_index + 1, "status": label, "source_url": url,
                            "title": item.get("title") or "", "error": str(exc)[:1000], "frame_count": 0})
                        if cancelled:
                            break
                        self.log(f"Batch frame item {item_index + 1} failed · {str(exc)[:200]}")
                # The archive remains structured even when some videos fail or the user cancels midway.
                manifest = {
                    "app": "Frex", "schema_version": 1, "created_at": datetime.now().astimezone().isoformat(),
                    "status": "partial" if any(x.get("status") != "complete" for x in manifest_items) else "complete",
                    "total_videos": batch["total"], "completed_videos": sum(x.get("status") == "complete" for x in manifest_items),
                    "total_frames": total_frames, "fps_setting": batch["fps"], "image_format": batch["format"].upper(),
                    "range_start": batch["start"] or None, "range_end": batch["end"] or None, "videos": manifest_items,
                }
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                csv_buffer = io.StringIO(newline="")
                fields = ["index", "status", "title", "uploader", "source_url", "folder", "frame_count", "effective_fps", "image_format", "error"]
                writer = csv.DictWriter(csv_buffer, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for row in manifest_items:
                    writer.writerow(row)
                archive.writestr("manifest.csv", csv_buffer.getvalue())
                readme = (
                    "Frex Batch Frame Export\n\n"
                    "Each numbered folder contains a frames/ directory with source-resolution images and a metadata.json file.\n"
                    "manifest.json contains the complete batch metadata; manifest.csv is convenient for spreadsheets.\n"
                    "Frames are sampled at the effective FPS recorded per video. Timestamps are encoded in image filenames.\n"
                    "Only content your account is authorized to access can be processed. Frex does not bypass DRM or access controls.\n"
                )
                archive.writestr("README.txt", readme)
            filename = f"Frex_Batch_Frames_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            final_path = batch_dir / filename
            shutil.move(str(archive_tmp), str(final_path))
            with self.lock:
                batch = self.frame_batches[batch_id]
                cancelled = bool(batch.get("cancel_requested"))
                batch.update(zip_path=str(final_path) if any_success else "", filename=filename if any_success else "",
                    progress=100.0, status="cancelled" if cancelled else ("complete" if any_success and batch["failed"] == 0 else ("partial" if any_success else "failed")),
                    detail=f"Finished · {sum(x.get('status') == 'complete' for x in manifest_items)} of {batch['total']} videos · {total_frames:,} frames",
                    error="" if any_success else "No videos produced an export. Check the per-video errors.")
            self.log(f"Batch frame export ready · {total_frames:,} frames · {filename if any_success else 'no output'}")
        except Exception as exc:
            with self.lock:
                batch = self.frame_batches.get(batch_id, {})
                batch.update(status="failed", error=str(exc)[:1600], detail="Batch frame extraction failed")
            self.log(f"Batch frame extraction failed · {str(exc)[:250]}")

    def cancel_frame_batch(self, batch_id: str) -> None:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if not batch:
                raise ValueError("Batch not found.")
            batch["cancel_requested"] = True
            session_id = batch.get("current_session_id")
        if session_id:
            self.cancel_frame_task(session_id)
        self.log("Batch frame cancellation requested.")

    def frame_batch_download_path(self, batch_id: str) -> tuple[Path, str]:
        with self.lock:
            batch = self.frame_batches.get(batch_id)
            if not batch or not batch.get("zip_path"):
                raise ValueError("Batch archive is not ready yet.")
            path = Path(batch["zip_path"])
            filename = str(batch.get("filename") or "Frex_Batch_Frames.zip")
        if not path.is_file():
            raise ValueError("Batch archive was not found. Run the batch again.")
        return path, filename

    def cancel_frame_task(self, session_id: str) -> None:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if not session:
                return
            if session.get("status") in ("downloading", "previewing"):
                session["cancel_requested"] = True
            exp = session.get("export") or {}
            if exp.get("status") == "exporting":
                exp["cancel_requested"] = True
            proc = self.frame_process
        if proc and proc.poll() is None:
            self._terminate(proc)
        self.log("Frame task cancellation requested.")

    def frame_download_path(self, session_id: str, export_id: str) -> tuple[Path, str]:
        with self.lock:
            session = self.frame_sessions.get(session_id)
            if not session:
                raise ValueError("Frame session not found.")
            export = session.get("export") or {}
            if export.get("id") != export_id or export.get("status") != "complete":
                raise ValueError("The frame ZIP is not ready yet.")
            path = Path(export.get("zip_path", ""))
            filename = str(export.get("filename") or "Frex_Frames.zip")
        if not path.is_file():
            raise ValueError("The exported frame ZIP no longer exists. Export the frames again.")
        return path, filename

    def start(self, urls: list[str], options: dict) -> dict:
        clean: list[str] = []
        seen: set[str] = set()
        for raw in urls:
            url = str(raw).strip()
            if not url:
                continue
            if not valid_url(url):
                raise ValueError(f"Invalid link: {url[:150]}")
            if url not in seen:
                clean.append(url)
                seen.add(url)
        if not clean:
            raise ValueError("Add at least one valid video link.")
        if len(clean) > MAX_BATCH:
            raise ValueError(f"Frex accepts up to {MAX_BATCH} links per batch.")
        if not (self.ytdlp or self.ytdlp_module):
            raise RuntimeError("yt-dlp is missing. Install it first using the button in Frex.")

        mode = options.get("mode", "Video")
        container = options.get("container", "MP4")
        quality = options.get("quality", "Best available")
        folder = str(Path(str(options.get("folder") or self.folder)).expanduser()).strip()
        if not folder:
            raise ValueError("Choose a destination folder.")
        Path(folder).expanduser().mkdir(parents=True, exist_ok=True)

        trim_enabled = bool(options.get("trim_enabled"))
        trim_start = trim_end = None
        if trim_enabled:
            if not self.ffmpeg:
                raise RuntimeError("FFmpeg is required for trimming. Install FFmpeg and add it to PATH.")
            trim_start = seconds_from_time(options.get("trim_start", "0"))
            trim_end = seconds_from_time(options.get("trim_end", "30"))
            if trim_end <= trim_start:
                raise ValueError("Trim end must be later than trim start.")

        # Validate authentication now, before any job enters the queue.
        auth_args = self.auth_args(options)
        if options.get("advanced") and options.get("auth_source") not in (None, "No login"):
            if not options.get("auth_confirmed"):
                raise ValueError("Confirm that you understand browser sessions and cookie files are sensitive before using Advanced account access.")

        self.folder, self.mode, self.quality, self.container = folder, mode, quality, container
        self._save_settings()
        with self.lock:
            if self.running:
                raise RuntimeError("A batch is already running. Wait for it to finish or cancel it first.")
            self.running = True
            self.cancel_requested = False
            self.jobs = []
            for index, url in enumerate(clean, 1):
                self.jobs.append({"id": f"{int(time.time()*1000)}-{index}", "index": index, "url": url,
                                  "state": "queued", "progress": 0, "detail": "Waiting", "title": "", "error": ""})
        self.log(f"Batch started · {len(clean)} link(s) · {mode} · {quality} · {container}")
        worker = threading.Thread(target=self._run_batch, args=(clean, dict(options), auth_args, folder, trim_start, trim_end), daemon=True)
        worker.start()
        return {"count": len(clean)}

    def _download_args(self, url: str, options: dict, auth_args: list[str], folder: str,
                       trim_start: int | None, trim_end: int | None) -> list[str]:
        args = self.ytdlp_command() + ["--newline", "--progress", "--no-playlist", "--socket-timeout", "20",
                "--retries", "5", "--fragment-retries", "10", "--file-access-retries", "3",
                "--concurrent-fragments", "4", "--windows-filenames", "-P", folder,
                "-o", "%(title).180B [%(id)s].%(ext)s"]
        args += self.runtime_args() + auth_args
        if self.ffmpeg:
            args += ["--ffmpeg-location", str(Path(self.ffmpeg).parent)]
        mode = options.get("mode", "Video")
        container = options.get("container", "MP4")
        quality = options.get("quality", "Best available")
        if mode == "Audio only":
            audio_fmt = container.lower()
            if audio_fmt not in ("mp3", "m4a", "wav", "flac", "opus"):
                audio_fmt = "mp3"
            args += ["-f", "ba/b", "-x", "--audio-format", audio_fmt]
            if audio_fmt == "mp3":
                args += ["--audio-quality", "0"]
        else:
            if quality == "Best available":
                args += ["-f", "bv*+ba/b"]
            else:
                match = re.search(r"\d+", str(quality))
                if match:
                    h = match.group(0)
                    args += ["-f", f"bv*[height<={h}]+ba/b[height<={h}]"]
            if container.lower() in ("mp4", "mkv", "webm"):
                args += ["--merge-output-format", container.lower()]
        if trim_start is not None and trim_end is not None:
            args += ["--download-sections", f"*{trim_start}-{trim_end}"]
            if options.get("trim_mode") == "Precise":
                args += ["--force-keyframes-at-cuts"]
        args.append(url)
        return args

    def _run_batch(self, urls: list[str], options: dict, auth_args: list[str], folder: str,
                   trim_start: int | None, trim_end: int | None) -> None:
        try:
            for idx, url in enumerate(urls):
                with self.lock:
                    if self.cancel_requested:
                        break
                    job = self.jobs[idx]
                    job["state"] = "downloading"
                    job["detail"] = "Starting downloader…"
                self.log(f"Downloading item {idx + 1} of {len(urls)}")
                args = self._download_args(url, options, auth_args, folder, trim_start, trim_end)
                creationflags = 0
                if os.name == "nt":
                    creationflags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                tail: list[str] = []
                try:
                    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, encoding="utf-8", errors="replace", bufsize=1,
                                            universal_newlines=True, creationflags=creationflags)
                    with self.lock:
                        self.process = proc
                    assert proc.stdout is not None
                    for raw in proc.stdout:
                        line = raw.strip()
                        if not line:
                            continue
                        pct = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
                        with self.lock:
                            job = self.jobs[idx]
                            if pct:
                                job["progress"] = min(100, float(pct.group(1)))
                                eta = re.search(r"ETA\s+([0-9:]+)", line)
                                speed = re.search(r" at\s+(.+?)(?:\s+ETA|$)", line)
                                detail = f"{job['progress']:.1f}%"
                                if speed:
                                    detail += " · " + speed.group(1).strip()
                                if eta:
                                    detail += " · ETA " + eta.group(1)
                                job["detail"] = detail
                            low = line.lower()
                            if any(t in low for t in ("error", "failed", "unable to", "warning")):
                                tail.append(line[:500])
                                tail[:] = tail[-8:]
                            if "destination:" in low:
                                job["detail"] = "Writing file…"
                            if "merging formats" in low or "post-process" in low:
                                job["detail"] = "Finalizing media…"
                        if self.cancel_requested:
                            self._terminate(proc)
                            break
                    code = proc.wait()
                except Exception as exc:
                    code = 1
                    tail.append(str(exc))
                finally:
                    with self.lock:
                        self.process = None
                with self.lock:
                    job = self.jobs[idx]
                    if self.cancel_requested:
                        job["state"] = "cancelled"
                        job["detail"] = "Cancelled"
                    elif code == 0:
                        job["state"] = "complete"
                        job["progress"] = 100
                        job["detail"] = "Download complete"
                    else:
                        job["state"] = "failed"
                        job["detail"] = "Download failed"
                        job["error"] = self.friendly_error("\n".join(tail[-5:]) or f"yt-dlp exited with code {code}")[:1600]
                if code == 0:
                    self.log(f"Item {idx + 1} completed.")
                elif not self.cancel_requested:
                    self.log(f"Item {idx + 1} failed; continuing with the next item.")
            with self.lock:
                if self.cancel_requested:
                    for job in self.jobs:
                        if job["state"] == "queued":
                            job["state"] = "cancelled"
                            job["detail"] = "Cancelled"
                self.running = False
                self.process = None
            complete = sum(1 for j in self.jobs if j["state"] == "complete")
            failed = sum(1 for j in self.jobs if j["state"] == "failed")
            cancelled = sum(1 for j in self.jobs if j["state"] == "cancelled")
            self.log(f"Batch finished · {complete} complete · {failed} failed · {cancelled} cancelled")
        except Exception as exc:
            self.log(f"Batch error: {exc}")
            with self.lock:
                self.running = False
                self.process = None

    def _terminate(self, proc: subprocess.Popen) -> None:
        try:
            if os.name == "nt":
                subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def cancel(self) -> None:
        with self.lock:
            self.cancel_requested = True
            proc = self.process
        if proc and proc.poll() is None:
            self._terminate(proc)
        self.log("Cancellation requested. Current item will stop and queued items will be cancelled.")

    def state(self) -> dict:
        with self.lock:
            return {"running": self.running, "cancel_requested": self.cancel_requested,
                    "jobs": json.loads(json.dumps(self.jobs)), "logs": list(self.logs)}

    def choose_folder(self) -> str | None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(parent=root, initialdir=self.folder if Path(self.folder).exists() else str(Path.home()))
            root.destroy()
            if selected:
                self.folder = selected
                self._save_settings()
            return selected or None
        except Exception as exc:
            self.log(f"Folder picker unavailable: {exc}")
            return None

    def choose_cookies(self) -> str | None:
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askopenfilename(parent=root, title="Select a Netscape-format cookies.txt file",
                                                  filetypes=[("Cookie text files", "*.txt"), ("All files", "*.*")])
            root.destroy()
            if selected:
                self.cookie_file = selected
                self.log("Cookies file selected. Frex does not read or log its contents.")
            return selected or None
        except Exception as exc:
            self.log(f"Cookie picker unavailable: {exc}")
            return None

    def install_or_update(self) -> dict:
        self.log("Starting yt-dlp install/update using this Python interpreter…")
        result = subprocess.run([PYTHON_EXECUTABLE, "-m", "pip", "install", "--upgrade", "yt-dlp"],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.ytdlp = locate("yt-dlp.exe", "yt-dlp")
        self.ytdlp_module = False
        if not self.ytdlp:
            try:
                subprocess.run([PYTHON_EXECUTABLE, "-m", "yt_dlp", "--version"], check=True,
                               capture_output=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.ytdlp_module = True
            except Exception:
                pass
        available = bool(self.ytdlp or self.ytdlp_module)
        return {"ok": result.returncode == 0 and available,
                "message": (result.stdout + "\n" + result.stderr)[-1400:],
                "ytdlp": self.ytdlp or ("Python module" if self.ytdlp_module else None)}


SERVICE = FrexService()


class Handler(BaseHTTPRequestHandler):
    server_version = "FrexLocal/3.2.0"

    def log_message(self, fmt, *args):
        # Keep access logs quiet; they may contain request paths with URL parameters.
        return

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        if size <= 0 or size > MAX_BODY:
            raise ValueError("Missing or oversized request body.")
        data = json.loads(self.rfile.read(size).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object.")
        return data

    def _local_request(self) -> bool:
        host = self.headers.get("Host", "").split(":")[0].strip("[]").lower()
        if host not in ("127.0.0.1", "localhost"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            parsed = urlparse(origin)
            if parsed.hostname not in ("127.0.0.1", "localhost"):
                return False
        return True

    def do_GET(self):
        if not self._local_request():
            return self._send(403, {"error": "Local requests only."})
        if self.path == "/" or self.path.startswith("/index"):
            if not WEB_FILE.is_file():
                return self._send(500, "Frex_3_1.html is missing beside Frex_3_1.py", "text/plain; charset=utf-8")
            return self._send(200, WEB_FILE.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/status":
            return self._send(200, SERVICE.status())
        if self.path == "/api/state":
            return self._send(200, SERVICE.state())
        parsed = urlparse(self.path)
        if parsed.path == "/api/frames/state":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            return self._send(200, SERVICE.frame_session_state((q.get("id") or [""])[0]))
        if parsed.path == "/api/frames/page":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            return self._send(200, SERVICE.frame_page((q.get("id") or [""])[0], (q.get("offset") or [0])[0], (q.get("limit") or [FRAME_PAGE_SIZE])[0]))
        if parsed.path == "/api/frames/image":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            path = SERVICE.frame_image_path((q.get("id") or [""])[0], int((q.get("index") or [-1])[0]))
            return self._send(200, path.read_bytes(), "image/jpeg")
        if parsed.path == "/api/frames/batch/state":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            return self._send(200, SERVICE.frame_batch_state((q.get("id") or [""])[0]))
        if parsed.path == "/api/frames/batch/download":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            path, filename = SERVICE.frame_batch_download_path((q.get("id") or [""])[0])
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
            return
        if parsed.path == "/api/frames/download":
            from urllib.parse import parse_qs
            q = parse_qs(parsed.query)
            path, filename = SERVICE.frame_download_path((q.get("id") or [""])[0], (q.get("export") or [""])[0])
            size = path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            with path.open("rb") as stream:
                shutil.copyfileobj(stream, self.wfile, length=1024 * 1024)
            return
        if parsed.path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._send(404, {"error": "Not found"})

    def do_POST(self):
        if not self._local_request():
            return self._send(403, {"error": "Local requests only."})
        try:
            data = self._json_body() if self.path not in ("/api/cancel", "/api/pick-folder", "/api/pick-cookies", "/api/install") else {}
            if self.path == "/api/inspect":
                result = SERVICE.inspect(str(data.get("url", "")), data.get("options") or {})
                return self._send(200, result)
            if self.path == "/api/start":
                result = SERVICE.start(data.get("urls") or [], data.get("options") or {})
                return self._send(200, {"ok": True, **result})
            if self.path == "/api/frames/batch/start":
                result = SERVICE.start_frame_batch(data.get("urls") or [], data.get("fps", 1),
                    data.get("start", ""), data.get("end", ""), data.get("format", "png"), data.get("options") or {})
                return self._send(200, {"ok": True, **result})
            if self.path == "/api/frames/batch/cancel":
                SERVICE.cancel_frame_batch(str(data.get("id", "")))
                return self._send(200, {"ok": True})
            if self.path == "/api/frames/prepare":
                result = SERVICE.start_frame_preview(
                    str(data.get("url", "")), data.get("fps", 1),
                    data.get("start", ""), data.get("end", ""), data.get("options") or {},
                    direct_export=bool(data.get("direct_export")),
                    image_format=data.get("format", "png")
                )
                return self._send(200, {"ok": True, **result})
            if self.path == "/api/frames/export":
                result = SERVICE.start_frame_export(
                    str(data.get("id", "")), data.get("indices") or [],
                    bool(data.get("all_frames")), data.get("format", "png")
                )
                return self._send(200, {"ok": True, **result})
            if self.path == "/api/frames/cancel":
                SERVICE.cancel_frame_task(str(data.get("id", "")))
                return self._send(200, {"ok": True})
            if self.path == "/api/cancel":
                SERVICE.cancel()
                return self._send(200, {"ok": True})
            if self.path == "/api/pick-folder":
                folder = SERVICE.choose_folder()
                return self._send(200, {"folder": folder or SERVICE.folder})
            if self.path == "/api/pick-cookies":
                path = SERVICE.choose_cookies()
                return self._send(200, {"selected": bool(path), "label": Path(path).name if path else "No cookies file selected"})
            if self.path == "/api/install":
                return self._send(200, SERVICE.install_or_update())
            return self._send(404, {"error": "Not found"})
        except subprocess.TimeoutExpired:
            return self._send(504, {"error": "The source took too long to respond. Try again later."})
        except Exception as exc:
            return self._send(400, {"error": str(exc)[:2000]})


def find_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _show_startup_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showerror("Frex could not start", message, parent=root)
        root.destroy()
    except Exception:
        # pythonw has no console; keep a diagnostic log for support.
        try:
            APPDATA.mkdir(parents=True, exist_ok=True)
            (APPDATA / "frex-startup-error.log").write_text(message, encoding="utf-8")
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--desktop", action="store_true")
    args, _ = parser.parse_known_args()

    if not WEB_FILE.is_file():
        _show_startup_error(f"Frex interface file is missing:\n{WEB_FILE}")
        return

    port = find_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://127.0.0.1:{port}/"
    SERVICE.log("Frex 3.2.0 desktop service started. Processing runs locally.")
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.4}, daemon=True)
    server_thread.start()

    if args.desktop:
        try:
            import webview
            window = webview.create_window(
                "Frex — Video & Frame Studio", url,
                width=1440, height=960, min_size=(1024, 720),
                background_color="#050507", text_select=True,
            )
            try:
                window.events.closed += lambda: SERVICE.cancel()
            except Exception:
                pass
            webview.start(debug=False, private_mode=False)
        except ImportError:
            _show_startup_error(
                "Frex desktop components are missing.\n\n"
                "Run Setup_Frex.vbs again to repair the installation."
            )
        except Exception as exc:
            _show_startup_error(
                "Frex could not open its desktop window.\n\n"
                f"{exc}\n\n"
                "Check that Microsoft Edge WebView2 Runtime is installed, "
                "then run Setup_Frex.vbs to repair Frex."
            )
        finally:
            SERVICE.cancel()
            try:
                server.shutdown()
            except Exception:
                pass
            server.server_close()
        return

    # Developer fallback; the installed desktop shortcut always uses --desktop.
    threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.4)
    except KeyboardInterrupt:
        pass
    finally:
        SERVICE.cancel()
        server.server_close()


if __name__ == "__main__":
    main()
