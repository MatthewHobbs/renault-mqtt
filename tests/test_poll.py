"""Unit tests for renault_mqtt.poll (Kamereon poll payload assembly and location carry-forward)."""
import asyncio
import enum
import logging
from types import SimpleNamespace

import pytest

from renault_mqtt import charge, debug, mqtt
from renault_mqtt.poll import POLL_KEYS, RHD_LOCALES, poll_vehicle
from renault_mqtt.util import _num

LOG = logging.getLogger("renault_mqtt.test_poll")

R5_KEYS = {
    "range": "battery_autonomy",
    "charging_power": "charging_rate",
    "charging_remaining": "charging_remaining_time",
    "plug_status": "charger_plug_status",
    "charging_flap": "charging_flap_status",
    "charging_status": "charger_status",
    "last_updated": "battery_last_activity",
    "mileage": "vehicle_mileage",
    "soc_target": "soc_max_target",
    "soc_min": "soc_min_target",
}


class _PlugState(enum.Enum):
    UNPLUGGED = 0
    PLUGGED = 1
    PLUG_ERROR = 2
    PLUG_UNKNOWN = 3


class _ChargeState(enum.Enum):
    NOT_IN_CHARGE = 0.0
    CHARGE_IN_PROGRESS = 1.0


PLUG_LABELS = {
    _PlugState.UNPLUGGED: "Disconnected",
    _PlugState.PLUGGED: "Connected",
    _PlugState.PLUG_ERROR: "Plug Error",
    _PlugState.PLUG_UNKNOWN: "Unknown",
}
CHARGE_LABELS = {
    _ChargeState.NOT_IN_CHARGE: "Not Charging",
    _ChargeState.CHARGE_IN_PROGRESS: "Charging",
}


class _FakeBattery:
    def __init__(self, *, soc=60, autonomy=200, temp=18, power=0.0, remaining=None,
                 energy=30.0, plug=_PlugState.PLUGGED, status=_ChargeState.NOT_IN_CHARGE,
                 timestamp="2026-01-01T00:00:00Z"):
        self.batteryLevel = soc
        self.batteryAutonomy = autonomy
        self.batteryTemperature = temp
        self.chargingInstantaneousPower = power
        self.chargingRemainingTime = remaining
        if energy is not False:
            self.batteryAvailableEnergy = energy
        self.plugStatus = plug.value if plug is not None else None
        self.chargingStatus = status.value if status is not None else None
        self.timestamp = timestamp
        self._plug = plug
        self._status = status

    def get_plug_status(self):
        return self._plug

    def get_charging_status(self):
        return self._status


class _FakeVehicle:
    def __init__(self, *, battery=None, fail=(), loc=None, cabin=20):
        self._battery = _FakeBattery() if battery is None else battery
        self._fail = set(fail)
        self._loc = (SimpleNamespace(gpsLatitude=51.512345, gpsLongitude=-0.123456,
                                     lastUpdateTime="2026-01-01T12:00:00Z")
                     if loc is None else loc)
        self._cabin = cabin

    def _check(self, name):
        if name in self._fail:
            raise RuntimeError(f"{name} down")

    async def get_battery_status(self):
        self._check("battery")
        return self._battery

    async def get_cockpit(self):
        self._check("cockpit")
        return SimpleNamespace(totalMileage=12345)

    async def get_hvac_status(self):
        self._check("hvac")
        return SimpleNamespace(
            externalTemperature=12, internalTemperature=self._cabin,
            hvacStatus="off", socThreshold=20, lastUpdateTime="t",
        )

    async def get_charge_schedule(self):
        self._check("sched")
        return {
            "preconditioningTemperature": 21,
            "preconditioningHeatedStrgWheel": True,
            "preconditioningHeatedLeftSeat": False,
            "preconditioningHeatedRightSeat": True,
            "chargeModeRq": "scheduled_charge",
            "chargeTimeStart": "0230",
            "chargeDuration": 360,
        }

    async def get_hvac_settings(self):
        self._check("hvac_settings")
        day = SimpleNamespace(readyAtTime="T07:15Z")
        sched = SimpleNamespace(
            activated=True, monday=day, tuesday=None, wednesday=None,
            thursday=None, friday=None, saturday=None, sunday=None,
        )
        return SimpleNamespace(mode="scheduled", schedules=[sched])

    async def get_battery_soc(self):
        self._check("soc")
        return SimpleNamespace(socTarget=80, socMin=20)

    async def get_tyre_pressure(self):
        self._check("pressure")
        return SimpleNamespace(flPressure=2.4, frPressure=2.4, rlPressure=2.3, rrPressure=2.3)

    async def get_charge_mode(self):
        self._check("charge_mode")
        return SimpleNamespace(chargeMode="always")

    async def get_location(self):
        self._check("location")
        return self._loc

    async def get_charges(self, start, end):
        self._check("charges")
        return SimpleNamespace(raw_data={"charges": [{
            "chargeStartDate": "2026-06-21T00:00:00+00:00",
            "chargeEndDate": "2026-06-21T03:00:00+00:00",
            "chargeStartBatteryLevel": 35,
            "chargeEndBatteryLevel": 80,
            "chargeBatteryLevelRecovered": 45,
            "chargeEnergyRecovered": 23.4,
        }]})

    async def get_details(self):
        return SimpleNamespace(raw_data={"vin": "SECRET", "batteryLevel": 60})


