# Cameras

[← README](../README.md)

## One camera per entrance station

Each entrance station with a camera has its own camera entity:

- **Camera** shows the station chosen in the options;
- each other station has a camera named after it, for example **Back
  camera**;
- with a single entrance station, only **Camera** exists.

## Live view and still images

- **Live view** (WebRTC, video and sound):
  - opening a camera starts a monitoring session on its entrance station;
  - the station ends each session after **about 60 seconds**; while the live
    view is open, a new session is opened each time, and the view continues
    after a pause of a few seconds;
  - several viewers share the same sessions, which stop a few seconds after
    the last viewer closes the live view.
- **Still image**:
  - refreshed only while it is displayed, for example on a dashboard card;
  - at most once per refresh interval (30 seconds by default, from 10 to 3600
    seconds in the options);
  - otherwise the last image is kept, and while a live view runs, images come
    from it.
  - A new image takes several seconds, since a monitoring session starts each
    time. So the camera shows the last image at once and the new one at the
    next refresh.

Each image or live view opens a monitoring session on the entrance station.
Depending on the installation, this may turn on the monitor screen or the
station's LED or lighting, and it keeps the station busy while it lasts.

## One session at a time

The intercom runs **one monitoring session at a time**, so the cameras take
turns:

- **Images** of several cameras are taken one after the other.
- **A live view goes first**:
  - it joins the session of an image of its own station;
  - an image of another station that has not started is skipped, and that
    camera keeps its last image;
  - an image whose station is answering the request (2 to 4 seconds) is
    never interrupted: the session is closed as soon as the station answers,
    without the image. Interrupting it could leave a session open on the
    station, which would then refuse every new one.
- **While the live view of one station is open**, or a call is answered:
  - the other cameras keep their last image;
  - opening their live view shows an explanation instead, so close the live
    view first;
  - a camera kept open for a returning viewer (a few seconds) gives way
    immediately.
- **The output buttons** use the open session, if any, without opening a new
  one.
- **A camera always shows its own station**: when its station is busy, it
  keeps its last image.

The **Camera activity** sensor tells what the session is doing, and for which
station: taking an image, live view, call, station busy, or idle. Show it next
to the cameras on a dashboard: a live view that takes a few seconds to start
then has a visible reason.

## When a station is busy

A station may refuse a session (busy) or not answer in time, for example
while the Aiphone app or the indoor monitor uses it, or while a session is
still closing.

- **Live view:** the integration asks again a few times over about ten
  seconds, then shows why it could not start ("The entrance station is busy…"
  or "did not answer in time").
- **Images:** the camera keeps its last image and does not ask that station
  again for 30 seconds, so that it does not keep it busy. The Camera activity
  sensor shows `busy` meanwhile.

## While a station rings

Rings are always received, even while a session is open or starting: they do
not go through the monitoring session.

A camera never opens a monitoring session on a station that rings, since that
may end the call:

- it keeps the last image, or the visitor's image with the snapshot on ring;
- its live view is refused with an explanation;
- the cameras of the other stations keep working.

Answer the call to see the visitor (see [Rings and calls](calls.md)).

## go2rtc

The cameras rely on [go2rtc](https://github.com/AlexxIT/go2rtc):

- **Home Assistant OS and Container:** Home Assistant runs it itself, through
  the go2rtc integration of the default configuration.
- **Home Assistant Core**, or to use another go2rtc server (1.9.15 or later):
  enter its URL in the options.

The integration creates two streams in go2rtc, shared by all the cameras:

- `aiphone_jo_<entry id>` receives the sessions. It is emptied before each
  session, so a camera never shows the previous session's video.
- `aiphone_jo_<entry id>_live` is the stream the live view reads. It re-reads
  the first one through go2rtc's RTSP server and ffmpeg, without transcoding,
  so that viewers move to each new session.

The go2rtc run by Home Assistant provides both. With another go2rtc server:

- **Keep its `rtsp` module enabled** (the default). Its port and credentials
  do not matter: go2rtc reads the stream from its own RTSP server on the
  loopback address.
- **Make ffmpeg available to go2rtc.** It is included in the official Docker
  image. If `exec.allow_paths` is set, allow `ffmpeg`.

Without the RTSP server, the live view stops with each session (about
60 seconds) and can be opened again. Without ffmpeg, the live view cannot
start.

If go2rtc cannot be reached, the cameras are unavailable and a repair notice
explains what to do. The other entities keep working.
