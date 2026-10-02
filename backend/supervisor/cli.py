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
    ts analyze --trade <trade_id> | --all-closed
    ts dna --trade <trade_id> | --all
    ts stats [--bot EA_Nasdaq_FVG_Retest] [--group-by version,session] [--from 2026-10-01]
    ts patterns --version <bot_version_id> [--symbol USTEC_x100] [--report-only]

Las cuentas y las API keys se crean aquí y no por HTTP: así una API key comprometida o un
token de administración filtrado no permiten fabricar credenciales nuevas.
"""

import argparse
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from supervisor.analytics import pattern_search
from supervisor.analytics.analyzer import analyze_by_id, closed_trade_ids
from supervisor.analytics.dna import dna_by_id, trade_ids_page
from supervisor.config import get_settings
from supervisor.db import session_scope
from supervisor.models import Account, ApiClient, Bot, BotVersion, Broker, RawEvent, Terminal
from supervisor.models.enums import AccountType, MarginMode, RawEventStatus, TradeSource
from supervisor.security.api_keys import generate_api_key
from supervisor.services import dna as dna_service
from supervisor.services import patterns as patterns_service
from supervisor.services import stats as stats_service
from supervisor.services.errors import ServiceError
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


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"no es un UUID: {value}") from exc


def _date(value: str) -> datetime:
    """Fecha u hora ISO; sin zona se interpreta como UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"fecha no válida: {value}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


ANALYZE_PAGE = 200


def analyze(args: argparse.Namespace) -> None:
    """Re-analiza operaciones cerradas. Solo crea versión nueva si cambiaron sus entradas o la
    versión del analizador: repetirlo no duplica nada."""
    settings = get_settings()
    if args.trade:
        with session_scope() as session:
            try:
                result = analyze_by_id(session, args.trade, settings)
            except LookupError as exc:
                sys.exit(str(exc))
            if result.status == "no_cerrada":
                sys.exit("La operación sigue abierta: se analiza al cerrarse.")
            a = result.analysis
            facts = sum(1 for f in a.findings if f.kind.value == "FACT")
            hyps = len(a.findings) - facts
            verb = "creada" if result.status == "creado" else "sin cambios, vigente"
            print(
                f"Análisis v{a.analysis_version} {verb}: {a.outcome.value}, {facts} hechos, "
                f"{hyps} hipótesis, {len(a.data_quality)} notas de calidad"
            )
        return
    totals: Counter = Counter()
    after = None
    while True:
        with session_scope() as session:
            page = closed_trade_ids(session, after, ANALYZE_PAGE)
            for _, trade_id in page:
                try:
                    with session.begin_nested():
                        totals[analyze_by_id(session, trade_id, settings).status] += 1
                except Exception as exc:  # una operación con datos raros no para el resto
                    totals["error"] += 1
                    print(f"error en {trade_id}: {exc}", file=sys.stderr)
        if len(page) < ANALYZE_PAGE:
            break
        after = page[-1]
    print(
        f"Operaciones cerradas: {sum(totals.values())}. Versiones nuevas: {totals['creado']}, "
        f"sin cambios: {totals['sin_cambios']}, errores: {totals['error']}"
    )


def _dna_value(value: Any) -> str:
    if isinstance(value, bool):
        return "sí" if value else "no"
    if isinstance(value, float):
        return f"{value:.4g}"
    return str(value)


def print_dna(dna) -> None:
    print(
        f"DNA v{dna.dna_version} (variables v{dna.feature_set_version}), datos anteriores a "
        f"{dna.data_cutoff.isoformat()} · última vela M1 "
        f"{dna.last_bar_time.isoformat() if dna.last_bar_time else 'ninguna'}"
    )
    for section in dna_service.sections(dna):
        print(f"\n[{section['title']}]")
        for f in section["features"]:
            if f["value"] is None:
                shown = f"sin dato ({f['null_reason']})"
            else:
                shown = _dna_value(f["value"]) + (f" {f['unit']}" if f["unit"] else "")
            print(f"  {f['label']}: {shown}")


