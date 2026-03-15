from __future__ import annotations

from . import DOMAIN

import logging
_LOGGER = logging.getLogger(__name__)

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.config_validation import PLATFORM_SCHEMA
from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.const import CONF_NAME, CONF_PORT, CONF_UNIQUE_ID
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.event import async_track_time_interval
from .hub import BIAS, EDGE
from gpiod.line import Value
from datetime import timedelta

import homeassistant.helpers.config_validation as cv
import voluptuous as vol

CONF_COUNTERS = "counters"
CONF_ENCODERS = "encoders"
CONF_PULL_MODE = "pull_mode"
DEFAULT_PULL_MODE = "UP"
CONF_BOUNCETIME = "bouncetime"
DEFAULT_BOUNCETIME = 50
CONF_INVERT_LOGIC = "invert_logic"
DEFAULT_INVERT_LOGIC = False
CONF_EDGE = "edge"
DEFAULT_EDGE = "RISING"
CONF_PORT_A = "port_a"
CONF_PORT_B = "port_b"
CONF_TACHOMETERS = "tachometers"
CONF_PULSES_PER_REV = "pulses_per_rev"
DEFAULT_PULSES_PER_REV = 2
CONF_UPDATE_INTERVAL = "update_interval"
DEFAULT_UPDATE_INTERVAL = 2  # seconds
CONF_TACH_EDGE = "edge"
DEFAULT_TACH_EDGE = "FALLING"

PLATFORM_SCHEMA = vol.All(
    PLATFORM_SCHEMA.extend({
        vol.Optional(CONF_COUNTERS): vol.All(
            cv.ensure_list, [{
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_PORT): cv.positive_int,
                vol.Optional(CONF_PULL_MODE, default=DEFAULT_PULL_MODE): vol.In(BIAS.keys()),
                vol.Optional(CONF_BOUNCETIME, default=DEFAULT_BOUNCETIME): cv.positive_int,
                vol.Optional(CONF_INVERT_LOGIC, default=DEFAULT_INVERT_LOGIC): cv.boolean,
                vol.Optional(CONF_EDGE, default=DEFAULT_EDGE): vol.In(EDGE.keys()),
                vol.Optional(CONF_UNIQUE_ID): cv.string,
            }]
        ),
        vol.Optional(CONF_ENCODERS): vol.All(
            cv.ensure_list, [{
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_PORT_A): cv.positive_int,
                vol.Required(CONF_PORT_B): cv.positive_int,
                vol.Optional(CONF_PULL_MODE, default=DEFAULT_PULL_MODE): vol.In(BIAS.keys()),
                vol.Optional(CONF_BOUNCETIME, default=DEFAULT_BOUNCETIME): cv.positive_int,
                vol.Optional(CONF_UNIQUE_ID): cv.string,
            }]
        ),
        vol.Optional(CONF_TACHOMETERS): vol.All(
            cv.ensure_list, [{
                vol.Required(CONF_NAME): cv.string,
                vol.Required(CONF_PORT): cv.positive_int,
                vol.Optional(CONF_PULL_MODE, default=DEFAULT_PULL_MODE): vol.In(BIAS.keys()),
                vol.Optional(CONF_BOUNCETIME, default=0): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=1000)),
                vol.Optional(CONF_TACH_EDGE, default=DEFAULT_TACH_EDGE): vol.In(("RISING", "FALLING")),
                vol.Optional(CONF_PULSES_PER_REV, default=DEFAULT_PULSES_PER_REV): cv.positive_int,
                vol.Optional(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=1, max=60)),
                vol.Optional(CONF_UNIQUE_ID): cv.string,
            }]
        ),
    })
)


