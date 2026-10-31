/*
 * ==============================================================================
 * FRIOSAFE IOT - FIRMWARE DE BORDA (ESP32 + DS18B20 + MQTT/LWT + OTA SEGURO)
 * ==============================================================================
 * - Leitura contínua do sensor submersível Dallas DS18B20 via OneWire
 * - Portal Web & Captive Portal Wi-Fi (NVS Preferences + WebServer + DNSServer)
 * - Transmissão de telemetria JSON via MQTT (loja/freezer/<id>/telemetry)
 * - Suporte a Last Will and Testament (LWT) para detecção imediata de queda
 * - Sincronização dinâmica da faixa ideal e intervalo de leitura via MQTT
 * - Atualização remota de firmware (OTA Seguro via HMAC-SHA256 + SHA-256)
 * ==============================================================================
 */

#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <PubSubClient.h>
#include <HTTPClient.h>
#include <Update.h>
#include <OneWire.h>
#include <DallasTemperature.h>
#include "driver/gpio.h"
#include "soc/gpio_struct.h"
#include "mbedtls/md.h"
#include "mbedtls/sha256.h"

// Carrega secrets.h se existir localmente; caso contrário, usa secrets.example.h
#if __has_include("secrets.h")
  #include "secrets.h"
#else
  #include "secrets.example.h"
#endif

#ifndef OTA_SHARED_SECRET
  #define OTA_SHARED_SECRET "friosafe_ota_hmac_secret_2026"
#endif

const char* FIRMWARE_VERSION = "1.2.1";

// ------------------------------------------------------------------------------
// 1. PINAGEM DO HARDWARE (ESP32 DevKit v1)
// ------------------------------------------------------------------------------
const int PIN_ONEWIRE_DS18B20 = 4;   // GPIO 4 (D4): Barramento de dados do DS18B20
const int PIN_STATUS_LED      = 2;   // GPIO 2: LED onboard
const int PIN_BOOT_BUTTON     = 0;   // GPIO 0: Botão BOOT (segurar 2s abre Portal Wi-Fi)

// Mantém fallback de bancada caso o sensor físico não responda no ciclo
const bool SIMULATE_SENSOR_IF_DISCONNECTED = true;

// ------------------------------------------------------------------------------
// 2. PARÂMETROS OPERACIONAIS E CONFIGURAÇÃO PERSISTENTE (NVS)
// ------------------------------------------------------------------------------
unsigned long telemetryIntervalMs          = 10000;  // 10 segundos (ajustável via MQTT / NVS)
const unsigned long RECONNECT_INTERVAL_MS  = 5000;   // 5 segundos entre reconexões MQTT
const unsigned long WIFI_RETRY_INTERVAL_MS = 20000;  // 20 segundos entre tentativas de Wi-Fi
const unsigned long AP_PORTAL_TIMEOUT_MS   = 300000; // 5 minutos de portal AP ativo após acionamento

const char* AP_SSID_PREFIX = "FrioSafe-Config";
const byte  DNS_PORT       = 53;

Preferences preferences;
WebServer   webServer(80);
DNSServer   dnsServer;

// Configurações carregadas da memória Flash NVS (com fallback para secrets.h)
String cfgWifiSsid   = WIFI_SSID;
String cfgWifiPass   = WIFI_PASSWORD;
String cfgMqttHost   = MQTT_BROKER_IP;
int    cfgMqttPort   = MQTT_BROKER_PORT;
String cfgMqttUser   = MQTT_USERNAME;
String cfgMqttPass   = MQTT_PASSWORD;
String cfgDeviceId   = DEVICE_ID;
String cfgOtaSecret  = OTA_SHARED_SECRET;

// Controle de atualização remota OTA segura (HMAC-SHA256 + One-Time Token + SHA-256)
bool   pendingOta        = false;
String otaTargetVersion  = "";
String otaTargetToken    = "";
String otaTargetSha256   = "";
int    otaTargetPort     = 8000;
int    otaTargetSize     = 0;

float minTempC = -10.0; // Limite mínimo inicial (ajustável pelo usuário)
float maxTempC = 5.0;   // Limite máximo inicial (ajustável pelo usuário)
float lastReadTempC = NAN;
bool  lastReadIsReal = false;

// Estado do Portal Wi-Fi (Access Point)
bool          apPortalActive    = false;
unsigned long apPortalStartedAt = 0;
unsigned long lastWifiAttemptAt = 0;
unsigned long bootBtnPressedAt  = 0;
bool          pendingReboot     = false;
unsigned long rebootScheduledAt = 0;

// ------------------------------------------------------------------------------
// 3. TÓPICOS MQTT (Convenção: loja/freezer/<id>/<campo>)
// ------------------------------------------------------------------------------
String topicTelemetry  = "";
String topicStatus     = "";
String topicConfigSet  = "";
String topicOtaTrigger = "";
String topicOtaStatus  = "";

void rebuildMqttTopics() {
  topicTelemetry  = String("loja/freezer/") + cfgDeviceId + "/telemetry";
  topicStatus     = String("loja/freezer/") + cfgDeviceId + "/status";
  topicConfigSet  = String("loja/freezer/") + cfgDeviceId + "/config/set";
  topicOtaTrigger = String("loja/freezer/") + cfgDeviceId + "/ota/trigger";
  topicOtaStatus  = String("loja/freezer/") + cfgDeviceId + "/ota/status";
}

// ------------------------------------------------------------------------------
// 4. INSTÂNCIAS DE OBJETOS
// ------------------------------------------------------------------------------
OneWire oneWire;
DallasTemperature sensors(&oneWire);
WiFiClient espClient;
PubSubClient mqttClient(espClient);

unsigned long lastTelemetryTime = 0;
unsigned long lastReconnectAttempt = 0;
unsigned long packetSequence = 0;
String bootSessionId = "";

// ------------------------------------------------------------------------------
// 5. DRIVER ONEWIRE NATIVO ESP32 (Core 3.x / IDF 5.x Direct Register + Spinlock)
// ------------------------------------------------------------------------------
int detectedSensorPin = PIN_ONEWIRE_DS18B20;
String lastOwDebug = "";
String lastPullupPins = "";
portMUX_TYPE owMux = portMUX_INITIALIZER_UNLOCKED;

void owInitPin(int pin) {
  pinMode(pin, OUTPUT | PULLUP);
  GPIO.out_w1tc = ((uint32_t)1 << pin);
  GPIO.enable_w1tc = ((uint32_t)1 << pin);
}

static inline void IRAM_ATTR owDriveLow(int pin) {
  GPIO.out_w1tc = ((uint32_t)1 << pin);
  GPIO.enable_w1ts = ((uint32_t)1 << pin);
}

static inline void IRAM_ATTR owReleaseBus(int pin) {
  GPIO.enable_w1tc = ((uint32_t)1 << pin);
}

