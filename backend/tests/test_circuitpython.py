from pathlib import Path
import os

import pytest
from pioneer.compat.circuitpython import SHIMS, Context, activate
from pioneer.compat.hardware import CircuitPythonHW
from pioneer.adafruit import stub_wheels, validate_site
from test_api import client, anon  # noqa: F401, F811

REAL_SITE = Path(
    os.environ.get(
        "PIONEER_ADAFRUIT_TEST_SITE", "/private/tmp/pioneer-adafruit-tests/drivers/adafruit/site"
    )
)


@pytest.fixture
def circuit(hw, monkeypatch):
    monkeypatch.syspath_prepend(str(SHIMS))
    transport = CircuitPythonHW(hw, "script:test", "test")
    context = Context(lambda op, **args: transport.call(**args))
    with activate(context):
        yield transport
    transport.close()


def test_gpio_adc_pwm_pull_cleanup(circuit, hw, bench):
    import board
    import digitalio
    import analogio
    import pwmio

    assert board.J9_7 is board.GPIO2_A4
    output = digitalio.DigitalInOut(board.J9_7)
    output.switch_to_output(True)
    assert output.value
    output.deinit()
    assert not hw.res.claims
    input = digitalio.DigitalInOut(board.J9_7)
    input.pull = digitalio.Pull.UP
    assert input.value
    input.pull = digitalio.Pull.DOWN
    assert not input.value
    input.deinit()
    assert not bench._runtime_pulls.get("J9-7")
    adc = analogio.AnalogIn(board.J9_26)
    assert adc.reference_voltage == 1.8
    assert adc.value == 0
    adc.deinit()
    with pytest.warns(RuntimeWarning, match="Soft-PWM"):
        pwm = pwmio.PWMOut(board.J9_7, frequency=50, duty_cycle=32768)
    assert bench.pwm("J9-7")[1] == pytest.approx(0.5, abs=0.0001)
    assert pwm.duty_cycle == 32768
    circuit.close()
    assert not hw.res.claims


def test_buffers_lock_and_conflict(circuit, hw, bench):
    import board
    import busio

    bench.add("i2c_regs", bus=1, addr=0x76)
    i2c = board.I2C()
    assert i2c is board.I2C()
    with pytest.raises(RuntimeError, match="try_lock"):
        i2c.scan()
    assert i2c.try_lock() and not i2c.try_lock()
    i2c.writeto(0x76, bytes([4, 9, 8]))
    buf = bytearray(b"----")
    i2c.writeto_then_readfrom(0x76, bytes([4]), buf, in_start=1, in_end=3)
    assert buf == b"-\x09\x08-"
    hw.i2c("other", "other", "i2c1", 0x77)
    assert i2c.scan() == [0x76]
    with pytest.raises(Exception, match="other"):
        i2c.readfrom_into(0x77, bytearray(1))
    i2c.unlock()
    i2c.deinit()
    assert list(hw.res.claims) == ["i2c1@0x77"]
    with pytest.raises(Exception, match="оверлей"):
        busio.SPI(board.SCK, board.MOSI, board.MISO)


def test_gpio_project_reservation_lifecycle(circuit, hw, bench):
    import board
    import digitalio

    root = "project:sensor"
    circuit.owner = root + ":node"
    hw.res.claim(root, "sensor", "J9-7", "gpio_out")
    first = digitalio.DigitalInOut(board.J9_7)
    second = digitalio.DigitalInOut(board.GPIO2_A4)
    for pin in (first, second):
        pin.switch_to_output(True)
        assert pin.value
        pin.switch_to_input(digitalio.Pull.UP)
        assert pin.value
    owners = set(hw.res.claims["J9-7"].owners)
    first.deinit()
    assert set(hw.res.claims["J9-7"].owners) == owners - {root + ":node:cp:1"}
    with pytest.raises(Exception, match="sensor"):
        hw.acquire("project:other", "other", "J9-7", "gpio_out")
    second.deinit()
    assert hw.res.claims["J9-7"].owners == {root: "sensor"}
    assert not bench._runtime_pulls.get("J9-7")
    hw.release(root)
    assert not hw.res.claims and not hw._open


