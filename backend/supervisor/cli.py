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
    ts backfill [--from 2026-01-01] [--page-size 200]
    ts experiment create --base <bot_version_id> --title "..." --change "..." [--hypothesis <id>]
    ts experiment run-filter --id 1
    ts experiment import-backtest --id 1 --file informe.csv   (o --file - por la entrada)
    ts experiment show --id 1 | ts experiment list
    ts alerts list [--kind TRAMPA_ACTIVA] [--unacknowledged] | ts alerts test
    ts telegram setup-help

Las cuentas y las API keys se crean aquí y no por HTTP: así una API key comprometida o un
token de administración filtrado no permiten fabricar credenciales nuevas.
"""

import argparse
import json
import sys
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update

from supervisor.alerts import telegram as tg
from supervisor.analytics import pattern_search
from supervisor.analytics.analyzer import analyze_by_id, closed_trade_ids
from supervisor.analytics.dna import dna_by_id, trade_ids_page
from supervisor.config import get_settings
from supervisor.db import session_scope
from supervisor.models import Account, ApiClient, Bot, BotVersion, Broker, RawEvent, Terminal
from supervisor.models.enums import AccountType, MarginMode, RawEventStatus, TradeSource
from supervisor.security.api_keys import generate_api_key
from supervisor.services import alerts as alerts_service
from supervisor.services import dna as dna_service
from supervisor.services import lab as lab_service
from supervisor.services import patterns as patterns_service
from supervisor.services import stats as stats_service
from supervisor.services.errors import ServiceError
from supervisor.services.trades import summary
from supervisor.worker.backfill import backfill_page


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


def backfill(args: argparse.Namespace) -> None:
    """Completa operaciones antiguas (tras importar el historial o registrar despliegues con
    fechas pasadas): despliegue, MFE/MAE, riesgo, Trading DNA y análisis. Por páginas, cada
    una en su transacción; repetirlo no duplica nada y salta lo que ya está hecho."""
    settings = get_settings()
    if (args.after_time is None) != (args.after_id is None):
        sys.exit("--after-time y --after-id van juntos (el cursor que imprime cada página).")
    after = (args.after_time, args.after_id) if args.after_time else None
    totals: Counter = Counter()
    pages = 0
    while True:
        with session_scope() as session:
            page = backfill_page(
                session, settings, after, args.page_size, args.date_from, args.date_to
            )
        pages += 1
        totals.update(page.stats)
        done = ", ".join(f"{k} {v}" for k, v in sorted(page.stats.items()) if v)
        cursor = (
            f" · seguir con --after-time {page.next_cursor[0].isoformat()} "
            f"--after-id {page.next_cursor[1]}"
            if page.next_cursor
            else ""
        )
        print(f"Página {pages}: {done or 'nada'}{cursor}", flush=True)
        if page.next_cursor is None or (args.max_pages and pages >= args.max_pages):
            break
        after = page.next_cursor
    print(
        f"Backfill: {totals['revisadas']} operaciones revisadas, {totals['asignadas']} "
        f"asignadas a un despliegue, {totals['excursiones']} MFE/MAE, {totals['riesgos']} "
        f"riesgos, {totals['dna']} DNA y {totals['analisis']} análisis nuevos, "
        f"{totals['errores']} errores."
    )
    if totals["asignadas"]:
        print("Hay operaciones nuevas en versiones de bot: ejecuta `patterns --version <id>`.")


LAB_HEADER = (
    "Brazo",
    "Tramo",
    "n",
    "Win% [IC95]",
    "PF [IC95]",
    "Exp. R [IC95]",
    "Neto",
    "Max DD [IC95]",
    "Aviso",
)


def _pair(values: list | None, places: int = 2) -> str:
    return f" [{_num(values[0], places)}, {_num(values[1], places)}]" if values else ""


def lab_table(rows: list[tuple[str, str, dict[str, Any]]]) -> str:
    """Tabla del laboratorio: un brazo y tramo por fila."""
    lines = [LAB_HEADER]
    for arm, segment, m in rows:
        lines.append(
            (
                arm,
                segment.replace("_", " "),
                str(m["n_trades"]),
                _pct(m["win_rate"]) + _ci(m["win_rate_ci95"], pct=True),
                _pf(m) + _pair(m.get("profit_factor_ic95")),
                _num(m["expectancy_r"]) + _ci(m["expectancy_r_ci95"]),
                _num(m["cumulative_return"]),
                _num(m["max_drawdown"]) + _pair(m.get("max_drawdown_ic95")),
                "muestra pequeña" if m["sample_warning"] else "",
            )
        )
    widths = [max(len(row[i]) for row in lines) for i in range(len(LAB_HEADER))]
    out = []
    for n, row in enumerate(lines):
        out.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if n == 0:
            out.append("  ".join("-" * w for w in widths))
    return "\n".join(out)


SEGMENT_ORDER = ("fuera_de_muestra", "dentro_de_muestra", "todo")


def print_filter_result(results: dict[str, Any]) -> None:
    print(f"Filtro: {results['filtro']['texto']}")
    pop = results["poblacion"]
    print(f"Operaciones reales de {pop['version']}: {pop['operaciones']}")
    print(f"Titular: {results['titular'].replace('_', ' ')}")
    rows = []
    for segment in SEGMENT_ORDER:
        seg = results["tramos"].get(segment)
        if seg is None:
            continue
        for arm in ("original", "filtrado", "evitadas"):
            rows.append((arm, segment, seg[arm]))
    print(lab_table(rows))
    for segment in SEGMENT_ORDER:
        seg = results["tramos"].get(segment)
        if seg and seg["evitadas_vs_mantenidas"]:
            w = seg["evitadas_vs_mantenidas"]
            print(
                f"{segment.replace('_', ' ')}: evitadas - mantenidas = {w['diferencia_r']:+.2f} R "
                f"[{w['ic95'][0]:+.2f}, {w['ic95'][1]:+.2f}], p={w['p_valor']:.4f}; "
                f"sin dato: {seg['sin_dato']}"
            )
    for warning in results["avisos"]:
        print(f"AVISO: {warning}")


def _experiment(session, ref: str):
    try:
        return lab_service.get_experiment(session, ref)
    except ServiceError as exc:
        sys.exit(exc.message)


def experiment_create(args: argparse.Namespace) -> None:
    filter_spec = None
    if args.filter_json:
        try:
            filter_spec = json.loads(args.filter_json)
        except json.JSONDecodeError as exc:
            sys.exit(f"--filter-json no es JSON válido: {exc}")
    with session_scope() as session:
        try:
            exp = lab_service.create_experiment(
                session,
                title=args.title,
                base_version_id=args.base,
                change_description=args.change,
                candidate_version_id=args.candidate,
                hypothesis_id=args.hypothesis,
                symbol=args.symbol,
                filter_spec=filter_spec,
            )
        except ServiceError as exc:
            sys.exit(exc.message)
        detail = lab_service.experiment_detail(session, exp)
    print(f"Experimento {detail['code']} creado: {detail['title']} ({detail['id']})")
    if detail["filter_text"]:
        print(
            f"Filtro: {detail['filter_text']}. Siguiente: experiment run-filter --id "
            f"{detail['number']}"
        )


def experiment_run_filter(args: argparse.Namespace) -> None:
    settings = get_settings()
    with session_scope() as session:
        exp = _experiment(session, args.id)
        try:
            row = lab_service.run_filter(session, settings, exp, args.notes)
        except ServiceError as exc:
            sys.exit(exc.message)
        code, revision, results = exp.code, row.revision, row.results
    print(f"Experimento {code}: revisión {revision} (filtro contrafactual)")
    print_filter_result(results)


def experiment_import_backtest(args: argparse.Namespace) -> None:
    settings = get_settings()
    if args.file == "-":
        data, name = sys.stdin.buffer.read(settings.lab_upload_max_bytes + 1), args.name
    else:
        try:
            with open(args.file, "rb") as fh:
                data = fh.read(settings.lab_upload_max_bytes + 1)
        except OSError as exc:
            sys.exit(f"No se pudo leer {args.file}: {exc}")
        name = args.name or args.file
    with session_scope() as session:
        exp = _experiment(session, args.id)
        try:
            run, row = lab_service.import_backtest(
                session,
                settings,
                exp,
                data,
                filename=name,
                version_id=args.version,
                label=args.label,
                symbol=args.symbol,
            )
        except ServiceError as exc:
            sys.exit(exc.message)
        code, revision, results = exp.code, row.revision, row.results
        label, fmt = run.label, run.report_format
    print(f"Experimento {code}: revisión {revision}, backtest '{label}' importado ({fmt})")
    print(lab_table([(f"backtest {label}", "todo", results["metricas"])]))
    for warning in results["avisos"]:
        print(f"AVISO: {warning}")


def experiment_show(args: argparse.Namespace) -> None:
    settings = get_settings()
    with session_scope() as session:
        exp = _experiment(session, args.id)
        detail = lab_service.experiment_detail(session, exp)
        comparison = lab_service.experiment_comparison(session, settings, exp)
    print(f"Experimento {detail['code']}: {detail['title']} [{detail['status_text']}]")
    print(
        f"Bot base: {detail['base_version']}"
        + (f" · {detail['symbol']}" if detail["symbol"] else "")
    )
    if detail["candidate_version"]:
        print(f"Versión candidata: {detail['candidate_version']}")
    print(f"Cambio: {detail['change_description']}")
    if detail["filter_text"]:
        print(f"Filtro: {detail['filter_text']}")
    if detail["hypothesis"]:
        h = detail["hypothesis"]
        print(f"Hipótesis: [{h['label']}] {h['statement']}")
    if detail["conclusion"]:
        print(f"Conclusión: {detail['conclusion']}")
    print(f"Revisiones: {len(detail['results'])}")
    for r in detail["results"]:
        print(
            f"  {r['revision']}. {r['kind_text']} · {r['created_at']:%Y-%m-%d %H:%M} UTC"
            + (f" · {r['notes']}" if r["notes"] else "")
        )
    rows = []
    for arm in comparison["brazos"]:
        for segment in SEGMENT_ORDER:
            if segment in arm["metricas"]:
                rows.append((arm["etiqueta"], segment, arm["metricas"][segment]))
    print(lab_table(rows))
    for arm in comparison["brazos"]:
        traps = arm.get("trampas")
        if traps and traps["validadas"]:
            print(
                f"Trampas validadas de {arm['etiqueta']}: "
                + "; ".join(t["statement"] for t in traps["validadas"])
            )
    for warning in comparison["avisos"]:
        print(f"AVISO: {warning}")
    print(comparison["nota"])


def experiment_list(_: argparse.Namespace) -> None:
    with session_scope() as session:
        items = lab_service.list_experiments(session)
    if not items:
        print("No hay experimentos.")
    for e in items:
        head = e["headline"]
        extra = ""
        if head:
            extra = (
                f" · {head['tramo'].replace('_', ' ')}: original {head['original']['n_trades']} "
                f"ops {_num(head['original']['expectancy_r'])} R, filtrado "
                f"{head['filtrado']['n_trades']} ops {_num(head['filtrado']['expectancy_r'])} R"
            )
        print(f"{e['code']} [{e['status_text']}] {e['title']} · {e['base_version']}{extra}")


# Alertas y Telegram ------------------------------------------------------------------------

TELEGRAM_HELP = """\
Cómo recibir las alertas en Telegram (unos 5 minutos, sin programar):

