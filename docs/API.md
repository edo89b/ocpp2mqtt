# Interfaces: OCPP 1.6 and MQTT

The bridge exposes one WebSocket endpoint to chargers and a topic tree on
MQTT. `<prefix>` below is `MQTT_PREFIX` (`ocpp2mqtt` in `.env.example`),
`<id>` the charger id, `<n>` the OCPP connector id.

## WebSocket endpoint

- URL: `ws://<host>:<OCPP_PORT>/<id>`. The last path segment is the charger
  id; it becomes a topic level, so keep it free of `/`, `+` and `#`.
- Subprotocol offered: `ocpp1.6` (OCPP 1.6 JSON).
- No TLS, no HTTP authentication, no allow-list: any client that reaches the
  port can register as a charger. Keep it on a trusted network.
- Heartbeat interval requested from the charger: 30 s.

## OCPP 1.6 census

Rule: before writing code that handles a new OCPP action, sends a new call or
uses a new field, add it to these tables **in the same commit, first**.

### Charger to Central System

| # | Action | Reply from the bridge | Published |
|---|---|---|---|
| 1 | `BootNotification` | `Accepted`, current UTC time, interval 30 | `boot`; `firmware` when present; `status=Connected` |
| 2 | `Heartbeat` | current UTC time | `heartbeat` |
| 3 | `StatusNotification` | empty | `connector/<n>/status`, `connector/<n>/error` |
| 4 | `MeterValues` | empty | one `connector/<n>/meter/<key>` per sampled value |
| 5 | `StartTransaction` | transaction id `1`, tag `Accepted` | `connector/<n>/session/start` |
| 6 | `StopTransaction` | empty | `session/stop` |
| 7 | `Authorize` | tag `Accepted`, whatever the tag | nothing |
| 8 | any other action (`DataTransfer`, `FirmwareStatusNotification`, ...) | `NotImplemented` error, from the `ocpp` library | nothing |

Fields not listed in the MQTT tables below (for example the `reason` of
`StopTransaction`, or `context` and `location` of a sampled value) are
received and dropped.

### Central System to charger

