# ocpp2mqtt

Lightweight **OCPP 1.6 Central System** (CSMS) that bridges EV chargers to
**MQTT**. Chargers connect over WebSocket; everything they report is
published as MQTT topics, and commands published on MQTT are sent to the
charger as OCPP calls. Built for self-hosted home setups on a trusted
network: no charger authentication, no billing. Public open-source project,
MIT licensed.

## Tech Stack

- Python 3.11 (`python:3.11-slim` base image), a single module:
  `central_system.py`, `asyncio` for the OCPP side
- `ocpp` 1.0.0 (The Mobility House): OCPP 1.6 JSON routing, payload classes,
  JSON-schema validation
- `websockets` 12.0: WebSocket server, legacy handler signature
  `(websocket, path)`
- `paho-mqtt` 1.6.1: MQTT 3.1.1 client, version 1 callback API, network loop
  in its own thread
- Docker Compose v2: one service, built locally from the `Dockerfile`
- No test suite, no CI

## Directory Structure

```
ocpp2mqtt/
├── central_system.py   - the whole bridge: OCPP server, per-charger handler, MQTT client, command dispatcher
├── Dockerfile          - pinned requirements + central_system.py, runs `python -u`
├── docker-compose.yml  - service `ocpp2mqtt`, explicit `environment:` list, two external networks
├── requirements.txt    - exact pins of the three runtime dependencies
├── .env.example        - settings template (tracked)
├── .env                - real settings and the MQTT password (gitignored)
├── docs/               - reference documentation (see Related Documents)
├── scripts/
│   └── doc_check.py    - documentation staleness check
├── .githooks/
│   └── pre-commit      - runs the check before every commit
├── README.md           - user-facing overview, topics, commands, security notes
└── LICENSE             - MIT
```

## Setup & Commands

```bash
cp .env.example .env && chmod 600 .env      # then fill it in: docs/SETUP.md
docker compose up -d --build                # build and start; also after any code or .env change
docker compose logs -f ocpp2mqtt            # follow the log
docker compose down                         # stop and remove the container
python3 -m py_compile central_system.py     # syntax check, needs no dependency
python3 scripts/doc_check.py                # documentation check (-v lists the documents)
```

