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

// Deixe vazio ("") se allow_anonymous estiver ativo na rede local
#define MQTT_USERNAME     ""
#define MQTT_PASSWORD     ""

// Identificador único do equipamento monitorado
#define DEVICE_ID         "camara01"

// Chave secreta compartilhada para validação HMAC-SHA256 de atualizações OTA remotas
// Deve ser idêntica ao OTA_SHARED_SECRET definido no arquivo .env do servidor
#define OTA_SHARED_SECRET "coutos_ota_hmac_secret_2026"

#endif
