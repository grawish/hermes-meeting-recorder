import importlib.util
import os
import sys
import unittest
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("meeting_recorder", ROOT / "meeting_recorder.py")
mr = importlib.util.module_from_spec(spec)
sys.modules["meeting_recorder"] = mr
spec.loader.exec_module(mr)


class RecorderTests(unittest.TestCase):
    def test_extract_hangout_link(self):
        event = {"hangoutLink": "https://meet.google.com/abc-defg-hij"}
        self.assertEqual(mr.extract_meet_url(event), event["hangoutLink"])

    def test_extract_conference_entry_point(self):
        event = {"conferenceData": {"entryPoints": [{"uri": "https://meet.google.com/xyz-abcd-efg"}]}}
        self.assertEqual(mr.extract_meet_url(event), "https://meet.google.com/xyz-abcd-efg")

    def test_extract_description(self):
        event = {"description": "Join: https://meet.google.com/abc-defg-hij."}
        self.assertEqual(mr.extract_meet_url(event), "https://meet.google.com/abc-defg-hij")

    def test_event_bounds_ignores_all_day(self):
        self.assertIsNone(mr.event_bounds({"start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}}))

    def test_parse_datetime_to_utc(self):
        value = mr.parse_dt("2026-07-20T10:00:00+05:30")
        self.assertEqual(value, datetime(2026, 7, 20, 4, 30, tzinfo=timezone.utc))

    def test_reject_non_meet_url(self):
        with self.assertRaises(ValueError):
            mr.Recorder().record("https://evil.example/x", 1)

    def test_capture_geometry_includes_full_browser_window(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEETING_RECORDER_SCREEN_SIZE", None)
            recorder = mr.Recorder(display=":99")
        self.assertEqual(recorder.screen_size, "1366x900")
        cmd = recorder._ffmpeg_cmd(Path("out.mp4"))
        self.assertEqual(cmd[cmd.index("-video_size") + 1], "1366x900")

    def test_video_only_ffmpeg_command_omits_unavailable_audio_source(self):
        recorder = mr.Recorder(display=":99")
        with patch.dict(os.environ, {"MEETING_RECORDER_PULSE_SOURCE": "hermes_record.monitor"}):
            cmd = recorder._ffmpeg_cmd(Path("out.mp4"), include_audio=False)
        self.assertNotIn("pulse", cmd)
        self.assertNotIn("hermes_record.monitor", cmd)

    def test_audio_bootstrap_starts_pulseaudio_and_creates_missing_sink(self):
        recorder = mr.Recorder()
        responses = [
            __import__("subprocess").CompletedProcess([], 1, "", "refused"),
            __import__("subprocess").CompletedProcess([], 0, "", ""),
            __import__("subprocess").CompletedProcess([], 0, "alsa.monitor\n", ""),
            __import__("subprocess").CompletedProcess([], 0, "42\n", ""),
            __import__("subprocess").CompletedProcess([], 0, "hermes_record.monitor\n", ""),
            __import__("subprocess").CompletedProcess([], 0, "", ""),
        ]
        with patch.dict(os.environ, {"MEETING_RECORDER_PULSE_SOURCE": "hermes_record.monitor"}), \
             patch.object(mr.subprocess, "run", side_effect=responses) as run:
            self.assertTrue(recorder.ensure_audio_source())
        commands = [item.args[0] for item in run.call_args_list]
        self.assertIn(["pulseaudio", "--start", "--exit-idle-time=-1"], commands)
        self.assertTrue(any(command[:3] == ["pactl", "load-module", "module-null-sink"] for command in commands))
        self.assertIn(["pactl", "set-default-sink", "hermes_record"], commands)

    def test_ffmpeg_audio_failure_retries_video_only(self):
        recorder = mr.Recorder()
        failed = MagicMock()
        failed.poll.return_value = 1
        video = MagicMock()
        video.poll.return_value = None
        with patch.object(recorder, "ensure_audio_source", return_value=True), \
             patch.object(mr.subprocess, "Popen", side_effect=[failed, video]) as popen, \
             patch.object(mr.time, "sleep"):
            process, audio_enabled = recorder.start_ffmpeg_with_fallback(Path("out.mp4"), MagicMock())
        self.assertIs(process, video)
        self.assertFalse(audio_enabled)
        self.assertEqual(popen.call_count, 2)
        self.assertNotIn("pulse", popen.call_args_list[1].args[0])

    def test_ffmpeg_video_failure_aborts_before_join(self):
        recorder = mr.Recorder()
        failed = MagicMock()
        failed.poll.return_value = 1
        with patch.object(recorder, "ensure_audio_source", return_value=False), \
             patch.object(mr.subprocess, "Popen", return_value=failed), \
             patch.object(mr.time, "sleep"):
            with self.assertRaises(RuntimeError):
                recorder.start_ffmpeg_with_fallback(Path("out.mp4"), MagicMock())

    def test_audio_bootstrap_exception_still_uses_video_failsafe(self):
        recorder = mr.Recorder()
        video = MagicMock()
        video.poll.return_value = None
        with patch.object(recorder, "ensure_audio_source", side_effect=FileNotFoundError("pactl")), \
             patch.object(mr.subprocess, "Popen", return_value=video) as popen, \
             patch.object(mr.time, "sleep"):
            process, audio_enabled = recorder.start_ffmpeg_with_fallback(Path("out.mp4"), MagicMock())
        self.assertIs(process, video)
        self.assertFalse(audio_enabled)
        self.assertNotIn("pulse", popen.call_args.args[0])

    def test_finished_notification_targets_discord_and_includes_drive_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "meet").mkdir()
            (work / "meet/status.json").write_text(json.dumps({"leaveReason": "everyone_left"}))
            session = mr.Session(
                "event", "Daily Stand-up", "https://meet.google.com/abc-defg-hij",
                "2026-07-21T02:30:00+00:00", "2026-07-21T03:00:00+00:00",
                str(work), str(work / "recording.mp4"), status="uploaded",
                drive_link="https://drive.google.com/file/d/example/view",
            )
            completed = __import__("subprocess").CompletedProcess([], 0, "sent", "")
            with patch.dict(os.environ, {
                "MEETING_RECORDER_NOTIFICATION_TARGET": "discord:server:notifications"
            }), patch.object(mr.subprocess, "run", return_value=completed) as run:
                self.assertTrue(mr.Recorder().notify_finished(session))
            cmd = run.call_args.args[0]
            self.assertEqual(cmd[:4], ["hermes", "send", "--to", "discord:server:notifications"])
            self.assertIn("Daily Stand-up", cmd[4])
            self.assertIn("everyone_left", cmd[4])
            self.assertIn("https://drive.google.com/file/d/example/view", cmd[4])

    def test_finished_notification_reports_video_only_fallback(self):
        session = mr.Session(
            "event", "Title", "https://meet.google.com/abc-defg-hij", "s", "e", "/tmp", "/tmp/a.mp4",
            drive_file_id="id", status="uploaded", audio_enabled=False,
            fallback_reason="PulseAudio source unavailable; recorded video only",
        )
        completed = __import__("subprocess").CompletedProcess([], 0, "sent", "")
        with patch.object(mr.subprocess, "run", return_value=completed) as run:
            self.assertTrue(mr.Recorder().notify_finished(session))
        self.assertIn("video only", run.call_args.args[0][4])

    def test_finished_notification_can_be_disabled(self):
        session = mr.Session("event", "Title", "https://meet.google.com/abc-defg-hij", "s", "e", "/tmp", "/tmp/a.mp4")
        with patch.dict(os.environ, {"MEETING_RECORDER_NOTIFICATION_TARGET": ""}):
            with patch.object(mr.subprocess, "run") as run:
                self.assertFalse(mr.Recorder().notify_finished(session))
                run.assert_not_called()

    def test_matching_meeting_recording_is_shared_as_reader(self):
        recorder = mr.Recorder()
        completed = __import__("subprocess").CompletedProcess(
            [], 0, '{"status":"shared","permissionId":"permission-1"}', ""
        )
        with patch.dict(os.environ, {
            "MEETING_RECORDER_SHARE_EMAIL": "arifrahamancob@gmail.com",
            "MEETING_RECORDER_SHARE_MEET_URLS": (
                "https://meet.google.com/dkj-ipmf-hnq,"
                "https://meet.google.com/hvy-yurh-jbi"
            ),
        }), patch.object(mr.subprocess, "run", return_value=completed) as run:
            result = recorder.share_recording_if_configured(
                "https://meet.google.com/dkj-ipmf-hnq?authuser=0", "drive-file-id"
            )
        self.assertTrue(result)
        self.assertEqual(run.call_args.args[0][-6:], [
            "share", "drive-file-id", "--email", "arifrahamancob@gmail.com", "--role", "reader"
        ])

    def test_unmatched_meeting_recording_is_not_shared(self):
        with patch.dict(os.environ, {
            "MEETING_RECORDER_SHARE_EMAIL": "arifrahamancob@gmail.com",
            "MEETING_RECORDER_SHARE_MEET_URLS": "https://meet.google.com/dkj-ipmf-hnq",
        }), patch.object(mr.subprocess, "run") as run:
            self.assertFalse(mr.Recorder().share_recording_if_configured(
                "https://meet.google.com/abc-defg-hij", "drive-file-id"
            ))
        run.assert_not_called()

    def test_uploaded_recording_is_deleted_locally(self):
        mr.RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        work = mr.RECORDINGS_DIR / "test-cleanup-uploaded"
        work.mkdir(parents=True, exist_ok=True)
        (work / "recording.mp4").write_bytes(b"video")
        session = mr.Session(
            "event", "Title", "https://meet.google.com/abc-defg-hij", "s", "e",
            str(work), str(work / "recording.mp4"), drive_file_id="drive-id", status="uploaded",
        )
        self.assertTrue(mr.Recorder().cleanup_uploaded_session(session))
        self.assertFalse(work.exists())

    def test_failed_upload_is_never_deleted(self):
        with tempfile.TemporaryDirectory(dir=mr.RECORDINGS_DIR) as tmp:
            work = Path(tmp)
            session = mr.Session(
                "event", "Title", "https://meet.google.com/abc-defg-hij", "s", "e",
                str(work), str(work / "recording.mp4"), status="failed",
            )
            self.assertFalse(mr.Recorder().cleanup_uploaded_session(session))
            self.assertTrue(work.exists())

    def test_finalized_session_is_saved_to_history_outside_cleanup_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            work = Path(tmp) / "recordings/20260721-040000-Test"
            session = mr.Session(
                "event", "Title", "https://meet.google.com/abc-defg-hij", "s", "e",
                str(work), str(work / "recording.mp4"), drive_file_id="drive-id", status="uploaded",
            )
            with patch.object(mr, "STATE_DIR", state):
                history = mr.Recorder().save_history(session)
            self.assertTrue(history.exists())
            self.assertEqual(json.loads(history.read_text())["drive_file_id"], "drive-id")
            self.assertNotIn(str(work), str(history))


if __name__ == "__main__":
    unittest.main()
