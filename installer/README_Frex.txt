FREX — WINDOWS DESKTOP APPLICATION
==================================

INSTALL FREX (NO COMMAND PROMPT)
--------------------------------
1. Download the Frex installer ZIP.
2. In File Explorer, right-click the ZIP and choose Extract All.
3. Open the extracted folder.
4. Double-click Setup_Frex.vbs.
5. Setup checks for Python and Microsoft Edge WebView2 Runtime, installs the required desktop components quietly, and creates Frex shortcuts.
6. Frex opens in its own Windows application window.

AFTER INSTALLATION
------------------
Use the green Frex icon on your Desktop or Start Menu. You do not need to open Command Prompt, a terminal, or a separate Chrome/Edge browser.

WHAT SETUP CONFIGURES
---------------------
- Frex desktop window powered by pywebview and Microsoft Edge WebView2.
- yt-dlp media engine.
- imageio-ffmpeg helper for video frame extraction when a system FFmpeg is not already available.
- Desktop and Start Menu shortcuts with the Frex icon.
- Uninstall shortcut in Start Menu > Frex.

REQUIREMENTS
------------
- Windows 10/11.
- Python 3.10 or newer. Setup checks common Python 3.10–3.13 locations and the Python 3.12 launcher.
- Internet connection for the one-time component installation and future yt-dlp updates.
- Microsoft Edge WebView2 Runtime. Setup checks for it and attempts to install Microsoft's Evergreen Runtime if missing.

FEATURES
--------
- Single video downloads and sequential batch queue.
- Quality and format selection.
- Fast / precise trim controls where supported by the selected media.
- Advanced account/cookie options, hidden until enabled.
- Frame Extractor with custom FPS from 0.1 to 30, paged previews, individual selection, select/deselect/invert, selected-frame export, and full-range extraction.
- PNG and high-quality JPG frame exports in ZIP files.
- Batch Frame Studio for up to 25 videos, processed sequentially with shared FPS/range/format settings.
- Structured batch ZIP with one folder per video, metadata.json files, and batch manifest.json / manifest.csv. Aggregate cap: 50,000 frames per batch.

WHERE FILES GO
--------------
- Application: %LOCALAPPDATA%\Programs\Frex
- Settings/cache: %APPDATA%\Frex
- Default video destination: Videos\Frex
- Frame export ZIP files are delivered using the app's download handling.

UNINSTALL
---------
Open Start Menu > Frex > Uninstall Frex. Your settings and temporary frame cache are preserved. Downloaded videos are not deleted.

TROUBLESHOOTING
---------------
- If Setup cannot find Python, install Python 3.12 from https://www.python.org/downloads/windows/ and run Setup_Frex.vbs again.
- If the WebView2 installer fails, use Microsoft's official download page: https://developer.microsoft.com/microsoft-edge/webview2/ .
- If the desktop window fails to open, run Setup_Frex.vbs again to repair the components.
- If a frame extraction says FFmpeg is unavailable, run Setup_Frex.vbs again so imageio-ffmpeg is installed, then restart Frex.

TECHNICAL NOTE
--------------
Frex is a Python-powered Windows desktop app hosted in a native WebView2 window. The installer and daily shortcuts do not show Command Prompt. It is not currently a self-contained, publisher-signed EXE/MSI; Python and WebView2 are installed as user-level prerequisites.

Processing runs locally on your PC. The app's local service binds to 127.0.0.1 only. Cookie/account features remain opt-in and cookie contents are not written to Frex logs.
