"""Dated on-demand batch actions update live load state atomically."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.emhass_companion.api import EmhassClient
from custom_components.emhass_companion.const import DOMAIN
from custom_components.emhass_companion.models import LastRun


async def _setup_loads(hass, count: int, control_entity: str | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"url": "http://localhost:5000"},
        subentries_data=[
            {
                "subentry_type": "deferrable_load",
                "title": f"Load {index}",
                "unique_id": f"load-{index}",
                "data": {
                    "name": f"Load {index}",
                    "nominal_power_w": 2000,
                    "operating_hours": 1,
                    "recurrence": "on_demand",
                    **({"control_entity": control_entity} if control_entity else {}),
                },
            }
            for index in range(count)
        ],
    )
    entry.add_to_hass(hass)
    client = AsyncMock(spec=EmhassClient)
    client.base_url = "http://localhost:5000"
    client.async_get_version.return_value = "0.18.8"
    client.async_optimize.return_value = (LastRun(status="no-run"), None)
    with patch("custom_components.emhass_companion.EmhassClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = entry.runtime_data.coordinator
    coordinator.async_request_settings_refresh = AsyncMock()
    devices = {
        next(
            load for load in entry.runtime_data.loads.all() if load.name == f"Load {i}"
        ).subentry_id: next(
            device
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            if (
                DOMAIN,
                f"{entry.entry_id}_{next(load for load in entry.runtime_data.loads.all() if load.name == f'Load {i}').subentry_id}",
            )
            in device.identifiers
        ).id
        for i in range(count)
    }
    return entry, coordinator, devices


def _session(device_id: str, index: int, *, start="2026-11-11T18:00:00+01:00"):
    start_at = datetime.fromisoformat(start)
    return {
        "device_id": device_id,
        "session_id": f"session-{index}",
        "start_at": start,
        "end_at": (start_at + timedelta(hours=12)).isoformat(),
        "total_hours": 2.0,
    }


async def test_three_sessions_are_upserted_in_one_action(hass):
    entry, coordinator, devices = await _setup_loads(hass, 3)
    rows = [_session(device, index) for index, device in enumerate(devices.values())]

    await hass.services.async_call(
        DOMAIN, "upsert_on_demand_sessions", {"sessions": rows}, blocking=True
    )

    loads = entry.runtime_data.loads.all()
    assert [load.session_id for load in loads] == ["session-0", "session-1", "session-2"]
    assert all(load.requested for load in loads)
    coordinator.async_request_settings_refresh.assert_awaited_once()


async def test_invalid_batch_member_does_not_partially_update(hass):
    entry, _coordinator, devices = await _setup_loads(hass, 2)
    rows = [_session(device, index) for index, device in enumerate(devices.values())]
    rows[1]["start_at"] = "2026-11-12T18:00:00"  # no explicit offset

    with pytest.raises(ServiceValidationError, match="include a UTC offset"):
        await hass.services.async_call(
            DOMAIN, "upsert_on_demand_sessions", {"sessions": rows}, blocking=True
        )

    assert all(
        load.session_id is None and not load.requested for load in entry.runtime_data.loads.all()
    )


async def test_shared_physical_control_rejects_overlapping_sessions(hass):
    entry, _coordinator, devices = await _setup_loads(hass, 2, "switch.shared_charger")
    rows = [_session(device, index) for index, device in enumerate(devices.values())]

    with pytest.raises(ServiceValidationError, match="session windows overlap"):
        await hass.services.async_call(
            DOMAIN, "upsert_on_demand_sessions", {"sessions": rows}, blocking=True
        )

    assert all(
        load.session_id is None and not load.requested for load in entry.runtime_data.loads.all()
    )


async def test_same_session_update_keeps_request_anchor_and_progress(hass):
    entry, _coordinator, devices = await _setup_loads(hass, 1)
    device_id = next(iter(devices.values()))
    row = _session(device_id, 1)
    await hass.services.async_call(
        DOMAIN, "upsert_on_demand_sessions", {"sessions": [row]}, blocking=True
    )
    load = entry.runtime_data.loads.all()[0]
    anchor = load.requested_at
    load.command_runtime = timedelta(minutes=30)
    row["total_hours"] = 2.5
    row["start_at"] = "2026-11-11T19:00:00+01:00"

    await hass.services.async_call(
        DOMAIN, "upsert_on_demand_sessions", {"sessions": [row]}, blocking=True
    )

    assert load.requested_at == anchor
    assert load.command_runtime == timedelta(minutes=30)
    assert load.operating_hours == 2.5
    assert load.session_start_at == datetime(2026, 11, 11, 18, 0, tzinfo=UTC)


async def test_user_sets_and_updates_dated_session_with_entities(hass):
    entry, _coordinator, _devices = await _setup_loads(hass, 1)
    load = entry.runtime_data.loads.all()[0]
    registry = er.async_get(hass)

    def entity(domain, suffix):
        return registry.async_get_entity_id(
            domain, DOMAIN, f"{entry.entry_id}_{load.subentry_id}_{suffix}"
        )

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity("select", "recurrence"), "option": "dated_on_demand"},
        blocking=True,
    )
    for suffix, value in (
        ("session_draft_start", "2027-03-27T18:00:00+01:00"),
        ("session_draft_end", "2027-03-28T07:00:00+02:00"),
    ):
        await hass.services.async_call(
            "datetime",
            "set_value",
            {"entity_id": entity("datetime", suffix), "datetime": value},
            blocking=True,
        )
    assert not load.requested
    await hass.services.async_call(
        "button", "press", {"entity_id": entity("button", "apply_dated_session")}, blocking=True
    )
    assert load.requested
    assert load.session_start_at == datetime(2027, 3, 27, 17, tzinfo=UTC)
    assert load.session_end_at == datetime(2027, 3, 28, 5, tzinfo=UTC)
    active = hass.states.get(entity("sensor", "active_session_start"))
    assert datetime.fromisoformat(active.state) == load.session_start_at
    original_id = load.session_id
    load.command_runtime = timedelta(minutes=30)
    await hass.services.async_call(
        "datetime",
        "set_value",
        {
            "entity_id": entity("datetime", "session_draft_start"),
            "datetime": "2027-03-27T19:00:00+01:00",
        },
        blocking=True,
    )
    assert load.session_start_at == datetime(2027, 3, 27, 17, tzinfo=UTC)
    await hass.services.async_call(
        "button", "press", {"entity_id": entity("button", "apply_dated_session")}, blocking=True
    )
    assert load.session_id == original_id
    assert load.command_runtime == timedelta(minutes=30)
    assert load.session_start_at == datetime(2027, 3, 27, 18, tzinfo=UTC)
