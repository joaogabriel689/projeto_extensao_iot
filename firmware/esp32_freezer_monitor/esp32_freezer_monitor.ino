/*
 * ==============================================================================
 * SISTEMA COUTOS CONVENIÊNCIA - FIRMWARE DE BORDA (ESP32 + DS18B20 + MQTT/LWT)
 * ==============================================================================
 * - Leitura contínua do sensor submersível Dallas DS18B20 via OneWire a cada 10s
 * - Transmissão de telemetria JSON via MQTT (loja/freezer/<id>/telemetry)
 * - Suporte a Last Will and Testament (LWT) para detecção imediata de queda
 * - Sincronização dinâmica da faixa ideal de temperatura via MQTT
 * ==============================================================================
 */

#include <WiFi.h>
#include <PubSubClient.h>
#include <OneWire.h>
#include <DallasTemperature.h>

// Carrega secrets.h se existir localmente; caso contrário, usa secrets.example.h
#if __has_include("secrets.h")
  #include "secrets.h"
#else
  #include "secrets.example.h"
#endif

// ------------------------------------------------------------------------------
// 1. PINAGEM DO HARDWARE (ESP32 DevKit v1)
// ------------------------------------------------------------------------------
const int PIN_ONEWIRE_DS18B20 = 4;   // GPIO 4: Barramento de dados do DS18B20 (com resistor pull-up 4.7kΩ)
const int PIN_STATUS_LED      = 2;   // GPIO 2: LED onboard (pisca ao enviar telemetria / aceso se fora da faixa)

// Defina como true se quiser simular temperaturas em bancada caso o sensor físico não esteja plugado
const bool SIMULATE_SENSOR_IF_DISCONNECTED = true;

// ------------------------------------------------------------------------------
// 2. PARÂMETROS OPERACIONAIS E FAIXA IDEAL (ATUALIZÁVEL VIA MQTT)
// ------------------------------------------------------------------------------
const unsigned long TELEMETRY_INTERVAL_MS = 10000; // 10 segundos (Meta 1 do Roteiro)
const unsigned long RECONNECT_INTERVAL_MS = 5000;  // 5 segundos entre tentativas de reconexão

float minTempC = -10.0; // Limite mínimo inicial (ajustável pelo usuário)
float maxTempC = 5.0;   // Limite máximo inicial (ajustável pelo usuário)

// ------------------------------------------------------------------------------
// 3. TÓPICOS MQTT (Convenção: loja/freezer/<id>/<campo>)
// ------------------------------------------------------------------------------
String topicTelemetry = String("loja/freezer/") + DEVICE_ID + "/telemetry";
String topicStatus    = String("loja/freezer/") + DEVICE_ID + "/status";
String topicConfigSet = String("loja/freezer/") + DEVICE_ID + "/config/set";

// ------------------------------------------------------------------------------
// 4. INSTÂNCIAS DE OBJETOS
// ------------------------------------------------------------------------------
OneWire oneWire(PIN_ONEWIRE_DS18B20);
DallasTemperature sensors(&oneWire);
WiFiClient espClient;
PubSubClient mqttClient(espClient);

unsigned long lastTelemetryTime = 0;
unsigned long lastReconnectAttempt = 0;
unsigned long packetSequence = 0;

// ------------------------------------------------------------------------------
// FUNÇÃO: Extrai valor float simples de uma chave JSON sem depender de libs extras
// ------------------------------------------------------------------------------
bool extractJsonFloat(const String& json, const char* key, float &outVal) {
  String searchKey = String("\"") + key + "\"";
  int keyIdx = json.indexOf(searchKey);
  if (keyIdx < 0) return false;
  int colonIdx = json.indexOf(':', keyIdx);
  if (colonIdx < 0) return false;

  int startIdx = colonIdx + 1;
  while (startIdx < (int)json.length() && (json[startIdx] == ' ' || json[startIdx] == '"')) {
    startIdx++;
  }
  int endIdx = startIdx;
  while (endIdx < (int)json.length() &&
         (isDigit(json[endIdx]) || json[endIdx] == '-' || json[endIdx] == '.')) {
    endIdx++;
  }
  if (endIdx > startIdx) {
    outVal = json.substring(startIdx, endIdx).toFloat();
    return true;
  }
  return false;
}

// ------------------------------------------------------------------------------
// FUNÇÃO: Callback de mensagens MQTT recebidas
// ------------------------------------------------------------------------------
void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String message = "";
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  message.trim();

  Serial.printf("[MQTT SUB] Topico: %s | Payload: %s\n", topic, message.c_str());

  // Atualização dinâmica da faixa ideal definida pelo comerciante
  if (String(topic) == topicConfigSet) {
    float newMin = minTempC;
    float newMax = maxTempC;
    bool hasMin = extractJsonFloat(message, "min_temp_c", newMin);
    bool hasMax = extractJsonFloat(message, "max_temp_c", newMax);
    if ((hasMin || hasMax) && newMin < newMax) {
      minTempC = newMin;
      maxTempC = newMax;
      Serial.printf("[CONFIG] Nova faixa ideal recebida: %.1f C a %.1f C\n", minTempC, maxTempC);
    }
  }
}