static inline uint8_t IRAM_ATTR owReadPin(int pin) {
  return (GPIO.in >> pin) & 0x1;
}

bool owReset(int pin, int *outPresUs = NULL, int *outIdle = NULL, int *outDrivenLow = NULL) {
  owInitPin(pin);
  delayMicroseconds(30);
  int idleLvl = owReadPin(pin);
  if (outIdle) *outIdle = idleLvl;
  if (idleLvl == 0) {
    if (outPresUs) *outPresUs = -2;
    return false;
  }

  portENTER_CRITICAL(&owMux);
  owDriveLow(pin);
  delayMicroseconds(10);
  int drivenLvl = owReadPin(pin);
  if (outDrivenLow) *outDrivenLow = drivenLvl;
  delayMicroseconds(490);
  owReleaseBus(pin);

  delayMicroseconds(10);

  int presUs = -1;
  for (int t = 10; t <= 250; t++) {
    if (owReadPin(pin) == 0) {
      presUs = t;
      break;
    }
    delayMicroseconds(1);
  }
  portEXIT_CRITICAL(&owMux);

  if (outPresUs) *outPresUs = presUs;
  if (presUs > 0) {
    delayMicroseconds(420);
    if (owReadPin(pin) == 0) {
      if (outPresUs) *outPresUs = -3;
      return false;
    }
    return true;
  }
  return false;
}

void IRAM_ATTR owWriteBit(int pin, uint8_t bit) {
  portENTER_CRITICAL(&owMux);
  owDriveLow(pin);
  if (bit) {
    delayMicroseconds(6);
    owReleaseBus(pin);
    portEXIT_CRITICAL(&owMux);
    delayMicroseconds(64);
  } else {
    delayMicroseconds(60);
    owReleaseBus(pin);
    portEXIT_CRITICAL(&owMux);
    delayMicroseconds(10);
  }
}

uint8_t IRAM_ATTR owReadBit(int pin) {
  portENTER_CRITICAL(&owMux);
  owDriveLow(pin);
  delayMicroseconds(3);
  owReleaseBus(pin);
  delayMicroseconds(10);
  uint8_t r = owReadPin(pin);
  portEXIT_CRITICAL(&owMux);
  delayMicroseconds(53);
  return r;
}

void owWriteByte(int pin, uint8_t data) {
  for (uint8_t i = 0; i < 8; i++) {
    owWriteBit(pin, data & 0x01);
    data >>= 1;
  }
}

uint8_t owReadByte(int pin) {
  uint8_t data = 0;
  for (uint8_t i = 0; i < 8; i++) {
    if (owReadBit(pin)) data |= (1 << i);
  }
  return data;
}

bool readDS18B20Native(int pin, float &outTempC, bool recordDebug = false) {
  int presUs = -1;
  int idleLvl = -1;
  int drivenLow = -1;
  if (!owReset(pin, &presUs, &idleLvl, &drivenLow)) {
    if (recordDebug) {
      lastOwDebug = String("pin=") + pin + ",idle=" + idleLvl + ",drv=" + drivenLow + ",pres=" + presUs;
    }
    return false;
  }
  owWriteByte(pin, 0xCC);
  owWriteByte(pin, 0x44);

  // Durante os 750ms de conversão, mantém o portal web e MQTT responsivos
  unsigned long convStart = millis();
  while (millis() - convStart < 750) {
    webServer.handleClient();
    if (apPortalActive) dnsServer.processNextRequest();
    delay(5);
  }

  if (!owReset(pin)) {
    if (recordDebug) {
      lastOwDebug = String("pin=") + pin + ",pres1=" + presUs + ",pres2=fail";
    }
    return false;
  }
  owWriteByte(pin, 0xCC);
  owWriteByte(pin, 0xBE);

  uint8_t data[9];
  bool allZero = true;
  bool allFF = true;
  char hexBuf[28];
  for (int i = 0; i < 9; i++) {
    data[i] = owReadByte(pin);
    if (data[i] != 0x00) allZero = false;
    if (data[i] != 0xFF) allFF = false;
  }
  snprintf(hexBuf, sizeof(hexBuf), "%02X%02X%02X%02X%02X%02X%02X%02X%02X",
           data[0], data[1], data[2], data[3], data[4], data[5], data[6], data[7], data[8]);

  if (recordDebug) {
    lastOwDebug = String("pin=") + pin + ",pres=" + presUs + ",sp=" + hexBuf;
  }

  if (allZero || allFF) return false;

  uint8_t crc = 0;
  for (int i = 0; i < 8; i++) {
    uint8_t inbyte = data[i];
    for (int j = 0; j < 8; j++) {
      uint8_t mix = (crc ^ inbyte) & 0x01;
      crc >>= 1;
      if (mix) crc ^= 0x8C;
      inbyte >>= 1;
    }
  }
  if (crc != data[8]) {
    return false;
  }

  int16_t raw = (int16_t)((data[1] << 8) | data[0]);
  float celsius = (float)raw / 16.0f;
  if (celsius < -55.0f || celsius > 125.0f) return false;
  if (celsius == 85.0f) {
    if (!owReset(pin)) return false;
    owWriteByte(pin, 0xCC);
    owWriteByte(pin, 0x44);
    delay(750);
    if (!owReset(pin)) return false;
    owWriteByte(pin, 0xCC);
    owWriteByte(pin, 0xBE);
    uint8_t d0 = owReadByte(pin);
    uint8_t d1 = owReadByte(pin);
    raw = (int16_t)((d1 << 8) | d0);
    celsius = (float)raw / 16.0f;
  }
  outTempC = celsius;
  return true;
}

// ------------------------------------------------------------------------------
// 6. PERSISTÊNCIA EM NVS (PREFERENCES)
// ------------------------------------------------------------------------------
void loadConfigurationFromNVS() {
  preferences.begin("friosafe_cfg", true);
  cfgWifiSsid = preferences.getString("wifi_ssid", WIFI_SSID);
  cfgWifiPass = preferences.getString("wifi_pass", WIFI_PASSWORD);
  cfgMqttHost = preferences.getString("mqtt_host", MQTT_BROKER_IP);
  cfgMqttPort = preferences.getInt("mqtt_port", MQTT_BROKER_PORT);
  cfgMqttUser = preferences.getString("mqtt_user", MQTT_USERNAME);
  cfgMqttPass = preferences.getString("mqtt_pass", MQTT_PASSWORD);
  cfgDeviceId = preferences.getString("device_id", DEVICE_ID);
  minTempC    = preferences.getFloat("min_temp", -10.0f);
  maxTempC    = preferences.getFloat("max_temp", 5.0f);
  int telSec  = preferences.getInt("tel_int_s", 10);
  preferences.end();

  if (telSec < 2) telSec = 2;
  if (telSec > 3600) telSec = 3600;
  telemetryIntervalMs = (unsigned long)telSec * 1000UL;

  if (cfgDeviceId.length() == 0) cfgDeviceId = DEVICE_ID;
  if (cfgMqttPort <= 0 || cfgMqttPort > 65535) cfgMqttPort = MQTT_BROKER_PORT;
  rebuildMqttTopics();

  Serial.println("\n=======================================================");
  Serial.println("[NVS] Configuracao Carregada:");
  Serial.printf("  • Device ID : %s\n", cfgDeviceId.c_str());
  Serial.printf("  • Wi-Fi SSID: %s\n", cfgWifiSsid.c_str());
  Serial.printf("  • MQTT Host : %s:%d\n", cfgMqttHost.c_str(), cfgMqttPort);
  Serial.printf("  • Faixa     : %.1f C a %.1f C\n", minTempC, maxTempC);
  Serial.printf("  • Intervalo : %lu s\n", telemetryIntervalMs / 1000UL);
  Serial.println("=======================================================");
}