Sent only when an MQTT command arrives (see [Commands](#commands)).

| # | Command | OCPP call | Values sent | Possible `status` |
|---|---|---|---|---|
| 1 | `start` | `RemoteStartTransaction` | `idTag`, `connectorId` | `Accepted`, `Rejected` |
| 2 | `stop` | `RemoteStopTransaction` | the tracked transaction id | `Accepted`, `Rejected` |
| 3 | `set_limit` | `SetChargingProfile` | profile id 1, stack level 0, `TxDefaultProfile`, `Relative`, unit `A`, one period from 0 s | `Accepted`, `Rejected`, `NotSupported` |
| 4 | `get_configuration` | `GetConfiguration` | key list, or none for all | (key/value map) |
| 5 | `change_configuration` | `ChangeConfiguration` | `key`, `value` as a string | `Accepted`, `Rejected`, `RebootRequired`, `NotSupported` |
| 6 | `trigger` | `TriggerMessage` | `requestedMessage`, `connectorId` | `Accepted`, `Rejected`, `NotImplemented` |
| 7 | `reset` | `Reset` | `Soft` or `Hard` | `Accepted`, `Rejected` |
| 8 | `unlock` | `UnlockConnector` | `connectorId` | `Unlocked`, `UnlockFailed`, `NotSupported` |
| 9 | `change_availability` | `ChangeAvailability` | `connectorId`, `Operative` or `Inoperative` | `Accepted`, `Rejected`, `Scheduled` |

`Scheduled` means the charger will apply the change when the running
transaction ends. The `set_limit` value is in amperes per phase, as OCPP 1.6
defines limits in `A`, and needs a charger that supports smart charging.

## MQTT topics published

All QoS 0.

| # | Topic under `<prefix>` | Payload | Retained | When |
|---|---|---|---|---|
| 1 | `bridge/status` | `online` / `offline` | yes | `online` on every MQTT connect; `offline` as LWT and on shutdown |
| 2 | `<id>/status` | `Connected` / `Disconnected` | yes | Socket open, `BootNotification`, socket close; `Disconnected` on MQTT connect for expected chargers not connected |
| 3 | `<id>/last_connected` | ISO 8601, UTC | yes | Socket open |
| 4 | `<id>/last_disconnected` | ISO 8601, UTC | yes | Socket close |
| 5 | `<id>/disconnect_reason` | `normal_closure`, `connection_error_<code>`, `connection_error`, `unexpected_error` | yes | Socket close |
| 6 | `<id>/boot` | JSON: `vendor`, `model`, `firmware` | no | `BootNotification` |
| 7 | `<id>/firmware` | firmware version | yes | `BootNotification` carrying a version |
| 8 | `<id>/heartbeat` | ISO 8601, UTC, time of reception | no | Every `Heartbeat` |
| 9 | `<id>/connector/<n>/status` | `Available`, `Preparing`, `Charging`, `SuspendedEVSE`, `SuspendedEV`, `Finishing`, `Reserved`, `Unavailable`, `Faulted` | yes | `StatusNotification` |
| 10 | `<id>/connector/<n>/error` | OCPP error code, `NoError` when fine | yes | `StatusNotification` |
| 11 | `<id>/connector/<n>/meter/<key>` | JSON: `value` (string, as sent), `unit` (may be empty) | no | `MeterValues`; power and current re-published as `0.0` on disconnect |
| 12 | `<id>/connector/<n>/session/start` | JSON: `meter_start_wh`, `id_tag`, `timestamp` | no | `StartTransaction` |
| 13 | `<id>/session/stop` | JSON: `meter_stop_wh`, `timestamp` | no | `StopTransaction` (the OCPP message has no connector) |
| 14 | `<id>/cmd/<command>/response` | see [Commands](#commands) | no | When the charger answers |

`<id>/status` is only meaningful while `bridge/status` is `online`: if the
bridge dies without a teardown, the retained `Connected` stays.

### Meter keys

The key is the measurand with `.` and spaces turned into `_`, lower-cased,
plus `_<phase>` lower-cased when the sample has a phase. A sample without a
measurand is `Energy.Active.Import.Register`, the OCPP default. Keys produced
by a three-phase charger reporting `Energy.Active.Import.Register`,
`Power.Active.Import`, `Current.Import` and `Voltage`:

```text
energy_active_import_register
power_active_import
current_import_l1   current_import_l2   current_import_l3
voltage_l1-n        voltage_l2-n        voltage_l3-n
```

Periodic, clock-aligned and transaction samples of the same measurand land on
the same topic. At disconnect, keys containing `power_active_import` or
`current_import` that were seen on that connection are set to `0.0`.

## Commands

Publish a JSON object on `<prefix>/<id>/cmd/<command>`, **not retained**:
the bridge does not check the retain flag and re-subscribes on every
connection, so a retained command would run again each time. An empty or
invalid JSON payload is treated as `{}`, so every field takes its default.

| # | Command | Payload fields (default) | Response payload on `cmd/<command>/response` |
|---|---|---|---|
| 1 | `start` | `connector_id` (1), `tag` (`FREE`) | status |
| 2 | `stop` | none | status, or `NoActiveTransaction` without calling the charger |
| 3 | `set_limit` | `amps` (16, clamped to 6–32), `connector_id` (0) | JSON: `amps` (after clamping), `status` |
| 4 | `get_configuration` | `keys` (all keys) | JSON: key to value |
| 5 | `change_configuration` | `key`, `value` (both required) | JSON: `key`, `value`, `status` |
| 6 | `trigger` | `message` (`StatusNotification`), `connector_id` (1) | JSON: `message`, `status` |
| 7 | `reset` | `type` (`Soft`; any other value is a hard reset) | status |
| 8 | `unlock` | `connector_id` (1) | status |
| 9 | `change_availability` | `available` (true), `connector_id` (0) | JSON: `available`, `status` |

Behaviour to rely on:

1. A command for an id that is not connected is dropped with a warning in
   the log; nothing is published.
2. Commands to one charger run one at a time; each waits up to 30 s for the
   answer.
3. No `.../response` after 30 s means the command failed: charger timeout,
   OCPP CallError (look for `Received a CALLError` in the log), a payload that
   is not a JSON object, or `change_configuration` without `key`/`value`.
4. `available` must be a JSON boolean: the string `"false"` counts as true.
5. `stop` only knows a session whose `StartTransaction` arrived on the
   current connection; after a bridge restart or a charger reconnection it
   answers `NoActiveTransaction`.
6. `change_configuration` persists on the charger; `RebootRequired` needs a
   `reset`. The README has a worked example of a first-time configuration.
7. A `set_limit` answered `Accepted` means the charger took the profile: check
   the effect on the current and power meter topics.

Example:

```bash
mosquitto_pub -h <broker> -u <user> -P '<password>' \
  -t '<prefix>/<id>/cmd/set_limit' -m '{"amps": 10}'
```
