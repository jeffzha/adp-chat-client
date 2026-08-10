from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from pathlib import Path

from jwcrypto import jwe, jwk
from jwcrypto.common import base64url_encode

from config import tagentic_config


class WorkbenchIntegrationCryptoError(RuntimeError):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _KeyRing:
    active_id: str
    keys: dict[str, bytes]
    typ: str


class WorkbenchIntegrationCrypto:
    """Independent compact JWE key rings for tokens and OAuth transaction data."""

    @staticmethod
    def _decode_key(encoded: object) -> bytes:
        if not isinstance(encoded, str):
            raise WorkbenchIntegrationCryptoError("integration encryption key is invalid")
        try:
            key = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError) as error:
            raise WorkbenchIntegrationCryptoError(
                "integration encryption key is invalid"
            ) from error
        if len(key) != 32:
            raise WorkbenchIntegrationCryptoError("integration encryption key is invalid")
        return key

    @classmethod
    def _ring(cls, purpose: str) -> _KeyRing:
        if purpose == "connector-token":
            active_id = str(tagentic_config.WORKBENCH_CONNECTOR_TOKEN_KEY_ID or "").strip()
            active_key = tagentic_config.WORKBENCH_CONNECTOR_TOKEN_KEY
            active_file = tagentic_config.WORKBENCH_CONNECTOR_TOKEN_KEY_FILE
            previous_json = tagentic_config.WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_JSON
            previous_file = tagentic_config.WORKBENCH_CONNECTOR_TOKEN_PREVIOUS_KEYS_FILE
            typ = "workbench-connector-token+jwe"
        elif purpose == "oauth-state":
            active_id = str(tagentic_config.WORKBENCH_OAUTH_STATE_KEY_ID or "").strip()
            active_key = tagentic_config.WORKBENCH_OAUTH_STATE_KEY
            active_file = tagentic_config.WORKBENCH_OAUTH_STATE_KEY_FILE
            previous_json = tagentic_config.WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_JSON
            previous_file = tagentic_config.WORKBENCH_OAUTH_STATE_PREVIOUS_KEYS_FILE
            typ = "workbench-oauth-state+jwe"
        else:
            raise WorkbenchIntegrationCryptoError("integration encryption purpose is invalid")
        if not active_id or len(active_id) > 64:
            raise WorkbenchIntegrationCryptoError("integration encryption key is not configured")
        active_key = cls._mounted_value(
            active_key,
            active_file,
            default_inline="",
            maximum_bytes=4096,
        )
        previous_json = cls._mounted_value(
            previous_json,
            previous_file,
            default_inline="{}",
            maximum_bytes=64 * 1024,
        )
        try:
            previous = json.loads(str(previous_json or "{}"))
        except (TypeError, ValueError) as error:
            raise WorkbenchIntegrationCryptoError(
                "integration previous encryption keys are invalid"
            ) from error
        if not isinstance(previous, dict) or len(previous) > 16:
            raise WorkbenchIntegrationCryptoError(
                "integration previous encryption keys are invalid"
            )
        keys: dict[str, bytes] = {active_id: cls._decode_key(active_key)}
        for key_id, encoded in previous.items():
            if (
                not isinstance(key_id, str)
                or not key_id
                or len(key_id) > 64
                or key_id == active_id
            ):
                raise WorkbenchIntegrationCryptoError(
                    "integration previous encryption keys are invalid"
                )
            keys[key_id] = cls._decode_key(encoded)
        return _KeyRing(active_id, keys, typ)

    @staticmethod
    def _mounted_value(
        inline: object,
        file_name: object,
        *,
        default_inline: str,
        maximum_bytes: int,
    ) -> str:
        inline_text = str(inline or "").strip()
        path_text = str(file_name or "").strip()
        if not path_text:
            return inline_text
        if inline_text not in {"", default_inline}:
            raise WorkbenchIntegrationCryptoError(
                "integration encryption inline and file settings are mutually exclusive"
            )
        try:
            path = Path(path_text).resolve(strict=True)
            if not path.is_file() or path.stat().st_size > maximum_bytes:
                raise OSError("invalid mounted key file")
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise WorkbenchIntegrationCryptoError(
                "integration encryption key file is invalid"
            ) from error

    @classmethod
    def encrypt(cls, purpose: str, plaintext: bytes) -> tuple[str, str]:
        ring = cls._ring(purpose)
        key = jwk.JWK(
            kty="oct",
            k=base64url_encode(ring.keys[ring.active_id]),
            kid=ring.active_id,
        )
        token = jwe.JWE(
            plaintext,
            protected={
                "alg": "A256KW",
                "enc": "A256GCM",
                "kid": ring.active_id,
                "typ": ring.typ,
            },
        )
        token.add_recipient(key)
        return token.serialize(compact=True), ring.active_id

    @classmethod
    def decrypt(cls, purpose: str, ciphertext: str) -> bytes:
        ring = cls._ring(purpose)
        token = jwe.JWE()
        try:
            token.deserialize(ciphertext)
            header = token.jose_header
            key_id = str(header.get("kid") or "")
            if header.get("typ") != ring.typ or key_id not in ring.keys:
                raise ValueError("unexpected JWE header")
            key = jwk.JWK(
                kty="oct",
                k=base64url_encode(ring.keys[key_id]),
                kid=key_id,
            )
            token.decrypt(key)
            return bytes(token.payload)
        except Exception as error:
            raise WorkbenchIntegrationCryptoError(
                "integration encrypted value cannot be decrypted", 409
            ) from error
