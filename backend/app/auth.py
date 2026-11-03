"""Módulo de Autenticação do Painel Web, Usuários no Banco (PBKDF2-HMAC-SHA256), RBAC, CSRF, Rate-Limit e Security Headers."""

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.database import SessionLocal
from app.models import AuditLog, User

logger = logging.getLogger(__name__)

SESSION_COOKIE_NAME = "coutos_session"
CSRF_COOKIE_NAME = "coutos_csrf"
SESSION_TTL_SECONDS = settings.session_ttl_seconds or 43200  # 12 horas padrão
PBKDF2_ITERATIONS = 600_000

# Expressão regular de defesa em profundidade contra acesso a arquivos sensíveis via HTTP
SENSITIVE_PATH_REGEX = re.compile(
    r"(^|/)\.|"  # Qualquer arquivo/diretório oculto (.env, .git, .htaccess)
    r"\.\.|"     # Path traversal
    r"\.(env|git|sqlite|sqlite3|db|sql|py|pyc|ini|sh|yml|yaml|conf|h|ino|bin|elf|map|key|pem|crt|log)$|"
    r"^/(data|ota|postgres|mosquitto|firmware|scripts)(/|$)",
    re.IGNORECASE,
)


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Gera hash seguro PBKDF2-HMAC-SHA256 com salt criptográfico de 128 bits (Padrão OWASP)."""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        iterations,
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


# Hash dummy pré-computado para mitigar enumeração de usuários por timing attack
_DUMMY_PASSWORD_HASH = hash_password(" dummy_timing_mitigation_secret_2026 ")


def verify_password(password: str, stored_hash: str) -> bool:
    """Verifica senha contra hash PBKDF2-HMAC-SHA256 em tempo constante."""
    if not stored_hash or not password:
        return False
    try:
        parts = stored_hash.split("$")
        if len(parts) != 4 or parts[0] != "pbkdf2_sha256":
            return False
        iterations = int(parts[1])
        salt_hex = parts[2]
        expected_hex = parts[3]
        dk = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
        )
        return hmac.compare_digest(dk.hex(), expected_hex)
    except Exception:
        return False


def validate_password_strength(password: str) -> Optional[str]:
    """Valida requisitos mínimos de complexidade para senhas administrativas."""
    if len(password) < 8:
        return "A senha deve ter pelo menos 8 caracteres."
    if len(password) > 128:
        return "A senha não pode exceder 128 caracteres."
    has_letter = any(c.isalpha() for c in password)
    has_digit_or_sym = any(c.isdigit() or not c.isalnum() for c in password)
    if not (has_letter and has_digit_or_sym):
        return "A senha deve conter letras e pelo menos um número ou caractere especial."
    return None


class IPRateLimiter:
    """Limitador de taxa em memória por IP para defesa em profundidade contra força bruta."""

    def __init__(self, max_attempts: int = 10, window_seconds: int = 60) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._attempts: Dict[str, list[float]] = {}

    def is_allowed(self, ip: str) -> Tuple[bool, int]:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            history = [t for t in self._attempts.get(ip, []) if t > cutoff]
            if len(history) >= self.max_attempts:
                self._attempts[ip] = history
                retry_after = max(1, int(self.window_seconds - (now - history[0])))
                return False, retry_after
            history.append(now)
            self._attempts[ip] = history
            return True, 0

    def reset(self, ip: str) -> None:
        with self._lock:
            self._attempts.pop(ip, None)


login_ip_limiter = IPRateLimiter(max_attempts=10, window_seconds=60)

# Cache curto em memória para estado de sessão do usuário (evita query SQL a cada poll de 5s)
_user_state_cache_lock = threading.Lock()
_user_state_cache: Dict[str, Dict[str, Any]] = {}
_USER_CACHE_TTL_SECONDS = 5.0


def invalidate_user_session_cache(username: Optional[str] = None) -> None:
    """Invalida o cache em memória de validação de sessão (imediato após alteração de usuário)."""
    with _user_state_cache_lock:
        if username:
            _user_state_cache.pop(username.lower(), None)
        else:
            _user_state_cache.clear()


def _get_cached_user_session_state(username: str) -> Optional[Dict[str, Any]]:
    """Retorna {is_active, role, session_version} do banco com cache de 5s."""
    key = username.lower()
    now = time.time()
    with _user_state_cache_lock:
        cached = _user_state_cache.get(key)
        if cached and now < cached["expires_at"]:
            return cached

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == username).first()
        if not user:
            return None
        state = {
            "is_active": bool(user.is_active),
            "role": user.role,
            "session_version": int(user.session_version or 1),
            "expires_at": now + _USER_CACHE_TTL_SECONDS,
        }
        with _user_state_cache_lock:
            _user_state_cache[key] = state
        return state
    except Exception:
        # Se o PostgreSQL estiver temporariamente indisponível, permite fallback resiliente
        return {
            "is_active": True,
            "role": "admin",
            "session_version": None,
            "expires_at": now + 2.0,
        }
    finally:
        db.close()


def get_client_ip(request: Request) -> str:
    """Extrai o IP real do cliente considerando o proxy reverso interno."""
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()[:64]
    xri = request.headers.get("X-Real-IP")
    if xri:
        return xri.strip()[:64]
    if request.client and request.client.host:
        return request.client.host[:64]
    return "unknown"


def record_audit_log(
    db: Optional[Session],
    username: str,
    action: str,
    details: Optional[str] = None,
    request: Optional[Request] = None,
) -> None:
    """Registra evento na trilha de auditoria do PostgreSQL sem interromper o fluxo em caso de falha."""
    ip_addr = get_client_ip(request) if request else None
    ua = (request.headers.get("User-Agent") or "")[:250] if request else None
    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True
    try:
        entry = AuditLog(
            username=username[:64] if username else "anonymous",
            action=action[:64],
            ip_address=ip_addr,
            user_agent=ua,
            details=details,
        )
        db.add(entry)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.debug("Não foi possível gravar AuditLog (%s): %s", action, exc)
    finally:
        if own_session:
            db.close()


def _sign_data(data: str) -> str:
    secret = (settings.api_secret_key or "default_fallback_secret").encode("utf-8")
    return hmac.new(secret, data.encode("utf-8"), hashlib.sha256).hexdigest()


def create_session_token(
    username: str,
    role: str = "admin",
    session_version: int = 1,
) -> Tuple[str, str]:
    """Gera token de sessão assinado via HMAC-SHA256 e token CSRF vinculado."""
    expires_at = int(time.time()) + SESSION_TTL_SECONDS
    csrf_token = secrets.token_urlsafe(24)
    payload = f"{username}|{role}|{session_version}|{expires_at}|{csrf_token}"
    sig = _sign_data(payload)
    raw = f"{payload}|{sig}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8"), csrf_token


def verify_session_token_detailed(token: Optional[str]) -> Optional[Dict[str, Any]]:
    """Valida o token do cookie HttpOnly, expiração, assinatura HMAC e versão de sessão no banco."""
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        parts = decoded.split("|")
        if len(parts) == 6:
            username, role, ver_str, exp_str, csrf_token, sig = parts
            session_version = int(ver_str)
            expires_at = int(exp_str)
            if time.time() > expires_at:
                return None
            payload = f"{username}|{role}|{ver_str}|{exp_str}|{csrf_token}"
            expected_sig = _sign_data(payload)
            if not hmac.compare_digest(sig, expected_sig):
                return None

            db_state = _get_cached_user_session_state(username)
            if db_state is None:
                return None
            if not db_state["is_active"]:
                return None
            if (
                db_state["session_version"] is not None
                and db_state["session_version"] != session_version
            ):
                return None

            return {
                "username": username,
                "role": db_state.get("role") or role,
                "session_version": session_version,
                "csrf_token": csrf_token,
                "auth_method": "cookie",
            }
        # Compatibilidade retroativa com formato de 3 partes se necessário
        if len(parts) == 3:
            username, exp_str, sig = parts
            if time.time() > int(exp_str):
                return None
            if not hmac.compare_digest(sig, _sign_data(f"{username}|{exp_str}")):
                return None
            return {
                "username": username,
                "role": "admin",
                "session_version": 1,
                "csrf_token": _sign_data(f"csrf|{username}|{exp_str}")[:32],
                "auth_method": "cookie",
            }
        return None
    except Exception:
        return None


def verify_session_token(token: Optional[str]) -> Optional[str]:
    """Retorna apenas o username se o token de sessão for válido."""
    info = verify_session_token_detailed(token)
    return info["username"] if info else None


def verify_dashboard_credentials(
    username: str,
    password: str,
    db: Optional[Session] = None,
    client_ip: Optional[str] = None,
) -> Tuple[bool, Optional[User], Optional[str]]:
    """Valida credenciais no banco PostgreSQL com proteção contra timing attack e lockout de conta."""
    clean_user = (username or "").strip()
    if not clean_user or not password:
        return False, None, "Informe usuário e senha."

    own_session = False
    if db is None:
        db = SessionLocal()
        own_session = True

    now = datetime.now(timezone.utc)
    try:
        user = db.query(User).filter(User.username == clean_user).first()
        if not user:
            # Executa PBKDF2 dummy para manter tempo de resposta constante (anti-enumeração)
            verify_password(password, _DUMMY_PASSWORD_HASH)
            return False, None, "Usuário ou senha incorretos."

        if not user.is_active:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            return False, user, "Esta conta de usuário está desativada. Contate o administrador."

        if user.locked_until:
            locked_until = (
                user.locked_until
                if user.locked_until.tzinfo
                else user.locked_until.replace(tzinfo=timezone.utc)
            )
            if now < locked_until:
                remaining_s = max(1, int((locked_until - now).total_seconds()))
                mins = max(1, (remaining_s + 59) // 60)
                return (
                    False,
                    user,
                    f"Conta temporariamente bloqueada por tentativas excessivas. Tente novamente em {mins} min.",
                )
            # Lockout expirou
            user.locked_until = None
            user.failed_login_attempts = 0

        if not verify_password(password, user.password_hash):
            user.failed_login_attempts = int(user.failed_login_attempts or 0) + 1
            max_attempts = max(3, int(settings.max_failed_login_attempts or 5))
            if user.failed_login_attempts >= max_attempts:
                lock_secs = max(60, int(settings.account_lockout_seconds or 900))
                user.locked_until = now + timedelta(seconds=lock_secs)
                db.commit()
                return (
                    False,
                    user,
                    f"Conta bloqueada por {lock_secs // 60} minutos após {user.failed_login_attempts} tentativas inválidas.",
                )
            db.commit()
            return False, user, "Usuário ou senha incorretos."

        # Autenticado com sucesso
        user.failed_login_attempts = 0
        user.locked_until = None
        user.last_login_at = now
        if client_ip:
            user.last_login_ip = client_ip[:64]
        db.commit()
        db.refresh(user)
        invalidate_user_session_cache(user.username)
        return True, user, None
    except Exception as exc:
        db.rollback()
        logger.warning("[AUTH] Banco indisponível no login (%s). Verificando fallback de emergência.", exc)
        # Fallback de emergência somente se o PostgreSQL estiver fora do ar
        expected_user = (settings.dashboard_username or "admin").strip()
        expected_pass = settings.dashboard_password or ""
        if expected_pass:
            u_ok = hmac.compare_digest(clean_user, expected_user)
            p_ok = hmac.compare_digest(password, expected_pass)
            if u_ok and p_ok:
                fallback_user = User(
                    id=0,
                    username=expected_user,
                    full_name="Administrador (Modo Resiliente)",
                    role="admin",
                    is_active=True,
                    session_version=1,
                )
                return True, fallback_user, None
        return False, None, "Erro ao validar credenciais."
    finally:
        if own_session:
            db.close()


def get_request_auth_context(request: Request) -> Dict[str, Any]:
    """Inspeciona a requisição e retorna contexto detalhado de autenticação e papel (role)."""
    # 1. Cookie de sessão HttpOnly
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    session_info = verify_session_token_detailed(cookie_token)
    if session_info:
        return {
            "authenticated": True,
            "username": session_info["username"],
            "role": session_info["role"],
            "csrf_token": session_info["csrf_token"],
            "auth_method": "cookie",
        }

    # 2. Header X-API-Key ou X-OTA-Secret (para automação do Makefile / CLI)
    api_key = request.headers.get("X-API-Key") or request.headers.get("X-OTA-Secret")
    if api_key:
        valid_keys = [
            k for k in (settings.api_secret_key, settings.ota_shared_secret) if k
        ]
        if any(hmac.compare_digest(api_key.strip(), k) for k in valid_keys):
            return {
                "authenticated": True,
                "username": "api_key_client",
                "role": "admin",
                "csrf_token": None,
                "auth_method": "api_key",
            }

    # 3. HTTP Basic Auth (para curl -u ou integrações)
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("basic "):
        try:
            b64_part = auth_header.split(" ", 1)[1].strip()
            decoded = base64.b64decode(b64_part).decode("utf-8")
            if ":" in decoded:
                u, p = decoded.split(":", 1)
                ok, user_obj, _ = verify_dashboard_credentials(u, p)
                if ok and user_obj:
                    return {
                        "authenticated": True,
                        "username": user_obj.username,
                        "role": user_obj.role,
                        "csrf_token": None,
                        "auth_method": "basic",
                    }
        except Exception:
            pass

    return {
        "authenticated": False,
        "username": None,
        "role": None,
        "csrf_token": None,
        "auth_method": None,
    }


def is_request_authenticated(request: Request) -> Tuple[bool, Optional[str]]:
    """Compatibilidade com chamadas que esperam (authed, username)."""
    ctx = get_request_auth_context(request)
    return ctx["authenticated"], ctx["username"]


def _verify_csrf_and_origin(request: Request, auth_ctx: Dict[str, Any]) -> Optional[str]:
    """Valida token CSRF e cabeçalhos Origin/Referer para mutações autenticadas via Cookie."""
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if auth_ctx.get("auth_method") != "cookie":
        return None

    # Verifica Origin / Referer se enviados pelo navegador (ignora diferença de porta do proxy reverso)
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host")
    origin = request.headers.get("Origin")
    if origin and host:
        parsed_origin = urlparse(origin)
        origin_host = (parsed_origin.hostname or parsed_origin.netloc.split(":")[0]).lower()
        target_host = host.split(":")[0].strip().lower()
        if origin_host and target_host and origin_host != target_host:
            return f"Origem da requisição ({parsed_origin.netloc}) não autorizada para este host ({host})."

    expected_csrf = auth_ctx.get("csrf_token")
    provided_csrf = request.headers.get("X-CSRF-Token")
    if not expected_csrf or not provided_csrf:
        return "Token de proteção CSRF (X-CSRF-Token) ausente. Recarregue a página e tente novamente."
    if not hmac.compare_digest(provided_csrf.strip(), expected_csrf):
        return "Token de proteção CSRF inválido ou expirado. Recarregue a página."
    return None


class SecurityAndAuthMiddleware(BaseHTTPMiddleware):
    """Middleware de Defesa em Profundidade: bloqueia arquivos sensíveis, exige autenticação, valida CSRF e aplica Security Headers."""

    PUBLIC_EXACT_PATHS = {
        "/login",
        "/health",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/check",
    }

    PUBLIC_STATIC_PREFIXES = (
        "/static/css/",
        "/static/js/",
    )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # 1. Bloqueio estrito de arquivos sensíveis / ocultos / traversal
        if SENSITIVE_PATH_REGEX.search(path):
            logger.warning("[SEGURANÇA] Tentativa de acesso a caminho sensível bloqueada: %s", path)
            return JSONResponse(
                status_code=403,
                content={"detail": "Acesso proibido a arquivos internos ou de configuração."},
            )

        # Bloqueia acesso direto aos arquivos HTML brutos em /static/*.html sem passar pelas rotas autenticadas
        if path.startswith("/static/") and path.endswith(".html"):
            return RedirectResponse(url="/login", status_code=302)

        auth_ctx = get_request_auth_context(request)
        request.state.auth = auth_ctx

        # 2. Proteção Server-Side das páginas HTML principais (/ e /admin)
        if path in ("/", "/admin"):
            if not auth_ctx["authenticated"]:
                return RedirectResponse(url="/login", status_code=302)
        elif path == "/login" and auth_ctx["authenticated"]:
            return RedirectResponse(url="/", status_code=302)

        # 3. Verifica se a rota é pública ou exige autenticação
        is_public = (
            path in self.PUBLIC_EXACT_PATHS
            or path.startswith("/api/v1/ota/download/")
            or any(path.startswith(pref) for pref in self.PUBLIC_STATIC_PREFIXES)
        )

        if not is_public:
            if not auth_ctx["authenticated"]:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Não autenticado. Faça login no painel ou informe X-API-Key válida."},
                )

            # 4. Proteção CSRF para requisições mutáveis autenticadas via Cookie de navegador
            csrf_err = _verify_csrf_and_origin(request, auth_ctx)
            if csrf_err:
                logger.warning(
                    "[SEGURANÇA CSRF] Bloqueado %s %s para usuário '%s': %s",
                    request.method,
                    path,
                    auth_ctx.get("username"),
                    csrf_err,
                )
                return JSONResponse(status_code=403, content={"detail": csrf_err})

        response = await call_next(request)

        # 5. Cabeçalhos de Segurança HTTP (Hardening OWASP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        if path.startswith("/api/") or path in ("/", "/admin", "/login"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response
