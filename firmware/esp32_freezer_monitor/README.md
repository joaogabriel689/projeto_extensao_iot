# 🔌 Firmware de Borda — ESP32 + Sensor DS18B20 + MQTT/LWT

Este diretório contém o código-fonte do microcontrolador **ESP32 DevKit v1** responsável por medir a temperatura da câmara fria/freezer a cada **10 segundos**, publicar a telemetria via **MQTT**, reportar presença via **Last Will and Testament (LWT)** e receber atualizações dinâmicas da faixa ideal de temperatura.

---

## 🛠️ Esquema de Ligação (Pinagem)

| Componente | Pino no Componente | Pino no ESP32 | Observação |
| :--- | :--- | :--- | :--- |
| **Sensor Dallas DS18B20** | `VDD` (Vermelho) | `3V3` | Alimentação 3.3V |
| **Sensor Dallas DS18B20** | `GND` (Preto) | `GND` | Terra comum |
| **Sensor Dallas DS18B20** | `DQ` / Dados (Amarelo) | `GPIO 4` | **Obrigatório** resistor *pull-up* de `4.7 kΩ` entre `GPIO 4` e `3V3` |
| **LED Status (Onboard)** | Anodo interno | `GPIO 2` | Pisca ao enviar telemetria; fica aceso fixo se fora da faixa ideal |

---

## 🔒 Segurança de Credenciais (Repositório Público)

Nunca salve a senha do seu Wi-Fi diretamente no arquivo `.ino`!

1. Copie o arquivo de exemplo [`secrets.example.h`](./secrets.example.h) para `secrets.h`:
   ```bash
   cp secrets.example.h secrets.h
   ```
2. Edite o arquivo `secrets.h` (que está protegido no `.gitignore`) com o nome da sua rede Wi-Fi e o IP da máquina que está rodando o Docker.

---

## 🚀 Como Compilar e Gravar no ESP32

### Opção A: Via VS Code + PlatformIO (Recomendado)
As bibliotecas (`PubSubClient`, `OneWire`, `DallasTemperature`) são baixadas automaticamente pelo arquivo [`platformio.ini`](./platformio.ini):
```bash
cd firmware/esp32_freezer_monitor
pio run --target upload
pio device monitor
```

### Opção B: Via Arduino IDE
1. No Gerenciador de Bibliotecas da Arduino IDE, instale:
   - **PubSubClient** (por *Nick O'Leary*)
   - **OneWire** (por *Paul Stoffregen*)
   - **DallasTemperature** (por *Miles Burton*)
2. Abra `esp32_freezer_monitor.ino`, selecione a placa **DOIT ESP32 DEVKIT V1**, escolha a porta USB (`/dev/ttyUSB0` no Linux ou `COMx` no Windows) e clique em **Upload**.