async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None) -> None:

    _LOGGER.debug(f"sensor setup_platform: {config}")
    hub = hass.data[DOMAIN]
    if not hub._online:
        _LOGGER.error("hub not online, bailing out")
        return

    entities = []

    for counter in config.get(CONF_COUNTERS, []):
        try:
            entities.append(
                GPIODCounterSensor(
                    hub,
                    counter[CONF_NAME],
                    counter[CONF_PORT],
                    counter.get(CONF_UNIQUE_ID) or f"{DOMAIN}_{counter[CONF_PORT]}_{counter[CONF_NAME].lower().replace(' ', '_')}",
                    counter.get(CONF_INVERT_LOGIC),
                    counter.get(CONF_PULL_MODE),
                    counter.get(CONF_BOUNCETIME),
                    counter.get(CONF_EDGE),
                )
            )
        except Exception as e:
            _LOGGER.error(f"Failed to add counter {counter[CONF_NAME]} for port {counter[CONF_PORT]}: {e}")

    for encoder in config.get(CONF_ENCODERS, []):
        try:
            entities.append(
                GPIODEncoderSensor(
                    hub,
                    encoder[CONF_NAME],
                    encoder[CONF_PORT_A],
                    encoder[CONF_PORT_B],
                    encoder.get(CONF_UNIQUE_ID) or f"{DOMAIN}_{encoder[CONF_PORT_A]}_{encoder[CONF_PORT_B]}_{encoder[CONF_NAME].lower().replace(' ', '_')}",
                    encoder.get(CONF_PULL_MODE),
                    encoder.get(CONF_BOUNCETIME),
                )
            )
        except Exception as e:
            _LOGGER.error(f"Failed to add encoder {encoder[CONF_NAME]} for ports {encoder[CONF_PORT_A]}/{encoder[CONF_PORT_B]}: {e}")

    for tach in config.get(CONF_TACHOMETERS, []):
        try:
            entities.append(
                GPIODTachometerSensor(
                    hub,
                    tach[CONF_NAME],
                    tach[CONF_PORT],
                    tach.get(CONF_UNIQUE_ID) or f"{DOMAIN}_{tach[CONF_PORT]}_{tach[CONF_NAME].lower().replace(' ', '_')}",
                    tach.get(CONF_PULL_MODE),
                    tach.get(CONF_BOUNCETIME),
                    tach.get(CONF_TACH_EDGE),
                    tach.get(CONF_PULSES_PER_REV),
                    tach.get(CONF_UPDATE_INTERVAL),
                )
            )
        except Exception as e:
            _LOGGER.error(f"Failed to add tachometer {tach[CONF_NAME]} for port {tach[CONF_PORT]}: {e}")

    async_add_entities(entities)

    async def handle_reset(call: ServiceCall) -> None:
        entity_ids = call.data.get("entity_id", [])
        for entity in entities:
            if entity.entity_id in entity_ids:
                entity.reset()

    hass.services.async_register(DOMAIN, "reset_sensor", handle_reset,
        schema=vol.Schema({vol.Required("entity_id"): cv.entity_ids}))


