# Rings and calls

[← README](../README.md)

## Ring event

Every ring fires an `aiphone_jo_ring` event with these fields:

| Field | Content |
|---|---|
| `station` | Name of the station that rang |
| `raw_name` | Raw name of that station |
| `ringing_at` | Time of the ring |
| `device_id` | The monitor device |
| `snapshot` | `true` when an image of the visitor was captured |
| `image_entity_id` | The Last visitor image entity |
| `image_url` | Link to the visitor's image; empty without an image |

The event is fired right away. With the snapshot on ring, it is fired once
the capture is done, after a few seconds and at most about 12.

Example of a notification with the image, for the Home Assistant companion
app:

```yaml
automation:
  - alias: Visitor at the door
    triggers:
      - trigger: event
        event_type: aiphone_jo_ring
    actions:
      - action: notify.mobile_app_my_phone
        data:
          title: "Someone is at the door"
          message: "{{ trigger.event.data.station }}"
          data:
            image: "{{ trigger.event.data.image_url }}"
```

If no image could be captured, `image_url` is empty and the notification is
sent without it.

## Snapshot on ring

Experimental option, disabled by default. It works on the real installations
that reported it; please report how it behaves with yours.

Each ring captures an image of the visitor from the call itself, without
answering it:

- the integration receives the call's video for a few seconds, keeps one
  image, then leaves the call;
- the call keeps ringing on the monitor and the other phones, and can still
  be answered;
- the image is shown by the **Last visitor** entity and by the camera of the
  station that rang, and kept across restarts.

If the call is answered from Home Assistant meanwhile, the image is taken from
the answered call. A session open for camera images gives way to the
visitor's image; only an open camera live view prevents it. If
the entrance station only sends video to an answered call, no image can be
captured.

## Call answering (experimental)

Disabled by default, and not yet validated on real hardware. When enabled in
the options, the integration adds the **Answer**, **Hang up** and **Reject**
buttons and a **Call** sensor (see [Entities](entities.md)).

- **The buttons** stay available while the cloud is connected. Pressing one
  when no call rings shows an error that says so; for Hang up, the error
  appears when no call was answered from Home Assistant.
- **After Answer**, open the camera of the station that rang: its live view
  shows the visitor with sound. The other cameras keep their last image until
  the call ends.
- **A call is hung up automatically** after 200 seconds (the limit of the
  official app), and when the integration is reloaded.
- **If another phone answers first**, the call shows `answered_elsewhere`.
  From then on, the call belongs to that phone.
- **The Call sensor's `end_reason`** tells why the call ended:
  `answered_elsewhere`, `ended_by_station`, `cancelled_by_station`,
  `expired`, `rejected` or `ended_by_client`.
- **Answering also takes the call:** the monitor and the other phones stop
  ringing.
- **Call answering needs the camera** (go2rtc). The output buttons keep
  working during a call.

## Talking to the visitor (experimental)

With the **Talk to the visitor** option (disabled by default), the integration
also opens the audio path towards the entrance station when you answer, and
the camera offers the microphone in its live view. Requirements:

- a Home Assistant version with two-way audio for cameras, with that preview
  feature enabled in **Settings → System → Labs**;
- the go2rtc server run by Home Assistant (Home Assistant OS or Container). A
  go2rtc server on another host, set in the options, cannot reach the
  integration's local audio endpoint;
- microphone access in the browser or app, which requires HTTPS outside the
  local network.

Answer the call, open the camera, then turn the microphone on in the live
view. If the audio path cannot be opened, the call goes on without it and a
warning is logged.

This feature follows the official app's behaviour but has not been validated
on real hardware yet. Please report how it behaves with your installation.
