"""Informe del diagnóstico para descargar (Markdown o HTML) y para la CLI.

El mismo contenido se construye una vez como bloques (títulos, párrafos, listas y tablas) y se
escribe en Markdown o en HTML sin estilos ni scripts (se entrega como archivo adjunto a quien
edita el EA). Todo el texto sale del informe guardado: no se recalcula nada.
"""

import html
from datetime import datetime
from typing import Any

Block = tuple[Any, ...]

STATUS_LABEL = {
    "validada": "VALIDADA",
    "candidata, no validada": "CANDIDATA, NO VALIDADA",
    "sin datos suficientes": "SIN DATOS SUFICIENTES",
}
SEGMENTS = (
    ("fuera_de_muestra", "Fuera de muestra (titular)"),
    ("validacion", "Validación"),
    ("entrenamiento", "Entrenamiento (donde se eligió: optimista)"),
    ("forward", "Forward (después del split)"),
)


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f} %"


def num(value: float | None, places: int = 2, signed: bool = True) -> str:
    if value is None:
        return "—"
    return f"{value:+.{places}f}" if signed else f"{value:.{places}f}"


def ci(pair: list | None, fmt=num) -> str:
    if not pair:
        return ""
    return f" [{fmt(pair[0])}, {fmt(pair[1])}]"