class GPIODCounterSensor(SensorEntity, RestoreEntity):
    """Counts rising/falling edge pulses on a GPIO pin."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.TOTAL
    _attr_native_unit_of_measurement = "pulses"
    _attr_icon = "mdi:counter"

    def __init__(self, hub, name, port, unique_id, active_low, bias, debounce, edge):
        _LOGGER.debug(f"GPIODCounterSensor init: port={port} name={name}")
        self._hub = hub
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._port = port
        self._active_low = active_low
        self._bias = bias
        self._debounce = debounce
        self._edge = edge
        self._count = 0
        self._attr_native_value = 0
        self._line = self._hub.add_counter(port, active_low, bias, debounce, edge)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore previous count across restarts
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._count = int(float(last_state.state))
                self._attr_native_value = self._count
            except (ValueError, TypeError):
                pass
        _LOGGER.debug(f"GPIODCounterSensor async_added_to_hass: Adding fd:{self._line.fd}, restored count={self._count}")
        self._hub._hass.loop.add_reader(self._line.fd, self.handle_event)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        _LOGGER.debug(f"GPIODCounterSensor async_will_remove_from_hass: Removing fd:{self._line.fd}")
        self._hub._hass.loop.remove_reader(self._line.fd)
        self._line.release()

    def handle_event(self) -> None:
        for event in self._line.read_edge_events():
            self._count += 1
            self._attr_native_value = self._count
            _LOGGER.debug(f"Counter event: {event}. Count: {self._count}")
        self.schedule_update_ha_state(False)

    def reset(self) -> None:
        self._count = 0
        self._attr_native_value = 0
        self.schedule_update_ha_state(False)


class GPIODEncoderSensor(SensorEntity, RestoreEntity):
    """Tracks quadrature encoder position using two GPIO pins (A and B)."""

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "steps"
    _attr_icon = "mdi:rotate-right"

    def __init__(self, hub, name, port_a, port_b, unique_id, bias, debounce):
        _LOGGER.debug(f"GPIODEncoderSensor init: port_a={port_a} port_b={port_b} name={name}")
        self._hub = hub
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._port_a = port_a
        self._port_b = port_b
        self._bias = bias
        self._debounce = debounce
        self._position = 0
        self._attr_native_value = 0
        self._line = self._hub.add_encoder(port_a, port_b, bias, debounce)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Restore previous position across restarts
        if (last_state := await self.async_get_last_state()) is not None:
            try:
                self._position = int(float(last_state.state))
                self._attr_native_value = self._position
            except (ValueError, TypeError):
                pass
        _LOGGER.debug(f"GPIODEncoderSensor async_added_to_hass: Adding fd:{self._line.fd}, restored position={self._position}")
        self._hub._hass.loop.add_reader(self._line.fd, self.handle_event)

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        _LOGGER.debug(f"GPIODEncoderSensor async_will_remove_from_hass: Removing fd:{self._line.fd}")
        self._hub._hass.loop.remove_reader(self._line.fd)
        self._line.release()

    def handle_event(self) -> None:
        for event in self._line.read_edge_events():
            # Quadrature decoding: read current state of the OTHER pin to determine direction.
            # When A changes: B LOW on rising → CW (+1), B HIGH on rising → CCW (-1)
            if event.line_offset == self._port_a:
                b_active = self._line.get_value(self._port_b) == Value.ACTIVE
                if event.event_type == event.Type.RISING_EDGE:
                    self._position += 1 if not b_active else -1
                else:
                    self._position += -1 if not b_active else 1
            # Ignore port_b events - port_a edges alone give X1 resolution (sufficient for most uses)
            self._attr_native_value = self._position
            _LOGGER.debug(f"Encoder event: {event}. Position: {self._position}")
        self.schedule_update_ha_state(False)

    def reset(self) -> None:
        self._position = 0
        self._attr_native_value = 0
        self.schedule_update_ha_state(False)


class GPIODTachometerSensor(SensorEntity):
    """Measures rotation speed (RPM) by counting GPIO pulses over a sliding time window.

    Typical use: fan tachometer wire. Most PC fans output 2 pulses per revolution.
    RPM = (pulses_in_window / pulses_per_rev) * (60 / window_seconds)
    """

    _attr_should_poll = False
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "RPM"
    _attr_icon = "mdi:fan"

    def __init__(self, hub, name, port, unique_id, bias, debounce, edge, pulses_per_rev, update_interval):
        _LOGGER.debug(f"GPIODTachometerSensor init: port={port} name={name} edge={edge} ppr={pulses_per_rev} interval={update_interval}s")
        self._hub = hub
        self._attr_name = name
        self._attr_unique_id = unique_id
        self._port = port
        self._bias = bias
        self._debounce = debounce
        self._edge = edge
        self._pulses_per_rev = pulses_per_rev
        self._update_interval = update_interval  # seconds
        self._attr_native_value = 0
        self._pulse_count = 0
        self._unsub_timer = None
        # Fan tach outputs are typically open-collector; counting FALLING edges is usually the most stable default.
        self._line = self._hub.add_counter(port, False, bias, debounce, edge)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        _LOGGER.debug(f"GPIODTachometerSensor async_added_to_hass: fd={self._line.fd}")
        self._hub._hass.loop.add_reader(self._line.fd, self._handle_pulse)
        self._unsub_timer = async_track_time_interval(
            self._hub._hass,
            self._async_compute_rpm,
            timedelta(seconds=self._update_interval),
        )

    async def async_will_remove_from_hass(self) -> None:
        await super().async_will_remove_from_hass()
        _LOGGER.debug(f"GPIODTachometerSensor async_will_remove_from_hass: fd={self._line.fd}")
        self._hub._hass.loop.remove_reader(self._line.fd)
        if self._unsub_timer:
            self._unsub_timer()
        self._line.release()

    def _handle_pulse(self) -> None:
        """Called by the event loop whenever a rising edge arrives."""
        for _ in self._line.read_edge_events():
            self._pulse_count += 1

    async def _async_compute_rpm(self, _now=None) -> None:
        """Periodically called to compute RPM from pulses accumulated since last tick."""
        pulse_count = self._pulse_count
        self._pulse_count = 0  # reset for next interval
        rpm = round((pulse_count / self._pulses_per_rev) * (60 / self._update_interval))
        _LOGGER.debug(f"Tachometer compute: {pulse_count} pulses → {rpm} RPM")
        self._attr_native_value = rpm
        self.async_write_ha_state()