1. Crear el bot
   - En Telegram, busca @BotFather (tiene la marca azul de verificado) y ábrelo.
   - Escribe /newbot y responde a sus dos preguntas: un nombre (p. ej. "Mi Supervisor") y un
     usuario que termine en "bot" (p. ej. mi_supervisor_alertas_bot).
   - BotFather responde con un TOKEN parecido a 123456789:AAH...xyz. Es una contraseña:
     no lo compartas ni lo pegues en ningún chat.

2. Hablar con el bot
   - Abre el enlace t.me/<usuario_de_tu_bot> que te dio BotFather y pulsa INICIAR (o escribe
     cualquier mensaje). Un bot no puede escribirte hasta que tú le escribas primero.
   - Si prefieres un grupo: crea el grupo, añade el bot y escribe un mensaje en el grupo.

3. Averiguar el chat id
   - En el navegador abre (cambiando TOKEN por el tuyo):
       https://api.telegram.org/botTOKEN/getUpdates
   - Busca "chat":{"id": ... }. Ese número es el chat id (en un grupo empieza por -100...).
   - Si sale "result":[] vacío, vuelve a escribir al bot y recarga la página.

4. Configurar el supervisor (en el VPS)
   - Edita deploy/.env y añade:
       SUPERVISOR_TELEGRAM_BOT_TOKEN=123456789:AAH...xyz
       SUPERVISOR_TELEGRAM_CHAT_ID=987654321
   - Aplica los cambios:  docker compose up -d
   - Prueba:  docker compose exec api supervisor-cli alerts test
     Debe llegarte un mensaje "Prueba del Trading Supervisor".

