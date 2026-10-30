"""Modelos relacionais do PostgreSQL (Dispositivos, Telemetria e Alertas)."""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text

from app.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeviceConfig(Base):
    """Configuração de cada freezer/câmara fria e sua faixa ideal de temperatura."""

    __tablename__ = "devices"

    device_id = Column(String(64), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    min_temp_c = Column(Float, nullable=False, default=-10.0)
    max_temp_c = Column(Float, nullable=False, default=5.0)
    alert_cooldown_seconds = Column(Integer, nullable=False, default=300)
    merchant_phone = Column(String(32), nullable=True)
    notifications_enabled = Column(Boolean, nullable=False, default=True)

    # Estado operacional atual (atualizado via MQTT / LWT)
    online_status = Column(String(16), nullable=False, default="unknown")
    last_temp_c = Column(Float, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    in_alert_state = Column(Boolean, nullable=False, default=False)
    last_alert_at = Column(DateTime(timezone=True), nullable=True)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class TelemetryLog(Base):
    """Registro histórico de leituras térmicas (com event_id único para replay idempotente)."""

    __tablename__ = "telemetry_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(String(128), unique=True, nullable=False, index=True)
    device_id = Column(String(64), nullable=False, index=True)
    temperature_c = Column(Float, nullable=False)
    min_temp_c = Column(Float, nullable=False)
    max_temp_c = Column(Float, nullable=False)
    is_out_of_range = Column(Boolean, nullable=False, default=False)
    replayed_from_buffer = Column(Boolean, nullable=False, default=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)


class AlertLog(Base):
    """Histórico de alertas disparados (desvio térmico e queda de energia LWT)."""

    __tablename__ = "alert_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    device_id = Column(String(64), nullable=False, index=True)
    alert_type = Column(String(64), nullable=False, index=True)
    severity = Column(String(32), nullable=False, default="CRITICAL")
    temperature_c = Column(Float, nullable=True)
    message = Column(Text, nullable=False)
    whatsapp_sent = Column(Boolean, nullable=False, default=False)
    whatsapp_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)
