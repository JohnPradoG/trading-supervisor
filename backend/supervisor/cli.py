"""Administración desde la línea de comandos (en el VPS):

    alias ts='docker compose exec api supervisor-cli'
    ts create-account --broker Exness --server Exness-MT5Real8 --login 12345678 \\
        --currency USD --type REAL --margin-mode HEDGING
    ts create-terminal --name exness-win-01 --server Exness-MT5Real8 --login 12345678
    ts create-api-key --terminal exness-win-01
    ts revoke-api-key --prefix 1a2b3c4d5e6f
    ts list-terminals
    ts trade-summary
    ts reprocess [--terminal exness-win-01]

Las cuentas y las API keys se crean aquí y no por HTTP: así una API key comprometida o un
token de administración filtrado no permiten fabricar credenciales nuevas.
"""

import argparse
import sys
from datetime import UTC, datetime

from sqlalchemy import select, update

from supervisor.db import session_scope
from supervisor.models import Account, ApiClient, Broker, RawEvent, Terminal
from supervisor.models.enums import AccountType, MarginMode, RawEventStatus
from supervisor.security.api_keys import generate_api_key
from supervisor.services.trades import summary


def _account(session, server: str, login: int) -> Account:
    account = session.scalar(
        select(Account).where(Account.server == server, Account.login == login)
    )
    if account is None:
        sys.exit(f"No existe la cuenta {login} en {server}. Créala con create-account.")
    return account


def create_account(args: argparse.Namespace) -> None:
    with session_scope() as session:
        broker = session.scalar(select(Broker).where(Broker.name == args.broker))
        if broker is None:
            broker = Broker(name=args.broker)
            session.add(broker)
            session.flush()
            print(f"Broker creado: {broker.name}")
        exists = session.scalar(
            select(Account).where(Account.server == args.server, Account.login == args.login)
        )
        if exists:
            sys.exit(f"La cuenta {args.login} en {args.server} ya existe.")
        account = Account(
            broker_id=broker.id,
            login=args.login,
            server=args.server,
            currency=args.currency.upper(),
            account_type=AccountType(args.type),
            margin_mode=MarginMode(args.margin_mode),
            leverage=args.leverage,
        )
        session.add(account)
        session.flush()
        print(f"Cuenta creada: {account.login} @ {account.server} ({account.id})")


def create_terminal(args: argparse.Namespace) -> None:
    with session_scope() as session:
        account = _account(session, args.server, args.login)
        terminal = Terminal(
            account_id=account.id, name=args.name, host_description=args.description
        )
        session.add(terminal)
        session.flush()
        print(f"Terminal creado: {terminal.name} ({terminal.id})")


def create_api_key(args: argparse.Namespace) -> None:
    with session_scope() as session:
        terminal = session.scalar(select(Terminal).where(Terminal.name == args.terminal))
        if terminal is None:
            sys.exit(f"No existe el terminal '{args.terminal}'.")
        key = generate_api_key()
        session.add(
            ApiClient(terminal_id=terminal.id, key_prefix=key.prefix, key_hash=key.key_hash)
        )
    print("API key creada. Cópiala ahora: no se vuelve a mostrar y solo se guarda su hash.\n")
    print(f"    {key.plaintext}\n")
    print("Pégala en el input ApiKey del EA Monitor en ese terminal.")


def revoke_api_key(args: argparse.Namespace) -> None:
    with session_scope() as session:
        client = session.scalar(select(ApiClient).where(ApiClient.key_prefix == args.prefix))
        if client is None:
            sys.exit(f"No existe ninguna key con prefijo {args.prefix}.")
        if client.revoked_at is not None:
            sys.exit("Esa key ya estaba revocada.")
        client.revoked_at = datetime.now(UTC)
    print(f"Key {args.prefix} revocada.")


