"""Endpoints REST para configuração da faixa térmica, telemetria e alertas."""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.buffer import local_buffer
from app.config import settings
from app.database import check_postgres_health, get_db
from app.models import AlertLog, DeviceConfig, TelemetryLog
from app.mqtt_service import mqtt_service
from app.notifier import whatsapp_notifier
from app.schemas import (
    AlertLogResponse,
    DeviceConfigResponse,
    TelemetryLogResponse,
    TestNotificationRequest,
    ThresholdUpdateRequest,
)

router = APIRouter(prefix="/api/v1")


@router.get("/system/status", tags=["Sistema & Resiliência"])
def get_system_status():
    """Retorna o status operacional do Broker MQTT, PostgreSQL e Buffer Local."""
    pg_ok = check_postgres_health()
    return {
        "status": "healthy" if (pg_ok and mqtt_service.is_connected) else "degraded",
        "mqtt_connected": mqtt_service.is_connected,
        "postgres_connected": pg_ok,
        "local_buffer_pending_records": local_buffer.pending_count(),
        "evolution_instance": settings.evolution_instance_name,
        "whatsapp_notifications_enabled": settings.whatsapp_notifications_enabled,
    }


@router.post("/system/replay", tags=["Sistema & Resiliência"])
def trigger_manual_replay():
    """Força manualmente o reenvio idempotente (replay) do buffer local para o PostgreSQL."""
    replayed = local_buffer.replay_to_postgres(batch_size=500)
    return {
        "replayed_records": replayed,
        "remaining_in_buffer": local_buffer.pending_count(),
    }


@router.get("/devices", response_model=List[DeviceConfigResponse], tags=["Freezers / Câmaras Frias"])
def list_devices(db: Session = Depends(get_db)):
    """Lista todos os freezers/câmaras frias monitorados e suas faixas de temperatura."""
    return db.query(DeviceConfig).order_by(DeviceConfig.device_id).all()


@router.get(
    "/devices/{device_id}",
    response_model=DeviceConfigResponse,
    tags=["Freezers / Câmaras Frias"],
)
def get_device(device_id: str, db: Session = Depends(get_db)):
    """Consulta o status atual, última temperatura lida e faixa ideal de um equipamento."""
    device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Dispositivo '{device_id}' não encontrado.")
    return device


@router.patch(
    "/devices/{device_id}/thresholds",
    response_model=DeviceConfigResponse,
    tags=["Freezers / Câmaras Frias"],
)
def update_device_thresholds(
    device_id: str,
    payload: ThresholdUpdateRequest,
    db: Session = Depends(get_db),
):
    """Atualiza a faixa ideal de temperatura (min/max) definível pelo usuário e sincroniza via MQTT."""
    device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
    if not device:
        device = DeviceConfig(
            device_id=device_id,
            name=payload.name or f"Freezer ({device_id})",
            min_temp_c=payload.min_temp_c,
            max_temp_c=payload.max_temp_c,
            alert_cooldown_seconds=payload.alert_cooldown_seconds or settings.alert_cooldown_seconds,
            merchant_phone=payload.merchant_phone or settings.merchant_whatsapp_number,
            notifications_enabled=(
                payload.notifications_enabled
                if payload.notifications_enabled is not None
                else settings.whatsapp_notifications_enabled
            ),
        )
        db.add(device)
    else:
        device.min_temp_c = payload.min_temp_c
        device.max_temp_c = payload.max_temp_c
        if payload.name is not None:
            device.name = payload.name
        if payload.alert_cooldown_seconds is not None:
            device.alert_cooldown_seconds = payload.alert_cooldown_seconds
        if payload.merchant_phone is not None:
            device.merchant_phone = payload.merchant_phone
        if payload.notifications_enabled is not None:
            device.notifications_enabled = payload.notifications_enabled

    db.commit()
    db.refresh(device)

    # Atualiza cache SQLite local para operação offline-first
    local_buffer.cache_device_config(
        {
            "device_id": device.device_id,
            "name": device.name,
            "min_temp_c": device.min_temp_c,
            "max_temp_c": device.max_temp_c,
            "alert_cooldown_seconds": device.alert_cooldown_seconds,
            "merchant_phone": device.merchant_phone,
            "notifications_enabled": device.notifications_enabled,
        }
    )

    # Sincroniza a nova faixa ideal com o próprio ESP32 via MQTT (retain=True)
    mqtt_service.publish_threshold_config(device_id, device.min_temp_c, device.max_temp_c)

    return device


@router.get("/telemetry", response_model=List[TelemetryLogResponse], tags=["Telemetria & Alertas"])
def list_telemetry(
    device_id: Optional[str] = Query(None, description="Filtrar por ID do equipamento"),
    out_of_range_only: bool = Query(False, description="Retornar apenas leituras fora da faixa ideal"),
    limit: int = Query(50, ge=1, le=500, description="Quantidade máxima de registros"),
    db: Session = Depends(get_db),
):
    """Consulta o histórico de leituras de temperatura armazenadas no PostgreSQL."""
    query = db.query(TelemetryLog)
    if device_id:
        query = query.filter(TelemetryLog.device_id == device_id)
    if out_of_range_only:
        query = query.filter(TelemetryLog.is_out_of_range.is_(True))
    return query.order_by(TelemetryLog.recorded_at.desc()).limit(limit).all()


@router.get("/alerts", response_model=List[AlertLogResponse], tags=["Telemetria & Alertas"])
def list_alerts(
    device_id: Optional[str] = Query(None, description="Filtrar por ID do equipamento"),
    limit: int = Query(50, ge=1, le=500, description="Quantidade máxima de alertas"),
    db: Session = Depends(get_db),
):
    """Consulta o histórico de alertas disparados (desvio térmico e LWT)."""
    query = db.query(AlertLog)
    if device_id:
        query = query.filter(AlertLog.device_id == device_id)
    return query.order_by(AlertLog.created_at.desc()).limit(limit).all()


@router.post("/notifications/test", tags=["Notificações WhatsApp"])
def send_test_whatsapp_notification(payload: TestNotificationRequest):
    """Dispara uma mensagem de teste para o WhatsApp do comerciante via Evolution API."""
    sent, err = whatsapp_notifier.send_message(payload.phone, payload.message)
    return {
        "whatsapp_sent": sent,
        "target_phone": payload.phone or settings.merchant_whatsapp_number,
        "evolution_instance": settings.evolution_instance_name,
        "error": err,
    }