- `docker-compose.yml` declares two `external: true` networks named after the
  original installation: create them, or adapt the `networks:` section, before
  the first `up` ([docs/SETUP.md](docs/SETUP.md#networking)).
- `docker compose restart` does not re-read `.env`: use `docker compose up -d`.
- A change is verified with a real charger or an OCPP 1.6 simulator pointed
  at the bridge ([docs/SETUP.md](docs/SETUP.md#verify-the-installation)): a
  charger talks to one Central System at a time.

## Coding Conventions

Naming and structure:

- One module, in this order: configuration, `MyChargePoint` (handlers, then
  commands), MQTT callbacks, WebSocket handler, `main()`. Sections are
  separated by `# ── Title ───` rulers.
- Configuration is read once with `os.getenv(NAME, default)` into aligned
  UPPER_CASE constants. Every new variable is added in the same commit to the
  `environment:` list of `docker-compose.yml`, to `.env.example` and to the
  README table: the compose file passes only what it lists (see the debt below).
- OCPP handlers are `on_<action>` methods decorated with `@on(Action.X)`,
  taking the snake_case fields the `ocpp` library produces and returning the
  matching `call_result.X`.
- Commands are `async` methods of `MyChargePoint` that send `call.X(...)`
  with `self.call()` and publish the charger's answer on
  `cmd/<command>/response`. A new command needs a method, a branch in
  `dispatch()` and a row in [docs/API.md](docs/API.md#commands).
- Per-charger MQTT output goes through `self.pub()` (dicts become JSON,
  everything else a string). State is retained; telemetry, events and
  command responses are not.
- Logging through `logger = logging.getLogger("ocpp2mqtt")`; per-charger
  lines start with `[<charger_id>]`.
- Type hints use builtin generics and `X | None`: Python 3.10 is the minimum.
- Comments and log text in English.

Patterns that must stay:

- Never touch a charger or the WebSocket from paho's thread:
  `on_mqtt_message()` hands the coroutine to the event loop with
  `asyncio.run_coroutine_threadsafe(..., loop)`.
- `on_mqtt_connect()` republishes `bridge/status` `online` (overriding the
  retained LWT), the `Disconnected` state of expected chargers, and
  re-subscribes `cmd/#` of every connected charger: subscriptions do not
  survive a broker reconnection.
- On disconnect the handler deregisters the charger, unsubscribes its
  commands, zeroes instantaneous power and current, and publishes the
  disconnect state, reason and time.
- `set_charging_limit()` clamps to 6–32 A (6 A is the IEC 61851 minimum), except
  `amps = 0`, which pauses the charge with a 0 A limit and keeps the transaction
  open. `purpose="tx"` swaps the `TxDefaultProfile` for a `TxProfile` bound to the
  running transaction (profile id 2, stack level 1, connector 1).

Anti-patterns (never acceptable):

- Blocking calls inside the event loop, or OCPP calls from a paho callback.
- Retaining a command topic or a telemetry topic.
- Exposing the OCPP port beyond a trusted network, or presenting the bridge
  as secure: there is no charger authentication and every RFID tag is
  accepted, by design and as the README states.
- Bumping `websockets` or `paho-mqtt` across a major version without porting:
  later majors changed the server handler signature and the paho `Client`
  constructor (callback API version).
- Deployment detail (addresses, network names, account names) added to a
  tracked file.

Known technical debt (documented, not fixed yet):

- `docker-compose.yml` passes only six variables. `EXPECTED_CHARGERS` and
  `LOG_LEVEL` in `.env` never reach the container, and the defaults written in
  the code never apply: an unset `MQTT_PORT` or `OCPP_PORT` reaches the code
  as an empty string and `int()` fails at startup; an empty `MQTT_PREFIX`
  gives topics starting with `/`.
- A failing command leaves no trace from the bridge: the future returned by
  `run_coroutine_threadsafe` is never inspected, so a charger timeout (30 s),
  an OCPP CallError (the library returns `None`) or a malformed payload ends
  without a `.../response` topic and without a bridge log line.
- Reconnection race: if a charger opens a new WebSocket before the old one is
  detected as closed, the old handler's `finally` removes the new connection
  from `connected_chargers`, unsubscribes its commands and publishes
  `Disconnected`.
- The transaction id is always `1` and lives in memory: after a bridge
  restart or a charger reconnection during a session, `stop` answers
  `NoActiveTransaction`.
- The retain flag of incoming commands is not checked, and `cmd/#` is
  re-subscribed on every connection: a retained command runs again each time.
- `MQTT_PREFIX` must be a single topic level: the dispatcher reads the
  charger id from the second level.
- The `ocpp` library logs every OCPP frame at INFO; with the 3 x 10 MB log
  rotation a chatty charger fills the log in under a day.
- `.gitignore` ignores `.env*`, which also matches the tracked `.env.example`:
  re-adding it needs `git add -f`.
- The tracked `docker-compose.yml` carries the original installation's
  network names and a comment in Italian, and commit `f48e012` has an Italian
  message: both against this repository's English convention.

## Git Workflow and Operating Rules

- Remote: `origin` on GitHub, public. History is linear on `main`: no other
  branches, no merge commits, no tags.
- Commit messages in English: short capitalised subject, imperative mood, no
  trailing period; the body explains why, wrapped at about 72 columns.
- Never commit `.env` or any `.env*` copy (only `.env.example` is tracked),
  `*.bak`, `*.bak-*`: all gitignored, because backups of `.env` hold the same
  credentials. The published history contains no secret; a secret that
  reaches GitHub even once is rotated on the broker, not just removed.
- No environments and no registry: a "release" is a push to `main`; each
  installation updates with `git pull` then `docker compose up -d --build`.
  Local edits to the tracked `docker-compose.yml` (networks) have to be
  carried across pulls by hand.
- Never send a command to a charger you are not allowed to operate. `reset`
  reboots it (anything but `Soft` is a hard reset), `change_availability`
  with `false` blocks charging, `unlock` releases the cable, and
  `change_configuration` persists on the charger across reboots.
- Documentation and code stay consistent: a change that makes a sentence of
  the documentation false fixes it in the same commit. `scripts/doc_check.py`
  catches broken references (names, paths, services that no longer exist), not
  descriptions that became false. When it fails, fix the document, do not
  silence the check; the `<!-- doc-check:ignore -->` markers are only for
  historical quotes and things not built yet.
- The check runs as a pre-commit hook. Enable it once per clone:

  ```bash
  git config core.hooksPath .githooks
  ```

  Run it by hand with `python3 scripts/doc_check.py`.

## Key Files & Directories

- `central_system.py`: everything; the module docstring has the topic scheme
  and the threading model.
- `docker-compose.yml`: which variables reach the container, networks,
  restart policy `unless-stopped`, log rotation.
- `.env.example`: settings template; `.env` holds the real values.
- `Dockerfile`, `requirements.txt`: image; `.dockerignore` keeps `.env`, the
  compose file and the Markdown files out of the build context.
- Tests: none. Documentation: `README.md` (users) and `docs/` (reference).

## Related Documents

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): components, threading model,
  connection lifecycle, failure behaviour, design decisions
- [docs/SETUP.md](docs/SETUP.md): installation, networking, charger
  configuration, verification, troubleshooting, operational traps
- [docs/API.md](docs/API.md): WebSocket endpoint, OCPP 1.6 message census,
  MQTT topics and commands
- [README.md](README.md): overview, worked example, security notes

## Tools and Integrations

- **EV chargers (OCPP 1.6 JSON)**: `ws://<host>:<OCPP_PORT>/<charger_id>`,
  subprotocol `ocpp1.6`, no TLS, no authentication. Message census:
  [docs/API.md](docs/API.md#ocpp-16-census).
- **MQTT broker**: any; username/password optional. Give the bridge its own
  broker account; the MQTT client id is fixed to `ocpp2mqtt`, so two bridges
  on one broker disconnect each other.
- **Consumers** (openHAB, Home Assistant, Node-RED, ...): plain MQTT, no
  discovery.

## Environment Variables and Secrets

| # | Variable | Read by | Reaches the container | Notes |
|---|---|---|---|---|
| 1 | `MQTT_HOST` | code | yes | Broker hostname or IP |
| 2 | `MQTT_PORT` | code | yes | Must be set: parsed with `int()` |
| 3 | `MQTT_USER` | code | yes | Empty for an anonymous broker |
| 4 | `MQTT_PASS` | code | yes | The only secret |
| 5 | `MQTT_PREFIX` | code | yes | Root topic, one level only |
| 6 | `OCPP_PORT` | code | yes | Must be set: parsed with `int()` |
| 7 | `EXPECTED_CHARGERS` | code | **no** | Not listed in `docker-compose.yml` |
| 8 | `LOG_LEVEL` | code | **no** | Not listed in `docker-compose.yml`; INFO applies |
| 9 | `OCPP_IPV4`, `OCPP_IPV6` | `docker-compose.yml` | not needed | Static addresses on the charger-facing network |

- `.env` is used by Compose for interpolation only (there is no `env_file`).
  Keep it `chmod 600`; the MQTT account comes from your broker.
- Excluded from git: `.env*` (the tracked `.env.example` excepted), `*.bak`,
  `*.bak-*`. Excluded from the image: `.env`, `.env.example`, the compose file.
- To list which variables really reached the container, without values:

  ```bash
  docker inspect ocpp2mqtt --format '{{range .Config.Env}}{{println .}}{{end}}' | sed 's/=.*//'
  ```
