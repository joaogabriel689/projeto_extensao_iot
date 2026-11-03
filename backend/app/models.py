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
    alert_delay_seconds = Column(Integer, nullable=False, default=180)
    alert_cooldown_seconds = Column(Integer, nullable=False, default=300)
    telemetry_interval_seconds = Column(Integer, nullable=False, default=10)
    merchant_phone = Column(String(32), nullable=True)
    notifications_enabled = Column(Boolean, nullable=False, default=True)

    # Modo Sem Alertas / Desligamento Noturno Programado (Janela de Silêncio)
    quiet_hours_enabled = Column(Boolean, nullable=False, default=False)
    quiet_hours_start = Column(String(8), nullable=False, default="23:00")
    quiet_hours_end = Column(String(8), nullable=False, default="07:00")

    # Estado operacional atual (atualizado via MQTT / LWT)
    online_status = Column(String(16), nullable=False, default="unknown")
    last_temp_c = Column(Float, nullable=True)
    last_seen_at = Column(DateTime(timezone=True), nullable=True)
    out_of_range_since = Column(DateTime(timezone=True), nullable=True)
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


class SystemSettings(Base):
    """Configurações globais da integração Evolution API (WhatsApp) editáveis pela Interface Web."""

    __tablename__ = "system_settings"

    id = Column(Integer, primary_key=True, default=1)
    evolution_api_url = Column(String(256), nullable=False)
    evolution_api_key = Column(String(256), nullable=False)
    evolution_instance_name = Column(String(128), nullable=False)
    merchant_whatsapp_number = Column(String(32), nullable=True)
    whatsapp_notifications_enabled = Column(Boolean, nullable=False, default=True)
    replay_interval_seconds = Column(Integer, nullable=False, default=10)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class User(Base):
    """Usuários do Painel Web armazenados no PostgreSQL com hash PBKDF2-HMAC-SHA256, RBAC e Lockout."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    full_name = Column(String(128), nullable=True)
    password_hash = Column(String(256), nullable=False)
    role = Column(String(32), nullable=False, default="admin")  # "admin" ou "operator"
    is_active = Column(Boolean, nullable=False, default=True)
    failed_login_attempts = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True), nullable=True)
    session_version = Column(Integer, nullable=False, default=1)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_login_ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at = Column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AuditLog(Base):
    """Trilha de auditoria de eventos de segurança, logins e alterações administrativas."""

    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, index=True)
    action = Column(String(64), nullable=False, index=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(256), nullable=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utc_now, index=True)