@pytest.mark.parametrize("chip", ["bme280", "sht4x", "ssd1306", "pca9685"])
def test_real_driver(circuit, bench, monkeypatch, chip):
    site = REAL_SITE
    if not site.exists():
        pytest.skip("Run real wheel installation smoke first")
    monkeypatch.syspath_prepend(str(site))
    monkeypatch.syspath_prepend(str(SHIMS))
    import board

    model = bench.add(chip)
    if chip == "bme280":
        from adafruit_bme280 import basic

        device = basic.Adafruit_BME280_I2C(board.I2C(), address=0x76)
        assert device.temperature == pytest.approx(25, abs=0.01)
        assert device.humidity == pytest.approx(50, abs=0.02)
        assert device.pressure == pytest.approx(1013.25, abs=0.01)
        bench.action(model.id, "set", {"temperature": -12, "humidity": 81, "pressure": 980})
        assert device.temperature == pytest.approx(-12, abs=0.01)
        assert device.humidity == pytest.approx(81, abs=0.02)
        assert device.pressure == pytest.approx(980, abs=0.01)
    elif chip == "sht4x":
        import adafruit_sht4x

        device = adafruit_sht4x.SHT4x(board.I2C())
        assert device.serial_number == 0x12345678
        assert device.measurements == pytest.approx((25, 50), abs=0.01)
    elif chip == "ssd1306":
        import adafruit_ssd1306

        device = adafruit_ssd1306.SSD1306_I2C(128, 64, board.I2C())
        device.fill(0)
        device.text("Pioneer", 0, 0, 1, font_name=board.FONT5X8)
        device.show()
        assert model.state()["lit_pixels"] > 20
        assert bytes(model.buffer) == bytes(device.buffer[1:])
    else:
        import adafruit_pca9685

        device = adafruit_pca9685.PCA9685(board.I2C())
        device.frequency = 50
        device.channels[3].duty_cycle = 32768
        assert model.state()["frequency"] == pytest.approx(50, abs=1)
        assert model.state()["channels"][3] == 32768
    board.I2C().deinit()
    from pioneer.adafruit import Drivers

    Drivers(REAL_SITE.parent.parent.parent).record_test(
        ["adafruit-circuitpython-" + chip, "adafruit-circuitpython-busdevice"],
        "bench",
        "Unmodified PyPI driver protocol assertions passed",
    )


def test_unsafe_blinka_rejected(tmp_path):
    stub_wheels(tmp_path / "wheels")
    assert len(list((tmp_path / "wheels").glob("*.whl"))) == 2
    (tmp_path / "board.py").write_text("raise RuntimeError()")
    with pytest.raises(ValueError, match="board"):
        validate_site(tmp_path)


def link_drivers(data):
    source = REAL_SITE
    if not source.exists():
        pytest.skip("Requires real Adafruit wheels")
    target = data / "drivers/adafruit/site"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source.resolve(), target_is_directory=True)


def test_actual_script_worker_and_cleanup(tmp_path, hw, bench):
    from conftest import wait_for
    from pioneer.scripts import ScriptManager
    from pioneer.adafruit_templates import snippet

    link_drivers(tmp_path)
    bench.add("bme280")
    manager = ScriptManager(tmp_path, hw)
    script = manager.create("real BME280", snippet("bme280", "i2c1", 0x76))
    try:
        manager.start(script.id)
        assert wait_for(lambda: manager.status(script.id).state in ("error", "exited"), 8)
        assert manager.status(script.id).state == "exited", manager.log_tail(script.id)
        assert any("25.0 50.0" in item["text"] for item in manager.log_tail(script.id))
        assert not hw.res.claims
    finally:
        manager.stop_all()


def test_generated_block_worker_timer_and_cleanup(tmp_path, hw, bench):
    from conftest import wait_for
    from pioneer.userblocks import BlockManager
    from pioneer.projects import ProjectManager
    from pioneer.nodes import registry
    from pioneer.adafruit_templates import block_template

    link_drivers(tmp_path)
    model = bench.add("bme280")
    reg = registry().copy()
    blocks = BlockManager(tmp_path, reg)
    manager = ProjectManager(tmp_path, hw, reg)
    try:
        block = blocks.create("BME280", block_template("bme280", "i2c1", 0x76))
        assert block["error"] is None, block
        project = manager.create("Adafruit")
        manager.apply_ops(
            project.id,
            [
                {
                    "op": "add_node",
                    "id": "sensor",
                    "type": block["types"][0],
                    "params": {"period": 0.1},
                }
            ],
        )
        manager.start(project.id)
        runtime = manager.engine.runtimes[project.id]
        assert runtime.sample("sensor", "temperature").value == pytest.approx(25, abs=0.01)
        bench.action(model.id, "set", {"temperature": 33})
        assert wait_for(
            lambda: abs((runtime.sample("sensor", "temperature").value or 0) - 33) < 0.01, 3
        )
        manager.stop(project.id)
        assert not hw.res.claims
    finally:
        manager.close()


