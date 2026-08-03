#!/usr/bin/env python3
"""Local meeting-recorder dashboard and read-only JSON API."""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import subprocess
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from meeting_recorder import MEET_RE, event_bounds, extract_meet_url, list_calendar_events, utcnow

ROOT = Path(__file__).resolve().parent
RECORDINGS = ROOT / "recordings"
CURRENT = ROOT / "state" / "current.json"
HISTORY = ROOT / "state" / "history"
DASHBOARD = ROOT / "dashboard.html"
LAUNCH_LOG = ROOT / "runtime" / "dashboard-launches.log"
ACTIVE_STATUSES = {"starting", "joining", "lobby", "recording", "uploading"}


class ConflictError(RuntimeError):
    pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def recorder_is_active() -> bool:
    current = read_json(CURRENT)
    if str(current.get("status", "")).lower() not in ACTIVE_STATUSES:
        return False
    pid = current.get("pid_meet") or current.get("pid_ffmpeg")
    if not isinstance(pid, int):
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _calendar_window(hours: int = 24) -> list[dict]:
    now = utcnow()
    return list_calendar_events(now - timedelta(minutes=2), now + timedelta(hours=hours))


def collect_upcoming(hours: int = 24) -> list[dict]:
    now = utcnow()
    projected = []
    for event in _calendar_window(hours):
        bounds = event_bounds(event)
        if not bounds or event.get("status") == "cancelled" or bounds[1] <= now:
            continue
        projected.append({"event_id": str(event.get("id") or ""),
                          "title": str(event.get("summary") or "Untitled meeting"),
                          "start": bounds[0].isoformat(), "end": bounds[1].isoformat(),
                          "can_join": bool(extract_meet_url(event))})
    return sorted(projected, key=lambda item: item["start"])


def _launch_join(url: str, title: str, duration: int) -> dict:
    if not MEET_RE.fullmatch(url.strip()):
        raise ValueError("Only https://meet.google.com/... links are accepted")
    if recorder_is_active():
        raise ConflictError("A meeting recorder is already active")
    duration = max(60, min(int(duration), 12 * 60 * 60))
    title = (title or "On-demand meeting").strip()[:200]
    LAUNCH_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = LAUNCH_LOG.open("ab", buffering=0)
    try:
        proc = subprocess.Popen([str(ROOT / "meeting-recorder"), "join", url.strip(), "--duration",
                                 str(duration), "--title", title], cwd=ROOT, stdout=log,
                                stderr=subprocess.STDOUT, start_new_session=True)
    finally:
        log.close()
    return {"accepted": True, "pid": proc.pid}


def start_on_demand(url: str, title: str = "On-demand meeting", duration: int = 3600) -> dict:
    return _launch_join(url, title, duration)


def start_calendar_event(event_id: str) -> dict:
    for event in _calendar_window(24):
        if str(event.get("id") or "") != event_id:
            continue
        bounds, url = event_bounds(event), extract_meet_url(event)
        if not bounds or not url or bounds[1] <= utcnow():
            raise ValueError("Calendar event is not joinable")
        duration = max(60, int((bounds[1] - utcnow()).total_seconds()) + 120)
        return _launch_join(url, str(event.get("summary") or "Calendar meeting"), duration)
    raise ValueError("Calendar event was not found")