def dna(args: argparse.Namespace) -> None:
    """Calcula el Trading DNA. Solo crea versión nueva si cambiaron sus entradas (velas,
    operación, parámetros o conjunto de variables): repetirlo no duplica nada."""
    settings = get_settings()
    if args.trade:
        with session_scope() as session:
            try:
                result = dna_by_id(session, args.trade, settings)
            except LookupError as exc:
                sys.exit(str(exc))
            verb = "creada" if result.status == "creado" else "sin cambios, vigente"
            nulls = sum(1 for v in result.dna.features.values() if v is None)
            print(
                f"Versión {result.dna.dna_version} del DNA {verb}: "
                f"{len(result.dna.features)} variables, {nulls} sin dato"
            )
            print_dna(result.dna)
        return
    totals: Counter = Counter()
    after = None
    while True:
        with session_scope() as session:
            page = trade_ids_page(session, after, ANALYZE_PAGE)
            for _, trade_id in page:
                try:
                    with session.begin_nested():
                        totals[dna_by_id(session, trade_id, settings).status] += 1
                except Exception as exc:  # una operación con datos raros no para el resto
                    totals["error"] += 1
                    print(f"error en {trade_id}: {exc}", file=sys.stderr)
        if len(page) < ANALYZE_PAGE:
            break
        after = page[-1]
    print(
        f"Operaciones: {sum(totals.values())}. Versiones nuevas del DNA: {totals['creado']}, "
        f"sin cambios: {totals['sin_cambios']}, errores: {totals['error']}"
    )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


def _num(value: float | None, places: int = 2) -> str:
    return "-" if value is None else f"{value:.{places}f}"


def _ci(interval: dict | None, pct: bool = False) -> str:
    if not interval:
        return ""
    fmt = _pct if pct else _num
    return f" [{fmt(interval['low'])}, {fmt(interval['high'])}]"


def _pf(m: dict[str, Any]) -> str:
    if m["profit_factor"] is not None:
        return _num(m["profit_factor"])
    return "inf" if "infinito" in (m["profit_factor_note"] or "") else "-"


def _streaks(m: dict[str, Any]) -> str:
    if m["max_consecutive_wins"] is None:
        return "-"
    return f"{m['max_consecutive_wins']}/{m['max_consecutive_losses']}"


STATS_HEADER = (
    "Grupo",
    "n",
    "Win% [IC95]",
    "PF",
    "Expect.",
    "Exp. R [IC95]",
    "Max DD",
    "Rachas G/P",
    "Sharpe",
    "Aviso",
)


def stats_table(rows: list[tuple[str, dict[str, Any]]]) -> str:
    """Tabla legible de métricas (una fila por grupo)."""
    header = STATS_HEADER
    lines = [header]
    for label, m in rows:
        lines.append(
            (
                label,
                str(m["n_trades"]),
                _pct(m["win_rate"]) + _ci(m["win_rate_ci95"], pct=True),
                _pf(m),
                _num(m["expectancy"]),
                _num(m["expectancy_r"]) + _ci(m["expectancy_r_ci95"]),
                _num(m["max_drawdown"]),
                _streaks(m),
                _num(m["sharpe"]),
                "muestra pequeña" if m["sample_warning"] else "",
            )
        )
    widths = [max(len(row[i]) for row in lines) for i in range(len(header))]
    out = []
    for n, row in enumerate(lines):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


