# ==============================================================================
# SISTEMA COUTOS CONVENIÊNCIA — MAKEFILE DE OPERAÇÃO, MONITORAMENTO E FIRMWARE
# ==============================================================================
# Execute `make` ou `make help` para ver todos os comandos disponíveis.
# ==============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Parâmetros configuráveis por linha de comando (ex: make esp-upload PORT=/dev/ttyACM0)
PORT ?= /dev/ttyUSB0
FQBN ?= esp32:esp32:esp32
DEVICE ?= camara01
MIN ?= -10.0
MAX ?= 5.0
LIMIT ?= 10
ARDUINO_CLI := $(shell command -v arduino-cli 2>/dev/null || echo "$(HOME)/.local/bin/arduino-cli")
FIRMWARE_DIR := firmware/esp32_freezer_monitor
API_URL := http://localhost:8000
API_KEY := $(shell grep -E '^API_SECRET_KEY=' .env 2>/dev/null | cut -d= -f2- || echo "troque_por_uma_chave_aleatoria_segura")

.PHONY: help setup ip up down restart ps \
        logs logs-backend logs-mqtt logs-whatsapp mqtt-watch \
        status telemetry alerts devices set-range test-whatsapp db-shell db-users oracle-firewall vps-firewall-ddos \
        esp-compile esp-build-bin esp-upload esp-ota esp-monitor \
        simulate simulate-alert simulate-lwt

help: ## Exibe esta lista de comandos úteis
	@echo ""
	@echo "❄️  Sistema Coutos Conveniência — Comandos Úteis (Make)"
	@echo "================================================================================"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
	@echo "================================================================================"
	@echo "Exemplos com parâmetros:"
	@echo "  make set-range MIN=-8 MAX=2 DEVICE=camara01"
	@echo "  make esp-upload PORT=/dev/ttyUSB0"
	@echo "  make telemetry LIMIT=5"
	@echo ""

# ------------------------------------------------------------------------------
# 1. CONFIGURAÇÃO INICIAL E REDE
# ------------------------------------------------------------------------------
setup: ## Cria .env e secrets.h a partir dos templates (se não existirem)
	@if [ ! -f .env ]; then \
		cp .env.example .env; \
		echo "✅ Arquivo .env criado a partir de .env.example"; \
	else \
		echo "ℹ️  Arquivo .env já existe (mantido)."; \
	fi
	@if [ ! -f $(FIRMWARE_DIR)/secrets.h ]; then \
		cp $(FIRMWARE_DIR)/secrets.example.h $(FIRMWARE_DIR)/secrets.h; \
		echo "✅ Arquivo $(FIRMWARE_DIR)/secrets.h criado. Edite-o com seu Wi-Fi e IP!"; \
	else \
		echo "ℹ️  Arquivo $(FIRMWARE_DIR)/secrets.h já existe (mantido)."; \
	fi
	@$(MAKE) ip

ip: ## Mostra o IP local desta máquina na rede para usar no secrets.h do ESP32
	@echo "🌐 Endereço(s) IP desta máquina na rede local:"
	@ip -4 -br addr show scope global | awk '{printf "   Interface %-12s -> IP: %s\n", $$1, $$3}'

# ------------------------------------------------------------------------------
# 2. DOCKER COMPOSE (CICLO DE VIDA DOS CONTÊINERES)
# ------------------------------------------------------------------------------
up: ## Sobe todos os 5 contêineres Docker em background (com build)
	docker compose up -d --build
	@echo "✅ Contêineres iniciados! Swagger UI: http://localhost:8000/docs | Evolution API: http://localhost:8081"

down: ## Para e remove os contêineres da aplicação (preserva os dados dos volumes)
	docker compose down

restart: ## Reinicia todos os contêineres
	docker compose restart

ps: ## Lista o status atual e portas de todos os contêineres
	docker compose ps

# ------------------------------------------------------------------------------
# 3. LOGS E MONITORAMENTO EM TEMPO REAL
# ------------------------------------------------------------------------------
logs: ## Acompanha os logs de TODOS os contêineres em tempo real (Ctrl+C para sair)
	docker compose logs -f --tail=50

logs-backend: ## Acompanha os logs do Backend FastAPI + Worker MQTT em tempo real
	docker compose logs -f --tail=50 backend

logs-mqtt: ## Acompanha os logs de conexões do Broker Mosquitto em tempo real
	docker compose logs -f --tail=50 mosquitto

logs-whatsapp: ## Acompanha os logs da Evolution API (WhatsApp) em tempo real
	docker compose logs -f --tail=50 evolution_api

