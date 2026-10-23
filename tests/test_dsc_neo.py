"""DSC PowerSeries Neo panels on PowerManage: STAY, NIGHT and status-driven states.

A Neo reached through the Connect Alarm cloud reports the same endpoints as a
Visonic panel but names its arm modes AWAY / STAY / NIGHT / DISARM, keeps the
armed state during the exit and entry delays and flags them in ``status``.
"""

import copy
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.alarm_control_panel import AlarmControlPanelEntityFeature
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.visonicalarm.const import DOMAIN

from .conftest import FakeApi

PANEL = "alarm_control_panel.example_house"


def _neo_state(name: str, transitions: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "settable": True,
        "options": [],
        "statuses": ["EXIT", "ENTRY", "HOLD"],
        "transitions": transitions,
    }


NEO_PANEL_INFO: dict[str, Any] = {
    "serial": "ABC123",
    "model": "HS2064",
    "manufacturer": "DSC",
    "current_user": "master_user",
    "state_sets": {
        "all": [
            _neo_state("AWAY", ["DISARM", "STAY"]),
            _neo_state("NIGHT", ["DISARM", "AWAY", "STAY"]),
            _neo_state("STAY", ["DISARM", "AWAY", "NIGHT"]),
            _neo_state("DISARM", ["DISARM", "STAY", "NIGHT", "AWAY"]),
            {
                "name": "PROGRAMMING",
                "settable": False,
                "options": [],
                "statuses": [],
                "transitions": [],
            },
        ]
    },
    "partitions": [
        {"id": 1, "active": True, "exit_delay_time": 40, "state_set": "all", "name": "Partition 1"},
        {"id": -1, "active": True, "exit_delay_time": 0, "state_set": "all", "name": "ALL"},
    ],
    "bypass_mode": "manual_bypass",
    "features": {},
}


async def _neo(
    hass: HomeAssistant, entry: MockConfigEntry, fake_api: FakeApi, **partition: Any
) -> None:
    """Reload the entry as a Neo whose single partition looks like ``partition``."""
    fake_api.status["partitions"] = [
        {"id": 1, "state": "DISARM", "status": "", "ready": True, "options": [], **partition}
    ]
    with patch.object(fake_api, "async_get_panel_info", return_value=copy.deepcopy(NEO_PANEL_INFO)):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()