def _date(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.strftime("%Y-%m-%d %H:%M")


def stats_text(s: dict[str, Any] | None) -> str:
    """n, aciertos con IC, expectativa con IC y profit factor de un grupo en R."""
    if not s or not s.get("n"):
        return "sin operaciones"
    return (
        f"n={s['n']} · aciertos {pct(s['win_rate'])}{ci(s.get('win_rate_ic95'), pct)} · "
        f"expectativa {num(s['expectancy_r'])} R{ci(s.get('expectancy_r_ic95'))} · "
        f"PF {num(s.get('profit_factor_r'), signed=False)}"
    )


def trade_link(base_url: str, trade_id: str) -> str:
    return f"{base_url}/dashboard/operaciones/{trade_id}"


def blocks(report: dict[str, Any], run: dict[str, Any], base_url: str = "") -> list[Block]:
    out: list[Block] = []
    scope = f"{report['bot_name']} v{report['version']}" + (
        f" · {report['symbol']}" if report.get("symbol") else " · todos los símbolos"
    )
    s = report["resumen"]
    out.append(("h1", f"Diagnóstico · {scope}"))
    out.append(
        (
            "p",
            f"Generado el {_date(run.get('created_at'))} UTC · diagnóstico "
            f"{report['diagnosis_version']} · {s['n']} operaciones cerradas con riesgo conocido.",
        )
    )
    out.append(
        (
            "quote",
            "Solo texto para quien edita el EA: el supervisor no modifica ni controla los bots. "
            '"VALIDADA" = la mejora se confirmó fuera de muestra; "CANDIDATA, NO VALIDADA" '
            "no es un resultado.",
        )
    )

    out.append(("h2", "Resumen"))
    period = s.get("periodo") or [None, None]
    out.append(
        (
            "ul",
            [
                f"Operaciones: {s['n']} (cierres de {_date(period[0])} a {_date(period[1])} UTC)",
                f"Aciertos (win rate): {pct(s['win_rate'])}{ci(s.get('win_rate_ic95'), pct)}",
                f"Expectativa: {num(s['expectancy_r'])} R por operación"
                f"{ci(s.get('expectancy_r_ic95'))}",
                f"Profit factor: {num(s.get('profit_factor'), signed=False)} · neto "
                f"{num(s.get('neto'))} · drawdown máximo {num(s.get('max_drawdown'), signed=False)}"
                f" ({num(s.get('max_drawdown_r'), signed=False)} R)",
                f"Datos: {s['suficiencia']['mensaje']}",
            ],
        )
    )
    if s.get("nota_win_rate"):
        out.append(("p", s["nota_win_rate"]))

    out.append(("h2", "Qué está fallando (ordenado por R perdido)"))
    if not report["fallos"]:
        out.append(("p", "No se detecta ningún modo de fallo con pérdidas."))
    for i, f in enumerate(report["fallos"], start=1):
        out.append(
            (
                "h3",
                f"{i}. {f['titulo']} — {num(f['r_perdido'], 1, signed=False)} R perdidos "
                f"({f['etiqueta']}; cambio: {f.get('estado', '—')})",
            )
        )
        out.append(("p", f["explicacion"]))
        items = [
            f"Grupo: {stats_text(f['grupo'])}",
            f"Resto: {stats_text(f['resto'])}",
        ]
        if f.get("cambios"):
            items.append("Cambios relacionados: " + ", ".join(f["cambios"]))
        items += [
            f"Ejemplo: operación {e['trade_id']} ({num(e['r'])} R, entrada "
            f"{_date(e['entrada'])} UTC) {trade_link(base_url, e['trade_id'])}"
            for e in f["ejemplos"][:3]
        ]
        out.append(("ul", items))

    out.append(("h2", "Qué cambiar"))
    changes = [c for c in report["cambios"] if not c["no_recomendada"]]
    if not changes:
        out.append(
            (
                "p",
                "Ningún cambio propuesto: "
                + (
                    "no hay datos suficientes para separar entrenamiento, validación y fuera de "
                    "muestra."
                    if s["suficiencia"]["estado"] != "suficiente"
                    else "ninguna variante mejora en entrenamiento."
                ),
            )
        )
    for c in changes:
        tag = STATUS_LABEL.get(c["estado"], c["estado"].upper())
        out.append(("h3", f"[{tag}] {c['accion']}"))
        out.append(("p", c["texto"]))
        if c.get("aviso_win_rate"):
            out.append(("p", c["aviso_win_rate"] + "."))
        out.append(("p", f"Estado: {c['motivo_estado']}."))
        rows = []
        for key, label in SEGMENTS:
            seg = c["tramos"].get(key)
            if not seg:
                continue
            gain = seg.get("mejora_r")
            rows.append(
                [
                    label,
                    str(seg["n"]),
                    stats_text(seg["original"]),
                    stats_text(seg["sugerida"]),
                    f"{num(gain)} R{ci(seg.get('mejora_r_ic95'))}",
                ]
            )
        first = "n (evitadas)" if c["tipo"] == "FILTRO" else "n"
        out.append(("table", ["Tramo", first, "Original", "Con el cambio", "Mejora"], rows))
        out.append(
            (
                "ul",
                [
                    f"Variantes probadas en su familia ({c['familia_texto']}): "
                    f"{c['variantes_probadas_familia']}",
                    *c["avisos"],
                ],
            )
        )
    if report.get("trampas_win_rate"):
        out.append(("h2", "Cambios que suben los aciertos pero pierden dinero (no recomendados)"))
        out.append(("ul", [t["texto"] for t in report["trampas_win_rate"]]))
    rejected = [c for c in report["cambios"] if c["no_recomendada"]]
    if rejected:
        out.append(("ul", [f"{c['accion']}: {c['aviso_win_rate']}." for c in rejected]))

    out.append(("h2", "Qué funciona mejor"))
    good = report["funciona"]
    items = [
        f"Ventaja validada: {e['statement']} · {stats_text(e['grupo'])}"
        for e in good["ventajas_validadas"]
    ]
    items += [f"Cambio validado: {c['texto']}" for c in good["cambios_validados"]]
    timing = good["mejores_horarios"]
    for key, label in (("sesiones", "Sesión"), ("franjas", "Franja"), ("dias", "Día")):
        for row in timing.get(key, []):
            items.append(f"{label} {row['etiqueta']} ({timing['etiqueta']}): {stats_text(row)}")
    out.append(("ul", items or ["Todavía nada destaca con muestra suficiente."]))

    out.append(("h2", "Cómo se ha calculado"))
    var = report["variantes"]
    rep = report["replica"]
    fid = rep["fidelidad"]
    out.append(
        (
            "ul",
            [
                f"Variantes probadas: {var['total']} ({var['salida']['probadas']} de salida y "
                f"{var['filtros']['probados']} filtros), elegidas solo con entrenamiento.",
                f"Réplica de salidas: {rep['replicadas']} operaciones con velas M1 suficientes; "
                f"excluidas {rep['excluidas'] or 'ninguna'}.",
                f"Fidelidad de la réplica: motivo de salida igual al real en "
                f"{pct(fid['motivo_igual_al_real'])}, diferencia media "
                f"{num(fid['diferencia_media_r'], signed=False)} R.",
                *report["avisos"],
            ],
        )
    )
    return out


def to_markdown(items: list[Block]) -> str:
    lines: list[str] = []
    for item in items:
        kind = item[0]
        if kind in ("h1", "h2", "h3"):
            lines += ["#" * int(kind[1]) + " " + item[1], ""]
        elif kind == "p":
            lines += [item[1], ""]
        elif kind == "quote":
            lines += ["> " + item[1], ""]
        elif kind == "ul":
            lines += [f"- {text}" for text in item[1]] + [""]
        elif kind == "table":
            header, rows = item[1], item[2]
            lines.append("| " + " | ".join(header) + " |")
            lines.append("|" + " --- |" * len(header))
            for row in rows:
                lines.append("| " + " | ".join(cell.replace("|", "/") for cell in row) + " |")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def to_html(items: list[Block], title: str) -> str:
    e = html.escape
    body: list[str] = []
    for item in items:
        kind = item[0]
        if kind in ("h1", "h2", "h3"):
            body.append(f"<{kind}>{e(item[1])}</{kind}>")
        elif kind == "p":
            body.append(f"<p>{e(item[1])}</p>")
        elif kind == "quote":
            body.append(f"<blockquote>{e(item[1])}</blockquote>")
        elif kind == "ul":
            body.append("<ul>" + "".join(f"<li>{e(t)}</li>" for t in item[1]) + "</ul>")
        elif kind == "table":
            head = "".join(f"<th>{e(h)}</th>" for h in item[1])
            rows = "".join(
                "<tr>" + "".join(f"<td>{e(c)}</td>" for c in row) + "</tr>" for row in item[2]
            )
            body.append(f"<table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table>")
    return (
        '<!doctype html>\n<html lang="es">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{e(title)}</title>\n</head>\n<body>\n" + "\n".join(body) + "\n</body>\n</html>\n"
    )


def markdown(report: dict[str, Any], run: dict[str, Any], base_url: str = "") -> str:
    return to_markdown(blocks(report, run, base_url))


def html_document(report: dict[str, Any], run: dict[str, Any], base_url: str = "") -> str:
    title = f"Diagnóstico {report['bot_name']} v{report['version']}"
    return to_html(blocks(report, run, base_url), title)
