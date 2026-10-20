"""Config flow tests."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_IMPORT, SOURCE_USER
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visonicalarm.api import (
    VisonicAuthError,
    VisonicConnectionError,
    VisonicError,
)
from custom_components.visonicalarm.const import (
    CONF_APP_ID,
    CONF_EVENT_HOUR_OFFSET,
    CONF_NO_PIN_REQUIRED,
    CONF_PANEL_ID,
    CONF_SCAN_INTERVAL,
    CONF_USER_CODE,
    CONF_USER_EMAIL,
    CONF_USER_PASSWORD,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    LEGACY_SCAN_INTERVAL,
)

from .conftest import ENTRY_DATA, FakeApi

USER_INPUT: dict[str, Any] = {
    "host": "visonic.example.com",
    CONF_USER_EMAIL: "user@example.com",
    CONF_USER_PASSWORD: "hunter2",
    CONF_PANEL_ID: "ABC123",
    CONF_USER_CODE: "1234",
    CONF_APP_ID: "",
}


async def test_user_flow_creates_entry(
    hass: HomeAssistant, fake_api: FakeApi, mock_setup_entry: AsyncMock
) -> None:
    """A valid submission creates an entry keyed on the panel serial."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "PowerMaster 360R"
    assert result["result"].unique_id == "ABC123"
    # A blank app id is filled in rather than rejected.
    assert result["data"][CONF_APP_ID]
    # New entries are born current, so they never take the 10 s migration path
    # and pick up the 30 s default instead.
    assert result["result"].minor_version == 2
    assert CONF_SCAN_INTERVAL not in result["result"].options


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (VisonicAuthError("no"), "invalid_auth"),
        (VisonicConnectionError("no"), "cannot_connect"),
        (VisonicError("no"), "unknown"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant,
    fake_api: FakeApi,
    mock_setup_entry: AsyncMock,
    error: Exception,
    expected: str,
) -> None:
    """Each failure shows its error, and the form still works afterwards."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})

    with patch.object(fake_api, "async_connect", side_effect=error):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_panel_aborts(
    hass: HomeAssistant, fake_api: FakeApi, mock_entry: MockConfigEntry
) -> None:
    """The same panel cannot be added twice."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_import_from_yaml(
    hass: HomeAssistant, fake_api: FakeApi, mock_setup_entry: AsyncMock
) -> None:
    """YAML import creates an entry and carries options across."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={**ENTRY_DATA, CONF_NO_PIN_REQUIRED: True, CONF_EVENT_HOUR_OFFSET: 2},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["options"][CONF_NO_PIN_REQUIRED] is True
    assert result["options"][CONF_EVENT_HOUR_OFFSET] == 2
    # A YAML setup is an upgrade from the 10 s era, so it keeps that speed.
    assert result["options"][CONF_SCAN_INTERVAL] == LEGACY_SCAN_INTERVAL


async def test_import_aborts_on_bad_credentials(hass: HomeAssistant, fake_api: FakeApi) -> None:
    """A YAML block with dead credentials must not create a broken entry."""
    with patch.object(fake_api, "async_connect", side_effect=VisonicAuthError("no")):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_IMPORT}, data=dict(ENTRY_DATA)
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "invalid_auth"


async def test_reauth_updates_credentials(
    hass: HomeAssistant, fake_api: FakeApi, mock_entry: MockConfigEntry
) -> None:
    """Reauth rewrites the stored password."""
    result = await mock_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.visonicalarm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_USER_PASSWORD: "newpass"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_entry.data[CONF_USER_PASSWORD] == "newpass"


async def test_reauth_shows_error_first(
    hass: HomeAssistant, fake_api: FakeApi, mock_entry: MockConfigEntry
) -> None:
    """Bad credentials during reauth are reported, not stored."""
    result = await mock_entry.start_reauth_flow(hass)
    with patch.object(fake_api, "async_connect", side_effect=VisonicAuthError("no")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_reconfigure_updates_entry(
    hass: HomeAssistant, fake_api: FakeApi, mock_entry: MockConfigEntry
) -> None:
    """Reconfigure can change the host."""
    result = await mock_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM

    with patch("custom_components.visonicalarm.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, "host": "other.example.com"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_entry.data["host"] == "other.example.com"


async def test_reconfigure_rejects_a_different_panel(
    hass: HomeAssistant, fake_api: FakeApi, mock_entry: MockConfigEntry
) -> None:
    """Reconfiguring must not silently repoint the entry at another panel."""
    result = await mock_entry.start_reconfigure_flow(hass)

    other = {"serial": "OTHER1", "model": "PowerMaster 10"}
    with patch.object(fake_api, "async_get_panel_info", return_value=other):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], dict(USER_INPUT))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_panel"


async def test_options_flow(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    """Options round-trip through the flow."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_NO_PIN_REQUIRED: True, CONF_EVENT_HOUR_OFFSET: 3},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert setup_integration.options[CONF_NO_PIN_REQUIRED] is True
    # Left untouched, the poll interval is saved at the default.
    assert setup_integration.options[CONF_SCAN_INTERVAL] == DEFAULT_SCAN_INTERVAL


async def test_options_flow_offers_the_current_interval(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """The form is pre-filled with the default when no interval is stored."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    schema = result["data_schema"].schema
    key = next(k for k in schema if k == CONF_SCAN_INTERVAL)
    assert key.default() == DEFAULT_SCAN_INTERVAL


async def test_options_flow_sets_poll_interval(
    hass: HomeAssistant, setup_integration: MockConfigEntry
) -> None:
    """A new interval is stored as whole seconds and reaches the coordinator."""
    assert setup_integration.runtime_data.update_interval == timedelta(
        seconds=DEFAULT_SCAN_INTERVAL
    )

    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_NO_PIN_REQUIRED: False, CONF_EVENT_HOUR_OFFSET: 0, CONF_SCAN_INTERVAL: 120.0},
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    stored = setup_integration.options[CONF_SCAN_INTERVAL]
    assert stored == 120
    assert isinstance(stored, int)
    # The update listener reloads the entry, so the running coordinator is new
    # and polls at the new interval without a restart.
    assert setup_integration.runtime_data.update_interval == timedelta(seconds=120)

    # The form now offers the stored value back.
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    key = next(k for k in result["data_schema"].schema if k == CONF_SCAN_INTERVAL)
    assert key.default() == 120


@pytest.mark.parametrize("seconds", [5, 301])
async def test_options_flow_rejects_out_of_range_interval(
    hass: HomeAssistant, setup_integration: MockConfigEntry, seconds: int
) -> None:
    """Intervals outside 10-300 s are refused, not stored."""
    result = await hass.config_entries.options.async_init(setup_integration.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_NO_PIN_REQUIRED: False, CONF_EVENT_HOUR_OFFSET: 0, CONF_SCAN_INTERVAL: seconds},
        )
    assert CONF_SCAN_INTERVAL not in setup_integration.options
