"""API keys de los terminales MT5.

Formato: ``tsk_<prefijo de 12 hex>_<secreto>``. Se guarda el prefijo (para encontrar la fila
sin revelar la key) y el SHA-256 de la key completa. Como el secreto tiene 256 bits de azar,
un hash rápido es suficiente: no hay diccionario posible que atacar.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass

KEY_SCHEME = "tsk"


@dataclass(frozen=True)
class NewApiKey:
    plaintext: str
    prefix: str
    key_hash: str


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


def generate_api_key() -> NewApiKey:
    prefix = secrets.token_hex(6)
    plaintext = f"{KEY_SCHEME}_{prefix}_{secrets.token_urlsafe(32)}"
    return NewApiKey(plaintext=plaintext, prefix=prefix, key_hash=hash_key(plaintext))


def parse_prefix(plaintext: str) -> str | None:
    parts = plaintext.split("_", 2)
    if len(parts) != 3 or parts[0] != KEY_SCHEME or len(parts[1]) != 12:
        return None
    return parts[1]


def verify_key(plaintext: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_key(plaintext), stored_hash)
