# ocpp2mqtt

A lightweight **OCPP 1.6 Central System (CSMS)** that bridges EV chargers to
**MQTT**. Every message a charger sends (status, meter values, transactions…)
is published as MQTT topics, and every charger command (start/stop charging,
set current limit, reset…) can be triggered by publishing to an MQTT topic.

Built for home / self-hosted setups (e.g. integrating a wallbox into
openHAB, Home Assistant, Node-RED, …) where you want a single, hackable MQTT
interface to your charger.

## How it works

```
   EV charger  ──WebSocket (ocpp1.6)──►  ocpp2mqtt   ──MQTT──►  broker  ──►  your automation
                ◄───── commands ───────              ◄──MQTT───            (openHAB / HA / …)
```

- The charger connects to `ws://<host>:9000/<charger_id>` using subprotocol `ocpp1.6`.
- Incoming OCPP messages are published under `ocpp2mqtt/<charger_id>/...`.
- Commands are sent by publishing to `ocpp2mqtt/<charger_id>/cmd/<command>`.

## Requirements

- Docker + Docker Compose
- An MQTT broker (e.g. [Eclipse Mosquitto](https://mosquitto.org/))
- An OCPP 1.6 capable charger reachable on the network

## Quick start

```bash
git clone <this-repo>
cd ocpp2mqtt
cp .env.example .env      # then edit .env with your broker host / credentials
docker compose up -d
docker compose logs -f
```

Point your charger's OCPP backend URL to:

```
ws://<server-ip>:9000/<charger_id>
```

`<charger_id>` is free-form; it becomes the MQTT topic namespace for that charger.

## Configuration

All configuration is via environment variables (see `.env.example`):

| Variable      | Default      | Description                              |
|---------------|--------------|------------------------------------------|
| `MQTT_HOST`   | `mosquitto`  | MQTT broker hostname / IP                |
| `MQTT_PORT`   | `1883`       | MQTT broker port                         |
| `MQTT_USER`   | *(empty)*    | MQTT username (omit for anonymous)       |
| `MQTT_PASS`   | *(empty)*    | MQTT password                            |
| `MQTT_PREFIX` | `ocpp2mqtt`  | Root topic prefix                        |
| `OCPP_PORT`   | `9000`       | WebSocket listen port for chargers       |
| `EXPECTED_CHARGERS` | *(empty)* | Comma-separated charger IDs; their `status` is pre-published as `Disconnected` (retained) on startup |
| `LOG_LEVEL`   | `INFO`       | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |

`OCPP_IPV4` / `OCPP_IPV6` in `.env` are only used by the example
`docker-compose.yml` to assign static addresses on a macvlan network — adapt or
remove the `networks:` section to match your own setup.

## MQTT topics

### Published by the bridge (charger → MQTT)

| Topic                                                  | Retained | Payload                              |
|--------------------------------------------------------|----------|--------------------------------------|
| `ocpp2mqtt/bridge/status`                                   | yes      | `online` / `offline` (LWT)           |
| `ocpp2mqtt/<id>/status`                                     | yes      | `Connected` / `Disconnected`         |
| `ocpp2mqtt/<id>/last_connected`                             | yes      | ISO-8601 timestamp of last connect   |
| `ocpp2mqtt/<id>/last_disconnected`                          | yes      | ISO-8601 timestamp of last disconnect|
| `ocpp2mqtt/<id>/disconnect_reason`                          | yes      | `normal_closure` / `connection_error_<code>` / `unexpected_error` |
| `ocpp2mqtt/<id>/boot`                                       | no       | `{"vendor":..., "model":..., "firmware":...}` |
| `ocpp2mqtt/<id>/firmware`                                   | yes      | charger firmware version             |
| `ocpp2mqtt/<id>/heartbeat`                                  | no       | ISO-8601 timestamp                   |
| `ocpp2mqtt/<id>/connector/<n>/status`                       | yes      | OCPP status (e.g. `Charging`)        |
| `ocpp2mqtt/<id>/connector/<n>/error`                        | yes      | OCPP error code                      |
| `ocpp2mqtt/<id>/connector/<n>/meter/<measurand>`            | no       | `{"value":..., "unit":...}`          |
| `ocpp2mqtt/<id>/connector/<n>/session/start`                | no       | `{"meter_start_wh":..., "id_tag":...}`|
| `ocpp2mqtt/<id>/session/stop`                               | no       | `{"meter_stop_wh":..., ...}`         |
| `ocpp2mqtt/<id>/cmd/<command>/response`                     | no       | command result                       |

> On disconnect, the instantaneous meters (`power_active_import`, `current_import*`)
> are republished as `0` so dashboards don't show stale "still charging" values.
> Cumulative energy and voltages keep their last value.

### Commands (MQTT → charger)

Publish to `ocpp2mqtt/<id>/cmd/<command>`:

| Command                | Payload example                                                            |
|------------------------|----------------------------------------------------------------------------|
| `start`                | `{"connector_id": 1, "tag": "FREE"}`                                       |
| `stop`                 | `{}`                                                                       |
| `set_limit`            | `{"amps": 16}` (clamped to 6–32 A)                                         |
| `unlock`               | `{"connector_id": 1}`                                                      |
| `reset`                | `{"type": "Soft"}` or `{"type": "Hard"}`                                   |
| `change_availability`  | `{"available": false, "connector_id": 0}` (false = block charging)         |
| `trigger`              | `{"message": "StatusNotification", "connector_id": 1}`                     |
| `get_configuration`    | `{}` or `{"keys": ["MeterValueSampleInterval"]}`                           |
| `change_configuration` | `{"key": "MeterValueSampleInterval", "value": "15"}`                       |

Example — set the charging current to 16 A:

```bash
mosquitto_pub -h <broker> -u <user> -P <pass> \
  -t 'ocpp2mqtt/my_wallbox/cmd/set_limit' -m '{"amps": 16}'
```

### Worked example — first-time charger setup

The following sequence configures meter-value sampling and disables
authentication on a real charger (here `my_wallbox`). Adjust the topic
namespace to your own `<charger_id>`.

```bash
# Read which configuration keys the charger supports
#   → response on ocpp2mqtt/my_wallbox/get_configuration/response
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/get_configuration' -m '{}'

# Choose which measurands are reported on each sample / aligned interval
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"MeterValuesSampledData","value":"Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage"}'
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"MeterValuesAlignedData","value":"Energy.Active.Import.Register,Power.Active.Import,Current.Import,Voltage"}'

# Sampling cadence (seconds)
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"MeterValueSampleInterval","value":"15"}'
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"ClockAlignedDataInterval","value":"15"}'

# Keep buffering meter values while the charger is offline
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"MeterValuesSkipWhenOffline","value":"true"}'

# Disable RFID authentication (free charging on a trusted network)
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/change_configuration' \
  -m '{"key":"AuthEnabled","value":"false"}'

# Charging control
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/start'     -m '{"connector_id": 1}'
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/unlock'    -m '{"connector_id": 1}'
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/stop'      -m '{}'
mosquitto_pub -t 'ocpp2mqtt/my_wallbox/cmd/set_limit' -m '{"amps": 16}'
```

## ⚠️ Security notes

This bridge is designed to run on a **trusted local network**. By default:

- **The WebSocket endpoint is not authenticated** — any host that can reach
  `:9000` can register as a charger.
- **`Authorize` and `StartTransaction` accept any RFID tag** — there is no
  access control on who may charge.

Do **not** expose port `9000` to the internet. Put it behind a VPN / firewall,
and rely on your MQTT broker's authentication for the control side.

## Tech stack

- [`ocpp`](https://github.com/mobilityhouse/ocpp) (The Mobility House, MIT) — OCPP 1.6 protocol
- [`websockets`](https://github.com/python-websockets/websockets) — WebSocket server
- [`paho-mqtt`](https://github.com/eclipse/paho.mqtt.python) — MQTT client

## License

[MIT](LICENSE) © Edoardo Barbano
