"""Python-блоки: схема из отдельного процесса, регистрация, исполнение в worker проекта."""
import time

import pytest

from conftest import wait_for
from pioneer.nodes import registry
from pioneer.projects import ProjectManager
from pioneer.userblocks import TEMPLATE, BlockManager
from test_api import anon, client  # noqa: F401
from pioneer.api.app import API


@pytest.fixture
def env(tmp_path, hw):
    reg = registry().copy()
    bm = BlockManager(tmp_path, reg)
    pm = ProjectManager(tmp_path, hw, reg)
    yield bm, pm, reg
    pm.close()


def test_template_registers(env):
    bm, _, reg = env
    b = bm.create("Гистерезис")
    assert b["error"] is None and b["types"] == ["user.hysteresis"]
    nt = reg.get("user.hysteresis@1")
    assert [i.name for i in nt.inputs.values()] == ["value", "low", "high"]
    assert nt.params["invert"].kind == "bool" and nt.outputs["out"].type == "Bool"
    assert "Включает выход" in nt.doc and nt.category == "Мои блоки"
    assert "user.hysteresis@1" not in registry().types  # встроенный реестр не тронут


def test_error_keeps_previous_version(env):
    bm, _, reg = env
    b = bm.create("Гистерезис")
    r = bm.save(b["id"], TEMPLATE.replace("def setup(self):", "def setup(self)"))
    assert "синтаксис, строка" in r["error"] and "user.hysteresis@1" in reg.types
    assert "def setup(self)\n" in bm.get(b["id"])["code"]  # код не потерян
    r = bm.save(b["id"], TEMPLATE.replace("In(Number, 0", "In(complex, 0"))
    assert "тип сокета" in r["error"]


def test_hysteresis_block_in_graph(env):
    """Фаза 2, «Готово, когда»: свой Гистерезис используется в графе без рестарта сервиса."""
    bm, pm, _ = env
    bm.create("Гистерезис")
    p = pm.create("С блоком")
    pm.apply_ops(p.id, [
        {"op": "add_node", "id": "n", "type": "builtin.ui_number"},
        {"op": "add_node", "id": "h", "type": "user.hysteresis", "inputs": {"low": 30, "high": 35}},
        {"op": "add_node", "id": "w", "type": "builtin.watch"},
        {"op": "connect", "from": ["n", "value"], "to": ["h", "value"]},
        {"op": "connect", "from": ["h", "out"], "to": ["w", "value"]},
    ])
    pm.start(p.id)
    rt = pm.engine.runtimes[p.id]
    for v, expect in ((40, True), (32, True), (20, False)):
        pm.ui(p.id, "n", v)
        assert rt.sample("h", "out").value is expect
    assert str(rt.sample("h", "out").origin) == "ui:n"


TIMER_BLOCK = '''from pioneer.block import node, Node, Out, Param, Event, Number

@node(id="ticker", title="Тикер")
class Ticker(Node):
    period = Param(float, 0.03, title="Период")
    tick = Out(Event)
    n = Out(Number)

    def setup(self):
        self.count = 0
        self.every(self.period, self.fire)

    def fire(self):
        self.count += 1
        self.n.set(self.count)
        self.tick.emit(self.count)
        if self.count == 2:
            self.log("второй тик")
'''


def test_block_timers_and_log(env):
    bm, pm, _ = env
    bm.create("Тикер", TIMER_BLOCK)
    p = pm.create("Тики")
    pm.apply_ops(p.id, [{"op": "add_node", "id": "t", "type": "user.ticker"},
                        {"op": "add_node", "id": "c", "type": "builtin.counter"},
                        {"op": "connect", "from": ["t", "tick"], "to": ["c", "up"]}])
    pm.start(p.id)
    rt = pm.engine.runtimes[p.id]
    assert wait_for(lambda: (rt.sample("c", "count").value or 0) >= 3, 2)
    assert rt.sample("t", "n").origin.kind == "block"
    assert wait_for(lambda: any("второй тик" in line["text"] for line in pm.log_tail(p.id)), 1)


GPIO_BLOCK = '''from pioneer.block import node, Node, Out, Param, Bool

@node(id="gpio_sensor", title="GPIO sensor")
class GPIOSensor(Node):
    pin = Param("pin", "J9-7", modes=("gpio_out",), required=True)
    fail = Param(bool, False)
    value = Out(Bool)

    def setup(self):
        import board
        import digitalio
        self.gpio = digitalio.DigitalInOut(getattr(board, self.pin.replace("-", "_")))
        self.gpio.switch_to_output(True)
        self.gpio.switch_to_input(digitalio.Pull.UP)
        self.gpio.switch_to_output(True)
        self.value.set(self.gpio.value)
        if self.fail:
            raise ValueError("setup failed after GPIO open")
'''


