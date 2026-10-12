"""HW + ResourceManager поверх стенда: захваты пинов и шин."""
import time

import pytest

from conftest import wait_for
from pioneer.hal.softpwm import SoftPwm
from pioneer.resources import ResourceConflict


def test_loopback_edges_via_hw(bench, hw):
    bench.add("wire", a="J9-16", b="J9-18")
    events = []
    hw.acquire("p1", "Тест", "J9-18", "gpio_in", on_edge=lambda pid, ev: events.append(ev))
    hw.acquire("p1", "Тест", "J9-16", "gpio_out")
    for v in (True, False, True):
        hw.write("J9-16", v)
    assert wait_for(lambda: len(events) == 3)
    assert hw.value("J9-18") is True


def test_button_toggles_led(bench, hw):
    """Критерий фазы 0 на стенде: кнопка на одном пине переключает LED на другом."""
    bench.add("button", "btn", pin="J10-2", bounce_ms=5)
    led = bench.add("led", "led", pin="J9-7")
    hw.acquire("s", "Скрипт", "J9-7", "gpio_out")
    state = {"on": False}

    def on_edge(pid, ev):
        if ev.rising:
            state["on"] = not state["on"]
            hw.write("J9-7", state["on"])

    hw.acquire("s", "Скрипт", "J10-2", "gpio_in", debounce_ms=20, on_edge=on_edge)
    bench.action("btn", "press")
    assert wait_for(lambda: led.state()["on"] is True, 0.5)
    bench.action("btn", "release")
    time.sleep(0.05)  # отпускание короче debounce отфильтровалось бы — как на железе
    bench.action("btn", "press")
    assert wait_for(lambda: led.state()["on"] is False, 0.5)


def test_shared_input_exclusive_output(hw):
    hw.acquire("p1", "Полив", "J9-7", "gpio_in")
    hw.acquire("p2", "Свет", "J9-7", "gpio_in")
    with pytest.raises(ResourceConflict, match='"Полив"'):
        hw.acquire("p3", "Чужой", "J9-7", "gpio_out")
    hw.acquire("p1", "Полив", "J9-12", "gpio_out")
    with pytest.raises(ResourceConflict, match="занят"):
        hw.acquire("p2", "Свет", "J9-12", "gpio_out")


def test_mode_switch_by_single_owner(bench, hw):
    hw.acquire("m", "Ручное", "J9-7", "gpio_out", initial=True)
    assert bench.level("J9-7") is True
    hw.acquire("m", "Ручное", "J9-7", "gpio_in")  # out -> in: выход отпущен, уровень = подтяжка
    assert hw.res.claims["J9-7"].mode == "gpio_in"
    assert bench.level("J9-7") is False


@pytest.mark.parametrize("mode", ["gpio_out", "soft_pwm"])
def test_project_gpio_lineage_reuses_exclusive_reservation(hw, mode):
    root, child = "project:sensor", "project:sensor:node:cp:1"
    hw.res.claim(root, "sensor", "J9-7", mode)
    hw.acquire(child, "sensor", "J9-7", mode)
    assert hw.res.claims["J9-7"].owners == {root: "sensor", child: "sensor"}
    for foreign in ("project:other", "project:sensor2:node:cp:1", "script:sensor:cp:1"):
        with pytest.raises(ResourceConflict, match="sensor"):
            hw.acquire(foreign, "sensor", "J9-7", mode)
    hw.release(child)
    assert hw.res.owned_by(root) == ["J9-7"]
    hw.release(root)
    assert not hw.res.claims and not hw._open


def test_project_gpio_modes_preserve_all_claims(hw):
    root, child, sibling = "project:sensor", "project:sensor:node:cp:1", "project:sensor:n2:cp:1"
    hw.res.claim(root, "sensor", "J9-7", "gpio_out")
    hw.acquire(child, "sensor", "J9-7", "gpio_in")
    hw.acquire(sibling, "sensor", "J9-7", "gpio_in")
    for mode in ("gpio_out", "gpio_in"):
        hw.acquire(child, "sensor", "J9-7", mode)
        claim = hw.res.claims["J9-7"]
        assert claim.owner_modes == {root: "gpio_out", child: mode, sibling: "gpio_in"}
        assert claim.mode == "gpio_out"
        assert hw._open["J9-7"].mode == mode
        for foreign_mode in ("gpio_in", "gpio_out", "soft_pwm"):
            with pytest.raises(ResourceConflict):
                hw.acquire("project:other", "other", "J9-7", foreign_mode)
    hw.release(child)
    assert hw.res.owned_by(sibling) == ["J9-7"]
    hw.release(sibling)
    assert hw.res.owned_by(root) == ["J9-7"]
    hw.release(root)
    assert not hw.res.claims and not hw._open


