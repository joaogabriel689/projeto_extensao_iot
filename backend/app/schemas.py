"""Schemas Pydantic para validação de entrada e serialização da API."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceConfigResponse(BaseModel):
    device_id: str
    name: str
    min_temp_c: float
    max_temp_c: float
    alert_delay_seconds: int = 180
    alert_cooldown_seconds: int
    telemetry_interval_seconds: int = 10
    replay_interval_seconds: int = 10
    merchant_phone: Optional[str] = None
    notifications_enabled: bool
    online_status: str
    last_temp_c: Optional[float] = None
    last_seen_at: Optional[datetime] = None
    out_of_range_since: Optional[datetime] = None
    out_of_range_elapsed_s: Optional[int] = None
    in_door_open_delay: bool = False
    in_alert_state: bool
    last_alert_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    wifi_ssid: Optional[str] = None
    wifi_ip: Optional[str] = None
    wifi_rssi: Optional[int] = None
    ap_active: Optional[bool] = None
    fw_version: Optional[str] = None
    ota_status: Optional[dict] = None

    model_config = ConfigDict(from_attributes=True)


class DeviceWifiConfigRequest(BaseModel):
    """Permite enviar nova configuração de Wi-Fi/Broker ou ativar o Portal AP no ESP32 via MQTT."""

    wifi_ssid: Optional[str] = Field(None, description="Novo SSID da rede Wi-Fi 2.4 GHz")
    wifi_pass: Optional[str] = Field(None, description="Nova senha do Wi-Fi")
    mqtt_host: Optional[str] = Field(None, description="IP ou domínio do Broker MQTT")
    mqtt_port: Optional[int] = Field(None, description="Porta do Broker MQTT (ex: 1884)")
    start_ap: bool = Field(False, description="Se True, instrui o ESP32 a abrir a rede AP Coutos-Freezer-Config")


class ThresholdUpdateRequest(BaseModel):
    """Permite ao usuário/comerciante definir a faixa ideal de temperatura, filtro de porta aberta e tempos."""

    name: Optional[str] = Field(None, description="Nome amigável do freezer/câmara fria")
    min_temp_c: float = Field(..., description="Temperatura mínima ideal em °C (ex: -10.0)")
    max_temp_c: float = Field(..., description="Temperatura máxima ideal em °C (ex: 5.0)")
    alert_delay_seconds: Optional[int] = Field(
        None,
        ge=0,
        le=7200,
        description="Filtro de porta aberta/reposição: segundos contínuos fora da faixa antes de disparar o alerta (0 = imediato)",
    )
    alert_cooldown_seconds: Optional[int] = Field(
        None, ge=10, description="Intervalo mínimo em segundos entre alertas repetidos"
    )
    telemetry_interval_seconds: Optional[int] = Field(
        None,
        ge=2,
        le=3600,
        description="Intervalo em segundos entre leituras enviadas pelo ESP32 (padrão: 10s)",
    )
    replay_interval_seconds: Optional[int] = Field(
        None,
        ge=2,
        le=3600,
        description="Intervalo em segundos de verificação/replay do buffer local para o PostgreSQL (padrão: 10s)",
    )
    merchant_phone: Optional[str] = Field(
        None, description="Número do WhatsApp do comerciante (ex: 5567999999999)"
    )
    notifications_enabled: Optional[bool] = Field(
        None, description="Habilita ou desabilita o envio de alertas via WhatsApp"
    )

    @model_validator(mode="after")
    def validate_temp_range(self):
        if self.min_temp_c >= self.max_temp_c:
            raise ValueError("min_temp_c deve ser estritamente menor que max_temp_c.")
        return self


class TelemetryLogResponse(BaseModel):
    id: int
    event_id: str
    device_id: str
    temperature_c: float
    min_temp_c: float
    max_temp_c: float
    is_out_of_range: bool
    replayed_from_buffer: bool
    recorded_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AlertLogResponse(BaseModel):
    id: int
    device_id: str
    alert_type: str
    severity: str
    temperature_c: Optional[float] = None
    message: str
    whatsapp_sent: bool
    whatsapp_error: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TestNotificationRequest(BaseModel):
    phone: Optional[str] = Field(
        None, description="Número de destino opcional (se omitido, usa MERCHANT_WHATSAPP_NUMBER)"
    )
    message: str = Field(
        default="✅ [Teste Sistema Coutos Conveniência] Integração de alertas via WhatsApp funcionando normalmente!",
        description="Mensagem de teste a ser enviada via Evolution API",
    )


class EvolutionSettingsUpdateRequest(BaseModel):
    evolution_api_url: str = Field(..., min_length=5, description="URL base da Evolution API (ex: http://evolution_api:8080)")
    evolution_api_key: str = Field(..., min_length=1, description="Global API Key da Evolution API")
    evolution_instance_name: str = Field(..., min_length=1, description="Nome da instância WhatsApp (ex: coutos_alertas)")
    merchant_whatsapp_number: Optional[str] = Field(None, description="Número de WhatsApp de destino (ex: 5511999999999)")
    whatsapp_notifications_enabled: bool = Field(True, description="Ativa ou desativa o envio de alertas")


class EvolutionSettingsResponse(BaseModel):
    evolution_api_url: str
    evolution_api_key: str
    evolution_instance_name: str
    merchant_whatsapp_number: Optional[str] = None
    whatsapp_notifications_enabled: bool
    instance_exists: bool = False
    instance_state: str = "unknown"
    instance_error: Optional[str] = None


class DashboardLoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=128)
