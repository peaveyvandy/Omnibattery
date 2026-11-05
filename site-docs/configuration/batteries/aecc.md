# AECC local TCP batteries

Omnibattery can control a Sunpura S2400 and compatible AECC-protocol batteries over the device's local TCP interface. No cloud account or separate Home Assistant integration is required.

## Before you start

- Give the battery a stable local IP address and confirm Home Assistant can reach it.
- Make sure no other client is connected to its local TCP port. The device may allow only one client at a time; disconnect the AECC Battery (Local TCP) integration or other TCP clients before using Omnibattery.
- Know the battery's nominal capacity. The protocol does not provide a verified capacity value to Omnibattery.
- Keep the connection on a trusted local network.

## Add it

1. In the Omnibattery setup flow, choose **AECC local TCP (Sunpura S2400)**.
2. Enter the device's name, IP address, TCP port (default `8080`), manufacturer, and model.
3. Keep the charge and discharge limits at their safe `800 W` defaults unless you have confirmed the specific unit supports more. The S2400 limit is `2,400 W` in either direction.
4. Enter nominal capacity and complete the common SOC and power limits.

## Telemetry and controls

Omnibattery reads SOC, battery power, and aggregate solar power from the AECC energy report. The AC-side delivery value is derived only when the report includes the required simultaneous power fields. The driver reports a signed power setpoint and verifies it against the control-slot readback when requested.

SOC limits are enforced by Omnibattery in software. The driver does not claim hardware SOC cutoffs, native cumulative energy counters, alarm registers, or independently addressable battery packs. Energy and cycle totals therefore use Omnibattery's synthetic counters, and nominal capacity is required for stored-energy calculations.

The signed control-slot readback confirms that the requested setpoint was accepted; it does not mean measured battery power has already settled. On one S2400 using the separate AECC integration, battery-power telemetry fell from about 125 W to 3 W around 20 seconds after an Idle command, while AC-charging telemetry reported zero around 40 seconds after the command. Omnibattery therefore allows 25 seconds for actuation and 45 seconds for settled telemetry/engagement. These are conservative observations from the existing integration, not yet a timing validation of Omnibattery's direct driver.

Automatic control uses the device's local signed schedule slot. The driver serializes requests on its persistent connection and closes it on unload, but a second integration or another Omnibattery configuration pointed at the same endpoint can compete for the device's single TCP client slot. Treat one endpoint as one controllable battery; commands to a master may not be forwarded to secondary packs.

## Validation status

The TCP framing, telemetry fields, and setpoint format are based on the [AECC Battery (Local TCP) integration](https://github.com/StekkerDeal/aecc-battery-local). Deterministic tests cover the protocol and the distinction between register acknowledgement and measured-power settling. The timing observations above came from the separate integration; Omnibattery's direct TCP driver still needs live validation for control acknowledgement, idle behavior, power direction, response timing, and single-client behavior before relying on automatic control.

For general limits and controls, see [Choose your battery connection](index.md).
