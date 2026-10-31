# 🔌 Firmware de Borda — ESP32 + Sensor DS18B20 + MQTT/LWT

Este diretório contém o código-fonte do microcontrolador **ESP32 DevKit v1** ([`esp32_freezer_monitor.ino`](./esp32_freezer_monitor.ino)) responsável por:
- Medir a temperatura da câmara fria/freezer a cada **10 segundos** usando a sonda à prova d'água **Dallas DS18B20** (resolução de 12 bits + validação de CRC8);
- Publicar a telemetria via **MQTT** (`loja/freezer/<id>/telemetry`);
- Reportar presença e quedas abruptas de energia via **Last Will and Testament (LWT)** (`loja/freezer/<id>/status`);
- Receber atualizações dinâmicas da faixa ideal de temperatura (`loja/freezer/<id>/config/set`);
- Fornecer telemetria de autodiagnóstico elétrico da protoboard (`sensor_real`, `pullup_pins` e `ow_debug`) diretamente no payload MQTT.

---

## 🛠️ Esquema de Ligação na Protoboard (Pinagem)

A sonda submersível **DS18B20** possui 3 fios e requer **1 resistor de pull-up** (entre **1 kΩ e 4,7 kΩ** — recomendado **1 kΩ** ou **4,7 kΩ** para cabos de 1 metro alimentados em 3,3V no ESP32):

| Componente | Cor do Fio / Pino | Pino no ESP32 | Como ligar na Protoboard |
| :--- | :--- | :--- | :--- |
| **Sensor DS18B20** | **Vermelho** (`VDD`) | **`3V3`** | Na mesma trilha do `3V3` do ESP32 + **Perna 1 do Resistor** |
| **Sensor DS18B20** | **Amarelo** (`DQ` / Dados) | **`D4` (`GPIO 4`)** | Na mesma trilha do `D4` do ESP32 + **Perna 2 do Resistor** |
| **Sensor DS18B20** | **Preto** (`GND`) | **`GND`** | Na mesma trilha do `GND` do ESP32 |
| **Resistor Pull-Up** | `1 kΩ` a `4,7 kΩ` | Entre **`D4`** e **`3V3`** | Faz uma ponte ligando a trilha de **Dados (`D4`)** à trilha de **3,3V (`3V3`)** |
| **LED Onboard** | Interno da placa | **`GPIO 2`** | Pisca a cada envio normal; fica **aceso fixo** se a temperatura sair da faixa ideal |

### Esquema Visual das Trilhas na Protoboard

```text
   [ESP32: Pino 3V3] ───┬─── [Fio VERMELHO da Sonda (VCC)]
                        │
                   [Resistor 1kΩ a 4.7kΩ]
                        │
   [ESP32: Pino D4 ] ───┴─── [Fio AMARELO da Sonda (DATA)]

   [ESP32: Pino GND] ─────── [Fio PRETO da Sonda (GND)]
```

---

## 🔒 Segurança de Credenciais (`secrets.h`)

O arquivo `secrets.h` está protegido no [`.gitignore`](../../.gitignore) para que suas senhas de Wi-Fi nunca sejam enviadas ao GitHub.

1. Na raiz do projeto, rode `make setup` (ou copie manualmente):
   ```bash
   cp secrets.example.h secrets.h
   ```
2. Descubra o IP da sua máquina na rede local (`make ip` na raiz do projeto) e preencha o `secrets.h`:
   ```cpp
   #define WIFI_SSID         "NOME_DO_SEU_WIFI_2.4GHZ"
   #define WIFI_PASSWORD     "SENHA_DO_SEU_WIFI"
   #define MQTT_BROKER_IP    "192.168.1.XXX" // IP local do computador rodando o Docker
   #define MQTT_BROKER_PORT  1884            // Porta exposta pelo container coutos_mosquitto
   #define MQTT_USERNAME     ""
   #define MQTT_PASSWORD     ""
   #define DEVICE_ID         "camara01"
   ```
   > ⚠️ **Importante**: O rádio Wi-Fi do ESP32 opera exclusivamente em **2.4 GHz** (`802.11 b/g/n`). Não utilize o nome da rede `_5G` no `WIFI_SSID` (mesmo que o computador esteja no 5 GHz do mesmo roteador, eles se enxergam normalmente na mesma rede local `192.168.x.x`).

---

## 🚀 Como Compilar e Gravar no ESP32

### Opção 1: Via `Makefile` na raiz do projeto (Mais rápido no Linux)
Com o ESP32 conectado na USB (`/dev/ttyUSB0`):
```bash
# Compila e envia o código para o ESP32
make esp-upload

# (Opcional) Se estiver em outra porta USB:
make esp-upload PORT=/dev/ttyACM0

# Abre o monitor serial em 115200 baud (sem travar os pinos DTR/RTS)
make esp-monitor
```

### Opção 2: Via `arduino-cli` diretamente
```bash
arduino-cli compile --fqbn esp32:esp32:esp32 -u -p /dev/ttyUSB0 firmware/esp32_freezer_monitor
```

### Opção 3: Via VS Code + PlatformIO
```bash
cd firmware/esp32_freezer_monitor
pio run --target upload
pio device monitor
```

### Opção 4: Via Arduino IDE
1. Instale o pacote de placas **esp32 by Espressif Systems** no Gerenciador de Placas.
2. Instale as bibliotecas **PubSubClient**, **OneWire** e **DallasTemperature**.
3. Selecione a placa **ESP32 Dev Module** (ou **DOIT ESP32 DEVKIT V1**), a porta USB correspondente e clique em **Upload**.

---

## 🔍 Entendendo os Campos de Diagnóstico no Payload MQTT

A cada 10 segundos, o ESP32 publica no tópico `loja/freezer/camara01/telemetry` um JSON como:
```json
{
  "event_id": "camara01_1214_3",
  "device_id": "camara01",
  "temperature_c": 29.38,
  "min_temp_c": -10.0,
  "max_temp_c": 5.0,
  "sensor_real": true,
  "pullup_pins": "4",
  "ow_debug": "pin=4,pres=26,sp=D6017F807FFF0A10CC",
  "uptime_s": 30
}
```
- **`"sensor_real": true`**: Confirma que a leitura veio fisicamente da sonda DS18B20 (se a sonda desconectar, muda para `false` e usa simulação de bancada se `SIMULATE_SENSOR_IF_DISCONNECTED = true`).
- **`"pullup_pins": "4"`**: Indica em qual pino GPIO o ESP32 detectou tensão de pull-up física vinda do resistor ligado ao `3V3`.
- **`"ow_debug"`**:
  - `pres=26`: Tempo em microssegundos (`26 µs`) em que a sonda DS18B20 respondeu ao pulso de presença (se aparecer `pres=-1`, a sonda não respondeu).
  - `sp=D6017F807FFF0A10CC`: Os 9 bytes brutos do *Scratchpad* lidos da sonda (incluindo o byte final `0xCC` de verificação CRC8).
