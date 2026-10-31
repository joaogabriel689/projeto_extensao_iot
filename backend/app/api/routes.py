"""Endpoints REST para configuração da faixa térmica, telemetria, alertas e Evolution API."""

from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session_token,
    is_request_authenticated,
    verify_dashboard_credentials,
)
from app.buffer import local_buffer
from app.config import settings
from app.database import check_postgres_health, get_db
from app.models import AlertLog, DeviceConfig, SystemSettings, TelemetryLog
from app.mqtt_service import mqtt_service
from app.notifier import whatsapp_notifier
from app.ota_manager import ota_manager
from app.schemas import (
    AlertLogResponse,
    DashboardLoginRequest,
    DeviceConfigResponse,
    DeviceWifiConfigRequest,
    EvolutionSettingsResponse,
    EvolutionSettingsUpdateRequest,
    TelemetryLogResponse,
    TestNotificationRequest,
    ThresholdUpdateRequest,
)

router = APIRouter(prefix="/api/v1")


@router.post("/auth/login", tags=["Autenticação do Painel Web"])
def dashboard_login(payload: DashboardLoginRequest, response: Response):
    """Autentica o operador no Painel Web e emite Cookie HttpOnly assinado (HMAC-SHA256)."""
    if not verify_dashboard_credentials(payload.username, payload.password):
        raise HTTPException(status_code=401, detail="Usuário ou senha incorretos.")

    token = create_session_token(payload.username.strip())
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return {"ok": True, "username": payload.username.strip()}


@router.post("/auth/logout", tags=["Autenticação do Painel Web"])
def dashboard_logout(response: Response):
    """Encerra a sessão autenticada do Painel Web."""
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/check", tags=["Autenticação do Painel Web"])
def dashboard_auth_check(request: Request):
    """Verifica se o navegador já possui uma sessão autenticada válida."""
    authed, username = is_request_authenticated(request)
    return {"authenticated": authed, "username": username}


def _get_or_init_system_settings(db: Session) -> SystemSettings:
    """Garante que o registro singleton de configurações da Evolution API exista no banco."""
    sys_cfg = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
    if not sys_cfg:
        sys_cfg = SystemSettings(
            id=1,
            evolution_api_url=whatsapp_notifier.base_url,
            evolution_api_key=whatsapp_notifier.api_key,
            evolution_instance_name=whatsapp_notifier.instance,
            merchant_whatsapp_number=whatsapp_notifier.default_phone,
            whatsapp_notifications_enabled=whatsapp_notifier.enabled,
        )
        db.add(sys_cfg)
        db.commit()
        db.refresh(sys_cfg)
    return sys_cfg


@router.get("/system/status", tags=["Sistema & Resiliência"])
def get_system_status():
    """Retorna o status operacional do Broker MQTT, PostgreSQL e Buffer Local."""
    pg_ok = check_postgres_health()
    return {
        "status": "healthy" if (pg_ok and mqtt_service.is_connected) else "degraded",
        "mqtt_connected": mqtt_service.is_connected,
        "postgres_connected": pg_ok,
        "local_buffer_pending_records": local_buffer.pending_count(),
        "replay_interval_seconds": settings.replay_interval_seconds,
        "evolution_instance": whatsapp_notifier.instance,
        "whatsapp_notifications_enabled": whatsapp_notifier.enabled,
    }


@router.post("/system/replay", tags=["Sistema & Resiliência"])
def trigger_manual_replay():
    """Força manualmente o reenvio idempotente (replay) do buffer local para o PostgreSQL."""
    replayed = local_buffer.replay_to_postgres(batch_size=500)
    return {
        "replayed_records": replayed,
        "remaining_in_buffer": local_buffer.pending_count(),
    }


@router.get(
    "/settings/evolution",
    response_model=EvolutionSettingsResponse,
    tags=["Configuração Evolution API (WhatsApp)"],
)
def get_evolution_settings(db: Session = Depends(get_db)):
    """Retorna as configurações atuais da Evolution API e o estado da instância WhatsApp."""
    sys_cfg = _get_or_init_system_settings(db)
    whatsapp_notifier.update_config(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
    )
    inst_status = whatsapp_notifier.get_instance_status()
    return EvolutionSettingsResponse(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
        instance_exists=inst_status["exists"],
        instance_state=inst_status["state"],
        instance_error=inst_status["error"],
    )