class _FakeSession:
    def __init__(self, vehicle, locale="en_GB"):
        self._vehicle = vehicle
        self.locale = locale

    async def vehicle(self):
        return self._vehicle


def _is_charging(battery):
    power = _num(getattr(battery, "chargingInstantaneousPower", None)) or 0
    return battery.get_charging_status() == _ChargeState.CHARGE_IN_PROGRESS or power > 0.1


def _charging_label(battery):
    status = battery.get_charging_status()
    return CHARGE_LABELS.get(status, "Unknown")


def _run_poll(vehicle=None, *, state=None, locale="en_GB", capacity=52.0,
              supported=frozenset({"pressure", "charge-mode", "hvac-settings"}),
              dist_unit="km", keys=None, cabin_temp=False, publish_location=True,
              latest=None, breakers=None, fails=None, skips=None,
              breaker_trip=3, breaker_retry_every=12):
    st = {} if state is None else state
    brk = {} if breakers is None else breakers
    fls = {} if fails is None else fails
    skp = {} if skips is None else skips
    lat = {} if latest is None else latest
    veh = _FakeVehicle() if vehicle is None else vehicle
    return asyncio.run(poll_vehicle(
        _FakeSession(veh, locale=locale),
        st,
        capacity,
        set(supported),
        dist_unit,
        plug_status_labels=PLUG_LABELS,
        plug_plugged=_PlugState.PLUGGED,
        charging_check=_is_charging,
        charging_label=_charging_label,
        breakers=brk,
        breaker_fails=fls,
        breaker_skips=skp,
        breaker_trip=breaker_trip,
        breaker_retry_every=breaker_retry_every,
        hvac_due=None,
        gps_precision=4,
        latest_data=lambda: lat,
        detect_plug_suspect=lambda s, p, m, soc, chg: "off",
        log=LOG,
        keys=keys,
        cabin_temp=cabin_temp,
        rhd_locales=RHD_LOCALES,
        publish_location=publish_location,
    ))


