"""Lectura de resultados del Strategy Tester de MT5 (fase 10). Funciones puras, sin red.

Formatos:

- **CSV de transacciones (vía principal).** Una fila por deal con las columnas de la tabla
  "Transacciones/Deals" del probador: Time, Deal, Symbol, Type, Direction, Volume, Price,
  Order, Commission, Swap, Profit, Balance, Comment (en inglés o en castellano; el orden da
  igual y sobran columnas). Separador `,`, `;` o tabulador; UTF-8 (con o sin BOM) o UTF-16;
  decimales con punto o coma.
- **Informe HTML** (Guardar como informe) y **XML de Excel 2003** (SpreadsheetML): lectura
  tolerante, el mejor esfuerzo. Se busca la tabla cuya cabecera tenga las columnas de los
  deals y, en el resumen, el experto, el símbolo, el periodo, el modelo, el depósito inicial
  y algunas cifras del informe (solo para contrastar). Un .xlsx no se lee: hay que guardar el
  informe como HTML o exportar el CSV.

Operaciones: los deals `in` abren lotes por símbolo; cada deal `out`/`out by` (y la parte de
cierre de un `in/out`) cierra volumen en orden FIFO y es UNA operación con neto = beneficio +
swap + comisión del cierre + la parte proporcional de la comisión de entrada. Un cierre
parcial cuenta como una operación. Las horas son las del servidor del probador, tal cual. No
hay SL en los deals: los backtests se comparan en dinero, no en R.
"""

import csv
import io
import re
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Any

MAX_DEALS = 200_000


class BacktestFormatError(ValueError):
    """Archivo ilegible o sin la tabla de deals (mensaje apto para el usuario)."""


# Columnas y valores reconocidos ------------------------------------------------------------------


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip().lower().rstrip(":").strip()


COLUMNS: dict[str, tuple[str, ...]] = {
    "time": ("time", "hora", "fecha", "fecha/hora", "date", "date/time", "open time"),
    "deal": ("deal", "transaccion", "operacion", "ticket", "deal #"),
    "symbol": ("symbol", "simbolo", "instrumento"),
    "type": ("type", "tipo"),
    "direction": ("direction", "direccion", "entry", "entrada/salida"),
    "volume": ("volume", "volumen", "lots", "lotes"),
    "price": ("price", "precio"),
    "order": ("order", "orden"),
    "commission": ("commission", "comision"),
    "fee": ("fee", "cargo", "tarifa"),
    "swap": ("swap",),
    "profit": ("profit", "beneficio", "ganancia", "resultado"),
    "balance": ("balance",),
    "comment": ("comment", "comentario"),
}
REQUIRED = ("time", "type", "direction", "volume", "profit")
_ALIAS = {alias: key for key, aliases in COLUMNS.items() for alias in aliases}

DIRECTIONS = {
    "in": "IN",
    "entrada": "IN",
    "out": "OUT",
    "salida": "OUT",
    "in/out": "INOUT",
    "inout": "INOUT",
    "entrada/salida": "INOUT",
    "out by": "OUT",
    "out_by": "OUT",
    "salida por": "OUT",
}
TYPES = {"buy": "BUY", "compra": "BUY", "sell": "SELL", "venta": "SELL"}
BALANCE_TYPES = {"balance", "credit", "credito", "deposit", "deposito", "withdrawal", "retirada"}

SUMMARY_KEYS: dict[str, tuple[str, ...]] = {
    "experto": ("expert", "experto", "asesor", "asesor experto"),
    "simbolo": ("symbol", "simbolo"),
    "periodo": ("period", "periodo"),
    "modelo": ("modelling", "model", "modelado", "modelo", "ticks"),
    "deposito_inicial": ("initial deposit", "deposito inicial"),
    "beneficio_neto": ("total net profit", "beneficio neto total", "beneficio neto"),
    "profit_factor": ("profit factor", "factor de beneficio", "factor de rentabilidad"),
    "operaciones": ("total trades", "total de operaciones", "operaciones totales"),
    "drawdown_balance_maximo": (
        "balance drawdown maximal",
        "reduccion maxima del balance",
        "drawdown maximo del balance",
    ),
}
_SUMMARY_ALIAS = {alias: key for key, aliases in SUMMARY_KEYS.items() for alias in aliases}

TIME_FORMATS = (
    "%Y.%m.%d %H:%M:%S",
    "%Y.%m.%d %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%Y.%m.%d",
    "%Y-%m-%d",
)