@pytest.mark.parametrize("fail", [False, True])
def test_block_pin_reservation_circuitpython_start_and_cleanup(env, hw, bench, fail):
    bm, pm, _ = env
    block = bm.create("GPIO sensor", GPIO_BLOCK)
    assert block["error"] is None, block
    project = pm.create("sensor")
    pm.apply_ops(project.id, [
        {"op": "add_node", "id": "sensor", "type": block["types"][0],
         "params": {"fail": fail}},
    ])
    if fail:
        with pytest.raises(Exception, match="setup failed after GPIO open"):
            pm.start(project.id)
        assert pm.status(project.id)["state"] == "error"
        assert project.id not in pm.engine.runtimes
        pm.apply_ops(project.id, [
            {"op": "set_param", "node": "sensor", "name": "fail", "value": False},
        ])
    else:
        pm.start(project.id)
        runtime = pm.engine.runtimes[project.id]
        assert runtime.sample("sensor", "value").value is True
        assert pm._foreign_claims(project.id) == {}
        foreign = pm.create("sensor")
        pm.apply_ops(foreign.id, [
            {"op": "add_node", "id": "sensor", "type": block["types"][0]},
        ])
        with pytest.raises(Exception, match="sensor"):
            pm.start(foreign.id)
        pm.stop(project.id)
    assert not hw.res.claims and not hw._open
    assert not bench._runtime_pulls.get("J9-7")
    pm.start(project.id)
    assert pm.engine.runtimes[project.id].sample("sensor", "value").value is True
    pm.stop(project.id)
    assert not hw.res.claims and not hw._open


def test_hung_block_isolated_and_dirty_after_save(env):
    bm, pm, _ = env
    slow = TEMPLATE.replace("    def on_change(self, changed):\n", "    def on_change(self, changed):\n        import time\n        time.sleep(3)\n")
    b = bm.create("Тормоз", slow)
    assert b["error"] is None, b["error"]
    p = pm.create("Зависание")
    pm.apply_ops(p.id, [{"op": "add_node", "id": "n", "type": "builtin.ui_number"},
                        {"op": "add_node", "id": "h", "type": "user.hysteresis"},
                        {"op": "add_node", "id": "ok", "type": "builtin.not"},
                        {"op": "connect", "from": ["n", "value"], "to": ["h", "value"]}])
    pm.start(p.id)
    t0 = time.monotonic()
    pm.ui(p.id, "n", 1)
    assert time.monotonic() - t0 < 2  # таймаут 1 с, а не 3
    rt = pm.engine.runtimes[p.id]
    assert "нет ответа" in rt.node_state("h")["error"] and rt.sample("ok", "out").value is True
    assert not pm.status(p.id)["dirty"]
    time.sleep(0.01)
    bm.save(b["id"], TEMPLATE)
    assert pm.status(p.id)["dirty"]  # новая версия блока — через «Применить»
    pm.restart(p.id)
    pm.ui(p.id, "n", 40)
    assert pm.engine.runtimes[p.id].sample("h", "out").value is True


def test_blocks_api(client):
    r = client.post(f"{API}/blocks", json={"name": "Мой"}).json()
    assert r["types"] == ["user.hysteresis"]
    assert client.post(f"{API}/blocks/validate", json={"name": "", "code": "x = ("}).json()["ok"] is False
    types = {t["type"] for t in client.get(f"{API}/node-types").json()["types"]}
    assert "user.hysteresis@1" in types
    assert client.delete(f"{API}/blocks/{r['id']}").status_code == 200
    types = {t["type"] for t in client.get(f"{API}/node-types").json()["types"]}
    assert "user.hysteresis@1" not in types


SCRIPT2 = '''from pioneer.block import node, Node, In, Out, Number, Bool

@node(id="s", title="Порог")
class S(Node):
    x = In(Number, 0)
    hot = Out(Bool)

    def on_change(self, changed):
        self.hot.set(self.x > 10)
'''


