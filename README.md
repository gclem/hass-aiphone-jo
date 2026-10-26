# Aiphone JO for Home Assistant

> [!WARNING]
> **Unofficial** project, not affiliated with Aiphone Co., Ltd. Read the
> [disclaimer](DISCLAIMER.md) before use.

Custom Home Assistant integration, installable through [HACS](https://hacs.xyz),
for the **Aiphone JO-1MDW** video intercom. Only this model is targeted; other
monitors of the JO series are not supported.

## Architecture

The project is split into two repositories:

| Repository | Role |
|---|---|
| [`gclem/aiphone-jo-lib`](https://github.com/gclem/aiphone-jo-lib) | Asyncio Python library, independent from Home Assistant, holding the whole protocol core (package `aiphone-jo`). |
| `gclem/hass-aiphone-jo` (this repository) | Home Assistant integration for the community and HACS. Contains no protocol logic of its own. |

```mermaid
flowchart LR
    HA[Home Assistant] --> I[hass-aiphone-jo<br/>custom_components/aiphone_jo]
    I --> L[aiphone-jo<br/>aiphone-jo-lib]
    L --> J[Aiphone JO-1MDW]
```

The integration ships with its own copy of the library.

## Features

- **Ring** detection (call button pressed on an entrance station).
- **Action 1** and **Action 2** buttons, which trigger the monitor's two
  outputs.
- **Camera**: still image of an entrance station, refreshed while it is
  displayed, and a WebRTC live view with sound, through go2rtc.

See the [roadmap](ROADMAP.md) for upcoming features.

## Entities

All entities belong to one device, the monitor.

| Platform | Entity | Description |
|---|---|---|
| `event` | Ring | Doorbell event fired on each call, with the `station` that rang and its `raw_name`. |
| `binary_sensor` | Ringing | On while a station is calling; off when the call is answered elsewhere, cancelled, or after 60 seconds. |
| `sensor` | Last ringing station | Name of the station that rang last (kept across restarts), with `rang_at` and `raw_name`. |
| `button` | Action 1 | Pulses output 1 of the monitor (unlock output). |
| `button` | Action 2 | Pulses output 2 of the monitor (option output). |
| `camera` | Camera | Image and live view of the entrance station chosen in the options. |

What each output does depends on your installation: a door strike, a gate
motor, lighting or any other equipment wired to it. Rename the buttons in Home
Assistant to match it.

The buttons send **pulses**: they are not locks or switches, and the intercom
does not report any state for its outputs. The monitor only accepts output
commands during a short monitoring session: the integration opens one on an
entrance station, sends the command and closes it immediately. One session
runs at a time, so successive presses are queued. If the station is busy (call
or monitoring in progress), the other camera stations are tried.

### Camera

The camera shows the entrance station chosen in the options. It relies on
[go2rtc](https://github.com/AlexxIT/go2rtc), which Home Assistant runs itself
on Home Assistant OS and Container installations (go2rtc integration, part of
the default configuration). On a Home Assistant Core installation, or to use
another go2rtc server (1.9.15 or later), enter its URL in the options.

- **Live view** (WebRTC, video and sound): opening the camera starts a
  monitoring session on the entrance station. The station ends each session
  after **about 60 seconds**; the live view then stops and can be opened
  again. Several viewers share the same session, which also stops when the
  last viewer closes it.
- **Still image**: refreshed only while it is displayed (for example on a
  dashboard card), at most once per refresh interval (30 seconds by default,
  configurable from 10 to 3600 seconds in the options). Otherwise the last
  image is kept. While a live view runs, images come from it.

Each image or live view opens a monitoring session on the entrance station.
Depending on the installation, this may turn on the monitor screen or the
station's LED or lighting, and keeps the station briefly busy. The output
buttons keep working during a live view.

If go2rtc cannot be reached, the camera is unavailable and a repair notice
explains what to do; the other entities keep working.

Entities become unavailable while the cloud connection is down; the
integration reconnects automatically.

## Installation

### Through HACS (custom repository)

1. HACS → ⋮ menu → **Custom repositories**.
2. Add `https://github.com/gclem/hass-aiphone-jo` with the **Integration** type.
3. Install **Aiphone JO**, then restart Home Assistant.

### Manual

Copy `custom_components/aiphone_jo` into the `custom_components` folder of
your Home Assistant configuration, then restart.

## Configuration

Home Assistant pairs with the monitor like a smartphone, over your local
network. Pairing uses **one smartphone slot** on the monitor.

1. Make sure Home Assistant and the monitor are on the same local network.
2. On the monitor, open the smartphone settings and start **adding a
   smartphone**. Keep the monitor in that mode until pairing is complete.
3. In Home Assistant: **Settings → Devices & services → Add integration →
   Aiphone JO**, then choose **Search the local network** (or enter the
   monitor's IP address).
4. Check the certificate fingerprint and confirm. Nothing is sent to the
   monitor before this confirmation, and Home Assistant only accepts this
   certificate afterwards.
5. Enter the code displayed on the monitor without waiting.
6. Choose the terminal name shown in the monitor's smartphone list
   (12 characters maximum, `HomeAssist` by default).

The identity received from Aiphone is stored only in the Home Assistant
configuration entry. After a restart, Home Assistant reconnects on its own.

### Troubleshooting pairing

Every error message says what to do. The most common cases:

| Message | What to do |
|---|---|
| No monitor answered | Check that the monitor is in "add smartphone" mode, on the same subnet/VLAN as Home Assistant, and that UDP ports 51711 and 51712 are not blocked. Or enter the monitor's address. |
| Pairing interrupted | The "add smartphone" mode ended, or the monitor refused the last request (the reason is shown). Fix it, start the mode again on the monitor, then submit right away: pairing resumes at the code step. |
| Wrong code / code expired | Enter exactly the code shown. When the monitor issues a new code, Home Assistant asks for it automatically. |
| Name already used | Choose another terminal name. |
| Licence agreement not accepted | Accept it in the monitor's smartphone settings, then submit again. |
| Smartphone limit reached | Delete a smartphone on the monitor, then try again. |
| Another registration in progress | Finish or cancel the smartphone being added on the monitor. |
| Network problem on the monitor | Check the monitor's internet connection. |
| Incompatible version | Only the JO-1MDW is supported; update the monitor. |
| Certificate changed | Expected after a monitor reset or replacement only. Otherwise, do not continue. |
| Unexpected error (code) | Try again; if it persists, open an issue with the code shown. |

### Options

**Configure** lets you choose:

- the entrance station used for the output buttons and the camera (the first
  camera station by default);
- the camera image refresh interval (30 seconds by default, 10 to 3600);
- the URL of a go2rtc server, with optional credentials, when Home Assistant
  does not run go2rtc itself (Home Assistant Core installations).

Changes apply immediately, without restarting Home Assistant.

### Pairing again

- **Reconfigure** pairs again with the same monitor, optionally with another
  terminal name.
- If the monitor no longer accepts Home Assistant (for example, the terminal
  was deleted on the monitor), Home Assistant notices it, even while running,
  and asks you to pair again. The monitor is identified by a stable identifier,
  so re-pairing a different monitor is refused.

### Removing the integration

Removing the integration unregisters the terminal from the Aiphone service and
deletes the stored identity. If the terminal (`HomeAssist` by default) still
appears in the monitor's smartphone settings, delete it there to free its slot.
If unregistering fails, Home Assistant shows a notification with the exact
name to delete.

## Known limitations

- **Doorbell detection is experimental.** Ring events may be missed or
  delayed; please report whether they work with your installation.
- Call answering, two-way audio and output status are not available yet.
- The live view lasts about 60 seconds per opening; continuous live view is
  planned. Audio is one-way (from the entrance station).
- The output buttons briefly open a monitoring session on an entrance station;
  the station may show it on its screen.
- Unregistering the terminal when the integration is removed may not remove
  it from the monitor's smartphone list; delete it there if it still appears.
- Only the JO-1MDW monitor is supported.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).