def parse_number(text: str | None) -> float | None:
    """'1 234.56', '1,234.56', '1234,56', '-0.35' -> float. '' -> None."""
    if text is None:
        return None
    s = text.replace("\xa0", " ").replace(" ", " ").strip()
    if not s:
        return None
    s = s.split(" (")[0].strip()  # "123.45 (1.23%)" del informe
    s = s.replace(" ", "").replace("%", "")
    if "," in s and "." in s:
        if s.rfind(".") > s.rfind(","):
            s = s.replace(",", "")  # 1,234.56
        else:
            s = s.replace(".", "").replace(",", ".")  # 1.234,56
    elif "," in s:
        s = s.replace(",", ".") if s.count(",") == 1 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_time(text: str) -> datetime | None:
    s = text.strip()
    for fmt in TIME_FORMATS:
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# Lectura del archivo -----------------------------------------------------------------------------


def decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    head = data[:2000]
    if head and head.count(b"\x00") > len(head) // 4:
        # UTF-16 sin BOM: los ceros caen en las posiciones impares (LE) o pares (BE).
        odd = head[1::2].count(b"\x00")
        return data.decode("utf-16-le" if odd >= head[0::2].count(b"\x00") else "utf-16-be")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def detect_format(data: bytes, text: str) -> str:
    if data.startswith(b"PK\x03\x04"):
        raise BacktestFormatError(
            "el archivo es un .xlsx (Excel moderno) y no se puede leer: en el probador guarda el "
            "informe como HTML o exporta las transacciones a CSV"
        )
    head = text[:4000].lstrip().lower()
    if head.startswith("<?xml") or "<workbook" in head:
        return "XML"
    if "<html" in head or "<table" in head or head.startswith("<!doctype html"):
        return "HTML"
    return "CSV"


