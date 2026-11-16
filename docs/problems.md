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
