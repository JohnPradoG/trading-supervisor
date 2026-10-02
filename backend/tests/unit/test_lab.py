"""Laboratorio (fase 10), funciones puras: lectura de archivos del Strategy Tester y filtro
contrafactual. Todos los archivos de estas pruebas están CONSTRUIDOS a mano con el formato de
las tablas del probador de MT5 (columnas y etiquetas reales); no son exportaciones reales."""

from datetime import UTC, datetime, timedelta

import pytest

from supervisor.analytics import backtest_report as bt
from supervisor.analytics import lab
from supervisor.analytics import patterns as pt
from supervisor.analytics.statistics import StatsParams, TradeResult

HEADER_EN = [
    "Time",
    "Deal",
    "Symbol",
    "Type",
    "Direction",
    "Volume",
    "Price",
    "Order",
    "Commission",
    "Swap",
    "Profit",
    "Balance",
    "Comment",
]
DEALS = [
    ["2026.01.02 00:00:00", "1", "", "balance", "", "", "", "", "0.00", "0.00", "10 000.00",
     "10 000.00", ""],
    ["2026.01.02 09:30:00", "2", "USTEC_x100", "buy", "in", "0.20", "20000.50", "2", "-0.70",
     "0.00", "0.00", "9 999.30", ""],
    ["2026.01.02 09:45:00", "3", "USTEC_x100", "sell", "out", "0.10", "20050.00", "3", "-0.35",
     "0.00", "4.95", "10 003.90", "parcial"],
    ["2026.01.02 10:00:00", "4", "USTEC_x100", "sell", "out", "0.10", "19990.00", "4", "-0.35",
     "-0.20", "-1.05", "10 002.30", "sl 19990"],
]  # fmt: skip
# Neto: parcial 4.95 - 0.35 - 0.35 (mitad de la comisión de entrada) = 4.25;
# cierre -1.05 - 0.20 - 0.35 - 0.35 = -1.95. Total 2.30.


def _html(rows: list[list[str]], summary: str = "") -> str:
    def tr(cells: list[str], tag: str = "td") -> str:
        return "<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>"

    return (
        "<!DOCTYPE html><html><head><title>Strategy Tester Report</title></head><body>"
        f"<table>{summary}</table>"
        "<table><tr><th colspan=13>Deals</th></tr>"
        + tr(HEADER_EN, "th")
        + "".join(tr(r) for r in rows)
        + '<tr><td colspan="8"></td><td>-1.40</td><td>-0.20</td><td>3.90</td><td>10 002.30</td>'
        "<td></td></tr></table></body></html>"
    )


SUMMARY_EN = (
    "<tr><td>Expert:</td><td colspan=3>EA_Nasdaq_FVG_Retest</td></tr>"
    "<tr><td>Symbol:</td><td>USTEC_x100</td></tr>"
    "<tr><td>Period:</td><td>M5 (2026.01.02 - 2026.01.03)</td></tr>"
    "<tr><td>Modelling:</td><td>Every tick based on real ticks</td></tr>"
    "<tr><td>Initial Deposit:</td><td>10 000.00</td><td>Total Net Profit:</td><td>2.30</td>"
    "<td>Profit Factor:</td><td>2.18</td></tr>"
)


def _check_deals(parsed: bt.ParsedBacktest) -> None:
    assert parsed.initial_deposit == 10000
    assert [t.net for t in parsed.trades] == [4.25, -1.95]
    first = parsed.trades[0]
    assert first.direction == "BUY" and first.volume == 0.1
    assert first.entry_time == datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    assert parsed.symbols == ["USTEC_x100"]
    assert parsed.period() == (datetime(2026, 1, 2).date(), datetime(2026, 1, 2).date())


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16"])
@pytest.mark.parametrize("sep", [";", ",", "\t"])
def test_csv_deals_any_separator_and_encoding(encoding: str, sep: str) -> None:
    rows = [HEADER_EN] + DEALS
    if sep == ",":
        rows = [[c if ":" in c else c.replace(" ", "") for c in r] for r in rows]
    text = "\n".join(sep.join(r) for r in rows)
    parsed = bt.parse_backtest(text.encode(encoding))
    assert parsed.format == "CSV" and parsed.warnings == []
    _check_deals(parsed)


def test_csv_spanish_headers_and_decimal_comma() -> None:
    header = "Hora;Transacción;Símbolo;Tipo;Dirección;Volumen;Precio;Orden;Comisión;Swap;"
    header += "Beneficio;Balance;Comentario"
    body = [";".join(c.replace(".", ",") if c[:1].isdigit() and "." in c and ":" not in c
                     else c for c in r) for r in DEALS]  # fmt: skip
    parsed = bt.parse_backtest(("﻿" + header + "\n" + "\n".join(body)).encode("utf-8"))
    _check_deals(parsed)