@router.put(
    "/settings/evolution",
    response_model=EvolutionSettingsResponse,
    tags=["Configuração Evolution API (WhatsApp)"],
)
def update_evolution_settings(
    payload: EvolutionSettingsUpdateRequest,
    db: Session = Depends(get_db),
):
    """Atualiza as configurações da Evolution API e sincroniza o telefone com os dispositivos."""
    sys_cfg = _get_or_init_system_settings(db)
    sys_cfg.evolution_api_url = payload.evolution_api_url.strip().rstrip("/")
    sys_cfg.evolution_api_key = payload.evolution_api_key.strip()
    sys_cfg.evolution_instance_name = payload.evolution_instance_name.strip()
    sys_cfg.merchant_whatsapp_number = (payload.merchant_whatsapp_number or "").strip()
    sys_cfg.whatsapp_notifications_enabled = payload.whatsapp_notifications_enabled

    # Sincroniza telefone e flag nos dispositivos monitorados
    devices = db.query(DeviceConfig).all()
    for dev in devices:
        dev.merchant_phone = sys_cfg.merchant_whatsapp_number
        dev.notifications_enabled = sys_cfg.whatsapp_notifications_enabled

    db.commit()
    db.refresh(sys_cfg)

    whatsapp_notifier.update_config(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
    )

    inst_status = whatsapp_notifier.get_instance_status()
    return EvolutionSettingsResponse(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
        instance_exists=inst_status["exists"],
        instance_state=inst_status["state"],
        instance_error=inst_status["error"],
    )


@router.post(
    "/settings/evolution/connect",
    tags=["Configuração Evolution API (WhatsApp)"],
)
def connect_evolution_instance(db: Session = Depends(get_db)):
    """Cria a instância (se não existir) e retorna o QR Code Base64 para leitura na interface web."""
    sys_cfg = _get_or_init_system_settings(db)
    whatsapp_notifier.update_config(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
    )
    return whatsapp_notifier.connect_or_create_instance()


@router.delete(
    "/settings/evolution/logout",
    tags=["Configuração Evolution API (WhatsApp)"],
)
def logout_evolution_instance():
    """Desconecta a sessão do WhatsApp da instância ativa na Evolution API."""
    return whatsapp_notifier.logout_instance()


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
    """Consulta o status atual, última temperatura lida, Wi-Fi do ESP32 e faixa ideal de um equipamento."""
    device = db.query(DeviceConfig).filter(DeviceConfig.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail=f"Dispositivo '{device_id}' não encontrado.")
    resp = DeviceConfigResponse.model_validate(device)
    sys_cfg = _get_or_init_system_settings(db)
    resp.replay_interval_seconds = int(
        sys_cfg.replay_interval_seconds or settings.replay_interval_seconds
    )
    runtime_info = mqtt_service.get_device_runtime_info(device_id)
    resp.wifi_ssid = runtime_info.get("wifi_ssid")
    resp.wifi_ip = runtime_info.get("wifi_ip")
    resp.wifi_rssi = runtime_info.get("wifi_rssi")
    resp.ap_active = runtime_info.get("ap_active")
    resp.fw_version = runtime_info.get("fw_version")
    resp.out_of_range_since = runtime_info.get("out_of_range_since") or device.out_of_range_since
    resp.out_of_range_elapsed_s = int(runtime_info.get("out_of_range_elapsed_s") or 0)
    resp.in_door_open_delay = bool(runtime_info.get("in_door_open_delay", False))
    resp.ota_status = ota_manager.get_device_ota_status(device_id)
    return resp


