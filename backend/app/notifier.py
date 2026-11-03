"""Cliente de integração e gerenciamento da Evolution API (WhatsApp)."""

import logging
from typing import Any, Dict, Tuple
import httpx

from app.config import settings

logger = logging.getLogger(__name__)


class WhatsAppNotifier:
    """Envia notificações e gerencia a instância WhatsApp via Evolution API v2."""

    def __init__(self) -> None:
        self.base_url = settings.evolution_api_url.rstrip("/")
        self.api_key = settings.evolution_api_key
        self.instance = settings.evolution_instance_name
        self.default_phone = settings.merchant_whatsapp_number
        self.enabled = settings.whatsapp_notifications_enabled

    def update_config(
        self,
        evolution_api_url: str,
        evolution_api_key: str,
        evolution_instance_name: str,
        merchant_whatsapp_number: str | None,
        whatsapp_notifications_enabled: bool,
    ) -> None:
        """Atualiza as configurações da Evolution API em tempo de execução."""
        self.base_url = evolution_api_url.strip().rstrip("/")
        self.api_key = evolution_api_key.strip()
        self.instance = evolution_instance_name.strip()
        self.default_phone = (merchant_whatsapp_number or "").strip()
        self.enabled = bool(whatsapp_notifications_enabled)

    def _headers(self) -> Dict[str, str]:
        return {
            "apikey": self.api_key,
            "Content-Type": "application/json",
        }

    def get_instance_status(self) -> Dict[str, Any]:
        """Verifica na Evolution API se a instância existe e qual seu estado de conexão."""
        if not self.base_url or not self.api_key or not self.instance:
            return {
                "exists": False,
                "state": "unconfigured",
                "error": "URL, API Key ou Nome da Instância não configurados.",
            }

        url = f"{self.base_url}/instance/connectionState/{self.instance}"
        try:
            with httpx.Client(timeout=6.0) as client:
                resp = client.get(url, headers=self._headers())
                if resp.status_code == 200:
                    data = resp.json()
                    state = (
                        data.get("instance", {}).get("state")
                        or data.get("state")
                        or "unknown"
                    )
                    return {"exists": True, "state": state, "error": None}
                if resp.status_code == 404:
                    return {"exists": False, "state": "not_created", "error": None}
                return {
                    "exists": False,
                    "state": "error",
                    "error": f"HTTP {resp.status_code}: {resp.text[:200]}",
                }
        except Exception as exc:
            return {
                "exists": False,
                "state": "unreachable",
                "error": f"Falha ao conectar na Evolution API ({self.base_url}): {exc}",
            }

    def connect_or_create_instance(self) -> Dict[str, Any]:
        """Cria a instância (se não existir) e retorna o QR Code base64 para leitura."""
        status = self.get_instance_status()
        qrcode_b64 = None
        pairing_code = None

        try:
            with httpx.Client(timeout=12.0) as client:
                if not status["exists"] and status["state"] in ("not_created", "error"):
                    create_url = f"{self.base_url}/instance/create"
                    create_payload = {
                        "instanceName": self.instance,
                        "integration": "WHATSAPP-BAILEYS",
                        "qrcode": True,
                    }
                    c_resp = client.post(create_url, json=create_payload, headers=self._headers())
                    if 200 <= c_resp.status_code < 300:
                        c_data = c_resp.json()
                        qr_obj = c_data.get("qrcode") or {}
                        if isinstance(qr_obj, dict):
                            qrcode_b64 = qr_obj.get("base64")
                            pairing_code = qr_obj.get("pairingCode")
                    elif c_resp.status_code not in (403, 409):
                        return {
                            "ok": False,
                            "state": "error",
                            "qrcode_base64": None,
                            "error": f"Erro ao criar instância ({c_resp.status_code}): {c_resp.text[:250]}",
                        }

                # Consulta o estado ou busca novo QR Code se ainda não estiver 'open'
                conn_url = f"{self.base_url}/instance/connect/{self.instance}"
                conn_resp = client.get(conn_url, headers=self._headers())
                if conn_resp.status_code == 200:
                    conn_data = conn_resp.json()
                    qrcode_b64 = (
                        conn_data.get("base64")
                        or (conn_data.get("qrcode", {}) or {}).get("base64")
                        or qrcode_b64
                    )
                    pairing_code = conn_data.get("pairingCode") or pairing_code
                    state = (
                        (conn_data.get("instance") or {}).get("state")
                        or ("qrcode" if qrcode_b64 else "open")
                    )
                    return {
                        "ok": True,
                        "state": state,
                        "qrcode_base64": qrcode_b64,
                        "pairing_code": pairing_code,
                        "error": None,
                    }

                return {
                    "ok": False,
                    "state": "error",
                    "qrcode_base64": qrcode_b64,
                    "error": f"HTTP {conn_resp.status_code}: {conn_resp.text[:250]}",
                }
        except Exception as exc:
            return {
                "ok": False,
                "state": "unreachable",
                "qrcode_base64": None,
                "error": f"Erro ao comunicar com Evolution API: {exc}",
            }

    def logout_instance(self) -> Dict[str, Any]:
        """Desconecta/remove a sessão atual da instância na Evolution API."""
        try:
            with httpx.Client(timeout=8.0) as client:
                logout_url = f"{self.base_url}/instance/logout/{self.instance}"
                resp = client.delete(logout_url, headers=self._headers())
                return {
                    "ok": resp.status_code in (200, 201, 404),
                    "status_code": resp.status_code,
                    "detail": resp.text[:200],
                }
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}

    def send_message(self, phone_number: str | None, text: str) -> Tuple[bool, str | None]:
        """Envia mensagem de texto para o WhatsApp do comerciante."""
        if not self.enabled:
            logger.info("[WHATSAPP DESABILITADO] Mensagem não enviada: %s", text)
            return False, "Notificações WhatsApp desabilitadas na configuração."

        target_phone = (phone_number or self.default_phone or "").strip()
        clean_phone = "".join(ch for ch in target_phone if ch.isdigit())

        if not clean_phone:
            msg = "Número de WhatsApp do comerciante não configurado."
            logger.warning("[WHATSAPP] %s", msg)
            return False, msg

        if not self.api_key:
            msg = "EVOLUTION_API_KEY não configurada."
            logger.warning("[WHATSAPP] %s", msg)
            return False, msg

        url = f"{self.base_url}/message/sendText/{self.instance}"
        payload = {
            "number": clean_phone,
            "text": text,
        }

        try:
            with httpx.Client(timeout=10.0) as client:
                response = client.post(url, json=payload, headers=self._headers())
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
        duration_seconds: int = 0,
    ) -> str:
        """Formata a mensagem de alerta de temperatura fora da faixa ideal."""
        if alert_type == "TEMP_BELOW_MIN":
            status_desc = f"*ABAIXO DA FAIXA IDEAL* (Mínimo: {min_temp_c:.1f} °C)"
        else:
            status_desc = f"*ACIMA DA FAIXA IDEAL* (Máximo: {max_temp_c:.1f} °C)"

        duration_line = ""
        if duration_seconds > 0:
            mins, secs = divmod(int(duration_seconds), 60)
            dur_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"
            duration_line = f"⏱*Tempo contínuo fora da faixa:* {dur_str}\n"

        return (
            "*ALERTA TÉRMICO - COUTOS CONVENIÊNCIA*\n\n"
            f"*Equipamento:* {device_name} (`{device_id}`)\n"
            f"*Status:* {status_desc}\n"
            f"*Temperatura Atual:* *{temperature_c:.2f} °C*\n"
            f"{duration_line}\n"
            "Verifique o equipamento imediatamente para evitar perdas no estoque."
        )

    @staticmethod
    def format_lwt_offline_alert(device_name: str, device_id: str) -> str:
        """Formata o alerta de queda de energia/conexão detectado via LWT do MQTT."""
        return (
            "*ALERTA DE QUEDA DE CONEXÃO / ENERGIA (LWT)*\n\n"
            f"*Equipamento:* {device_name} (`{device_id}`)\n"
            "*Status:* *OFFLINE (Desconexão Abrupta Detectada)*\n\n"
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
            "*TEMPERATURA NORMALIZADA - COUTOS CONVENIÊNCIA*\n\n"
            f"*Equipamento:* {device_name} (`{device_id}`)\n"
            f"*Temperatura Atual:* *{temperature_c:.2f} °C*\n"
            f"*Faixa Ideal:* {min_temp_c:.1f} °C a {max_temp_c:.1f} °C\n\n"
            "O equipamento retornou à margem de operação segura."
        )


whatsapp_notifier = WhatsAppNotifier()