def test_html_report_with_summary_and_totals_row() -> None:
    parsed = bt.parse_backtest(_html(DEALS, SUMMARY_EN).encode("utf-16"))
    assert parsed.format == "HTML"
    _check_deals(parsed)
    assert parsed.summary["experto"] == "EA_Nasdaq_FVG_Retest"
    assert parsed.summary["simbolo"] == "USTEC_x100"
    assert parsed.summary["modelo"] == "Every tick based on real ticks"
    assert parsed.warnings == []  # el neto del informe (2.30) coincide


def test_html_report_net_mismatch_is_warned() -> None:
    summary = SUMMARY_EN.replace("<td>2.30</td>", "<td>99.00</td>")
    parsed = bt.parse_backtest(_html(DEALS, summary).encode("utf-8"))
    assert any("no coincide con el del informe" in w for w in parsed.warnings)


def test_spreadsheetml_xml_report() -> None:
    def row(cells: list[str]) -> str:
        return (
            "<Row>"
            + "".join(f'<Cell><Data ss:Type="String">{c}</Data></Cell>' for c in cells)
            + "</Row>"
        )

    xml = (
        '<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
        'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="Report">'
        "<Table>"
        + row(["Expert:", "EA_Nasdaq_FVG_Retest"])
        + '<Row><Cell><Data ss:Type="String">Symbol:</Data></Cell>'
        '<Cell ss:Index="3"><Data ss:Type="String">USTEC_x100</Data></Cell></Row>'
        + row(HEADER_EN)
        + "".join(row(r) for r in DEALS)
        + "</Table></Worksheet></Workbook>"
    )
    parsed = bt.parse_backtest(xml.encode("utf-8"))
    assert parsed.format == "XML" and parsed.summary["simbolo"] == "USTEC_x100"
    _check_deals(parsed)


def test_inout_netting_closes_and_reopens() -> None:
    rows = [HEADER_EN] + [
        ["2026.01.05 09:00", "1", "XAUUSD", "buy", "in", "1", "2000", "1", "-2", "0", "0", "", ""],
        ["2026.01.05 10:00", "2", "XAUUSD", "sell", "in/out", "3", "2010", "2", "-6", "0", "100",
         "", ""],
        ["2026.01.05 11:00", "3", "XAUUSD", "buy", "out", "2", "2005", "3", "-4", "0", "10", "",
         ""],
    ]  # fmt: skip
    parsed = bt.parse_backtest("\n".join(";".join(r) for r in rows).encode())
    first, second = parsed.trades
    # in/out: cierra 1 lote (beneficio 100, 1/3 de su comisión, toda la de entrada) y abre 2.
    assert (first.direction, first.volume, first.net) == ("BUY", 1.0, 100 - 2 - 2)
    assert (second.direction, second.volume, second.net) == ("SELL", 2.0, 10 - 4 - 4)
    assert parsed.initial_deposit is None


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "vacío"),
        (b"PK\x03\x04xlsx", ".xlsx"),
        (b"a;b;c\n1;2;3\n", "no se encontró la tabla"),
        (
            b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><Workbook/>',
            "DOCTYPE",
        ),
        (b"<?xml version='1.0'?><Workbook><Row>", "XML mal formado"),
        (
            ("\n".join([";".join(HEADER_EN), ";".join(DEALS[0])])).encode(),
            "ninguna compra ni venta",
        ),
        (
            ("\n".join([";".join(HEADER_EN), ";".join(DEALS[1])])).encode(),
            "ninguna operación cerrada",
        ),
    ],
)
def test_clear_errors(data: bytes, message: str) -> None:
    with pytest.raises(bt.BacktestFormatError, match=message):
        bt.parse_backtest(data)


def test_unknown_rows_and_open_positions_are_warned() -> None:
    rows = [HEADER_EN] + DEALS[:2] + [
        ["2026.01.02 09:40", "9", "USTEC_x100", "raro", "in", "1", "1", "9", "0", "0", "0", "", ""],
    ] + DEALS[2:3]  # fmt: skip
    parsed = bt.parse_backtest("\n".join(";".join(r) for r in rows).encode())
    assert any("no se entendieron" in w for w in parsed.warnings)
    assert any("siguen abiertas" in w for w in parsed.warnings)