@router.post(
    "/devices/{device_id}/wifi",
    tags=["Freezers / Câmaras Frias"],
)
def configure_device_wifi(device_id: str, payload: DeviceWifiConfigRequest):
    """Envia comando via MQTT para o ESP32 abrir o Portal AP (Coutos-Freezer-Config) ou trocar de Wi-Fi."""
    cmd: dict = {}
    if payload.start_ap:
        cmd["start_ap"] = True
    if payload.wifi_ssid:
        cmd["wifi_ssid"] = payload.wifi_ssid.strip()
        cmd["wifi_pass"] = (payload.wifi_pass or "").strip()
    if payload.mqtt_host:
        cmd["mqtt_host"] = payload.mqtt_host.strip()
    if payload.mqtt_port:
        cmd["mqtt_port"] = payload.mqtt_port

    if not cmd:
        raise HTTPException(status_code=400, detail="Informe start_ap=True ou o novo wifi_ssid.")

    published = mqtt_service.publish_wifi_command(device_id, cmd)
    return {
        "ok": published,
        "device_id": device_id,
        "command_sent": cmd,
    }


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
            alert_delay_seconds=(
                payload.alert_delay_seconds
                if payload.alert_delay_seconds is not None
                else settings.alert_delay_seconds
            ),
            alert_cooldown_seconds=payload.alert_cooldown_seconds or settings.alert_cooldown_seconds,
            telemetry_interval_seconds=(
                payload.telemetry_interval_seconds
                if payload.telemetry_interval_seconds is not None
                else settings.default_telemetry_interval_seconds
            ),
            merchant_phone=payload.merchant_phone or whatsapp_notifier.default_phone,
            notifications_enabled=(
                payload.notifications_enabled
                if payload.notifications_enabled is not None
                else whatsapp_notifier.enabled
            ),
        )
        db.add(device)
    else:
        device.min_temp_c = payload.min_temp_c
        device.max_temp_c = payload.max_temp_c
        if payload.name is not None:
            device.name = payload.name
        if payload.alert_delay_seconds is not None:
            device.alert_delay_seconds = payload.alert_delay_seconds
        if payload.alert_cooldown_seconds is not None:
            device.alert_cooldown_seconds = payload.alert_cooldown_seconds
        if payload.telemetry_interval_seconds is not None:
            device.telemetry_interval_seconds = payload.telemetry_interval_seconds
        if payload.merchant_phone is not None:
            device.merchant_phone = payload.merchant_phone
        if payload.notifications_enabled is not None:
            device.notifications_enabled = payload.notifications_enabled

    sys_cfg = _get_or_init_system_settings(db)
    if payload.replay_interval_seconds is not None:
        sys_cfg.replay_interval_seconds = payload.replay_interval_seconds
        settings.replay_interval_seconds = payload.replay_interval_seconds

    db.commit()
    db.refresh(device)

    local_buffer.cache_device_config(
        {
            "device_id": device.device_id,
            "name": device.name,
            "min_temp_c": device.min_temp_c,
            "max_temp_c": device.max_temp_c,
            "alert_delay_seconds": device.alert_delay_seconds,
            "alert_cooldown_seconds": device.alert_cooldown_seconds,
            "telemetry_interval_seconds": device.telemetry_interval_seconds,
            "merchant_phone": device.merchant_phone,
            "notifications_enabled": device.notifications_enabled,
        }
    )

    mqtt_service.publish_threshold_config(
        device_id,
        device.min_temp_c,
        device.max_temp_c,
        device.telemetry_interval_seconds,
    )

    resp = DeviceConfigResponse.model_validate(device)
    resp.replay_interval_seconds = int(
        sys_cfg.replay_interval_seconds or settings.replay_interval_seconds
    )
    runtime_info = mqtt_service.get_device_runtime_info(device_id)
    resp.wifi_ssid = runtime_info.get("wifi_ssid")
    resp.wifi_ip = runtime_info.get("wifi_ip")
    resp.wifi_rssi = runtime_info.get("wifi_rssi")
    resp.ap_active = runtime_info.get("ap_active")
    resp.fw_version = runtime_info.get("fw_version")
    resp.out_of_range_since = runtime_info.get("out_of_range_since") or device.out_of_range_since
    resp.out_of_range_elapsed_s = int(runtime_info.get("out_of_range_elapsed_s") or 0)
    resp.in_door_open_delay = bool(runtime_info.get("in_door_open_delay", False))
    resp.ota_status = ota_manager.get_device_ota_status(device_id)
    return resp


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
        "target_phone": payload.phone or whatsapp_notifier.default_phone,
        "evolution_instance": whatsapp_notifier.instance,
        "error": err,
    }


