-- ==============================================================================
-- Inicialização automática do banco adicional para a Evolution API (WhatsApp)
-- Usamos arquivo .sql puro (com \gexec) em vez de .sh para garantir 100% de
-- compatibilidade entre Linux e Windows (evita problemas de permissão chmod +x).
-- ==============================================================================
SELECT 'CREATE DATABASE evolution_db'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'evolution_db')\gexec
