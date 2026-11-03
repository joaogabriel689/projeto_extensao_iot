"""Endpoints REST para configuração da faixa térmica, telemetria, alertas, usuários, auditoria e Evolution API."""

from typing import List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.auth import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    create_session_token,
    get_client_ip,
    get_request_auth_context,
    hash_password,
    invalidate_user_session_cache,
    login_ip_limiter,
    record_audit_log,
    validate_password_strength,
    verify_dashboard_credentials,
    verify_password,
)
from app.buffer import local_buffer
from app.config import settings
from app.database import check_postgres_health, get_db
from app.models import AlertLog, AuditLog, DeviceConfig, SystemSettings, TelemetryLog, User
from app.mqtt_service import mqtt_service
from app.notifier import whatsapp_notifier
from app.ota_manager import MAX_FIRMWARE_BYTES, ota_manager
from app.schemas import (
    AlertLogResponse,
    AuditLogResponse,
    ChangeOwnPasswordRequest,
    DashboardLoginRequest,
    DeviceConfigResponse,
    DeviceWifiConfigRequest,
    EvolutionSettingsResponse,
    EvolutionSettingsUpdateRequest,
    TelemetryLogResponse,
    TestNotificationRequest,
    ThresholdUpdateRequest,
    UserCreateRequest,
    UserResponse,
    UserUpdateRequest,
)

router = APIRouter(prefix="/api/v1")

MASKED_SECRET_PLACEHOLDER = "••••••••"


def _is_secure_request(request: Request) -> bool:
    """Determina se o cookie deve receber a flag Secure (HTTPS ou configurado no .env)."""
    if settings.cookie_secure:
        return True
    proto = (request.headers.get("X-Forwarded-Proto") or request.url.scheme or "").lower()
    return proto == "https"


def _set_auth_cookies(
    response: Response,
    request: Request,
    session_token: str,
    csrf_token: str,
) -> None:
    """Aplica cookies de sessão (HttpOnly) e CSRF (SameSite=Strict) com flags seguras."""
    secure_flag = _is_secure_request(request)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=secure_flag,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=csrf_token,
        max_age=SESSION_TTL_SECONDS,
        httponly=False,
        secure=secure_flag,
        samesite="strict",
        path="/",
    )


def _require_admin(request: Request) -> dict:
    """Exige que o usuário autenticado possua perfil 'admin'."""
    ctx = getattr(request.state, "auth", None) or get_request_auth_context(request)
    if not ctx.get("authenticated"):
        raise HTTPException(status_code=401, detail="Não autenticado.")
    if ctx.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Acesso negado: esta operação exige privilégios de Administrador.",
        )
    return ctx


