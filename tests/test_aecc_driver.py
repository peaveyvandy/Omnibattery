"""Tests for the AECC local TCP battery driver."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from custom_components.omnibattery.config_flow import MarstekVenusConfigFlow
from custom_components.omnibattery.drivers.aecc import AeccTcpDriver


class _FakeWriter:
    def __init__(self, reader, respond):
        self.reader = reader
        self.respond = respond
        self.closed = False

    def write(self, data):
        self.respond(json.loads(data), self.reader)

    async def drain(self):
        return None

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


def _feed_response(payload, reader, response):
    reader.feed_data(
        (json.dumps({"SerialNumber": payload["SerialNumber"], **response}) + "\n").encode()
    )


@pytest.mark.asyncio
async def test_connect_and_telemetry_decode_aecc_json(monkeypatch):
    sample = {
        "Storage_list": [
            {"BatterySoc": 42, "BatteryChargingPower": 1234, "BatteryDischargingPower": 0}
        ],
        "SSumInfoList": {
            "AverageBatteryAverageSOC": 42,
            "TotalBatteryOutputPower": 0,
            "TotalPVPower": 350,
            "TotalGridOutputPower": -250,
            "TotalBackUpPower": 0,
        },
    }

    def respond(payload, reader):
        if payload.get("Get") == "Energycontrolparameters":
            response = {
                "ControlInfo": {
                    "3003": "0,00:00,00:00,0,0,0,0,0,0,100,10"
                }
            }
        else:
            response = sample
        _feed_response(payload, reader, response)

    async def open_connection(host, port):
        reader = asyncio.StreamReader()
        return reader, _FakeWriter(reader, respond)

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    driver = AeccTcpDriver("battery.local")

    assert await driver.connect() is True
    snapshot = await driver.read_telemetry()
    assert snapshot == {
        "battery_soc": 42,
        "battery_power": pytest.approx(123.4),
        "solar_power": 350,
        "ac_delivered_power": 600,
        "power_setpoint": 0,
    }
    await driver.close()
    assert driver.connected is False


@pytest.mark.asyncio
async def test_read_telemetry_honors_requested_keys(monkeypatch):
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "SSumInfoList": {"AverageBatteryAverageSOC": 52},
    })

    assert await driver.read_telemetry(["battery_soc"]) == {"battery_soc": 52}
    driver._request.assert_awaited_once_with("EnergyParameter")


@pytest.mark.asyncio
async def test_ac_delivery_is_omitted_when_source_power_fields_are_missing():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "SSumInfoList": {"TotalGridOutputPower": -250},
    })

    assert await driver.read_telemetry(["ac_delivered_power"]) == {}


@pytest.mark.asyncio
async def test_ac_delivery_fills_missing_battery_charge_power():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "Storage_list": [{"BatterySoc": 71}],
        "SSumInfoList": {
            "TotalBatteryOutputPower": 0,
            "TotalPVPower": 0,
            "TotalGridOutputPower": -408,
            "TotalBackUpPower": 0,
        },
    })

    snapshot = await driver.read_telemetry(["battery_power", "ac_delivered_power"])

    assert snapshot["battery_power"] == 408
    assert snapshot["ac_delivered_power"] == 408


@pytest.mark.asyncio
async def test_ac_delivery_replaces_near_zero_reported_battery_power():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "Storage_list": [{"BatteryChargingPower": 0}],
        "SSumInfoList": {
            "TotalBatteryOutputPower": 0,
            "TotalPVPower": 0,
            "TotalGridOutputPower": -408,
            "TotalBackUpPower": 0,
        },
    })

    snapshot = await driver.read_telemetry(["battery_power", "ac_delivered_power"])

    assert snapshot["battery_power"] == 408
    assert snapshot["ac_delivered_power"] == 408


@pytest.mark.asyncio
async def test_ac_delivery_replaces_small_negative_battery_power_noise():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "Storage_list": [{"BatterySoc": 71}],
        "SSumInfoList": {
            "TotalBatteryOutputPower": 5,
            "TotalPVPower": 0,
            "TotalGridOutputPower": -408,
            "TotalBackUpPower": 0,
        },
    })

    snapshot = await driver.read_telemetry(["battery_power", "ac_delivered_power"])

    assert snapshot["battery_power"] == 408
    assert snapshot["ac_delivered_power"] == 408


@pytest.mark.asyncio
async def test_nonzero_battery_power_is_preserved_over_ac_estimate():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={
        "Storage_list": [{"BatteryChargingPower": 1234}],
        "SSumInfoList": {
            "TotalBatteryOutputPower": 0,
            "TotalPVPower": 0,
            "TotalGridOutputPower": -408,
            "TotalBackUpPower": 0,
        },
    })

    snapshot = await driver.read_telemetry(["battery_power", "ac_delivered_power"])

    assert snapshot["battery_power"] == pytest.approx(123.4)
    assert snapshot["ac_delivered_power"] == 408


def test_capabilities_allow_for_observed_aecc_response_latency():
    capabilities = AeccTcpDriver("battery.local").capabilities

    assert capabilities.actuator_latency_s == 25.0
    assert capabilities.readback_latency_s == 45.0
    assert capabilities.engage_grace_s == 45.0
    assert capabilities.setpoint_confirm_reliable is False


@pytest.mark.asyncio
async def test_setpoint_sign_clamping_and_readback():
    driver = AeccTcpDriver("battery.local", max_charge_power_w=800)
    driver._has_storage = True
    driver._request = AsyncMock(side_effect=[
        {"Response": "OK"},
        {"ControlInfo": {"3003": "1,00:00,23:59,-800,0,6,5,0,0,100,10"}},
    ])

    result = await driver.apply_setpoint(2400)

    assert result.ok is True
    assert result.net_power_w == 800
    assert result.confirmed is True
    write = driver._request.await_args_list[0]
    assert write.args[0] == "Energycontrolparameters"
    assert write.kwargs["set_command"] is True
    assert write.args[1]["SetControlInfo"]["3003"].split(",")[3] == "-800"


@pytest.mark.asyncio
async def test_control_slot_ack_can_precede_settled_power_telemetry():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(side_effect=[
        {"Response": "OK"},
        {"ControlInfo": {"3003": "0,00:00,00:00,0,0,0,0,0,0,100,10"}},
        {
            "Storage_list": [
                {"BatteryChargingPower": 1250, "BatteryDischargingPower": 0}
            ],
            "SSumInfoList": {},
        },
    ])

    result = await driver.apply_setpoint(0)
    telemetry = await driver.read_telemetry(["battery_power"])

    assert result.ok is True
    assert result.confirmed is True
    assert result.applied == {"power_setpoint": 0}
    assert telemetry["battery_power"] == pytest.approx(125)


@pytest.mark.asyncio
async def test_discharge_and_idle_use_aecc_register_sign_and_slot():
    driver = AeccTcpDriver(
        "battery.local",
        max_charge_power_w=2400,
        max_discharge_power_w=2400,
    )
    driver._request = AsyncMock(return_value={"Response": "OK"})

    discharge = await driver.apply_setpoint(-900, read_back=False)
    idle = await driver.apply_setpoint(0, read_back=False)

    assert discharge.net_power_w == -900
    assert driver._request.await_args_list[0].args[1]["SetControlInfo"]["3003"].split(",")[3] == "900"
    assert idle.net_power_w == 0
    assert driver._request.await_args_list[1].args[1]["SetControlInfo"]["3003"].startswith(
        "0,00:00,00:00,0"
    )


@pytest.mark.asyncio
async def test_setpoint_write_failure_is_not_reported_as_success():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value=None)

    result = await driver.apply_setpoint(500, read_back=False)

    assert result.ok is False
    assert result.failure_reason == "setpoint_write_failed"


@pytest.mark.asyncio
async def test_apply_config_sets_extended_limit_and_caps_power_at_s2400_rating():
    driver = AeccTcpDriver("battery.local")
    driver._request = AsyncMock(return_value={"Response": "OK"})

    assert await driver.apply_config(
        max_soc_pct=95,
        min_soc_pct=10,
        max_charge_power_w=3000,
        max_discharge_power_w=1800,
    )
    limit_write = driver._request.await_args_list[0]
    assert limit_write.args[1]["SetControlInfo"] == {"3039": "2400"}

    result = await driver.apply_setpoint(3000, read_back=False)

    assert result.net_power_w == 2400


@pytest.mark.asyncio
async def test_tcp_connection_failure_can_be_retried(monkeypatch):
    reader = asyncio.StreamReader()
    attempts = 0

    async def open_connection(host, port):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("offline")
        writer = _FakeWriter(
            reader,
            lambda payload, target: _feed_response(
                payload,
                target,
                {
                    "Storage_list": [{"BatterySoc": 45}],
                    "SSumInfoList": {},
                },
            ),
        )
        return reader, writer

    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    driver = AeccTcpDriver("battery.local")

    assert await driver.connect() is False
    assert await driver.connect() is True
    assert attempts == 2
    await driver.close()


@pytest.mark.asyncio
async def test_concurrent_requests_keep_serials_with_slow_drain():
    driver = AeccTcpDriver("battery.local")
    drain_started = asyncio.Event()
    release_drain = asyncio.Event()
    expected_serials = []
    writer = _FakeWriter(asyncio.StreamReader(), lambda *_: None)
    drain = writer.drain

    async def slow_drain():
        if not drain_started.is_set():
            drain_started.set()
            await release_drain.wait()
        await drain()

    writer.drain = slow_drain
    driver._get_reader_writer_unlocked = AsyncMock(return_value=(writer.reader, writer))

    async def read_json(_reader, expected_serial):
        expected_serials.append(expected_serial)
        return {"SerialNumber": expected_serial}

    driver._read_json = read_json
    first = asyncio.create_task(driver._request("First"))
    await drain_started.wait()
    second = asyncio.create_task(driver._request("Second"))
    await asyncio.sleep(0)
    release_drain.set()

    assert await asyncio.gather(first, second) == [
        {"SerialNumber": 1},
        {"SerialNumber": 2},
    ]
    assert expected_serials == [1, 2]


@pytest.mark.asyncio
async def test_config_flow_probes_and_saves_aecc_endpoint(monkeypatch):
    flow = MarstekVenusConfigFlow()
    flow.battery_index = 0
    flow._current_battery_data = {"brand": "aecc"}
    flow.async_step_battery_limits = AsyncMock(return_value={"type": "form"})
    probe = AsyncMock(return_value=True)
    monkeypatch.setattr(AeccTcpDriver, "probe", probe)

    result = await flow.async_step_battery_connection_aecc({
        "name": "Garage",
        "host": "192.0.2.10",
        "port": 8080,
        "manufacturer": "Sunpura",
        "model": "S2400",
    })

    assert result == {"type": "form"}
    probe.assert_awaited_once_with("192.0.2.10", 8080)
    assert flow._current_battery_data["brand"] == "aecc"
    assert flow._current_battery_data["max_charge_power"] == 800
    assert flow._current_battery_data["max_discharge_power"] == 800
    flow.async_step_battery_limits.assert_awaited_once()
