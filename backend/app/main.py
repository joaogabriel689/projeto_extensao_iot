"""Ponto de entrada FastAPI, ciclo de vida (lifespan), inicialização do DB, Worker MQTT e Interface Web."""

import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import re

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text

from app.api.routes import router as api_router
from app.auth import SecurityAndAuthMiddleware, hash_password
from app.buffer import local_buffer
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import DeviceConfig, SystemSettings, User
from app.mqtt_service import mqtt_service
from app.notifier import whatsapp_notifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
_SAFE_PG_IDENT_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,62}$")


def _validate_pg_ident(identifier: str, label: str) -> str:
    """Garante que identificadores SQL (roles/databases) obedeçam estritamente ao padrão seguro."""
    clean = (identifier or "").strip()
    if not _SAFE_PG_IDENT_RE.match(clean):
        raise ValueError(f"Identificador PostgreSQL inválido para {label}: {clean!r}")
    return clean


def _bootstrap_dedicated_roles_and_schema() -> None:
    """Garante de forma idempotente as roles dedicadas (menor privilégio) e migrações de schema."""
    admin_engine = create_engine(settings.admin_database_url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(bind=admin_engine)
        app_user = _validate_pg_ident(settings.postgres_app_user, "POSTGRES_APP_USER") if settings.postgres_app_user else ""
        app_pass = settings.postgres_app_password.replace("'", "''")
        evo_user = _validate_pg_ident(settings.evolution_db_user, "EVOLUTION_DB_USER") if settings.evolution_db_user else ""
        evo_pass = settings.evolution_db_password.replace("'", "''")
        db_name = _validate_pg_ident(settings.postgres_db, "POSTGRES_DB")
        evo_db = _validate_pg_ident(settings.evolution_db_name, "EVOLUTION_DB_NAME")

        with admin_engine.begin() as conn:
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS alert_delay_seconds INTEGER NOT NULL DEFAULT 180;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS telemetry_interval_seconds INTEGER NOT NULL DEFAULT 10;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS out_of_range_since TIMESTAMPTZ NULL;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS quiet_hours_enabled BOOLEAN NOT NULL DEFAULT FALSE;"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS quiet_hours_start VARCHAR(8) NOT NULL DEFAULT '23:00';"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE devices ADD COLUMN IF NOT EXISTS quiet_hours_end VARCHAR(8) NOT NULL DEFAULT '07:00';"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS replay_interval_seconds INTEGER NOT NULL DEFAULT 10;"
                )
            )

            if app_user and app_user != settings.postgres_user:
                conn.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{app_user}') THEN
                                CREATE ROLE {app_user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{app_pass}';
                            ELSE
                                ALTER ROLE {app_user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{app_pass}';
                            END IF;
                        END
                        $$;
                        """
                    )
                )
                conn.execute(text(f"GRANT CONNECT ON DATABASE {db_name} TO {app_user};"))
                conn.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {app_user};"))
                conn.execute(
                    text(
                        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_user};"
                    )
                )
                conn.execute(
                    text(
                        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {app_user};"
                    )
                )

            if evo_user and evo_user != settings.postgres_user:
                conn.execute(
                    text(
                        f"""
                        DO $$
                        BEGIN
                            IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = '{evo_user}') THEN
                                CREATE ROLE {evo_user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{evo_pass}';
                            ELSE
                                ALTER ROLE {evo_user} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD '{evo_pass}';
                            END IF;
                        END
                        $$;
                        """
                    )
                )
                conn.execute(text(f"GRANT ALL PRIVILEGES ON DATABASE {evo_db} TO {evo_user};"))
    finally:
        admin_engine.dispose()

    # Garante permissões de schema dentro do banco evolution_db para o usuário dedicado da Evolution API
    if settings.evolution_db_user and settings.evolution_db_user != settings.postgres_user:
        evo_admin_url = (
            f"postgresql+psycopg2://{settings.postgres_user}:{settings.postgres_password}"
            f"@{settings.postgres_host}:{settings.postgres_port}/{settings.evolution_db_name}"
        )
        evo_engine = create_engine(evo_admin_url, pool_pre_ping=True)
        try:
            with evo_engine.begin() as conn:
                eu = _validate_pg_ident(settings.evolution_db_user, "EVOLUTION_DB_USER")
                conn.execute(text(f"GRANT ALL ON SCHEMA public TO {eu};"))
                conn.execute(text(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {eu};"))
                conn.execute(text(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {eu};"))
                conn.execute(
                    text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {eu};")
                )
                conn.execute(
                    text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {eu};")
                )
        except Exception as exc:
            logger.debug("Permissões em evolution_db serão aplicadas quando o banco existir: %s", exc)
        finally:
            evo_engine.dispose()


def init_db_and_default_device() -> None:
    """Cria as tabelas no PostgreSQL, configura roles dedicadas, usuário admin inicial e dispositivo padrão."""
    try:
        _bootstrap_dedicated_roles_and_schema()
        db = SessionLocal()
        try:
            # 1. Garante usuário Administrador inicial no banco de dados (PBKDF2-HMAC-SHA256)
            user_count = db.query(User).count()
            if user_count == 0:
                initial_username = (settings.dashboard_username or "admin").strip()
                initial_password = settings.dashboard_password or "coutos_painel_2026"
                admin_user = User(
                    username=initial_username,
                    full_name="Administrador Principal",
                    password_hash=hash_password(initial_password),
                    role="admin",
                    is_active=True,
                    session_version=1,
                )
                db.add(admin_user)
                db.commit()
                logger.info(
                    "[DB INIT] Usuário administrador inicial '%s' criado no PostgreSQL (PBKDF2-HMAC-SHA256).",
                    initial_username,
                )

            # 2. Garante configurações globais da Evolution API
            sys_cfg = db.query(SystemSettings).filter(SystemSettings.id == 1).first()
            if not sys_cfg:
                sys_cfg = SystemSettings(
                    id=1,
                    evolution_api_url=settings.evolution_api_url.rstrip("/"),
                    evolution_api_key=settings.evolution_api_key,
                    evolution_instance_name=settings.evolution_instance_name,
                    merchant_whatsapp_number=settings.merchant_whatsapp_number,
                    whatsapp_notifications_enabled=settings.whatsapp_notifications_enabled,
                    replay_interval_seconds=settings.replay_interval_seconds,
                )
                db.add(sys_cfg)
                db.commit()
                db.refresh(sys_cfg)
            else:
                settings.replay_interval_seconds = int(
                    sys_cfg.replay_interval_seconds or settings.replay_interval_seconds
                )

            whatsapp_notifier.update_config(
                evolution_api_url=sys_cfg.evolution_api_url,
                evolution_api_key=sys_cfg.evolution_api_key,
                evolution_instance_name=sys_cfg.evolution_instance_name,
                merchant_whatsapp_number=sys_cfg.merchant_whatsapp_number,
                whatsapp_notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
            )

            # 3. Garante dispositivo padrão (câmara fria / freezer)
            device = (
                db.query(DeviceConfig)
                .filter(DeviceConfig.device_id == settings.default_device_id)
                .first()
            )
            if not device:
                device = DeviceConfig(
                    device_id=settings.default_device_id,
                    name=settings.default_device_name,
                    min_temp_c=settings.default_min_temp_c,
                    max_temp_c=settings.default_max_temp_c,
                    alert_delay_seconds=settings.alert_delay_seconds,
                    alert_cooldown_seconds=settings.alert_cooldown_seconds,
                    telemetry_interval_seconds=settings.default_telemetry_interval_seconds,
                    merchant_phone=sys_cfg.merchant_whatsapp_number,
                    notifications_enabled=sys_cfg.whatsapp_notifications_enabled,
                    quiet_hours_enabled=settings.default_quiet_hours_enabled,
                    quiet_hours_start=settings.default_quiet_hours_start,
                    quiet_hours_end=settings.default_quiet_hours_end,
                    online_status="unknown",
                )
                db.add(device)
                db.commit()
                db.refresh(device)
                logger.info(
                    "[DB INIT] Dispositivo padrão '%s' criado (Faixa ideal: %.1f°C a %.1f°C).",
                    device.device_id,
                    device.min_temp_c,
                    device.max_temp_c,
                )

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
                    "quiet_hours_enabled": device.quiet_hours_enabled,
                    "quiet_hours_start": device.quiet_hours_start,
                    "quiet_hours_end": device.quiet_hours_end,
                }
            )
        finally:
            db.close()
    except Exception as exc:
        logger.warning(
            "[DB INIT] PostgreSQL indisponível na inicialização (%s). Operando em modo resiliente local.",
            exc,
        )


async def periodic_buffer_replay_task(stop_event: asyncio.Event) -> None:
    """Tarefa assíncrona que verifica periodicamente o buffer local e faz replay idempotente."""
    while not stop_event.is_set():
        try:
            pending = local_buffer.pending_count()
            if pending > 0:
                Base.metadata.create_all(bind=engine)
                local_buffer.replay_to_postgres(batch_size=200)
        except Exception as exc:
            logger.debug("Ciclo de replay aguardando PostgreSQL: %s", exc)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.replay_interval_seconds)
        except asyncio.TimeoutError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia inicialização e encerramento limpo dos serviços."""
    init_db_and_default_device()
    mqtt_service.start()

    stop_event = asyncio.Event()
    replay_task = asyncio.create_task(periodic_buffer_replay_task(stop_event))

    yield

    stop_event.set()
    replay_task.cancel()
    mqtt_service.stop()


app = FastAPI(
    title="Sistema Coutos Conveniência - Telemetria Térmica Resiliente (IoT & MQTT)",
    description=(
        "API, Interface Web e Worker de Telemetria MQTT com detecção de anomalias térmicas, "
        "suporte a Last Will and Testament (LWT), buffer local tolerante a falhas, "
        "gestão de usuários RBAC no PostgreSQL e notificações via WhatsApp (Evolution API)."
    ),
    version="1.2.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.enable_swagger_docs else None,
    redoc_url="/redoc" if settings.enable_swagger_docs else None,
    openapi_url="/openapi.json" if settings.enable_swagger_docs else None,
)

app.add_middleware(SecurityAndAuthMiddleware)

# Configuração segura de CORS: apenas origens explicitamente autorizadas no .env
_allowed_origins = [
    o.strip() for o in (settings.cors_allowed_origins or "").split(",") if o.strip()
]
if _allowed_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-API-Key", "X-OTA-Secret"],
    )

app.include_router(api_router)

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/login", include_in_schema=False)
def serve_login_page():
    """Entrega a página dedicada de autenticação do Painel Web."""
    return FileResponse(str(STATIC_DIR / "login.html"))


@app.get("/", include_in_schema=False)
def serve_web_dashboard():
    """Entrega a Interface Web de Monitoramento e Configuração (protegida por sessão)."""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/admin", include_in_schema=False)
def serve_admin_page():
    """Entrega a Página Administrativa de Gestão de Usuários, Senhas e Auditoria de Segurança."""
    return FileResponse(str(STATIC_DIR / "admin.html"))


@app.get("/health", tags=["Health"])
def health_check():
    """Healthcheck rápido para o Docker Compose."""
    return {"status": "ok"}