def stats(args: argparse.Namespace) -> None:
    settings = get_settings()
    with session_scope() as session:
        bot_id = None
        if args.bot:
            try:
                bot_id = uuid.UUID(args.bot)
            except ValueError:
                bot = session.scalar(select(Bot).where(Bot.name == args.bot))
                if bot is None:
                    sys.exit(f"No existe el bot '{args.bot}'.")
                bot_id = bot.bot_id
        filters = stats_service.StatsFilters(
            bot_id=bot_id,
            version_id=args.version_id,
            symbol=args.symbol,
            account_id=args.account_id,
            source=TradeSource(args.source) if args.source else None,
            date_from=args.date_from,
            date_to=args.date_to,
        )
        try:
            dims = stats_service.parse_group_by(args.group_by)
            result = stats_service.stats(session, settings, filters, dims)
        except ServiceError as exc:
            sys.exit(exc.message)
    rows = [("TOTAL (con bot)", result["total"])]
    rows += [(g["label"], g["metrics"]) for g in result["groups"]]
    if result["unassigned"] is not None and result["unassigned"]["n_trades"]:
        rows.append(("SIN ASIGNAR (aparte)", result["unassigned"]))
    print(stats_table(rows))
    print(
        f"\nSolo operaciones cerradas; fechas por hora de cierre. Breakeven: |neto| <= "
        f"{settings.breakeven_r_fraction} R. Sharpe por operación (base R) solo con n >= "
        f"{settings.stats_min_sample}. IC95: Wilson (win rate) y t de Student (expectancy)."
    )


def _segment_line(name: str, seg: dict[str, Any] | None) -> str:
    if not seg:
        return f"    {name}: -"
    ci = seg.get("expectancy_r_ic95")
    ci_text = f" [{_num(ci[0])}, {_num(ci[1])}]" if ci else ""
    wr_ci = seg.get("win_rate_ic95")
    wr_text = f" [{_pct(wr_ci[0])}, {_pct(wr_ci[1])}]" if wr_ci else ""
    pf = seg.get("profit_factor_r")
    line = (
        f"    {name}: n={seg['n']}  win rate {_pct(seg.get('win_rate'))}{wr_text}  "
        f"expectativa {_num(seg.get('expectancy_r'))} R{ci_text}  PF {_num(pf)}"
    )
    if seg.get("p_ajustado") is not None:
        line += f"  p ajustado {seg['p_ajustado']:.4f} ({seg.get('pruebas_en_familia')} pruebas)"
    if seg.get("motivo") and name != "entrenamiento":
        line += f"\n      {seg['motivo']}"
    return line


def _print_pattern(item: dict[str, Any]) -> None:
    print(f"  [{item['label'].upper()}] {item['statement']}")
    if item.get("status_note"):
        print(f"    estado: {item['status_note']}")
    print(_segment_line("entrenamiento", item.get("entrenamiento")))
    print(_segment_line("validación", item.get("validacion")))
    print(_segment_line("fuera de muestra", item.get("fuera_de_muestra")))
    if item.get("forward"):
        print(_segment_line("forward", item["forward"]))


