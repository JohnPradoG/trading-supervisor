"""Resultados posibles al procesar un evento crudo."""

from dataclasses import dataclass

from supervisor.models.enums import RawEventStatus

# Prefijos de `raw_events.error` que marcan eventos que esperan a su apertura. El worker los
# reactiva cuando se crea la operación de esa posición.
WAITING_PREFIX = "en espera: "
ORPHAN_PREFIX = "huérfano: "


@dataclass(frozen=True)
class Outcome:
    status: RawEventStatus
    note: str | None = None


PROCESSED = Outcome(RawEventStatus.PROCESSED)


class Deferred(Exception):
    """El evento depende de otro que aún no ha llegado (p. ej. el deal de apertura).

    Se deja PENDING y se reintenta más tarde. Si se supera el plazo de espera, el evento pasa
    a `expire_status` (FAILED para deals, IGNORED para modificaciones)."""

    def __init__(self, reason: str, expire_status: RawEventStatus = RawEventStatus.FAILED):
        super().__init__(reason)
        self.reason = reason
        self.expire_status = expire_status


class ProcessingError(Exception):
    """Error determinista: el evento no se puede aplicar tal como está."""
