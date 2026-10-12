"""HW: захват ресурса (ResourceManager) + открытие его в HAL.

Единая точка доступа к железу для движка проектов, скриптов и ручного управления.
Разделяемый вход — одна линия HAL, события раздаются всем подписчикам.
I2C-шина открывается одна на core; владелец получает I2CDevice, привязанный к адресу.
"""
from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from pioneer.board import Board, Interface, InterfaceError, RuntimeMode
from pioneer.hal.base import (
    HAL, EdgeEvent, I2CBus, InputLine, LedLine, LineBusy, OutputLine, PwmLine, SPIDevice, UARTPort,
)
from pioneer.resources import ResourceConflict, ResourceManager, i2c_id, led_id, spi_id, uart_id

PinListener = Callable[[str, EdgeEvent], None]


@dataclass
class _Open:
    mode: RuntimeMode
    line: InputLine | OutputLine | PwmLine | None = None
    listeners: dict[str, PinListener] = field(default_factory=dict)
    safe: bool = False


class I2CDevice:
    """Устройство по одному адресу на общей шине."""

    def __init__(self, bus: I2CBus, iface: Interface, addr: int):
        self.bus, self.iface, self.addr = bus, iface, addr

    def write(self, data: bytes) -> None:
        self.bus.write(self.addr, data)

    def read(self, n: int) -> bytes:
        return self.bus.read(self.addr, n)

    def write_read(self, data: bytes, n: int) -> bytes:
        return self.bus.write_read(self.addr, data, n)


