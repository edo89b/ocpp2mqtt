"""ocpp2mqtt — a lightweight OCPP 1.6 Central System (CSMS) bridged to MQTT.

The bridge runs two concurrent pieces:

  * an **OCPP 1.6 WebSocket server** that EV chargers connect to
    (``ws://<host>:<OCPP_PORT>/<charger_id>``), handled with asyncio; and
  * an **MQTT client** (paho) that publishes everything a charger reports and
    relays commands coming back from MQTT.

Data flows in both directions:

  charger ──OCPP──▶ bridge ──MQTT publish──▶ broker ──▶ home automation
  charger ◀──OCPP── bridge ◀──MQTT subscribe── broker ◀── home automation

MQTT topic scheme (``<prefix>`` defaults to ``ocpp2mqtt``):

  <prefix>/bridge/status                         online/offline (LWT)
  <prefix>/<id>/status                           Connected/Disconnected
  <prefix>/<id>/last_connected | last_disconnected | disconnect_reason
  <prefix>/<id>/firmware | boot | heartbeat
  <prefix>/<id>/connector/<n>/status | error
  <prefix>/<id>/connector/<n>/meter/<measurand>  {"value":..,"unit":..}
  <prefix>/<id>/connector/<n>/session/start | <prefix>/<id>/session/stop
  <prefix>/<id>/cmd/<command>                     (subscribed) command input
  <prefix>/<id>/cmd/<command>/response           command result

Threading note: paho runs its network loop in its own background thread, while
the OCPP side lives in the asyncio event loop. The two never touch each other's
internals directly — MQTT commands are marshalled onto the event loop with
``asyncio.run_coroutine_threadsafe`` (see :func:`on_mqtt_message`).
"""

import asyncio
import json
import logging
import os
import signal
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import websockets
from ocpp.routing import on
from ocpp.v16 import ChargePoint as cp
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    Action,
    AuthorizationStatus,
    AvailabilityType,
    RegistrationStatus,
    ChargingProfilePurposeType,
    ChargingProfileKindType,
    ChargingRateUnitType,
    ResetType,
)

# ── Configuration from environment variables ───────────────────────────────────
MQTT_HOST   = os.getenv("MQTT_HOST", "mosquitto")
MQTT_PORT   = int(os.getenv("MQTT_PORT", 1883))
MQTT_USER   = os.getenv("MQTT_USER", "")
MQTT_PASS   = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "ocpp2mqtt")  # root of every MQTT topic
OCPP_PORT   = int(os.getenv("OCPP_PORT", 9000))      # WebSocket listen port
LOG_LEVEL   = os.getenv("LOG_LEVEL", "INFO").upper()
# Known chargers: at startup we publish their 'Disconnected' state (retained) so
# the integration (openHAB/HA) immediately shows the correct state even if the
# charger is not connected yet. Comma-separated list, e.g. "wlb_01,wlb_02".
EXPECTED_CHARGERS = [
    c.strip() for c in os.getenv("EXPECTED_CHARGERS", "").split(",") if c.strip()
]

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ocpp2mqtt")

# Single shared MQTT client for the whole process. `clean_session=True` means we
# don't want the broker to queue messages for us while disconnected.
mqttc: mqtt.Client = mqtt.Client(client_id="ocpp2mqtt", clean_session=True)
if MQTT_USER:
    mqttc.username_pw_set(MQTT_USER, MQTT_PASS)

# Registry of currently connected chargers, keyed by charger id. Populated by the
# WebSocket handler and read by the MQTT command dispatcher to route commands.
connected_chargers: dict[str, "MyChargePoint"] = {}
# The asyncio event loop, captured in main(); needed to schedule coroutines from
# the paho callback thread.
loop: asyncio.AbstractEventLoop = None


