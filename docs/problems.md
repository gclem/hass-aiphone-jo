# Problems and errors

[← README](../README.md)

Problems are not only logged, they are reported in Home Assistant:

- **Cloud connection sensor:** whether the integration is connected, since
  when, the last error and how many times it reconnected.
- **Repairs:** a repair appears in two cases:
  - the Aiphone cloud stays unreachable for 5 minutes;
  - the cloud refuses the ring topic, so rings would be missed.

  Each one explains what to do and disappears once the problem is gone.
- **Problem entity and `aiphone_jo_error` event:** fired on every problem,
  with `type`, `message`, `at`, `entry_id` and `device_id`. The types are
  `connection_lost`, `cloud_unreachable`, `auth_refused`,
  `ring_topic_refused`, `ring_snapshot_failed`, `camera_failed`,
  `call_failed` and `command_failed`.
- **Diagnostics:** they keep the recent connections, problems and the
  signaling of the last call.

The Problem entity shows the **last** problem, even once it is solved. After a
lost connection, it still says so while the Cloud connection sensor is back to
connected. Use the sensor and the repairs for the current state.

Messages never contain the identifiers of the terminal. Example of a
notification for lasting problems only:

```yaml
automation:
  - alias: Intercom problem
    triggers:
      - trigger: event
        event_type: aiphone_jo_error
    conditions:
      - condition: template
        value_template: >
          {{ trigger.event.data.type not in ["connection_lost", "camera_failed"] }}
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Intercom problem"
          message: "{{ trigger.event.data.message }}"
```

## No video received

A camera image or live view starts a monitoring session, pushes it into
go2rtc, then waits 20 seconds for video. When none comes, the `camera_failed`
problem says how far the session got, for example:

```text
No video received (no pushed session; seen from 1.2 s to 6.4 s with no track, 0 bytes received, path udp prflx by 1.2 s; offer audio+video, 4 candidates, answer audio+video, 8 candidates, answered 21.3 s ago)
```

The first part is what go2rtc holds when the wait ends:

- **`pushed session: no track`:** go2rtc still has the session but no media
  reached it.
- **`video 0 packets`:** the video track was set up but stayed empty.
- **`no pushed session`:** go2rtc has no session any more. It drops a
  session as soon as its connection ends (closed by the Aiphone relay, lost,
  or failed encryption).
- **`no offer pushed`** (last part): the session never reached go2rtc.

The second part is what the session did during the wait, in seconds after
go2rtc answered the offer:

- **`seen from … to …`:** when go2rtc listed the session, and what it had
  received when last seen. Without `to`, it is still there.
- **`path udp prflx by …`:** go2rtc found a network path to the Aiphone
  relay: its transport (`udp`, `tcp`) and the kind of address of the relay
  (`host`, `srflx`, `prflx`, `relay`).
- **`no path found`:** go2rtc never reached the relay: firewall, UDP
  blocked, no usable candidate. go2rtc keeps such a session 30 seconds, so
  it is still listed when the wait ends.
- **`never seen`:** go2rtc never listed the session.

Only media kinds, counts, transports and kinds of address are reported, never
addresses.

The diagnostics also list the last session starts of the cameras
(`camera_sessions`): the station, what the session was for (`image`,
`live_view`, `command`), whether video came (`video`, `no_video`, `failed: …`,
`interrupted`), and how long each step took, in seconds after the start:

| Field | Step |
| --- | --- |
| `request_s` | The stream is ready, the station is asked for a session |
| `offer_s` | The relay's offer is received |
| `answer_s` | go2rtc answered it |
| `negotiated_s` | The station accepted the answer |
| `video_s` | First video packets (or `failed_s`: when the start failed) |

A problem is reported once per kind of error until an image succeeds; this
list tells every session that got no video.

## Monitoring sessions left open

The intercom refuses every new monitoring session ("busy") while one is left
open on the station, for example a session given up because the station did
not answer in time, or lost with the connection. The integration:

- keeps a history of its monitoring sessions with their status;
- closes again, once each, the sessions that may still be open: after each
  connection to the cloud (so also after a restart), and when the station
  answers busy;
- stores only the sessions that went wrong and how they ended, privately,
  so that they can still be closed after a restart. Nothing is written for
  ordinary sessions. At most 50 are kept, for 15 days at most;
- shows the recent history in the diagnostics (`sessions`), with the time
  of each session but not its identifier.

## Reporting a problem

Open an [issue](https://github.com/gclem/hass-aiphone-jo/issues) and attach:

- the integration version, shown in **Settings → Devices & services → Aiphone
  JO**;
- the diagnostics: device page → ⋮ → **Download diagnostics**. They are
  redacted, but check them before sharing;
- the error messages from **Settings → System → Logs**.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the details.
