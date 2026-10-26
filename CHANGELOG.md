# Changelog

All notable changes to this integration.

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
- Large opening and Small opening buttons.
- Re-pairing (reauth and reconfigure), options to choose the entrance station
  used for openings, and removal guidance.
- English and French translations.
- Update the aiphone-jo library to 0.2.0.

## 0.1.0

- Include the aiphone-jo protocol library (0.1.0).
- Security and privacy improvements, including redacted diagnostics.

## 0.0.1

- Initial release: installation through HACS and setup from the Home Assistant UI.
