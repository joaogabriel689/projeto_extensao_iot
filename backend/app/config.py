"""Configurações centralizadas da aplicação via variáveis de ambiente."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Carrega e valida todas as configurações do ambiente (.env)."""

    # Geral / Dispositivo padrão
    tz: str = "America/Campo_Grande"
    default_device_id: str = "camara01"
    default_device_name: str = "Camara Fria Principal - Coutos Conveniencia"
    default_min_temp_c: float = -10.0
    default_max_temp_c: float = 5.0
    alert_cooldown_seconds: int = 300

    # Broker MQTT
    mqtt_broker_host: str = "mosquitto"
    mqtt_broker_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_topic_prefix: str = "loja/freezer"

    # Banco de Dados PostgreSQL
    postgres_user: str = "coutos_admin"
    postgres_password: str = "troque_esta_senha_segura_123"
    postgres_db: str = "coutos_telemetria"
    postgres_host: str = "postgres"
    postgres_port: int = 5432

    # Buffer Local Tolerante a Falhas (SQLite)
    local_buffer_db_path: str = "/app/data/offline_buffer.sqlite3"
    replay_interval_seconds: int = 10

    # Segurança da API
    api_secret_key: str = "troque_por_uma_chave_aleatoria_segura"

    # Evolution API (WhatsApp)
    evolution_api_url: str = "http://evolution_api:8080"
    evolution_api_key: str = ""
    evolution_instance_name: str = "coutos_alertas"
    merchant_whatsapp_number: str = ""
    whatsapp_notifications_enabled: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def database_url(self) -> str:
        """Retorna a URL de conexão SQLAlchemy para o PostgreSQL."""
        return (
            f"postgresql+psycopg2://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


settings = Settings()