def test_manager_use_confirmation_and_lock(tmp_path, monkeypatch):
    import json
    from pioneer.adafruit import Drivers
    from pioneer.compat.circuitpython import check_lock
    from pioneer.codetree import CodeTree

    root = tmp_path / "scripts/abcdef"
    CodeTree(root).write("main.py", b"import adafruit_bme280\n")
    CodeTree(root).set_meta({"name": "Sensor"})
    site = tmp_path / "drivers/adafruit/site"
    site.mkdir(parents=True)
    state = {
        "requested": {"adafruit-circuitpython-bme280": "2.0"},
        "versions": {"adafruit-circuitpython-bme280": "2.0"},
    }
    (site / ".pioneer-adafruit.json").write_text(json.dumps(state))
    manager = Drivers(tmp_path)
    result = manager.start("adafruit-circuitpython-bme280", remove=True)
    assert result["confirmation_required"]
    assert result["uses"][0]["name"] == "Sensor"
    assert manager.job is None
    manager.bind(root, "adafruit-circuitpython-bme280")
    check_lock(root)
    from pioneer.compat.circuitpython import site as driver_site

    nested = root / "src/device"
    nested.mkdir(parents=True)
    assert driver_site(nested) == site
    check_lock(nested)
    state["versions"]["adafruit-circuitpython-bme280"] = "3.0"
    (site / ".pioneer-adafruit.json").write_text(json.dumps(state))
    with pytest.raises(RuntimeError, match="lock=2.0"):
        check_lock(root)


def test_oserror_semantics_over_rpc(circuit, bench, monkeypatch):
    source = REAL_SITE
    if not source.exists():
        pytest.skip("Requires real Adafruit wheels")
    monkeypatch.syspath_prepend(str(source))
    import board
    from adafruit_bus_device.i2c_device import I2CDevice

    with pytest.raises(ValueError, match="No I2C device"):
        I2CDevice(board.I2C(), 0x35)


def test_open_drain(circuit, hw, bench):
    import board
    import digitalio

    bench.add("level", pin="J9-7", value=True)
    pin = digitalio.DigitalInOut(board.J9_7)
    pin.switch_to_output(True, digitalio.DriveMode.OPEN_DRAIN)
    assert pin.value is True
    pin.value = False
    assert pin.value is False
    pin.value = True
    assert pin.value is True
    pin.deinit()
    assert not hw.res.claims
    assert not bench._runtime_pulls.get("J9-7")


def test_static_diagnostics_unknown_pin_driver_and_overlay(tmp_path, board):
    from dataclasses import asdict
    from pioneer.compat.diagnostics import diagnose

    data = {
        "pins": [
            dict(asdict(p), function=board.function(p.id), claim=None) for p in board.pins.values()
        ],
        "interfaces": [
            dict(asdict(i), kind=i.kind, enabled=board.interface_enabled(i.id))
            for i in board.interfaces.values()
        ],
    }
    root = tmp_path / "scripts/abcdef"
    root.mkdir(parents=True)
    code = "import board\nimport adafruit_bme280\nimport busio\nfrom digitalio import DigitalInOut\nx = board.J9_999\ni2c = busio.I2C(board.J9_24, board.J9_23)\n"
    items = diagnose(code, root, data, {})
    assert any(i["fix"] == "install:adafruit-circuitpython-bme280" for i in items)
    assert any("J9_999" in i["message"] for i in items)
    assert any("I2C" in i["message"] and i["fix"] == "board" for i in items)


def test_pull_diagnostics_plan_hint(tmp_path, board):
    from dataclasses import asdict
    from pioneer.compat.diagnostics import diagnose

    root = tmp_path / "scripts/test"
    root.mkdir(parents=True)
    data = {
        "hal": "linux",
        "pins": [dict(asdict(p), actual_pull="down") for p in board.pins.values()],
    }
    code = "import board, digitalio\np = digitalio.DigitalInOut(board.J9_7)\np.switch_to_input(pull=digitalio.Pull.UP)\n"
    assert any(i["fix"] == "pull:J9-7:up" for i in diagnose(code, root, data, {}))
    data["hal"] = "bench"
    assert not any(i["fix"].startswith("pull:") for i in diagnose(code, root, data, {}) if i["fix"])


def test_module_export_import_and_model(tmp_path, hw, bench):
    from pioneer.adafruit import Drivers
    from pioneer.adafruit_bundle import export
    from pioneer.modules import Modules
    from pioneer.nodes import registry
    from pioneer.hal.models import MODEL_KINDS

    link_drivers(tmp_path)
    drivers = Drivers(tmp_path)
    modules = Modules(tmp_path, registry().copy(), bench)
    result = modules.install(export(drivers, "bme280", "i2c1", 0x76, "bme_adafruit", 1), True)
    assert result["model"]["params"]["bus"]["default"] == 1
    assert result["types"] and result["model_kind"]
    model = bench.add(
        result["model_kind"], bus=1, addr=0x76, temperature=19, humidity=44, pressure=1010
    )
    try:
        assert model.state()["temperature"] == 19
        model.i2c_write(1, 0x76, b"\xd0")
        assert model.i2c_read(1, 0x76, 1) == b"\x60"
        model.action("set", {"temperature": 32})
        assert model.state()["temperature"] == 32
    finally:
        bench.remove(model.id)
        modules.remove("bme_adafruit", 1)
    assert result["model_kind"] not in MODEL_KINDS


