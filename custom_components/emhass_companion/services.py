"""Integration services."""

from __future__ import annotations

from datetime import UTC
import math
from pathlib import Path
from typing import Any

from homeassistant.core import (
    Context,
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError, Unauthorized
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util
import voluptuous as vol

from .const import ACTION_DAYAHEAD, ACTION_MPC, DOMAIN, PROFILE_KIND_NETWORK
from .hot_water_plan import async_set_plan
from .network_calendar import HolidayCache, NetworkCalendar, NetworkCalendarError
from .profiles import ProfileError, async_resolve_series, resolve_network, resolve_settings
from .profiles.schema import humanize_error
from .typical_load import typical_day_records

SERVICE_RUN_DAYAHEAD = "run_dayahead"
SERVICE_RUN_MPC = "run_mpc"
SERVICE_TRAIN_LOAD_FORECASTER = "train_load_forecaster"
SERVICE_EXPORT_SETTINGS = "export_settings"
SERVICE_TEST_PROFILE = "test_profile"
SERVICE_TYPICAL_LOAD_FORECAST = "typical_load_forecast"
SERVICE_SET_HOT_WATER_PLAN = "set_hot_water_plan"
SERVICE_UPSERT_ON_DEMAND_SESSIONS = "upsert_on_demand_sessions"

TEST_PROFILE_SCHEMA = vol.Schema(
    {
        vol.Required("profile"): str,
        vol.Optional("options", default=dict): dict,
        vol.Optional("limit", default=10): vol.All(int, vol.Range(min=1, max=500)),
    }
)

TYPICAL_LOAD_FORECAST_SCHEMA = vol.Schema(
    {
        vol.Required("day"): cv.date,
        vol.Required("average_w"): vol.All(vol.Coerce(float), vol.Range(min=0)),
    }
)

ON_DEMAND_SESSIONS_SCHEMA = vol.Schema(
    {
        vol.Required("sessions"): vol.All(
            [
                {
                    vol.Required("device_id"): str,
                    vol.Required("session_id"): str,
                    vol.Required("start_at"): str,
                    vol.Required("end_at"): str,
                    vol.Required("total_hours"): vol.Coerce(float),
                }
            ],
            vol.Length(min=1),
        )
    }
)


async def async_require_admin(hass: HomeAssistant, context: Context | None) -> None:
    """Reject a non-admin caller of a diagnostic or configuration-writing action.

    ``context.user_id`` is None for anything Home Assistant itself triggered --
    an automation, a script, another integration -- and those stay allowed;
    only a real, non-admin user is turned away.
    """
    if context is None or context.user_id is None:
        return
    user = await hass.auth.async_get_user(context.user_id)
    if user is not None and not user.is_admin:
        raise Unauthorized(context=context)


async def _require_admin(hass: HomeAssistant, call: ServiceCall) -> None:
    await async_require_admin(hass, call.context)


def _coordinator(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError("EMHASS Companion is not set up")
    return entries[0].runtime_data.coordinator


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register services once, shared across entries."""
    if hass.services.has_service(DOMAIN, SERVICE_RUN_DAYAHEAD):
        return

    async def _run_dayahead(call: ServiceCall) -> None:
        await _coordinator(hass).async_run(ACTION_DAYAHEAD)

    async def _run_mpc(call: ServiceCall) -> None:
        await _coordinator(hass).async_run(ACTION_MPC)

    async def _train_load_forecaster(call: ServiceCall) -> None:
        coordinator = _coordinator(hass)
        if not coordinator.uses_mlforecaster:
            raise ServiceValidationError(
                "The load forecast method is not the machine-learning forecaster"
            )
        await coordinator.async_run_forecast_fit()

    async def _export_settings(call: ServiceCall) -> None:
        # Overwrites EMHASS's saved configuration, so not open to non-admin users.
        await _require_admin(hass, call)
        await _coordinator(hass).async_export_settings()

    async def _set_hot_water_plan(call: ServiceCall) -> None:
        # Resolve a device through its integration identifier, across all
        # loaded entries, rather than assuming the first entry owns the tank.
        from homeassistant.helpers import device_registry as dr

        device = dr.async_get(hass).async_get(call.data["device_id"])
        if device is not None:
            for entry in hass.config_entries.async_loaded_entries(DOMAIN):
                for load in entry.runtime_data.loads.all():
                    if (DOMAIN, f"{entry.entry_id}_{load.subentry_id}") in device.identifiers:
                        try:
                            await async_set_plan(
                                entry.runtime_data.coordinator, load, call.data["periods"]
                            )
                        except ValueError as err:
                            raise ServiceValidationError(str(err)) from err
                        return
        raise ServiceValidationError("Select a hot-water tank device")

    async def _upsert_on_demand_sessions(call: ServiceCall) -> None:
        """Atomically upsert dated requests for configured on-demand loads."""
        from homeassistant.helpers import device_registry as dr

        device_registry = dr.async_get(hass)
        coordinator = _coordinator(hass)
        validated: list[tuple[Any, str, Any, Any, float]] = []
        seen_devices: set[str] = set()
        for item in call.data["sessions"]:
            device_id = item["device_id"]
            session_id = item["session_id"].strip()
            start_at = dt_util.parse_datetime(item["start_at"])
            end_at = dt_util.parse_datetime(item["end_at"])
            hours = item["total_hours"]
            if device_id in seen_devices:
                raise ServiceValidationError(
                    "A batch may contain only one session per configured load device"
                )
            seen_devices.add(device_id)
            if not session_id:
                raise ServiceValidationError("session_id must not be empty")
            if (
                start_at is None
                or end_at is None
                or start_at.utcoffset() is None
                or end_at.utcoffset() is None
            ):
                raise ServiceValidationError("start_at and end_at must include a UTC offset")
            start_at = start_at.astimezone(UTC)
            end_at = end_at.astimezone(UTC)
            if end_at <= start_at:
                raise ServiceValidationError("end_at must be later than start_at")
            if not math.isfinite(hours) or not 0 < hours <= 24:
                raise ServiceValidationError(
                    "total_hours must be greater than 0 and no more than 24"
                )

            device = device_registry.async_get(device_id)
            selected = None
            for entry in hass.config_entries.async_loaded_entries(DOMAIN):
                for load in entry.runtime_data.loads.all():
                    if (
                        device is not None
                        and (
                            DOMAIN,
                            f"{entry.entry_id}_{load.subentry_id}",
                        )
                        in device.identifiers
                    ):
                        selected = load
                        break
                if selected is not None:
                    break
            if selected is None:
                raise ServiceValidationError("Select a configured EMHASS Companion load device")
            if not selected.on_demand:
                raise ServiceValidationError(f"{selected.name} must be configured as On demand")
            if selected.session_id != session_id and selected.requested:
                raise ServiceValidationError(
                    f"{selected.name} has an active session; cancel it before changing session_id"
                )
            validated.append((selected, session_id, start_at, end_at, hours))

        # No state is mutated until every entry has been fully validated.
        proposed = {row[0].subentry_id: row for row in validated}
        all_loads = coordinator.loads.all()
        for load, _session_id, start_at, end_at, _hours in validated:
            if not load.control_entity:
                continue
            for other in all_loads:
                if other is load or other.control_entity != load.control_entity:
                    continue
                if other.subentry_id in proposed:
                    _other, _sid, other_start, other_end, _target = proposed[other.subentry_id]
                    overlaps = start_at < other_end and other_start < end_at
                    if overlaps:
                        raise ServiceValidationError(
                            f"{load.name} and {other.name} share control entity "
                            f"{load.control_entity} and their session windows overlap"
                        )
                    continue
                if not other.enabled:
                    continue
                if other.mode != "auto" or other.recurrence == "daily":
                    raise ServiceValidationError(
                        f"{load.name} and {other.name} share control entity "
                        f"{load.control_entity}; only one load may own that actuator"
                    )
                if not other.requested:
                    continue
                if other.session_id is None or other.session_start_at is None:
                    raise ServiceValidationError(
                        f"{load.name} and {other.name} share control entity "
                        f"{load.control_entity}; cancel the undated request first"
                    )
                if other.session_end_at is not None and (
                    start_at < other.session_end_at and other.session_start_at < end_at
                ):
                    raise ServiceValidationError(
                        f"{load.name} and {other.name} share control entity "
                        f"{load.control_entity} and their session windows overlap"
                    )

        now = dt_util.utcnow()
        changed = False
        for load, session_id, start_at, end_at, hours in validated:
            if load.upsert_dated_session(
                session_id=session_id,
                start_at=start_at,
                end_at=end_at,
                total_hours=hours,
                now=now,
            ):
                changed = True
                load.notify()
        if changed:
            await coordinator.async_request_settings_refresh()

    async def _test_profile(call: ServiceCall) -> ServiceResponse:
        """Resolve a profile and return what it produced.

        A YAML profile that silently yields nothing is far harder to debug than
        a Python one that raises. This turns "my prices are wrong" into a single
        call that shows exactly which records came back.

        Admin-only, unlike the run actions: the response renders the profile's
        ``emhass:`` block, which for a user profile is where a forecast API key
        lives. Home Assistant does not restrict action calls by user, so the
        check has to be here. A call with no user (an automation, a script,
        anything internal) keeps working.
        """
        await _require_admin(hass, call)

        coordinator = _coordinator(hass)
        key = call.data["profile"]

        if (profile := coordinator.profiles.get(key)) is None:
            available = ", ".join(sorted(coordinator.profiles)) or "(none loaded)"
            raise ServiceValidationError(f"Unknown profile '{key}'. Available: {available}")

        options: dict[str, Any] = dict(call.data["options"])
        if options:
            # Caller-supplied options are rendered into the profile's own
            # templates, so they are held to the schema the config flow would
            # have shown rather than accepted as an arbitrary mapping -- with
            # the Solcast profile, an unchecked ``entities`` list is a way to
            # read the attributes of any entity by name.
            try:
                options = vol.Schema(profile.selector_schema())(options)
            except vol.Invalid as err:
                raise ServiceValidationError(
                    f"Invalid options for profile '{key}': {humanize_error(err)}"
                ) from err
        else:
            # Fall back to whatever this profile is configured with, so the
            # common case needs no arguments at all. Not re-validated: these
            # came from the config flow, and a profile whose options have since
            # changed shape should still be diagnosable.
            for selection in (
                coordinator.config.price,
                coordinator.config.pv,
                coordinator.config.load,
                coordinator.config.temperature,
                coordinator.config.inverter,
            ):
                if selection.key == key:
                    options = dict(selection.options)
                    break

        result: dict[str, Any] = {
            "profile": key,
            "name": profile.name,
            "kind": profile.kind,
            # Filename only: which file this is remains obvious, without
            # handing out the layout of the config directory.
            "file": Path(profile.path).name,
            "is_builtin": profile.is_builtin,
            "options_used": options,
        }

        if profile.kind == PROFILE_KIND_NETWORK:
            # A network profile fetches nothing and delegates nothing to
            # EMHASS -- it has no ``emhass:`` settings worth reporting and no
            # series to resolve, so the generic branches below (written for
            # every other kind) would report "contributes nothing" for a
            # profile that in fact defines bands and a demand charge. The
            # useful diagnostic here is what actually matches *now*.
            try:
                resolved = resolve_network(hass, profile, options)
                calendar = NetworkCalendar.from_resolved(resolved)
            except (ProfileError, NetworkCalendarError) as err:
                result["error"] = str(err)
                return result
            now = dt_util.utcnow()
            holidays = HolidayCache()
            if entity_id := next(iter(calendar.holiday_entities()), None):
                await holidays.async_ensure(hass, entity_id, {dt_util.as_local(now).date()})
            band = calendar.current_band(now, holidays)
            result.update(
                {
                    "bands": [b.name for b in calendar.bands],
                    "current_band": band.name if band else None,
                    "current_adjustment": (
                        {"multiplier": band.buy.multiplier, "adder": band.buy.adder}
                        if band
                        else None
                    ),
                    "has_demand_charge": calendar.demand_charge is not None,
                }
            )
            return result

        try:
            settings = resolve_settings(hass, profile, options)
        except Exception as err:  # noqa: BLE001 - a template can raise anything;
            # this action exists to diagnose broken profiles, so the failure is
            # returned in the response rather than raised out of the service.
            result["settings_error"] = str(err)
        else:
            result["emhass_settings"] = settings

        if not profile.produces_series:
            result["series"] = None
            result["note"] = (
                "This profile contributes EMHASS settings and does not fetch a series of its own."
            )
            return result

        try:
            series = await async_resolve_series(hass, profile, options)
        except ProfileError as err:
            result["error"] = str(err)
            return result

        limit = call.data["limit"]
        result.update(
            {
                "count": len(series),
                "unit": profile.unit,
                "start": dt_util.as_local(series.start).isoformat() if series else None,
                "end": dt_util.as_local(series.end).isoformat() if series else None,
                "step_minutes": (step.total_seconds() / 60 if (step := series.step()) else None),
                "series": [
                    {
                        "time": dt_util.as_local(point.time).isoformat(),
                        "value": point.value,
                    }
                    for point in list(series)[:limit]
                ],
                "truncated": len(series) > limit,
            }
        )
        return result

    async def _typical_load_forecast(call: ServiceCall) -> ServiceResponse:
        """Back the "Typical household" load profile's ``source: service`` block.

        Kept as a service rather than a fourth engine source type: the engine
        deliberately offers only attributes/service/template, and a service is
        the existing, sanctioned way for a profile to fetch code-computed
        records -- this one is just backed by our own domain instead of a
        third-party integration's.
        """
        records = await hass.async_add_executor_job(
            typical_day_records, call.data["day"], call.data["average_w"]
        )
        return {"forecast": records}

    hass.services.async_register(DOMAIN, SERVICE_RUN_DAYAHEAD, _run_dayahead)
    hass.services.async_register(DOMAIN, SERVICE_RUN_MPC, _run_mpc)
    hass.services.async_register(DOMAIN, SERVICE_TRAIN_LOAD_FORECASTER, _train_load_forecaster)
    hass.services.async_register(DOMAIN, SERVICE_EXPORT_SETTINGS, _export_settings)
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_HOT_WATER_PLAN,
        _set_hot_water_plan,
        schema=vol.Schema({vol.Required("device_id"): str, vol.Required("periods"): list}),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_UPSERT_ON_DEMAND_SESSIONS,
        _upsert_on_demand_sessions,
        schema=ON_DEMAND_SESSIONS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_PROFILE,
        _test_profile,
        schema=TEST_PROFILE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_TYPICAL_LOAD_FORECAST,
        _typical_load_forecast,
        schema=TYPICAL_LOAD_FORECAST_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )


@callback
def async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_RUN_DAYAHEAD,
        SERVICE_RUN_MPC,
        SERVICE_TRAIN_LOAD_FORECASTER,
        SERVICE_EXPORT_SETTINGS,
        SERVICE_TEST_PROFILE,
        SERVICE_TYPICAL_LOAD_FORECAST,
        SERVICE_SET_HOT_WATER_PLAN,
        SERVICE_UPSERT_ON_DEMAND_SESSIONS,
    ):
        hass.services.async_remove(DOMAIN, service)