mqtt-watch: ## Escuta ao vivo todos os pacotes MQTT publicados em loja/freezer/#
	@echo "📡 Escutando tópicos MQTT em 'loja/freezer/#' (Ctrl+C para sair)..."
	docker exec -it coutos_mosquitto mosquitto_sub -v -t "loja/freezer/#"

# ------------------------------------------------------------------------------
# 4. CONSULTAS RÁPIDAS NA API REST E BANCO DE DADOS (AUTENTICADAS VIA X-API-Key)
# ------------------------------------------------------------------------------
status: ## Verifica a saúde do sistema (PostgreSQL, Broker MQTT e Buffer SQLite)
	@curl -s -H "X-API-Key: $(API_KEY)" "$(API_URL)/api/v1/system/status" | python3 -m json.tool

devices: ## Lista os freezers/câmaras cadastrados e suas faixas ideais de temperatura
	@curl -s -H "X-API-Key: $(API_KEY)" "$(API_URL)/api/v1/devices" | python3 -m json.tool

telemetry: ## Exibe as últimas leituras de temperatura gravadas (use LIMIT=10)
	@curl -s -H "X-API-Key: $(API_KEY)" "$(API_URL)/api/v1/telemetry?limit=$(LIMIT)" | python3 -m json.tool

alerts: ## Exibe os últimos alertas térmicos e de queda de energia LWT (use LIMIT=10)
	@curl -s -H "X-API-Key: $(API_KEY)" "$(API_URL)/api/v1/alerts?limit=$(LIMIT)" | python3 -m json.tool

set-range: ## Atualiza a faixa ideal de temperatura (Ex: make set-range MIN=-10 MAX=5)
	@echo "⚙️  Atualizando faixa ideal de '$(DEVICE)' para [$(MIN) °C a $(MAX) °C]..."
	@curl -s -X PATCH "$(API_URL)/api/v1/devices/$(DEVICE)/thresholds" \
		-H "X-API-Key: $(API_KEY)" \
		-H "Content-Type: application/json" \
		-d '{"min_temp_c": $(MIN), "max_temp_c": $(MAX)}' | python3 -m json.tool

test-whatsapp: ## Envia uma mensagem de teste via Evolution API para o WhatsApp configurado
	@curl -s -X POST "$(API_URL)/api/v1/notifications/test" \
		-H "X-API-Key: $(API_KEY)" \
		-H "Content-Type: application/json" \
		-d '{"message": "✅ Teste de notificação do Sistema Coutos Conveniência!"}' | python3 -m json.tool

db-shell: ## Abre o terminal interativo SQL (psql) no banco PostgreSQL com o usuário dedicado
	docker exec -it coutos_postgres psql -U telemetry_app_user -d coutos_telemetria

db-users: ## Lista as roles dedicadas do PostgreSQL e confirma ausência de privilégio SUPERUSER
	docker exec -it coutos_postgres psql -U coutos_admin -d coutos_telemetria -c "\du"

oracle-firewall: ## Configura regras de firewall (iptables) na VM Oracle Cloud Always Free (portas 8000 e 1884)
	sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 8000 -j ACCEPT
	sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 1884 -j ACCEPT
	sudo netfilter-persistent save || true
	@echo "✅ Portas 8000 (Proxy Web) e 1884 (MQTT) liberadas no iptables da Oracle Cloud!"

vps-firewall-ddos: ## Aplica proteção DDoS no Kernel Linux (SYN Cookies, Anti-Spoofing) e iptables (portas 8000 e 1884) na VPS
	@echo "🛡️  Aplicando hardening de Kernel (sysctl) contra SYN Flood e IP Spoofing..."
	sudo sysctl -w net.ipv4.tcp_syncookies=1
	sudo sysctl -w net.ipv4.tcp_max_syn_backlog=4096
	sudo sysctl -w net.ipv4.tcp_synack_retries=2
	sudo sysctl -w net.ipv4.tcp_syn_retries=2
	sudo sysctl -w net.ipv4.tcp_fin_timeout=15
	sudo sysctl -w net.ipv4.conf.all.rp_filter=1
	sudo sysctl -w net.ipv4.conf.default.rp_filter=1
	sudo sysctl -w net.ipv4.icmp_echo_ignore_broadcasts=1
	@echo "🛡️  Aplicando limites de conexão e pacotes inválidos no iptables (INPUT e DOCKER-USER)..."
	sudo iptables -A INPUT -m conntrack --ctstate INVALID -j DROP || true
	sudo iptables -A INPUT -p tcp ! --syn -m conntrack --ctstate NEW -j DROP || true
	sudo iptables -I DOCKER-USER -m conntrack --ctstate INVALID -j DROP 2>/dev/null || true
	sudo iptables -I DOCKER-USER -p tcp --dport 1884 -m connlimit --connlimit-above 15 --connlimit-mask 32 -j DROP 2>/dev/null || true
	sudo iptables -I DOCKER-USER -p tcp --dport 8000 -m connlimit --connlimit-above 30 --connlimit-mask 32 -j DROP 2>/dev/null || true
	sudo netfilter-persistent save 2>/dev/null || true
	@echo "✅ Proteção DDoS de Kernel e Firewall aplicada com sucesso!"