def test_poll_vehicle_default_and_custom_keys_with_drive_side_and_cabin_temp(monkeypatch):
    monkeypatch.setattr(debug, "maybe_dump_api", lambda v: asyncio.sleep(0))
    data_a290, attrs = _run_poll()
    assert data_a290["battery_level"] == 60
    assert data_a290["range"] == 200.0
    assert data_a290["mileage"] == 12345.0
    assert data_a290["plug_status"] == "Connected"
    assert data_a290["charging_flap"] == "Open: Plugged In"
    assert data_a290["charging_status"] == "Not Charging"
    assert data_a290["charging"] == "off"
    assert data_a290["last_updated"] == "2026-01-01T00:00:00Z"
    assert data_a290["drive_side"] == "RHD"
    assert data_a290["heated_seat_driver"] == "on"
    assert data_a290["heated_seat_passenger"] == "off"
    assert data_a290["soc_target"] == 80 and data_a290["soc_min"] == 20
    assert data_a290["tyre_pressure_fl"] == 2.4 and data_a290["tyre_pressure_rr"] == 2.3
    assert data_a290["charge_mode"] == "always"
    assert data_a290["charge_schedule_mode"] == "Scheduled Charge"
    assert data_a290["scheduled_charge_start"] == "02:30"
    assert data_a290["scheduled_charge_duration"] == 360
    assert data_a290["climate_schedule_mode"] == "Scheduled"
    assert data_a290["climate_ready_time"] == "Mon 07:15"
    assert "cabin_temperature" not in data_a290
    assert attrs == {
        "latitude": 51.5123,
        "longitude": -0.1235,
        "gps_accuracy": 11,
        "last_update": "2026-01-01T12:00:00Z",
    }

    bat_chg = _FakeBattery(
        soc=55, autonomy=150, power=7.4, remaining=90, energy=False,
        plug=_PlugState.UNPLUGGED, status=_ChargeState.CHARGE_IN_PROGRESS, timestamp=None,
    )
    veh_r5 = _FakeVehicle(battery=bat_chg, cabin=19.5)
    data_r5, _ = _run_poll(
        veh_r5, locale="fr_FR", dist_unit="mi", keys=R5_KEYS, cabin_temp=True,
    )
    assert data_r5["battery_autonomy"] == 93.2
    assert data_r5["vehicle_mileage"] == 7670.8
    assert data_r5["charging_rate"] == 7.4
    assert data_r5["charging_remaining_time"] == 90
    assert data_r5["available_energy"] == 28.6  # 55% of 52 kWh fallback
    assert data_r5["charger_plug_status"] == "Disconnected"
    assert data_r5["charging_flap_status"] == "Closed"
    assert data_r5["charger_status"] == "Charging"
    assert data_r5["charging"] == "on"
    assert data_r5["battery_last_activity"] is None
    assert data_r5["drive_side"] == "LHD"
    assert data_r5["heated_seat_driver"] == "off"
    assert data_r5["heated_seat_passenger"] == "on"
    assert data_r5["cabin_temperature"] == 19.5
    assert data_r5["soc_max_target"] == 80 and data_r5["soc_min_target"] == 20
    assert set(POLL_KEYS) == set(R5_KEYS)


def test_poll_vehicle_propagates_battery_failure_and_tolerates_secondary_failures(caplog):
    with pytest.raises(RuntimeError, match="battery down"):
        _run_poll(_FakeVehicle(fail={"battery"}))

    veh = _FakeVehicle(
        fail={"cockpit", "hvac", "sched", "hvac_settings", "soc", "pressure", "charge_mode", "location"},
        cabin=21,
    )
    with caplog.at_level(logging.WARNING, logger=LOG.name):
        data, attrs = _run_poll(veh, cabin_temp=True)
    assert attrs is None
    assert data["battery_level"] == 60
    for omitted in ("mileage", "external_temperature", "cabin_temperature", "hvac_status",
                    "preconditioning_temperature", "charge_schedule_mode", "climate_schedule_mode",
                    "soc_target", "soc_min", "tyre_pressure_fl", "charge_mode", "gps_last_activity"):
        assert omitted not in data
    messages = [r.getMessage() for r in caplog.records]
    for expected in ("cockpit unavailable:", "hvac unavailable:", "ev/settings unavailable:",
                     "hvac-settings unavailable:", "battery_soc unavailable:",
                     "tyre_pressure unavailable:", "charge_mode unavailable:",
                     "location unavailable:"):
        assert any(expected in m for m in messages), expected


def test_poll_vehicle_skips_unadvertised_optional_endpoints_and_disabled_location(monkeypatch):
    monkeypatch.setattr(mqtt, "PUBLISH_LOCATION", False)
    calls = []

    class V(_FakeVehicle):
        async def get_hvac_settings(self):
            calls.append("hvac-settings")
            return await super().get_hvac_settings()

        async def get_tyre_pressure(self):
            calls.append("pressure")
            return await super().get_tyre_pressure()

        async def get_charge_mode(self):
            calls.append("charge-mode")
            return await super().get_charge_mode()

        async def get_location(self):
            calls.append("location")
            return await super().get_location()

    data, attrs = _run_poll(V(), supported=frozenset(), publish_location=None)
    assert calls == []
    assert attrs is None
    assert "gps_last_activity" not in data
    assert "tyre_pressure_fl" not in data and "charge_mode" not in data
    assert "climate_schedule_mode" not in data


