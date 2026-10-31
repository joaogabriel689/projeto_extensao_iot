"""Módulo de Autenticação do Painel Web, Sessões HMAC-SHA256 e Bloqueio de Arquivos Sensíveis."""

import base64
import hashlib
import hmac
import logging
import re
import time
from typing import Optional, Tuple

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "coutos_session"
SESSION_TTL_SECONDS = 86400  # 24 horas

# Expressão regular de defesa em profundidade contra acesso a arquivos sensíveis via HTTP
SENSITIVE_PATH_REGEX = re.compile(
    r"(^|/)\.|"  # Qualquer arquivo/diretório oculto (.env, .git, .htaccess)
    r"\.\.|"     # Path traversal
    r"\.(env|git|sqlite|sqlite3|db|sql|py|pyc|ini|sh|yml|yaml|conf|h|ino|bin|elf|map|key|pem|crt|log)$|"
    r"^/(data|ota|postgres|mosquitto|firmware|scripts)(/|$)",
    re.IGNORECASE,
)


def _sign_data(data: str) -> str:
    secret = (settings.api_secret_key or "default_fallback_secret").encode("utf-8")
    return hmac.new(secret, data.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(username: str) -> str:
    """Gera token de sessão assinado via HMAC-SHA256 com expiração."""
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    payload = f"{username}|{expires_at}"
    sig = _sign_data(payload)
    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def verify_session_token(token: Optional[str]) -> Optional[str]:
    """Valida o token do cookie HttpOnly e retorna o username se válido e não expirado."""
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) != 3:
            return None
        username, exp_str, sig = parts
        expires_at = int(exp_str)
        if time.time() > expires_at:
            return None
        expected_sig = _sign_data(f"{username}|{exp_str}")
        if not hmac.compare_digest(sig, expected_sig):
            return None
        return username
    except Exception:
        return None


def verify_dashboard_credentials(username: str, password: str) -> bool:
    """Compara usuário e senha do painel em tempo constante (proteção contra timing attack)."""
    expected_user = (settings.dashboard_username or "admin").strip()
    expected_pass = settings.dashboard_password or ""
    if not expected_pass:
        return False
    user_ok = hmac.compare_digest(username.strip(), expected_user)
    pass_ok = hmac.compare_digest(password, expected_pass)
    return user_ok and pass_ok


def is_request_authenticated(request: Request) -> Tuple[bool, Optional[str]]:
    """Verifica se a requisição possui Cookie de Sessão, Header X-API-Key/X-OTA-Secret ou Basic Auth válidos."""
    # 1. Cookie de sessão HttpOnly
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    user = verify_session_token(cookie_token)
    if user:
        return True, user

    # 2. Header X-API-Key ou X-OTA-Secret (para automação do Makefile / CLI)
    api_key = request.headers.get("X-API-Key") or request.headers.get("X-OTA-Secret")
    if api_key:
        valid_keys = [
            k for k in (settings.api_secret_key, settings.ota_shared_secret) if k
        ]
        if any(hmac.compare_digest(api_key.strip(), k) for k in valid_keys):
            return True, "api_key_client"

    # 3. HTTP Basic Auth (para acesso ao Swagger /docs ou curl -u)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("basic "):
        try:
            b64_part = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(b64_part).decode("utf-8")
            if ":" in decoded:
                u, p = decoded.split(":", 1)
                if verify_dashboard_credentials(u, p):
                    return True, u
        except Exception:
            pass

    return False, None


class SecurityAndAuthMiddleware(BaseHTTPMiddleware):
    """Middleware de Defesa em Profundidade: bloqueia arquivos sensíveis, exige autenticação e aplica Security Headers."""

    PUBLIC_EXACT_PATHS = {
        "/",
        "/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/check",
    }

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Bloqueio estrito de arquivos sensíveis / ocultos / traversal
        if SENSITIVE_PATH_REGEX.search(path):
            logger.warning("[SEGURANÇA] Tentativa de acesso a caminho sensível bloqueada: %s", path)
            return JSONResponse(
                status_code=403,
                content={"detail": "Acesso proibido a arquivos internos ou de configuração."},
            )

        # 2. Verifica se a rota exige autenticação do painel/API
        is_public = (
            path in self.PUBLIC_EXACT_PATHS
            or path.startswith("/api/v1/ota/download/")
        )

        if not is_public:
            authed, _ = is_request_authenticated(request)
            if not authed:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Não autenticado. Faça login no painel ou informe X-API-Key válida."},
                    headers={"WWW-Authenticate": 'Basic realm="Coutos Conveniencia Restrito"'},
                )

        response = await call_next(request)

        # 3. Cabeçalhos de Segurança HTTP (Hardening)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
        return response
