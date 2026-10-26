# Changelog

All notable changes to this integration.

## Unreleased

- Entrance stations that keep their factory default Japanese name (such as
  `玄関1`) are shown translated, for example **Entrance 1** in English or
  **Entrée 1** in French, in the options, the ring event and the last ringing
  station sensor. Custom names are unchanged. The name returned by the Aiphone
  service is available in the new `raw_name` attribute.
- Pairing: when the monitor ends the "add smartphone" mode while the
  certificate is being checked, the approval is kept and the pairing can be
  resumed, instead of failing with an unknown error.

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
