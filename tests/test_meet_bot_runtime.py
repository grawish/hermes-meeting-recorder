import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("meet_bot_runtime", ROOT / "meet_bot_runtime.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class CameraArgumentsTests(unittest.TestCase):
    def test_static_camera_file_is_passed_to_chromium(self):
        args = runtime.build_chrome_args(realtime_enabled=False, camera_file="/tmp/recorder.y4m")
        self.assertIn("--use-fake-device-for-media-stream", args)
        self.assertIn("--use-file-for-fake-video-capture=/tmp/recorder.y4m", args)


if __name__ == "__main__":
    unittest.main()