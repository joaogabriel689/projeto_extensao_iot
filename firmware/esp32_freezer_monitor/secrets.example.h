#ifndef SECRETS_H
#define SECRETS_H

// ==============================================================================
// CREDENCIAIS DE REDE E BROKER MQTT (ARQUIVO DE EXEMPLO)
// Para usar suas credenciais reais sem enviá-las ao GitHub público:
//   1. Copie este arquivo para "secrets.h" na mesma pasta
//   2. Edite "secrets.h" com sua rede Wi-Fi e o IP do computador com Docker
//   (O arquivo "secrets.h" já está protegido no .gitignore)
// ==============================================================================

#define WIFI_SSID         "SUA_REDE_WIFI"
#define WIFI_PASSWORD     "SUA_SENHA_WIFI"

// IP do computador/notebook onde o container Mosquitto está rodando
#define MQTT_BROKER_IP    "192.168.1.100"
#define MQTT_BROKER_PORT  1884

// Deixe vazio ("") apenas em bancada isolada; em produção (VPS), use as credenciais do .env
#define MQTT_USERNAME     "coutos_mqtt"
#define MQTT_PASSWORD     "troque_senha_mqtt_segura_2026"

// Identificador único do equipamento monitorado
#define DEVICE_ID         "camara01"

// Senha WPA2 (mínimo 8 caracteres) para o Ponto de Acesso de configuração (FrioSafe-Config)
// Também exigida no formulário web local (http://192.168.4.1) para alterar Wi-Fi/MQTT
#define AP_PORTAL_PASSWORD "coutos2026"

// Em produção real mantenha false para que falha física do sensor DS18B20 não seja mascarada
#define SIMULATE_SENSOR_IF_DISCONNECTED_DEFAULT false

// Chave secreta compartilhada para validação HMAC-SHA256 de atualizações OTA remotas
// Deve ser idêntica ao OTA_SHARED_SECRET definido no arquivo .env do servidor
#define OTA_SHARED_SECRET "coutos_ota_hmac_secret_2026"

#endif
