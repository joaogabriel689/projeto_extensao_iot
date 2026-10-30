"""Gerenciamento de conexão com o PostgreSQL e sessão SQLAlchemy."""

import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

logger = logging.getLogger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """Dependência FastAPI para fornecer sessão de banco de dados por requisição."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def check_postgres_health() -> bool:
    """Verifica se o banco PostgreSQL principal está acessível."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.warning("PostgreSQL indisponível no momento: %s", exc)
        return False
