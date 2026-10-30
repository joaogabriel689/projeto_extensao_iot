"""Ponto de entrada FastAPI, ciclo de vida (lifespan), inicialização do DB e Worker de Replay."""

import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router as api_router
from app.buffer import local_buffer
from app.config import settings
from app.database import Base, SessionLocal, engine
from app.models import DeviceConfig
from app.mqtt_service import mqtt_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


def init_db_and_default_device() -> None:
    """Cria as tabelas no PostgreSQL e garante o cadastro inicial da câmara fria padrão."""
    try:
        Base.metadata.create_all(bind=engine)
        db = SessionLocal()
        try:
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
                    alert_cooldown_seconds=settings.alert_cooldown_seconds,
                    merchant_phone=settings.merchant_whatsapp_number,
                    notifications_enabled=settings.whatsapp_notifications_enabled,
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
                    "alert_cooldown_seconds": device.alert_cooldown_seconds,
                    "merchant_phone": device.merchant_phone,
                    "notifications_enabled": device.notifications_enabled,
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
        "API e Worker de Telemetria MQTT com detecção de anomalias térmicas, "
        "suporte a Last Will and Testament (LWT), buffer local tolerante a falhas "
        "e notificações via WhatsApp (Evolution API)."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# CORS liberado para facilitar integração com a futura Interface Web
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router)


@app.get("/health", tags=["Health"])
def health_check():
    """Healthcheck rápido para o Docker Compose."""
    return {"status": "ok"}
