# Setup

From a clean clone to a charger publishing on MQTT. Topics and commands are
in [API.md](API.md); the README has a worked example of a first-time charger
configuration.

## Prerequisites

| # | What | Why |
|---|---|---|
| 1 | Docker Engine with Compose v2 (`docker compose`) | The image is built locally from the `Dockerfile` |
| 2 | An MQTT broker reachable from the container | Output and command input |
| 3 | An OCPP 1.6 JSON charger whose backend URL you can set | Input |
| 4 | A network segment you trust between charger and bridge | The WebSocket has no authentication |
| 5 | Optional: a dedicated MQTT account for the bridge | Broker access control |

## Install

1. Clone and create the settings file:

   ```bash
   git clone https://github.com/edo89b/ocpp2mqtt.git
   cd ocpp2mqtt
   cp .env.example .env && chmod 600 .env
   ```

2. Fill in `.env`. `MQTT_HOST`, `MQTT_PORT`, `MQTT_PREFIX` and `OCPP_PORT`
   must all have a value: `docker-compose.yml` passes them explicitly, so an
   unset one reaches the code as an empty string instead of the code
   default. Leave `MQTT_USER`/`MQTT_PASS` empty only for an anonymous broker.
3. Adapt the networks (next section).
4. Build and start:

   ```bash
   docker compose up -d --build
   ```

5. On the charger, set the OCPP backend (Central System) URL to
   `ws://<address>:<OCPP_PORT>/<id>`, where `<id>` is the name you want in
   the topics (one level, no `/`).

## Networking

The tracked `docker-compose.yml` reflects the installation it was written
for:

- it attaches the container to two existing networks declared `external:
  true` at the bottom of the file: a charger-facing network where the
  container gets the static addresses `OCPP_IPV4`/`OCPP_IPV6` (a macvlan, so
  the container has its own LAN address), and the broker's internal network;
- it publishes no port: chargers reach the container at its own address.

Pick one of these:

1. **Same layout.** Create a macvlan for the chargers and a network shared
   with the broker, then set their names in the `networks:` sections and the
   addresses in `.env`.
2. **Plain bridge.** Remove both `networks:` sections and add a `ports:`
   mapping for `OCPP_PORT` to the service; point the charger at the host
   address. `OCPP_IPV4`/`OCPP_IPV6` are then unused.

Notes:

- On a container attached to several networks, point `MQTT_HOST` at a name
  that resolves only on the broker network, so MQTT traffic does not leave
  through the charger network.
- Your edits to the tracked compose file stay local: `git pull` refuses to
  overwrite a locally modified file, so carry them across updates by hand.
- Never expose `OCPP_PORT` to the internet.

## Verify the installation

1. The container runs and received the expected variables (names only):

   ```bash
   docker compose ps
   docker inspect ocpp2mqtt --format '{{range .Config.Env}}{{println .}}{{end}}' | sed 's/=.*//'
   ```

   The list shows the six variables of `docker-compose.yml`, not
   `EXPECTED_CHARGERS` or `LOG_LEVEL` (trap 1 below).

2. The log shows the bridge ready and the charger arriving:

   ```text
   [INFO] ocpp2mqtt - MQTT connecting to <MQTT_HOST>:<MQTT_PORT>
   [INFO] ocpp2mqtt - OCPP Central System listening on ws://0.0.0.0:<OCPP_PORT>
   [INFO] ocpp2mqtt - MQTT (re)connected (rc=0)
   [INFO] ocpp2mqtt - New WebSocket connection: charger_id=<id> path=/<id>
   [INFO] ocpp2mqtt - [<id>] BootNotification: <vendor> <model> (fw: <version>)
   ```

   ```bash
   docker compose logs --tail 100 ocpp2mqtt
   ```

3. The topics arrive: `bridge/status` is `online`, `<id>/status` is
   `Connected`, `heartbeat` changes every 30 s, meter topics appear once the
   charger samples.

   ```bash
   mosquitto_sub -h <broker> -u <user> -P '<password>' -t '<MQTT_PREFIX>/#' -v
   ```

4. A command round trip works. `get_configuration` only reads, so it is a
   safe first command; the answer arrives on
   `<MQTT_PREFIX>/<id>/cmd/get_configuration/response`.

   ```bash
   mosquitto_pub -h <broker> -u <user> -P '<password>' -t '<MQTT_PREFIX>/<id>/cmd/get_configuration' -m '{}'
   ```

## Troubleshooting

| # | Symptom | Cause | Remedy |
|---|---|---|---|
| 1 | `MQTT connection refused (rc=<n>)` | Credentials or ACL rejected by the broker | Fix `.env`, then `docker compose up -d` |
| 2 | Container restarting, `ValueError: invalid literal for int()` in the log | `MQTT_PORT` or `OCPP_PORT` empty in `.env` | Set both |
| 3 | Topics start with `/` | `MQTT_PREFIX` empty in `.env` | Set it |
| 4 | No `New WebSocket connection` line | The charger cannot reach the address or port, or its URL is wrong | Check the networking choice and the backend URL on the charger |
| 5 | `Command for a charger that is not connected: <id>` | The id in the topic differs from the one in the charger URL, or the charger is offline | Compare with the `<id>/status` topics |
| 6 | `Unknown command: <name>` | Typo in the command topic | See the command list in [API.md](API.md#commands) |
| 7 | No `.../response` after a command | Charger timeout, CallError, or malformed payload | See rule 3 in [API.md](API.md#commands) |
| 8 | `[<id>] Abrupt disconnect (no close frame)`, `disconnect_reason` `connection_error_1006` | The charger dropped the socket without closing it | None: it reconnects on its own; power and current read `0.0` meanwhile |
| 9 | `stop` answers `NoActiveTransaction` during a session | The session started before the current connection or before a bridge restart | Known limit: end the session at the charger or from the vehicle |

## Operational traps

One entry per failure that actually happened.

1. **`EXPECTED_CHARGERS` and `LOG_LEVEL` are silently ignored.** They are
   set in `.env` but missing from the `environment:` list of
   `docker-compose.yml`, so the container never receives them: expected
   chargers are not pre-published as `Disconnected` and the level stays
   INFO. Check with step 1 of the verification. Until the compose file lists
   them, add `EXPECTED_CHARGERS` and `LOG_LEVEL` to that list in your copy.
2. **The container log covers less than a day.** The `ocpp` library logs
   every OCPP frame at INFO; with a charger sending meter values every few
   seconds, the 3 x 10 MB rotation of `docker-compose.yml` held only about 19
   hours (29,000 lines). For history, rely on the MQTT consumer's
   persistence. Once `LOG_LEVEL` reaches the container, `WARNING` removes the
   frame trace, together with the bridge's own INFO lines.
