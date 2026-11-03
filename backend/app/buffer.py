"""Buffer local resiliente em SQLite para retenção e replay idempotente de telemetria.

Atende ao requisito do Roteiro de Extensão (Meta 2 - Tolerância a Interrupções):
Caso o banco PostgreSQL principal fique indisponível, as leituras MQTT são
gravadas localmente em disco e reenviadas de forma idempotente (replay) assim
que a conexão com o banco central é restabelecida.
"""

from datetime import datetime, timezone
import logging
import os
import sqlite3
from typing import Any, Dict, List

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.database import SessionLocal
from app.models import TelemetryLog

logger = logging.getLogger(__name__)


class LocalResilientBuffer:
    """Gerencia o armazenamento local SQLite (WAL) e o replay idempotente para o PostgreSQL."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or settings.local_buffer_db_path
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_sqlite()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_sqlite(self) -> None:
        with self._get_conn() as conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buffered_telemetry (
                    event_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    min_temp_c REAL NOT NULL,
                    max_temp_c REAL NOT NULL,
                    is_out_of_range INTEGER NOT NULL,
                    recorded_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS device_config_cache (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    min_temp_c REAL NOT NULL,
                    max_temp_c REAL NOT NULL,
                    alert_delay_seconds INTEGER NOT NULL DEFAULT 180,
                    alert_cooldown_seconds INTEGER NOT NULL,
                    telemetry_interval_seconds INTEGER NOT NULL DEFAULT 10,
                    merchant_phone TEXT,
                    notifications_enabled INTEGER NOT NULL
                )
                """
            )
            cols = {
                r["name"]
                for r in conn.execute("PRAGMA table_info(device_config_cache)").fetchall()
            }
            if "alert_delay_seconds" not in cols:
                conn.execute(
                    "ALTER TABLE device_config_cache ADD COLUMN alert_delay_seconds INTEGER NOT NULL DEFAULT 180"
                )
            if "telemetry_interval_seconds" not in cols:
                conn.execute(
                    "ALTER TABLE device_config_cache ADD COLUMN telemetry_interval_seconds INTEGER NOT NULL DEFAULT 10"
                )
            if "quiet_hours_enabled" not in cols:
                conn.execute(
                    "ALTER TABLE device_config_cache ADD COLUMN quiet_hours_enabled INTEGER NOT NULL DEFAULT 0"
                )
            if "quiet_hours_start" not in cols:
                conn.execute(
                    "ALTER TABLE device_config_cache ADD COLUMN quiet_hours_start TEXT NOT NULL DEFAULT '23:00'"
                )
            if "quiet_hours_end" not in cols:
                conn.execute(
                    "ALTER TABLE device_config_cache ADD COLUMN quiet_hours_end TEXT NOT NULL DEFAULT '07:00'"
                )
            conn.commit()

    def cache_device_config(self, config_data: Dict[str, Any]) -> None:
        """Mantém cópia local da faixa ideal e modo noturno para operar mesmo sem o banco principal."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO device_config_cache (
                    device_id, name, min_temp_c, max_temp_c,
                    alert_delay_seconds, alert_cooldown_seconds, telemetry_interval_seconds,
                    merchant_phone, notifications_enabled,
                    quiet_hours_enabled, quiet_hours_start, quiet_hours_end
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    name=excluded.name,
                    min_temp_c=excluded.min_temp_c,
                    max_temp_c=excluded.max_temp_c,
                    alert_delay_seconds=excluded.alert_delay_seconds,
                    alert_cooldown_seconds=excluded.alert_cooldown_seconds,
                    telemetry_interval_seconds=excluded.telemetry_interval_seconds,
                    merchant_phone=excluded.merchant_phone,
                    notifications_enabled=excluded.notifications_enabled,
                    quiet_hours_enabled=excluded.quiet_hours_enabled,
                    quiet_hours_start=excluded.quiet_hours_start,
                    quiet_hours_end=excluded.quiet_hours_end
                """,
                (
                    config_data["device_id"],
                    config_data["name"],
                    float(config_data["min_temp_c"]),
                    float(config_data["max_temp_c"]),
                    int(config_data.get("alert_delay_seconds", settings.alert_delay_seconds)),
                    int(config_data["alert_cooldown_seconds"]),
                    int(
                        config_data.get(
                            "telemetry_interval_seconds",
                            settings.default_telemetry_interval_seconds,
                        )
                    ),
                    config_data.get("merchant_phone"),
                    1 if config_data.get("notifications_enabled", True) else 0,
                    1 if config_data.get("quiet_hours_enabled", False) else 0,
                    str(config_data.get("quiet_hours_start") or "23:00"),
                    str(config_data.get("quiet_hours_end") or "07:00"),
                ),
            )
            conn.commit()

    def get_cached_device_config(self, device_id: str) -> Dict[str, Any] | None:
        """Recupera a configuração em cache caso o PostgreSQL esteja offline."""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM device_config_cache WHERE device_id = ?",
                (device_id,),
            ).fetchone()
            if not row:
                return None
            return {
                "device_id": row["device_id"],
                "name": row["name"],
                "min_temp_c": float(row["min_temp_c"]),
                "max_temp_c": float(row["max_temp_c"]),
                "alert_delay_seconds": int(row["alert_delay_seconds"]),
                "alert_cooldown_seconds": int(row["alert_cooldown_seconds"]),
                "telemetry_interval_seconds": int(row["telemetry_interval_seconds"]),
                "merchant_phone": row["merchant_phone"],
                "notifications_enabled": bool(row["notifications_enabled"]),
                "quiet_hours_enabled": bool(row["quiet_hours_enabled"]),
                "quiet_hours_start": str(row["quiet_hours_start"] or "23:00"),
                "quiet_hours_end": str(row["quiet_hours_end"] or "07:00"),
            }

    def enqueue_telemetry(self, item: Dict[str, Any]) -> None:
        """Salva leitura no buffer local em disco (idempotente por event_id)."""
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO buffered_telemetry (
                    event_id, device_id, temperature_c, min_temp_c,
                    max_temp_c, is_out_of_range, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item["event_id"],
                    item["device_id"],
                    float(item["temperature_c"]),
                    float(item["min_temp_c"]),
                    float(item["max_temp_c"]),
                    1 if item.get("is_out_of_range") else 0,
                    item["recorded_at"].isoformat()
                    if isinstance(item["recorded_at"], datetime)
                    else str(item["recorded_at"]),
                ),
            )
            conn.commit()
        logger.warning(
            "[BUFFER LOCAL] Telemetria retida em disco (event_id=%s, temp=%.2f°C)",
            item["event_id"],
            float(item["temperature_c"]),
        )

    def pending_count(self) -> int:
        """Retorna a quantidade de leituras aguardando replay para o PostgreSQL."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS cnt FROM buffered_telemetry").fetchone()
            return int(row["cnt"]) if row else 0

    def replay_to_postgres(self, batch_size: int = 100) -> int:
        """Reenvia leituras retidas para o PostgreSQL de forma idempotente (ON CONFLICT DO NOTHING)."""
        with self._get_conn() as conn:
            rows: List[sqlite3.Row] = conn.execute(
                "SELECT * FROM buffered_telemetry ORDER BY recorded_at ASC LIMIT ?",
                (batch_size,),
            ).fetchall()

        if not rows:
            return 0

        records = []
        event_ids = []
        for r in rows:
            event_ids.append(r["event_id"])
            rec_dt = datetime.fromisoformat(r["recorded_at"])
            if rec_dt.tzinfo is None:
                rec_dt = rec_dt.replace(tzinfo=timezone.utc)
            records.append(
                {
                    "event_id": r["event_id"],
                    "device_id": r["device_id"],
                    "temperature_c": float(r["temperature_c"]),
                    "min_temp_c": float(r["min_temp_c"]),
                    "max_temp_c": float(r["max_temp_c"]),
                    "is_out_of_range": bool(r["is_out_of_range"]),
                    "replayed_from_buffer": True,
                    "recorded_at": rec_dt,
                }
            )

        db = SessionLocal()
        try:
            stmt = pg_insert(TelemetryLog).values(records)
            stmt = stmt.on_conflict_do_nothing(index_elements=["event_id"])
            db.execute(stmt)
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.debug("Replay adiado (PostgreSQL ainda indisponível): %s", exc)
            return 0
        finally:
            db.close()

        # Remove do buffer SQLite apenas após commit confirmado no PostgreSQL
        with self._get_conn() as conn:
            placeholders = ",".join("?" for _ in event_ids)
            conn.execute(
                f"DELETE FROM buffered_telemetry WHERE event_id IN ({placeholders})",
                event_ids,
            )
            conn.commit()

        logger.info(
            "[REPLAY CONCLUÍDO] %d leituras sincronizadas do buffer local para o PostgreSQL.",
            len(event_ids),
        )
        return len(event_ids)


local_buffer = LocalResilientBuffer()