@pytest.mark.parametrize(
    ("text", "value"),
    [("1 234.56", 1234.56), ("1,234.56", 1234.56), ("1.234,56", 1234.56), ("-0,35", -0.35),
     ("12.5 (1.2%)", 12.5), ("", None), ("abc", None)],
)  # fmt: skip
def test_parse_number(text: str, value: float | None) -> None:
    assert bt.parse_number(text) == value


# Filtro contrafactual ----------------------------------------------------------------------------

T0 = datetime(2026, 3, 2, tzinfo=UTC)
TREND = pt.Condition((pt.Clause("tendencia_h1_rel", "eq", value="EN_CONTRA"),))


def _trade(i: int, trend: str | None, r: float) -> lab.LabTrade:
    close = T0 + timedelta(hours=i)
    obs = pt.Obs(str(i), close, r, r > 0, {"tendencia_h1_rel": trend})
    return lab.LabTrade(obs, TradeResult(f"t{i:04d}", close, r * 100, risk=100.0))


def test_apply_filter_modes_and_unknown() -> None:
    trades = [_trade(0, "EN_CONTRA", -1), _trade(1, "A_FAVOR", 1.5), _trade(2, None, 1)]
    kept, removed, unknown = lab.apply_filter(trades, TREND, lab.EXCLUDE)
    assert [t.obs.trade_id for t in kept] == ["1", "2"] and len(removed) == 1 and unknown == 1
    kept, removed, unknown = lab.apply_filter(trades, TREND, lab.ONLY)
    assert [t.obs.trade_id for t in kept] == ["0", "2"] and len(removed) == 1


def test_counterfactual_segments_and_warnings() -> None:
    trades = [_trade(i, "EN_CONTRA" if i % 3 == 0 else "A_FAVOR", -1 if i % 3 == 0 else 1)
              for i in range(90)]  # fmt: skip
    split = lab.Split("s", T0 + timedelta(hours=59), T0 + timedelta(hours=89),
                      T0 + timedelta(hours=40), "ultimo_split")  # fmt: skip
    spec = {"modo": "excluir", "condicion": TREND.spec()}
    res = lab.counterfactual(
        trades, spec, TREND, split, StatsParams(), 100, from_hypothesis=True,
        min_trades_for_split=100,
    )  # fmt: skip
    assert res["titular"] == "fuera_de_muestra"
    oos = res["tramos"]["fuera_de_muestra"]
    assert oos["original"]["n_trades"] == 30 and oos["filtrado"]["n_trades"] == 20
    assert oos["filtrado"]["expectancy_r"] == 1 and oos["evitadas"]["expectancy_r"] == -1
    assert res["tramos"]["dentro_de_muestra"]["original"]["n_trades"] == 60
    assert res["avisos"][0] == lab.WARNING_IN_SAMPLE
    assert lab.WARNING_COUNTERFACTUAL in res["avisos"]
    assert lab.WARNING_MANUAL not in res["avisos"]

    no_split = lab.counterfactual(
        trades[:20], spec, TREND, None, StatsParams(), 0, from_hypothesis=False,
        min_trades_for_split=100,
    )  # fmt: skip
    assert no_split["titular"] == "todo" and "100 operaciones" in no_split["avisos"][0]
    assert lab.WARNING_MANUAL in no_split["avisos"]
    assert no_split["tramos"]["todo"]["original"]["max_drawdown_ic95"] is None  # samples=0


def test_bootstrap_is_deterministic_and_bounded() -> None:
    results = [TradeResult(f"t{i}", T0 + timedelta(hours=i), 100.0 if i % 2 else -80.0)
               for i in range(40)]  # fmt: skip
    a = lab.bootstrap_intervals(results, StatsParams(), 300, 42)
    b = lab.bootstrap_intervals(results, StatsParams(), 300, 42)
    assert a == b
    low, high = a["max_drawdown_ic95"]
    assert 0 <= low <= high
    pf_low, pf_high = a["profit_factor_ic95"]
    assert pf_low < 100 * 20 / (80 * 20) < pf_high
    assert lab.bootstrap_intervals(results[:5], StatsParams(), 300, 1)["max_drawdown_ic95"] is None


def test_equity_curve_downsampling_keeps_last_point() -> None:
    results = [TradeResult(f"t{i}", T0 + timedelta(minutes=i), 1.0) for i in range(1000)]
    curve = lab.equity_curve(results, 100)
    assert len(curve) == 100 and curve[-1][1] == 1000 and curve[0][1] == 1
    assert lab.equity_curve(results[:5], 100)[-1] == [
        int((T0 + timedelta(minutes=4)).timestamp() * 1000),
        5,
    ]
