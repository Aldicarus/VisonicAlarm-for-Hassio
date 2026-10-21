"""The Visonic Alarm integration."""

from __future__ import annotations

import logging

import homeassistant.helpers.config_validation as cv

try:
    # Home Assistant 2026.10 replaced voluptuous with probatio, which keeps
    # the same names. Older releases, which this still supports, lack it.
    import probatio as vol
except ImportError:  # pragma: no cover - Home Assistant before 2026.10
    import voluptuous as vol  # type: ignore[no-redef]
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.const import CONF_HOST, CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_APP_ID,
    CONF_EVENT_HOUR_OFFSET,
    CONF_NO_PIN_REQUIRED,
    CONF_PANEL_ID,
    CONF_PARTITION,
    CONF_SCAN_INTERVAL,
    CONF_USER_CODE,
    CONF_USER_EMAIL,
    CONF_USER_PASSWORD,
    DEFAULT_HOST,
    DEFAULT_NAME,
    DEFAULT_PARTITION,
    DOMAIN,
    ISSUE_YAML_DEPRECATED,
    LEGACY_SCAN_INTERVAL,
    PLATFORMS,
)
from .coordinator import VisonicConfigEntry, VisonicDataUpdateCoordinator
from .entity import panel_device_info
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

# The legacy YAML schema, kept only so existing configurations can be imported.
CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_HOST, default=DEFAULT_HOST): cv.string,
                vol.Required(CONF_APP_ID): cv.string,
                vol.Required(CONF_USER_CODE): cv.string,
                vol.Required(CONF_USER_EMAIL): cv.string,
                vol.Required(CONF_USER_PASSWORD): cv.string,
                vol.Required(CONF_PANEL_ID): cv.string,
                vol.Optional(CONF_PARTITION, default=DEFAULT_PARTITION): cv.string,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Optional(CONF_NO_PIN_REQUIRED, default=False): cv.boolean,
                vol.Optional(CONF_EVENT_HOUR_OFFSET, default=0): vol.All(
                    vol.Coerce(int), vol.Range(min=-24, max=24)
                ),
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions and import any legacy YAML configuration.

    Quality scale (action-setup): actions are registered here so they exist even
    with no entry loaded, and the handlers raise if the entry they need is gone.
    """
    async_setup_services(hass)

    if DOMAIN not in config:
        # The notice tells the user to delete the YAML block, so it has to clear
        # itself once they have. Otherwise it nags forever after they comply.
        ir.async_delete_issue(hass, DOMAIN, ISSUE_YAML_DEPRECATED)
        return True

    hass.async_create_task(
        hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": SOURCE_IMPORT},
            data=dict(config[DOMAIN]),
        )
    )

    ir.async_create_issue(
        hass,
        DOMAIN,
        ISSUE_YAML_DEPRECATED,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=ISSUE_YAML_DEPRECATED,
    )

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: VisonicConfigEntry) -> bool:
    """Migrate an entry from an older config entry version.

    1.1 -> 1.2: the poll interval became an option, and new entries default to
    30 s. Entries made before that polled every 10 s, and automations may rely
    on that speed, so they are given an explicit 10 s rather than inheriting
    the new default. Nobody's polling changes on upgrade.
    """
    if entry.version > 1:
        # A downgrade from a future major version. Refuse rather than guess.
        return False

    if entry.minor_version < 2:
        options = {**entry.options}
        options.setdefault(CONF_SCAN_INTERVAL, LEGACY_SCAN_INTERVAL)
        hass.config_entries.async_update_entry(entry, options=options, minor_version=2)
        _LOGGER.debug(
            "Migrated %s to 1.2, poll interval kept at %s s",
            entry.title,
            options[CONF_SCAN_INTERVAL],
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: VisonicConfigEntry) -> bool:
    """Set up Visonic Alarm from a config entry."""
    coordinator = VisonicDataUpdateCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    # Per-entry state lives on the entry itself (quality scale: runtime-data).
    entry.runtime_data = coordinator

    # The panel is registered before any platform, so each zone can name it by
    # device id rather than by the via_device tuple Home Assistant deprecated.
    panel = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **panel_device_info(coordinator)
    )
    coordinator.panel_device_id = panel.id

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_move_entity_areas_to_devices(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


@callback
def _async_move_entity_areas_to_devices(hass: HomeAssistant, entry: VisonicConfigEntry) -> None:
    """Give an area set on an unnamed entity to its device instead.

    Before 4.0 the zones had no devices, so the only way to put a zone in a
    room was to set the area on the entity. Those entities now stand for
    their zone's device and have no name of their own, and Home Assistant
    2026.10 raises a repair for each unnamed entity carrying its own area.
    Moving the area to the device keeps the zone in its room and clears it.
    """
    entities = er.async_get(hass)
    devices = dr.async_get(hass)
    for entity in er.async_entries_for_config_entry(entities, entry.entry_id):
        if (
            entity.area_id is None
            or entity.device_id is None
            or entity.name
            or entity.original_name
        ):
            continue
        if (device := devices.async_get(entity.device_id)) is None:
            continue
        if device.area_id is None:
            devices.async_update_device(device.id, area_id=entity.area_id)
        elif device.area_id != entity.area_id:
            # Two different rooms. Which one is right is the user's call.
            continue
        entities.async_update_entity(entity.entity_id, area_id=None)


async def _async_update_listener(hass: HomeAssistant, entry: VisonicConfigEntry) -> None:
    """Reload the entry when its options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VisonicConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
