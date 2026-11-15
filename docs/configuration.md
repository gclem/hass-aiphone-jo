# Configuration

[← README](../README.md)

## Pairing

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

## Troubleshooting pairing

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

## Options

**Configure** lets you choose:

- **Preferred entrance station** (the first camera station by default):
  - the output buttons use it first, and the **Camera** entity shows it;
  - changing it keeps the **Camera** entity (and your dashboards) on the new
    station, and the station previously shown gets its own camera entity.
- **Camera image refresh interval**: 30 seconds by default, from 10 to 3600.
  - With several cameras on a dashboard, keep it at 30 seconds or more. Each
    image takes several seconds and keeps the station busy (see
    [Cameras](cameras.md)).
- **go2rtc server**: its URL, with optional credentials, when Home Assistant
  does not run go2rtc itself (Home Assistant Core installations).
- **Snapshot on ring**, **call answering** and **talking to the visitor**,
  all disabled by default (see [Rings and calls](calls.md)).

Changes apply immediately, without restarting Home Assistant.

## Pairing again

- **Reconfigure** pairs again with the same monitor, optionally with another
  terminal name.
- **When the monitor no longer accepts Home Assistant**, for example because
  the terminal was deleted on the monitor:
  - Home Assistant notices it, even while running, and asks you to pair
    again;
  - the monitor is identified by a stable identifier, so re-pairing a
    different monitor is refused.

## Removing the integration

Removing the integration unregisters the terminal from the Aiphone service and
deletes the stored identity and the last visitor image.

- **If the terminal still appears** in the monitor's smartphone settings
  (`HomeAssist` by default), delete it there to free its slot.
- **If unregistering fails**, Home Assistant shows a notification with the
  exact name to delete.
