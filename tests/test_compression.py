import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from app.Frex_3_1 import FrexService


class CompressionSmokeTest(unittest.TestCase):
    def test_cpu_h264_target_percentage(self):
        service = FrexService()
        self.assertTrue(service.ffmpeg, "FFmpeg must be available for this test")
        caps = service.compression_capabilities()
        if not caps.get("cpu", {}).get("h264"):
            self.skipTest("This FFmpeg build does not include libx264")

        with tempfile.TemporaryDirectory(prefix="frex-compress-test-") as folder:
            root = Path(folder)
            source = root / "sample_source.mp4"
            output = root / "sample_compressed.mp4"
            create = subprocess.run(
                [
                    service.ffmpeg, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=24:duration=3",
                    "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=3",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(source),
                ],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(create.returncode, 0, create.stderr[-2000:])
            original_size = source.stat().st_size

            started = service.start_compression({
                "input_path": str(source),
                "output_path": str(output),
                "codec": "h264_cpu",
                "method": "percentage",
                "target_percent": 80,
                "threads": 2,
                "audio_kbps": 96,
                "compatibility": True,
            })
            self.assertTrue(started.get("id"))

            deadline = time.time() + 180
            state = service.compression_state()
            while state.get("status") in ("queued", "running") and time.time() < deadline:
                time.sleep(0.5)
                state = service.compression_state()

            self.assertEqual(state.get("status"), "complete", state.get("error") or state.get("detail"))
            self.assertTrue(output.is_file())
            self.assertGreater(output.stat().st_size, 0)
            self.assertEqual(source.stat().st_size, original_size)
            self.assertEqual(state.get("output_size"), output.stat().st_size)


if __name__ == "__main__":
    unittest.main()
