# Changelog

All notable changes to this integration.

## 0.4.19

- "No video received" now also tells what the session did while its video
  was awaited: when go2rtc listed it, whether it found a network path to the
  Aiphone relay (transport and kind of address, never an address), and when
  go2rtc dropped it. go2rtc forgets a session whose connection ended, so a
  session that connected then ended was reported like one that never
  arrived.
- The diagnostics list the last session starts of the cameras
  (`camera_sessions`): station, purpose, whether video came, and how long
  each step took. Problems are reported once per kind of error, so repeated
  failures were not all visible.

## 0.4.18

- Closing the live view while it is still starting no longer leaves the
  camera streaming: the session used to stay open, renewed every minute,
  until Home Assistant restarted, keeping the station busy for the images of
  the other cameras. It now stops a few seconds after the start, like a
  live view closed normally.
- When a camera session gets no video ("No video received"), the problem
  message now says how far the session got: whether go2rtc received the
  pushed session, which tracks reached it and how many packets, and what the
  offer and answer held (media sections and number of candidates, no
  address). The diagnostics and the `aiphone_jo_error` event carry it.

## 0.4.17

- The monitoring sessions are recorded with their status (library 0.4.7).
  Those that went wrong (given up, lost with the connection) are stored
  privately and closed again after a restart, after each connection and when
  the station answers busy, so that a session left open on the station no
  longer keeps it busy until it is unplugged. Only problematic sessions are
  written to disk, at most 50, for 15 days at most. The diagnostics show the
  recent history, without the session identifiers.
- Update the aiphone-jo library to 0.4.7.

## 0.4.16

- A station that refused every session (486) after monitoring requests left
  without an answer recovers better: the integration waits up to 30 seconds
  for the station's answer instead of 13, and on a busy answer it closes
  again the sessions it gave up earlier (library 0.4.6).
- Update the aiphone-jo library to 0.4.6.

## 0.4.15

- Diagnostic service `aiphone_jo.test_output_without_session`: sends one
  output command without a monitoring session and returns the monitor's
  reply, to check on real hardware whether the session is needed. It may
  operate the equipment wired to the output.

## 0.4.14

- Output buttons, for gates in particular: a press opens a monitoring
  session and keeps it 40 seconds, so the next presses (stop, then close)
  go through it at once instead of opening a new session each. Repeated
  presses saturated the station, which then refused every session (486).
- Presses no longer pile up: one being sent and one waiting at most; more
  are refused with a message.
- A press goes through any open session (live view, camera image) at once.
- The Camera activity sensor shows `command` while a press is being sent.

## 0.4.13

- The integration has its own icon and logo in Home Assistant (Home Assistant
  2026.3 or later), instead of "icon not available".

## 0.4.12

- A station that refused a session or did not answer could stay busy for a
  long time (over 20 minutes on real hardware): its end of session was not
  answered when the integration had given the session up (library 0.4.5).
  An image whose station is answering the request is no longer interrupted
  by a live view: its session is closed as soon as the station answers.
- After a refusal or no answer, a camera does not ask that station again for
  30 seconds, instead of at every image.
