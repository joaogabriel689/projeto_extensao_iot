-- ==============================================================================
-- Inicialização Automática e Isolamento de Privilégios (Menor Privilégio)
-- Cria o banco evolution_db e configura roles dedicadas sem privilégio SUPERUSER:
--   1. telemetry_app_user -> Acesso restrito apenas ao banco coutos_telemetria
--   2. evolution_app_user -> Acesso restrito apenas ao banco evolution_db
-- ==============================================================================

SELECT 'CREATE DATABASE evolution_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evolution_db')\gexec

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'telemetry_app_user') THEN
        CREATE ROLE telemetry_app_user WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD 'troque_senha_app_telemetria_123';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'evolution_app_user') THEN
        CREATE ROLE evolution_app_user WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT PASSWORD 'troque_senha_app_evolution_123';
    END IF;
END
$$;

REVOKE ALL ON DATABASE coutos_telemetria FROM PUBLIC;
GRANT CONNECT ON DATABASE coutos_telemetria TO telemetry_app_user;
GRANT USAGE, CREATE ON SCHEMA public TO telemetry_app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO telemetry_app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO telemetry_app_user;

\c evolution_db
REVOKE ALL ON DATABASE evolution_db FROM PUBLIC;
GRANT ALL PRIVILEGES ON DATABASE evolution_db TO evolution_app_user;
GRANT ALL ON SCHEMA public TO evolution_app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO evolution_app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO evolution_app_user;
