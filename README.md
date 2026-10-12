<div align="center">

# napi-pIOneer

**Визуальное программирование железа на одноплатнике Napi-C — вместо Arduino и ESP.**
Граф нод в духе Blender, Python-скрипты, драйверы Adafruit и виртуальный стенд — прямо в браузере, а логика живёт на плате.

[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Python 3.12](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![React 19](https://img.shields.io/badge/react-19-61DAFB?logo=react&logoColor=black)
![Board](<https://img.shields.io/badge/board-Napi--C%20(RK3308)-orange>)
![OS](https://img.shields.io/badge/os-NapiLinux%200.3.x-informational)

<img src="docs/screenshots/project-editor.webp" alt="Редактор проекта napi-pIOneer: граф нод «TM1637: секундомер», плата Napi-C с гребёнками J9/J10 и проводами от пинов к нодам" width="900">

<sub>Редактор проекта: секундомер на дисплее TM1637 — интервал → счётчик → дисплей, плата с проводами от пинов J10-17/J10-18 к ноде. <a href="docs/mockups/ui-overview.webp">Концепт, с которого всё начиналось</a>.</sub>

</div>

---

## Что это

`napi-pIOneer` — сервис для платы [Napi-C](https://napiworld.ru/docs/computers/napi-c), который превращает её в удобный контроллер для реле, кнопок, датчиков, дисплеев и сервоприводов:

- 🧩 **Граф нод** — собираешь логику мышкой: кнопка → фронт → переключатель → реле. Типизированные сокеты, inline-значения, группы нод, undo/redo.
- 🔌 **Плата на канвасе** — схема гребёнок J9/J10, провода от пинов к нодам, подсветка уровней и захватов в реальном времени.
- 🐍 **Python-скрипты** — простой SDK (`Output`, `Input`, `Pwm`, `Adc`, `I2C`, `SPI`, `UART`), многофайловые проекты, свои библиотеки, зависимости из колёс.
- 🧱 **Свои блоки** — Python-класс превращается в ноду палитры; Script-нода прямо в графе.
- 🟣 **Драйверы Adafruit** — слой совместимости с CircuitPython (`board`, `busio`, `digitalio`…): пакеты `adafruit-circuitpython-*` из PyPI работают без правок, мастер «Подключить устройство» в редакторе.
- 🧪 **Виртуальный стенд** — кнопки, LED, источники напряжения и протокольные модели чипов (BME280, SHT4x, SSD1306, PCA9685) для отладки без железа.
- 📡 **Удалённое управление** — объявленные проектом команды и состояния по HTTP и MQTT 5, ответы на запросы, Home Assistant discovery.
- 🤖 **AI-помощник** (Anthropic / OpenAI / совместимый) — знает плату и ноды, предлагает изменения графа «призраками» и черновики кода; ничего не применяет сам.
- 📚 **Справка внутри** — руководства, SDK из докстрингов, схемы нод, каталог ошибок, контекстные подсказки в редакторе.
- 🛡️ **Безопасность железа** — эксклюзивные захваты пинов и шин, безопасные состояния выходов при остановке и падении, правка загрузки (`uEnv.txt`, оверлеи, подтяжки) только с предпросмотром, бэкапом и откатом.

Логика исполняется **на плате**: работает при закрытой вкладке и после перезагрузки, несколько проектов параллельно, если не делят выходы.

## Статус

Рабочий проект одного автора, активно развивается. Проверено на Napi-C с NapiLinux 0.3.1.
Что готово и что проверено на живом железе — в [аудите реализации](docs/oneshot-audit.md) и [журнале ревью](docs/review-2026-10-11.md).
В планах: симуляция для отдельного проекта и гибрид «железо + стенд» ([идея 006](docs/ideas/006-simulation-per-run.md)), камера ESP32-CAM.

## Быстрый старт

### Подготовка после скачивания

На компьютере для разработки или деплоя нужны [uv](https://docs.astral.sh/uv/), Node.js и npm.

```bash
make prepare
```

Команда создаёт или синхронизирует `backend/.venv` с Python 3.12 по `backend/uv.lock` и устанавливает зависимости фронтенда по `frontend/package-lock.json`, включая TypeScript и Vite.

### Без платы (Mac/Linux, виртуальный стенд)

```bash
make dev
```

Откроется `http://localhost:5173/pioneer/`, пароль входа по умолчанию — `pioneer` (меняется через `DEV_PASSWORD`).

### На плату Napi-C

Плата с NapiLinux 0.3.x в локальной сети, доступ по SSH-ключу:

```bash
make deploy BOARD=root@<адрес-платы> SSH_KEY=~/.ssh/<ваш-ключ>
```

Затем задайте пароль панели на плате и откройте `https://<адрес-платы>/pioneer/`:

```bash
ssh root@<адрес-платы> /mount/rfs/rw/.pioneer/bin/pioneer set-password
```

Код и данные живут в `/mount/rfs/rw/.pioneer` — переживают OTA-обновление и сброс к заводским; повторный `make deploy` восстанавливает systemd-юнит и nginx-сниппет.
При деплое отдельно подготавливаются AI-зависимости для Python 3.12 / aarch64 на плате.

### Тесты

```bash
make test
```

## Как выглядит код

Кнопка переключает LED:

```python
from pioneer.sdk import Input, Output, log

led = Output("J9-7")
button = Input("J10-2", debounce_ms=30)

@button.on_press
def toggle(event):
    led.toggle()
    log("LED", "вкл" if led.value else "выкл")
```

Датчик Adafruit — без правок драйвера:

```python
import board
from adafruit_bme280 import basic as adafruit_bme280

bme = adafruit_bme280.Adafruit_BME280_I2C(board.I2C(), address=0x76)  # I2C1: J9-3 SDA, J9-5 SCL
print(f"{bme.temperature:.1f} °C, {bme.humidity:.0f} %")
```

## Как устроено

```
Браузер (React + React Flow + Monaco)  ──HTTPS/WS──►  nginx (штатный NapiLinux)  ──►  pioneer-core (FastAPI, :8100)
                                                                                    ├─ Engine — реактивные волны графов
                                                                                    ├─ worker-процессы скриптов и Python-блоков
                                                                                    ├─ ResourceManager — кто держит пин/шину
                                                                                    └─ HAL: gpiod · iio ADC · smbus2 · spidev · pyserial · PWM
                                                                                          или BenchHAL — виртуальный стенд
```

Подробно — в [спецификации](docs/spec.md).

## Документация

|                                                                                                                 |                                             |
| --------------------------------------------------------------------------------------------------------------- | ------------------------------------------- |
| 🚀 [Начало работы](docs/sdk/start.md)                                                                           | первый скрипт, цикл событий, таймеры        |
| 🧩 [Проекты и шаблоны](docs/sdk/projects.md) · [Рецепты](docs/sdk/recipes.md)                                   | граф нод, типовые сценарии                  |
| 🐍 [Скрипты и файлы](docs/sdk/workspace.md) · [Свои блоки](docs/sdk/blocks.md)                                  | многофайловые проекты, CLI, Python-блоки    |
| 🟣 [Adafruit](docs/sdk/adafruit.md) · [Python-модули](docs/sdk/modules.md) · [Датчики I2C](docs/sdk/sensors.md) | драйверы и периферия                        |
| 🔧 [Железо](docs/sdk/hardware.md) · [Драйверы ядра](docs/sdk/drivers.md)                                        | пины, оверлеи, подтяжки, как не сжечь плату |
| 📡 [Сеть, HTTP, MQTT](docs/sdk/network.md) · [AI](docs/sdk/ai.md)                                               | удалённое управление, помощник              |
| 🩺 [Диагностика](docs/sdk/system.md) · [Ошибки](docs/sdk/errors.md)                                             | watchdog, отчёт о системе, разбор ошибок    |
| 📐 [Спецификация](docs/spec.md) · [Идеи](docs/ideas/)                                                           | архитектура и планы                         |

## ⚠️ Про железо

Napi-C — это 3.3 В логика и единицы миллиампер на пин. Реле и моторы — только модулями с драйвером, ADC принимает максимум **1.8 В** (нужен делитель), программная подтяжка на vendor-ядре не работает. Подробности и распиновка — в [руководстве по железу](docs/sdk/hardware.md).

---

## 🧡 Про Napi и lab240

Проект сделан для плат **Napi** — открытой линейки одноплатных и промышленных компьютеров на Rockchip от команды **lab240 / napilab**. Огромное спасибо им за железо, NapiLinux и открытые исходники.

**Платформа**

- 🌐 [napiworld.ru](https://napiworld.ru) — сайт, документация и блог
- 📘 [Napi-C](https://napiworld.ru/docs/computers/napi-c) — плата, на которой всё это работает
- 🛠️ [Программная поддержка](https://napiworld.ru/software/) — консоль, U-Boot, GPIO, I2C, оверлеи, Modbus
- 💾 [Прошивки](https://napiworld.ru/downloads/images/) и [релизы NapiLinux](https://download.napilinux.ru/napilinux/)
- 📦 [repo.napilab.ru](https://napiworld.ru/software/notes/napilab-repo/) — apt-репозиторий с mbusd, mbscan, modbus-slave

**GitHub**

- [lab240](https://github.com/lab240) и [napilab](https://github.com/napilab) — организации проекта
- [napilab/napi-boards](https://github.com/napilab/napi-boards) — DTS, оверлеи и распиновки плат
- [lab240/napi-board-config](https://github.com/lab240/napi-board-config) — настройка платы: EEPROM, MAC, оверлеи
- [lab240/napi-www](https://github.com/lab240/napi-www) — исходники napiworld.ru
- [lab240/napi-scripts](https://github.com/lab240/napi-scripts) — Python-примеры для датчиков и дисплеев (NapiSci)
- [lab240/mbscan](https://github.com/lab240/mbscan) · [lab240/modbus-slave](https://github.com/lab240/modbus-slave) — сканер и эмулятор Modbus RTU
- [napilab/discoverd](https://github.com/napilab/discoverd) — обнаружение устройств в сети
- [lab240/napi-armbian-build](https://github.com/lab240/napi-armbian-build) · [napi-debian-build](https://github.com/lab240/napi-debian-build) · [napi-openwrt-build](https://github.com/lab240/napi-openwrt-build) — сборки систем

<sub>napi-pIOneer — независимый проект; названия Napi и NapiLinux принадлежат их владельцам.</sub>

---

## Благодарности и сторонние компоненты

[FastAPI](https://fastapi.tiangolo.com) · [libgpiod](https://git.kernel.org/pub/scm/libs/libgpiod/libgpiod.git) · [smbus2](https://github.com/kplindegaard/smbus2) · [pyserial](https://github.com/pyserial/pyserial) · [Pillow](https://python-pillow.org) · [React](https://react.dev) · [React Flow](https://reactflow.dev) · [Monaco Editor](https://microsoft.github.io/monaco-editor/) · [Tailwind CSS](https://tailwindcss.com) · [Vite](https://vite.dev) · [Jedi](https://github.com/davidhalter/jedi) · [Adafruit CircuitPython](https://circuitpython.org)

В репозиторий включены:

- [paho-mqtt](https://github.com/eclipse-paho/paho.mqtt.python) 2.1.0 — `backend/pioneer/vendor/`, используется под **BSD-3-Clause** (двойная лицензия EPL-2.0 / EDL-1.0), лицензии — в `vendor/paho_mqtt-2.1.0.dist-info/licenses/`;
- шрифт 5×8 из Adafruit CircuitPython framebuf — `backend/pioneer/compat/circuitpython/`, MIT, см. `FONT-LICENSE`.

Драйверы Adafruit и прочие Python-пакеты в репозиторий не входят — ставятся пользователем из PyPI под их собственными лицензиями.

## Лицензия

[MIT](LICENSE) © 2026 MK
