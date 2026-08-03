import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("meet_runner", ROOT / "meet_runner.py")
meet_runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meet_runner)


class FakeButton:
    def __init__(self, visible_after):
        self.visible_after = visible_after
        self.checks = 0
        self.clicked = False

    @property
    def first(self):
        return self

    def count(self):
        return 1

    def is_visible(self):
        self.checks += 1
        return self.checks >= self.visible_after

    def click(self, timeout):
        self.clicked = True


class FakePage:
    def __init__(self):
        self.button = FakeButton(visible_after=3)

    def get_by_role(self, role, name, exact=False):
        return self.button


class FakeState:
    def __init__(self):
        self.values = {}

    def set(self, **values):
        self.values.update(values)


class SwitchHerePage:
    def __init__(self):
        self.button = FakeButton(visible_after=1)

    def get_by_role(self, role, name, exact=False):
        if name == "Switch here":
            return self.button
        return FakeButton(visible_after=999)


class MultipleJoinOptionsPage:
    def __init__(self):
        self.buttons = {
            "Join here too": FakeButton(visible_after=1),
            "Switch here": FakeButton(visible_after=1),
        }

    def get_by_role(self, role, name, exact=False):
        return self.buttons.get(name, FakeButton(visible_after=999))


class MeetJoinTests(unittest.TestCase):
    def test_camera_image_mode_turns_camera_on_when_currently_off(self):
        class CameraPage:
            def __init__(self):
                self.clicked = False

            def get_by_role(self, role, name, exact=False):
                button = FakeButton(1 if name == "Turn on camera" else 999)
                original_click = button.click

                def click(timeout):
                    original_click(timeout)
                    self.clicked = True

                button.click = click
                return button

        page = CameraPage()
        with patch.dict(os.environ, {"MEETING_RECORDER_CAMERA_IMAGE": "/tmp/recorder.y4m"}):
            self.assertTrue(meet_runner.enforce_camera_state(page, in_call=True))
        self.assertTrue(page.clicked)

    def test_camera_image_mode_does_not_turn_an_enabled_camera_off(self):
        class EnabledCameraPage:
            def __init__(self):
                self.clicked = False

            def get_by_role(self, role, name, exact=False):
                button = FakeButton(1 if name == "Turn off camera" else 999)
                original_click = button.click

                def click(timeout):
                    original_click(timeout)
                    self.clicked = True

                button.click = click
                return button

        page = EnabledCameraPage()
        with patch.dict(os.environ, {"MEETING_RECORDER_CAMERA_IMAGE": "/tmp/recorder.y4m"}):
            self.assertFalse(meet_runner.enforce_camera_state(page, in_call=True))
        self.assertFalse(page.clicked)

    def test_waits_for_delayed_join_button_and_clicks_it(self):
        page, state = FakePage(), FakeState()
        clicked = meet_runner.click_join_when_ready(page, state, timeout_seconds=1, poll_seconds=0)
        self.assertTrue(clicked)
        self.assertTrue(page.button.clicked)
        self.assertGreaterEqual(page.button.checks, 3)

    def test_never_switches_the_users_existing_call(self):
        page, state = SwitchHerePage(), FakeState()
        clicked = meet_runner.click_join_when_ready(page, state, timeout_seconds=0.01, poll_seconds=0)
        self.assertFalse(clicked)
        self.assertFalse(page.button.clicked)
    def test_prefers_join_here_too_over_switching_existing_call(self):
        page, state = MultipleJoinOptionsPage(), FakeState()
        clicked = meet_runner.click_join_when_ready(page, state, timeout_seconds=1, poll_seconds=0)
        self.assertTrue(clicked)
        self.assertTrue(page.buttons["Join here too"].clicked)
        self.assertFalse(page.buttons["Switch here"].clicked)

    def test_join_here_too_is_preferred_over_join_now(self):
        page, state = MultipleJoinOptionsPage(), FakeState()
        page.buttons["Join now"] = FakeButton(visible_after=1)
        meet_runner.click_join_when_ready(page, state, timeout_seconds=1, poll_seconds=0)
        self.assertTrue(page.buttons["Join here too"].clicked)
        self.assertFalse(page.buttons["Join now"].clicked)

    def test_opens_other_ways_to_join_then_uses_join_here_too(self):
        class ExpandButton(FakeButton):
            def __init__(self, page):
                super().__init__(1)
                self.page = page

            def click(self, timeout):
                super().click(timeout)
                self.page.expanded = True

        class NestedJoinPage:
            def __init__(self):
                self.expanded = False
                self.other = ExpandButton(self)
                self.join = FakeButton(1)
                self.switch = FakeButton(1)

            def get_by_role(self, role, name, exact=False):
                if name == "Other ways to join":
                    return self.other
                if name == "Join here too":
                    return self.join if self.expanded else FakeButton(999)
                if name == "Switch here":
                    return self.switch
                return FakeButton(999)

        page, state = NestedJoinPage(), FakeState()
        self.assertTrue(meet_runner.click_join_when_ready(page, state, timeout_seconds=1, poll_seconds=0))
        self.assertTrue(page.other.clicked)
        self.assertTrue(page.join.clicked)
        self.assertFalse(page.switch.clicked)

    def test_recording_announcement_is_configurable(self):
        with patch.dict(os.environ, {"MEETING_RECORDER_CHAT_MESSAGE": "Recording started"}):
            self.assertEqual(meet_runner.configured_chat_message(), "Recording started")
        with patch.dict(os.environ, {"MEETING_RECORDER_CHAT_MESSAGE": ""}):
            self.assertIsNone(meet_runner.configured_chat_message())

    def test_empty_meeting_requires_another_participant_then_consecutive_confirmations(self):
        guard = meet_runner.EmptyMeetingGuard(confirmations=3)
        self.assertFalse(guard.observe(1))
        self.assertFalse(guard.observe(1))
        self.assertFalse(guard.observe(1))
        self.assertFalse(guard.observe(2))
        self.assertFalse(guard.observe(1))
        self.assertFalse(guard.observe(1))
        self.assertTrue(guard.observe(1))

    def test_camera_is_disabled_when_enabled(self):
        class CameraPage:
            def __init__(self):
                self.clicked = False

            def get_by_role(self, role, name, exact=False):
                button = FakeButton(1)
                original_click = button.click

                def click(timeout):
                    original_click(timeout)
                    self.clicked = True

                button.click = click
                return button

        page = CameraPage()
        self.assertTrue(meet_runner.disable_camera(page))
        self.assertTrue(page.clicked)

    def test_post_admission_guard_turns_camera_off(self):
        class CameraPage:
            def __init__(self):
                self.clicked = False

            def get_by_role(self, role, name, exact=False):
                button = FakeButton(1)
                original_click = button.click

                def click(timeout):
                    original_click(timeout)
                    self.clicked = True

                button.click = click
                return button

        page = CameraPage()
        self.assertTrue(meet_runner.enforce_camera_off_after_admission(page, in_call=True))
        self.assertTrue(page.clicked)

    def test_post_admission_guard_does_nothing_before_admission(self):
        page = object()
        self.assertFalse(meet_runner.enforce_camera_off_after_admission(page, in_call=False))

    def test_detects_removal_when_meet_redirects_to_landing(self):
        class RemovedPage:
            url = "https://meet.google.com/landing"

            def evaluate(self, script):
                return "Secure video conferencing for everyone"

        self.assertTrue(meet_runner.detect_call_ended(RemovedPage()))

    def test_does_not_end_while_still_on_meeting_url(self):
        class ActivePage:
            url = "https://meet.google.com/abc-defg-hij"

            def evaluate(self, script):
                return "Daily Stand-up"

        self.assertFalse(meet_runner.detect_call_ended(ActivePage()))


    def test_saves_rotated_google_session_state_after_meeting(self):
        from meet_runner import save_refreshed_auth_state

        class Context:
            def __init__(self):
                self.saved_to = None
            def storage_state(self, path):
                self.saved_to = path

        context = Context()
        self.assertTrue(save_refreshed_auth_state(context, "/tmp/auth.json"))
        self.assertEqual(context.saved_to, "/tmp/auth.json")


if __name__ == "__main__":
    unittest.main()
