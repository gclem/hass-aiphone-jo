# Contributing

Bug reports, feature requests and pull requests are welcome. Upcoming features
are listed in the [roadmap](ROADMAP.md).

## Reporting a bug

Open an issue with:

- the integration and Home Assistant versions;
- what you did, what you expected and what happened;
- the error message or code shown, if any;
- relevant logs or the integration diagnostics
  (*Settings → Devices & services → Aiphone JO → ⋮ → Download diagnostics*).

Before sharing anything, remove what identifies your installation: MAC
addresses, client IDs, MQTT topics, cloud endpoints, IP addresses, serial
numbers, certificates, keys, one-time passwords and credentials. Diagnostics
are redacted, but check them anyway.

## Development setup

Python 3.13 is required.

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -r requirements_test.txt -r requirements_lint.txt aiomqtt
ruff check . && ruff format --check .
pytest
```

## Project layout

| Path | Content |
|---|---|
| `custom_components/aiphone_jo/` | The integration. |
| `custom_components/aiphone_jo/vendor/aiphone_jo/` | Bundled copy of the [aiphone-jo](https://github.com/gclem/aiphone-jo-lib) library. Do not edit it here: protocol changes belong in the library. |
| `tests/` | Tests based on `pytest-homeassistant-custom-component`. |

The integration contains no protocol logic: it relies on the library's public
API for discovery, pairing, the cloud connection and commands.

## Pull requests

- Keep changes focused and add tests for new behaviour.
- Write code, comments, documentation and commit messages in English.
- Update `strings.json`, `translations/en.json` and `translations/fr.json`
  together; every error message should tell the user what to do.
- Add user-facing changes to the `Unreleased` section of `CHANGELOG.md`.

## Data and privacy rules

- **No real data** in code, tests, commits, issues or pull requests: no MAC
  address, client ID, real topic, real cloud endpoint, LAN IP, local path, host
  name, certificate, key or one-time password. Use fictitious values only, such
  as `192.0.2.x` ([RFC 5737](https://www.rfc-editor.org/rfc/rfc5737)),
  `00:00:5e:00:53:xx` ([RFC 7042](https://www.rfc-editor.org/rfc/rfc7042)) and
  `example.invalid` ([RFC 2606](https://www.rfc-editor.org/rfc/rfc2606)).
- **Credentials live only in the config entry** (`entry.data`). They are never
  written to logs, entity attributes, events or exception messages.
- **Diagnostics are always redacted**: every sensitive field goes through
  `async_redact_data` with the `TO_REDACT` set from `const.py`. Extend that set
  whenever a new sensitive field is stored.