def test_project_mode_switch_cannot_upgrade_foreign_shared_input(hw):
    hw.res.claim("project:sensor", "sensor", "J9-7", "gpio_in")
    hw.acquire("project:sensor:node:cp:1", "sensor", "J9-7", "gpio_in")
    hw.acquire("project:other", "other", "J9-7", "gpio_in")
    with pytest.raises(ResourceConflict, match="other"):
        hw.acquire("project:sensor:node:cp:1", "sensor", "J9-7", "gpio_out")
    assert hw.res.claims["J9-7"].mode == "gpio_in"
    assert hw._open["J9-7"].mode == "gpio_in"


def test_releasing_last_project_output_restores_remaining_input(hw, bench):
    root, child = "project:sensor", "project:sensor:node:cp:1"
    hw.acquire(root, "sensor", "J9-7", "gpio_in")
    hw.acquire(child, "sensor", "J9-7", "gpio_out", initial=True)
    assert bench.level("J9-7") is True
    hw.release(child)
    assert hw.res.claims["J9-7"].owner_modes == {root: "gpio_in"}
    assert hw._open["J9-7"].mode == "gpio_in"
    assert bench.level("J9-7") is False
    hw.acquire("project:other", "other", "J9-7", "gpio_in")
    with pytest.raises(ResourceConflict, match="other"):
        hw.acquire(root, "sensor", "J9-7", "gpio_out")


def test_lineage_does_not_share_incompatible_modes_or_buses(hw):
    hw.res.claim("project:sensor", "sensor", "J9-7", "soft_pwm")
    with pytest.raises(ResourceConflict):
        hw.acquire("project:sensor:node:cp:1", "sensor", "J9-7", "gpio_out")
    hw.res.claim("project:sensor", "sensor", "i2c1@0x50", "i2c")
    with pytest.raises(ResourceConflict):
        hw.res.claim("project:sensor:node:cp:1", "sensor", "i2c1@0x50", "i2c")
    hw.acquire("script:sensor:cp:1", "sensor", "J9-12", "gpio_out")
    with pytest.raises(ResourceConflict):
        hw.acquire("script:sensor:cp:2", "sensor", "J9-12", "gpio_out")


@pytest.mark.parametrize("already_open", [False, True])
def test_failed_child_mode_switch_restores_claim_and_line(hw, monkeypatch, already_open):
    root, child = "project:sensor", "project:sensor:node:cp:1"
    hw.acquire(root, "sensor", "J9-7", "gpio_out", initial=True)
    if already_open:
        hw.acquire(child, "sensor", "J9-7", "gpio_out")

    def fail(*args, **kwargs):
        raise OSError("HAL cannot open input")

    monkeypatch.setattr(hw.hal, "open_input", fail)
    with pytest.raises(OSError, match="HAL cannot open input"):
        hw.acquire(child, "sensor", "J9-7", "gpio_in")
    expected = {root: "gpio_out", child: "gpio_out"} if already_open else {root: "gpio_out"}
    assert hw.res.claims["J9-7"].owner_modes == expected
    assert hw._open["J9-7"].mode == "gpio_out"
    assert hw.value("J9-7") is True


def test_failed_child_output_restores_its_input_mode_not_reservation(hw, monkeypatch):
    root, child = "project:sensor", "project:sensor:node:cp:1"
    hw.res.claim(root, "sensor", "J9-7", "gpio_out")
    hw.acquire(child, "sensor", "J9-7", "gpio_in")

    def fail(*args, **kwargs):
        raise OSError("HAL cannot open output")

    monkeypatch.setattr(hw.hal, "open_output", fail)
    with pytest.raises(OSError, match="HAL cannot open output"):
        hw.acquire(child, "sensor", "J9-7", "gpio_out")
    assert hw.res.claims["J9-7"].owner_modes == {root: "gpio_out", child: "gpio_in"}
    assert hw._open["J9-7"].mode == "gpio_in"


def test_interface_pin_rejected_with_hint(hw):
    with pytest.raises(ResourceConflict, match="I2C3 m0.*перезагрузите"):
        hw.acquire("p1", "Тест", "J9-11", "gpio_out")
    with pytest.raises(ResourceConflict, match="не поддерживает"):
        hw.acquire("p1", "Тест", "J9-26", "gpio_out")


def test_claim_many_atomic(hw):
    with pytest.raises(ResourceConflict):
        hw.res.claim_many("p1", "Тест", {"J9-7": "gpio_out", "J9-8": "gpio_out"})
    assert hw.res.claims == {}


def test_release_all(bench, hw):
    hw.acquire("p1", "Тест", "J9-7", "gpio_out", initial=True)
    hw.i2c("p1", "Тест", "i2c1", 0x50)
    hw.release("p1")
    assert bench.level("J9-7") is False
    assert hw.res.claims == {}


def test_pwm_via_hw(bench, hw):
    led = bench.add("led", pin="J9-12")
    hw.acquire("p", "Тест", "J9-12", "soft_pwm")
    hw.set_pwm("J9-12", freq=100, duty=0.4)
    assert hw.value("J9-12") == 0.4
    assert led.state()["brightness"] == 0.4
    with pytest.raises(ValueError):
        hw.set_pwm("J9-12", freq=1000)


