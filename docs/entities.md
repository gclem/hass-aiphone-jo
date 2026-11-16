# Entities

[← README](../README.md)

All the entities belong to one device, the monitor.

| Platform | Entity | Description |
|---|---|---|
| `event` | Ring | Fired on each ring, with the `station` that rang and its `raw_name`. |
| `binary_sensor` | Ringing | On while a station is calling. Off when the call is answered elsewhere, cancelled, or after 60 seconds. |
| `sensor` | Last ringing station | Name of the station that rang last, with `rang_at` and `raw_name`. Kept across restarts. |
| `button` | Action 1 | Pulses output 1 of the monitor (unlock output). |
| `button` | Action 2 | Pulses output 2 of the monitor (option output). |
| `camera` | Camera | Image and live view of the entrance station chosen in the options, with its `station`. |
| `camera` | *Station* camera | Image and live view of each other entrance station with a camera (for example **Back camera**), with its `station`. |
| `sensor` | Camera activity | What the shared monitoring session is doing: `idle`, `command` (sending an output command), `image` (taking an image), `live_view`, `call` or `busy` (the station refused or did not answer), with the `station` concerned. See [Cameras](cameras.md#one-session-at-a-time). |
| `image` | Last visitor | Image captured at the last ring, with `station` and `ringing_at`. Kept across restarts. Only with the snapshot on ring option. |
| `binary_sensor` | Cloud connection | Diagnostic: connection to the Aiphone cloud. Attributes: `connected_since`, `disconnected_since`, `last_error`, `reconnections` and `ring_topic_subscribed`. |
| `event` | Problem | Diagnostic: fired on every problem, with its `message` (see [Problems and errors](problems.md)). |

With the **call answering** option, see [Rings and calls](calls.md#call-answering-experimental):

| Platform | Entity | Description |
|---|---|---|
| `button` | Answer | Answers the ringing call. |
| `button` | Hang up | Ends the call answered from Home Assistant. |
| `button` | Reject | Declines the ringing call. |
| `sensor` | Call | `idle`, `ringing`, `connecting`, `talking`, `ended`, `rejected`, `missed`, `answered_elsewhere` or `failed`. Attributes: `station`, `started_at` and `end_reason`. |

## Availability

- Most entities are unavailable while the cloud connection is down; the
  integration reconnects automatically.
- **Cloud connection** and **Problem** stay available, to tell why.
- **Cameras** are unavailable when go2rtc cannot be reached. A repair notice
  then explains what to do, and the other entities keep working.

## Output buttons

What each output does depends on your installation: a door strike, a gate
motor, lighting or any other equipment wired to it. Rename the buttons in
Home Assistant to match it.

The buttons send **pulses**: they are not locks or switches, and the intercom
does not report any state for its outputs.

- **How a press works:** the monitor only accepts output commands during a
  monitoring session.
  - If a session is open (live view, camera image, previous press), the
    command goes through it at once, whatever its station.
  - Otherwise the integration opens one on an entrance station (a few
    seconds), sends the command, and **keeps the session 40 seconds**. The
    next presses within that time are immediate: for a gate, open, stop,
    then close.
  - The station may show the session on its screen.
- **Presses do not pile up:** one press being sent and one waiting at most.
  Pressing more while they are sent shows "A press of this button is already
  waiting to be sent", instead of queueing monitoring requests that would
  saturate the station. The Camera activity sensor shows `command` while a
  press is being sent.
- **Busy station:** if the station is busy (call or monitoring in progress),
  the other camera stations are tried.
- **While a station rings:** the buttons use another station first, because
  monitoring the station that calls may end the call.
- **During a live view or a call:** the buttons keep working.
