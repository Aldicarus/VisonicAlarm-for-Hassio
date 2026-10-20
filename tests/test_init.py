"""Setup, teardown, services, diagnostics and repair-issue tests."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.visonicalarm.api import (
    VisonicAuthError,
    VisonicConnectionError,
    VisonicError,
)
from custom_components.visonicalarm.const import (
    CONF_EVENT_HOUR_OFFSET,
    CONF_NO_PIN_REQUIRED,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ISSUE_PANEL_OFFLINE,
    ISSUE_YAML_DEPRECATED,
    LEGACY_SCAN_INTERVAL,
)
from custom_components.visonicalarm.services import (
    ATTR_CONFIG_ENTRY_ID,
    SERVICE_REFRESH,
    SERVICE_SET_ZONE_NAME,
    SERVICE_SILENCE_SIREN,
    SERVICE_SOUND_SIREN,
)

from .conftest import ENTRY_DATA, FakeApi


async def test_setup_and_unload(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The entry loads and unloads cleanly."""
    assert setup_integration.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED


async def test_setup_retries_on_connection_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_api: FakeApi
) -> None:
    """A transient outage leaves the entry in retry, not failed."""
    with patch.object(fake_api, "async_connect", side_effect=VisonicConnectionError("down")):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()
    assert mock_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_triggers_reauth_on_auth_error(
    hass: HomeAssistant, mock_entry: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Rejected credentials start a reauth flow."""
    with patch.object(fake_api, "async_connect", side_effect=VisonicAuthError("nope")):
        await hass.config_entries.async_setup(mock_entry.entry_id)
        await hass.async_block_till_done()

    assert mock_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_yaml_import_raises_deprecation_issue(hass: HomeAssistant, fake_api: FakeApi) -> None:
    """Legacy YAML is imported and flagged for removal."""
    with patch("custom_components.visonicalarm.async_setup_entry", return_value=True):
        assert await async_setup_component(hass, DOMAIN, {DOMAIN: dict(ENTRY_DATA)})
        await hass.async_block_till_done()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, ISSUE_YAML_DEPRECATED) is not None


async def test_panel_offline_raises_and_clears_issue(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """The offline repair issue appears and then clears again.

    This failure is otherwise invisible: the cloud keeps serving the last known
    arm state, so the alarm entity looks healthy while being stale.
    """
    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, ISSUE_PANEL_OFFLINE) is None

    fake_api.status["connected"] = False
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, ISSUE_PANEL_OFFLINE) is not None

    fake_api.status["connected"] = True
    await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert issues.async_get_issue(DOMAIN, ISSUE_PANEL_OFFLINE) is None


async def test_refresh_service(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """The refresh action polls immediately."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_REFRESH,
        {ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id},
        blocking=True,
    )


async def test_siren_services(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Siren actions reach the API when the panel advertises the feature."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SOUND_SIREN,
        {ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id},
        blocking=True,
    )
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SILENCE_SIREN,
        {ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id, "mode": "all"},
        blocking=True,
    )
    assert fake_api.siren_calls == ["on", "off:all"]


async def test_siren_refused_when_unsupported(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """A panel without the feature gets a clear error, not a failed request."""
    info = await fake_api.async_get_panel_info()
    info["features"]["enabling_siren"] = False
    with (
        patch.object(fake_api, "async_get_panel_info", return_value=info),
        pytest.raises(ServiceValidationError),
    ):
        await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SOUND_SIREN,
            {ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id},
            blocking=True,
        )


async def test_set_zone_name_service(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Renaming a zone reaches the panel."""
    await hass.services.async_call(
        DOMAIN,
        SERVICE_SET_ZONE_NAME,
        {
            ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id,
            "zone": 5,
            "name": "Scullery",
        },
        blocking=True,
    )
    assert fake_api.name_calls == [("zone", 5, "Scullery")]


async def test_service_on_unknown_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Actions exist without an entry, so a bad target must be a clean error."""
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {ATTR_CONFIG_ENTRY_ID: "does-not-exist"},
            blocking=True,
        )


async def test_service_on_unloaded_entry(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """Calling an action against an unloaded entry is a clean error."""
    await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_REFRESH,
            {ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id},
            blocking=True,
        )


async def test_service_reports_panel_failure(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Panel-side failures surface as HomeAssistantError."""
    with (
        patch.object(fake_api, "async_set_name", side_effect=VisonicError("no")),
        pytest.raises(HomeAssistantError),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_SET_ZONE_NAME,
            {
                ATTR_CONFIG_ENTRY_ID: setup_integration.entry_id,
                "zone": 5,
                "name": "X",
            },
            blocking=True,
        )


async def test_diagnostics_redacts_secrets(
    hass: HomeAssistant, hass_client, setup_integration: MockConfigEntry
) -> None:
    """Diagnostics must not leak credentials or identifying details."""
    diag = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    blob = str(diag)
    assert "hunter2" not in blob
    assert "1234" not in diag["entry"]["data"].values()
    assert diag["entry"]["data"]["user_password"] == "**REDACTED**"
    assert diag["counts"]["devices"] == 13
    assert diag["faulty_devices"] == {"Kitchen": ["INACTIVE", "1_WAY"]}


def _entry(hass: HomeAssistant, *, version: int, minor_version: int, options=None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=ENTRY_DATA,
        options=options or {},
        unique_id="ABC123",
        title="PowerMaster 360R",
        version=version,
        minor_version=minor_version,
    )
    entry.add_to_hass(hass)
    return entry


async def test_migration_keeps_existing_entries_at_10s(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    """Entries from before the option keep polling every 10 s, not the new 30 s."""
    entry = _entry(hass, version=1, minor_version=1)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.version == 1
    assert entry.minor_version == 2
    assert entry.options[CONF_SCAN_INTERVAL] == LEGACY_SCAN_INTERVAL
    assert entry.runtime_data.update_interval == timedelta(seconds=LEGACY_SCAN_INTERVAL)


async def test_migration_preserves_other_options(hass: HomeAssistant, fake_api: FakeApi) -> None:
    """Migration only adds the interval; the user's other options survive."""
    entry = _entry(
        hass,
        version=1,
        minor_version=1,
        options={CONF_NO_PIN_REQUIRED: True, CONF_EVENT_HOUR_OFFSET: 2},
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert dict(entry.options) == {
        CONF_NO_PIN_REQUIRED: True,
        CONF_EVENT_HOUR_OFFSET: 2,
        CONF_SCAN_INTERVAL: LEGACY_SCAN_INTERVAL,
    }


async def test_current_entry_without_interval_uses_default(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    """An entry already at 1.2 is not migrated, so it polls at the default."""
    entry = _entry(hass, version=1, minor_version=2)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert CONF_SCAN_INTERVAL not in entry.options
    assert entry.runtime_data.update_interval == timedelta(seconds=DEFAULT_SCAN_INTERVAL)


async def test_migration_refuses_a_future_major_version(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    """A downgrade from a newer major version fails cleanly instead of guessing."""
    entry = _entry(hass, version=2, minor_version=1)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert CONF_SCAN_INTERVAL not in entry.options


async def test_migrate_entry_rejects_newer_major_directly(
    hass: HomeAssistant, fake_api: FakeApi
) -> None:
    """The handler itself also refuses a newer major version."""
    from custom_components.visonicalarm import async_migrate_entry

    entry = _entry(hass, version=2, minor_version=1)
    assert await async_migrate_entry(hass, entry) is False