# ------------------------------------------------------------------------------
# 5. ESP32 FIRMWARE (COMPILAÇÃO, UPLOAD USB, OTA REMOTO SEGURO E MONITOR SERIAL)
# ------------------------------------------------------------------------------
VERSION ?= 1.1.0
BUILD_DIR := $(FIRMWARE_DIR)/build

esp-compile: ## Compila o firmware do ESP32 sem gravar na placa
	$(ARDUINO_CLI) compile --fqbn $(FQBN) $(FIRMWARE_DIR)

esp-build-bin: ## Compila e exporta o arquivo .bin pronto para atualização OTA segura
	@mkdir -p $(BUILD_DIR)
	$(ARDUINO_CLI) compile --fqbn $(FQBN) --output-dir $(BUILD_DIR) $(FIRMWARE_DIR)
	@echo "✅ Binário OTA gerado: $(BUILD_DIR)/esp32_freezer_monitor.ino.bin"
	@sha256sum $(BUILD_DIR)/esp32_freezer_monitor.ino.bin

esp-upload: ## Compila e grava o firmware no ESP32 via USB (Ex: make esp-upload PORT=/dev/ttyUSB0)
	$(ARDUINO_CLI) compile --fqbn $(FQBN) -u -p $(PORT) $(FIRMWARE_DIR)

esp-ota: esp-build-bin ## Compila o .bin e dispara atualização OTA remota assinada (HMAC-SHA256) sem cabo USB
	@echo "🔐 Enviando firmware v$(VERSION) assinado para '$(DEVICE)' via Backend OTA..."
	@OTA_SECRET=$$(grep -E '^OTA_SHARED_SECRET=' .env 2>/dev/null | cut -d= -f2- || echo "coutos_ota_hmac_secret_2026"); \
	curl -s -X POST "$(API_URL)/api/v1/devices/$(DEVICE)/ota/deploy?version=$(VERSION)&filename=esp32_freezer_monitor.ino.bin" \
		-H "Content-Type: application/octet-stream" \
		-H "X-OTA-Secret: $$OTA_SECRET" \
		--data-binary "@$(BUILD_DIR)/esp32_freezer_monitor.ino.bin" | python3 -m json.tool

esp-monitor: ## Abre o monitor serial do ESP32 em 115200 baud sem travar o pino RTS/DTR
	@echo "🔌 Monitorando porta serial $(PORT) em 115200 baud (Ctrl+C para sair)..."
	@python3 -c 'import os, termios, fcntl, struct, select, sys; \
	fd = os.open("$(PORT)", os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK); \
	attrs = termios.tcgetattr(fd); \
	attrs[2] &= ~termios.HUPCL; \
	attrs[4] = attrs[5] = termios.B115200; \
	termios.tcsetattr(fd, termios.TCSANOW, attrs); \
	fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_DTR | termios.TIOCM_RTS)); \
	try: \
	  while True: \
	    r, _, _ = select.select([fd], [], [], 0.2); \
	    if r: sys.stdout.write(os.read(fd, 4096).decode("utf-8", errors="replace")); sys.stdout.flush() \
	except KeyboardInterrupt: pass \
	finally: os.close(fd)'

# ------------------------------------------------------------------------------
# 6. SIMULAÇÃO DE BANCADA (SEM HARDWARE)
# ------------------------------------------------------------------------------
simulate: ## Executa o simulador de telemetria normal em Python
	python3 scripts/simulate_esp32.py --mode normal --interval 5

simulate-alert: ## Simula anomalia de temperatura alta para testar disparo de alertas
	python3 scripts/simulate_esp32.py --mode high-temp --count 3 --interval 5

simulate-lwt: ## Simula queda abrupta de energia no ESP32 (teste de Last Will and Testament)
	python3 scripts/simulate_esp32.py --mode lwt-drop