def print_report(report: dict[str, Any]) -> None:
    r = report["resumen"]
    scope = f"{report['bot_name']} {report['version']}" + (
        f" ({report['symbol']})" if report["symbol"] else ""
    )
    print(f"\nInforme de {scope}")
    if r["aviso"]:
        print(f"  {r['aviso']}")
    print(
        f"  Trampas validadas: {r['trampas_validadas']}  candidatas (no validadas): "
        f"{r['trampas_candidatas']}  ventajas validadas: {r['ventajas_validadas']}  "
        f"rechazadas: {r['rechazadas']}  caducadas: {r['caducadas']}"
    )
    print(
        f"  Candidatas probadas: {r['candidatas_probadas_ultima']} en la última búsqueda, "
        f"{r['candidatas_probadas_total']} en total"
    )
    wl = report["ganadoras_vs_perdedoras"]
    print("\nGanadoras frente a perdedoras (descriptivo, no validado)")
    for name in ("ganadoras", "perdedoras"):
        g = wl[name]
        print(f"  {name}: n={g['n']}  R medio {_num(g['r_medio'])}  MAE {_num(g['mae_r_medio'])} R")
    for d in wl["diferencias"]:
        print(
            f"  {d['condicion']}: {_pct(d['en_ganadoras'])} de las ganadoras, "
            f"{_pct(d['en_perdedoras'])} de las perdedoras (n={d['n']})"
        )
    for title, key in (
        ("Peores condiciones (trampas)", "peores_condiciones"),
        ("Mejores condiciones (ventajas)", "mejores_condiciones"),
    ):
        print(f"\n{title}")
        if not report[key]:
            print("  ninguna")
        for item in report[key]:
            _print_pattern(item)
    print("\nMejor combinación")
    if report["mejor_combinacion"]:
        _print_pattern(report["mejor_combinacion"])
    else:
        print("  ninguna")
    print("\nCondiciones que aumentan el drawdown (MAE en R; descriptivo, no validado)")
    if not report["aumentan_drawdown"]:
        print("  ninguna")
    for d in report["aumentan_drawdown"]:
        tr, oos = d["entrenamiento"], d["fuera_de_muestra"]
        oos_text = (
            f"fuera de muestra MAE {_num(oos['mae_r'])} R frente a "
            f"{_num(oos['mae_r_complemento'])} R (n={oos['n']})"
            if oos
            else "fuera de muestra: sin datos"
        )
        print(
            f"  {d['condicion']}: MAE {_num(tr['mae_r'])} R frente a "
            f"{_num(tr['mae_r_complemento'])} R en entrenamiento (n={tr['n']}); {oos_text}"
        )
    print(f"\n{report['nota']}")


def patterns(args: argparse.Namespace) -> None:
    """Ejecuta la búsqueda de patrones de una versión (idempotente: sin operaciones nuevas no
    repite nada) y muestra el informe."""
    settings = get_settings()
    with session_scope() as session:
        version = session.get(BotVersion, args.version)
        if version is None:
            sys.exit(f"No existe la versión {args.version}.")
        if not args.report_only:
            result = pattern_search.run_patterns(session, settings, version.id, args.symbol)
            print(f"Búsqueda: {result.status}. {result.message}")
        report = patterns_service.version_report(
            session, settings, version.bot_id, version.id, args.symbol
        )
    print_report(report)


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

    p = sub.add_parser("analyze", help="re-analizar operaciones cerradas (versiones nuevas)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--trade", type=_uuid, help="trade_id de una operación")
    target.add_argument("--all-closed", action="store_true", help="todas las cerradas")
    p.set_defaults(func=analyze)

    p = sub.add_parser("dna", help="calcular el Trading DNA (versiones nuevas si cambió algo)")
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--trade", type=_uuid, help="trade_id de una operación")
    target.add_argument("--all", action="store_true", help="todas las operaciones")
    p.set_defaults(func=dna)

    p = sub.add_parser("stats", help="estadísticas de operaciones cerradas")
    p.add_argument("--bot", help="nombre o bot_id")
    p.add_argument("--version-id", type=_uuid)
    p.add_argument("--symbol")
    p.add_argument("--account-id", type=_uuid)
    p.add_argument("--source", choices=[s.value for s in TradeSource])
    p.add_argument("--from", dest="date_from", type=_date, help="hora de cierre desde (UTC)")
    p.add_argument("--to", dest="date_to", type=_date, help="hora de cierre hasta (UTC)")
    p.add_argument(
        "--group-by",
        help="hasta 2 de: " + ", ".join(stats_service.GROUP_DIMENSIONS) + ", dna:<variable>",
    )
    p.set_defaults(func=stats)

    p = sub.add_parser("patterns", help="buscar y validar trampas de una versión de bot")
    p.add_argument("--version", required=True, type=_uuid, help="bot_version_id")
    p.add_argument("--symbol", help="solo este símbolo (por defecto, todos juntos)")
    p.add_argument(
        "--report-only", action="store_true", help="solo el informe, sin ejecutar la búsqueda"
    )
    p.set_defaults(func=patterns)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
