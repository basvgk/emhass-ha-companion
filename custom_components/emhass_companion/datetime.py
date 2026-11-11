"""Draft date and time settings for a dated on-demand request."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from .entity import EmhassLoadEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    for load in entry.runtime_data.loads.all():
        if not load.is_thermal:
            async_add_entities(
                [
                    SessionDraftDateTime(entry.runtime_data.coordinator, load, start)
                    for start in (True, False)
                ],
                config_subentry_id=load.subentry_id,
            )


class SessionDraftDateTime(EmhassLoadEntity, DateTimeEntity, RestoreEntity):
    """A draft; the apply button validates both dates before changing a request."""

    def __init__(self, coordinator, load, start: bool) -> None:
        self._field = "session_draft_start" if start else "session_draft_end"
        super().__init__(coordinator, load, self._field)
        self._attr_translation_key = self._field

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (
            (last := await self.async_get_last_state()) is not None
            and (parsed := dt_util.parse_datetime(last.state)) is not None
            and parsed.utcoffset() is not None
        ):
            setattr(self.load, self._field, parsed)
        if getattr(self.load, self._field) is None:
            setattr(self.load, self._field, self.native_value)

    @property
    def available(self) -> bool:
        return super().available and self.load.on_demand

    @property
    def native_value(self):
        draft = getattr(self.load, self._field)
        if draft is not None:
            return draft
        active = (
            self.load.session_start_at
            if self._field.endswith("start")
            else self.load.session_end_at
        )
        if active is not None and self.load.requested:
            return active
        start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        return start if self._field.endswith("start") else start + timedelta(hours=12)

    async def async_set_value(self, value) -> None:
        setattr(self.load, self._field, value)
        self.load.notify()
