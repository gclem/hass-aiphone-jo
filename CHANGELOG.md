# Changelog

All notable changes to this integration.

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
