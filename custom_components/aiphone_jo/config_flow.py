"""Config flow for the Aiphone JO integration: local pairing with the monitor."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_ADDRESS, CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    Station,
    async_discover,
    async_unregister,
    credentials_from_storage,
    credentials_to_storage,
    is_monitor_id,
    normalize_fingerprint,
    stations_from_room_info,
)
from .const import (
    CONF_CREDENTIALS,
    CONF_FINGERPRINT,
    CONF_GO2RTC_PASSWORD,
    CONF_GO2RTC_URL,
    CONF_GO2RTC_USERNAME,
    CONF_IMAGE_REFRESH,
    CONF_MONITOR,
    CONF_STATION,
    CONF_STATIONS,
    CONF_SYSTEM_VERSION,
    CONF_TERMINAL_NAME,
    DEFAULT_IMAGE_REFRESH,
    DEFAULT_TERMINAL_NAME,
    DEFAULT_TITLE,
    DOMAIN,
    MAX_IMAGE_REFRESH,
    MAX_TERMINAL_NAME,
    MIN_IMAGE_REFRESH,
)
from .errors import REASON_KEYS, FlowError, flow_error
from .go2rtc import Go2RtcError, async_check_server, async_get_server
from .notify import async_notify_manual_removal
from .vendor.aiphone_jo import (
    AiphoneJoError,
    CloudCredentials,
    DiscoveredMonitor,
    InvalidCredentialsError,
    Mid,
    PairingFailureReason,
    PairingFlow,
    PairingStep,
    PeerCertificate,
    UntrustedPeerError,
    get_room_info,
    pairing_reason,
)

_LOGGER = logging.getLogger(__name__)

CONF_METHOD = "method"
CONF_TRUST = "trust"
CONF_CODE = "code"
METHOD_DISCOVER = "discover"
METHOD_MANUAL = "manual"
CONF_MONITOR_CHOICE = "monitor"

# Code errors after which the library closed the link: a new code is requested
# automatically on a fresh connection.
_NEW_CODE_REASONS = frozenset(
    {PairingFailureReason.WRONG_CODE, PairingFailureReason.CODE_EXPIRED}
)


def _format_fingerprint(value: str) -> str:
    raw = normalize_fingerprint(value).upper()
    return ":".join(raw[i : i + 2] for i in range(0, len(raw), 2))


class AiphoneJoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Pair Home Assistant with an Aiphone JO-1MDW monitor."""

    VERSION = 1
    MINOR_VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow."""
        self._monitors: dict[str, DiscoveredMonitor] = {}
        self._monitor: DiscoveredMonitor | None = None
        self._pairing: PairingFlow | None = None
        self._peer: PeerCertificate | None = None
        self._expected_fingerprint: str | None = None
        self._terminal_name = DEFAULT_TERMINAL_NAME
        self._credentials: CloudCredentials | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Options flow."""
        return AiphoneJoOptionsFlow()

    @callback
    def async_remove(self) -> None:
        """Close the link and drop an unfinished registration when the flow ends."""
        if self._pairing is not None:
            pairing, self._pairing = self._pairing, None
            self.hass.async_create_task(pairing.cancel())

    # ------------------------------------------------------------------ helpers

    def _form(
        self,
        step_id: str,
        schema: vol.Schema,
        error: FlowError | None = None,
        placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        return self.async_show_form(
            step_id=step_id,
            data_schema=schema,
            errors={"base": error.key} if error else {},
            description_placeholders={
                "code": "",
                **(placeholders or {}),
                **(error.placeholders if error else {}),
            },
        )

    def _pairing_flow(self) -> PairingFlow:
        if self._pairing is None:
            self._pairing = PairingFlow(async_get_clientsession(self.hass))
        return self._pairing

    def _get_entry(self) -> ConfigEntry | None:
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    async def _async_on_pairing_error(
        self,
        err: AiphoneJoError,
        step_id: str,
        schema: vol.Schema,
        placeholders: Mapping[str, str] | None = None,
    ) -> ConfigFlowResult:
        """Retry on the same connection when possible, otherwise resume."""
        error = flow_error(err)
        if getattr(err, "retry_same_connection", False):
            return self._form(step_id, schema, error, placeholders)
        reason = getattr(err, "reason", None)
        if step_id == "code" and reason in _NEW_CODE_REASONS:
            try:
                await self._pairing_flow().reconnect()
                await self._pairing_flow().request_code()
            except AiphoneJoError:
                return self._resume_form(error)
            key = (
                "code_expired"
                if reason is PairingFailureReason.CODE_EXPIRED
                else ("invalid_code_new_code")
            )
            return self._form("code", schema, FlowError(key, error.placeholders))
        return self._resume_form(error)

    # --------------------------------------------------------- monitor choice

    def _user_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_METHOD, default=METHOD_DISCOVER): SelectSelector(
                    SelectSelectorConfig(
                        options=[METHOD_DISCOVER, METHOD_MANUAL],
                        translation_key=CONF_METHOD,
                        mode=SelectSelectorMode.LIST,
                    )
                )
            }
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the prerequisites and choose how to find the monitor."""
        if user_input is None:
            return self._form("user", self._user_schema())
        if user_input[CONF_METHOD] == METHOD_MANUAL:
            return await self.async_step_manual()
        return await self._async_discover("user", self._user_schema())

    def _manual_schema(self, host: str = "") -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=host): TextSelector(),
                vol.Optional(CONF_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
            }
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Find the monitor at a given address."""
        if user_input is None:
            return self._form("manual", self._manual_schema())
        host = user_input[CONF_HOST].strip()
        return await self._async_discover(
            "manual", self._manual_schema(host), host, user_input.get(CONF_PORT)
        )

    async def _async_discover(
        self,
        step_id: str,
        schema: vol.Schema,
        host: str | None = None,
        port: int | None = None,
    ) -> ConfigFlowResult:
        try:
            found = await async_discover(self.hass, host)
        except AiphoneJoError as err:
            return self._form(step_id, schema, flow_error(err))
        monitors = [monitor for monitor in found if monitor.accepted]
        refused = [monitor for monitor in found if not monitor.accepted]
        if not monitors and refused:
            reason = pairing_reason(Mid.DISCOVERY_REP, refused[0].rslt)
            key = REASON_KEYS.get(reason, "monitor_rejected")
            return self._form(
                step_id,
                schema,
                FlowError(key, {"code": f"MID 11001, RSLT {refused[0].rslt}"}),
            )
        if not monitors and host and port:
            monitors = [DiscoveredMonitor(address=host, port=port)]
        if not monitors:
            key = "no_monitor_at_address" if host else "no_monitor_found"
            return self._form(step_id, schema, FlowError(key))
        if len(monitors) == 1:
            self._monitor = monitors[0]
            return await self.async_step_confirm_fingerprint()
        self._monitors = {f"{m.address}:{m.port}": m for m in monitors}
        return await self.async_step_pick_monitor()

    async def async_step_pick_monitor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose among several monitors in "add smartphone" mode."""
        if user_input is not None:
            self._monitor = self._monitors[user_input[CONF_MONITOR_CHOICE]]
            return await self.async_step_confirm_fingerprint()
        return self._form(
            "pick_monitor",
            vol.Schema(
                {
                    vol.Required(CONF_MONITOR_CHOICE): SelectSelector(
                        SelectSelectorConfig(options=list(self._monitors))
                    )
                }
            ),
        )

    # ------------------------------------------------------------ TLS trust

    def _trust_schema(self) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_TRUST, default=False): bool})

    def _already_paired(self, fingerprint: str) -> bool:
        current = self._get_entry()
        return any(
            entry.data.get(CONF_MONITOR, {}).get(CONF_FINGERPRINT) == fingerprint
            and (current is None or entry.entry_id != current.entry_id)
            for entry in self._async_current_entries(include_ignore=False)
        )

    async def async_step_confirm_fingerprint(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the monitor certificate fingerprint (trust on first use)."""
        pairing = self._pairing_flow()
        if user_input is None:
            assert self._monitor is not None
            try:
                peer = await pairing.connect(
                    self._monitor, expected_fingerprint=self._expected_fingerprint
                )
            except UntrustedPeerError as err:
                self._peer = err.peer
                return await self.async_step_fingerprint_changed()
            except AiphoneJoError as err:
                error = flow_error(err)
                if error.key == "link_closed":
                    error = FlowError("cannot_connect", error.placeholders)
                return self._form("user", self._user_schema(), error)
            self._peer = peer
            if self._already_paired(normalize_fingerprint(peer.fingerprint)):
                return self.async_abort(reason="already_configured")
            if pairing.step is PairingStep.CONNECTED:
                return await self.async_step_code()
            return self._form(
                "confirm_fingerprint",
                self._trust_schema(),
                placeholders=self._peer_placeholders(peer),
            )
        if not user_input[CONF_TRUST] or self._peer is None:
            return self.async_abort(reason="fingerprint_rejected")
        pairing.approve(self._peer.fingerprint)
        return await self.async_step_code()

    def _peer_placeholders(self, peer: PeerCertificate | None) -> dict[str, str]:
        monitor = self._monitor
        valid_until = (
            peer.not_after.date().isoformat() if peer and peer.not_after else "-"
        )
        return {
            "fingerprint": _format_fingerprint(peer.fingerprint) if peer else "",
            "valid_until": valid_until,
            "address": monitor.address if monitor else "",
        }

    async def async_step_fingerprint_changed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The monitor presents a different certificate than the paired one."""
        if user_input is None:
            placeholders = self._peer_placeholders(self._peer)
            previous = self._expected_fingerprint or ""
            placeholders["previous"] = _format_fingerprint(previous)
            return self._form(
                "fingerprint_changed", self._trust_schema(), placeholders=placeholders
            )
        if not user_input[CONF_TRUST] or self._peer is None:
            return self.async_abort(reason="fingerprint_rejected")
        new = normalize_fingerprint(self._peer.fingerprint)
        if self._already_paired(new):
            return self.async_abort(reason="already_configured")
        self._expected_fingerprint = new
        return await self.async_step_confirm_fingerprint()

    # ------------------------------------------------------------------ code

    def _code_schema(self) -> vol.Schema:
        return vol.Schema({vol.Required(CONF_CODE): TextSelector()})

    async def async_step_code(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Request a code on the monitor and enter it."""
        pairing = self._pairing_flow()
        schema = self._code_schema()
        if user_input is None:
            if pairing.step is PairingStep.CONNECTED:
                try:
                    await pairing.request_code()
                except AiphoneJoError as err:
                    return await self._async_on_pairing_error(err, "code", schema)
            return self._form("code", schema)
        try:
            await pairing.authenticate(str(user_input[CONF_CODE]).strip())
        except AiphoneJoError as err:
            return await self._async_on_pairing_error(err, "code", schema)
        return await self.async_step_terminal_name()

    # --------------------------------------------------------- registration

    def _name_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(
                    CONF_TERMINAL_NAME, default=self._terminal_name
                ): TextSelector()
            }
        )

    async def async_step_terminal_name(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name the Home Assistant terminal and register it on the monitor."""
        if user_input is None:
            return self._form("terminal_name", self._name_schema())
        name = str(user_input[CONF_TERMINAL_NAME]).strip()
        self._terminal_name = name or self._terminal_name
        if not name or len(name) > MAX_TERMINAL_NAME:
            return self._form(
                "terminal_name", self._name_schema(), FlowError("name_too_long")
            )
        pairing = self._pairing_flow()
        try:
            if pairing.step is PairingStep.AUTHENTICATED:
                await pairing.register_cloud()
                if (abort := await self._async_check_monitor(pairing)) is not None:
                    return abort
            self._credentials = await pairing.register_terminal(name, auto_rename=False)
        except AiphoneJoError as err:
            return await self._async_on_pairing_error(
                err, "terminal_name", self._name_schema()
            )
        return await self._async_finish()

    async def _async_check_monitor(
        self, pairing: PairingFlow
    ) -> ConfigFlowResult | None:
        """Refuse a duplicate or, when re-pairing, a different monitor."""
        monitor_id = pairing.monitor_id
        if monitor_id is None:
            return None
        entry = self._get_entry()
        if entry is not None:
            # Entries keyed by fingerprint (no monitor id) cannot be compared.
            if is_monitor_id(entry.unique_id) and entry.unique_id != monitor_id:
                await pairing.cancel()
                return self.async_abort(reason="wrong_monitor")
            return None
        if self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, monitor_id
        ):
            await pairing.cancel()
            return self.async_abort(reason="already_configured")
        return None

    # -------------------------------------------------------- window closed

    def _resume_form(self, error: FlowError | None = None) -> ConfigFlowResult:
        return self._form("pairing_window_closed", vol.Schema({}), error)

    async def async_step_pairing_window_closed(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The monitor closed the pairing link: reconnect and resume."""
        if user_input is None:
            return self._resume_form()
        pairing = self._pairing_flow()
        try:
            await pairing.reconnect()
        except UntrustedPeerError as err:
            self._peer = err.peer
            self._expected_fingerprint = pairing.approved_fingerprint
            return await self.async_step_fingerprint_changed()
        except AiphoneJoError as err:
            return self._resume_form(flow_error(err))
        return await self.async_step_code()

    # ---------------------------------------------------------------- finish

    async def _async_finish(self) -> ConfigFlowResult:
        credentials = self._credentials
        pairing = self._pairing
        assert credentials is not None and self._monitor is not None
        assert pairing is not None and pairing.approved_fingerprint is not None
        fingerprint = normalize_fingerprint(pairing.approved_fingerprint)
        self._pairing = None
        await pairing.close()
        stations: list[Station] = []
        try:
            room_info = await get_room_info(
                async_get_clientsession(self.hass), credentials
            )
            stations = stations_from_room_info(room_info)
        except AiphoneJoError as err:
            _LOGGER.debug("Entrance stations not available yet: %s", type(err).__name__)
        data = {
            CONF_CREDENTIALS: credentials_to_storage(credentials),
            CONF_MONITOR: {
                CONF_FINGERPRINT: fingerprint,
                CONF_ADDRESS: self._monitor.address,
                CONF_PORT: self._monitor.port,
                CONF_SYSTEM_VERSION: credentials.system_version
                or self._monitor.version,
            },
            CONF_STATIONS: [station.as_dict() for station in stations],
        }
        unique_id = credentials.monitor_id or fingerprint
        entry = self._get_entry()
        if entry is None:
            await self.async_set_unique_id(unique_id)
            return self.async_create_entry(title=DEFAULT_TITLE, data=data)
        removed = await self._async_unregister_previous(entry)
        if not removed and self.source == SOURCE_RECONFIGURE:
            await async_notify_manual_removal(self.hass, entry)
        return self.async_update_reload_and_abort(entry, unique_id=unique_id, data=data)

    async def _async_unregister_previous(self, entry: ConfigEntry) -> bool:
        try:
            previous = credentials_from_storage(
                entry.data[CONF_CREDENTIALS], check_validity=False
            )
        except (InvalidCredentialsError, KeyError):
            return False
        return await async_unregister(async_get_clientsession(self.hass), previous)

    # ---------------------------------------------------- reauth / reconfigure

    def _prepare_repair(self, entry: ConfigEntry) -> None:
        self._expected_fingerprint = entry.data[CONF_MONITOR][CONF_FINGERPRINT]
        self._terminal_name = (
            entry.data[CONF_CREDENTIALS].get("term_name") or DEFAULT_TERMINAL_NAME
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """The stored identity was refused: pair again."""
        self._prepare_repair(self._get_reauth_entry())
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explain the re-pairing."""
        if user_input is None:
            return self._form("reauth_confirm", vol.Schema({}))
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pair again, optionally with another terminal name."""
        entry = self._get_reconfigure_entry()
        if user_input is None:
            self._prepare_repair(entry)
            return self._form("reconfigure", self._name_schema())
        name = str(user_input[CONF_TERMINAL_NAME]).strip()
        if not name or len(name) > MAX_TERMINAL_NAME:
            self._terminal_name = name or self._terminal_name
            return self._form(
                "reconfigure", self._name_schema(), FlowError("name_too_long")
            )
        self._terminal_name = name
        return await self.async_step_user()


class AiphoneJoOptionsFlow(OptionsFlow):
    """Entrance station, camera image refresh interval and go2rtc server."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Options form."""
        stations = [
            Station.from_dict(item) for item in self.config_entry.data[CONF_STATIONS]
        ]
        errors: dict[str, str] = {}
        current: Mapping[str, Any] = self.config_entry.options
        if user_input is not None:
            options: dict[str, Any] = {
                CONF_IMAGE_REFRESH: int(user_input[CONF_IMAGE_REFRESH])
            }
            if CONF_STATION in user_input:
                options[CONF_STATION] = int(user_input[CONF_STATION])
            if url := str(user_input.get(CONF_GO2RTC_URL) or "").strip():
                options[CONF_GO2RTC_URL] = url
                for key in (CONF_GO2RTC_USERNAME, CONF_GO2RTC_PASSWORD):
                    if value := user_input.get(key):
                        options[key] = value
                if not url.startswith(("http://", "https://")):
                    errors[CONF_GO2RTC_URL] = "invalid_url"
                else:
                    server = async_get_server(self.hass, options)
                    assert server is not None
                    try:
                        await async_check_server(server)
                    except Go2RtcError:
                        errors["base"] = "go2rtc_cannot_connect"
            if not errors:
                return self.async_create_entry(data=options)
            current = {**current, **options}
        schema: dict[Any, Any] = {}
        if stations:
            choices = [
                SelectOptionDict(value=str(s.id), label=s.name) for s in stations
            ]
            default = str(current.get(CONF_STATION, stations[0].id))
            schema[vol.Required(CONF_STATION, default=default)] = SelectSelector(
                SelectSelectorConfig(options=choices)
            )
        schema[
            vol.Required(
                CONF_IMAGE_REFRESH,
                default=current.get(CONF_IMAGE_REFRESH, DEFAULT_IMAGE_REFRESH),
            )
        ] = NumberSelector(
            NumberSelectorConfig(
                min=MIN_IMAGE_REFRESH,
                max=MAX_IMAGE_REFRESH,
                step=1,
                mode=NumberSelectorMode.BOX,
                unit_of_measurement="s",
            )
        )
        for key, selector in (
            (
                CONF_GO2RTC_URL,
                TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            ),
            (CONF_GO2RTC_USERNAME, TextSelector()),
            (
                CONF_GO2RTC_PASSWORD,
                TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            ),
        ):
            if current.get(key):
                schema[vol.Optional(key, default=current[key])] = selector
            else:
                schema[vol.Optional(key)] = selector
        return self.async_show_form(
            step_id="init", data_schema=vol.Schema(schema), errors=errors
        )
