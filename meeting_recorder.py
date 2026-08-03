#!/usr/bin/env python3
"""Google Meet recorder for Hermes.

Uses Hermes' Google Meet plugin to join, FFmpeg to capture the bot's virtual
screen/audio, Google Calendar to discover Meet events, and Hermes' Google
Workspace skill to upload artifacts to Drive.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
STATE_DIR = ROOT / "state"
RECORDINGS_DIR = ROOT / "recordings"
VENDOR = ROOT / "vendor"
GOOGLE_API = HERMES_HOME / "skills/productivity/google-workspace/scripts/google_api.py"
TOKEN_PATH = HERMES_HOME / "google_token.json"
CLIENT_PATH = HERMES_HOME / "google_client_secret.json"
MEET_SITE_PACKAGES = Path("/usr/local/lib/hermes-agent/venv/lib/python3.11/site-packages")
MEET_RE = re.compile(r"https://meet\.google\.com/(?:[a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}|lookup/[^\s/?#]+)(?:[^\s]*)?", re.I)
LOG = logging.getLogger("meeting-recorder")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def extract_meet_url(event: dict[str, Any]) -> str | None:
    candidates = [event.get("hangoutLink", ""), event.get("location", ""), event.get("description", "")]
    for ep in event.get("conferenceData", {}).get("entryPoints", []) or []:
        candidates.append(ep.get("uri", ""))
    for text in candidates:
        match = MEET_RE.search(str(text or ""))
        if match:
            return match.group(0).rstrip(".,)>]")
    return None


def _google_credentials():
    sys.path.insert(0, str(MEET_SITE_PACKAGES))
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    token = json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    creds = Credentials.from_authorized_user_info(token)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def list_calendar_events(start: datetime, end: datetime, calendar_id: str = "primary") -> list[dict[str, Any]]:
    sys.path.insert(0, str(MEET_SITE_PACKAGES))
    from googleapiclient.discovery import build
    service = build("calendar", "v3", credentials=_google_credentials(), cache_discovery=False)
    result = service.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=100,
    ).execute()
    return list(result.get("items", []))


@dataclass
class Session:
    event_id: str
    title: str
    url: str
    start: str
    end: str
    work_dir: str
    recording: str
    pid_meet: int | None = None
    pid_ffmpeg: int | None = None
    pid_xvfb: int | None = None
    drive_file_id: str | None = None
    drive_link: str | None = None
    status: str = "starting"
    audio_enabled: bool | None = None
    fallback_reason: str | None = None


class Recorder:
    def __init__(self, drive_folder_id: str | None = None, display: str = ":99"):
        self.drive_folder_id = drive_folder_id or os.getenv("MEETING_RECORDER_DRIVE_FOLDER_ID")
        self.display = display
        self.screen_size = os.getenv("MEETING_RECORDER_SCREEN_SIZE", "1366x900")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _env(display: str) -> dict[str, str]:
        env = os.environ.copy()
        env["DISPLAY"] = display
        env["PYTHONPATH"] = os.pathsep.join([str(VENDOR), str(MEET_SITE_PACKAGES), env.get("PYTHONPATH", "")])
        return env

    def _start_xvfb(self, log) -> subprocess.Popen:
        return subprocess.Popen(["Xvfb", self.display, "-screen", "0", f"{self.screen_size}x24", "-ac", "-nolisten", "tcp"], stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def _ffmpeg_cmd(self, output: Path, include_audio: bool = True) -> list[str]:
        cmd = ["ffmpeg", "-y", "-nostdin", "-loglevel", "warning", "-f", "x11grab", "-framerate", "25", "-video_size", self.screen_size, "-i", f"{self.display}.0"]
        pulse_source = os.getenv("MEETING_RECORDER_PULSE_SOURCE", "")
        if include_audio and pulse_source:
            cmd += ["-f", "pulse", "-i", pulse_source, "-c:a", "aac", "-b:a", "128k"]
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
        return cmd

    def ensure_audio_source(self) -> bool:
        """Start PulseAudio and recreate the configured null sink when needed."""
        source = os.getenv("MEETING_RECORDER_PULSE_SOURCE", "").strip()
        if not source:
            return False
        check = subprocess.run(["pactl", "info"], capture_output=True, text=True, timeout=10)
        if check.returncode:
            started = subprocess.run(
                ["pulseaudio", "--start", "--exit-idle-time=-1"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if started.returncode:
                LOG.warning("PulseAudio bootstrap failed: %s", started.stderr.strip() or started.stdout.strip())
                return False
        sources = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=10)
        if sources.returncode:
            return False
        if source not in sources.stdout:
            sink = source.removesuffix(".monitor")
            loaded = subprocess.run(
                ["pactl", "load-module", "module-null-sink", f"sink_name={sink}", "sink_properties=device.description=HermesRecorder"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if loaded.returncode:
                LOG.warning("Could not create PulseAudio null sink: %s", loaded.stderr.strip() or loaded.stdout.strip())
                return False
            sources = subprocess.run(["pactl", "list", "short", "sources"], capture_output=True, text=True, timeout=10)
        ready = sources.returncode == 0 and source in sources.stdout
        if ready:
            sink = source.removesuffix(".monitor")
            defaulted = subprocess.run(
                ["pactl", "set-default-sink", sink], capture_output=True, text=True, timeout=10
            )
            if defaulted.returncode:
                LOG.warning("Could not set recorder sink as default: %s", defaulted.stderr.strip() or defaulted.stdout.strip())
        return ready

    def start_ffmpeg_with_fallback(self, output: Path, log) -> tuple[subprocess.Popen, bool]:
        """Start capture with audio, falling back to video-only if necessary."""
        try:
            audio_ready = self.ensure_audio_source()
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.warning("Audio bootstrap failed; using video-only failsafe: %s", exc)
            audio_ready = False
        attempts = [True, False] if audio_ready else [False]
        for include_audio in attempts:
            proc = subprocess.Popen(
                self._ffmpeg_cmd(output, include_audio=include_audio),
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            time.sleep(float(os.getenv("MEETING_RECORDER_FFMPEG_STARTUP_CHECK_SECONDS", "2")))
            if proc.poll() is None:
                if not include_audio:
                    LOG.warning("Recording started in video-only failsafe mode")
                return proc, include_audio
            LOG.warning("FFmpeg startup failed with audio; retrying video-only")
        raise RuntimeError("FFmpeg failed to start even in video-only failsafe mode")

    def record(self, url: str, duration_seconds: int, title: str = "On-demand meeting", event_id: str = "ondemand", start: datetime | None = None) -> Session:
        if not MEET_RE.fullmatch(url.strip()):
            raise ValueError("Only https://meet.google.com/... links are accepted")
        start = start or utcnow()
        slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", title).strip("-")[:60] or "meeting"
        stamp = start.strftime("%Y%m%d-%H%M%S")
        work = RECORDINGS_DIR / f"{stamp}-{slug}"
        work.mkdir(parents=True, exist_ok=True)
        output = work / f"{stamp}-{slug}.mp4"
        session = Session(event_id, title, url, start.isoformat(), (start + timedelta(seconds=duration_seconds)).isoformat(), str(work), str(output))
        log = (work / "recorder.log").open("ab", buffering=0)
        xvfb = self._start_xvfb(log)
        session.pid_xvfb = xvfb.pid
        self._save(session)
        try:
            time.sleep(1)
            ffmpeg, _audio_enabled = self.start_ffmpeg_with_fallback(output, log)
            session.pid_ffmpeg = ffmpeg.pid
            session.audio_enabled = _audio_enabled
            if not _audio_enabled:
                session.fallback_reason = "PulseAudio source unavailable; recorded video only"
            env = self._env(self.display)
            meet_cmd = [sys.executable, str(ROOT / "meet_runner.py")]
            meet_env = env | {
                "HERMES_MEET_URL": url,
                "HERMES_MEET_OUT_DIR": str(work / "meet"),
                "HERMES_MEET_HEADED": "1",
                # The Meet bot leaves based on participant presence, not Calendar end.
                "HERMES_MEET_DURATION": "",
                "HERMES_MEET_GUEST_NAME": os.getenv("MEETING_RECORDER_GUEST_NAME", "Hermes Meeting Recorder"),
            }
            auth = HERMES_HOME / "workspace/meetings/auth.json"
            if auth.exists() and os.getenv("MEETING_RECORDER_USE_AUTH", "1").lower() not in {"0", "false", "no"}:
                meet_env["HERMES_MEET_AUTH_STATE"] = str(auth)
            meet = subprocess.Popen(meet_cmd, env=meet_env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            session.pid_meet = meet.pid
            session.status = "recording"
            self._save(session)
            while meet.poll() is None:
                if ffmpeg.poll() is not None:
                    raise RuntimeError("FFmpeg exited while the meeting was still active")
                time.sleep(1)
        except subprocess.TimeoutExpired:
            LOG.warning("Meet process exceeded duration; terminating")
        finally:
            for proc in (locals().get("meet"), locals().get("ffmpeg"), xvfb):
                if proc and proc.poll() is None:
                    proc.send_signal(signal.SIGTERM)
                    try: proc.wait(timeout=15)
                    except subprocess.TimeoutExpired: proc.kill()
            log.close()
        session.status = "recorded" if output.exists() and output.stat().st_size > 0 else "failed"
        transcript = work / "meet/transcript.txt"
        metadata = work / "metadata.json"
        metadata.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        if session.status == "recorded":
            upload = self.upload(output)
            session.drive_file_id = upload.get("id")
            session.drive_link = upload.get("webViewLink")
            self.share_recording_if_configured(session.url, session.drive_file_id)
            if transcript.exists():
                self.upload(transcript)
            session.status = "uploaded"
        self._save(session)
        metadata.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        self.save_history(session)
        if session.status == "uploaded":
            self.upload(metadata)
        try:
            self.notify_finished(session)
        finally:
            self.cleanup_uploaded_session(session)
        return session

    def upload(self, path: Path) -> dict[str, Any]:
        cmd = [sys.executable, str(GOOGLE_API), "drive", "upload", str(path)]
        if self.drive_folder_id:
            cmd += ["--parent", self.drive_folder_id]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError(f"Drive upload failed: {result.stderr.strip() or result.stdout.strip()}")
        return json.loads(result.stdout)

    def share_recording_if_configured(self, meeting_url: str, drive_file_id: str | None) -> bool:
        """Share selected meetings' uploaded recording with the configured reader."""
        email = os.getenv("MEETING_RECORDER_SHARE_EMAIL", "").strip()
        configured_urls = {
            value.strip().split("?", 1)[0].split("#", 1)[0].rstrip("/")
            for value in os.getenv("MEETING_RECORDER_SHARE_MEET_URLS", "").split(",")
            if value.strip()
        }
        normalized_url = meeting_url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if not email or not drive_file_id or normalized_url not in configured_urls:
            return False
        cmd = [
            sys.executable, str(GOOGLE_API), "drive", "share", drive_file_id,
            "--email", email, "--role", "reader",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError(f"Drive share failed: {result.stderr.strip() or result.stdout.strip()}")
        response = json.loads(result.stdout)
        if response.get("status") != "shared":
            raise RuntimeError(f"Drive share did not confirm success: {result.stdout.strip()}")
        return True

    def notify_finished(self, session: Session) -> bool:
        """Best-effort Discord notification after the recorder has left."""
        target = os.getenv(
            "MEETING_RECORDER_NOTIFICATION_TARGET",
            "discord:948411835877564486:1521852955970502696",
        ).strip()
        if not target:
            return False
        meet_status_path = Path(session.work_dir) / "meet/status.json"
        leave_reason = "meeting ended"
        try:
            meet_status = json.loads(meet_status_path.read_text(encoding="utf-8"))
            leave_reason = str(meet_status.get("leaveReason") or leave_reason)
        except (OSError, ValueError, TypeError):
            pass
        lines = [
            "⏹️ Meeting recorder finished",
            f"**Meeting:** {session.title}",
            f"**Status:** {session.status}",
            f"**Leave reason:** {leave_reason}",
        ]
        if session.drive_link:
            lines.append(f"**Recording:** {session.drive_link}")
        if session.fallback_reason:
            lines.append(f"**Failsafe:** {session.fallback_reason}")
        try:
            result = subprocess.run(
                ["hermes", "send", "--to", target, "\n".join(lines)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            LOG.error("Discord completion notification failed: %s", exc)
            return False
        if result.returncode:
            LOG.error("Discord completion notification failed: %s", result.stderr.strip() or result.stdout.strip())
            return False
        return True

    def cleanup_uploaded_session(self, session: Session) -> bool:
        """Delete local meeting artifacts only after a successful Drive upload."""
        if session.status != "uploaded" or not session.drive_file_id:
            return False
        work = Path(session.work_dir).resolve()
        recordings_root = RECORDINGS_DIR.resolve()
        if work == recordings_root or recordings_root not in work.parents:
            raise ValueError(f"Refusing to delete path outside recordings directory: {work}")
        if work.exists():
            shutil.rmtree(work)
        return True

    def save_history(self, session: Session) -> Path:
        """Persist lightweight final metadata outside deletable recording directories."""
        history_dir = STATE_DIR / "history"
        history_dir.mkdir(parents=True, exist_ok=True)
        work_name = Path(session.work_dir).name or f"{session.event_id}-{int(time.time())}"
        target = history_dir / f"{work_name}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")
        temporary.replace(target)
        return target

    def _save(self, session: Session) -> None:
        (STATE_DIR / "current.json").write_text(json.dumps(asdict(session), indent=2), encoding="utf-8")


def event_bounds(event: dict[str, Any]) -> tuple[datetime, datetime] | None:
    s = event.get("start", {}).get("dateTime")
    e = event.get("end", {}).get("dateTime")
    if not s or not e:
        return None
    return parse_dt(s), parse_dt(e)


def run_scheduler(args) -> None:
    recorder = Recorder(args.drive_folder_id, args.display)
    seen_path = STATE_DIR / "seen.json"
    seen = set(json.loads(seen_path.read_text()) if seen_path.exists() else [])
    while True:
        now = utcnow()
        events = list_calendar_events(now - timedelta(minutes=2), now + timedelta(minutes=args.lookahead), args.calendar)
        for event in events:
            bounds = event_bounds(event)
            url = extract_meet_url(event)
            eid = str(event.get("id", ""))
            if not bounds or not url or not eid or eid in seen or event.get("status") == "cancelled":
                continue
            start, end = bounds
            if start - timedelta(seconds=args.join_early) <= now < end:
                seen.add(eid)
                seen_path.write_text(json.dumps(sorted(seen), indent=2), encoding="utf-8")
                duration = max(60, int((end - now).total_seconds()) + args.overrun)
                try:
                    recorder.record(url, duration, event.get("summary", "Calendar meeting"), eid, start)
                except Exception:
                    LOG.exception("Meeting failed: %s", eid)
        if args.once:
            return
        time.sleep(args.poll_seconds)


def preflight() -> dict[str, Any]:
    return {
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "xvfb": bool(shutil.which("Xvfb")),
        "playwright_vendor": (VENDOR / "playwright").exists(),
        "meet_plugin": (MEET_SITE_PACKAGES / "plugins/google_meet/meet_bot.py").exists(),
        "google_token": TOKEN_PATH.exists(),
        "google_client_secret": CLIENT_PATH.exists(),
        "audio_enabled": bool(os.getenv("MEETING_RECORDER_PULSE_SOURCE")),
    }


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Automatically record Google Meet calls and upload them to Drive")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    join = sub.add_parser("join", help="Record one Meet link now")
    join.add_argument("url")
    join.add_argument("--duration", type=int, default=3600, help="seconds")
    join.add_argument("--title", default="On-demand meeting")
    join.add_argument("--drive-folder-id")
    join.add_argument("--display", default=":99")
    sched = sub.add_parser("scheduler", help="Poll Calendar and auto-join upcoming Meet events")
    sched.add_argument("--calendar", default="primary")
    sched.add_argument("--lookahead", type=int, default=10)
    sched.add_argument("--join-early", type=int, default=30)
    sched.add_argument("--overrun", type=int, default=120)
    sched.add_argument("--poll-seconds", type=int, default=30)
    sched.add_argument("--drive-folder-id")
    sched.add_argument("--display", default=":99")
    sched.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.command == "preflight":
        data = preflight(); print(json.dumps(data, indent=2)); return 0 if all(v for k, v in data.items() if k != "audio_enabled") else 1
    if args.command == "join":
        session = Recorder(args.drive_folder_id, args.display).record(args.url, args.duration, args.title)
        print(json.dumps(asdict(session), indent=2)); return 0 if session.status == "uploaded" else 1
    run_scheduler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
