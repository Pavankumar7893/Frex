# Frex

**Frex** is a Windows desktop video downloader and frame-extraction studio. It combines a local Python service with a dark WebView2 interface.

> Current project version: **3.2.0**  
> Platform: **Windows 10/11**  
> Processing: **local on your PC**

## Features

- Single-video downloads and sequential batch queues powered by `yt-dlp`.
- Video quality and output-format selection.
- Fast and precise trim options where supported by the source and FFmpeg.
- Opt-in advanced account/cookie controls. They remain disabled unless explicitly enabled in the UI.
- Frame Extractor with custom sampling rate (0.1–30 FPS), paginated previews, per-frame selection, select/deselect/invert, selected-frame export, and full-range extraction.
- PNG and high-quality JPG frame exports packaged as ZIP files.
- Batch Frame Studio: sequential extraction for up to 25 video URLs, with shared FPS/time-range/format settings and cancellation.
- Structured batch ZIP: numbered per-video folders, full-resolution frames, per-video `metadata.json`, plus batch `manifest.json` and `manifest.csv`; aggregate cap of 50,000 frames per batch.
- Native Windows desktop window using `pywebview` and Microsoft Edge WebView2.

## Install (no terminal required)

1. Download `Frex_Windows_Desktop_Installer.zip` from the latest GitHub Release.
2. In File Explorer, right-click the ZIP and choose **Extract All**.
3. Open the extracted folder and double-click `Setup_Frex.vbs`.
4. The setup checks for Python and WebView2, installs the required Python components, and creates Desktop and Start Menu shortcuts.
5. Launch Frex from its Desktop or Start Menu shortcut.

See [`installer/README_Frex.txt`](installer/README_Frex.txt) for troubleshooting and install details.

## Developer setup

Requirements: Windows 10/11, Python 3.10+, Microsoft Edge WebView2 Runtime, and an internet connection for Python packages / downloader updates.

```powershell
py -m pip install -r requirements.txt
py app/Frex_3_1.py --desktop
```

The installer is a VBScript wrapper for user-level setup; source files are in `app/` and installer files are in `installer/`.

## Runtime dependencies

- `pywebview` — native desktop window.
- `yt-dlp` — supported-source metadata and media downloads.
- `imageio-ffmpeg` — FFmpeg helper used by frame extraction if system FFmpeg is unavailable.
- Microsoft Edge WebView2 Runtime — Windows web rendering.

These dependencies are installed on the user's machine by the installer or via `requirements.txt` for development.

## Privacy and local processing

- The local application service binds to `127.0.0.1` (loopback); it is not intended as a network server.
- Media processing and frame generation happen locally.
- Account/cookie support is opt-in. Keep browser sessions and exported cookie files private. Frex does not intentionally write cookie contents to its activity log.
- Video URLs are passed to `yt-dlp`; the media host/platform may receive the requests required to retrieve the media.

## Limitations

- This is a Python-powered Windows desktop app, not a self-contained or publisher-signed installer executable.
- Download availability depends on `yt-dlp` support, source accessibility, and the user's rights/access to the media.
- Frame export is limited by source duration, FPS, available storage, and the app's built-in frame-count safeguards.
- Some trim / merge / format combinations require FFmpeg.

## Project status

This repository publishes the current 3.2.0 source and Windows installer bundle. The Android Studio project and Android release are not included in this repository version.

## License

This project is distributed under the MIT License. See [`LICENSE`](LICENSE). Third-party dependencies retain their own licenses.