# ------------------------------------------------------------------------------
# ATUALIZAÇÃO REMOTA DE FIRMWARE (OTA SEGURO — HMAC-SHA256 + ONE-TIME TOKEN)
# ------------------------------------------------------------------------------
@router.get("/ota/info", tags=["OTA Remoto Seguro (ESP32)"])
def get_ota_firmware_info(device_id: str = Query(default="camara01")):
    """Retorna metadados do último firmware armazenado e o progresso OTA do dispositivo."""
    return {
        "stored_firmware": ota_manager.get_stored_firmware_meta(),
        "device_ota_status": ota_manager.get_device_ota_status(device_id),
    }


@router.post("/devices/{device_id}/ota/deploy", tags=["OTA Remoto Seguro (ESP32)"])
async def deploy_ota_firmware(
    device_id: str,
    request: Request,
    version: str = Query("1.1.0", min_length=1, max_length=32, description="Versão semântica do firmware"),
    filename: str = Query("esp32_freezer_monitor.ino.bin", description="Nome original do binário"),
    x_ota_secret: Optional[str] = Header(None, alias="X-OTA-Secret"),
):
    """Recebe (opcionalmente) o binário .bin, valida cabeçalho 0xE9 e SHA-256, e publica ordem OTA assinada."""
    if not ota_manager.verify_admin_secret(x_ota_secret):
        raise HTTPException(
            status_code=401,
            detail="Não autorizado: chave administrativa X-OTA-Secret ausente ou inválida.",
        )

    body = await request.body()
    try:
        if len(body) > 0:
            meta = ota_manager.validate_and_store_firmware(body, version=version, filename=filename)
        else:
            meta = ota_manager.get_stored_firmware_meta()
            if not meta:
                raise HTTPException(
                    status_code=400,
                    detail="Envie o arquivo .bin no corpo da requisição para armazenar e disparar o OTA.",
                )

        signed_trigger = ota_manager.create_signed_ota_trigger(
            device_id=device_id,
            version_override=version if len(body) > 0 else meta["version"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    published = mqtt_service.publish_ota_trigger(device_id, signed_trigger)
    if not published:
        raise HTTPException(
            status_code=503,
            detail="Firmware validado, mas o Broker MQTT está desconectado no momento.",
        )

    return {
        "ok": True,
        "device_id": device_id,
        "version": signed_trigger["version"],
        "size_bytes": signed_trigger["size"],
        "sha256": signed_trigger["sha256"],
        "signature_hmac_sha256": signed_trigger["sig"],
        "token_ttl_seconds": 180,
        "message": (
            f"Ordem OTA v{signed_trigger['version']} assinada (HMAC-SHA256) enviada ao ESP32 ({device_id}) via MQTT!"
        ),
    }


@router.get("/ota/download/{token}", include_in_schema=False)
def download_ota_firmware_with_one_time_token(
    token: str,
    x_device_id: Optional[str] = Header(None, alias="X-Device-ID"),
    x_ota_auth: Optional[str] = Header(None, alias="X-OTA-Auth"),
):
    """Endpoint de uso único (One-Time Token) exclusivo para o ESP32 baixar o binário autenticado."""
    try:
        binary_data, info = ota_manager.consume_download_token(
            token=token,
            header_device_id=x_device_id,
            header_ota_auth=x_ota_auth,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return Response(
        content=binary_data,
        media_type="application/octet-stream",
        headers={
            "Content-Length": str(len(binary_data)),
            "X-Firmware-SHA256": info["sha256"],
            "X-Firmware-Version": info["version"],
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )
