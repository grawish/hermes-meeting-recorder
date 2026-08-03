import importlib.util
import json
import sys
import unittest
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("dashboard", ROOT / "dashboard.py")
dashboard = importlib.util.module_from_spec(spec)
sys.modules["dashboard"] = dashboard
spec.loader.exec_module(dashboard)


class DashboardActionTests(unittest.TestCase):
    def test_library_reads_persistent_history_after_local_recording_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = root / "state/history"
            history.mkdir(parents=True)
            (history / "20260721-test.json").write_text(json.dumps({
                "event_id": "evt-uploaded", "title": "Uploaded meeting",
                "url": "https://meet.google.com/abc-defg-hij",
                "start": "2026-07-21T04:00:00+00:00", "end": "2026-07-21T04:10:00+00:00",
                "work_dir": str(root / "deleted"), "recording": str(root / "deleted/video.mp4"),
                "status": "uploaded", "drive_file_id": "drive-id",
                "drive_link": "https://drive.google.com/file/d/drive-id/view",
            }))
            with patch.object(dashboard, "RECORDINGS", root / "recordings"), \
                 patch.object(dashboard, "CURRENT", root / "state/current.json"), \
                 patch.object(dashboard, "HISTORY", history), \
                 patch.object(dashboard, "collect_upcoming", return_value=[]):
                data = dashboard.collect_data()
        self.assertEqual(data["counts"]["total"], 1)
        self.assertEqual(data["meetings"][0]["event_id"], "evt-uploaded")
        self.assertEqual(data["meetings"][0]["drive_file_id"], "drive-id")
        self.assertEqual(data["meetings"][0]["meeting_link"], "https://meet.google.com/abc-defg-hij")

    def test_library_never_exposes_non_google_meet_link(self):
        item = dashboard.public_meeting({"event_id": "x", "url": "https://evil.example/private"})
        self.assertIsNone(item["meeting_link"])

    def test_collect_upcoming_projects_calendar_events_without_exposing_link(self):
        now = datetime(2026, 7, 21, 5, 0, tzinfo=timezone.utc)
        event = {
            "id": "evt-1", "summary": "Design review", "status": "confirmed",
            "start": {"dateTime": "2026-07-21T05:30:00Z"},
            "end": {"dateTime": "2026-07-21T06:00:00Z"},
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }
        with patch.object(dashboard, "utcnow", return_value=now), patch.object(
            dashboard, "list_calendar_events", return_value=[event]
        ):
            upcoming = dashboard.collect_upcoming(24)
        self.assertEqual(upcoming, [{
            "event_id": "evt-1", "title": "Design review",
            "start": "2026-07-21T05:30:00+00:00", "end": "2026-07-21T06:00:00+00:00",
            "can_join": True,
        }])
        self.assertNotIn("url", upcoming[0])

    def test_start_calendar_event_resolves_link_server_side_and_launches(self):
        event = {
            "id": "evt-1", "summary": "Design review",
            "start": {"dateTime": "2026-07-21T05:30:00Z"},
            "end": {"dateTime": "2026-07-21T06:00:00Z"},
            "hangoutLink": "https://meet.google.com/abc-defg-hij",
        }
        proc = unittest.mock.MagicMock(pid=4321)
        with patch.object(dashboard, "recorder_is_active", return_value=False), patch.object(
            dashboard, "list_calendar_events", return_value=[event]
        ), patch.object(dashboard.subprocess, "Popen", return_value=proc) as popen:
            result = dashboard.start_calendar_event("evt-1")
        self.assertEqual(result["pid"], 4321)
        command = popen.call_args.args[0]
        self.assertIn("https://meet.google.com/abc-defg-hij", command)
        self.assertIn("Design review", command)

    def test_start_on_demand_rejects_non_google_meet_url(self):
        with self.assertRaises(ValueError):
            dashboard.start_on_demand("https://evil.example/meeting", "Bad", 3600)

    def test_start_on_demand_rejects_when_recorder_active(self):
        with patch.object(dashboard, "recorder_is_active", return_value=True):
            with self.assertRaises(dashboard.ConflictError):
                dashboard.start_on_demand("https://meet.google.com/abc-defg-hij", "Now", 3600)

    def test_start_on_demand_launches_detached_recorder(self):
        proc = unittest.mock.MagicMock(pid=1234)
        with patch.object(dashboard, "recorder_is_active", return_value=False), patch.object(
            dashboard.subprocess, "Popen", return_value=proc
        ) as popen:
            result = dashboard.start_on_demand("https://meet.google.com/abc-defg-hij", "Customer call", 1800)
        self.assertEqual(result, {"accepted": True, "pid": 1234})
        self.assertEqual(popen.call_args.args[0][1:3], ["join", "https://meet.google.com/abc-defg-hij"])
        self.assertIn("Customer call", popen.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