def test_script_node(env):
    bm, pm, reg = env
    p = pm.create("Скрипт")
    r = pm.apply_ops(p.id, [
        {"op": "add_node", "id": "n", "type": "builtin.ui_number"},
        {"op": "add_node", "id": "s", "type": "builtin.script"},
        {"op": "add_node", "id": "w", "type": "builtin.watch"},
        {"op": "connect", "from": ["n", "value"], "to": ["s", "value"]},
        {"op": "connect", "from": ["s", "out"], "to": ["w", "value"]},
    ])
    assert r["validation"]["can_start"]
    full = pm.get(p.id)
    assert [i["name"] for i in full["node_types"]["s"]["inputs"]] == ["value"]
    pm.start(p.id)
    pm.ui(p.id, "n", 21)
    assert pm.engine.runtimes[p.id].sample("s", "out").value == 42
    pm.stop(p.id)

    # новый код: сокеты другие — провода к исчезнувшим сняты, undo возвращает
    r = pm.apply_ops(p.id, [{"op": "set_param", "node": "s", "name": "code", "value": SCRIPT2}])
    assert pm.get(p.id)["graph"]["edges"] == []
    pm.apply_ops(p.id, [{"op": "connect", "from": ["n", "value"], "to": ["s", "x"]},
                        {"op": "connect", "from": ["s", "hot"], "to": ["w", "value"]}])
    pm.start(p.id)
    pm.ui(p.id, "n", 11)
    assert pm.engine.runtimes[p.id].sample("s", "hot").value is True
    pm.stop(p.id)
    undo = pm.apply_ops(p.id, [{"op": "disconnect", "to": ["s", "x"]}, {"op": "disconnect", "to": ["w", "value"]}])
    pm.apply_ops(p.id, r["inverse"])
    edges = {tuple(map(tuple, (e["from"], e["to"]))) for e in pm.get(p.id)["graph"]["edges"]}
    assert edges == {(("n", "value"), ("s", "value")), (("s", "out"), ("w", "value"))}
    assert undo

    # ошибка в коде: сохраняется, но не запускается
    r = pm.apply_ops(p.id, [{"op": "set_param", "node": "s", "name": "code", "value": "x = ("}])
    assert not r["validation"]["can_start"] and "скрипт: синтаксис" in r["validation"]["incomplete"][0]["message"]


def test_worker_waits_for_ready_before_handler_budget(tmp_path, monkeypatch):
    import subprocess
    import sys
    import pioneer.userblocks as workers

    original = subprocess.Popen

    def delayed(args, **kwargs):
        if args == [sys.executable, "-m", "pioneer.blockhost"]:
            args = [sys.executable, "-c", "import time,runpy; time.sleep(0.3); runpy.run_module('pioneer.blockhost',run_name='__main__')"]
        return original(args, **kwargs)

    monkeypatch.setattr(workers.subprocess, "Popen", delayed)
    path = tmp_path / "block.py"
    path.write_text(TEMPLATE)
    host = workers.BlockHostClient(lambda *a: None, lambda *a: None)
    try:
        assert host._ready.is_set() and host.dead is None
        host.call({"op": "load", "file": str(path), "block": "hysteresis", "node": "h", "params": {}}, timeout=0.2)
        actions = host.call({"op": "call", "method": "on_change", "node": "h", "inputs": {"value": 40, "low": 30, "high": 35}, "changed": ["value"]}, timeout=0.2)
        assert ["set", "out", True] in actions
    finally:
        host.close()


def test_worker_startup_timeout_reaps_process(monkeypatch):
    import subprocess
    import sys
    import pioneer.userblocks as workers
    from pioneer.engine.node import NodeError

    original = subprocess.Popen
    started = []

    def stuck(args, **kwargs):
        proc = original([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        started.append(proc)
        return proc

    monkeypatch.setattr(workers.subprocess, "Popen", stuck)
    from pioneer.limits import Limits
    monkeypatch.setattr(workers.limits, "get", lambda: Limits(block_worker_start_s=0.05))
    with pytest.raises(NodeError, match="не готов"):
        workers.BlockHostClient(lambda *a: None, lambda *a: None)
    assert started[0].poll() is not None


def test_import_scope_reuses_path_classification(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from pioneer.importscope import ImportScope

    root = tmp_path / "blocks" / "one"
    root.mkdir(parents=True)
    scope = ImportScope(root)
    paths = [str(root / "helper.py"), str(tmp_path / "elsewhere.py")]
    original = Path.resolve
    calls = []

    def resolve(path, *args, **kwargs):
        calls.append(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    for _ in range(3):
        assert scope.local(SimpleNamespace(__file__=paths[0]))
        assert not scope.local(SimpleNamespace(__file__=paths[1]))
    assert calls == paths