Si algo falla, `alerts test` explica el motivo (token o chat id incorrectos, sin conexión).
Sin token o sin chat id el canal queda desactivado y las alertas solo se ven en el dashboard
(https://TU_DOMINIO/dashboard/alertas). El bot solo envía avisos: no recibe órdenes ni
controla MT5.
"""


def telegram_setup_help(_: argparse.Namespace) -> None:
    print(TELEGRAM_HELP)


def alerts_test(_: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.telegram_enabled:
        sys.exit(
            "Telegram no está configurado: faltan SUPERVISOR_TELEGRAM_BOT_TOKEN o "
            "SUPERVISOR_TELEGRAM_CHAT_ID. Ver: supervisor-cli telegram setup-help"
        )
    client = tg.TelegramClient.from_settings(settings)
    text = tg.format_alert(
        severity="INFO",
        event="DISPARO",
        title="Prueba del Trading Supervisor",
        lines=["Si lees esto, las alertas llegarán a este chat."],
        link=(settings.public_url.rstrip("/") + "/dashboard/alertas")
        if settings.public_url
        else None,
    )
    try:
        client.send_message(text)
    except tg.TelegramError as exc:
        sys.exit(f"No se pudo enviar: {client.redact(exc.message)}")
    print("Mensaje de prueba enviado a Telegram.")


def alerts_list(args: argparse.Namespace) -> None:
    with session_scope() as session:
        items = alerts_service.list_alerts(
            session, rule=args.kind, unacknowledged=args.unacknowledged, limit=args.limit
        )
    if not items:
        print("No hay alertas.")
    for a in items:
        ack = " [reconocida]" if a["acknowledged_at"] else ""
        event = " [resuelta]" if a["event"] == "RESUELTA" else ""
        print(
            f"{a['created_at']:%Y-%m-%d %H:%M} UTC · {a['severity_text']} · {a['rule']}{event}"
            f"{ack} · Telegram {a['delivery_text']} · {a['message']}"
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

    p = sub.add_parser(
        "backfill",
        help="completar operaciones antiguas: despliegue, MFE/MAE, riesgo, DNA y análisis",
    )
    p.add_argument("--from", dest="date_from", type=_date, help="hora de entrada desde (UTC)")
    p.add_argument("--to", dest="date_to", type=_date, help="hora de entrada hasta (UTC)")
    p.add_argument("--page-size", type=int, default=200, help="operaciones por página (≤ 500)")
    p.add_argument("--max-pages", type=int, default=0, help="parar tras N páginas (0 = todas)")
    p.add_argument("--after-time", type=_date, help="reanudar: cursor impreso (hora)")
    p.add_argument("--after-id", type=_uuid, help="reanudar: cursor impreso (trade_id)")
    p.set_defaults(func=backfill)

    exp = sub.add_parser("experiment", help="laboratorio: experimentos, filtros y backtests")
    exp_sub = exp.add_subparsers(dest="experiment_command", required=True)
    p = exp_sub.add_parser("create", help="crear un experimento (#001, #002...)")
    p.add_argument("--base", type=_uuid, required=True, help="versión de bot base (id)")
    p.add_argument("--title", required=True)
    p.add_argument("--change", required=True, help="el cambio, en texto libre")
    p.add_argument("--candidate", type=_uuid, help="versión de bot con el cambio (id)")
    p.add_argument("--hypothesis", type=_uuid, help="hipótesis o trampa de la fase 9 (id)")
    p.add_argument("--symbol")
    p.add_argument(
        "--filter-json",
        help='filtro: {"modo":"excluir","condicion":{"tipo":"busqueda","clausulas":[...]}}',
    )
    p.set_defaults(func=experiment_create)
    p = exp_sub.add_parser("run-filter", help="calcular el filtro contrafactual (nueva revisión)")
    p.add_argument("--id", required=True, help="número o id del experimento")
    p.add_argument("--notes")
    p.set_defaults(func=experiment_run_filter)
    p = exp_sub.add_parser("import-backtest", help="importar un resultado del Strategy Tester")
    p.add_argument("--id", required=True, help="número o id del experimento")
    p.add_argument("--file", required=True, help="CSV, HTML o XML; - para la entrada estándar")
    p.add_argument("--name", help="nombre del archivo (con --file -)")
    p.add_argument("--version", type=_uuid, help="versión probada (por defecto la base)")
    p.add_argument("--label", help="etiqueta del brazo (base, candidata...)")
    p.add_argument("--symbol")
    p.set_defaults(func=experiment_import_backtest)
    p = exp_sub.add_parser("show", help="experimento, revisiones y comparación lado a lado")
    p.add_argument("--id", required=True, help="número o id del experimento")
    p.set_defaults(func=experiment_show)
    p = exp_sub.add_parser("list", help="lista de experimentos")
    p.set_defaults(func=experiment_list)

    alerts = sub.add_parser("alerts", help="alertas: listar y probar Telegram")
    alerts_sub = alerts.add_subparsers(dest="alerts_command", required=True)
    p = alerts_sub.add_parser("list", help="últimas alertas")
    p.add_argument("--kind", choices=alerts_service.KINDS)
    p.add_argument("--unacknowledged", action="store_true", help="solo sin reconocer")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=alerts_list)
    p = alerts_sub.add_parser("test", help="enviar un mensaje de prueba a Telegram")
    p.set_defaults(func=alerts_test)

    telegram = sub.add_parser("telegram", help="ayuda para configurar Telegram")
    telegram_sub = telegram.add_subparsers(dest="telegram_command", required=True)
    p = telegram_sub.add_parser("setup-help", help="paso a paso: crear el bot y el chat id")
    p.set_defaults(func=telegram_setup_help)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
