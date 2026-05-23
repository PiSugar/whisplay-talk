# whisplay-talk

<img src="https://docs.pisugar.com/img/whisplay_logo@4x-8.png" alt="Whisplay Talk" width="200" />

[中文](README_CN.md)

A P2P voice intercom app for Whisplay HAT, designed for real-time voice broadcasting between multiple Whisplay devices.

This version already includes the core flow:
- Runs as a `whisplay-daemon` app
- Discovers online devices through Tailscale `MagicDNS`, using the `whisplay-talk-` hostname prefix
- While one device holds the talk button, microphone audio is compressed and sent to all online peers over UDP
- Other devices play the audio in real time and show who is currently speaking
- While idle, the screen shows the list of online intercom devices

## Current Implementation

This repository is a runnable MVP with the following design:

- Discovery:
  Polls `tailscale status --json` for devices whose hostname starts with `whisplay-talk-`, then probes the app TCP port on each device before marking it online
- Transport:
  All devices listen on fixed TCP port `24680` for audio streams
- Audio:
  Uses `arecord` / `aplay`, with default 16kHz / 16-bit / mono capture, `Opus` voice encoding, a small receive jitter buffer, and one-frame redundant resend
- Display:
  Uses Pillow to render a 240x280 UI into the framebuffer provided by `whisplay-daemon`
- Input:
  Uses `whisplay-daemon` button events for push-to-talk

## Project Structure

```text
whisplay-talk/
├── main.py
├── application.py
├── config.py
├── audio/
├── display/
├── hardware/
├── network/
├── install.sh
├── run.sh
├── requirements.txt
└── .env.template
```

## Installation

```bash
git clone <this-repo>
cd whisplay-talk
bash install.sh
```

`install.sh` will:
- Install Python / ALSA utils / curl / `libopus0`
- Create a `venv`
- Install `Pillow` and `python-dotenv`
- Download the `NotoSansSC-Bold.ttf` font
- Auto-register the app if `whisplay-daemon` is detected

## Configuration

First copy the config file:

```bash
cp .env.template .env
```

Important settings:

- `WHISPLAY_TALK_DEVICE_PREFIX`
  Default: `whisplay-talk-`
- `WHISPLAY_TALK_DEVICE_NAME`
  If empty, the system hostname is used. If it does not already have the prefix, the prefix is added automatically.
- `WHISPLAY_TALK_UDP_PORT`
  Default: `24680`
- `ALSA_INPUT_DEVICE`
  Recording device, default `default`
- `ALSA_OUTPUT_DEVICE`
  Playback device, default `default`
- `AUDIO_CODEC`
  Default `opus`, recommended for current real-time talkback
- `AUDIO_FRAME_MS`
  Default `40`, which lowers packet rate and usually helps continuity on weaker links
- `AUDIO_REDUNDANCY_FRAMES`
  Default `1`, which resends the previous compressed frame to help recover a single lost packet
- `AUDIO_OPUS_BITRATE`
  Default `16000`, tuned for mono intercom voice with stronger continuity
- `AUDIO_OPUS_COMPLEXITY`
  Default `6`, still light enough for Raspberry Pi while improving encode quality a bit
- `AUDIO_OPUS_PACKET_LOSS_PERC`
  Default `15`, hints expected network loss to the Opus encoder
- `AUDIO_OPUS_ENABLE_FEC`
  Default `1`, enables Opus in-band forward error correction
- `WHISPLAY_TALK_RECEIVE_PREBUFFER_FRAMES`
  Default `24`, roughly one second of audio at the current 40ms Opus frame size

It is recommended to give all devices consistent hostnames, for example:

```bash
sudo hostnamectl set-hostname whisplay-talk-kitchen
sudo hostnamectl set-hostname whisplay-talk-office
```

Also make sure all devices have joined the same Tailscale tailnet.

## Run

```bash
bash run.sh
```

If `whisplay-daemon` is running on the system, it is recommended to launch `Talk` from the daemon app list.

For systems that do not use `whisplay-daemon`, you can configure boot startup with:

```bash
bash startup.sh
```

`startup.sh` installs a `systemd` service for this app. If it detects `whisplay-daemon`, it exits without making changes.

## Interaction

- While idle:
  The screen shows the online device list
- If Tailscale is not installed:
  The screen shows an install reminder
- If Tailscale is installed but not logged in or not running:
  The screen shows the matching login/start hint
- While holding the button:
  The local device enters `Speaking`
- After releasing the button:
  Sending stops and an end packet is broadcast
- When remote audio is received:
  The device enters `Receiving`, plays audio, and shows who is speaking

## UDP Packet Format

The current implementation uses a small custom header:

- magic: `WT01`
- type: `1`
- flags:
  `1 = start`, `2 = end`
- sender name
- stream id
- sequence
- codec id
- compressed audio payload, typically `Opus`
- optional redundant payload for the previous frame

This makes it easy to evolve later toward:
- Opus compression
- Unicast priority
- Push-to-talk arbitration
- Half-duplex / full-duplex strategy
- Stronger packet loss handling

## Known Limits

This is still an MVP, so a few things are not implemented yet:

- It now defaults to `Opus`, but still uses a simple custom RTP-less transport instead of a full media stack
- No arbitration yet; if two devices speak at once, the latest incoming stream wins
- It currently prefers `whisplay-daemon` and does not yet provide a full direct-hardware fallback
- No peer nickname management yet; names are derived from hostnames

## Suggested Next Steps

If we continue from here, the highest-value next improvements would be:

1. Add Opus to reduce Tailscale bandwidth usage
2. Add a simple channel lock to avoid two people grabbing the mic at the same time
3. Refine the UI to look closer to `whisplay-ai-chatbot`
4. Add more daemon-facing install artifacts such as a desktop entry or app manifest

## License

This project is licensed under the GPL-3.0 license. See [LICENSE](LICENSE).
