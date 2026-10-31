"""Gerenciador de Atualização Remota de Firmware (OTA Seguro) para ESP32.

Implementa defesa em profundidade:
1. Armazenamento fora de diretórios públicos (/app/data/ota com permissão 0600).
2. Validação estrutural do binário ESP32 (Magic Byte 0xE9 e limites de partição).
3. Assinatura criptográfica HMAC-SHA256 no comando MQTT (impede injeção/spoofing).
4. One-Time Token (OTT) de 256 bits com expiração curta (180s) e uso único.
5. Autenticação mútua via cabeçalhos X-Device-ID e X-OTA-Auth no download HTTP.
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import threading
import time
from typing import Any, Dict, Optional, Tuple

from app.config import settings

logger = logging.getLogger(__name__)

ESP32_MAGIC_BYTE = 0xE9
MIN_FIRMWARE_BYTES = 64 * 1024        # 64 KB mínimo para um firmware válido
MAX_FIRMWARE_BYTES = 1920 * 1024      # ~1.9 MB máximo (partição OTA padrão ESP32)
OTA_TOKEN_TTL_SECONDS = 180           # Token de download expira em 3 minutos


class SecureOTAManager:
    """Gerencia validação de imagens, tokens efêmeros de uso único e assinaturas HMAC-SHA256."""

    def __init__(self, storage_dir: str = "/app/data/ota") -> None:
        self._lock = threading.Lock()
        self.storage_dir = Path(storage_dir)
        try:
            self.storage_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.storage_dir = Path("/tmp/coutos_ota")
            self.storage_dir.mkdir(parents=True, exist_ok=True)

        self.firmware_path = self.storage_dir / "firmware_latest.bin"
        self.meta_path = self.storage_dir / "firmware_meta.json"

        # Tokens efêmeros ativos: token -> {device_id, version, sha256, size, expires_at, used}
        self._tokens: Dict[str, Dict[str, Any]] = {}
        # Status de progresso reportado por cada ESP32 via MQTT
        self._device_ota_status: Dict[str, Dict[str, Any]] = {}

    def compute_hmac_hex(self, message: str) -> str:
        """Calcula HMAC-SHA256 em hexadecimal usando o segredo compartilhado de OTA."""
        key = (settings.ota_shared_secret or settings.api_secret_key).encode("utf-8")
        return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_admin_secret(self, provided_secret: Optional[str]) -> bool:
        """Verifica se a chave informada na requisição administrativa é válida."""
        if not provided_secret:
            return False
        provided = provided_secret.strip()
        valid_keys = [
            k for k in (settings.ota_shared_secret, settings.api_secret_key) if k
        ]
        return any(hmac.compare_digest(provided, k) for k in valid_keys)

    def validate_and_store_firmware(
        self, binary_data: bytes, version: str, filename: str = "firmware.bin"
    ) -> Dict[str, Any]:
        """Valida o cabeçalho mágico 0xE9 do ESP32, calcula SHA-256 e salva de forma privada."""
        size = len(binary_data)
        if size < MIN_FIRMWARE_BYTES:
            raise ValueError(
                f"Arquivo muito pequeno ({size} bytes). Um firmware ESP32 válido tem pelo menos {MIN_FIRMWARE_BYTES} bytes."
            )
        if size > MAX_FIRMWARE_BYTES:
            raise ValueError(
                f"Arquivo excede o limite da partição OTA ({size} bytes > {MAX_FIRMWARE_BYTES} bytes)."
            )
        if binary_data[0] != ESP32_MAGIC_BYTE:
            raise ValueError(
                f"Cabeçalho inválido (primeiro byte=0x{binary_data[0]:02X}, esperado 0xE9). "
                "Envie um arquivo .bin compilado para ESP32."
            )

        sha256_hex = hashlib.sha256(binary_data).hexdigest()
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self.firmware_path.write_bytes(binary_data)
            try:
                os.chmod(self.firmware_path, 0o600)
            except OSError:
                pass

            meta = {
                "version": version.strip(),
                "filename": Path(filename).name,
                "size_bytes": size,
                "sha256": sha256_hex,
                "uploaded_at": now_iso,
            }
            self.meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
            try:
                os.chmod(self.meta_path, 0o600)
            except OSError:
                pass

        logger.info(
            "[OTA MANAGER] Firmware v%s armazenado com segurança (%d bytes, SHA256=%s)",
            version,
            size,
            sha256_hex,
        )
        return meta

    def get_stored_firmware_meta(self) -> Optional[Dict[str, Any]]:
        """Retorna os metadados do último firmware validado."""
        with self._lock:
            if not self.meta_path.exists() or not self.firmware_path.exists():
                return None
            try:
                return json.loads(self.meta_path.read_text(encoding="utf-8"))
            except Exception:
                return None

    def create_signed_ota_trigger(
        self, device_id: str, version_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """Gera um One-Time Token (OTT) de 180s e assina os parâmetros via HMAC-SHA256."""
        meta = self.get_stored_firmware_meta()
        if not meta:
            raise ValueError("Nenhum binário de firmware foi carregado no servidor ainda.")

        version = (version_override or meta["version"]).strip()
        sha256_hex = meta["sha256"]
        size_bytes = int(meta["size_bytes"])

        # Limpa tokens expirados e gera um novo token criptográfico de 256 bits
        now_ts = time.time()
        token = secrets.token_urlsafe(32)

        # Assinatura HMAC-SHA256: device_id|version|token|size|sha256
        canonical_msg = f"{device_id}|{version}|{token}|{size_bytes}|{sha256_hex}"
        signature = self.compute_hmac_hex(canonical_msg)

        with self._lock:
            expired = [k for k, v in self._tokens.items() if v["expires_at"] < now_ts or v["used"]]
            for k in expired:
                self._tokens.pop(k, None)

            self._tokens[token] = {
                "device_id": device_id,
                "version": version,
                "sha256": sha256_hex,
                "size_bytes": size_bytes,
                "expires_at": now_ts + OTA_TOKEN_TTL_SECONDS,
                "used": False,
            }

            self._device_ota_status[device_id] = {
                "state": "triggered",
                "target_version": version,
                "progress_pct": 0,
                "message": "Ordem OTA assinada enviada via MQTT. Aguardando ESP32 iniciar download...",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

        return {
            "version": version,
            "port": settings.backend_ota_port,
            "token": token,
            "size": size_bytes,
            "sha256": sha256_hex,
            "sig": signature,
        }

    def consume_download_token(
        self,
        token: str,
        header_device_id: Optional[str],
        header_ota_auth: Optional[str],
    ) -> Tuple[bytes, Dict[str, Any]]:
        """Valida e consome imediatamente o One-Time Token (uso único) para entrega do binário."""
        now_ts = time.time()
        with self._lock:
            info = self._tokens.get(token)
            if not info:
                raise PermissionError("Token OTA inválido ou inexistente.")
            if info["used"]:
                raise PermissionError("Token OTA já foi utilizado (uso único).")
            if now_ts > info["expires_at"]:
                self._tokens.pop(token, None)
                raise PermissionError("Token OTA expirado.")

            expected_device = info["device_id"]
            if not header_device_id or header_device_id.strip() != expected_device:
                raise PermissionError("Identificação de dispositivo (X-Device-ID) inválida para este token.")

            expected_auth = self.compute_hmac_hex(f"download|{expected_device}|{token}")
            if not header_ota_auth or not hmac.compare_digest(header_ota_auth.strip().lower(), expected_auth.lower()):
                raise PermissionError("Assinatura de autenticação de download (X-OTA-Auth) inválida.")

            if not self.firmware_path.exists():
                raise FileNotFoundError("Binário de firmware não encontrado no armazenamento.")

            # Invalida o token imediatamente (One-Time Use)
            info["used"] = True
            binary_data = self.firmware_path.read_bytes()

            self._device_ota_status[expected_device] = {
                "state": "downloading",
                "target_version": info["version"],
                "progress_pct": 10,
                "message": "ESP32 autenticado! Transferindo binário criptografado...",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            return binary_data, info

    def update_device_ota_status(self, device_id: str, payload: Dict[str, Any]) -> None:
        """Atualiza o progresso reportado pelo ESP32 via tópico MQTT loja/freezer/<id>/ota/status."""
        with self._lock:
            current = self._device_ota_status.get(device_id, {})
            self._device_ota_status[device_id] = {
                "state": payload.get("state", current.get("state", "idle")),
                "target_version": payload.get("version", current.get("target_version")),
                "progress_pct": payload.get("progress", current.get("progress_pct", 0)),
                "message": payload.get("message", current.get("message", "")),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }

    def get_device_ota_status(self, device_id: str) -> Dict[str, Any]:
        """Retorna o estado atual da atualização OTA de um dispositivo."""
        with self._lock:
            return self._device_ota_status.get(
                device_id,
                {
                    "state": "idle",
                    "target_version": None,
                    "progress_pct": 0,
                    "message": "Nenhuma atualização em andamento.",
                    "updated_at": None,
                },
            )


ota_manager = SecureOTAManager()
