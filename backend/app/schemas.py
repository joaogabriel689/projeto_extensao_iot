"""Schemas Pydantic para validação de entrada e serialização da API."""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeviceConfigResponse(BaseModel):
    device_id: str
    name: str
    min_temp_c: float
    max_temp_c: float
    alert_cooldown_seconds: int
    merchant_phone: Optional[str] = None
    notifications_enabled: bool
    online_status: str
    last_temp_c: Optional[float] = None
    last_seen_at: Optional[datetime] = None
    in_alert_state: bool
    last_alert_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ThresholdUpdateRequest(BaseModel):
    """Permite ao usuário/comerciante definir a faixa ideal de temperatura e parâmetros de alerta."""

    name: Optional[str] = Field(None, description="Nome amigável do freezer/câmara fria")
    min_temp_c: float = Field(..., description="Temperatura mínima ideal em °C (ex: -10.0)")
    max_temp_c: float = Field(..., description="Temperatura máxima ideal em °C (ex: 5.0)")
    alert_cooldown_seconds: Optional[int] = Field(
        None, ge=10, description="Intervalo mínimo em segundos entre alertas repetidos"
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
