"""Autenticación del dashboard: sesión en cookie firmada, CSRF y límite de intentos.

Sesión
    Cookie `ts_session` = base64url(JSON) + "." + base64url(HMAC-SHA256). El JSON lleva la
    caducidad (`exp`, por defecto 12 h) y un token CSRF aleatorio. La clave HMAC se deriva del
    SUPERVISOR_ADMIN_TOKEN (HMAC del token con una etiqueta fija), así que no hay otro secreto
    que guardar y rotar el token invalida todas las sesiones. La firma se compara en tiempo
    constante. La cookie es HttpOnly, SameSite=Strict, Path=/dashboard y Secure (configurable
    solo para pruebas). Es una sesión sin estado en el servidor: "Salir" borra la cookie del
    navegador, pero una cookie robada vale hasta su caducidad; para cortarlas todas, rotar el
    token de administración.

CSRF (todas las peticiones POST, incluidos login y logout)
    1. SameSite=Strict: el navegador no envía la cookie de sesión desde otros sitios.
    2. Token sincronizado: cada formulario lleva `csrf_token`, que debe coincidir (en tiempo
       constante) con el de la sesión firmada. Antes de iniciar sesión se usa una cookie
       aparte (`ts_login_csrf`, doble envío).
    3. Origen: si el navegador envía Sec-Fetch-Site debe ser `same-origin`; si envía Origin
       (distinto de "null") debe coincidir con el host de la petición.

Límite de intentos
    Fallos de login por IP en una ventana deslizante, en memoria. Es por proceso: con 2
    workers de uvicorn el límite efectivo es el doble. Suficiente para frenar fuerza bruta
    contra un token de 256 bits; no sustituye a un WAF.
"""

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass

from fastapi import Request

from supervisor.config import Settings

SESSION_COOKIE = "ts_session"
LOGIN_CSRF_COOKIE = "ts_login_csrf"
COOKIE_PATH = "/dashboard"
_KEY_LABEL = b"trading-supervisor/dashboard-session/v1"


def _b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def session_key(settings: Settings) -> bytes:
    token = settings.admin_token.get_secret_value().encode("utf-8")
    return hmac.new(token, _KEY_LABEL, hashlib.sha256).digest()


@dataclass(frozen=True)
class DashboardSession:
    expires_at: int
    csrf_token: str


def create_session_cookie(settings: Settings, now: float | None = None) -> tuple[str, int]:
    """Devuelve (valor de la cookie, segundos de vida)."""
    now = time.time() if now is None else now
    max_age = int(settings.dashboard_session_hours * 3600)
    payload = {"v": 1, "iat": int(now), "exp": int(now) + max_age, "csrf": secrets.token_hex(16)}
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = hmac.new(session_key(settings), body.encode("ascii"), hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}", max_age


def read_session_cookie(
    settings: Settings, value: str | None, now: float | None = None
) -> DashboardSession | None:
    """Sesión válida o None (sin cookie, firma incorrecta, formato inválido o caducada)."""
    if not value or len(value) > 1024 or value.count(".") != 1:
        return None
    body, sig = value.split(".")
    expected = hmac.new(session_key(settings), body.encode("ascii", "ignore"), hashlib.sha256)
    try:
        given = _b64d(sig)
    except ValueError:
        return None
    if not hmac.compare_digest(expected.digest(), given):
        return None
    try:
        payload = json.loads(_b64d(body))
        exp = int(payload["exp"])
        csrf = str(payload["csrf"])
    except (ValueError, KeyError, TypeError):
        return None
    now = time.time() if now is None else now
    if exp <= now or not csrf:
        return None
    return DashboardSession(expires_at=exp, csrf_token=csrf)


def current_session(request: Request) -> DashboardSession | None:
    settings: Settings = request.app.state.settings
    return read_session_cookie(settings, request.cookies.get(SESSION_COOKIE))


def same_origin(request: Request) -> bool:
    """Comprobación de origen para POST (complementa SameSite=Strict y el token CSRF)."""
    site = request.headers.get("sec-fetch-site")
    if site is not None and site != "same-origin":
        return False
    origin = request.headers.get("origin")
    if origin and origin != "null":
        return origin == f"{request.url.scheme}://{request.url.netloc}"
    return True


def tokens_match(expected: str | None, given: str | None) -> bool:
    if not expected or not given:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), given.encode("utf-8"))


def admin_token_ok(settings: Settings, given: str) -> bool:
    expected = settings.admin_token.get_secret_value()
    return hmac.compare_digest(expected.encode("utf-8"), given.strip().encode("utf-8"))


def cookie_options(settings: Settings) -> dict:
    return {
        "path": COOKIE_PATH,
        "httponly": True,
        "secure": settings.dashboard_cookie_secure,
        "samesite": "strict",
    }


class LoginRateLimiter:
    """Fallos de login por IP en una ventana deslizante. En memoria y por proceso."""

    MAX_TRACKED_IPS = 10_000

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures.get(key)
        if attempts is None:
            return deque()
        while attempts and attempts[0] <= now - self.window:
            attempts.popleft()
        if not attempts:
            del self._failures[key]
        return attempts

    def blocked(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, now)) >= self.max_failures

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            if key not in self._failures and len(self._failures) >= self.MAX_TRACKED_IPS:
                # Memoria acotada: se descartan las entradas más antiguas.
                for old in list(self._failures)[: self.MAX_TRACKED_IPS // 10]:
                    del self._failures[old]
            self._prune(key, now)
            self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)


def client_ip(request: Request) -> str:
    # Detrás de Caddy, uvicorn (--proxy-headers) ya pone aquí la IP real del cliente.
    return request.client.host if request.client else "desconocida"
