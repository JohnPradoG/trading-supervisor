"""Canal de Telegram: sendMessage de la Bot API con la biblioteca estándar (urllib).

- Desactivado limpiamente si faltan SUPERVISOR_TELEGRAM_BOT_TOKEN o SUPERVISOR_TELEGRAM_CHAT_ID.
- El token va en la URL de la API: nunca se registra. Todo texto de error pasa por `redact`
  antes de guardarse o escribirse en el log.
- Mensajes cortos en HTML (parse_mode=HTML) con todo el texto variable escapado.
- Solo envía avisos a un chat: no recibe órdenes ni actúa sobre MT5.
"""

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from supervisor.config import Settings

API_URL = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 4000  # Telegram admite 4096 caracteres por mensaje
SEVERITY_EMOJI = {"CRITICAL": "🚨", "WARNING": "⚠️", "INFO": "ℹ️"}
RESOLVED_EMOJI = "✅"
REDACTED = "<token oculto>"

Opener = Callable[..., Any]


class TelegramError(Exception):
    """Fallo al enviar. `retry_after`: segundos que pide Telegram (HTTP 429). `permanent`: no
    tiene sentido reintentar (p. ej. chat_id o token incorrectos)."""

    def __init__(self, message: str, retry_after: float | None = None, permanent: bool = False):
        super().__init__(message)
        self.message = message
        self.retry_after = retry_after
        self.permanent = permanent


def redact(text: str, token: str | None) -> str:
    """Quita el token (y la parte bot<token> de las URLs) de un texto."""
    if token:
        text = text.replace(token, REDACTED)
        text = text.replace(urllib.parse.quote(token, safe=""), REDACTED)
    return text


class TelegramClient:
    def __init__(
        self,
        token: str,
        chat_id: str,
        timeout: float = 10,
        opener: Opener | None = None,
        base_url: str = API_URL,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._base_url = base_url

    @classmethod
    def from_settings(cls, settings: Settings, opener: Opener | None = None) -> "TelegramClient":
        if not settings.telegram_enabled:
            raise TelegramError(
                "Telegram no está configurado (faltan SUPERVISOR_TELEGRAM_BOT_TOKEN o "
                "SUPERVISOR_TELEGRAM_CHAT_ID)",
                permanent=True,
            )
        return cls(
            settings.telegram_bot_token.get_secret_value().strip(),
            settings.telegram_chat_id.strip(),
            settings.telegram_timeout_seconds,
            opener,
        )

    def redact(self, text: str) -> str:
        return redact(text, self._token)

    def send_message(self, text: str) -> dict[str, Any]:
        """Envía un mensaje HTML. Lanza TelegramError (ya sin el token) si falla."""
        body = urllib.parse.urlencode(
            {
                "chat_id": self.chat_id,
                "text": text[:MAX_TEXT],
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            }
        ).encode()
        url = self._base_url.format(token=self._token, method="sendMessage")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with self._open(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise TelegramError(self.redact(f"sin conexión con Telegram: {exc}")) from None
        if not payload.get("ok"):
            description = str(payload.get("description") or "respuesta sin ok")
            raise TelegramError(self.redact(f"Telegram rechazó el mensaje: {description}"))
        return payload

    def _http_error(self, exc: urllib.error.HTTPError) -> TelegramError:
        description, retry_after = "", None
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
            description = str(payload.get("description") or "")
            retry_after = (payload.get("parameters") or {}).get("retry_after")
        except Exception:  # noqa: BLE001 - el cuerpo del error es opcional
            pass
        message = self.redact(f"HTTP {exc.code} de Telegram: {description or exc.reason}")
        # 400 (chat_id o HTML inválidos), 401 (token), 403 (bot bloqueado), 404 (token mal
        # escrito): reintentar no sirve. 429 y 5xx sí.
        permanent = exc.code in (400, 401, 403, 404)
        return TelegramError(message, retry_after=retry_after, permanent=permanent)


def esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def format_alert(
    *,
    severity: str,
    event: str,
    title: str,
    lines: list[str],
    link: str | None,
) -> str:
    """Mensaje corto: emoji por gravedad, título en negrita, líneas y enlace al dashboard."""
    emoji = RESOLVED_EMOJI if event == "RESUELTA" else SEVERITY_EMOJI.get(severity, "ℹ️")
    head = f"{emoji} <b>{esc(title)}</b>"
    body = [esc(line) for line in lines if line]
    if link:
        body.append(f'<a href="{html.escape(link, quote=True)}">Abrir en el dashboard</a>')
    return "\n".join([head, *body])