async def test_neo_stay_is_armed_home(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    await _neo(hass, setup_integration, fake_api, state="STAY")
    assert hass.states.get(PANEL).state == "armed_home"


async def test_neo_night_is_armed_night(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    await _neo(hass, setup_integration, fake_api, state="NIGHT")
    assert hass.states.get(PANEL).state == "armed_night"


async def test_neo_exit_delay_is_arming_in_every_mode(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    for mode in ("AWAY", "STAY", "NIGHT"):
        await _neo(hass, setup_integration, fake_api, state=mode, status="EXIT")
        assert hass.states.get(PANEL).state == "arming", mode


async def test_neo_exit_delay_from_disarm_is_arming(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Armed from disarm, a Neo says DISARM + EXIT for the first seconds of the delay."""
    await _neo(hass, setup_integration, fake_api, state="DISARM", status="EXIT")
    assert hass.states.get(PANEL).state == "arming"


async def test_neo_entry_delay_is_pending(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    await _neo(hass, setup_integration, fake_api, state="STAY", status="ENTRY")
    assert hass.states.get(PANEL).state == "pending"


async def test_neo_alarm_while_stay_is_triggered(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    with patch.object(fake_api, "async_get_alarms", return_value=[{"id": 1}]):
        await _neo(hass, setup_integration, fake_api, state="STAY")
    assert hass.states.get(PANEL).state == "triggered"


async def test_neo_offers_home_night_and_away(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    await _neo(hass, setup_integration, fake_api)
    features = hass.states.get(PANEL).attributes["supported_features"]
    assert features & AlarmControlPanelEntityFeature.ARM_HOME
    assert features & AlarmControlPanelEntityFeature.ARM_NIGHT
    assert features & AlarmControlPanelEntityFeature.ARM_AWAY


async def test_neo_arm_commands_use_the_panel_names(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    await _neo(hass, setup_integration, fake_api)
    for service, expected in (
        ("alarm_arm_home", "STAY"),
        ("alarm_arm_night", "NIGHT"),
        ("alarm_arm_away", "AWAY"),
        ("alarm_disarm", "DISARM"),
    ):
        fake_api.set_state_calls.clear()
        await hass.services.async_call(
            "alarm_control_panel", service, {ATTR_ENTITY_ID: PANEL, "code": "1234"}, blocking=True
        )
        assert fake_api.set_state_calls == [expected], service


async def test_visonic_panels_still_arm_home_with_home(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """The captured Visonic fixture is untouched: HOME stays HOME, no NIGHT offered."""
    features = hass.states.get(PANEL).attributes["supported_features"]
    assert not features & AlarmControlPanelEntityFeature.ARM_NIGHT
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_home",
        {ATTR_ENTITY_ID: PANEL, "code": "1234"},
        blocking=True,
    )
    assert fake_api.set_state_calls == ["HOME"]


async def _set_partition(
    hass: HomeAssistant, entry: MockConfigEntry, fake_api: FakeApi, **partition: Any
) -> None:
    """Change what the cloud reports and poll it once."""
    fake_api.status["partitions"][0].update(partition)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def _arm_night(hass: HomeAssistant) -> None:
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_arm_night",
        {ATTR_ENTITY_ID: PANEL, "code": "1234"},
        blocking=True,
    )
    await hass.async_block_till_done()


async def test_arm_command_shows_arming_until_the_cloud_catches_up(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """The cloud serves the old state for a while after an arm command.

    Meanwhile the panel is arming, and a dashboard showing "disarmed" would
    offer every mode again.
    """
    await _neo(hass, setup_integration, fake_api, state="DISARM")
    await _arm_night(hass)
    assert fake_api.set_state_calls == ["NIGHT"]
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api)  # still DISARM
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api, status="EXIT")
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api, state="NIGHT", status="EXIT")
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api, state="NIGHT", status="")
    assert hass.states.get(PANEL).state == "armed_night"


async def test_arm_command_that_never_shows_up_gives_way(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_api: FakeApi,
    freezer: FrozenDateTimeFactory,
) -> None:
    """If the cloud never moves, the panel goes back to what it reports."""
    await _neo(hass, setup_integration, fake_api, state="DISARM")
    await _arm_night(hass)
    freezer.tick(timedelta(seconds=31))
    await _set_partition(hass, setup_integration, fake_api)
    assert hass.states.get(PANEL).state == "disarmed"


async def test_switching_armed_modes_shows_arming_until_the_new_one(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """From STAY to NIGHT the cloud keeps STAY, then STAY + EXIT, then NIGHT."""
    await _neo(hass, setup_integration, fake_api, state="STAY")
    await _arm_night(hass)
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api)  # still STAY
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api, status="EXIT")
    assert hass.states.get(PANEL).state == "arming"
    await _set_partition(hass, setup_integration, fake_api, state="NIGHT", status="")
    assert hass.states.get(PANEL).state == "armed_night"


async def test_disarm_command_is_not_anticipated(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """Disarmed is only shown once the cloud says so."""
    await _neo(hass, setup_integration, fake_api, state="NIGHT")
    await hass.services.async_call(
        "alarm_control_panel",
        "alarm_disarm",
        {ATTR_ENTITY_ID: PANEL, "code": "1234"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert fake_api.set_state_calls == ["DISARM"]
    assert hass.states.get(PANEL).state == "armed_night"


async def test_delays_poll_fast_and_then_settle(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """An exit or entry delay polls every 10 s; a settled panel at the configured rate."""
    await _neo(hass, setup_integration, fake_api, state="NIGHT")
    coordinator = setup_integration.runtime_data
    configured = coordinator.update_interval
    assert configured > timedelta(seconds=10)
    await _set_partition(hass, setup_integration, fake_api, status="EXIT")
    assert coordinator.update_interval == timedelta(seconds=10)
    await _set_partition(hass, setup_integration, fake_api, status="")
    assert coordinator.update_interval == configured
    await _set_partition(hass, setup_integration, fake_api, status="ENTRY")
    assert coordinator.update_interval == timedelta(seconds=10)
    await _set_partition(hass, setup_integration, fake_api, state="DISARM", status="")
    assert coordinator.update_interval == configured
    await _arm_night(hass)
    assert coordinator.update_interval == timedelta(seconds=10)


def _wired(
    device_id: int, number: int, name: str, zone_type: str, *, opened: bool = False
) -> dict[str, Any]:
    return {
        "id": device_id,
        "device_number": number,
        "device_type": "ZONE",
        "zone_type": zone_type,
        "subtype": "HW_ZONE_CONNECTED_DIRECTLY_TO_THE_PANEL",
        "preenroll": False,
        "warnings": [{"type": "OPENED"}] if opened else None,
        "partitions": [1],
        "traits": {"bypass": {"enabled": False}, "soak": {"enabled": False}},
        "name": name,
        "removable": True,
        "renamable": True,
    }


NEO_DEVICES = [
    _wired(9001, 1, "Hall", "NIGHT", opened=True),
    _wired(9002, 4, "Garage", "INSTANT"),
    _wired(9003, 17, "Tampers", "24HR_BURGLARY"),
    _wired(9004, 7, "Garden", "INSTANT_STAY_AWAY"),
]


async def test_neo_wired_zones_report_live_state_as_motion(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.devices = copy.deepcopy(NEO_DEVICES)
    await _neo(hass, setup_integration, fake_api)
    registry = er.async_get(hass)

    hall = registry.async_get_entity_id("binary_sensor", DOMAIN, "visonic_zone_9001")
    garage = registry.async_get_entity_id("binary_sensor", DOMAIN, "visonic_zone_9002")
    assert hall and garage
    assert hass.states.get(hall).state == "on"
    assert hass.states.get(hall).attributes["device_class"] == "motion"
    assert hass.states.get(garage).state == "off"
    # Wired zones are zones, not faulty devices.
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, "visonic_device_9001") is None


async def test_neo_24h_loop_is_tamper_and_cannot_be_bypassed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    fake_api.devices = copy.deepcopy(NEO_DEVICES)
    await _neo(hass, setup_integration, fake_api)
    registry = er.async_get(hass)

    tampers = registry.async_get_entity_id("binary_sensor", DOMAIN, "visonic_zone_9003")
    assert hass.states.get(tampers).attributes["device_class"] == "tamper"
    assert hass.states.get(tampers).state == "off"
    assert registry.async_get_entity_id("switch", DOMAIN, "visonic_bypass_9003") is None
    assert registry.async_get_entity_id("switch", DOMAIN, "visonic_bypass_9002") is not None


async def test_neo_wired_zone_says_whether_it_is_armed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_api: FakeApi
) -> None:
    """STAY drops NIGHT zones and the STAY_AWAY garden; NIGHT re-arms the garden."""
    fake_api.devices = copy.deepcopy(NEO_DEVICES)
    registry = er.async_get(hass)

    def armed(unique_id: str) -> bool | None:
        entity_id = registry.async_get_entity_id("binary_sensor", DOMAIN, unique_id)
        return hass.states.get(entity_id).attributes["armed"]

    await _neo(hass, setup_integration, fake_api, state="STAY")
    assert armed("visonic_zone_9001") is False  # hall: NIGHT type
    assert armed("visonic_zone_9004") is False  # garden: STAY_AWAY
    assert armed("visonic_zone_9002") is True  # garage
    assert armed("visonic_zone_9003") is True  # 24-hour loop

    await _neo(hass, setup_integration, fake_api, state="NIGHT")
    assert armed("visonic_zone_9001") is False
    assert armed("visonic_zone_9004") is True

    await _neo(hass, setup_integration, fake_api, state="AWAY")
    assert armed("visonic_zone_9001") is True

    await _neo(hass, setup_integration, fake_api)
    assert armed("visonic_zone_9002") is False
    assert armed("visonic_zone_9003") is True
