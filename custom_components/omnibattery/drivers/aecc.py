"""Local TCP driver for AECC-protocol batteries such as the Sunpura S2400.

The JSON command framing and register semantics follow the MIT-licensed
AECC Battery (Local TCP) integration:
https://github.com/StekkerDeal/aecc-battery-local
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any, Optional

from .base import (
    DELIVERED_AC_POWER_KEY,
    BatteryDriver,
    DriverCapabilities,
    ReadGroup,
    SetpointResult,
    TelemetrySnapshot,
)

_LOGGER = logging.getLogger(__name__)
_DEFAULT_PORT = 8080
_DEFAULT_POWER_LIMIT_W = 800
_MAX_POWER_W = 2400
_ACTUATOR_LATENCY_S = 25.0
_READBACK_LATENCY_S = 45.0
_ENGAGE_GRACE_S = 45.0
_CONNECT_TIMEOUT_S = 5
_REQUEST_TIMEOUT_S = 10
_MAX_RESPONSE_BYTES = 1_048_576

_REG_EMS_ENABLE = "3000"
_REG_SCHEDULE_MODE = "3020"
_REG_AI_SMART_CHARGE = "3021"
_REG_AI_SMART_DISCHARGE = "3022"
_REG_CONTROL_TIME1 = "3003"
_REG_CUSTOM_MODE = "3030"
_REG_MAX_FEED_POWER = "3039"
_CONTROL_READ_REGISTERS = [int(_REG_CONTROL_TIME1)]
_CONTROL_KEYS = frozenset({"power_setpoint"})
_POWER_KEYS = frozenset({
    "battery_soc",
    "battery_power",
    "solar_power",
    DELIVERED_AC_POWER_KEY,
})

SENSOR_DEFINITIONS = [
    {"key": "battery_soc", "name": "Battery SOC", "unit": "%", "device_class": "battery", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "battery_power", "name": "Battery Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "solar_power", "name": "Solar Power", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "high", "enabled_by_default": True},
    {"key": "power_setpoint", "name": "Power Setpoint", "unit": "W", "device_class": "power", "state_class": "measurement", "scale": 1, "precision": 0, "scan_interval": "medium", "enabled_by_default": False},
]


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _register_value(parameters: dict, address: str) -> Any:
    return parameters.get(address, parameters.get(int(address)))


class AeccTcpDriver(BatteryDriver):
    """Own one persistent, serialized TCP connection to an AECC device."""

    def __init__(
        self,
        host: str,
        *,
        port: int = _DEFAULT_PORT,
        manufacturer: str = "Sunpura",
        model: str = "S2400",
        max_charge_power_w: int = _DEFAULT_POWER_LIMIT_W,
        max_discharge_power_w: int = _DEFAULT_POWER_LIMIT_W,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.manufacturer = manufacturer.strip() or "AECC"
        self._model = model.strip() or "S2400"
        self._max_charge_power_w = max(0, min(int(max_charge_power_w), _MAX_POWER_W))
        self._max_discharge_power_w = max(0, min(int(max_discharge_power_w), _MAX_POWER_W))
        self._capabilities = DriverCapabilities(
            hardware_soc_cutoff=False,
            has_force_mode=False,
            push_telemetry=False,
            max_charge_power_w=_MAX_POWER_W,
            max_discharge_power_w=_MAX_POWER_W,
            has_mppt_pv=False,
            has_alarm_registers=False,
            has_rs485_control=False,
            has_energy_counters=False,
            has_nominal_capacity=False,
            has_daily_energy_counters=False,
            has_solar_telemetry=True,
            setpoint_confirm_reliable=False,
            actuator_latency_s=_ACTUATOR_LATENCY_S,
            readback_latency_s=_READBACK_LATENCY_S,
            engage_grace_s=_ENGAGE_GRACE_S,
        )
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._rx_reader: asyncio.StreamReader | None = None
        self._rx_buffer = b""
        self._serial = 0
        self._io_lock = asyncio.Lock()
        self._connected = False
        self._shutting_down = False
        self._has_storage = False
        self._max_soc_pct = 100
        self._min_soc_pct = 10
        self._read_groups = [
            ReadGroup("high", tuple(_POWER_KEYS)),
            ReadGroup("medium", tuple(_CONTROL_KEYS)),
        ]

    @property
    def capabilities(self) -> DriverCapabilities:
        return self._capabilities

    @property
    def model_label(self) -> Optional[str]:
        return self._model

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def read_groups(self) -> list[ReadGroup]:
        return self._read_groups

    @property
    def sensor_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    @property
    def number_definitions(self) -> list[dict]:
        return []

    @property
    def select_definitions(self) -> list[dict]:
        return []

    @property
    def switch_definitions(self) -> list[dict]:
        return []

    @property
    def binary_sensor_definitions(self) -> list[dict]:
        return []

    @property
    def button_definitions(self) -> list[dict]:
        return []

    @property
    def all_definitions(self) -> list[dict]:
        return SENSOR_DEFINITIONS

    @property
    def control_dependency_keys(self) -> frozenset:
        return frozenset({"battery_soc", "battery_power", "power_setpoint", DELIVERED_AC_POWER_KEY})

    async def connect(self) -> bool:
        result = await self._request("EnergyParameter")
        self._connected = bool(
            result
            and (result.get("Storage_list") or result.get("SSumInfoList"))
        )
        return self._connected

    async def close(self) -> None:
        async with self._io_lock:
            await self._close_unlocked()

    def set_shutting_down(self, value: bool) -> None:
        self._shutting_down = value

    async def read_telemetry(self, keys: Optional[list[str]] = None) -> TelemetrySnapshot:
        requested = set(keys) if keys is not None else set(_POWER_KEYS | _CONTROL_KEYS)
        snapshot: dict[str, Any] = {}

        if requested & _POWER_KEYS:
            response = await self._request("EnergyParameter")
            if response is not None:
                storage = response.get("Storage_list")
                if isinstance(storage, list):
                    self._has_storage = bool(storage)
                summary = response.get("SSumInfoList")
                summary = summary if isinstance(summary, dict) else {}
                storage = storage if isinstance(storage, list) else []

                soc = _number(summary.get("AverageBatteryAverageSOC"))
                if soc is None:
                    values = [
                        n for unit in storage
                        if isinstance(unit, dict)
                        if (n := _number(unit.get("BatterySoc"))) is not None
                    ]
                    if values:
                        soc = sum(values) / len(values)
                if soc is not None and "battery_soc" in requested:
                    snapshot["battery_soc"] = soc

                charge_values = [
                    n * 0.1 for unit in storage
                    if isinstance(unit, dict)
                    if (n := _number(unit.get("BatteryChargingPower"))) is not None
                ]
                discharge = _number(summary.get("TotalBatteryOutputPower"))
                if discharge is None:
                    discharge_values = [
                        n * 0.1 for unit in storage
                        if isinstance(unit, dict)
                        if (n := _number(unit.get("BatteryDischargingPower"))) is not None
                    ]
                    if discharge_values:
                        discharge = sum(discharge_values)
                if "battery_power" in requested and (charge_values or discharge is not None):
                    snapshot["battery_power"] = sum(charge_values) - (discharge or 0.0)

                pv = _number(summary.get("TotalPVPower"))
                if pv is not None and "solar_power" in requested:
                    snapshot["solar_power"] = pv

                grid_output = _number(summary.get("TotalGridOutputPower"))
                backup = _number(summary.get("TotalBackUpPower"))
                ac_delivered = None
                if (
                    pv is not None
                    and grid_output is not None
                    and backup is not None
                    and (
                        DELIVERED_AC_POWER_KEY in requested
                        or "battery_power" in requested
                    )
                ):
                    ac_delivered = pv - grid_output - (backup * 10)
                    if DELIVERED_AC_POWER_KEY in requested:
                        snapshot[DELIVERED_AC_POWER_KEY] = ac_delivered

                if (
                    "battery_power" in requested
                    and not charge_values
                    and snapshot.get("battery_power", 0.0) == 0.0
                    and ac_delivered
                ):
                    # AECC can omit per-battery charging power while still
                    # reporting AC flow. Use that AC-side estimate only when
                    # no per-battery charge reading exists; conversion losses
                    # mean it can differ from power reaching the cells.
                    snapshot["battery_power"] = ac_delivered

        if requested & _CONTROL_KEYS:
            response = await self._request(
                "Energycontrolparameters",
                {"RegControlAddr": _CONTROL_READ_REGISTERS},
            )
            params = (
                response.get("ControlInfo") or response.get("GetParameters") or {}
                if response
                else {}
            )
            if isinstance(params, dict):
                net_power = self._decode_slot(_register_value(params, _REG_CONTROL_TIME1))
                if net_power is not None:
                    snapshot["power_setpoint"] = net_power

        return snapshot

    async def apply_setpoint(
        self,
        net_power_w: int,
        *,
        mode_hint: Optional[str] = None,
        read_back: bool = True,
    ) -> SetpointResult:
        applied = max(
            -self._max_discharge_power_w,
            min(self._max_charge_power_w, int(net_power_w)),
        )
        slot = self._slot_for(applied)
        payload = {
            _REG_EMS_ENABLE: "1",
            _REG_SCHEDULE_MODE: "6",
            _REG_AI_SMART_CHARGE: "0",
            _REG_AI_SMART_DISCHARGE: "0",
            _REG_CUSTOM_MODE: "1",
            _REG_CONTROL_TIME1: slot,
        }
        response = await self._request(
            "Energycontrolparameters",
            {"SetControlInfo": payload},
            set_command=True,
        )
        if response is None:
            return SetpointResult(
                False, applied, False, failure_reason="setpoint_write_failed"
            )
        if not read_back:
            return SetpointResult(
                True,
                applied,
                False,
                applied={"power_setpoint": applied},
            )

        readback = await self._request(
            "Energycontrolparameters",
            {"RegControlAddr": _CONTROL_READ_REGISTERS},
        )
        params = (
            readback.get("ControlInfo") or readback.get("GetParameters") or {}
            if readback
            else {}
        )
        actual = (
            self._decode_slot(_register_value(params, _REG_CONTROL_TIME1))
            if isinstance(params, dict)
            else None
        )
        return SetpointResult(
            True,
            applied,
            actual == applied,
            applied={"power_setpoint": applied},
        )

    async def write_control(self, key: str, value: int) -> bool:
        return False

    def net_power_from_data(self, data: dict) -> Optional[int]:
        value = data.get("power_setpoint")
        return int(value) if value is not None else None

    async def apply_config(
        self,
        *,
        max_soc_pct: float,
        min_soc_pct: float,
        max_charge_power_w: int,
        max_discharge_power_w: int,
    ) -> bool:
        self._max_soc_pct = max(0, min(100, round(float(max_soc_pct))))
        self._min_soc_pct = max(0, min(100, round(float(min_soc_pct))))
        self._max_charge_power_w = max(0, min(int(max_charge_power_w), _MAX_POWER_W))
        self._max_discharge_power_w = max(0, min(int(max_discharge_power_w), _MAX_POWER_W))
        extended_limit = max(self._max_charge_power_w, self._max_discharge_power_w)
        if extended_limit <= _DEFAULT_POWER_LIMIT_W:
            return True
        response = await self._request(
            "Energycontrolparameters",
            {"SetControlInfo": {_REG_MAX_FEED_POWER: str(extended_limit)}},
            set_command=True,
        )
        return response is not None

    async def standby(self) -> bool:
        return (await self.apply_setpoint(0, read_back=False)).ok

    def _slot_for(self, net_power_w: int) -> str:
        if net_power_w == 0:
            return (
                f"0,00:00,00:00,0,0,0,0,0,0,"
                f"{self._max_soc_pct},{self._min_soc_pct}"
            )
        # AECC's register uses negative values for charging and positive for
        # discharging, opposite to Omnibattery's signed net-power convention.
        register_power = -net_power_w
        field7 = 5 if self._has_storage else 4
        return (
            f"1,00:00,23:59,{register_power},0,6,{field7},0,0,"
            f"{self._max_soc_pct},{self._min_soc_pct}"
        )

    @staticmethod
    def _decode_slot(value: Any) -> Optional[int]:
        if not isinstance(value, str):
            return None
        fields = value.split(",")
        if len(fields) < 4:
            return None
        try:
            return -int(float(fields[3]))
        except (TypeError, ValueError):
            return None

    async def _request(
        self,
        command: str,
        extra: dict | None = None,
        *,
        set_command: bool = False,
    ) -> Optional[dict]:
        async with self._io_lock:
            self._serial += 1
            serial = self._serial
            payload = {
                "Set" if set_command else "Get": command,
                "SerialNumber": serial,
                "CommandSource": "HA",
                **(extra or {}),
            }
            try:
                reader, writer = await self._get_reader_writer_unlocked()
                writer.write((json.dumps(payload) + "\n").encode("utf-8"))
                await writer.drain()
                response = await self._read_json(reader, serial)
                self._connected = True
                return response
            except (
                OSError,
                TimeoutError,
                asyncio.IncompleteReadError,
                UnicodeDecodeError,
                ValueError,
            ) as exc:
                self._connected = False
                if not self._shutting_down:
                    _LOGGER.warning(
                        "AECC TCP %s %s failed at %s:%s: %s",
                        "SET" if set_command else "GET",
                        command,
                        self.host,
                        self.port,
                        exc,
                    )
                await self._close_unlocked()
                return None

    async def _get_reader_writer_unlocked(
        self,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if (
            self._reader is not None
            and self._writer is not None
            and not self._writer.is_closing()
        ):
            return self._reader, self._writer
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=_CONNECT_TIMEOUT_S,
        )
        self._connected = True
        return self._reader, self._writer

    async def _read_json(
        self, reader: asyncio.StreamReader, expected_serial: int
    ) -> dict:
        if reader is not self._rx_reader:
            self._rx_reader = reader
            self._rx_buffer = b""
        decoder = json.JSONDecoder()
        async with asyncio.timeout(_REQUEST_TIMEOUT_S):
            while True:
                while self._rx_buffer:
                    text = self._rx_buffer.decode("utf-8").lstrip()
                    try:
                        response, end = decoder.raw_decode(text)
                    except json.JSONDecodeError:
                        break
                    self._rx_buffer = text[end:].encode("utf-8")
                    if not isinstance(response, dict):
                        continue
                    serial = response.get("SerialNumber")
                    if serial is not None and serial != expected_serial:
                        continue
                    return response
                if len(self._rx_buffer) > _MAX_RESPONSE_BYTES:
                    raise ValueError("AECC TCP response exceeded the size limit")
                chunk = await reader.read(4096)
                if not chunk:
                    raise ConnectionResetError("AECC battery closed the TCP connection")
                self._rx_buffer += chunk

    async def _close_unlocked(self) -> None:
        writer = self._writer
        self._reader = None
        self._writer = None
        self._rx_reader = None
        self._rx_buffer = b""
        if writer is not None and not writer.is_closing():
            writer.close()
            try:
                await writer.wait_closed()
            except OSError as exc:
                if not self._shutting_down:
                    _LOGGER.debug("AECC TCP close at %s:%s failed: %s", self.host, self.port, exc)
        self._connected = False

    @classmethod
    async def probe(cls, host: str, port: int = _DEFAULT_PORT) -> bool:
        driver = cls(host, port=port)
        try:
            return await driver.connect()
        finally:
            await driver.close()
