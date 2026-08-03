import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LauncherRuntimeTests(unittest.TestCase):
    def test_launcher_uses_python_runtime_compatible_with_vendored_playwright(self):
        launcher = (ROOT / "meeting-recorder").read_text()
        self.assertIn("/usr/local/lib/hermes-agent/venv/bin/python", launcher)
        self.assertNotIn("exec /usr/bin/python3", launcher)

    def test_launcher_loads_project_configuration_for_all_entry_points(self):
        launcher = (ROOT / "meeting-recorder").read_text()
        self.assertIn('[ ! -f "$ROOT/.env" ] || . "$ROOT/.env"', launcher)

    def test_launcher_exposes_temporary_vnc_auth_workflow(self):
        launcher = (ROOT / "meeting-recorder").read_text()
        self.assertIn('if [[ "${1:-}" == "auth-vnc" ]]', launcher)
        self.assertIn('exec "$ROOT/meet-auth-vnc"', launcher)

    def test_auth_helper_is_private_temporary_and_has_cleanup(self):
        helper = (ROOT / "meet-auth-vnc").read_text()
        self.assertIn('tailscale ip -4', helper)
        self.assertIn('-listen "$TAILSCALE_IP"', helper)
        self.assertIn('auth.json', helper)
        self.assertIn('cleanup', helper)
        self.assertIn('save)', helper)
        self.assertIn('stop)', helper)


if __name__ == "__main__":
    unittest.main()
