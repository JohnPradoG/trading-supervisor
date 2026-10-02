/* Trading Supervisor · dashboard. Sin scripts en línea (CSP): los gráficos leen su
   configuración de atributos data-* y los datos de endpoints JSON del dashboard. */
(function () {
  "use strict";

  var COLORS = {
    balance: "#3987e5",
    equity: "#d95926",
    price: "#c3c2b7",
    entry: "#3987e5",
    sl: "#e66767",
    tp: "#3fb67f",
    close: "#9085e9",
    grid: "rgba(255,255,255,0.07)",
    tick: "#8f8e86",
    text: "#c3c2b7"
  };

  function pad(n) { return (n < 10 ? "0" : "") + n; }

  // Todas las horas en UTC.
  function fmtTime(ms, withDate) {
    var d = new Date(ms);
    var hm = pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
    return withDate ? pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + hm : hm;
  }

  function baseOptions(spanMs) {
    var withDate = spanMs > 20 * 3600 * 1000;
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      parsing: false,
      normalized: true,
      interaction: { mode: "nearest", axis: "x", intersect: false },
      plugins: {
        legend: { labels: { color: COLORS.text, boxWidth: 12, usePointStyle: true } },
        tooltip: {
          callbacks: {
            title: function (items) {
              return items.length ? fmtTime(items[0].parsed.x, true) + " UTC" : "";
            }
          }
        }
      },
      scales: {
        x: {
          type: "linear",
          grid: { color: COLORS.grid },
          ticks: {
            color: COLORS.tick,
            maxRotation: 0,
            autoSkipPadding: 16,
            callback: function (value) { return fmtTime(value, withDate); }
          }
        },
        y: { grid: { color: COLORS.grid }, ticks: { color: COLORS.tick } }
      }
    };
  }

  function line(label, color, data, extra) {
    var ds = {
      label: label,
      data: data,
      borderColor: color,
      backgroundColor: color,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0
    };
    for (var k in extra || {}) { ds[k] = extra[k]; }
    return ds;
  }

  function getJSON(url) {
    return fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (r) {
        if (r.status === 401) { window.location.href = "/dashboard/login"; throw new Error("401"); }
        if (!r.ok) { throw new Error("HTTP " + r.status); }
        return r.json();
      });
  }

  function replaceChart(canvas, config) {
    if (canvas._chart) { canvas._chart.destroy(); }
    canvas._chart = new window.Chart(canvas, config);
  }

  // Balance y equity -------------------------------------------------------------------
  function loadEquity(canvas) {
    var url = canvas.dataset.url + "?account_id=" + encodeURIComponent(canvas.dataset.account) +
      "&range=" + encodeURIComponent(canvas.dataset.range);
    getJSON(url).then(function (data) {
      var pts = data.points;
      var span = pts.length > 1 ? pts[pts.length - 1].t - pts[0].t : 0;
      replaceChart(canvas, {
        type: "line",
        data: {
          datasets: [
            line("Balance", COLORS.balance, pts.map(function (p) { return { x: p.t, y: p.balance }; })),
            line("Equity", COLORS.equity, pts.map(function (p) { return { x: p.t, y: p.equity }; }))
          ]
        },
        options: baseOptions(span)
      });
    }).catch(function (e) { console.error("equity", e); });
  }

  function initEquity(root) {
    var canvas = root.querySelector('canvas[data-chart="equity"]');
    if (!canvas) { return; }
    loadEquity(canvas);
    root.querySelectorAll("[data-equity-range]").forEach(function (btn) {
      btn.addEventListener("click", function () {
        root.querySelectorAll("[data-equity-range]").forEach(function (b) {
          b.setAttribute("aria-pressed", b === btn ? "true" : "false");
        });
        canvas.dataset.range = btn.dataset.equityRange;
        loadEquity(canvas);
      });
    });
    var select = root.querySelector("[data-equity-account]");
    if (select) {
      select.addEventListener("change", function () {
        canvas.dataset.account = select.value;
        loadEquity(canvas);
      });
    }
  }

  // Operación: cierres M1 con entrada, SL, TP y cierre ----------------------------------
  function initTrade(root) {
    var canvas = root.querySelector('canvas[data-chart="trade"]');
    if (!canvas) { return; }
    getJSON(canvas.dataset.url).then(function (d) {
      var bars = d.bars.map(function (b) { return { x: b.t, y: b.close }; });
      if (!bars.length) {
        var empty = document.querySelector("[data-chart-empty]");
        if (empty) { empty.hidden = false; }
        canvas.parentElement.hidden = true;
        return;
      }
      var x0 = Math.min(bars[0].x, d.entry.t);
      var x1 = Math.max(bars[bars.length - 1].x, d.close.t || bars[bars.length - 1].x);
      function level(label, color, value, dash) {
        return line(label, color, [{ x: x0, y: value }, { x: x1, y: value }],
          { borderWidth: 1.5, borderDash: dash || [], pointHoverRadius: 0 });
      }
      var sets = [line("Cierre M1", COLORS.price, bars)];
      sets.push(level("Entrada", COLORS.entry, d.entry.price, [4, 3]));
      var L = d.levels;
      if (L.initial_sl !== null) { sets.push(level("SL inicial", COLORS.sl, L.initial_sl, [6, 4])); }
      if (L.initial_tp !== null) { sets.push(level("TP inicial", COLORS.tp, L.initial_tp, [6, 4])); }
      if (L.current_sl !== null && L.current_sl !== L.initial_sl) { sets.push(level("SL final", COLORS.sl, L.current_sl, [2, 3])); }
      if (L.current_tp !== null && L.current_tp !== L.initial_tp) { sets.push(level("TP final", COLORS.tp, L.current_tp, [2, 3])); }
      var marks = [{ x: d.entry.t, y: d.entry.price }];
      if (d.close.t !== null && d.close.price !== null) {
        sets.push(level("Cierre", COLORS.close, d.close.price, [1, 3]));
        marks.push({ x: d.close.t, y: d.close.price });
      }
      sets.push({
        type: "scatter", label: "Entrada / cierre", data: marks,
        backgroundColor: COLORS.text, borderColor: "#1a1a19", borderWidth: 2,
        pointRadius: 5, pointHoverRadius: 7
      });
      replaceChart(canvas, { type: "line", data: { datasets: sets }, options: baseOptions(x1 - x0) });
    }).catch(function (e) { console.error("velas", e); });
  }

  // Laboratorio: neto acumulado de cada brazo ------------------------------------------
  var ARM_COLORS = ["#3987e5", "#3fb67f", "#d95926", "#9085e9", "#c98500", "#e66767"];

  function initCurves(root) {
    root.querySelectorAll('canvas[data-chart="curves"]').forEach(function (canvas) {
      getJSON(canvas.dataset.url).then(function (d) {
        var x0 = Infinity, x1 = -Infinity;
        var sets = d.series.map(function (s, i) {
          s.points.forEach(function (p) { x0 = Math.min(x0, p.x); x1 = Math.max(x1, p.x); });
          var color = ARM_COLORS[i % ARM_COLORS.length];
          var extra = s.fuente === "BACKTEST" ? { borderDash: [6, 4] } :
            s.fuente === "CONTRAFACTUAL" ? { borderDash: [2, 3] } : {};
          return line(s.label, color, s.points, extra);
        });
        replaceChart(canvas, {
          type: "line",
          data: { datasets: sets },
          options: baseOptions(isFinite(x1 - x0) ? x1 - x0 : 0)
        });
      }).catch(function (e) { console.error("curvas", e); });
    });
  }

  function init(root) {
    if (!window.Chart) { return; }
    initEquity(root);
    initTrade(root);
    initCurves(root);
  }

  document.addEventListener("DOMContentLoaded", function () { init(document); });
})();
