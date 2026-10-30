"""Serviço MQTT (Paho-MQTT) com avaliação de faixa térmica, LWT e buffer resiliente."""

from datetime import datetime, timezone
import json
import logging
import threading
from typing import Any, Dict
import uuid

import paho.mqtt.client as mqtt
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.buffer import local_buffer
from app.config import settings
from app.database import SessionLocal
from app.models import AlertLog, DeviceConfig, TelemetryLog
from app.notifier import whatsapp_notifier

logger = logging.getLogger(__name__)


class MQTTTelemetryService:
    """Gerencia a conexão com o Broker Mosquitto, processamento de telemetria e alertas."""

    def __init__(self) -> None:
        self.prefix = settings.mqtt_topic_prefix.rstrip("/")
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"coutos_backend_worker_{uuid.uuid4().hex[:6]}",
        )
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)

        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message

        self._connected = False
        self._lock = threading.Lock()
        # Controle em memória de estado de alerta e último envio (funciona mesmo com DB offline)
        self._memory_state: Dict[str, Dict[str, Any]] = {}

    @property
    def is_connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        """Inicia o loop de rede do cliente MQTT em background thread."""
        try:
            self.client.reconnect_delay_set(min_delay=1, max_delay=30)
            self.client.connect_async(settings.mqtt_broker_host, settings.mqtt_broker_port, keepalive=60)
            self.client.loop_start()
            logger.info(
                "[MQTT] Serviço iniciado (Broker=%s:%d, Prefixo=%s/#)",
                settings.mqtt_broker_host,
                settings.mqtt_broker_port,
                self.prefix,
            )
        except Exception as exc:
            logger.error("[MQTT] Erro ao iniciar cliente MQTT: %s", exc)

    def stop(self) -> None:
        """Encerra a conexão com o broker MQTT de forma limpa."""
        try:
            self.client.loop_stop()
            self.client.disconnect()
            self._connected = False
        except Exception as exc:
            logger.warning("[MQTT] Erro ao encerrar cliente MQTT: %s", exc)

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code == 0:
            self._connected = True
            topics = [
                (f"{self.prefix}/+/telemetry", 1),
                (f"{self.prefix}/+/status", 1),
            ]
            client.subscribe(topics)
            logger.info("[MQTT] Conectado ao Mosquitto! Inscrito em: %s", [t[0] for t in topics])
        else:
            logger.error("[MQTT] Falha na conexão com Broker. reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected = False
        logger.warning("[MQTT] Desconectado do Broker (reason_code=%s). Tentando reconectar...", reason_code)

    def publish_threshold_config(self, device_id: str, min_temp_c: float, max_temp_c: float) -> bool:
        """Publica a faixa ideal atualizada (com retain=True) para que o ESP32 também a conheça."""
        topic = f"{self.prefix}/{device_id}/config/set"
        payload = json.dumps({"min_temp_c": min_temp_c, "max_temp_c": max_temp_c})
        info = self.client.publish(topic, payload=payload, qos=1, retain=True)
        success = info.rc == mqtt.MQTT_ERR_SUCCESS
        logger.info("[MQTT PUB] Tópico=%s | Payload=%s | Sucesso=%s", topic, payload, success)
        return success

    def _get_or_create_device_config(self, device_id: str) -> Dict[str, Any]:
        """Obtém a configuração do dispositivo no PostgreSQL ou no cache SQLite local."""
        db = SessionLocal()
        try:
            device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
            if not device:
                device = DeviceConfig(
                    device_id=device_id,
                    name=settings.default_device_name
                    if device_id == settings.default_device_id
                    else f"Freezer / Camara ({device_id})",
                    min_temp_c=settings.default_min_temp_c,
                    max_temp_c=settings.default_max_temp_c,
                    alert_cooldown_seconds=settings.alert_cooldown_seconds,
                    merchant_phone=settings.merchant_whatsapp_number,
                    notifications_enabled=settings.whatsapp_notifications_enabled,
                    online_status="online",
                )
                db.add(device)
                db.commit()
                db.refresh(device)

            cfg = {
                "device_id": device.device_id,
                "name": device.name,
                "min_temp_c": float(device.min_temp_c),
                "max_temp_c": float(device.max_temp_c),
                "alert_cooldown_seconds": int(device.alert_cooldown_seconds),
                "merchant_phone": device.merchant_phone or settings.merchant_whatsapp_number,
                "notifications_enabled": bool(device.notifications_enabled),
                "in_alert_state": bool(device.in_alert_state),
                "last_alert_at": device.last_alert_at,
            }
            local_buffer.cache_device_config(cfg)
            return cfg
        except Exception as exc:
            db.rollback()
            logger.warning("[MQTT] Banco principal indisponível ao ler config (%s). Usando cache local.", exc)
            cached = local_buffer.get_cached_device_config(device_id)
            if cached:
                mem = self._memory_state.get(device_id, {})
                cached["in_alert_state"] = mem.get("in_alert_state", False)
                cached["last_alert_at"] = mem.get("last_alert_at")
                return cached
            return {
                "device_id": device_id,
                "name": settings.default_device_name,
                "min_temp_c": settings.default_min_temp_c,
                "max_temp_c": settings.default_max_temp_c,
                "alert_cooldown_seconds": settings.alert_cooldown_seconds,
                "merchant_phone": settings.merchant_whatsapp_number,
                "notifications_enabled": settings.whatsapp_notifications_enabled,
                "in_alert_state": self._memory_state.get(device_id, {}).get("in_alert_state", False),
                "last_alert_at": self._memory_state.get(device_id, {}).get("last_alert_at"),
            }
        finally:
            db.close()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        """Roteia mensagens recebidas conforme a estrutura loja/freezer/<id>/<campo>."""
        try:
            topic = msg.topic
            raw_payload = msg.payload.decode("utf-8", errors="replace").strip()
            parts = topic.split("/")
            if len(parts) < 4:
                return

            device_id = parts[2]
            field_path = "/".join(parts[3:])

            if field_path == "telemetry":
                self._handle_telemetry(device_id, raw_payload)
            elif field_path == "status":
                self._handle_status_lwt(device_id, raw_payload)
        except Exception as exc:
            logger.exception("[MQTT] Erro ao processar mensagem no tópico %s: %s", msg.topic, exc)

    def _handle_telemetry(self, device_id: str, raw_payload: str) -> None:
        """Processa leitura de temperatura do ESP32, avalia faixa ideal e persiste."""
        now = datetime.now(timezone.utc)
        event_id = None

        try:
            data = json.loads(raw_payload)
            if isinstance(data, (int, float)):
                temperature_c = float(data)
            else:
                temperature_c = float(data.get("temperature_c", data.get("temp", data.get("temperature"))))
                event_id = data.get("event_id")
                if data.get("timestamp"):
                    try:
                        parsed_dt = datetime.fromisoformat(str(data["timestamp"]))
                        now = parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
        except (json.JSONDecodeError, TypeError, ValueError):
            temperature_c = float(raw_payload)

        if not event_id:
            event_id = f"{device_id}_{int(now.timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"

        cfg = self._get_or_create_device_config(device_id)
        min_temp = cfg["min_temp_c"]
        max_temp = cfg["max_temp_c"]

        is_below = temperature_c < min_temp
        is_above = temperature_c > max_temp
        is_out_of_range = is_below or is_above

        logger.info(
            "[TELEMETRIA] Device=%s | Temp=%.2f°C | Faixa=[%.1f°C, %.1f°C] | ForaDaFaixa=%s",
            device_id,
            temperature_c,
            min_temp,
            max_temp,
            is_out_of_range,
        )

        # Avaliação de alerta térmico e envio via WhatsApp (Evolution API)
        self._evaluate_thermal_alert(
            cfg=cfg,
            temperature_c=temperature_c,
            is_below=is_below,
            is_above=is_above,
            now=now,
        )

        # Persistência no PostgreSQL com fallback automático para o Buffer SQLite local
        telemetry_record = {
            "event_id": event_id,
            "device_id": device_id,
            "temperature_c": temperature_c,
            "min_temp_c": min_temp,
            "max_temp_c": max_temp,
            "is_out_of_range": is_out_of_range,
            "replayed_from_buffer": False,
            "recorded_at": now,
        }

        db = SessionLocal()
        try:
            stmt = pg_insert(TelemetryLog).values([telemetry_record])
            stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
            db.execute(stmt)

            device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
            if device:
                device.last_temp_c = temperature_c
                device.last_seen_at = now
                device.online_status = "online"
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("[RESILIÊNCIA] Falha ao gravar no PostgreSQL (%s). Redirecionando para buffer local.", exc)
            local_buffer.enqueue_telemetry(telemetry_record)
        finally:
            db.close()

    def _evaluate_thermal_alert(
        self,
        cfg: Dict[str, Any],
        temperature_c: float,
        is_below: bool,
        is_above: bool,
        now: datetime,
    ) -> None:
        """Verifica se deve disparar alerta de desvio térmico ou de normalização."""
        device_id = cfg["device_id"]
        mem = self._memory_state.setdefault(device_id, {})
        in_alert = mem.get("in_alert_state", cfg.get("in_alert_state", False))
        last_alert_at: datetime | None = mem.get("last_alert_at") or cfg.get("last_alert_at")
        cooldown_s = cfg["alert_cooldown_seconds"]

        if is_below or is_above:
            alert_type = "TEMP_BELOW_MIN" if is_below else "TEMP_ABOVE_MAX"
            should_notify = False

            if not in_alert or last_alert_at is None:
                should_notify = True
            else:
                if last_alert_at.tzinfo is None:
                    last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
                elapsed = (now - last_alert_at).total_seconds()
                if elapsed >= cooldown_s:
                    should_notify = True

            if should_notify:
                msg_text = whatsapp_notifier.format_temperature_alert(
                    device_name=cfg["name"],
                    device_id=device_id,
                    temperature_c=temperature_c,
                    min_temp_c=cfg["min_temp_c"],
                    max_temp_c=cfg["max_temp_c"],
                    alert_type=alert_type,
                )
                sent, err = (False, "Notificações desabilitadas para o dispositivo")
                if cfg.get("notifications_enabled", True):
                    sent, err = whatsapp_notifier.send_message(cfg.get("merchant_phone"), msg_text)

                mem["in_alert_state"] = True
                mem["last_alert_at"] = now
                self._record_alert_in_db(
                    device_id=device_id,
                    alert_type=alert_type,
                    severity="CRITICAL",
                    temperature_c=temperature_c,
                    message=msg_text,
                    whatsapp_sent=sent,
                    whatsapp_error=err,
                    in_alert_state=True,
                    last_alert_at=now,
                )
        else:
            # Temperatura dentro da faixa ideal: se estava em estado de alerta, notifica normalização
            if in_alert:
                msg_text = whatsapp_notifier.format_recovery_alert(
                    device_name=cfg["name"],
                    device_id=device_id,
                    temperature_c=temperature_c,
                    min_temp_c=cfg["min_temp_c"],
                    max_temp_c=cfg["max_temp_c"],
                )
                sent, err = (False, "Notificações desabilitadas para o dispositivo")
                if cfg.get("notifications_enabled", True):
                    sent, err = whatsapp_notifier.send_message(cfg.get("merchant_phone"), msg_text)

                mem["in_alert_state"] = False
                self._record_alert_in_db(
                    device_id=device_id,
                    alert_type="TEMP_NORMALIZED",
                    severity="INFO",
                    temperature_c=temperature_c,
                    message=msg_text,
                    whatsapp_sent=sent,
                    whatsapp_error=err,
                    in_alert_state=False,
                    last_alert_at=last_alert_at,
                )

    def _handle_status_lwt(self, device_id: str, status_payload: str) -> None:
        """Processa mensagens de presença ('online' ou 'offline' via Last Will and Testament)."""
        status_clean = status_payload.lower().strip()
        now = datetime.now(timezone.utc)
        cfg = self._get_or_create_device_config(device_id)

        logger.info("[MQTT STATUS / LWT] Device=%s | Novo Status=%s", device_id, status_clean)

        if status_clean == "offline":
            msg_text = whatsapp_notifier.format_lwt_offline_alert(
                device_name=cfg["name"],
                device_id=device_id,
            )
            sent, err = (False, "Notificações desabilitadas para o dispositivo")
            if cfg.get("notifications_enabled", True):
                sent, err = whatsapp_notifier.send_message(cfg.get("merchant_phone"), msg_text)

            self._record_alert_in_db(
                device_id=device_id,
                alert_type="DEVICE_OFFLINE_LWT",
                severity="CRITICAL",
                temperature_c=None,
                message=msg_text,
                whatsapp_sent=sent,
                whatsapp_error=err,
                in_alert_state=cfg.get("in_alert_state", False),
                last_alert_at=now,
                online_status="offline",
            )
        else:
            db = SessionLocal()
            try:
                device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
                if device:
                    device.online_status = "online"
                    device.last_seen_at = now
                    db.commit()
            except Exception as exc:
                db.rollback()
                logger.warning("[MQTT] Não foi possível atualizar status online no DB: %s", exc)
            finally:
                db.close()

    def _record_alert_in_db(
        self,
        device_id: str,
        alert_type: str,
        severity: str,
        temperature_c: float | None,
        message: str,
        whatsapp_sent: bool,
        whatsapp_error: str | None,
        in_alert_state: bool,
        last_alert_at: datetime | None,
        online_status: str | None = None,
    ) -> None:
        """Persiste o log de alerta e atualiza estado do dispositivo no PostgreSQL."""
        db = SessionLocal()
        try:
            alert = AlertLog(
                device_id=device_id,
                alert_type=alert_type,
                severity=severity,
                temperature_c=temperature_c,
                message=message,
                whatsapp_sent=whatsapp_sent,
                whatsapp_error=whatsapp_error,
            )
            db.add(alert)

            device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
            if device:
                device.in_alert_state = in_alert_state
                if last_alert_at:
                    device.last_alert_at = last_alert_at
                if online_status:
                    device.online_status = online_status
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("[ALERTA] Não foi possível gravar AlertLog no PostgreSQL: %s", exc)
        finally:
            db.close()


mqtt_service = MQTTTelemetryService()
