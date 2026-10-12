BOARD ?= root@192.168.1.161
SSH_KEY ?= ~/.ssh/mkzev_rsa
APP_DIR := /mount/rfs/rw/.pioneer/app
PY := uv run --no-sync --python 3.12  # вызывается из backend/ после make prepare

.PHONY: help prepare dev core ui build test lint format deploy reference vendor-ai

help: ## Помощь
	@awk 'BEGIN {FS = ":.*##"; printf "\nДоступные цели:\n"} /^[a-zA-Z0-9_.-]+:.*##/ {printf "  %-14s %s\n", $$1, $$2} END {printf "\n"}' $(MAKEFILE_LIST)

dev:  ## core (mock-HAL на Mac) :8100 + Vite :5173 -> http://localhost:5173/pioneer/
	$(MAKE) -j2 core ui

prepare:  ## зависимости для разработки и деплоя после скачивания проекта
	cd backend && uv sync --locked --python 3.12
	cd frontend && npm ci --include=dev

DEV_PASSWORD ?= pioneer

core:  ## при первом запуске задаёт пароль входа $(DEV_PASSWORD) в .devdata
	cd backend && echo "$(DEV_PASSWORD)" | $(PY) python -m pioneer set-password --if-missing --stdin
	cd backend && $(PY) python -m pioneer

ui:
	cd frontend && npm run dev

build:
	cd frontend && npm run build

test:
	cd backend && $(PY) pytest -q
	cd frontend && npm run typecheck
	cd frontend && npm test

reference:  ## справочник SDK в JSON (для статического сайта; UI берёт его из /api/help)
	cd backend && $(PY) python ../tools/gen_sdk_reference.py ../sdk-reference.json

vendor-ai:  ## SDK + cp312/aarch64 wheels, separately from system Python
	cd backend && uv pip install --python .venv/bin/python --target pioneer/vendor_ai --no-deps --python-platform aarch64-manylinux_2_28 --python-version 3.12 --only-binary :all: -r ../deploy/ai-requirements.txt

deploy: build vendor-ai  ## код на плату (tar по ssh — rsync на плате нет) + идемпотентный install.sh
	COPYFILE_DISABLE=1 tar czf - --exclude __pycache__ backend/pioneer boards deploy docs/sdk frontend/dist modules/examples tools/media-test-server.py \
		| ssh -i $(SSH_KEY) $(BOARD) 'set -e; mkdir -p $(APP_DIR).new && tar xzf - -C $(APP_DIR).new 2>/dev/null; \
		  rm -rf $(APP_DIR).old; [ -d $(APP_DIR) ] && mv $(APP_DIR) $(APP_DIR).old; mv $(APP_DIR).new $(APP_DIR)'
	ssh -i $(SSH_KEY) $(BOARD) 'sh $(APP_DIR)/deploy/install.sh'

FORMAT_PY := pioneer/ai/__init__.py pioneer/ai/attachments.py pioneer/ai/provider_worker.py pioneer/ai/providers.py \
	pioneer/ai/tools.py pioneer/api/ai.py pioneer/api/code.py pioneer/api/drivers.py \
	pioneer/api/language.py pioneer/api/modules.py pioneer/api/system.py pioneer/api/watchdog.py \
	pioneer/block_hardware.py pioneer/codetree.py pioneer/dependencies.py pioneer/drivers/sensors.py \
	pioneer/hal/hardware_pwm.py pioneer/hal/uart_hub.py pioneer/images.py pioneer/importscope.py \
	pioneer/kernel_drivers.py pioneer/kernel_packages.py pioneer/language_service.py pioneer/language_worker.py \
	pioneer/model_worker.py pioneer/module_models.py pioneer/modules.py pioneer/nodes/builtin/media.py \
	pioneer/nodes/builtin/sensors.py pioneer/overlays.py pioneer/project_bundle.py pioneer/pulls.py \
	pioneer/state_store.py pioneer/templates.py pioneer/uenv.py pioneer/watchdog.py \
	tests/test_review.py

lint:  ## проверка форматирования модулей из review #15
	cd backend && $(PY) ruff format --check $(FORMAT_PY)
	cd frontend && npm run lint

format:
	cd backend && $(PY) ruff format $(FORMAT_PY)
	cd frontend && npm run format