// ------------------------------------------------------------------------------
// FUNÇÃO: Conexão Wi-Fi com auto-reconexão
// ------------------------------------------------------------------------------
void ensureWiFiConnected() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.printf("\n[WiFi] Conectando a '%s'", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] Conectado! IP: %s\n", WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WiFi] Falha temporaria ao conectar. Tentando novamente no proximo ciclo.");
  }
}

// ------------------------------------------------------------------------------
// FUNÇÃO: Conexão MQTT com Last Will and Testament (LWT)
// ------------------------------------------------------------------------------
bool reconnectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return false;

  String clientId = String("esp32_coutos_") + DEVICE_ID;
  Serial.printf("[MQTT] Conectando ao Broker %s:%d como '%s'... ",
                MQTT_BROKER_IP, MQTT_BROKER_PORT, clientId.c_str());

  // Configuração do Last Will and Testament (LWT):
  // Caso o ESP32 perca energia subitamente, o Mosquitto publica "offline" imediatamente
  const int willQos = 1;
  const bool willRetain = true;
  const char* willPayload = "offline";

  bool connected = false;
  if (strlen(MQTT_USERNAME) > 0) {
    connected = mqttClient.connect(
      clientId.c_str(),
      MQTT_USERNAME,
      MQTT_PASSWORD,
      topicStatus.c_str(),
      willQos,
      willRetain,
      willPayload
    );
  } else {
    connected = mqttClient.connect(
      clientId.c_str(),
      NULL,
      NULL,
      topicStatus.c_str(),
      willQos,
      willRetain,
      willPayload
    );
  }

  if (connected) {
    Serial.println("CONECTADO!");
    // Publica status online com retenção
    mqttClient.publish(topicStatus.c_str(), "online", true);
    // Inscreve no tópico de atualização da faixa ideal
    mqttClient.subscribe(topicConfigSet.c_str(), 1);
    return true;
  } else {
    Serial.printf("FALHA (rc=%d)\n", mqttClient.state());
    return false;
  }
}

// ------------------------------------------------------------------------------
// FUNÇÃO: Leitura do Sensor Dallas DS18B20
// ------------------------------------------------------------------------------
float readFreezerTemperatureC() {
  sensors.requestTemperatures();
  float tempC = sensors.getTempCByIndex(0);

  if (tempC == DEVICE_DISCONNECTED_C || tempC < -55.0 || tempC > 125.0) {
    Serial.println("[AVISO] Sensor DS18B20 nao detectado no barramento OneWire!");
    if (SIMULATE_SENSOR_IF_DISCONNECTED) {
      // Gera leve oscilação em torno de -2.5°C para testes de bancada sem sensor físico
      float simulated = -2.5 + ((random(-15, 16)) / 10.0);
      Serial.printf("[SIMULACAO] Usando temperatura simulada de bancada: %.2f C\n", simulated);
      return simulated;
    }
    return NAN;
  }
  return tempC;
}

// ------------------------------------------------------------------------------
// SETUP
// ------------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(500);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);

  sensors.begin();

  ensureWiFiConnected();

  mqttClient.setServer(MQTT_BROKER_IP, MQTT_BROKER_PORT);
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setKeepAlive(15); // Keep-alive de 15s (detecção rápida de queda via LWT)
}

// ------------------------------------------------------------------------------
// LOOP PRINCIPAL
// ------------------------------------------------------------------------------
void loop() {
  ensureWiFiConnected();

  if (!mqttClient.connected()) {
    unsigned long now = millis();
    if (now - lastReconnectAttempt >= RECONNECT_INTERVAL_MS) {
      lastReconnectAttempt = now;
      if (reconnectMQTT()) {
        lastReconnectAttempt = 0;
      }
    }
  } else {
    mqttClient.loop();
  }

  unsigned long now = millis();
  if (now - lastTelemetryTime >= TELEMETRY_INTERVAL_MS) {
    lastTelemetryTime = now;
    float tempC = readFreezerTemperatureC();

    if (!isnan(tempC)) {
      packetSequence++;
      unsigned long uptimeSec = now / 1000;
      String eventId = String(DEVICE_ID) + "_" + String(uptimeSec) + "_" + String(packetSequence);

      // Sinalização visual no LED onboard (GPIO 2): aceso se fora da faixa, pisca rápido se normal
      bool outOfRange = (tempC < minTempC || tempC > maxTempC);
      if (outOfRange) {
        digitalWrite(PIN_STATUS_LED, HIGH);
      } else {
        digitalWrite(PIN_STATUS_LED, HIGH);
        delay(80);
        digitalWrite(PIN_STATUS_LED, LOW);
      }

      char payload[256];
      snprintf(
        payload,
        sizeof(payload),
        "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"temperature_c\":%.2f,\"min_temp_c\":%.1f,\"max_temp_c\":%.1f,\"uptime_s\":%lu}",
        eventId.c_str(),
        DEVICE_ID,
        tempC,
        minTempC,
        maxTempC,
        uptimeSec
      );

      if (mqttClient.connected()) {
        mqttClient.publish(topicTelemetry.c_str(), payload, false);
        Serial.printf("[TELEMETRIA PUB] %s -> %s\n", topicTelemetry.c_str(), payload);
      } else {
        Serial.printf("[TELEMETRIA OFFLINE] Leitura local: %.2f C (aguardando reconexao MQTT)\n", tempC);
      }
    }
  }
}