@router.post("/auth/login", tags=["Autenticação & Usuários"])
def dashboard_login(
    payload: DashboardLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Autentica o usuário no banco PostgreSQL com proteção Brute-Force/Lockout e emite Cookie HttpOnly + CSRF."""
    client_ip = get_client_ip(request)
    allowed, retry_after = login_ip_limiter.is_allowed(client_ip)
    if not allowed:
        record_audit_log(
            db,
            username=payload.username.strip(),
            action="AUTH_RATE_LIMIT",
            details=f"Limite de tentativas por IP excedido (aguardar {retry_after}s)",
            request=request,
        )
        raise HTTPException(
            status_code=429,
            detail=f"Muitas tentativas de login a partir deste IP. Aguarde {retry_after} segundos.",
        )

    ok, user_obj, err_msg = verify_dashboard_credentials(
        payload.username,
        payload.password,
        db=db,
        client_ip=client_ip,
    )
    if not ok or not user_obj:
        action = "AUTH_LOCKOUT" if (err_msg and "bloqueada" in err_msg.lower()) else "AUTH_LOGIN_FAIL"
        record_audit_log(
            db,
            username=payload.username.strip(),
            action=action,
            details=err_msg or "Credenciais inválidas",
            request=request,
        )
        raise HTTPException(status_code=401, detail=err_msg or "Usuário ou senha incorretos.")

    login_ip_limiter.reset(client_ip)
    session_token, csrf_token = create_session_token(
        username=user_obj.username,
        role=user_obj.role,
        session_version=int(user_obj.session_version or 1),
    )
    _set_auth_cookies(response, request, session_token, csrf_token)

    record_audit_log(
        db,
        username=user_obj.username,
        action="AUTH_LOGIN_OK",
        details=f"Login realizado com sucesso (perfil: {user_obj.role})",
        request=request,
    )
    return {
        "ok": True,
        "username": user_obj.username,
        "full_name": user_obj.full_name,
        "role": user_obj.role,
        "csrf_token": csrf_token,
    }


@router.post("/auth/logout", tags=["Autenticação & Usuários"])
def dashboard_logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Encerra a sessão autenticada do Painel Web e remove os cookies."""
    ctx = get_request_auth_context(request)
    if ctx.get("authenticated") and ctx.get("username"):
        record_audit_log(
            db,
            username=ctx["username"],
            action="AUTH_LOGOUT",
            details="Sessão encerrada pelo usuário",
            request=request,
        )
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(key=CSRF_COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/auth/check", tags=["Autenticação & Usuários"])
def dashboard_auth_check(request: Request):
    """Verifica se o navegador possui sessão autenticada válida e retorna perfil e token CSRF."""
    ctx = get_request_auth_context(request)
    return {
        "authenticated": ctx["authenticated"],
        "username": ctx["username"],
        "role": ctx["role"],
        "csrf_token": ctx["csrf_token"],
    }


@router.post("/auth/change-password", tags=["Autenticação & Usuários"])
def change_own_password(
    payload: ChangeOwnPasswordRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    """Permite ao usuário autenticado alterar a própria senha e invalida sessões anteriores."""
    ctx = getattr(request.state, "auth", None) or get_request_auth_context(request)
    username = ctx.get("username")
    if not username or username == "api_key_client":
        raise HTTPException(status_code=400, detail="Operação disponível apenas para usuários de sessão.")

    strength_err = validate_password_strength(payload.new_password)
    if strength_err:
        raise HTTPException(status_code=400, detail=strength_err)

    user = db.query(User).filter(User.username == username).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado no banco de dados.")

    if not verify_password(payload.current_password, user.password_hash):
        record_audit_log(
            db,
            username=username,
            action="USER_PASSWORD_CHANGE_FAIL",
            details="Senha atual incorreta ao tentar alterar a própria senha",
            request=request,
        )
        raise HTTPException(status_code=401, detail="A senha atual informada está incorreta.")

    user.password_hash = hash_password(payload.new_password)
    user.session_version = int(user.session_version or 1) + 1
    db.commit()
    db.refresh(user)
    invalidate_user_session_cache(user.username)

    # Emite novo cookie de sessão atualizado para manter o navegador atual logado
    session_token, csrf_token = create_session_token(
        username=user.username,
        role=user.role,
        session_version=user.session_version,
    )
    _set_auth_cookies(response, request, session_token, csrf_token)

    record_audit_log(
        db,
        username=username,
        action="USER_PASSWORD_CHANGE",
        details="Senha alterada com sucesso e sessões anteriores revogadas",
        request=request,
    )
    return {
        "ok": True,
        "message": "Senha atualizada com sucesso! Todas as outras sessões foram encerradas.",
        "csrf_token": csrf_token,
    }


# ------------------------------------------------------------------------------
# GERENCIAMENTO DE USUÁRIOS E TRILHA DE AUDITORIA (EXCLUSIVO ADMIN)
# ------------------------------------------------------------------------------
@router.get("/users", response_model=List[UserResponse], tags=["Autenticação & Usuários"])
def list_users(request: Request, db: Session = Depends(get_db)):
    """Lista todos os usuários cadastrados no PostgreSQL (Exclusivo Admin)."""
    _require_admin(request)
    return db.query(User).order_by(User.id.asc()).all()


@router.post("/users", response_model=UserResponse, status_code=201, tags=["Autenticação & Usuários"])
def create_user(
    payload: UserCreateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Cria um novo usuário no banco de dados com senha hash PBKDF2-HMAC-SHA256 (Exclusivo Admin)."""
    ctx = _require_admin(request)
    clean_username = payload.username.strip()

    strength_err = validate_password_strength(payload.password)
    if strength_err:
        raise HTTPException(status_code=400, detail=strength_err)

    existing = db.query(User).filter(User.username == clean_username).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"O usuário '{clean_username}' já está cadastrado.")

    new_user = User(
        username=clean_username,
        full_name=(payload.full_name or "").strip() or None,
        password_hash=hash_password(payload.password),
        role=payload.role,
        is_active=payload.is_active,
        session_version=1,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    invalidate_user_session_cache(new_user.username)

    record_audit_log(
        db,
        username=ctx["username"],
        action="USER_CREATE",
        details=f"Criou usuário '{new_user.username}' (perfil={new_user.role}, ativo={new_user.is_active})",
        request=request,
    )
    return new_user


@router.patch("/users/{user_id}", response_model=UserResponse, tags=["Autenticação & Usuários"])
def update_user(
    user_id: int,
    payload: UserUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Atualiza perfil, status ativo, desbloqueia conta, redefine senha ou revoga sessões (Exclusivo Admin)."""
    ctx = _require_admin(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    # Proteção contra remoção ou desativação do último administrador ativo
    if user.role == "admin" and (payload.role == "operator" or payload.is_active is False):
        active_admins = (
            db.query(User)
            .filter(User.role == "admin", User.is_active.is_(True), User.id != user.id)
            .count()
        )
        if active_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Operação bloqueada: o sistema deve possuir pelo menos um Administrador ativo.",
            )

    changes = []
    if payload.full_name is not None:
        user.full_name = payload.full_name.strip() or None
        changes.append("nome")

    if payload.role is not None and payload.role != user.role:
        user.role = payload.role
        user.session_version = int(user.session_version or 1) + 1
        changes.append(f"perfil->{payload.role}")

    if payload.is_active is not None and payload.is_active != user.is_active:
        if user.username == ctx.get("username") and not payload.is_active:
            raise HTTPException(status_code=400, detail="Você não pode desativar sua própria conta.")
        user.is_active = payload.is_active
        user.session_version = int(user.session_version or 1) + 1
        changes.append(f"ativo->{payload.is_active}")

    if payload.new_password:
        strength_err = validate_password_strength(payload.new_password)
        if strength_err:
            raise HTTPException(status_code=400, detail=strength_err)
        user.password_hash = hash_password(payload.new_password)
        user.failed_login_attempts = 0
        user.locked_until = None
        user.session_version = int(user.session_version or 1) + 1
        changes.append("senha_redefinida")

    if payload.unlock_account:
        user.failed_login_attempts = 0
        user.locked_until = None
        changes.append("conta_desbloqueada")

    if payload.revoke_sessions:
        user.session_version = int(user.session_version or 1) + 1
        changes.append("sessoes_revogadas")

    db.commit()
    db.refresh(user)
    invalidate_user_session_cache(user.username)

    record_audit_log(
        db,
        username=ctx["username"],
        action="USER_UPDATE",
        details=f"Atualizou '{user.username}': {', '.join(changes) or 'sem alterações'}",
        request=request,
    )
    return user


@router.delete("/users/{user_id}", tags=["Autenticação & Usuários"])
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db)):
    """Remove um usuário do banco de dados, protegendo contra auto-exclusão ou remoção do último admin."""
    ctx = _require_admin(request)
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuário não encontrado.")

    if user.username == ctx.get("username"):
        raise HTTPException(status_code=400, detail="Você não pode excluir sua própria conta ativa.")

    if user.role == "admin" and user.is_active:
        other_admins = (
            db.query(User)
            .filter(User.role == "admin", User.is_active.is_(True), User.id != user.id)
            .count()
        )
        if other_admins == 0:
            raise HTTPException(
                status_code=400,
                detail="Não é possível excluir o único Administrador ativo do sistema.",
            )

    target_username = user.username
    db.delete(user)
    db.commit()
    invalidate_user_session_cache(target_username)

    record_audit_log(
        db,
        username=ctx["username"],
        action="USER_DELETE",
        details=f"Excluiu usuário '{target_username}'",
        request=request,
    )
    return {"ok": True, "deleted_username": target_username}


@router.get("/audit-logs", response_model=List[AuditLogResponse], tags=["Autenticação & Usuários"])
def list_audit_logs(
    request: Request,
    username: Optional[str] = Query(None, description="Filtrar por nome de usuário"),
    action: Optional[str] = Query(None, description="Filtrar por tipo de evento"),
    limit: int = Query(100, ge=1, le=500, description="Quantidade máxima de registros"),
    db: Session = Depends(get_db),
):
    """Consulta a trilha de auditoria de segurança e administração (Exclusivo Admin)."""
    _require_admin(request)
    query = db.query(AuditLog)
    if username:
        query = query.filter(AuditLog.username == username.strip())
    if action:
        query = query.filter(AuditLog.action == action.strip())
    return query.order_by(AuditLog.created_at.desc()).limit(limit).all()


# ------------------------------------------------------------------------------
# SISTEMA, RESILIÊNCIA E EVOLUTION API (WHATSAPP)
# ------------------------------------------------------------------------------
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
    """Retorna as configurações atuais da Evolution API (com API Key mascarada) e o estado da instância WhatsApp."""
    sys_cfg = _get_or_init_system_settings(db)
    whatsapp_notifier.update_config(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=sys_cfg.evolution_api_key,
        evolution_instance_name=sys_cfg.evolution_instance_name,
        merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
        whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
    )
    inst_status = whatsapp_notifier.get_instance_status()
    masked_key = MASKED_SECRET_PLACEHOLDER if sys_cfg.evolution_api_key else ""
    return EvolutionSettingsResponse(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=masked_key,
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
    request: Request,
    db: Session = Depends(get_db),
):
    """Atualiza as configurações da Evolution API (Exclusivo Admin) e sincroniza o telefone com os dispositivos."""
    ctx = _require_admin(request)
    sys_cfg = _get_or_init_system_settings(db)
    sys_cfg.evolution_api_url = payload.evolution_api_url.strip().rstrip("/")
    if (
        payload.evolution_api_key
        and payload.evolution_api_key.strip()
        and payload.evolution_api_key.strip() != MASKED_SECRET_PLACEHOLDER
    ):
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

    record_audit_log(
        db,
        username=ctx["username"],
        action="EVOLUTION_CONFIG_UPDATE",
        details=f"Atualizou Evolution API (instância={sys_cfg.evolution_instance_name}, destino={sys_cfg.merchant_whatsapp_number})",
        request=request,
    )

    inst_status = whatsapp_notifier.get_instance_status()
    masked_key = MASKED_SECRET_PLACEHOLDER if sys_cfg.evolution_api_key else ""
    return EvolutionSettingsResponse(
        evolution_api_url=sys_cfg.evolution_api_url,
        evolution_api_key=masked_key,
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
def connect_evolution_instance(request: Request, db: Session = Depends(get_db)):
    """Cria a instância (se não existir) e retorna o QR Code Base64 para leitura na interface web (Admin)."""
    _require_admin(request)
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
def logout_evolution_instance(request: Request, db: Session = Depends(get_db)):
    """Desconecta a sessão do WhatsApp da instância ativa na Evolution API (Exclusivo Admin)."""
    ctx = _require_admin(request)
    record_audit_log(
        db,
        username=ctx["username"],
        action="EVOLUTION_LOGOUT",
        details="Desconectou sessão WhatsApp da Evolution API",
        request=request,
    )
    return whatsapp_notifier.logout_instance()


@router.get("/devices", response_model=List[DeviceConfigResponse], tags=["Freezers / Câmaras Frias"])
def list_devices(db: Session = Depends(get_db)):
    """Lista todos os freezers/câmaras frias monitorados e suas faixas de temperatura."""
    devices = db.query(DeviceConfig).order_by(DeviceConfig.device_id).all()
    results: List[DeviceConfigResponse] = []
    for dev in devices:
        resp = DeviceConfigResponse.model_validate(dev)
        resp.in_quiet_hours = mqtt_service.is_in_quiet_hours(
            {
                "quiet_hours_enabled": bool(getattr(dev, "quiet_hours_enabled", False)),
                "quiet_hours_start": getattr(dev, "quiet_hours_start", None) or settings.default_quiet_hours_start,
                "quiet_hours_end": getattr(dev, "quiet_hours_end", None) or settings.default_quiet_hours_end,
            }
        )
        results.append(resp)
    return results


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
    resp.in_quiet_hours = mqtt_service.is_in_quiet_hours(
        {
            "quiet_hours_enabled": bool(getattr(device, "quiet_hours_enabled", False)),
            "quiet_hours_start": getattr(device, "quiet_hours_start", None) or settings.default_quiet_hours_start,
            "quiet_hours_end": getattr(device, "quiet_hours_end", None) or settings.default_quiet_hours_end,
        }
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
def configure_device_wifi(
    device_id: str,
    payload: DeviceWifiConfigRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Envia comando via MQTT para o ESP32 abrir o Portal AP ou trocar de Wi-Fi (Exclusivo Admin)."""
    ctx = _require_admin(request)
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
    safe_details = f"device={device_id}, start_ap={payload.start_ap}, ssid={payload.wifi_ssid or '-'}"
    record_audit_log(
        db,
        username=ctx["username"],
        action="DEVICE_WIFI_CMD",
        details=safe_details,
        request=request,
    )
    return {
        "ok": published,
        "device_id": device_id,
        "command_sent": {k: ("***" if k == "wifi_pass" else v) for k, v in cmd.items()},
    }


@router.patch(
    "/devices/{device_id}/thresholds",
    response_model=DeviceConfigResponse,
    tags=["Freezers / Câmaras Frias"],
)
def update_device_thresholds(
    device_id: str,
    payload: ThresholdUpdateRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Atualiza a faixa ideal de temperatura (min/max) definível pelo usuário e sincroniza via MQTT."""
    ctx = getattr(request.state, "auth", None) or get_request_auth_context(request)
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
            quiet_hours_enabled=(
                payload.quiet_hours_enabled
                if payload.quiet_hours_enabled is not None
                else settings.default_quiet_hours_enabled
            ),
            quiet_hours_start=payload.quiet_hours_start or settings.default_quiet_hours_start,
            quiet_hours_end=payload.quiet_hours_end or settings.default_quiet_hours_end,
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
        if payload.quiet_hours_enabled is not None:
            device.quiet_hours_enabled = payload.quiet_hours_enabled
        if payload.quiet_hours_start is not None:
            device.quiet_hours_start = payload.quiet_hours_start.strip()
        if payload.quiet_hours_end is not None:
            device.quiet_hours_end = payload.quiet_hours_end.strip()

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
            "quiet_hours_enabled": bool(device.quiet_hours_enabled),
            "quiet_hours_start": device.quiet_hours_start or settings.default_quiet_hours_start,
            "quiet_hours_end": device.quiet_hours_end or settings.default_quiet_hours_end,
        }
    )

    mqtt_service.publish_threshold_config(
        device_id,
        device.min_temp_c,
        device.max_temp_c,
        device.telemetry_interval_seconds,
    )

    quiet_desc = (
        f"ativo ({device.quiet_hours_start} às {device.quiet_hours_end})"
        if device.quiet_hours_enabled
        else "desativado"
    )
    record_audit_log(
        db,
        username=ctx.get("username") or "unknown",
        action="DEVICE_THRESHOLD_UPDATE",
        details=(
            f"device={device_id}, faixa=[{device.min_temp_c}°C a {device.max_temp_c}°C], "
            f"delay={device.alert_delay_seconds}s, modo_sem_alertas={quiet_desc}"
        ),
        request=request,
    )

    resp = DeviceConfigResponse.model_validate(device)
    resp.replay_interval_seconds = int(
        sys_cfg.replay_interval_seconds or settings.replay_interval_seconds
    )
    resp.in_quiet_hours = mqtt_service.is_in_quiet_hours(
        {
            "quiet_hours_enabled": bool(device.quiet_hours_enabled),
            "quiet_hours_start": device.quiet_hours_start or settings.default_quiet_hours_start,
            "quiet_hours_end": device.quiet_hours_end or settings.default_quiet_hours_end,
        }
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
    db: Session = Depends(get_db),
):
    """Recebe (opcionalmente) o binário .bin, valida cabeçalho 0xE9 e SHA-256, e publica ordem OTA assinada (Exclusivo Admin)."""
    ctx = _require_admin(request)
    if not ota_manager.verify_admin_secret(x_ota_secret):
        record_audit_log(
            db,
            username=ctx["username"],
            action="OTA_DEPLOY_FAIL",
            details=f"Tentativa OTA para {device_id} bloqueada: X-OTA-Secret inválido",
            request=request,
        )
        raise HTTPException(
            status_code=401,
            detail="Não autorizado: chave administrativa X-OTA-Secret ausente ou inválida.",
        )

    content_len_str = request.headers.get("content-length")
    if content_len_str and content_len_str.isdigit() and int(content_len_str) > MAX_FIRMWARE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Arquivo excede o tamanho máximo permitido ({MAX_FIRMWARE_BYTES} bytes).",
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

    record_audit_log(
        db,
        username=ctx["username"],
        action="OTA_DEPLOY",
        details=f"Disparou OTA v{signed_trigger['version']} para '{device_id}' (SHA256={signed_trigger['sha256'][:16]}...)",
        request=request,
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