def list_terminals(_: argparse.Namespace) -> None:
    with session_scope() as session:
        rows = session.execute(
            select(Terminal, Account).join(Account).order_by(Terminal.name)
        ).all()
        if not rows:
            print("No hay terminales registrados.")
        for terminal, account in rows:
            keys = session.scalars(
                select(ApiClient).where(ApiClient.terminal_id == terminal.id)
            ).all()
            active = [k.key_prefix for k in keys if k.revoked_at is None]
            seen = terminal.last_seen_at.isoformat() if terminal.last_seen_at else "nunca"
            print(
                f"{terminal.name}: cuenta {account.login} @ {account.server}, "
                f"última conexión {seen}, keys activas {active or 'ninguna'}"
            )


def reprocess(args: argparse.Namespace) -> None:
    """Devuelve a PENDING los eventos FAILED para que el worker los intente de nuevo (p. ej.
    tras corregir un despliegue o completar un backfill). attempts conserva el historial."""
    with session_scope() as session:
        stmt = update(RawEvent).where(RawEvent.status == RawEventStatus.FAILED)
        if args.terminal:
            terminal = session.scalar(select(Terminal).where(Terminal.name == args.terminal))
            if terminal is None:
                sys.exit(f"No existe el terminal '{args.terminal}'.")
            stmt = stmt.where(RawEvent.terminal_id == terminal.id)
        result = session.execute(
            stmt.values(status=RawEventStatus.PENDING, next_attempt_at=None).execution_options(
                synchronize_session=False
            )
        )
        count = result.rowcount
    scope = f" del terminal {args.terminal}" if args.terminal else ""
    print(f"{count} eventos FAILED{scope} devueltos a PENDING; el worker los procesará.")


def trade_summary(_: argparse.Namespace) -> None:
    with session_scope() as session:
        info = summary(session)
        trades = info.trades_by_status
        print(
            f"Operaciones: {trades['OPEN']} abiertas, {trades['CLOSED']} cerradas, "
            f"{info.unassigned} sin despliegue asignado"
        )
        raw = ", ".join(f"{status} {count}" for status, count in info.raw_by_status.items())
        print(f"Eventos crudos: {raw}")
        last = info.last_processed
        if last is None:
            print("Último evento procesado: ninguno")
        else:
            print(
                f"Último evento procesado: #{last.id} {last.event_type} {last.status} "
                f"a las {last.processed_at.isoformat()}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="supervisor-cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-account", help="registrar broker y cuenta (sin contraseñas)")
    p.add_argument("--broker", required=True)
    p.add_argument("--server", required=True, help="servidor MT5, p. ej. Exness-MT5Real8")
    p.add_argument("--login", required=True, type=int)
    p.add_argument("--currency", required=True)
    p.add_argument("--type", required=True, choices=[t.value for t in AccountType])
    p.add_argument("--margin-mode", required=True, choices=[m.value for m in MarginMode])
    p.add_argument("--leverage", type=int)
    p.set_defaults(func=create_account)

    p = sub.add_parser("create-terminal", help="registrar un terminal MT5 de una cuenta")
    p.add_argument("--name", required=True)
    p.add_argument("--server", required=True)
    p.add_argument("--login", required=True, type=int)
    p.add_argument("--description")
    p.set_defaults(func=create_terminal)

    p = sub.add_parser("create-api-key", help="generar la API key de un terminal")
    p.add_argument("--terminal", required=True)
    p.set_defaults(func=create_api_key)

    p = sub.add_parser("revoke-api-key", help="revocar una API key por su prefijo")
    p.add_argument("--prefix", required=True)
    p.set_defaults(func=revoke_api_key)

    p = sub.add_parser("list-terminals", help="ver terminales, última conexión y keys")
    p.set_defaults(func=list_terminals)

    p = sub.add_parser("reprocess", help="devolver eventos FAILED a PENDING para reintentarlos")
    p.add_argument("--terminal", help="solo los de este terminal")
    p.set_defaults(func=reprocess)

    p = sub.add_parser("trade-summary", help="operaciones abiertas/cerradas y estado del worker")
    p.set_defaults(func=trade_summary)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