# ──────────────────────────────────────────────────────────────────────────────
class MyChargePoint(cp):
    """One instance per connected charger.

    Subclasses the ``ocpp`` library's ChargePoint: methods decorated with
    ``@on(Action.X)`` handle incoming OCPP messages, while the plain ``async``
    methods send commands to the charger via ``self.call(...)``.
    """

    def __init__(self, charge_point_id: str, websocket):
        super().__init__(charge_point_id, websocket)
        # Set on StartTransaction, cleared on StopTransaction; used to know
        # whether a RemoteStopTransaction is meaningful.
        self._current_transaction_id: int | None = None
        # Per-charger MQTT topic prefix, e.g. "ocpp2mqtt/wlb_01".
        self._prefix = f"{MQTT_PREFIX}/{charge_point_id}"
        # meter subtopic → last seen unit, used to reset power/current to 0 on
        # disconnect (avoids "frozen" values on the dashboard).
        self._meter_topics: dict[str, str] = {}

    # ── MQTT helper ────────────────────────────────────────────────────────────
    def pub(self, subtopic: str, payload, retain: bool = False):
        """Publish ``payload`` under ``<prefix>/<subtopic>``.

        dicts are serialised as JSON, everything else is stringified. ``retain``
        is used for state that should survive a subscriber (re)connect.
        """
        topic = f"{self._prefix}/{subtopic}"
        body  = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        mqttc.publish(topic, body, retain=retain)
        logger.debug("MQTT ← %s : %s", topic, body)

    # ── Handlers: Charger → CSMS ───────────────────────────────────────────────
    # Each handler must return the matching call_result; the ocpp library turns
    # that into the OCPP reply sent back to the charger.

    @on(Action.BootNotification)
    async def on_boot_notification(self, charge_point_model,
                                   charge_point_vendor, **kwargs):
        # Sent by the charger on power-up (and on demand via TriggerMessage). It
        # carries identity + firmware; we expose them and accept the charger.
        firmware = kwargs.get("firmware_version", "")
        logger.info("[%s] BootNotification: %s %s (fw: %s)",
                    self.id, charge_point_vendor, charge_point_model,
                    firmware or "n/a")
        self.pub("boot", {"vendor": charge_point_vendor,
                          "model": charge_point_model,
                          "firmware": firmware})
        if firmware:
            self.pub("firmware", firmware, retain=True)
        self.pub("status", "Connected", retain=True)
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).isoformat(),
            interval=30,  # seconds between heartbeats we ask the charger for
            status=RegistrationStatus.accepted,
        )

    @on(Action.Heartbeat)
    async def on_heartbeat(self):
        # Periodic keep-alive; we echo it to MQTT and answer with the wall clock.
        self.pub("heartbeat", datetime.now(timezone.utc).isoformat())
        return call_result.Heartbeat(
            current_time=datetime.now(timezone.utc).isoformat()
        )

    @on(Action.StatusNotification)
    async def on_status_notification(self, connector_id,
                                     error_code, status, **kwargs):
        # Per-connector availability/charging state (e.g. Available, Charging,
        # SuspendedEV) and any error code. Retained so the last state persists.
        logger.info("[%s] Status connector %s: %s (err: %s)",
                    self.id, connector_id, status, error_code)
        self.pub(f"connector/{connector_id}/status", status, retain=True)
        self.pub(f"connector/{connector_id}/error",  error_code, retain=True)
        return call_result.StatusNotification()

    @on(Action.MeterValues)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        # Telemetry samples (energy, power, current, voltage, ...). We flatten
        # each sampled value to its own topic so consumers can subscribe à la
        # carte; the measurand (+ optional phase) becomes the topic key.
        for mv in meter_value:
            for sv in mv.get("sampled_value", []):
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                value = sv.get("value", "0")
                unit  = sv.get("unit", "")
                phase = sv.get("phase", "")
                # "Energy.Active.Import.Register" + phase "L1" → energy_active_import_register_l1
                key   = measurand.replace(".", "_").replace(" ", "_").lower()
                if phase:
                    key = f"{key}_{phase.lower()}"
                subtopic = f"connector/{connector_id}/meter/{key}"
                # Remember the topic+unit so we can zero it out on disconnect.
                self._meter_topics[subtopic] = unit
                self.pub(subtopic, {"value": value, "unit": unit})
        return call_result.MeterValues()

    # ── Reset instantaneous meters on disconnect ───────────────────────────────
    def reset_instant_meters(self):
        """Reset power and currents to 0 when the charger disconnects, so the
        dashboard doesn't stay on 'frozen' values. Cumulative energy and voltages
        keep their last known value."""
        for subtopic, unit in self._meter_topics.items():
            if "power_active_import" in subtopic or "current_import" in subtopic:
                self.pub(subtopic, {"value": "0.0", "unit": unit})

    @on(Action.StartTransaction)
    async def on_start_transaction(self, connector_id, id_tag,
                                   meter_start, timestamp, **kwargs):
        # A charging session begins. We assign a fixed transaction id (this is a
        # single-charger home setup, not a billing backend) and accept the tag.
        self._current_transaction_id = 1
        logger.info("[%s] StartTransaction connector=%s meterStart=%s Wh",
                    self.id, connector_id, meter_start)
        self.pub(f"connector/{connector_id}/session/start", {
            "meter_start_wh": meter_start,
            "id_tag":         id_tag,
            "timestamp":      timestamp,
        })
        return call_result.StartTransaction(
            transaction_id=self._current_transaction_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action.StopTransaction)
    async def on_stop_transaction(self, meter_stop, timestamp, **kwargs):
        # The charging session ends; publish the final meter and clear the id.
        logger.info("[%s] StopTransaction meterStop=%s Wh",
                    self.id, meter_stop)
        self.pub("session/stop", {
            "meter_stop_wh": meter_stop,
            "timestamp":     timestamp,
        })
        self._current_transaction_id = None
        return call_result.StopTransaction()

    @on(Action.Authorize)
    async def on_authorize(self, id_tag, **kwargs):
        # No access control by design (free charging on a trusted network): any
        # RFID tag is accepted. See the security note in the README.
        logger.info("[%s] Authorize id_tag=%s → Accepted", self.id, id_tag)
        return call_result.Authorize(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    # ── Commands: CSMS → Charger ───────────────────────────────────────────────
    # These are invoked by the MQTT dispatcher. Each sends an OCPP request with
    # self.call(...) and publishes the charger's reply on a ".../response" topic.

    async def remote_start(self, id_tag: str = "FREE", connector_id: int = 1):
        resp = await self.call(call.RemoteStartTransaction(
            id_tag=id_tag, connector_id=connector_id
        ))
        self.pub("cmd/start/response", resp.status)
        logger.info("[%s] RemoteStart → %s", self.id, resp.status)

    async def remote_stop(self):
        # Nothing to stop if no session is currently tracked.
        if self._current_transaction_id is None:
            self.pub("cmd/stop/response", "NoActiveTransaction")
            return
        resp = await self.call(call.RemoteStopTransaction(
            transaction_id=self._current_transaction_id
        ))
        self.pub("cmd/stop/response", resp.status)
        logger.info("[%s] RemoteStop → %s", self.id, resp.status)

    async def set_charging_limit(self, amps: float, connector_id: int = 0,
                                 purpose: str = "tx_default"):
        """Cap the charging current with a charging profile.

        ``amps = 0`` pauses the charge: OCPP 1.6 allows a 0 A limit, and the charge point
        suspends (``SuspendedEVSE``) while keeping the transaction open, so charging resumes
        as soon as a limit of at least 6 A is sent again. Any other value is clamped to the
        6–32 A window (6 A is the IEC 61851 minimum).

        ``purpose`` selects the profile: ``tx_default`` (default) sets a ``TxDefaultProfile``
        on ``connector_id`` (0 = the whole charge point), ``tx`` sets a ``TxProfile`` bound to
        the running transaction, for chargers that apply the default profile only to the next
        session. A ``TxProfile`` needs a connector of its own, so connector 0 becomes 1.
        """
        amps = 0.0 if float(amps) <= 0 else max(6.0, min(float(amps), 32.0))
        profile = {
            "chargingProfileId":      1,
            "stackLevel":             0,
            "chargingProfilePurpose": ChargingProfilePurposeType.tx_default_profile,
            "chargingProfileKind":    ChargingProfileKindType.relative,
            "chargingSchedule": {
                "chargingRateUnit": ChargingRateUnitType.amps,
                "chargingSchedulePeriod": [
                    {"startPeriod": 0, "limit": amps}
                ],
            },
        }
        if purpose == "tx":
            if self._current_transaction_id is None:
                self.pub("cmd/set_limit/response",
                         {"amps": amps, "purpose": purpose, "status": "NoActiveTransaction"})
                logger.warning("[%s] set_limit purpose=tx without a running transaction", self.id)
                return
            profile.update({
                "chargingProfileId":      2,
                "stackLevel":             1,
                "chargingProfilePurpose": ChargingProfilePurposeType.tx_profile,
                "transactionId":          self._current_transaction_id,
            })
            connector_id = connector_id or 1
        resp = await self.call(call.SetChargingProfile(
            connector_id=connector_id, cs_charging_profiles=profile,
        ))
        self.pub("cmd/set_limit/response",
                 {"amps": amps, "purpose": purpose, "status": resp.status})
        logger.info("[%s] SetChargingProfile %.1fA (%s) → %s",
                    self.id, amps, purpose, resp.status)

    async def get_configuration(self, keys: list[str] | None = None):
        # Read OCPP configuration keys (all of them, or a specific subset).
        req  = call.GetConfiguration(key=keys) if keys else call.GetConfiguration()
        resp = await self.call(req)
        result = {
            item["key"]: item.get("value")
            for item in (resp.configuration_key or [])
        }
        self.pub("cmd/get_configuration/response", result)
        logger.info("[%s] GetConfiguration → %d keys", self.id, len(result))

    async def change_configuration(self, key: str, value: str):
        # Write a single OCPP configuration key (e.g. MeterValueSampleInterval).
        resp = await self.call(call.ChangeConfiguration(key=key, value=value))
        self.pub("cmd/change_configuration/response",
                 {"key": key, "value": value, "status": resp.status})
        logger.info("[%s] ChangeConfiguration %s=%s → %s",
                    self.id, key, value, resp.status)

    async def trigger_message(self, requested_message: str = "StatusNotification",
                              connector_id: int = 1):
        # Ask the charger to (re)send a specific message, e.g. BootNotification
        # to refresh firmware info, or StatusNotification to refresh state.
        resp = await self.call(call.TriggerMessage(
            requested_message=requested_message,
            connector_id=connector_id,
        ))
        self.pub("cmd/trigger/response",
                 {"message": requested_message, "status": resp.status})
        logger.info("[%s] TriggerMessage %s → %s",
                    self.id, requested_message, resp.status)

    async def reset(self, reset_type: str = "Soft"):
        # Soft = restart the application; Hard = full reboot of the charger.
        t    = ResetType.soft if reset_type.lower() == "soft" else ResetType.hard
        resp = await self.call(call.Reset(type=t))
        self.pub("cmd/reset/response", resp.status)
        logger.info("[%s] Reset %s → %s", self.id, reset_type, resp.status)

    async def unlock_connector(self, connector_id: int = 1):
        # Release the cable lock on a connector.
        resp = await self.call(call.UnlockConnector(connector_id=connector_id))
        self.pub("cmd/unlock/response", resp.status)
        logger.info("[%s] UnlockConnector %s → %s",
                    self.id, connector_id, resp.status)

    async def change_availability(self, available: bool = True,
                                  connector_id: int = 0):
        """Enable/disable the charger (or a single connector). connector_id=0 =
        whole station. available=False → Inoperative (charging blocked)."""
        a_type = AvailabilityType.operative if available else AvailabilityType.inoperative
        resp = await self.call(call.ChangeAvailability(
            connector_id=connector_id, type=a_type
        ))
        self.pub("cmd/change_availability/response",
                 {"available": available, "status": resp.status})
        logger.info("[%s] ChangeAvailability %s → %s",
                    self.id, a_type, resp.status)


# ── MQTT connection callback ───────────────────────────────────────────────────
def on_mqtt_connect(client, userdata, flags, rc):
    """Runs on every (re)connection to the broker (paho callback thread)."""
    if rc != 0:
        logger.error("MQTT connection refused (rc=%s)", rc)
        return
    logger.info("MQTT (re)connected (rc=0)")
    # Republish the bridge status (overrides any 'offline' left by the LWT).
    client.publish(f"{MQTT_PREFIX}/bridge/status", "online", retain=True)
    # Known but not (yet) connected chargers: publish 'Disconnected' retained so
    # the integration immediately shows the correct state without waiting for boot.
    for charger_id in EXPECTED_CHARGERS:
        if charger_id not in connected_chargers:
            client.publish(f"{MQTT_PREFIX}/{charger_id}/status",
                           "Disconnected", retain=True)
    # Re-subscribe to the commands of every still-connected charger: after a broker
    # reconnection the previous subscriptions are lost.
    for charger_id in connected_chargers:
        client.subscribe(f"{MQTT_PREFIX}/{charger_id}/cmd/#")
        logger.info("Re-subscribed cmd for [%s]", charger_id)


# ── MQTT command dispatcher ────────────────────────────────────────────────────
def on_mqtt_message(client, userdata, msg):
    """Route an inbound MQTT command to the right charger (paho callback thread).

    Expected topic shape: ``<prefix>/<charger_id>/cmd/<command>``.
    """
    parts = msg.topic.split("/")
    # Need at least prefix/id/cmd/command, and the second-to-last part must be "cmd"
    # (ignores e.g. our own ".../cmd/<command>/response" echoes).
    if len(parts) < 4 or parts[-2] != "cmd":
        return

    charger_id = parts[1]
    command    = parts[-1]

    if charger_id not in connected_chargers:
        logger.warning("Command for a charger that is not connected: %s", charger_id)
        return

    charger = connected_chargers[charger_id]
    # Tolerate empty or malformed payloads — fall back to an empty dict.
    try:
        payload = json.loads(msg.payload) if msg.payload else {}
    except json.JSONDecodeError:
        payload = {}

    logger.info("MQTT cmd → [%s] %s %s", charger_id, command, payload)

    async def dispatch():
        # Map the command name to the corresponding charger coroutine.
        if command == "start":
            await charger.remote_start(
                id_tag=payload.get("tag", "FREE"),
                connector_id=int(payload.get("connector_id", 1)),
            )
        elif command == "stop":
            await charger.remote_stop()
        elif command == "set_limit":
            await charger.set_charging_limit(
                amps=float(payload.get("amps", 16)),
                connector_id=int(payload.get("connector_id", 0)),
                purpose=str(payload.get("purpose", "tx_default")),
            )
        elif command == "get_configuration":
            await charger.get_configuration(keys=payload.get("keys"))
        elif command == "change_configuration":
            await charger.change_configuration(
                key=payload["key"], value=str(payload["value"])
            )
        elif command == "trigger":
            await charger.trigger_message(
                requested_message=payload.get("message", "StatusNotification"),
                connector_id=int(payload.get("connector_id", 1)),
            )
        elif command == "reset":
            await charger.reset(reset_type=payload.get("type", "Soft"))
        elif command == "unlock":
            await charger.unlock_connector(
                connector_id=int(payload.get("connector_id", 1))
            )
        elif command == "change_availability":
            await charger.change_availability(
                available=bool(payload.get("available", True)),
                connector_id=int(payload.get("connector_id", 0)),
            )
        else:
            logger.warning("Unknown command: %s", command)

    # We're on paho's thread here; hand the coroutine to the asyncio loop so the
    # OCPP call runs on the loop that owns the WebSocket.
    asyncio.run_coroutine_threadsafe(dispatch(), loop)


# ── WebSocket handler ──────────────────────────────────────────────────────────
async def websocket_handler(websocket, path):
    """Lifecycle of a single charger connection, from connect to disconnect."""
    # The charger id is the last path segment of ws://host:port/<charger_id>.
    charger_id = path.strip("/").split("/")[-1]
    logger.info("New WebSocket connection: charger_id=%s path=%s",
                charger_id, path)
    charger = MyChargePoint(charger_id, websocket)
    connected_chargers[charger_id] = charger
    now = datetime.now(timezone.utc).isoformat()
    # Mark connected and subscribe to this charger's command topics.
    mqttc.publish(f"{MQTT_PREFIX}/{charger_id}/status", "Connected", retain=True)
    mqttc.publish(f"{MQTT_PREFIX}/{charger_id}/last_connected", now, retain=True)
    mqttc.subscribe(f"{MQTT_PREFIX}/{charger_id}/cmd/#")

    # `charger.start()` blocks until the socket closes; the reason we record here
    # depends on how it ended.
    reason = "normal_closure"
    try:
        await charger.start()
    except websockets.exceptions.ConnectionClosedError as e:
        reason = f"connection_error_{e.code}" if e.code else "connection_error"
        logger.warning("[%s] Abrupt disconnect (no close frame)", charger_id)
    except websockets.exceptions.ConnectionClosedOK:
        logger.info("[%s] Clean disconnect", charger_id)
    except Exception as e:
        reason = "unexpected_error"
        logger.error("[%s] Unexpected error: %s", charger_id, e)
    finally:
        # Tear down: deregister, unsubscribe, zero instantaneous meters and
        # publish the disconnect state/reason/timestamp (all retained).
        connected_chargers.pop(charger_id, None)
        mqttc.unsubscribe(f"{MQTT_PREFIX}/{charger_id}/cmd/#")
        charger.reset_instant_meters()
        mqttc.publish(f"{MQTT_PREFIX}/{charger_id}/status",
                      "Disconnected", retain=True)
        mqttc.publish(f"{MQTT_PREFIX}/{charger_id}/disconnect_reason",
                      reason, retain=True)
        mqttc.publish(f"{MQTT_PREFIX}/{charger_id}/last_disconnected",
                      datetime.now(timezone.utc).isoformat(), retain=True)
        logger.info("Charger disconnected: %s (reason: %s)", charger_id, reason)


# ── Entry point ────────────────────────────────────────────────────────────────
async def main():
    global loop
    # Capture the running loop so paho callbacks can schedule coroutines onto it.
    loop = asyncio.get_running_loop()

    # Wire up MQTT: callbacks, Last Will (marks the bridge offline if we die),
    # then connect and start paho's background network thread.
    mqttc.on_connect = on_mqtt_connect
    mqttc.on_message = on_mqtt_message
    mqttc.will_set(f"{MQTT_PREFIX}/bridge/status", "offline", retain=True)
    if MQTT_USER:
        mqttc.username_pw_set(MQTT_USER, MQTT_PASS)
    # Automatic reconnection with backoff if the broker drops; on_connect restores
    # the 'online' status and the command subscriptions.
    mqttc.reconnect_delay_set(min_delay=1, max_delay=30)
    mqttc.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    mqttc.loop_start()
    logger.info("MQTT connecting to %s:%s", MQTT_HOST, MQTT_PORT)

    # Start the OCPP WebSocket server (chargers must speak the ocpp1.6 subprotocol).
    server = await websockets.serve(
        websocket_handler,
        "0.0.0.0",
        OCPP_PORT,
        subprotocols=["ocpp1.6"],
    )
    logger.info("OCPP Central System listening on ws://0.0.0.0:%s", OCPP_PORT)

    # Block until SIGINT/SIGTERM, then shut down gracefully.
    stop = asyncio.Future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set_result, None)
    await stop

    server.close()
    await server.wait_closed()
    # Best-effort: announce offline before dropping the MQTT connection.
    mqttc.publish(f"{MQTT_PREFIX}/bridge/status", "offline", retain=True)
    mqttc.loop_stop()
    mqttc.disconnect()
    logger.info("Bridge stopped.")


if __name__ == "__main__":
    asyncio.run(main())
