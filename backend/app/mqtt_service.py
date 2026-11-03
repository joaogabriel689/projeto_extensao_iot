"""Serviço MQTT (Paho-MQTT) com avaliação de faixa térmica, LWT e buffer resiliente."""

from datetime import datetime, timezone
import json
import logging
import math
import re
import threading
from typing import Any, Dict
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import paho.mqtt.client as mqtt
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.buffer import local_buffer
from app.config import settings
from app.database import SessionLocal
from app.models import AlertLog, DeviceConfig, TelemetryLog
from app.notifier import whatsapp_notifier
from app.ota_manager import ota_manager

logger = logging.getLogger(__name__)

_VALID_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
MAX_MQTT_PAYLOAD_BYTES = 4096


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
                (f"{self.prefix}/+/ota/status", 1),
            ]
            client.subscribe(topics)
            logger.info("[MQTT] Conectado ao Mosquitto! Inscrito em: %s", [t[0] for t in topics])
        else:
            logger.error("[MQTT] Falha na conexão com Broker. reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected = False
        logger.warning("[MQTT] Desconectado do Broker (reason_code=%s). Tentando reconectar...", reason_code)

    def publish_threshold_config(
        self,
        device_id: str,
        min_temp_c: float,
        max_temp_c: float,
        telemetry_interval_s: int | None = None,
    ) -> bool:
        """Publica a faixa ideal e intervalo de leitura (com retain=True) para sincronizar o ESP32."""
        topic = f"{self.prefix}/{device_id}/config/set"
        data: Dict[str, Any] = {"min_temp_c": min_temp_c, "max_temp_c": max_temp_c}
        if telemetry_interval_s is not None:
            data["telemetry_interval_s"] = int(telemetry_interval_s)
        payload = json.dumps(data)
        info = self.client.publish(topic, payload=payload, qos=1, retain=True)
        success = info.rc == mqtt.MQTT_ERR_SUCCESS
        logger.info("[MQTT PUB] Tópico=%s | Payload=%s | Sucesso=%s", topic, payload, success)
        return success

    def publish_wifi_command(self, device_id: str, command_data: Dict[str, Any]) -> bool:
        """Publica comando efêmero (retain=False) para configurar Wi-Fi ou abrir AP no ESP32."""
        topic = f"{self.prefix}/{device_id}/config/set"
        payload = json.dumps(command_data)
        info = self.client.publish(topic, payload=payload, qos=1, retain=False)
        success = info.rc == mqtt.MQTT_ERR_SUCCESS
        logger.info("[MQTT WIFI CMD] Tópico=%s | Payload=%s | Sucesso=%s", topic, payload, success)
        return success

    def publish_ota_trigger(self, device_id: str, signed_ota_payload: Dict[str, Any]) -> bool:
        """Publica ordem OTA assinada (HMAC-SHA256) com retain=False para nunca repetir sem autorização."""
        topic = f"{self.prefix}/{device_id}/ota/trigger"
        payload = json.dumps(signed_ota_payload)
        info = self.client.publish(topic, payload=payload, qos=1, retain=False)
        success = info.rc == mqtt.MQTT_ERR_SUCCESS
        logger.info(
            "[MQTT OTA TRIGGER] Dispositivo=%s | Versão=%s | SHA256=%s | Sucesso=%s",
            device_id,
            signed_ota_payload.get("version"),
            signed_ota_payload.get("sha256"),
            success,
        )
        return success

    def get_device_runtime_info(self, device_id: str) -> Dict[str, Any]:
        """Retorna metadados de Wi-Fi, versão de firmware e estado de porta aberta do ESP32."""
        mem = self._memory_state.get(device_id, {})
        return {
            "wifi_ssid": mem.get("wifi_ssid"),
            "wifi_ip": mem.get("wifi_ip"),
            "wifi_rssi": mem.get("wifi_rssi"),
            "ap_active": mem.get("ap_active"),
            "fw_version": mem.get("fw_version"),
            "out_of_range_since": mem.get("out_of_range_since"),
            "out_of_range_elapsed_s": mem.get("out_of_range_elapsed_s", 0),
            "in_door_open_delay": bool(mem.get("in_door_open_delay", False)),
        }

    @staticmethod
    def is_in_quiet_hours(cfg: Dict[str, Any], now_utc: datetime | None = None) -> bool:
        """Verifica se o dispositivo está dentro da janela 'Modo Sem Alertas' (desligamento noturno)."""
        if not cfg.get("quiet_hours_enabled", False):
            return False

        start_str = str(cfg.get("quiet_hours_start") or settings.default_quiet_hours_start or "23:00").strip()
        end_str = str(cfg.get("quiet_hours_end") or settings.default_quiet_hours_end or "07:00").strip()
        try:
            sh, sm = (int(x) for x in start_str.split(":")[:2])
            eh, em = (int(x) for x in end_str.split(":")[:2])
            start_min = sh * 60 + sm
            end_min = eh * 60 + em
        except (ValueError, TypeError):
            return False

        if start_min == end_min:
            return True

        dt_utc = now_utc or datetime.now(timezone.utc)
        if dt_utc.tzinfo is None:
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        try:
            local_tz = ZoneInfo(settings.tz)
            dt_local = dt_utc.astimezone(local_tz)
        except (ZoneInfoNotFoundError, Exception):
            dt_local = dt_utc

        cur_min = dt_local.hour * 60 + dt_local.minute
        if start_min < end_min:
            # Janela no mesmo dia (ex: 01:00 às 06:00)
            return start_min <= cur_min < end_min
        # Janela que cruza a meia-noite (ex: 23:00 às 07:00)
        return cur_min >= start_min or cur_min < end_min

    def _get_or_create_device_config(self, device_id: str) -> Dict[str, Any] | None:
        """Obtém a configuração do dispositivo no PostgreSQL ou no cache SQLite local."""
        db = SessionLocal()
        try:
            device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
            if not device:
                if (
                    device_id != settings.default_device_id
                    and not settings.mqtt_auto_register_unknown_devices
                ):
                    logger.warning(
                        "[SEGURANÇA MQTT] Dispositivo desconhecido '%s' ignorado (MQTT_AUTO_REGISTER_UNKNOWN_DEVICES=false).",
                        device_id,
                    )
                    return None
                device = DeviceConfig(
                    device_id=device_id,
                    name=settings.default_device_name
                    if device_id == settings.default_device_id
                    else f"Freezer / Camara ({device_id})",
                    min_temp_c=settings.default_min_temp_c,
                    max_temp_c=settings.default_max_temp_c,
                    alert_delay_seconds=settings.alert_delay_seconds,
                    alert_cooldown_seconds=settings.alert_cooldown_seconds,
                    telemetry_interval_seconds=settings.default_telemetry_interval_seconds,
                    merchant_phone=settings.merchant_whatsapp_number,
                    notifications_enabled=settings.whatsapp_notifications_enabled,
                    quiet_hours_enabled=settings.default_quiet_hours_enabled,
                    quiet_hours_start=settings.default_quiet_hours_start,
                    quiet_hours_end=settings.default_quiet_hours_end,
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
                "alert_delay_seconds": int(
                    device.alert_delay_seconds
                    if device.alert_delay_seconds is not None
                    else settings.alert_delay_seconds
                ),
                "alert_cooldown_seconds": int(device.alert_cooldown_seconds),
                "telemetry_interval_seconds": int(
                    device.telemetry_interval_seconds
                    if device.telemetry_interval_seconds is not None
                    else settings.default_telemetry_interval_seconds
                ),
                "merchant_phone": device.merchant_phone or settings.merchant_whatsapp_number,
                "notifications_enabled": bool(device.notifications_enabled),
                "quiet_hours_enabled": bool(getattr(device, "quiet_hours_enabled", False)),
                "quiet_hours_start": getattr(device, "quiet_hours_start", None) or settings.default_quiet_hours_start,
                "quiet_hours_end": getattr(device, "quiet_hours_end", None) or settings.default_quiet_hours_end,
                "out_of_range_since": device.out_of_range_since,
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
                cached["out_of_range_since"] = mem.get("out_of_range_since")
                cached["in_alert_state"] = mem.get("in_alert_state", False)
                cached["last_alert_at"] = mem.get("last_alert_at")
                return cached
            if (
                device_id != settings.default_device_id
                and not settings.mqtt_auto_register_unknown_devices
            ):
                return None
            return {
                "device_id": device_id,
                "name": settings.default_device_name,
                "min_temp_c": settings.default_min_temp_c,
                "max_temp_c": settings.default_max_temp_c,
                "alert_delay_seconds": settings.alert_delay_seconds,
                "alert_cooldown_seconds": settings.alert_cooldown_seconds,
                "telemetry_interval_seconds": settings.default_telemetry_interval_seconds,
                "merchant_phone": settings.merchant_whatsapp_number,
                "notifications_enabled": settings.whatsapp_notifications_enabled,
                "quiet_hours_enabled": settings.default_quiet_hours_enabled,
                "quiet_hours_start": settings.default_quiet_hours_start,
                "quiet_hours_end": settings.default_quiet_hours_end,
                "out_of_range_since": self._memory_state.get(device_id, {}).get("out_of_range_since"),
                "in_alert_state": self._memory_state.get(device_id, {}).get("in_alert_state", False),
                "last_alert_at": self._memory_state.get(device_id, {}).get("last_alert_at"),
            }
        finally:
            db.close()

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        """Roteia mensagens recebidas conforme a estrutura loja/freezer/<id>/<campo>."""
        try:
            if len(msg.payload) > MAX_MQTT_PAYLOAD_BYTES:
                logger.warning(
                    "[SEGURANÇA MQTT] Payload descartado por exceder %d bytes no tópico %s",
                    MAX_MQTT_PAYLOAD_BYTES,
                    msg.topic,
                )
                return

            topic = msg.topic
            raw_payload = msg.payload.decode("utf-8", errors="replace").strip()
            parts = topic.split("/")
            if len(parts) < 4:
                return

            device_id = parts[2].strip()
            if not _VALID_DEVICE_ID_RE.match(device_id):
                logger.warning("[SEGURANÇA MQTT] device_id inválido descartado: %r", device_id)
                return

            field_path = "/".join(parts[3:])

            if field_path == "telemetry":
                self._handle_telemetry(device_id, raw_payload)
            elif field_path == "status":
                self._handle_status_lwt(device_id, raw_payload)
            elif field_path == "ota/status":
                self._handle_ota_status(device_id, raw_payload)
        except Exception as exc:
            logger.exception("[MQTT] Erro ao processar mensagem no tópico %s: %s", msg.topic, exc)

    def _handle_telemetry(self, device_id: str, raw_payload: str) -> None:
        """Processa leitura de temperatura do ESP32, avalia faixa ideal e persiste."""
        cfg = self._get_or_create_device_config(device_id)
        if not cfg:
            return

        now = datetime.now(timezone.utc)
        event_id = None

        try:
            data = json.loads(raw_payload)
            if isinstance(data, (int, float)):
                temperature_c = float(data)
            else:
                temperature_c = float(data.get("temperature_c", data.get("temp", data.get("temperature"))))
                raw_ev = data.get("event_id")
                if raw_ev:
                    event_id = str(raw_ev).strip()[:128]
                mem = self._memory_state.setdefault(device_id, {})
                if "wifi_ssid" in data and data.get("wifi_ssid") is not None:
                    mem["wifi_ssid"] = str(data.get("wifi_ssid"))[:64]
                if "wifi_ip" in data and data.get("wifi_ip") is not None:
                    mem["wifi_ip"] = str(data.get("wifi_ip"))[:45]
                if "wifi_rssi" in data and data.get("wifi_rssi") is not None:
                    mem["wifi_rssi"] = int(data.get("wifi_rssi"))
                if "ap_active" in data:
                    mem["ap_active"] = bool(data.get("ap_active"))
                if "fw_version" in data and data.get("fw_version") is not None:
                    mem["fw_version"] = str(data.get("fw_version"))[:32]
                if data.get("timestamp"):
                    try:
                        parsed_dt = datetime.fromisoformat(str(data["timestamp"]))
                        now = parsed_dt if parsed_dt.tzinfo else parsed_dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        pass
        except (json.JSONDecodeError, TypeError, ValueError):
            temperature_c = float(raw_payload)

        if not math.isfinite(temperature_c) or temperature_c < -100.0 or temperature_c > 150.0:
            logger.warning(
                "[SEGURANÇA MQTT] Leitura térmica fora dos limites físicos (%.2f°C) ignorada para %s.",
                temperature_c,
                device_id,
            )
            return

        if not event_id:
            event_id = f"{device_id}_{int(now.timestamp() * 1000)}_{uuid.uuid4().hex[:6]}"
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
                device.out_of_range_since = self._memory_state.get(device_id, {}).get("out_of_range_since")
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
        """Verifica filtro de porta aberta/reposição (alert_delay_seconds), disparo de alerta e normalização."""
        device_id = cfg["device_id"]
        mem = self._memory_state.setdefault(device_id, {})

        # Modo Sem Alertas (Desligamento Noturno Programado)
        if self.is_in_quiet_hours(cfg, now):
            mem["out_of_range_since"] = None
            mem["out_of_range_elapsed_s"] = 0
            mem["in_door_open_delay"] = False
            if is_below or is_above:
                logger.info(
                    "[MODO SEM ALERTAS] Device=%s | Temp=%.2f°C fora da faixa, porém dentro do horário silencioso (%s às %s). Alerta suprimido.",
                    device_id,
                    temperature_c,
                    cfg.get("quiet_hours_start"),
                    cfg.get("quiet_hours_end"),
                )
            return

        in_alert = mem.get("in_alert_state", cfg.get("in_alert_state", False))
        last_alert_at: datetime | None = mem.get("last_alert_at") or cfg.get("last_alert_at")
        out_since: datetime | None = mem.get("out_of_range_since") or cfg.get("out_of_range_since")
        delay_s = max(0, int(cfg.get("alert_delay_seconds", settings.alert_delay_seconds)))
        cooldown_s = max(10, int(cfg["alert_cooldown_seconds"]))

        if is_below or is_above:
            if out_since is None:
                out_since = now
                mem["out_of_range_since"] = now
            elif out_since.tzinfo is None:
                out_since = out_since.replace(tzinfo=timezone.utc)
                mem["out_of_range_since"] = out_since

            elapsed_out_s = max(0, int((now - out_since).total_seconds()))
            mem["out_of_range_elapsed_s"] = elapsed_out_s

            # Filtro de Porta Aberta / Reposição: aguarda delay_s contínuos fora da faixa antes do 1º disparo
            if not in_alert and delay_s > 0 and elapsed_out_s < delay_s:
                mem["in_door_open_delay"] = True
                logger.info(
                    "[FILTRO PORTA ABERTA] Device=%s | Temp=%.2f°C fora da faixa há %ds / %ds (aguardando confirmação para evitar alarme falso).",
                    device_id,
                    temperature_c,
                    elapsed_out_s,
                    delay_s,
                )
                return

            mem["in_door_open_delay"] = False
            alert_type = "TEMP_BELOW_MIN" if is_below else "TEMP_ABOVE_MAX"
            should_notify = False

            if not in_alert or last_alert_at is None:
                should_notify = True
            else:
                if last_alert_at.tzinfo is None:
                    last_alert_at = last_alert_at.replace(tzinfo=timezone.utc)
                elapsed_cooldown = (now - last_alert_at).total_seconds()
                if elapsed_cooldown >= cooldown_s:
                    should_notify = True

            if should_notify:
                msg_text = whatsapp_notifier.format_temperature_alert(
                    device_name=cfg["name"],
                    device_id=device_id,
                    temperature_c=temperature_c,
                    min_temp_c=cfg["min_temp_c"],
                    max_temp_c=cfg["max_temp_c"],
                    alert_type=alert_type,
                    duration_seconds=elapsed_out_s,
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
            # Temperatura voltou para a faixa ideal
            if out_since is not None and not in_alert:
                if out_since.tzinfo is None:
                    out_since = out_since.replace(tzinfo=timezone.utc)
                recovered_after_s = max(0, int((now - out_since).total_seconds()))
                logger.info(
                    "[FILTRO PORTA ABERTA] Device=%s | Temperatura normalizou (%.2f°C) após %ds (tolerância=%ds). Alarme falso evitado!",
                    device_id,
                    temperature_c,
                    recovered_after_s,
                    delay_s,
                )

            mem["out_of_range_since"] = None
            mem["out_of_range_elapsed_s"] = 0
            mem["in_door_open_delay"] = False

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
        if status_clean not in ("online", "offline"):
            return
        now = datetime.now(timezone.utc)
        cfg = self._get_or_create_device_config(device_id)
        if not cfg:
            return

        logger.info("[MQTT STATUS / LWT] Device=%s | Novo Status=%s", device_id, status_clean)

        if status_clean == "offline":
            if self.is_in_quiet_hours(cfg, now):
                logger.info(
                    "[MODO SEM ALERTAS] Device=%s ficou offline durante o horário silencioso (%s às %s). Alerta LWT suprimido.",
                    device_id,
                    cfg.get("quiet_hours_start"),
                    cfg.get("quiet_hours_end"),
                )
                db = SessionLocal()
                try:
                    device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
                    if device:
                        device.online_status = "offline"
                        db.commit()
                except Exception as exc:
                    db.rollback()
                    logger.warning("[MQTT] Não foi possível atualizar status offline no DB: %s", exc)
                finally:
                    db.close()
                return

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

    def _handle_ota_status(self, device_id: str, raw_payload: str) -> None:
        """Registra o andamento e resultado da atualização OTA reportado pelo ESP32."""
        try:
            data = json.loads(raw_payload)
            if isinstance(data, dict):
                ota_manager.update_device_ota_status(device_id, data)
                if data.get("version") and data.get("state") in ("success", "rebooting"):
                    mem = self._memory_state.setdefault(device_id, {})
                    mem["fw_version"] = str(data.get("version"))
                logger.info(
                    "[MQTT OTA STATUS] Device=%s | Estado=%s | Progresso=%s%% | Msg=%s",
                    device_id,
                    data.get("state"),
                    data.get("progress", 0),
                    data.get("message", ""),
                )
        except Exception as exc:
            logger.warning("[MQTT OTA STATUS] Payload inválido de %s: %s", device_id, exc)

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
