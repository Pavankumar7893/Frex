# Changelog

## 3.3.0

- Added a dedicated YouTube Thumbnail Downloader workspace.
- Added previews for max-resolution, standard, and high-quality thumbnail variants.
- Added individual image downloads with actual preview dimensions and unavailable-quality handling.
- Restricted thumbnail retrieval to YouTube video metadata and YouTube image hosts.

## 3.2.0

- Added Batch Frame Studio for sequential frame extraction from up to 25 video URLs per batch.
- Added shared FPS, time-range, and PNG/JPG format settings for each batch.
- Added per-video progress, cancellation, partial-result packaging, and a 50,000-frame aggregate safety cap.
- Added structured batch ZIP folders with source-resolution frames and per-video `metadata.json`.
- Added batch-level `manifest.json`, `manifest.csv`, and README for organized downstream workflows.

## 3.1.1

- Added a dedicated Frame Extractor workspace.
- Added custom frame sampling rate from 0.1 to 30 FPS.
- Added paginated frame previews and individual selection controls.
- Added selected-frame and full-range ZIP exports.
- Added PNG and high-quality JPG export options.
- Retained single downloads, sequential batch queue, trimming, and opt-in advanced account settings.
- Renamed application branding to Frex.
- Added Windows WebView2 desktop installer and shortcuts.