def test_spi_and_uart_rpc_buffers(circuit, bench, board):
    import busio
    import board as pins

    original = list(board.overlays)
    board.overlays[:] = [o for o in original if o not in ("rk3308-uart1", "rk3308-uart2-m0")] + [
        "rk3308-spi2-spidev"
    ]
    bench.add("spi_loopback", bus=2, cs=0)
    spi = busio.SPI(pins.SCK, pins.MOSI, pins.MISO)
    assert spi.try_lock()
    spi.configure(baudrate=200000, polarity=1, phase=1)
    target = bytearray(b"----")
    spi.write_readinto(b"1234", target, out_start=1, out_end=3, in_start=1, in_end=3)
    assert target == b"-23-"
    spi.unlock()
    spi.deinit()
    board.overlays[:] = original
    bench.add("uart_loopback", device="/dev/ttyS2")
    uart = pins.UART()
    uart.baudrate = 115200
    assert uart.write(b"test\n") == 5
    from conftest import wait_for

    assert wait_for(lambda: uart.in_waiting == 5)
    assert uart.readline() == b"test\n"
    assert uart.read(1) is None
    uart.deinit()
    assert not circuit.hw.res.claims


def test_connect_api_unique_blocks_preview_and_atomic_locks(client, tmp_path):  # noqa: F811
    import json
    from pioneer.api.app import API

    link_drivers(tmp_path)
    manager = client.app.state.adafruit
    (manager.root / "catalog.json").write_text("[]")
    versions = manager.state()["versions"]
    manager.record_test(["adafruit-circuitpython-bme280"], "bench", "real driver protocol test")
    catalog = client.get(API + "/adafruit").json()
    assert any(
        d["compatibility"] == "на стенде" for d in catalog["drivers"] if d["chip"] == "bme280"
    )
    assert client.post(API + "/adafruit/change", json={"package": "../bad"}).status_code == 409
    for i in range(2):
        r = client.post(API + "/adafruit/connect", json={"chip": "bme280", "block": True})
        assert r.status_code == 200, r.text
        block = r.json()["block"]
        assert not block["error"]
        if i == 0:
            previous = block["types"]
        else:
            assert block["types"] != previous
    s = client.app.state.scripts.create("preview", "")
    body = {"chip": "lcd", "address": 0x27, "kind": "scripts", "id": s.id}
    assert client.post(API + "/adafruit/connect", json=body).status_code == 200
    lock = tmp_path / "scripts" / s.id / "adafruit.lock.json"
    assert not lock.exists()
    assert client.post(API + "/adafruit/connect", json={**body, "bind": True}).status_code == 200
    assert json.loads(lock.read_text())["drivers"] == {
        p: versions[p] for p in ("adafruit-circuitpython-pcf8574", "adafruit-circuitpython-charlcd")
    }
    assert (
        client.post(
            API + "/adafruit/module", json={"chip": "bme280", "module_id": "../oops"}
        ).status_code
        == 422
    )


def test_official_example_cache_and_source_validation(tmp_path, monkeypatch):
    import json
    from pioneer.adafruit import Drivers

    manager = Drivers(tmp_path)
    package = "adafruit-circuitpython-bme280"
    monkeypatch.setattr(manager, "state", lambda: {"versions": {package: "2.6.32"}})
    monkeypatch.setattr(
        manager,
        "catalog",
        lambda: {
            "drivers": [
                {
                    "package": package,
                    "url": "https://github.com/adafruit/Adafruit_CircuitPython_BME280",
                }
            ]
        },
    )
    source = "https://raw.githubusercontent.com/adafruit/Adafruit_CircuitPython_BME280/main/examples/bme280_simpletest.py"
    calls = []

    def fetch(url):
        calls.append(url)
        if url == source:
            return b"import board\ni2c = board.I2C()\n"
        return json.dumps(
            [{"type": "file", "name": "bme280_simpletest.py", "size": 30, "download_url": source}]
        ).encode()

    monkeypatch.setattr("pioneer.adafruit.fetch", fetch)
    assert "board.I2C()" in manager.example(package)["code"]
    assert manager.example(package)["source"] == source
    assert len(calls) == 2
    with pytest.raises(ValueError):
        manager.example("../../oops")