- A live view retries a busy station for about ten seconds, then says why it
  could not start ("The entrance station is busy…", "did not answer in
  time") instead of `StationBusyError`.
- New **Camera activity** sensor: idle, taking an image, live view, call or
  station busy, with the station concerned.
- A session open for camera images gives way to the visitor's image on ring.
- The README is shorter: what the integration does, its status, features,
  requirements and installation. The details moved to guides in `docs/`
  (configuration, entities, cameras, rings and calls, problems). The roadmap
  is removed.
- Update the aiphone-jo library to 0.4.5.

## 0.4.11

- A camera no longer shows another station's video (#45). The video of an
  ended session stays in go2rtc for a few seconds, longer than the next
  session takes to start, so an image or the start of a live view could
  come from the previous station. The stream is now emptied before each
  session.

## 0.4.10

- Opening a live view interrupts the image being taken of another station,
  instead of waiting for it (about 8 seconds on real hardware): that camera
  keeps its last image. The library closes the interrupted session on the
  station (0.4.4), so the live view does not find the station busy.

## 0.4.9

- A live view goes before the camera images waiting for the session (#40):
  it waits only for the image being taken, if any, instead of every image
  queued before it (up to about 25 seconds with two cameras). An image of
  the same station being taken when the live view opens is kept as its
  session instead of being closed and opened again.
- Update the aiphone-jo library to 0.4.4.

## 0.4.8

- Camera images are refreshed in the background. Home Assistant gives up on a
  camera image after 10 seconds, about the time a monitoring session takes to
  start: images that took longer were lost and the camera showed an error. A
  camera now returns its last image at once while the new one is taken for
  the next refresh, and an image is kept even when Home Assistant gave up.

## 0.4.7

- A camera without any image yet (after a restart) waits up to 15 seconds for
  the session used by another station, instead of returning no image (HTTP
  500) while another camera's image or live view is in progress.

## 0.4.6

- One camera per entrance station (#32): installations with several
  entrance stations, for example on both sides of a gate, can now view each
  of them. **Camera** keeps showing the station chosen in the options (same
  entity, so dashboards keep working); each other station gets a camera
  named after it, such as **Back camera**. The intercom runs one monitoring
  session at a time: images are taken one after the other, and while a live
  view or an answered call uses the session, the other cameras keep their
  last image and refuse their live view with an explanation.
- A camera only shows its own station: when it is busy or ringing, the last
  image is kept instead of an image of another station. The visitor's image
  captured on ring is shown by the camera of the station that rang.
- Cameras report `streaming` while their live view or call runs, and have a
  `station` attribute.

## 0.4.5

- While the live view is open, camera images wait for its video: when the
  station renews its session, the live stream restarts and briefly carries
  only sound, which made images fail (`codecs not matched`) (#32).
- The README explains that the Problem entity keeps the last problem, while
  the Cloud connection sensor gives the current state.

## 0.4.4

- Device lookups no longer use the device registry call deprecated in Home
  Assistant 2027.8 (#31).
- A camera image that go2rtc cannot make yet (no key frame) is requested
  again, up to 3 times; errors keep go2rtc's reason (#32).
- When the snapshot on ring times out, the error says which step was too
  slow, instead of an empty `TimeoutError ()` (#33).

## 0.4.3

- Problems are reported in Home Assistant, not only in the logs: a **Cloud
  connection** diagnostic sensor (with the last error, the time of the last
  connection or disconnection and the number of reconnections), repairs when
  the Aiphone cloud stays unreachable for 5 minutes or refuses the ring topic,
  and a **Problem** event entity with the matching `aiphone_jo_error` event
  (lost connection, failed command, image, camera or call). The diagnostics
  keep the recent history and the signaling of the last call.
- A refused ring topic is now reported at startup too; it was only logged
  after a reconnection.
- While a station rings, still images and the live view no longer open a
  monitoring session on it, which may end the call (calls seen as missed
  after a few seconds); the output buttons use another station first.
- The Answer, Hang up and Reject buttons stay available while connected,
  instead of only during the few seconds of a ring; pressing one without a
  call says why.
- The Last visitor image, its station and time are kept across restarts.
- Update the aiphone-jo library to 0.4.3.

## 0.4.2

- Update the aiphone-jo library to 0.4.2.

## 0.4.1

- Experimental snapshot on ring, disabled by default: each ring captures an
  image of the visitor without answering, shown by the new Last visitor image
  entity and the camera. Every ring also fires an `aiphone_jo_ring` event,
  with a link to the image, for notifications with a photo (example in the
  README).
- Experimental call answering, disabled by default: Answer, Hang up and Reject
  buttons and a Call state sensor. An answered call shows the visitor with
  sound in the camera live view. Calls are hung up automatically after 200
  seconds. The sensor tells why a call ended,
  including when another phone answered first.
- Experimental talking to the visitor, disabled by default (option "Talk to
  the visitor"): when a call is answered, the camera live view offers the
  microphone with Home Assistant's two-way audio preview feature. Needs the
  go2rtc server run by Home Assistant.
- Update the aiphone-jo library to 0.4.1.

## 0.4.0

- Entrance stations that keep their factory default Japanese name (such as
  `玄関1`) are shown translated, for example **Entrance 1** in English or
  **Entrée 1** in French, in the options, the ring event and the last ringing
  station sensor. Custom names are unchanged. The name returned by the Aiphone
  service is available in the new `raw_name` attribute.
- Pairing: when the monitor ends the "add smartphone" mode while the
  certificate is being checked, the approval is kept and the pairing can be
  resumed, instead of failing with an unknown error.
- Continuous live view: while the camera is open, a new monitoring session is
  opened each time the entrance station ends one (about every minute), and
  the live view continues after an interruption of a few seconds. Sessions
  stop a few seconds after the last viewer leaves. This relies on go2rtc's
  RTSP server and ffmpeg (see the README for an external go2rtc server);
  without the RTSP server, the live view stops with each session as before.
- Update the aiphone-jo library to 0.4.0.

## 0.3.0

- New camera entity, through go2rtc: WebRTC live view with sound (about one
  minute per opening), and a still image of the chosen entrance station,
  refreshed only while displayed (interval configurable in the options,
  30 seconds by default). A go2rtc URL can be set in the options for Home
  Assistant Core installations.
- Update the aiphone-jo library to 0.3.0.

## 0.2.2

- The output buttons are now named **Action 1** (output 1, unlock) and
  **Action 2** (output 2, option), since their effect depends on each
  installation. Existing entities are kept; only their default name changes.
- Update the aiphone-jo library to 0.2.2.

## 0.2.1

- Clearer pairing errors, including refusals reported during discovery, an
  automatic new code after a wrong or expired code, and choosing another name
  when one is already used.
- The monitor is identified by a stable identifier; existing installations are
  migrated automatically, entities are kept.
- Re-pairing is requested as soon as the monitor or the Aiphone service refuses
  Home Assistant, including while running.
- The terminal is unregistered from the Aiphone service when the integration
  is removed or re-paired, and an unfinished pairing is cleaned up.
- Update the aiphone-jo library to 0.2.1.

## 0.2.0

- Pair with the monitor from Home Assistant: network discovery or manual
  address, certificate check, monitor code, terminal name, with actionable
  error messages and resumable steps.
- Ring event, ringing and last ringing station sensors.
- Buttons triggering the monitor's two outputs.
- Re-pairing (reauth and reconfigure), options to choose the entrance station
  used for the output buttons, and removal guidance.
- English and French translations.
- Update the aiphone-jo library to 0.2.0.

## 0.1.0

- Include the aiphone-jo protocol library (0.1.0).
- Security and privacy improvements, including redacted diagnostics.

## 0.0.1

- Initial release: installation through HACS and setup from the Home Assistant UI.
