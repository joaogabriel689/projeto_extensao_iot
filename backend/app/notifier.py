"""Cliente de integração com a Evolution API para envio de alertas via WhatsApp."""

import logging
from typing import Tuple
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class WhatsAppNotifier:
    """Envia notificações operacionais e alertas térmicos via Evolution API v2."""

    def __init__(self) -> None:
        self.base_url = settings.evolution_api_url.rstrip("/")
        self.api_key = settings.evolution_api_key
        self.instance = settings.evolution_instance_name

    def send_message(self, phone_number: str | None, text: str) -> Tuple[bool, str | None]:
        """Envia mensagem de texto para o WhatsApp do comerciante.

        Retorna (sucesso: bool, erro: str | None).
        """
        if not settings.whatsapp_notifications_enabled:
            logger.info("[WHATSAPP DESABILITADO] Mensagem não enviada: %s", text)
            return False, "Notificações WhatsApp desabilitadas na configuração."

        target_phone = (phone_number or settings.merchant_whatsapp_number or "").strip()
        # Mantém apenas dígitos no número de telefone
        clean_phone = "".join(ch for ch in target_phone if ch.isdigit())

        if not clean_phone:
            msg = "Número de WhatsApp do comerciante não configurado."
            logger.warning("[WHATSAPP] %s", msg)
            return False, msg

        if not self.api_key:
            msg = "EVOLUTION_API_KEY não configurada no ambiente."
            logger.warning("[WHATSAPP] %s", msg)
            return False, msg

        url = f"{self.base_url}/message/sendText/{self.instance}"
        headers = {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "number": clean_phone,
            "text": text,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=payload, headers=headers)
                if 200 <= response.status_code < 300:
                    logger.info(
                        "[WHATSAPP ENVIADO] Destino=%s | Instância=%s",
                        clean_phone,
                        self.instance,
                    )
                    return True, None

                err_detail = f"HTTP {response.status_code}: {response.text[:300]}"
                logger.error("[WHATSAPP ERRO] Falha na Evolution API: %s", err_detail)
                return False, err_detail
        except Exception as exc:
            err_detail = f"Erro de conexão com Evolution API: {exc}"
            logger.error("[WHATSAPP EXCEÇÃO] %s", err_detail)
            return False, err_detail

    @staticmethod
    def format_temperature_alert(
        device_name: str,
        device_id: str,
        temperature_c: float,
        min_temp_c: float,
        max_temp_c: float,
        alert_type: str,
    ) -> str:
        """Formata a mensagem de alerta de temperatura fora da faixa ideal."""
        if alert_type == "TEMP_BELOW_MIN":
            status_desc = f"❄️ *ABAIXO DA FAIXA IDEAL* (Mínimo: {min_temp_c:.1f} °C)"
        else:
            status_desc = f"🔥 *ACIMA DA FAIXA IDEAL* (Máximo: {max_temp_c:.1f} °C)"

        return (
            "🚨 *ALERTA TÉRMICO - COUTOS CONVENIÊNCIA*\n\n"
            f"📍 *Equipamento:* {device_name} (`{device_id}`)\n"
            f"⚠️ *Status:* {status_desc}\n"
            f"🌡️ *Temperatura Atual:* *{temperature_c:.2f} °C*\n"
            f"🎯 *Faixa Ideal Definida:* {min_temp_c:.1f} °C a {max_temp_c:.1f} °C\n\n"
            "Verifique o equipamento imediatamente para evitar perdas no estoque."
        )

    @staticmethod
    def format_lwt_offline_alert(device_name: str, device_id: str) -> str:
        """Formata o alerta de queda de energia/conexão detectado via LWT do MQTT."""
        return (
            "⚡ *ALERTA DE QUEDA DE CONEXÃO / ENERGIA (LWT)*\n\n"
            f"📍 *Equipamento:* {device_name} (`{device_id}`)\n"
            "🔴 *Status:* *OFFLINE (Desconexão Abrupta Detectada)*\n\n"
            "O microcontrolador ESP32 parou de responder ao Broker MQTT (Last Will and Testament). "
            "Pode haver queda de energia elétrica na câmara fria/freezer ou falha na rede local."
        )

    @staticmethod
    def format_recovery_alert(
        device_name: str,
        device_id: str,
        temperature_c: float,
        min_temp_c: float,
        max_temp_c: float,
    ) -> str:
        """Formata mensagem informando que a temperatura voltou para a faixa ideal."""
        return (
            "✅ *TEMPERATURA NORMALIZADA - COUTOS CONVENIÊNCIA*\n\n"
            f"📍 *Equipamento:* {device_name} (`{device_id}`)\n"
            f"🌡️ *Temperatura Atual:* *{temperature_c:.2f} °C*\n"
            f"🎯 *Faixa Ideal:* {min_temp_c:.1f} °C a {max_temp_c:.1f} °C\n\n"
            "O equipamento retornou à margem de operação segura."
        )


whatsapp_notifier = WhatsAppNotifier()