void saveConfigurationToNVS(
  const String& newSsid,
  const String& newPass,
  const String& newMqttHost,
  int newMqttPort,
  const String& newDeviceId
) {
  preferences.begin("friosafe_cfg", false);
  preferences.putString("wifi_ssid", newSsid);
  preferences.putString("wifi_pass", newPass);
  preferences.putString("mqtt_host", newMqttHost);
  preferences.putInt("mqtt_port", newMqttPort);
  preferences.putString("device_id", newDeviceId);
  preferences.end();

  cfgWifiSsid = newSsid;
  cfgWifiPass = newPass;
  cfgMqttHost = newMqttHost;
  cfgMqttPort = newMqttPort;
  cfgDeviceId = newDeviceId;
  rebuildMqttTopics();
  mqttClient.setServer(cfgMqttHost.c_str(), cfgMqttPort);

  Serial.println("[NVS] Novas configuracoes gravadas na memoria Flash com sucesso!");
}

void resetConfigurationToDefaults() {
  preferences.begin("friosafe_cfg", false);
  preferences.clear();
  preferences.end();
  loadConfigurationFromNVS();
  Serial.println("[NVS] Configuracoes restauradas para os padroes de fabrica (secrets.h).");
}

// ------------------------------------------------------------------------------
// 7. PORTAL DE CONFIGURAÇÃO WI-FI (AP + SERVIDOR WEB HTTP PORTA 80)
// ------------------------------------------------------------------------------
String htmlEscape(const String& raw) {
  String out = raw;
  out.replace("&", "&amp;");
  out.replace("<", "&lt;");
  out.replace(">", "&gt;");
  out.replace("\"", "&quot;");
  out.replace("'", "&#39;");
  return out;
}

void startCaptivePortalAP() {
  if (apPortalActive) {
    apPortalStartedAt = millis();
    return;
  }

  WiFi.mode(WIFI_AP_STA);
  IPAddress apIP(192, 168, 4, 1);
  IPAddress netMsk(255, 255, 255, 0);
  WiFi.softAPConfig(apIP, apIP, netMsk);
  WiFi.softAP(AP_SSID_PREFIX); // Rede aberta para facilitar configuração rápida em campo

  dnsServer.setErrorReplyCode(DNSReplyCode::NoError);
  dnsServer.start(DNS_PORT, "*", apIP);

  apPortalActive = true;
  apPortalStartedAt = millis();

  Serial.println("\n*******************************************************");
  Serial.printf("[PORTAL AP] Rede Wi-Fi de Configuracao ATIVA: '%s'\n", AP_SSID_PREFIX);
  Serial.printf("[PORTAL AP] Conecte no Wi-Fi '%s' e acesse: http://192.168.4.1\n", AP_SSID_PREFIX);
  Serial.println("*******************************************************\n");

  // Pisca LED 4 vezes rápidas para indicar que o modo AP entrou em operação
  for (int i = 0; i < 4; i++) {
    digitalWrite(PIN_STATUS_LED, HIGH);
    delay(90);
    digitalWrite(PIN_STATUS_LED, LOW);
    delay(90);
  }
}

void stopCaptivePortalAP() {
  if (!apPortalActive) return;
  dnsServer.stop();
  WiFi.softAPdisconnect(true);
  WiFi.mode(WIFI_STA);
  apPortalActive = false;
  Serial.println("[PORTAL AP] Ponto de Acesso desativado (conectado ao Wi-Fi principal).");
}

