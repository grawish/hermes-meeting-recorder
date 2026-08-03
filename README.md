# Hermes Google Meet Recorder

A local service that:

- polls Google Calendar for upcoming events containing Google Meet links;
- joins automatically through Hermes' bundled `google_meet` plugin;
- records the complete browser window on a configurable 1366×900 virtual screen;
- records meeting audio when a PulseAudio/PipeWire monitor source is configured;
- uploads MP4, transcript, and metadata JSON to Google Drive;
- supports immediate on-demand joins from a Meet URL.

> **Consent:** recording laws and company policies vary. Inform participants and obtain consent before recording. The bot joins as **Hermes Meeting Recorder** by default so it is visible in the participant list.

## Status on this VM

Installed and verified: FFmpeg, Xvfb, vendored Playwright 1.61, Playwright Chromium, Hermes Google Meet plugin. The Xvfb→FFmpeg recording path was exercised successfully.

Still requires user authentication/configuration:

1. Google Workspace OAuth for **Calendar + Drive**.
2. Google Meet browser sign-in (recommended; otherwise the bot asks to be admitted as a guest).
3. PulseAudio/PipeWire utilities and a monitor source for audio. Video-only works without this, but the requirement is audio + video, so do not enable production scheduling until audio is configured and tested.

## 1. Google Calendar + Drive OAuth

Create a Desktop OAuth client and enable **Google Calendar API** and **Google Drive API**:

- Project: https://console.cloud.google.com/projectselector2/home/dashboard
- APIs: https://console.cloud.google.com/apis/library
- OAuth credentials: https://console.cloud.google.com/apis/credentials
- If the app is in Testing, add your Google account: https://console.cloud.google.com/auth/audience

Then run (replace the JSON path):

```bash
GSETUP="python ~/.hermes/skills/productivity/google-workspace/scripts/setup.py"
$GSETUP --client-secret /path/to/client_secret.json
$GSETUP --auth-url --services calendar,drive --format json
```

Open the returned URL, approve, copy the complete failed `http://localhost:1/?code=...` redirect URL, then run:

```bash
$GSETUP --auth-code 'COMPLETE_REDIRECT_URL' --format json
$GSETUP --check
```

## 2. Google Meet browser authentication

On this VM, use the built-in temporary private noVNC workflow—no manual
`DISPLAY`, `PYTHONPATH`, or VNC setup is required:

```bash
cd /var/lib/hermes/meeting-recorder
./meeting-recorder auth-vnc start
```

The command prints a Tailscale-only noVNC URL and a one-time random password.
Open the URL, sign in to Google, then save the browser session and remove the
VNC server/password with:

```bash
./meeting-recorder auth-vnc save
```

Other lifecycle commands:

```bash
./meeting-recorder auth-vnc status
./meeting-recorder auth-vnc stop
```

`start` is idempotent and prints the existing access details if an auth desktop
is already running. `save` verifies that `auth.json` was updated before cleanup.
Normal authenticated meeting runs also write Google's rotated cookies back to
`auth.json`, reducing unnecessary interactive logins.

For a headless VM, Hermes also supports a remote Meet node on a signed-in Mac/Linux desktop (`hermes meet node ...`), but this recorder currently records locally on the VM.

## 3. Enable meeting audio capture (Linux)

The current account cannot use passwordless sudo. Install PulseAudio utilities manually:

```bash
sudo apt-get update
sudo apt-get install -y pulseaudio-utils
```

Configure the desired null-sink monitor as the FFmpeg input:

```bash
cp .env.example .env
# Set: MEETING_RECORDER_PULSE_SOURCE=hermes_record.monitor
```

Confirm the source exists:

```bash
pactl list short sources
ffmpeg -f pulse -i hermes_record.monitor -t 5 /tmp/audio-test.wav
```

At every recording start, the recorder starts PulseAudio if needed and recreates
the configured null sink. This survives VM and PulseAudio restarts without an
interactive login. FFmpeg startup is verified before the bot joins Meet. If
audio still cannot initialize, recording continues in video-only failsafe mode
and the Discord completion notification reports the fallback. If video capture
cannot start or dies mid-meeting, the bot exits immediately and the session is
marked failed rather than pretending to record.

## On-demand join

```bash
./meeting-recorder join 'https://meet.google.com/abc-defg-hij' \
  --duration 3600 \
  --title 'Customer discovery'
```

The duration is in seconds. Files are stored under `recordings/` and then uploaded to Drive.
After admission, participant presence controls departure: the recorder remains in
Meet until it is the sole participant for three consecutive checks.

The recorder turns its camera off before joining and posts a configurable notice
to the in-meeting chat after admission. Set `MEETING_RECORDER_CHAT_MESSAGE=` to
disable that notice.

## Calendar auto-join

Dry-run one Calendar poll (it joins only an event currently in its join window):

```bash
./meeting-recorder scheduler --once
```

Run continuously:

```bash
./meeting-recorder scheduler
```

Defaults: poll every 30 seconds, inspect 10 minutes ahead, join 30 seconds early, record through event end plus a 2-minute overrun. All-day events are ignored. Only `meet.google.com` URLs are accepted.

## Drive folder

By default uploads go to My Drive root. To use a folder, set its ID in `.env`:

```dotenv
MEETING_RECORDER_DRIVE_FOLDER_ID=your_google_drive_folder_id
```

## User service

After OAuth, Meet auth, and audio validation:

```bash
mkdir -p ~/.config/systemd/user
cp systemd/hermes-meeting-recorder.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hermes-meeting-recorder
systemctl --user status hermes-meeting-recorder
journalctl --user -u hermes-meeting-recorder -f
```

## Verification

```bash
./meeting-recorder preflight
python -m unittest discover -s tests -v
```

`preflight` must report all required fields true and `audio_enabled: true` before production use.

## Artifacts and state

- Recordings: `/var/lib/hermes/meeting-recorder/recordings/`
- Current session: `/var/lib/hermes/meeting-recorder/state/current.json`
- Seen Calendar event IDs: `/var/lib/hermes/meeting-recorder/state/seen.json`
- Per-meeting transcript: `<recording directory>/meet/transcript.txt`
- Per-meeting logs: `<recording directory>/recorder.log`