class HW:
    def __init__(self, board: Board, hal: HAL, resources: ResourceManager):
        self.board, self.hal, self.res = board, hal, resources
        self.drivers = None  # attached by application; kernel configuration is not exposed to scripts
        self._open: dict[str, _Open] = {}
        self._handles: dict[str, SPIDevice | UARTPort | LedLine] = {}  # эксклюзивные открытые ресурсы
        self._lock = threading.RLock()
        self._uart_hubs = {}
        self._spi_settings: dict[str, tuple[int, int]] = {}
        self._watchers: list[PinListener] = []  # все изменения входов (live-уровни для UI)

    def watch(self, cb: PinListener) -> None:
        self._watchers.append(cb)

    # ---- пины
    def acquire(self, owner: str, owner_name: str, pin_id: str, mode: RuntimeMode, *,
                initial: bool = False, safe: bool = False, debounce_ms: int = 0,
                on_edge: PinListener | None = None, listener_key: str | None = None) -> None:
        """Захватить пин и открыть линию. listener_key — несколько слушателей фронтов у одного
        владельца (ноды проекта): ключ вида "<owner>#<нода>", снимаются вместе с владельцем."""
        with self._lock:
            previous = self.res.claims.get(pin_id)
            was_owner = previous is not None and owner in previous.owners
            previous_mode = previous.owner_modes[owner] if was_owner else None
            self.res.claim(owner, owner_name, pin_id, mode)
            cur = self._open.get(pin_id)
            old = cur
            old_value = bool(cur.line.read()) if cur and cur.mode == "gpio_out" else False
            if cur and cur.mode != mode:
                self._close_pin(pin_id)
                cur = None
            if cur is None:
                # Разделяемый вход открывается с debounce первого захватившего.
                try:
                    cur = self._open[pin_id] = self._open_line(pin_id, mode, initial, debounce_ms)
                except Exception as e:
                    if not was_owner:
                        self.res.release(owner, pin_id)
                    else:
                        self.res.claim(owner, owner_name, pin_id, previous_mode)
                    if old is not None:
                        try:
                            restored = self._open[pin_id] = self._open_line(pin_id, old.mode, old_value, debounce_ms)
                            restored.listeners, restored.safe = old.listeners, old.safe
                            if old.mode == "soft_pwm":
                                restored.line.set(old.line.freq, old.line.duty)
                        except Exception as restore_error:
                            e.add_note(f"не удалось восстановить прежнюю линию: {restore_error}")
                    if isinstance(e, LineBusy):
                        raise ResourceConflict(f"{pin_id} {e.strerror or e}") from e
                    raise
            if mode == "gpio_out":
                cur.safe = bool(safe)
            if on_edge:
                cur.listeners[listener_key or owner] = on_edge

    def write(self, pin_id: str, value: bool) -> None:
        self._require(pin_id, "gpio_out").line.write(value)  # type: ignore[union-attr]

    def set_pwm(self, pin_id: str, *, freq: float | None = None, duty: float | None = None) -> None:
        self._require(pin_id, "soft_pwm").line.set(freq, duty)  # type: ignore[union-attr]

    def value(self, pin_id: str) -> bool | float | None:
        o = self._open.get(pin_id)
        if o is None:
            return None
        if o.mode == "adc":
            return self.hal.read_adc(self.board.pin(pin_id).adc_channel)  # type: ignore[arg-type]
        if o.mode == "soft_pwm":
            return o.line.duty  # type: ignore[union-attr]
        return o.line.read()  # type: ignore[union-attr]

    # ---- шины (адресуются по интерфейсу из boards/*.yaml: "i2c1", "uart2", "spi2_m0"…)
    def bus(self, name: str, kind: str) -> Interface:
        """Включённый интерфейс по имени из SDK/нод; ResourceConflict с подсказкой, если выключен."""
        try:
            return self.board.bus_interface(name, kind)
        except InterfaceError as e:
            raise ResourceConflict(str(e)) from None

    def i2c(self, owner: str, owner_name: str, iface: str, addr: int) -> I2CDevice:
        bus = self.bus(iface, "i2c")
        rid = i2c_id(bus.controller, addr)
        with self._lock:
            self.res.claim(owner, owner_name, rid, "i2c")
            try:
                return I2CDevice(self.hal.open_i2c(bus.number), bus, addr)
            except Exception:
                self.res.release(owner, rid)
                raise

    def spi(self, owner: str, owner_name: str, iface: str, cs: int, *, mode: int = 0,
            speed_hz: int = 1_000_000) -> SPIDevice:
        bus = self.bus(iface, "spi")
        rid = spi_id(bus.controller, cs)
        with self._lock:
            previous = self._spi_settings.get(rid)
            if previous is not None and previous != (mode, speed_hz):
                raise ResourceConflict("SPI уже открыт с другими mode/speed_hz; сначала освободите устройство")
            handle = self._exclusive(owner, owner_name, rid, "spi",
                                     lambda: self.hal.open_spi(bus.number, cs, mode=mode, speed_hz=speed_hz))
            self._spi_settings[rid] = (mode, speed_hz)
            return handle

    def uart(self, owner: str, owner_name: str, iface: str, *, baud: int = 9600, direction="both") -> UARTPort:
        from pioneer.hal.uart_hub import UARTHub
        if direction not in ("both", "rx", "tx"):
            raise ValueError("UART direction: both/rx/tx")
        if not isinstance(baud, int) or not 300 <= baud <= 4_000_000:
            raise ValueError("скорость UART 300…4000000 бод")
        bus = self.bus(iface, "uart")
        rid = uart_id(bus.controller) + (":" + direction if direction != "both" else "")
        mode = "uart_" + direction if direction != "both" else "uart"
        opener = None
        while True:
            with self._lock:
                hub = self._uart_hubs.get(bus.controller)
                if hub is not None or opener is not None:
                    was_owned = rid in self.res.claims and owner in self.res.claims[rid].owners
                    self.res.claim(owner, owner_name, rid, mode)
                    try:
                        if hub is None:
                            hub = self._uart_hubs[bus.controller] = UARTHub(opener())
                        return hub.subscribe(owner, direction, baud)
                    except Exception:
                        if not was_owned: self.res.release(owner, rid)
                        if hub is not None and not hub.subscriptions:
                            hub.close();self._uart_hubs.pop(bus.controller, None)
                        raise
            # The hub can appear while scanning; recheck it under the lock.
            opener = self.hal.prepare_uart(bus.device, baud=baud)

    def hardware_pwm(self, owner, owner_name, iface):
        bus = self.bus(iface, "pwm")
        return self._exclusive(owner, owner_name, bus.controller, "pwm",
                               lambda: self.hal.open_hw_pwm(bus, self.board))

    def led(self, owner: str, owner_name: str, led: str) -> LedLine:
        info = self.board.leds[led] if led in self.board.leds else None
        if info is None:
            raise ResourceConflict(f"нет встроенного LED {led!r}")
        return self._exclusive(owner, owner_name, led_id(led), "led",
                               lambda: self.hal.open_led(info.sysfs, default_trigger=info.default_trigger))

    def _exclusive(self, owner, owner_name, rid, mode, opener):
        with self._lock:
            self.res.claim(owner, owner_name, rid, mode)
            if rid not in self._handles:
                try:
                    self._handles[rid] = opener()
                except LineBusy as e:
                    self.res.release(owner, rid)
                    raise ResourceConflict(e.strerror or str(e)) from e
                except Exception:
                    self.res.release(owner, rid)
                    raise
            return self._handles[rid]

    # ---- освобождение
    def release(self, owner: str, resource: str | None = None, *, safe_value: bool | None = None) -> None:
        with self._lock:
            for ctl, hub in list(self._uart_hubs.items()):
                if resource is None or resource.split(":")[0] == ctl:
                    hub.remove(owner, resource.split(":",1)[1] if resource and ":" in resource else ("both" if resource else None))
                    if not hub.subscriptions:
                        hub.close();del self._uart_hubs[ctl]
            for rid in self.res.release(owner, resource):
                self._close(rid, safe_value)
            for rid, o in list(self._open.items()):
                if resource is None or rid == resource:
                    for key in [k for k in o.listeners if k == owner or k.startswith(owner + "#")]:
                        del o.listeners[key]
                    claim = self.res.claims[rid]
                    if o.mode == "gpio_out" and claim.mode == "gpio_in":
                        self._close_pin(rid, safe_value)
                        restored = self._open[rid] = self._open_line(rid, "gpio_in", False, 0)
                        restored.listeners = o.listeners

    def close_all(self, safe_value: bool | None = None) -> None:
        with self._lock:
            for hub in self._uart_hubs.values(): hub.close()
            self._uart_hubs.clear()
            for rid in list(self.res.claims):
                self._close(rid, safe_value)
            self.res.claims.clear()

    # ---- внутреннее
    def _require(self, pin_id: str, mode: RuntimeMode) -> _Open:
        o = self._open.get(pin_id)
        if o is None or o.mode != mode:
            raise RuntimeError(f"{pin_id} не открыт в режиме {mode}")
        return o

    def _open_line(self, pin_id: str, mode: RuntimeMode, initial: bool, debounce_ms: int) -> _Open:
        pin = self.board.pin(pin_id)
        if mode == "adc":
            return _Open(mode)
        assert pin.gpio
        if mode == "gpio_out":
            return _Open(mode, self.hal.open_output(pin.gpio, initial=initial))
        if mode == "soft_pwm":
            return _Open(mode, self.hal.open_pwm(pin.gpio))
        return _Open(mode, self.hal.open_input(
            pin.gpio, edge="both", debounce_ms=debounce_ms,
            on_edge=lambda ev, pid=pin_id: self._dispatch(pid, ev)))

    def _dispatch(self, pin_id: str, ev: EdgeEvent) -> None:
        o = self._open.get(pin_id)
        for cb in [*(o.listeners.values() if o else ()), *self._watchers]:
            cb(pin_id, ev)

    def _close(self, rid: str, safe_value: bool | None) -> None:
        if rid in self._open:
            self._close_pin(rid, safe_value)
        elif h := self._handles.pop(rid, None):
            self._spi_settings.pop(rid, None)
            h.close(bool(safe_value)) if isinstance(h, PwmLine) else h.close()
        # i2c: шина общая и остаётся открытой; захват адреса уже снят

    def _close_pin(self, pin_id: str, safe_value: bool | None = None) -> None:
        o = self._open.pop(pin_id, None)
        if o is None or o.line is None:
            return
        safe_value = o.safe if safe_value is None else safe_value
        if isinstance(o.line, PwmLine):
            o.line.close(safe_value)
            return
        if isinstance(o.line, OutputLine):
            o.line.write(safe_value)
        o.line.close()