void handleWebRoot() {
  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  bool mqttOk = mqttClient.connected();
  String tempStr = isnan(lastReadTempC) ? "--" : (String(lastReadTempC, 2) + " &deg;C");
  String staIp   = wifiOk ? WiFi.localIP().toString() : "Desconectado";
  String rssiStr = wifiOk ? (String(WiFi.RSSI()) + " dBm") : "--";

  String page = R"rawliteral(<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Configurar Dispositivo - FrioSafe IoT</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#f8fafc;color:#0f172a;padding:16px;line-height:1.45}
  .wrap{max-width:480px;margin:0 auto}
  .card{background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:18px;margin-bottom:14px}
  .hdr{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #f1f5f9;padding-bottom:12px;margin-bottom:14px}
  h1{font-size:16px;font-weight:600;color:#0f172a}
  .sub{font-size:12px;color:#64748b}
  .badge{font-size:11px;font-weight:600;padding:3px 8px;border-radius:4px}
  .ok{background:#ecfdf5;color:#047857;border:1px solid #a7f3d0}
  .warn{background:#fffbeb;color:#b45309;border:1px solid #fde68a}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px}
  .stat{background:#f8fafc;border:1px solid #e2e8f0;border-radius:5px;padding:9px 11px}
  .stat-l{font-size:11px;color:#64748b;text-transform:uppercase;letter-spacing:.03em}
  .stat-v{font-size:14px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
  label{display:block;font-size:12px;font-weight:500;color:#334155;margin:10px 0 4px}
  input{width:100%;padding:8px 10px;font-size:13px;border:1px solid #cbd5e1;border-radius:5px;background:#fff;color:#0f172a}
  input:focus{outline:none;border-color:#2563eb}
  .btn{display:inline-flex;align-items:center;justify-content:center;width:100%;padding:9px 12px;font-size:13px;font-weight:600;border-radius:5px;border:1px solid transparent;cursor:pointer;margin-top:12px;text-decoration:none}
  .btn-p{background:#0f172a;color:#fff}
  .btn-p:hover{background:#1e293b}
  .btn-s{background:#f1f5f9;color:#334155;border-color:#cbd5e1;margin-top:8px}
  .btn-s:hover{background:#e2e8f0}
  .net-list{margin-top:8px;max-height:180px;overflow-y:auto;border:1px solid #e2e8f0;border-radius:5px;display:none}
  .net-item{display:flex;justify-content:space-between;padding:8px 10px;font-size:12px;border-bottom:1px solid #f1f5f9;cursor:pointer}
  .net-item:hover{background:#f1f5f9}
  .net-item:last-child{border-bottom:none}
  .hint{font-size:11px;color:#64748b;margin-top:4px}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <div class="hdr">
      <div>
        <h1>Guardião Térmico ESP32</h1>
        <div class="sub">Configuração de Wi-Fi e Servidor MQTT</div>
      </div>
      <span class="badge )rawliteral";

  page += wifiOk ? "ok\">Wi-Fi Conectado" : "warn\">Modo Portal AP";
  page += R"rawliteral(</span>
    </div>
    <div class="grid">
      <div class="stat">
        <div class="stat-l">Temperatura Sonda</div>
        <div class="stat-v">)rawliteral" + tempStr + R"rawliteral(</div>
      </div>
      <div class="stat">
        <div class="stat-l">Broker MQTT</div>
        <div class="stat-v">)rawliteral" + String(mqttOk ? "Online" : "Desconectado") + R"rawliteral(</div>
      </div>
      <div class="stat">
        <div class="stat-l">IP na Rede Local</div>
        <div class="stat-v">)rawliteral" + staIp + R"rawliteral(</div>
      </div>
      <div class="stat">
        <div class="stat-l">Sinal Wi-Fi</div>
        <div class="stat-v">)rawliteral" + rssiStr + R"rawliteral(</div>
      </div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <strong style="font-size:13px">1. Rede Wi-Fi (2.4 GHz)</strong>
      <button type="button" class="btn btn-s" style="width:auto;margin:0;padding:4px 10px;font-size:11px" onclick="scanNetworks()" id="btnScan">Buscar Redes</button>
    </div>
    <div id="netList" class="net-list"></div>

    <form method="POST" action="/save">
      <label for="ssid">Nome do Wi-Fi (SSID)</label>
      <input type="text" id="ssid" name="wifi_ssid" value=")rawliteral" + htmlEscape(cfgWifiSsid) + R"rawliteral(" required placeholder="Ex: WiFi_Da_Loja">

      <label for="pass">Senha do Wi-Fi</label>
      <input type="password" id="pass" name="wifi_pass" value=")rawliteral" + htmlEscape(cfgWifiPass) + R"rawliteral(" placeholder="Digite a senha do Wi-Fi">
      <div class="hint"><label style="display:inline;font-weight:400"><input type="checkbox" style="width:auto;margin-right:4px" onclick="var p=document.getElementById('pass');p.type=this.checked?'text':'password'">Mostrar senha</label></div>

      <hr style="border:none;border-top:1px solid #f1f5f9;margin:14px 0 10px">
      <strong style="font-size:13px">2. Servidor Central (MQTT) e Identificação</strong>

      <label for="host">IP ou Domínio do Servidor (Broker MQTT)</label>
      <input type="text" id="host" name="mqtt_host" value=")rawliteral" + htmlEscape(cfgMqttHost) + R"rawliteral(" required placeholder="Ex: 192.168.1.181">

      <div class="grid" style="margin-top:4px">
        <div>
          <label for="port">Porta MQTT</label>
          <input type="number" id="port" name="mqtt_port" value=")rawliteral" + String(cfgMqttPort) + R"rawliteral(" required>
        </div>
        <div>
          <label for="devid">ID do Freezer</label>
          <input type="text" id="devid" name="device_id" value=")rawliteral" + htmlEscape(cfgDeviceId) + R"rawliteral(" required placeholder="camara01">
        </div>
      </div>

      <button type="submit" class="btn btn-p">Salvar e Conectar</button>
    </form>

    <form method="POST" action="/ap" style="margin-top:6px">
      <button type="submit" class="btn btn-s">Ativar Rede AP (FrioSafe-Config) por 5 min</button>
    </form>
  </div>
</div>
<script>
async function scanNetworks(){
  const btn=document.getElementById('btnScan');
  const box=document.getElementById('netList');
  btn.textContent='Buscando...';
  btn.disabled=true;
  box.style.display='block';
  box.innerHTML='<div class="net-item">Escaneando redes Wi-Fi 2.4 GHz...</div>';
  try{
    const r=await fetch('/scan');
    const nets=await r.json();
    if(!nets.length){
      box.innerHTML='<div class="net-item">Nenhuma rede encontrada.</div>';
    }else{
      box.innerHTML=nets.map(n=>`<div class="net-item" onclick="document.getElementById('ssid').value='${n.ssid.replace(/'/g,"\\'")}';document.getElementById('pass').focus()"><span><strong>${n.ssid}</strong> ${n.open?'(Aberta)':'🔒'}</span><span style="color:#64748b">${n.rssi} dBm</span></div>`).join('');
    }
  }catch(e){
    box.innerHTML='<div class="net-item">Erro ao buscar redes.</div>';
  }
  btn.textContent='Buscar Redes';
  btn.disabled=false;
}
</script>
</body>
</html>)rawliteral";

  webServer.send(200, "text/html; charset=utf-8", page);
}

void handleWebScan() {
  int n = WiFi.scanNetworks(false, false);
  String json = "[";
  for (int i = 0; i < n; i++) {
    if (i > 0) json += ",";
    String ssid = WiFi.SSID(i);
    ssid.replace("\"", "\\\"");
    bool open = (WiFi.encryptionType(i) == WIFI_AUTH_OPEN);
    json += "{\"ssid\":\"" + ssid + "\",\"rssi\":" + String(WiFi.RSSI(i)) + ",\"open\":" + (open ? "true" : "false") + "}";
  }
  json += "]";
  WiFi.scanDelete();
  webServer.sendHeader("Access-Control-Allow-Origin", "*");
  webServer.send(200, "application/json", json);
}

void handleWebStatusJson() {
  bool wifiOk = (WiFi.status() == WL_CONNECTED);
  String json = "{";
  json += "\"device_id\":\"" + cfgDeviceId + "\",";
  json += "\"fw_version\":\"" + String(FIRMWARE_VERSION) + "\",";
  json += "\"wifi_connected\":" + String(wifiOk ? "true" : "false") + ",";
  json += "\"wifi_ssid\":\"" + cfgWifiSsid + "\",";
  json += "\"wifi_ip\":\"" + (wifiOk ? WiFi.localIP().toString() : "") + "\",";
  json += "\"wifi_rssi\":" + String(wifiOk ? WiFi.RSSI() : 0) + ",";
  json += "\"ap_active\":" + String(apPortalActive ? "true" : "false") + ",";
  json += "\"ap_ssid\":\"" + String(AP_SSID_PREFIX) + "\",";
  json += "\"mqtt_host\":\"" + cfgMqttHost + "\",";
  json += "\"mqtt_port\":" + String(cfgMqttPort) + ",";
  json += "\"mqtt_connected\":" + String(mqttClient.connected() ? "true" : "false") + ",";
  json += "\"temperature_c\":" + (isnan(lastReadTempC) ? "null" : String(lastReadTempC, 2));
  json += "}";
  webServer.sendHeader("Access-Control-Allow-Origin", "*");
  webServer.send(200, "application/json", json);
}

void handleWebSave() {
  String newSsid = webServer.arg("wifi_ssid");
  String newPass = webServer.arg("wifi_pass");
  String newHost = webServer.arg("mqtt_host");
  int    newPort = webServer.arg("mqtt_port").toInt();
  String newId   = webServer.arg("device_id");

  newSsid.trim();
  newPass.trim();
  newHost.trim();
  newId.trim();

  if (newSsid.length() == 0 || newHost.length() == 0 || newId.length() == 0) {
    webServer.send(400, "text/plain; charset=utf-8", "Campos obrigatorios ausentes.");
    return;
  }
  if (newPort <= 0 || newPort > 65535) newPort = 1884;

  saveConfigurationToNVS(newSsid, newPass, newHost, newPort, newId);

  String resp = R"rawliteral(<!DOCTYPE html><html lang="pt-BR"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="6;url=/"><title>Configuração Salva</title><style>body{font-family:sans-serif;background:#f8fafc;color:#0f172a;padding:24px;text-align:center}.box{max-width:420px;margin:40px auto;background:#fff;border:1px solid #e2e8f0;border-radius:6px;padding:24px}</style></head><body><div class="box"><h2 style="font-size:16px;margin-bottom:8px">Configuração salva na memória Flash!</h2><p style="font-size:13px;color:#475569">O ESP32 está reiniciando e conectando à rede <strong>)rawliteral" + htmlEscape(newSsid) + R"rawliteral(</strong>...</p></div></body></html>)rawliteral";

  webServer.sendHeader("Access-Control-Allow-Origin", "*");
  webServer.send(200, "text/html; charset=utf-8", resp);

  pendingReboot = true;
  rebootScheduledAt = millis() + 1200;
}

void handleWebStartAp() {
  startCaptivePortalAP();
  webServer.sendHeader("Location", "/");
  webServer.send(302, "text/plain", "Modo AP ativado");
}

void setupWebServer() {
  webServer.on("/", HTTP_GET, handleWebRoot);
  webServer.on("/scan", HTTP_GET, handleWebScan);
  webServer.on("/status.json", HTTP_GET, handleWebStatusJson);
  webServer.on("/save", HTTP_POST, handleWebSave);
  webServer.on("/ap", HTTP_POST, handleWebStartAp);
  // Captive portal redirects para Android / iOS / Windows
  webServer.on("/generate_204", HTTP_GET, handleWebRoot);
  webServer.on("/hotspot-detect.html", HTTP_GET, handleWebRoot);
  webServer.on("/ncsi.txt", HTTP_GET, handleWebRoot);
  webServer.onNotFound(handleWebRoot);
  webServer.begin();
  Serial.println("[HTTP] Servidor de configuracao iniciado na porta 80.");
}

// ------------------------------------------------------------------------------
// 8. CRIPTOGRAFIA OTA (HMAC-SHA256 + SHA-256 ON-THE-FLY) E CALLBACK MQTT
// ------------------------------------------------------------------------------
String bytesToHexLower(const uint8_t* bytes, size_t len) {
  char buf[65];
  if (len > 32) len = 32;
  for (size_t i = 0; i < len; i++) {
    snprintf(buf + (i * 2), 3, "%02x", bytes[i]);
  }
  buf[len * 2] = '\0';
  return String(buf);
}

String computeHmacSha256Hex(const String& message, const String& secret) {
  uint8_t hmacResult[32];
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, (const unsigned char*)secret.c_str(), secret.length());
  mbedtls_md_hmac_update(&ctx, (const unsigned char*)message.c_str(), message.length());
  mbedtls_md_hmac_finish(&ctx, hmacResult);
  mbedtls_md_free(&ctx);
  return bytesToHexLower(hmacResult, 32);
}

bool constantTimeHexEquals(const String& a, const String& b) {
  if (a.length() != 64 || b.length() != 64) return false;
  uint8_t diff = 0;
  for (int i = 0; i < 64; i++) {
    char ca = tolower(a[i]);
    char cb = tolower(b[i]);
    diff |= (uint8_t)(ca ^ cb);
  }
  return (diff == 0);
}

void publishOtaStatus(const char* state, int progress, const String& msg, const String& ver = "") {
  if (!mqttClient.connected()) return;
  String targetVer = ver.length() > 0 ? ver : String(FIRMWARE_VERSION);
  String safeMsg = msg;
  safeMsg.replace("\"", "'");
  String json = String("{\"state\":\"") + state +
                "\",\"version\":\"" + targetVer +
                "\",\"progress\":" + String(progress) +
                ",\"message\":\"" + safeMsg + "\"}";
  mqttClient.publish(topicOtaStatus.c_str(), json.c_str(), false);
}

void performSecureOtaUpdate() {
  pendingOta = false;
  Serial.println("\n=======================================================");
  Serial.printf("[OTA SEGURO] Iniciando atualizacao para v%s (%d bytes)\n",
                otaTargetVersion.c_str(), otaTargetSize);
  Serial.println("=======================================================");

  publishOtaStatus("starting", 5, "Assinatura HMAC-SHA256 validada! Conectando ao servidor...", otaTargetVersion);

  // Anti-SSRF: O ESP32 conecta exclusivamente ao host confiável (cfgMqttHost)
  String url = String("http://") + cfgMqttHost + ":" + String(otaTargetPort) +
               "/api/v1/ota/download/" + otaTargetToken;
  String dlAuth = computeHmacSha256Hex(
    String("download|") + cfgDeviceId + "|" + otaTargetToken,
    cfgOtaSecret
  );

  WiFiClient otaWifiClient;
  HTTPClient http;
  http.setTimeout(15000);

  if (!http.begin(otaWifiClient, url)) {
    Serial.println("[OTA ERRO] Falha ao inicializar cliente HTTP.");
    publishOtaStatus("failed", 0, "Falha ao inicializar cliente HTTP.", otaTargetVersion);
    return;
  }

  http.addHeader("X-Device-ID", cfgDeviceId);
  http.addHeader("X-OTA-Auth", dlAuth);

  int httpCode = http.GET();
  if (httpCode != HTTP_CODE_OK) {
    Serial.printf("[OTA ERRO] Servidor recusou download (HTTP %d).\n", httpCode);
    publishOtaStatus("failed", 0, String("Download recusado pelo servidor (HTTP ") + httpCode + ").", otaTargetVersion);
    http.end();
    return;
  }

  int contentLength = http.getSize();
  if (contentLength != otaTargetSize || contentLength <= 65536) {
    Serial.printf("[OTA ERRO] Tamanho divergente (HTTP=%d vs Assinado=%d).\n", contentLength, otaTargetSize);
    publishOtaStatus("failed", 0, "Tamanho do binario diverge da assinatura.", otaTargetVersion);
    http.end();
    return;
  }

  if (!Update.begin(contentLength, U_FLASH)) {
    Serial.printf("[OTA ERRO] Espaco insuficiente na particao OTA (err=%d).\n", Update.getError());
    publishOtaStatus("failed", 0, "Falha ao abrir particao OTA secundaria.", otaTargetVersion);
    http.end();
    return;
  }

  mbedtls_sha256_context shaCtx;
  mbedtls_sha256_init(&shaCtx);
  mbedtls_sha256_starts(&shaCtx, 0);

  WiFiClient* stream = http.getStreamPtr();
  uint8_t buff[1024];
  int totalWritten = 0;
  int lastReportedMilestone = 0;
  bool headerChecked = false;
  unsigned long lastDataAt = millis();

  publishOtaStatus("downloading", 10, "Baixando e verificando blocos na Flash...", otaTargetVersion);

  while (http.connected() && totalWritten < contentLength) {
    size_t avail = stream->available();
    if (avail > 0) {
      size_t toRead = (avail > sizeof(buff)) ? sizeof(buff) : avail;
      int readBytes = stream->readBytes(buff, toRead);
      if (readBytes > 0) {
        if (!headerChecked) {
          headerChecked = true;
          if (buff[0] != 0xE9) {
            Serial.printf("[OTA SEGURANCA] Magic byte invalido (0x%02X != 0xE9)! Abortando.\n", buff[0]);
            Update.abort();
            mbedtls_sha256_free(&shaCtx);
            http.end();
            publishOtaStatus("failed", 0, "Cabecalho ESP32 invalido (Magic Byte != 0xE9).", otaTargetVersion);
            return;
          }
        }

        size_t written = Update.write(buff, readBytes);
        if (written != (size_t)readBytes) {
          Serial.println("[OTA ERRO] Falha de escrita na memoria Flash!");
          Update.abort();
          mbedtls_sha256_free(&shaCtx);
          http.end();
          publishOtaStatus("failed", 0, "Erro de escrita na particao Flash.", otaTargetVersion);
          return;
        }

        mbedtls_sha256_update(&shaCtx, buff, readBytes);
        totalWritten += readBytes;
        lastDataAt = millis();

        int pct = (totalWritten * 100) / contentLength;
        int milestone = (pct / 25) * 25;
        if (milestone > lastReportedMilestone && milestone < 100) {
          lastReportedMilestone = milestone;
          mqttClient.loop();
          publishOtaStatus("downloading", milestone, String("Gravando particao OTA: ") + milestone + "%", otaTargetVersion);
        }
      }
    } else {
      if (millis() - lastDataAt > 15000) {
        Serial.println("[OTA ERRO] Timeout de rede durante transferencia.");
        Update.abort();
        mbedtls_sha256_free(&shaCtx);
        http.end();
        publishOtaStatus("failed", 0, "Timeout de rede durante download OTA.", otaTargetVersion);
        return;
      }
      delay(5);
    }
  }

  uint8_t shaOut[32];
  mbedtls_sha256_finish(&shaCtx, shaOut);
  mbedtls_sha256_free(&shaCtx);
  http.end();

  if (totalWritten != contentLength) {
    Serial.printf("[OTA ERRO] Download incompleto (%d/%d bytes).\n", totalWritten, contentLength);
    Update.abort();
    publishOtaStatus("failed", 0, "Download incompleto. Particao anterior mantida.", otaTargetVersion);
    return;
  }

  String computedSha256 = bytesToHexLower(shaOut, 32);
  if (!constantTimeHexEquals(computedSha256, otaTargetSha256)) {
    Serial.printf("[OTA SEGURANCA] SHA-256 divergente!\n  Calculado: %s\n  Esperado : %s\n",
                  computedSha256.c_str(), otaTargetSha256.c_str());
    Update.abort();
    publishOtaStatus("failed", 0, "Falha de integridade SHA-256! Rollback automatico.", otaTargetVersion);
    return;
  }

  if (!Update.end(true)) {
    Serial.printf("[OTA ERRO] Update.end falhou (err=%d).\n", Update.getError());
    publishOtaStatus("failed", 0, "Falha na validacao final da imagem ESP-IDF.", otaTargetVersion);
    return;
  }

  Serial.println("[OTA SUCESSO] SHA-256 verificado e nova particao ativada! Reiniciando...");
  publishOtaStatus("rebooting", 100, "SHA-256 verificado com sucesso! Reiniciando ESP32...", otaTargetVersion);
  for (int i = 0; i < 6; i++) {
    mqttClient.loop();
    delay(80);
  }
  ESP.restart();
}

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

bool extractJsonString(const String& json, const char* key, String &outVal) {
  String searchKey = String("\"") + key + "\"";
  int keyIdx = json.indexOf(searchKey);
  if (keyIdx < 0) return false;
  int colonIdx = json.indexOf(':', keyIdx);
  if (colonIdx < 0) return false;
  int firstQuote = json.indexOf('"', colonIdx + 1);
  if (firstQuote < 0) return false;
  int secondQuote = json.indexOf('"', firstQuote + 1);
  if (secondQuote < 0) return false;
  outVal = json.substring(firstQuote + 1, secondQuote);
  return true;
}

void onMqttMessage(char* topic, byte* payload, unsigned int length) {
  String message = "";
  for (unsigned int i = 0; i < length; i++) {
    message += (char)payload[i];
  }
  message.trim();

  Serial.printf("[MQTT SUB] Topico: %s | Payload: %s\n", topic, message.c_str());

  if (String(topic) == topicConfigSet) {
    // 1. Atualização de faixa ideal de temperatura
    float newMin = minTempC;
    float newMax = maxTempC;
    bool hasMin = extractJsonFloat(message, "min_temp_c", newMin);
    bool hasMax = extractJsonFloat(message, "max_temp_c", newMax);
    if ((hasMin || hasMax) && newMin < newMax) {
      minTempC = newMin;
      maxTempC = newMax;
      preferences.begin("friosafe_cfg", false);
      preferences.putFloat("min_temp", minTempC);
      preferences.putFloat("max_temp", maxTempC);
      preferences.end();
      Serial.printf("[CONFIG] Nova faixa ideal salva em NVS: %.1f C a %.1f C\n", minTempC, maxTempC);
    }

    // 1.b Atualização dinâmica do intervalo de leitura/telemetria (segundos)
    float newTelSecFloat = 0;
    if (extractJsonFloat(message, "telemetry_interval_s", newTelSecFloat)) {
      int newTelSec = (int)newTelSecFloat;
      if (newTelSec >= 2 && newTelSec <= 3600) {
        telemetryIntervalMs = (unsigned long)newTelSec * 1000UL;
        preferences.begin("friosafe_cfg", false);
        preferences.putInt("tel_int_s", newTelSec);
        preferences.end();
        Serial.printf("[CONFIG] Novo intervalo de leitura salvo em NVS: %d s\n", newTelSec);
      }
    }

    // 2. Comando remoto para abrir o Ponto de Acesso Wi-Fi (Portal Cativo)
    if (message.indexOf("\"start_ap\":true") >= 0 || message.indexOf("\"start_ap\": true") >= 0) {
      startCaptivePortalAP();
    }

    // 3. Atualização remota de credenciais Wi-Fi via MQTT (opcional pela interface web)
    String newSsid = "";
    String newPass = "";
    if (extractJsonString(message, "wifi_ssid", newSsid) && newSsid.length() > 0) {
      extractJsonString(message, "wifi_pass", newPass);
      String newHost = cfgMqttHost;
      extractJsonString(message, "mqtt_host", newHost);
      float portFloat = cfgMqttPort;
      extractJsonFloat(message, "mqtt_port", portFloat);
      saveConfigurationToNVS(newSsid, newPass, newHost, (int)portFloat, cfgDeviceId);
      pendingReboot = true;
      rebootScheduledAt = millis() + 1000;
    }
  } else if (String(topic) == topicOtaTrigger) {
    // 4. Ordem de atualização remota de firmware (OTA Seguro com HMAC-SHA256)
    String ver = "", token = "", sha256 = "", sig = "";
    float sizeFloat = 0, portFloat = 8000;
    extractJsonString(message, "version", ver);
    extractJsonString(message, "token", token);
    extractJsonString(message, "sha256", sha256);
    extractJsonString(message, "sig", sig);
    extractJsonFloat(message, "size", sizeFloat);
    extractJsonFloat(message, "port", portFloat);

    int fSize = (int)sizeFloat;
    int fPort = (int)portFloat;

    if (ver.length() == 0 || token.length() == 0 || sha256.length() != 64 || sig.length() != 64 || fSize <= 0) {
      Serial.println("[OTA SEGURANCA] Payload OTA incompleto ou malformado. Ignorando.");
      publishOtaStatus("failed", 0, "Payload OTA malformado rejeitado.");
      return;
    }

    String canonical = cfgDeviceId + "|" + ver + "|" + token + "|" + String(fSize) + "|" + sha256;
    String expectedSig = computeHmacSha256Hex(canonical, cfgOtaSecret);

    if (!constantTimeHexEquals(expectedSig, sig)) {
      Serial.println("[OTA SEGURANCA] ALERTA: Assinatura HMAC-SHA256 invalida! Tentativa OTA bloqueada.");
      publishOtaStatus("failed", 0, "Bloqueado: assinatura criptografica HMAC-SHA256 invalida!", ver);
      return;
    }

    otaTargetVersion = ver;
    otaTargetToken   = token;
    otaTargetSha256  = sha256;
    otaTargetSize    = fSize;
    otaTargetPort    = (fPort > 0 && fPort <= 65535) ? fPort : 8000;
    pendingOta       = true;
  }
}

// ------------------------------------------------------------------------------
// 9. GERENCIAMENTO DE CONEXÃO WI-FI + FALLBACK AUTOMÁTICO PARA AP
// ------------------------------------------------------------------------------
void connectWiFiInitial() {
  Serial.printf("\n[WiFi] Conectando a '%s'", cfgWifiSsid.c_str());
  WiFi.mode(WIFI_STA);
  WiFi.begin(cfgWifiSsid.c_str(), cfgWifiPass.c_str());

  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 30) {
    delay(500);
    Serial.print(".");
    attempts++;
    if (digitalRead(PIN_BOOT_BUTTON) == LOW) {
      Serial.println("\n[BOOT BTN] Botao BOOT pressionado! Abrindo Portal AP imediatamente.");
      break;
    }
  }

  lastWifiAttemptAt = millis();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.printf("\n[WiFi] Conectado! IP: %s | Painel de Configuracao do ESP32: http://%s/\n",
                  WiFi.localIP().toString().c_str(),
                  WiFi.localIP().toString().c_str());
  } else {
    Serial.println("\n[WiFi] Nao foi possivel conectar ao Wi-Fi configurado. Ativando Portal AP de Configuracao!");
    startCaptivePortalAP();
  }
}

void checkBootButtonAndPortal() {
  // Verifica se o usuário segurou o botão BOOT (GPIO 0) por 1.5s para abrir o AP
  if (digitalRead(PIN_BOOT_BUTTON) == LOW) {
    if (bootBtnPressedAt == 0) {
      bootBtnPressedAt = millis();
    } else if (millis() - bootBtnPressedAt >= 1500 && !apPortalActive) {
      Serial.println("[BOOT BTN] Botao BOOT segurado! Ativando Ponto de Acesso FrioSafe-Config...");
      startCaptivePortalAP();
    }
  } else {
    bootBtnPressedAt = 0;
  }

  // Se o Portal AP estiver ativo e o Wi-Fi principal já estiver conectado, desliga o AP após o timeout
  if (apPortalActive && WiFi.status() == WL_CONNECTED) {
    if (millis() - apPortalStartedAt >= AP_PORTAL_TIMEOUT_MS && WiFi.softAPgetStationNum() == 0) {
      stopCaptivePortalAP();
    }
  }
}

void ensureWiFiConnected() {
  if (WiFi.status() == WL_CONNECTED) return;

  unsigned long now = millis();
  if (!apPortalActive) {
    startCaptivePortalAP();
  }

  // Enquanto ninguém estiver conectado no AP, tenta reconectar ao Wi-Fi da loja a cada 20s
  if (WiFi.softAPgetStationNum() == 0 && (now - lastWifiAttemptAt >= WIFI_RETRY_INTERVAL_MS)) {
    lastWifiAttemptAt = now;
    Serial.printf("[WiFi] Tentando reconectar em background a '%s'...\n", cfgWifiSsid.c_str());
    WiFi.begin(cfgWifiSsid.c_str(), cfgWifiPass.c_str());
  }
}

// ------------------------------------------------------------------------------
// 10. CONEXÃO MQTT COM LAST WILL AND TESTAMENT (LWT)
// ------------------------------------------------------------------------------
bool reconnectMQTT() {
  if (WiFi.status() != WL_CONNECTED) return false;

  String clientId = String("esp32_friosafe_") + cfgDeviceId;
  Serial.printf("[MQTT] Conectando ao Broker %s:%d como '%s'... ",
                cfgMqttHost.c_str(), cfgMqttPort, clientId.c_str());

  const int willQos = 1;
  const bool willRetain = true;
  const char* willPayload = "offline";

  bool connected = false;
  if (cfgMqttUser.length() > 0) {
    connected = mqttClient.connect(
      clientId.c_str(),
      cfgMqttUser.c_str(),
      cfgMqttPass.c_str(),
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
    mqttClient.publish(topicStatus.c_str(), "online", true);
    mqttClient.subscribe(topicConfigSet.c_str(), 1);
    mqttClient.subscribe(topicOtaTrigger.c_str(), 1);
    return true;
  } else {
    Serial.printf("FALHA (rc=%d)\n", mqttClient.state());
    return false;
  }
}

// ------------------------------------------------------------------------------
// 11. LEITURA DO SENSOR DALLAS DS18B20
// ------------------------------------------------------------------------------
float readFreezerTemperatureC(bool &isRealSensor) {
  float tempC = DEVICE_DISCONNECTED_C;
  if (readDS18B20Native(detectedSensorPin, tempC, true)) {
    isRealSensor = true;
    lastPullupPins = String(detectedSensorPin);
    Serial.printf("[DS18B20] Temperatura real lida no GPIO %d: %.2f C\n", detectedSensorPin, tempC);
    return tempC;
  }

  const int candidatePins[] = {4, 2, 15, 5, 16, 17, 18, 19, 21, 22, 23, 12, 13, 14, 25, 26, 27, 32, 33};
  String pullupPins = "";
  for (unsigned int i = 0; i < sizeof(candidatePins) / sizeof(candidatePins[0]); i++) {
    int p = candidatePins[i];
    pinMode(p, INPUT_PULLDOWN);
    gpio_pullup_dis((gpio_num_t)p);
    gpio_pulldown_en((gpio_num_t)p);
    delayMicroseconds(100);
    if (gpio_get_level((gpio_num_t)p) == 1) {
      pullupPins += String(p) + " ";
    }
    if (owReset(p)) {
      Serial.printf("[DS18B20] Pulso de presenca detectado no GPIO %d!\n", p);
      if (readDS18B20Native(p, tempC, (p == PIN_ONEWIRE_DS18B20))) {
        detectedSensorPin = p;
        isRealSensor = true;
        lastPullupPins = String(p);
        Serial.printf("[DS18B20] Sensor detectado automaticamente no GPIO %d! Temp: %.2f C\n", p, tempC);
        return tempC;
      }
    }
  }
  pullupPins.trim();
  lastPullupPins = pullupPins;
  Serial.printf("[DIAGNOSTICO] Pinos com resistor pull-up p/ 3.3V detectado: [%s]\n", pullupPins.c_str());

  isRealSensor = false;
  Serial.println("[AVISO] Sensor DS18B20 nao detectado!");
  if (SIMULATE_SENSOR_IF_DISCONNECTED) {
    float simulated = -2.5 + ((random(-15, 16)) / 10.0);
    Serial.printf("[SIMULACAO] Usando temperatura simulada de bancada: %.2f C\n", simulated);
    return simulated;
  }
  return NAN;
}

// ------------------------------------------------------------------------------
// SETUP
// ------------------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(400);

  bootSessionId = String(esp_random() & 0xFFFF, HEX);

  pinMode(PIN_STATUS_LED, OUTPUT);
  digitalWrite(PIN_STATUS_LED, LOW);
  pinMode(PIN_BOOT_BUTTON, INPUT_PULLUP);

  owInitPin(PIN_ONEWIRE_DS18B20);

  loadConfigurationFromNVS();
  connectWiFiInitial();
  setupWebServer();

  mqttClient.setBufferSize(768);
  mqttClient.setServer(cfgMqttHost.c_str(), cfgMqttPort);
  mqttClient.setCallback(onMqttMessage);
  mqttClient.setKeepAlive(15);
}

// ------------------------------------------------------------------------------
// LOOP PRINCIPAL
// ------------------------------------------------------------------------------
void loop() {
  if (pendingReboot && millis() >= rebootScheduledAt) {
    Serial.println("[SISTEMA] Reiniciando ESP32 para aplicar novas configuracoes...");
    delay(100);
    ESP.restart();
  }

  if (pendingOta && WiFi.status() == WL_CONNECTED) {
    performSecureOtaUpdate();
  }

  webServer.handleClient();
  if (apPortalActive) {
    dnsServer.processNextRequest();
  }

  checkBootButtonAndPortal();
  ensureWiFiConnected();

  if (WiFi.status() == WL_CONNECTED) {
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
  }

  unsigned long now = millis();
  if (now - lastTelemetryTime >= telemetryIntervalMs) {
    lastTelemetryTime = now;
    bool isRealSensor = false;
    float tempC = readFreezerTemperatureC(isRealSensor);
    lastReadTempC = tempC;
    lastReadIsReal = isRealSensor;

    if (!isnan(tempC)) {
      packetSequence++;
      unsigned long uptimeSec = now / 1000;
      String eventId = cfgDeviceId + "_" + bootSessionId + "_" + String(packetSequence);

      bool outOfRange = (tempC < minTempC || tempC > maxTempC);
      if (outOfRange) {
        digitalWrite(PIN_STATUS_LED, HIGH);
      } else {
        digitalWrite(PIN_STATUS_LED, HIGH);
        delay(80);
        digitalWrite(PIN_STATUS_LED, LOW);
      }

      String ipStr = (WiFi.status() == WL_CONNECTED) ? WiFi.localIP().toString() : "0.0.0.0";
      int rssiVal  = (WiFi.status() == WL_CONNECTED) ? WiFi.RSSI() : 0;

      char payload[512];
      snprintf(
        payload,
        sizeof(payload),
        "{\"event_id\":\"%s\",\"device_id\":\"%s\",\"fw_version\":\"%s\",\"temperature_c\":%.2f,\"min_temp_c\":%.1f,\"max_temp_c\":%.1f,\"sensor_real\":%s,\"wifi_ssid\":\"%s\",\"wifi_ip\":\"%s\",\"wifi_rssi\":%d,\"ap_active\":%s,\"uptime_s\":%lu}",
        eventId.c_str(),
        cfgDeviceId.c_str(),
        FIRMWARE_VERSION,
        tempC,
        minTempC,
        maxTempC,
        isRealSensor ? "true" : "false",
        cfgWifiSsid.c_str(),
        ipStr.c_str(),
        rssiVal,
        apPortalActive ? "true" : "false",
        uptimeSec
      );

      if (mqttClient.connected()) {
        mqttClient.publish(topicTelemetry.c_str(), payload, false);
        Serial.printf("[TELEMETRIA PUB] %s -> %s\n", topicTelemetry.c_str(), payload);
      }
    }
  }
}