@pytest.mark.parametrize("loc_mode,state_prev,latest_prev,expected_ts,expect_warn", [
    ("sentinel", None, None, None, True),
    ("sentinel", "2026-09-01T08:00:00Z", "2026-09-02T09:00:00Z", "2026-09-01T08:00:00Z", True),
    ("sentinel", None, "2026-09-02T09:00:00Z", "2026-09-02T09:00:00Z", True),
    ("null_coords", "2026-09-01T08:00:00Z", None, "2026-09-01T08:00:00Z", False),
    ("error", "2026-09-01T08:00:00Z", None, "2026-09-01T08:00:00Z", False),
    ("error", None, "2026-09-02T09:00:00Z", "2026-09-02T09:00:00Z", False),
    ("valid", "2026-09-01T08:00:00Z", None, "2026-09-07T11:11:11Z", False),
])
def test_poll_vehicle_location_sentinel_error_and_carry_forward(
        caplog, loc_mode, state_prev, latest_prev, expected_ts, expect_warn):
    state = {"gps_last_activity": state_prev} if state_prev else {}
    latest = {"gps_last_activity": latest_prev} if latest_prev else {}
    fail = {"location"} if loc_mode == "error" else set()
    if loc_mode == "sentinel":
        loc = SimpleNamespace(gpsLatitude=91, gpsLongitude=181, lastUpdateTime="2026-09-07T10:00:00Z")
    elif loc_mode == "null_coords":
        loc = SimpleNamespace(gpsLatitude=None, gpsLongitude=None, lastUpdateTime=None)
    else:
        loc = SimpleNamespace(gpsLatitude=51.5, gpsLongitude=-0.1, lastUpdateTime="2026-09-07T11:11:11Z")

    with caplog.at_level(logging.WARNING, logger=LOG.name):
        data, attrs = _run_poll(_FakeVehicle(fail=fail, loc=loc), state=state, latest=latest)

    if loc_mode == "valid":
        assert attrs is not None and attrs["latitude"] == 51.5
    else:
        assert attrs is None
    assert data.get("gps_last_activity") == expected_ts
    assert state.get("gps_last_activity") == expected_ts
    warned = any("location fix rejected" in r.getMessage() for r in caplog.records)
    assert warned is expect_warn


def test_poll_vehicle_hvac_breaker_trip_retry_and_recovery(caplog):
    breakers, fails, skips = {}, {}, {}
    broken = {"v": True}

    class V(_FakeVehicle):
        async def get_hvac_settings(self):
            if broken["v"]:
                raise RuntimeError("502000")
            return await super().get_hvac_settings()

    with caplog.at_level(logging.INFO, logger=LOG.name):
        for _ in range(3):
            data, _ = _run_poll(
                V(), supported={"hvac-settings"}, breakers=breakers, fails=fails, skips=skips,
                breaker_trip=3, breaker_retry_every=2,
            )
            assert "climate_schedule_mode" not in data
        assert breakers.get("hvac-settings") is True
        # First poll while tripped is skipped; second poll probes and recovers when broken=False.
        _run_poll(
            V(), supported={"hvac-settings"}, breakers=breakers, fails=fails, skips=skips,
            breaker_trip=3, breaker_retry_every=2,
        )
        broken["v"] = False
        data, _ = _run_poll(
            V(), supported={"hvac-settings"}, breakers=breakers, fails=fails, skips=skips,
            breaker_trip=3, breaker_retry_every=2,
        )
    assert "hvac-settings" not in breakers
    assert data["climate_schedule_mode"] == "Scheduled"
    assert any("pausing it" in r.getMessage() for r in caplog.records)
    assert any("re-enabled" in r.getMessage() for r in caplog.records)


def test_poll_vehicle_reconciles_charges_and_handles_charges_failure(monkeypatch):
    monkeypatch.setattr(charge, "now_ts", lambda: 1000.0)
    state = {}
    data, _ = _run_poll(_FakeVehicle(), state=state, supported={"charges"})
    assert data["last_charge_end"] == "2026-06-21T03:00:00+00:00"
    assert data["last_charge_recovered_pct"] == 45
    assert state["charges_last_fetch"] == 1000.0

    data_fail, _ = _run_poll(_FakeVehicle(fail={"charges"}), state={}, supported={"charges"})
    assert data_fail["battery_level"] == 60
    assert "last_charge_end" not in data_fail
