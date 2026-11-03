#!/bin/sh
set -e

CONF_SRC="/mosquitto/config/mosquitto.conf"
CONF_RUN="/tmp/mosquitto_runtime.conf"
PASSWD_FILE="/tmp/mosquitto_passwd"
ACL_FILE="/tmp/mosquitto_acl"

cp "$CONF_SRC" "$CONF_RUN"

if [ -n "$MQTT_USERNAME" ] && [ -n "$MQTT_PASSWORD" ]; then
    echo "[MOSQUITTO SEC] Habilitando autenticacao obrigatoria (allow_anonymous false) e ACL..."
    mosquitto_passwd -b -c "$PASSWD_FILE" "$MQTT_USERNAME" "$MQTT_PASSWORD"
    chmod 0600 "$PASSWD_FILE"

    cat <<EOF > "$ACL_FILE"
user $MQTT_USERNAME
topic readwrite ${MQTT_TOPIC_PREFIX:-loja/freezer}/#
topic read \$SYS/#
EOF
    chmod 0600 "$ACL_FILE"

    cat <<EOF >> "$CONF_RUN"

# Autenticacao e Controle de Acesso (ACL) gerados automaticamente
allow_anonymous false
password_file $PASSWD_FILE
acl_file $ACL_FILE
EOF
else
    echo "[MOSQUITTO AVISO] MQTT_USERNAME/MQTT_PASSWORD vazios. Operando com allow_anonymous true (recomendado apenas em rede local isolada)."
    cat <<EOF >> "$CONF_RUN"

allow_anonymous true
EOF
fi

exec /usr/sbin/mosquitto -c "$CONF_RUN"