def iso_from_mtime(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    except OSError:
        return None


def safe_relative_recording(value: str | None, work_dir: Path) -> Path | None:
    if not value:
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = work_dir / candidate
    try:
        resolved = candidate.resolve()
        root = RECORDINGS.resolve()
        if (
            resolved == root
            or root not in resolved.parents
            or not resolved.is_file()
            or resolved.stat().st_size <= 0
        ):
            return None
        return resolved
    except OSError:
        return None


def public_meeting(raw: dict, source: Path | None = None) -> dict:
    work_dir = Path(raw.get("work_dir") or (source.parent if source else RECORDINGS))
    recording = safe_relative_recording(raw.get("recording"), work_dir)
    if recording is None and source:
        candidates = [path.resolve() for path in sorted(source.parent.glob("*.mp4")) if path.stat().st_size > 0]
        recording = candidates[0] if candidates else None
    default_id = source.parent.name if source else "unknown"
    default_title = source.parent.name if source else "Untitled meeting"
    raw_url = str(raw.get("url") or "").strip()
    meeting_link = raw_url if MEET_RE.fullmatch(raw_url) else None
    result = {
        "event_id": str(raw.get("event_id") or default_id),
        "title": str(raw.get("title") or default_title),
        "start": raw.get("start"),
        "end": raw.get("end"),
        "status": str(raw.get("status") or "unknown").lower(),
        "audio_enabled": raw.get("audio_enabled"),
        "fallback_reason": raw.get("fallback_reason"),
        "drive_link": raw.get("drive_link"),
        "drive_file_id": raw.get("drive_file_id"),
        "meeting_link": meeting_link,
        "updated_at": iso_from_mtime(source) if source else iso_from_mtime(CURRENT),
        "recording_link": None,
        "recording_size": None,
    }
    if recording:
        rel = recording.relative_to(RECORDINGS.resolve())
        result["recording_link"] = "/recordings/" + quote(str(rel))
        result["recording_size"] = recording.stat().st_size
    return result


def collect_data() -> dict:
    current_raw = read_json(CURRENT)
    current = public_meeting(current_raw) if current_raw else None
    meetings_by_key: dict[str, dict] = {}
    def add(raw: dict, source: Path | None = None) -> None:
        item = public_meeting(raw, source)
        work_name = Path(str(raw.get("work_dir") or "")).name
        key = work_name or f'{item["event_id"]}|{item.get("start") or item.get("updated_at")}'
        meetings_by_key[key] = item

    if HISTORY.exists():
        for metadata in HISTORY.glob("*.json"):
            raw = read_json(metadata)
            if raw:
                add(raw, metadata)
    if RECORDINGS.exists():
        for metadata in RECORDINGS.glob("*/metadata.json"):
            raw = read_json(metadata)
            if raw:
                add(raw, metadata)
    # Active sessions often do not have metadata until finalization.
    if current_raw:
        add(current_raw)
    meetings = list(meetings_by_key.values())
    meetings.sort(key=lambda m: m.get("start") or m.get("updated_at") or "", reverse=True)
    counts = {"total": len(meetings), "uploaded": 0, "failed": 0, "recording": 0}
    for meeting in meetings:
        status = meeting["status"]
        if status in counts:
            counts[status] += 1
    try:
        upcoming, upcoming_error = collect_upcoming(), None
    except Exception as exc:
        upcoming, upcoming_error = [], f"Calendar unavailable: {type(exc).__name__}"
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "current": current,
        "meetings": meetings,
        "counts": counts,
        "upcoming": upcoming,
        "upcoming_error": upcoming_error,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesMeetingDashboard/1.0"

    def send_bytes(self, body: bytes, content_type: str, status: int = 200, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/dashboard.html"}:
            try:
                self.send_bytes(DASHBOARD.read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        if parsed.path == "/api/meetings":
            body = json.dumps(collect_data(), separators=(",", ":")).encode()
            self.send_bytes(body, "application/json; charset=utf-8")
            return
        if parsed.path.startswith("/recordings/"):
            relative = unquote(parsed.path.removeprefix("/recordings/"))
            try:
                target = (RECORDINGS / relative).resolve()
                root = RECORDINGS.resolve()
                if root not in target.parents or not target.is_file():
                    raise FileNotFoundError
                size = target.stat().st_size
                content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                with target.open("rb") as handle:
                    self.send_bytes(handle.read(), content_type, extra={
                        "Content-Disposition": f'inline; filename="{target.name}"',
                        "Accept-Ranges": "bytes",
                    })
            except OSError:
                self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path not in {"/api/join", "/api/join-now"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 8192:
                raise ValueError("Invalid request size")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON object required")
            if parsed.path == "/api/join-now":
                result = start_calendar_event(str(payload.get("event_id") or ""))
            else:
                result = start_on_demand(str(payload.get("url") or ""),
                                         str(payload.get("title") or "On-demand meeting"),
                                         int(payload.get("duration") or 3600))
            self.send_bytes(json.dumps(result).encode(), "application/json; charset=utf-8",
                            status=HTTPStatus.ACCEPTED)
        except ConflictError as exc:
            self.send_bytes(json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8",
                            status=HTTPStatus.CONFLICT)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self.send_bytes(json.dumps({"error": str(exc)}).encode(), "application/json; charset=utf-8",
                            status=HTTPStatus.BAD_REQUEST)

    def log_message(self, fmt: str, *args) -> None:
        print(f"dashboard {self.address_string()} {fmt % args}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meeting recording management dashboard")
    parser.add_argument("--host", default=os.getenv("MEETING_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MEETING_DASHBOARD_PORT", "8765")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Meeting dashboard: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
