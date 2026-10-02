"""Dependencias de FastAPI: sesión de base de datos y autenticación."""

import hmac
import logging
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from supervisor.config import Settings
from supervisor.models import Account, ApiClient, Terminal
from supervisor.security.api_keys import parse_prefix, verify_key
from supervisor.services.ingest import TerminalContext, touch_terminal

log = logging.getLogger(__name__)


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_session(request: Request) -> Iterator[Session]:
    """Una transacción por petición: commit si la respuesta se genera bien, rollback si no."""
    session: Session = request.app.state.session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="API key inválida o revocada",
    headers={"WWW-Authenticate": "ApiKey"},
)


def terminal_auth(
    session: SessionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> TerminalContext:
    if not x_api_key:
        raise _UNAUTHORIZED
    prefix = parse_prefix(x_api_key)
    if prefix is None:
        raise _UNAUTHORIZED
    row = session.execute(
        select(ApiClient, Terminal, Account)
        .join(Terminal, ApiClient.terminal_id == Terminal.id)
        .join(Account, Terminal.account_id == Account.id)
        .where(ApiClient.key_prefix == prefix)
    ).first()
    if row is None:
        log.warning("api key desconocida", extra={"key_prefix": prefix})
        raise _UNAUTHORIZED
    client, terminal, account = row
    if client.revoked_at is not None or not verify_key(x_api_key, client.key_hash):
        log.warning("api key rechazada", extra={"key_prefix": prefix})
        raise _UNAUTHORIZED
    if not account.active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="la cuenta está desactivada")
    touch_terminal(session, terminal.id)
    return TerminalContext(
        api_client_id=client.id,
        terminal_id=terminal.id,
        terminal_name=terminal.name,
        account_id=account.id,
        account_login=account.login,
        broker_id=account.broker_id,
    )


_bearer = HTTPBearer(auto_error=False)


def admin_auth(
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> None:
    expected = settings.admin_token.get_secret_value()
    if credentials is None or not hmac.compare_digest(credentials.credentials, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="token de administración inválido",
            headers={"WWW-Authenticate": "Bearer"},
        )


TerminalDep = Annotated[TerminalContext, Depends(terminal_auth)]