def test_adc(bench, hw):
    bench.add("voltage", pin="J9-26", volts=0.59)
    hw.acquire("p1", "Тест", "J9-26", "adc")
    assert hw.value("J9-26") == pytest.approx(0.59, abs=0.002)


# ---- шины (по интерфейсу из boards/napi-c.yaml, как в SDK)
def test_i2c_address_exclusive_bus_shared(bench, hw):
    bench.add("i2c_regs", bus=1, addr=0x50)
    dev = hw.i2c("a", "Дисплей", "i2c1", 0x50)
    dev.write(bytes([0, 42]))
    assert dev.write_read(b"\x00", 1) == b"\x2a"
    hw.i2c("b", "Датчик", "i2c1", 0x51)  # другой адрес на той же шине — можно
    with pytest.raises(ResourceConflict, match='i2c1@0x50 занят.*"Дисплей"'):
        hw.i2c("b", "Датчик", "i2c1", 0x50)


def test_bus_must_be_enabled(hw):
    with pytest.raises(ResourceConflict, match=r"i2c0 выключен: нужен оверлей `rk3308-i2c0` \(J9-23 SDA, J9-24 SCL\)"):
        hw.i2c("a", "x", "i2c0", 0x50)  # i2c0 по умолчанию выключен
    with pytest.raises(ResourceConflict, match="spi1 выключен: нужен оверлей `rk3308-spi1-spidev`"):
        hw.spi("a", "x", "spi1", 0)
    with pytest.raises(ResourceConflict, match=r"uart4 выключен.*\(J10-5 TX, J10-7 RX\)"):
        hw.uart("a", "x", "uart4")
    with pytest.raises(ResourceConflict, match="занят системой"):
        hw.uart("a", "x", "uart0")
    with pytest.raises(ResourceConflict, match="0x03…0x77"):
        hw.i2c("a", "x", "i2c1", 0x01)
    with pytest.raises(ResourceConflict, match="нет интерфейса 'i2c5'.*есть: i2c0, i2c1, i2c3"):
        hw.i2c("a", "x", "i2c5", 0x50)
    with pytest.raises(ResourceConflict, match="нет интерфейса 'uart1'"):
        hw.i2c("a", "x", "uart1", 0x50)  # не та шина


def test_mux_variant_resolved(board, hw):
    """Имя без варианта -> включённый вариант; ресурс — по контроллеру."""
    assert board.bus_interface("uart2").id == "uart2_m0"
    assert board.bus_interface("i2c3").id == "i2c3_m0"
    assert board.bus_interface("i2c3_m0").id == "i2c3_m0"
    with pytest.raises(Exception, match="i2c3_m1 выключен"):
        board.bus_interface("i2c3_m1")
    hw.i2c("a", "x", "i2c3", 0x20)
    assert "i2c3@0x20" in hw.res.claims
    with pytest.raises(ResourceConflict, match="i2c3@0x20 занят"):
        hw.i2c("b", "y", "i2c3_m0", 0x20)  # тот же контроллер под точным именем


def test_uart_exclusive_and_loopback(bench, hw):
    bench.add("uart_loopback", device="/dev/ttyS3")
    port = hw.uart("a", "Modbus", "uart3", baud=19200)
    port.write(b"\x01\x03")
    assert port.read(8, timeout=0.1) == b"\x01\x03"
    with pytest.raises(ResourceConflict, match="uart3 занят"):
        hw.uart("b", "Другой", "uart3")
    hw.release("a")
    hw.uart("b", "Другой", "uart3")  # после освобождения порт снова открывается


def test_spi_resource_id(board):
    from pioneer.board import load_board
    from conftest import BOARD_FILE, DEFAULT_OVERLAYS
    from pioneer.hal import BenchHAL
    from pioneer.hw import HW
    from pioneer.resources import ResourceManager
    b = load_board(BOARD_FILE, [*DEFAULT_OVERLAYS, "rk3308-spi2-spidev"])
    # spi2 конфликтует с uart1/uart2 по пинам, но тут проверяем только захват CS
    h = HW(b, BenchHAL(b), ResourceManager(b))
    h.spi("a", "x", "spi2", 0)
    assert "spi2.cs0" in h.res.claims
    with pytest.raises(ResourceConflict, match="CSN0"):
        h.spi("a", "x", "spi2", 1)


def test_softpwm_thread_on_output(bench, board):
    """SoftPwm (реальная реализация для LinuxHAL) поверх выхода стенда."""
    line = bench.open_output(board.pin("J9-7").gpio)
    changes = []
    orig = line.write
    line.write = lambda v: (changes.append(v), orig(v))
    pwm = SoftPwm(line, freq=200, duty=0.5)
    time.sleep(0.2)
    pwm.close()
    rising = sum(1 for a, b in zip(changes, changes[1:]) if not a and b)
    assert 25 <= rising <= 45  # ~40 периодов за 0.2 с
    assert changes[-1] is False