class _TableParser(HTMLParser):
    """Filas (listas de textos de celda) de todas las tablas del HTML, en orden."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._close_row()
            self._row = []
        elif tag in ("td", "th"):
            if self._row is None:
                self._row = []
            self._close_cell()
            self._cell = []
            span = dict(attrs).get("colspan") or "1"
            self._span = int(span) if span.isdigit() and 0 < int(span) <= 50 else 1
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th"):
            self._close_cell()
        elif tag in ("tr", "table"):
            self._close_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def _close_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            text = re.sub(r"\s+", " ", "".join(self._cell)).strip()
            # Una celda con colspan ocupa varias columnas: se repite vacía para no descuadrar.
            self._row.extend([text] + [""] * (self._span - 1))
        self._cell = None
        self._span = 1

    def _close_row(self) -> None:
        self._close_cell()
        if self._row:
            self.rows.append(self._row)
        self._row = None

    def close(self) -> None:
        super().close()
        self._close_row()


def html_rows(text: str) -> list[list[str]]:
    parser = _TableParser()
    parser.feed(text)
    parser.close()
    return parser.rows


def xml_rows(text: str) -> list[list[str]]:
    upper = text[:20000].upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise BacktestFormatError("el XML trae DOCTYPE/ENTITY y no se procesa por seguridad")
    try:
        root = ET.fromstring(text.lstrip("﻿").strip())
    except ET.ParseError as exc:
        raise BacktestFormatError(f"XML mal formado: {exc}") from exc
    rows: list[list[str]] = []
    for row in root.iter():
        if not row.tag.endswith("Row"):
            continue
        cells: list[str] = []
        for cell in row:
            if not cell.tag.endswith("Cell"):
                continue
            index = next((v for k, v in cell.attrib.items() if k.endswith("Index")), None)
            if index and index.isdigit():
                while len(cells) < int(index) - 1 and len(cells) < 200:
                    cells.append("")
            cells.append(re.sub(r"\s+", " ", "".join(cell.itertext())).strip())
        if any(cells):
            rows.append(cells)
    return rows


def csv_rows(text: str) -> list[list[str]]:
    sample = "\n".join(text.splitlines()[:20])
    try:
        dialect: Any = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if sample.count("\t") > sample.count(",") else csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in row)]


# Tabla de deals y resumen ------------------------------------------------------------------------


def _header_map(row: list[str]) -> dict[str, int] | None:
    found: dict[str, int] = {}
    for i, cell in enumerate(row):
        key = _ALIAS.get(_norm(cell))
        if key and key not in found:
            found[key] = i
    return found if all(k in found for k in REQUIRED) else None


@dataclass(frozen=True)
class Deal:
    time: datetime
    ticket: str
    symbol: str
    kind: str  # BUY, SELL o BALANCE
    direction: str | None  # IN, OUT, INOUT (None en balance)
    volume: float
    price: float | None
    commission: float
    fee: float
    swap: float
    profit: float
    balance: float | None
    comment: str


def _cell(row: list[str], cols: dict[str, int], key: str) -> str:
    i = cols.get(key)
    return row[i] if i is not None and i < len(row) else ""


def deals_from_rows(rows: list[list[str]]) -> tuple[list[Deal], list[str], int]:
    """Deals de la primera tabla con cabecera de deals. Devuelve (deals, avisos, índice de la
    cabecera)."""
    start = None
    cols: dict[str, int] = {}
    for i, row in enumerate(rows):
        found = _header_map(row)
        if found:
            start, cols = i, found
            break
    if start is None:
        raise BacktestFormatError(
            "no se encontró la tabla de transacciones (deals): debe tener al menos las columnas "
            "Time, Type, Direction, Volume y Profit (o Hora, Tipo, Dirección, Volumen, Beneficio)"
        )
    deals: list[Deal] = []
    skipped = 0
    for row in rows[start + 1 :]:
        if _header_map(row):
            break  # otra tabla (p. ej. órdenes) después de los deals
        raw_time = _cell(row, cols, "time")
        when = parse_time(raw_time) if raw_time else None
        if when is None:
            if deals and not any(c.strip() for c in row[1:]):
                continue
            if deals:
                break  # fin de la tabla (fila de totales)
            skipped += 1
            continue
        kind_text = _norm(_cell(row, cols, "type"))
        kind = TYPES.get(kind_text) or ("BALANCE" if kind_text in BALANCE_TYPES else None)
        if kind is None:
            skipped += 1
            continue
        direction = DIRECTIONS.get(_norm(_cell(row, cols, "direction")))
        if kind != "BALANCE" and direction is None:
            skipped += 1
            continue
        deals.append(
            Deal(
                time=when,
                ticket=_cell(row, cols, "deal"),
                symbol=_cell(row, cols, "symbol").strip(),
                kind=kind,
                direction=direction,
                volume=parse_number(_cell(row, cols, "volume")) or 0.0,
                price=parse_number(_cell(row, cols, "price")),
                commission=parse_number(_cell(row, cols, "commission")) or 0.0,
                fee=parse_number(_cell(row, cols, "fee")) or 0.0,
                swap=parse_number(_cell(row, cols, "swap")) or 0.0,
                profit=parse_number(_cell(row, cols, "profit")) or 0.0,
                balance=parse_number(_cell(row, cols, "balance")),
                comment=_cell(row, cols, "comment")[:64],
            )
        )
        if len(deals) > MAX_DEALS:
            raise BacktestFormatError(f"demasiados deals (más de {MAX_DEALS})")
    warnings = (
        [f"{skipped} filas de la tabla de deals no se entendieron y se ignoraron"]
        if (skipped)
        else []
    )
    return deals, warnings, start


def summary_from_rows(rows: list[list[str]]) -> dict[str, str]:
    """Pares etiqueta: valor del resumen del informe (la celda no vacía siguiente)."""
    out: dict[str, str] = {}
    for row in rows:
        for i, cell in enumerate(row):
            if not cell.strip().endswith(":"):
                continue
            key = _SUMMARY_ALIAS.get(_norm(cell))
            if key is None or key in out:
                continue
            value = next((c for c in row[i + 1 :] if c.strip()), "")
            if value:
                out[key] = value.strip()[:200]
    return out


# Operaciones -------------------------------------------------------------------------------------


@dataclass
class _Lot:
    direction: str
    volume: float
    remaining: float
    time: datetime
    commission: float
    symbol: str


@dataclass(frozen=True)
class BacktestTrade:
    entry_time: datetime | None
    close_time: datetime
    symbol: str
    direction: str | None
    volume: float
    net: float


@dataclass
class ParsedBacktest:
    format: str
    deals: list[Deal]
    trades: list[BacktestTrade]
    summary: dict[str, str]
    initial_deposit: float | None
    warnings: list[str] = field(default_factory=list)

    @property
    def symbols(self) -> list[str]:
        return sorted({d.symbol for d in self.deals if d.kind != "BALANCE" and d.symbol})

    def period(self) -> tuple[date, date]:
        times = [d.time for d in self.deals]
        return min(times).date(), max(times).date()


def _close(
    lots: list[_Lot], deal: Deal, volume: float, cost_share: float = 1.0
) -> tuple[list[BacktestTrade], float]:
    """Cierra `volume` del símbolo en FIFO contra lotes de la dirección contraria al deal.
    El beneficio y el swap del deal son del cierre; su comisión, en la parte `cost_share`.
    Devuelve las operaciones (una) y el volumen que no encontró lote."""
    closing = "BUY" if deal.kind == "SELL" else "SELL"
    left = volume
    entry_commission = 0.0
    first_time: datetime | None = None
    for lot in lots:
        if left <= 1e-9:
            break
        if lot.symbol != deal.symbol or lot.direction != closing or lot.remaining <= 1e-9:
            continue
        take = min(lot.remaining, left)
        lot.remaining -= take
        left -= take
        entry_commission += lot.commission * take / lot.volume if lot.volume else 0.0
        first_time = lot.time if first_time is None else min(first_time, lot.time)
    lots[:] = [lot for lot in lots if lot.remaining > 1e-9]
    net = deal.profit + deal.swap + (deal.commission + deal.fee) * cost_share + entry_commission
    trade = BacktestTrade(
        entry_time=first_time,
        close_time=deal.time,
        symbol=deal.symbol,
        direction=closing if first_time is not None else None,
        volume=round(volume, 4),
        net=round(net, 4),
    )
    return [trade], left


def build_trades(deals: list[Deal]) -> tuple[list[BacktestTrade], float | None, list[str]]:
    """Operaciones cerradas, depósito inicial y avisos (ver cabecera del módulo)."""
    lots: list[_Lot] = []
    trades: list[BacktestTrade] = []
    deposit: float | None = None
    warnings: list[str] = []
    unmatched = 0
    extra_balance = 0
    for d in deals:
        if d.kind == "BALANCE":
            if deposit is None and not trades and not lots:
                deposit = d.profit
            else:
                extra_balance += 1
            continue
        if d.volume <= 0:
            continue
        if d.direction == "IN":
            lots.append(_Lot(d.kind, d.volume, d.volume, d.time, d.commission + d.fee, d.symbol))
            continue
        if d.direction == "OUT":
            closed, left = _close(lots, d, d.volume)
            trades += closed
            unmatched += left > 1e-9
            continue
        # INOUT: cierra lo abierto en sentido contrario y abre el resto en el sentido del deal.
        closing = "BUY" if d.kind == "SELL" else "SELL"
        open_volume = sum(
            lot.remaining for lot in lots if lot.symbol == d.symbol and lot.direction == closing
        )
        close_volume = min(open_volume, d.volume)
        if close_volume > 1e-9:
            closed, _ = _close(lots, d, close_volume, close_volume / d.volume)
            trades += closed
        rest = d.volume - close_volume
        if rest > 1e-9:
            commission = (d.commission + d.fee) * rest / d.volume
            lots.append(_Lot(d.kind, rest, rest, d.time, commission, d.symbol))
    if unmatched:
        warnings.append(
            f"{unmatched} cierres sin apertura previa en el archivo (se cuentan sin hora de "
            "entrada ni dirección)"
        )
    if lots:
        warnings.append(f"{len(lots)} posiciones siguen abiertas al final del archivo y no cuentan")
    if extra_balance:
        warnings.append(
            f"{extra_balance} movimientos de balance después del inicio (depósitos o retiradas "
            "durante la prueba): no son operaciones y no cuentan en el neto"
        )
    return trades, deposit, warnings


def parse_backtest(data: bytes) -> ParsedBacktest:
    """Lee un CSV, HTML o XML del Strategy Tester (ver cabecera)."""
    if not data.strip():
        raise BacktestFormatError("el archivo está vacío")
    text = decode(data)
    fmt = detect_format(data, text)
    if fmt == "HTML":
        rows = html_rows(text)
    elif fmt == "XML":
        rows = xml_rows(text)
    else:
        rows = csv_rows(text)
    deals, warnings, header_at = deals_from_rows(rows)
    if not deals:
        raise BacktestFormatError(
            "la tabla de deals está vacía o no se entendió ninguna fila (la hora debe ser como "
            "2026.01.02 09:30:00)"
        )
    if not any(d.kind != "BALANCE" for d in deals):
        raise BacktestFormatError("la tabla de deals no tiene ninguna compra ni venta")
    summary = summary_from_rows(rows[:header_at]) if fmt != "CSV" else {}
    trades, deposit, more = build_trades(deals)
    warnings += more
    if not trades:
        raise BacktestFormatError("no hay ninguna operación cerrada en el archivo")
    reported_deposit = parse_number(summary.get("deposito_inicial"))
    if deposit is None and reported_deposit is not None:
        deposit = reported_deposit
    reported_net = parse_number(summary.get("beneficio_neto"))
    if reported_net is not None:
        computed = sum(t.net for t in trades)
        if abs(computed - reported_net) > max(0.01, abs(reported_net) * 0.001):
            warnings.append(
                f"el neto calculado con los deals ({computed:.2f}) no coincide con el del informe "
                f"({reported_net:.2f}): revisa que el archivo tenga todos los deals"
            )
    return ParsedBacktest(fmt, deals, trades, summary, deposit, warnings)
