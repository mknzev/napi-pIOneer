"""ResourceManager — кто какой ресурс держит (spec 3.5).

Ресурсы:
- пин: "J9-7" в рантайм-режиме (gpio_in / gpio_out / soft_pwm / adc);
- устройство на I2C: "i2c1@0x3c" — шина общая, адрес эксклюзивный;
- SPI-устройство: "spi2.cs0" — эксклюзивно;
- UART: "uart3" — эксклюзивно;
- встроенный LED: "led:user" — эксклюзивно (через /sys/class/leds).

Шины адресуются по контроллеру из boards/*.yaml без mux-варианта (i2c3, а не i2c3_m0) —
как в SDK. Доступны, только если интерфейс включён (живой DT / uEnv.txt); их пины при
этом уже отданы интерфейсу (Board.function) и как GPIO недоступны.

gpio_out / soft_pwm — эксклюзивно; gpio_in / adc — разделяемые (рефкаунт владельцев).
GPIO-хендлы одного project:<id> используют общий захват; резервирование выхода
сохраняется, пока его владелец не освободит ресурс, даже если хендл открыл вход.
Пакет захватов атомарен: либо все, либо ни одного.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Literal

from pioneer.board import Board, Interface, InterfaceError, RuntimeMode

BusMode = Literal["i2c", "spi", "uart", "led", "pwm", "uart_rx", "uart_tx"]
Mode = RuntimeMode | BusMode
SHARED: frozenset[str] = frozenset({"gpio_in", "adc", "uart_rx"})

_I2C = re.compile(r"^(i2c\d+)@0x([0-9a-f]{1,2})$")
_SPI = re.compile(r"^(spi\d+)\.cs(\d+)$")
_UART = re.compile(r"^(uart\d+)$")
_LED = re.compile(r"^led:(\w+)$")


def i2c_id(controller: str, addr: int) -> str:
    return f"{controller}@0x{addr:02x}"


def spi_id(controller: str, cs: int) -> str:
    return f"{controller}.cs{cs}"


def uart_id(controller: str) -> str:
    return controller


def led_id(led: str) -> str:
    return f"led:{led}"


def same_owner_lineage(owner: str, other: str) -> bool:
    if owner == other:
        return True
    parts = owner.split(":", 2)
    return (len(parts) >= 2 and parts[0] == "project" and bool(parts[1])
            and other.split(":", 2)[:2] == parts[:2])


@dataclass
class Claim:
    resource: str
    mode: Mode
    owners: dict[str, str] = field(default_factory=dict)  # owner id -> человекочитаемое имя
    owner_modes: dict[str, Mode] = field(default_factory=dict)

    def refresh_mode(self) -> None:
        self.mode = ("gpio_out" if "gpio_out" in self.owner_modes.values()
                     else next(iter(self.owner_modes.values())))


class ResourceConflict(Exception):
    pass


class ResourceManager:
    def __init__(self, board: Board):
        self.board = board
        self.claims: dict[str, Claim] = {}
        self._lock = threading.Lock()

    # ---- проверки
    def check(self, owner: str, resource: str, mode: Mode) -> None:
        if resource in self.board.pins:
            self._check_pin(resource, mode)
        else:
            self._check_bus(resource, mode)
        if resource.startswith("uart"):
            ctl = resource.split(":")[0]
            clashes = ([ctl + ":rx", ctl + ":tx"] if resource == ctl else [ctl])
            for rid in clashes:
                other = self.claims.get(rid)
                if other and any(o != owner for o in other.owners):
                    raise ResourceConflict(f"{resource}: UART занят в несовместимом режиме")
        claim = self.claims.get(resource)
        if claim is None:
            return
        holders = []
        for existing, name in claim.owners.items():
            existing_mode = claim.owner_modes[existing]
            if existing == owner:
                continue
            if existing_mode == mode and mode in SHARED:
                continue
            if resource in self.board.pins and same_owner_lineage(owner, existing):
                if (existing_mode == mode and mode in ("gpio_out", "soft_pwm")
                        or {existing_mode, mode} <= {"gpio_in", "gpio_out"}):
                    continue
            holders.append(f'"{name}"')
        if holders:
            raise ResourceConflict(f"{resource} занят ({claim.mode}): {', '.join(holders)}")

    def _check_pin(self, pin_id: str, mode: Mode) -> None:
        board = self.board
        pin = board.pin(pin_id)
        if mode in board.runtime_modes(pin_id):
            return
        func = board.function(pin_id)
        if func == "kernel":
            raise ResourceConflict(
                f"{pin_id} занят драйвером ядра: {', '.join(board.kernel_users(pin_id))}")
        if func in board.interfaces:
            iface = board.interfaces[func]
            raise ResourceConflict(
                f"{pin_id} отдан интерфейсу {iface.title} ({iface.pins[pin_id]}); чтобы использовать как "
                f"{mode}, выключите {iface.title} в uEnv.txt и перезагрузите плату")
        raise ResourceConflict(f"{pin_id} ({pin.name or pin.kind}) не поддерживает режим {mode}")

    def _check_bus(self, resource: str, mode: Mode) -> None:
        if m := _I2C.match(resource):
            self._need(mode, "i2c", resource)
            if not 0x03 <= int(m[2], 16) <= 0x77:
                raise ResourceConflict(f"{resource}: адрес I2C 0x03…0x77")
            self._enabled_iface(m[1], "i2c")
        elif m := _SPI.match(resource):
            self._need(mode, "spi", resource)
            iface = self._enabled_iface(m[1], "spi")
            cs_roles = {r for r in iface.pins.values() if r.startswith("CSN")}
            if f"CSN{m[2]}" not in cs_roles:
                raise ResourceConflict(f"{resource}: на гребёнке выведены только {', '.join(sorted(cs_roles))}")
        elif re.fullmatch(r"pwm\d+", resource):
            self._need(mode, "pwm", resource)
            self._enabled_iface(resource, "pwm")
        elif m := re.fullmatch(r"(uart\d+):(rx|tx)", resource):
            self._need(mode, "uart_" + m[2], resource)
            self._enabled_iface(m[1], "uart")
        elif m := _UART.match(resource):
            self._need(mode, "uart", resource)
            self._enabled_iface(m[1], "uart")
        elif m := _LED.match(resource):
            self._need(mode, "led", resource)
            if m[1] not in self.board.leds:
                raise ResourceConflict(f"нет встроенного LED {m[1]!r}")
        else:
            raise ResourceConflict(f"неизвестный ресурс {resource!r}")

    @staticmethod
    def _need(mode: Mode, expected: str, resource: str) -> None:
        if mode != expected:
            raise ResourceConflict(f"{resource}: режим должен быть {expected}, а не {mode}")

    def _enabled_iface(self, controller: str, kind: str) -> Interface:
        try:
            return self.board.bus_interface(controller, kind)
        except InterfaceError as e:
            raise ResourceConflict(str(e)) from None

    # ---- захват
    def claim_many(self, owner: str, owner_name: str, wants: dict[str, Mode]) -> None:
        with self._lock:
            errors = []
            for res, mode in wants.items():
                try:
                    self.check(owner, res, mode)
                except ResourceConflict as e:
                    errors.append(str(e))
                except KeyError as e:  # нет такого пина
                    errors.append(e.args[0])
            if errors:
                raise ResourceConflict("; ".join(errors))
            for res, mode in wants.items():
                claim = self.claims.get(res)
                if claim is None:
                    claim = self.claims[res] = Claim(res, mode)
                claim.owners[owner] = owner_name
                claim.owner_modes[owner] = mode
                claim.refresh_mode()

    def claim(self, owner: str, owner_name: str, resource: str, mode: Mode) -> None:
        self.claim_many(owner, owner_name, {resource: mode})

    def release(self, owner: str, resource: str | None = None) -> list[str]:
        """Освободить ресурс (или все ресурсы владельца). Возвращает ставшие свободными."""
        freed = []
        with self._lock:
            for rid, claim in list(self.claims.items()):
                if (resource is None or rid == resource) and owner in claim.owners:
                    del claim.owners[owner]
                    del claim.owner_modes[owner]
                    if not claim.owners:
                        del self.claims[rid]
                        freed.append(rid)
                    else:
                        claim.refresh_mode()
        return freed

    def owned_by(self, owner: str) -> list[str]:
        return [rid for rid, c in self.claims.items() if owner in c.owners]
