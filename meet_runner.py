#!/usr/bin/env python3
"""Reliability shim for Hermes' Google Meet bot.

The bundled bot clicks immediately after DOMContentLoaded; Meet renders its
pre-join controls asynchronously. This shim waits for a visible join control
before delegating to the bundled bot's normal admission/caption loop.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


def save_refreshed_auth_state(context, auth_state: str | None) -> bool:
    """Persist Google's rotated cookies after a successful browser session."""
    if not auth_state:
        return False
    try:
        context.storage_state(path=auth_state)
        return True
    except Exception:
        return False


class EmptyMeetingGuard:
    """Avoid leaving on a transient/bad participant-count observation."""

    def __init__(self, confirmations: int = 3):
        self.confirmations = confirmations
        self.empty_observations = 0
        self.saw_other_participant = False

    def observe(self, participant_count: int | None) -> bool:
        if participant_count is not None and participant_count >= 2:
            self.saw_other_participant = True
            self.empty_observations = 0
        elif participant_count == 1 and self.saw_other_participant:
            self.empty_observations += 1
        else:
            self.empty_observations = 0
        return self.saw_other_participant and self.empty_observations >= self.confirmations


def configured_chat_message() -> str | None:
    message = os.environ.get(
        "MEETING_RECORDER_CHAT_MESSAGE",
        "This meeting is being recorded by Hermes Meeting Recorder.",
    ).strip()
    return message or None


def disable_camera(page) -> bool:
    """Turn camera off only when Meet exposes the enabled-camera control."""
    for label in ("Turn off camera", "Turn camera off"):
        try:
            button = page.get_by_role("button", name=label, exact=False).first
            if button.count() and button.is_visible():
                button.click(timeout=5_000)
                return True
        except Exception:
            continue
    return False


def enable_camera(page) -> bool:
    """Turn camera on only when Meet exposes the disabled-camera control."""
    for label in ("Turn on camera", "Turn camera on"):
        try:
            button = page.get_by_role("button", name=label, exact=False).first
            if button.count() and button.is_visible():
                button.click(timeout=5_000)
                return True
        except Exception:
            continue
    return False


def enforce_camera_state(page, in_call: bool = True) -> bool:
    """Broadcast the configured image, otherwise preserve camera-off behavior."""
    if not in_call:
        return False
    if os.environ.get("MEETING_RECORDER_CAMERA_IMAGE", "").strip():
        return enable_camera(page)
    return disable_camera(page)


def enforce_camera_off_after_admission(page, in_call: bool) -> bool:
    """Hard invariant: once admitted, immediately disable any active camera."""
    if not in_call:
        return False
    return disable_camera(page)


def send_recording_announcement(page) -> bool:
    message = configured_chat_message()
    if not message:
        return False
    try:
        for label in ("Chat with everyone", "Open chat", "Chat"):
            button = page.get_by_role("button", name=label, exact=False).first
            if button.count() and button.is_visible():
                button.click(timeout=5_000)
                break
        box = page.locator(
            'textarea[aria-label*="message" i], textarea[placeholder*="message" i], '
            'input[aria-label*="message" i]'
        ).first
        box.wait_for(state="visible", timeout=5_000)
        box.fill(message)
        box.press("Enter")
        return True
    except Exception:
        return False


def participant_count(page) -> int | None:
    """Read Meet's own participant-count badge; unknown is never treated empty."""
    try:
        value = page.evaluate(r"""
        (() => {
          const candidates = [...document.querySelectorAll(
            '[aria-label*="participant" i], [aria-label*="people" i], [data-tooltip*="people" i]'
          )];
          for (const el of candidates) {
            const text = `${el.getAttribute('aria-label') || ''} ${el.innerText || ''}`;
            const matches = [...text.matchAll(/\b(\d+)\b/g)].map(m => Number(m[1]));
            if (matches.length) return Math.max(...matches);
          }
          return null;
        })()
        """)
        return int(value) if value is not None else None
    except Exception:
        return None


def detect_call_ended(page) -> bool:
    """Detect removal/end after admission, including Meet's landing redirect."""
    try:
        url = str(page.url or "")
        active_meeting = re.match(
            r"^https://meet\.google\.com/(?:[a-z0-9]{3,}-[a-z0-9]{3,}-[a-z0-9]{3,}|lookup/)",
            url,
            re.I,
        )
        if not active_meeting:
            return True
        text = str(page.evaluate("() => document.body ? document.body.innerText || '' : ''") or "")
        return bool(re.search(
            r"You were removed from the meeting|You left the meeting|The call has ended",
            text,
            re.I,
        ))
    except Exception:
        return False


def click_join_when_ready(page, state, timeout_seconds: float = 30, poll_seconds: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout_seconds
    # Never use "Switch here": it would disconnect the user's existing call.
    labels = ("Join here too", "Join now", "Ask to join", "Join meeting")
    expanded_other_ways = False
    while time.monotonic() < deadline:
        for label in labels:
            try:
                button = page.get_by_role("button", name=label, exact=False).first
                if button.count() and button.is_visible():
                    enforce_camera_state(page)
                    button.click(timeout=5_000)
                    state.set(lobby_waiting=(label == "Ask to join"))
                    return True
            except Exception:
                continue
        if not expanded_other_ways:
            try:
                got_it = page.get_by_role("button", name="Got it", exact=False).first
                if got_it.count() and got_it.is_visible():
                    got_it.click(timeout=5_000)
                other = page.get_by_role("button", name="Other ways to join", exact=False).first
                if other.count() and other.is_visible():
                    other.click(timeout=5_000)
                    expanded_other_ways = True
                    continue
            except Exception:
                pass
        if poll_seconds:
            time.sleep(poll_seconds)

    # Persist diagnostic evidence for selector/UI regressions.
    out_dir = Path(os.environ.get("HERMES_MEET_OUT_DIR", "."))
    try:
        page.screenshot(path=str(out_dir / "join-timeout.png"), full_page=True)
        (out_dir / "join-timeout.html").write_text(page.content(), encoding="utf-8")
    except Exception:
        pass
    state.set(error="join control did not become visible within timeout")
    return False


def main() -> int:
    import meet_bot_runtime as meet_bot

    meet_bot._click_join = click_join_when_ready
    return meet_bot.run_bot()


if __name__ == "__main__":
    raise SystemExit(main())
