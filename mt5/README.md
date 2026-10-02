# EA Monitor (SupervisorMonitor.mq5) y script de importación del historial

Archivos (los tres son de **solo lectura**):

| Archivo | Dónde va en MT5 | Qué es |
| --- | --- | --- |
| `Experts/SupervisorMonitor.mq5` | `MQL5\Experts\` | EA Monitor: envía lo que pasa en la cuenta en vivo |
| `Scripts/SupervisorImportHistory.mq5` | `MQL5\Scripts\` | Script que se ejecuta **una vez** para enviar el historial antiguo (ver [Importar el historial](#importar-el-historial-una-vez)) |
| `Include/Supervisor/SupervisorComun.mqh` | `MQL5\Include\Supervisor\` | Código común: los dos construyen exactamente el mismo mensaje de cada deal |

EA de **solo lectura** que observa toda la cuenta MT5 y envía al Trading Supervisor cada
operación, cada cambio de SL/TP, el balance/equity y las velas M1. No abre, modifica ni cierra
nada: no incluye `CTrade` ni llama a `OrderSend` (una prueba automática del backend lo
verifica). Tus bots no se tocan.

## Qué envía

| Dato | Cuándo | Ruta |
| --- | --- | --- |
| Cada deal de compra/venta (apertura, aumento, parcial, cierre) | Al instante (`OnTradeTransaction`) y en un barrido cada segundo | `/v1/ingest/events` |
| Cambio de SL o TP de una posición | Al detectarlo (cada segundo) | `/v1/ingest/events` |
| Foto de posiciones abiertas | Al arrancar y cada 5 min | `/v1/ingest/events` |
| Latido del terminal | Cada 30 s | `/v1/ingest/heartbeat` |
| Balance, equity, margen | Cada minuto | `/v1/ingest/account-snapshot` |
| Velas M1 cerradas de los símbolos operados | Cada minuto | `/v1/ingest/bars` |

Al arrancar revisa las últimas 48 horas de historial (`BackfillHours`); el servidor descarta lo
que ya tenía, así que reiniciar el EA o el terminal nunca duplica datos.

**Cola en disco:** los eventos de trading se escriben primero en
`MQL5\Files\Supervisor\outbox\` y solo se borran cuando el supervisor responde `accepted` o
`duplicate`. Si se cae internet o el VPS, se acumulan ahí y se envían al volver (con espera
creciente de hasta 2 minutos). Un evento que el servidor rechaza por inválido se mueve a
`MQL5\Files\Supervisor\rejected\` y se avisa en la pestaña Expertos.

## Instalación en el servidor Windows de Exness

1. **Crear la API key** (en el VPS Linux, una vez por terminal):
   ```bash
   cd /opt/trading-supervisor/deploy
   alias ts='docker compose exec api supervisor-cli'
   ts create-account --broker Exness --server <servidor> --login <cuenta> \
      --currency USD --type REAL --margin-mode HEDGING
   ts create-terminal --name exness-win-01 --server <servidor> --login <cuenta>
   ts create-api-key --terminal exness-win-01
   ```
   El servidor y el número de cuenta aparecen en MT5 en Archivo → Iniciar sesión en cuenta
   comercial. El modo (HEDGING o NETTING) aparece en la ventana Navegador → Cuentas.
2. **Copiar los archivos:** en MT5, Archivo → Abrir carpeta de datos. Dentro de `MQL5\`:
   - crea la carpeta `Include\Supervisor\` y pega `SupervisorComun.mqh`;
   - pega `SupervisorMonitor.mq5` en `Experts\`.

   Abre `SupervisorMonitor.mq5` en MetaEditor (F4) y compílalo (F7). Debe terminar con
   `0 errors`. Si dice que no encuentra `Supervisor\SupervisorComun.mqh`, el include no está en
   `MQL5\Include\Supervisor\`.
3. **Permitir la conexión:** Herramientas → Opciones → Asesores Expertos → marca
   *Permitir WebRequest para las URL listadas* y añade `https://johntrading.duckdns.org`.
4. **Activarlo:** abre un gráfico cualquiera (uno que no uses para un bot, por ejemplo
   EURUSD M1) y arrastra `SupervisorMonitor` encima. En Parámetros de entrada pega la
   `ApiKey`. No hace falta activar "Permitir trading algorítmico" para este EA: no opera.
5. **Comprobar:** en la pestaña Expertos debe aparecer
   `Supervisor Monitor 1.0.1 iniciado: cuenta ..., solo lectura.` y en el VPS
   `ts list-terminals` debe mostrar la última conexión de hace segundos.

Basta **un EA Monitor por cuenta**, aunque en ella corran varios bots: el supervisor asigna
cada operación a su bot por el magic number y el símbolo (despliegues registrados en la API).

## Parámetros

| Parámetro | Por defecto | Para qué |
| --- | --- | --- |
| `ApiBaseUrl` | `https://johntrading.duckdns.org` | Dirección del supervisor. Solo HTTPS. |
| `ApiKey` | (vacío) | Key del terminal, empieza por `tsk_`. |
| `BackfillHours` | 48 | Historial que revisa al arrancar. |
| `BarsBackfillHours` | 24 | Velas M1 que envía la primera vez por símbolo. Recomendado **720** (30 días): EMAs y volatilidad del análisis y todo el Trading DNA desde el primer día. |
| `ExtraBarSymbols` | (vacío) | Símbolos extra para guardar velas aunque no se operen, separados por coma. |
| `HttpTimeoutMs` | 5000 | Espera máxima por petición. |
| `VerboseLog` | false | Escribe cada envío en el diario (para diagnosticar). |

## Importar el historial (una vez)

El EA solo ve lo que pasa desde que está activo (más 48 h). Para que el Trading DNA, el
análisis y la búsqueda de trampas tengan datos **desde el primer día**, el script
`SupervisorImportHistory` envía una sola vez todo el historial de la cuenta:

1. **Primero las velas M1** de cada símbolo que aparece en el historial (más los de
   `ExtraBarSymbols`), desde el primer deal menos 45 días (`WarmupDays`, el calentamiento que
   necesitan las EMAs y el ATR) hasta ahora. Van en envíos de 3 días (máximo 4320 velas, por
   debajo del límite de 5000 del servidor y de unos 650 KB, lejos de los 2 MB de Caddy).
2. **Después los deals** cerrados del historial (por defecto, todo), en lotes de 500 como
   máximo, con el mismo formato que el EA y marcados como importados. El supervisor los procesa
   en segundo plano sin retrasar lo que envía el EA en vivo.

No envía fotos de balance (el historial de MT5 no las tiene y no se inventan): el % de riesgo
de cada operación importada usa el balance tras el deal, reconstruido hacia atrás desde el
balance actual. **No abre, modifica ni cierra nada.**

### Paso a paso

1. **Instala el EA Monitor primero** (apartado anterior) y comprueba que funciona: el script
   usa la misma API key y la misma URL permitida en WebRequest.
2. **Copia el script:** Archivo → Abrir carpeta de datos → `MQL5\Scripts\` y pega
   `SupervisorImportHistory.mq5` (el include `MQL5\Include\Supervisor\SupervisorComun.mqh` ya
   está si instalaste el EA). Ábrelo en MetaEditor (F4) y compílalo (F7): `0 errors`.
3. **Carga todo el historial de la cuenta:** en la ventana Caja de herramientas → pestaña
   *Historial*, clic derecho → *Todo el historial*. MT5 solo entrega al script los deals que
   tiene descargados.
4. **Prepara las velas antiguas (recomendado):** Herramientas → Opciones → Gráficos →
   *Máx. barras en gráfico* = *Ilimitado* (y reinicia MT5 si lo cambias). Abre un gráfico
   **M1** de cada símbolo que operaron tus bots (por ejemplo `USTEC_x100`), pulsa la tecla
   **Inicio** y mantén pulsada la flecha izquierda hasta que deje de cargar velas antiguas.
5. **Ejecuta el script:** en el Navegador → Scripts, arrastra `SupervisorImportHistory`
   sobre cualquier gráfico. En *Parámetros de entrada* pega la `ApiKey` (la del EA) y pulsa
   Aceptar. El resto de parámetros puede quedarse como está.
6. **Mira el progreso en la pestaña Expertos.** Verás algo así:
   ```
   Supervisor Import import-1.0.0: cuenta 12345678, solo lectura. Desfase servidor-UTC: 0 s (GMT+0.0).
   Supervisor: 2 símbolo(s) operados; primer deal 2026.01.12 09:31 (hora del servidor).
   Supervisor: velas USTEC_x100 desde 2025.11.28 09:31 hasta 2026.10.02 11:58 ...
   Supervisor: velas USTEC_x100: 290000 enviadas, 290000 nuevas, 0 ya estaban.
   Supervisor: deals enviados hasta 2026.05.03 15:10: 500
   Supervisor: RESUMEN velas: ... RESUMEN deals: 1840 enviados, 1840 nuevos, 0 duplicados, 0 rechazados.
   ```
   Puede tardar varios minutos. Si aparece `AVISO: faltan velas M1 de ...`, el terminal no tenía
   velas tan antiguas: repite el paso 4 para ese símbolo y vuelve a ejecutar el script. Si un
   deal sale `rechazado`, la línea dice el motivo que dio el servidor: pásamelo.
7. **En el VPS, registra con fechas pasadas qué versión de bot corrió con cada magic** (los
   despliegues). Un despliegue ya terminado lleva `ended_at`; el actual, no:
   ```bash
   TOKEN=$(grep SUPERVISOR_ADMIN_TOKEN /opt/trading-supervisor/deploy/.env | cut -d= -f2)
   curl -s -X POST https://TU_DOMINIO/v1/deployments -H "Authorization: Bearer $TOKEN" \
     -H 'content-type: application/json' -d '{"bot_version_id":"<id v1.0>",
     "account_login":12345678,"account_server":"Exness-MT5Real","symbol":"USTEC_x100",
     "magic_number":20260903,"started_at":"2026-01-01T00:00:00Z","ended_at":"2026-06-01T00:00:00Z"}'
   ```
   Si dos periodos del mismo magic, cuenta y símbolo se cruzan, el servidor responde 409 con
   el periodo que choca. El dashboard (Sistema) lista las operaciones sin despliegue.
8. **Completa las operaciones antiguas** cuando el worker haya terminado (en el dashboard,
   Resumen → cola del worker sin PENDING):
   ```bash
   cd /opt/trading-supervisor/deploy
   docker compose exec api supervisor-cli backfill
   ```
   Asigna cada operación a su despliegue y rehace MFE/MAE, riesgo, Trading DNA y análisis si
   cambió algo. Va por páginas; si se corta, repítelo (lo hecho se salta) o sigue desde el
   cursor que imprime. Después: `supervisor-cli patterns --version <id>` para buscar trampas.

**Repetirlo es seguro:** el servidor reconoce cada deal (`deal:<cuenta>:<ticket>`) y cada vela
que ya tenía y los cuenta como duplicados. Los deals que el EA ya había enviado en vivo también
salen como duplicados.

### Parámetros del script

| Parámetro | Por defecto | Para qué |
| --- | --- | --- |
| `ApiBaseUrl` | `https://johntrading.duckdns.org` | Dirección del supervisor. Solo HTTPS. |
| `ApiKey` | (vacío) | La key del terminal (la misma del EA). |
| `HistoryFrom` / `HistoryTo` | todo / ahora | Rango de deals, en hora del servidor del broker. |
| `WarmupDays` | 45 | Días de velas antes del primer deal de cada símbolo. |
| `ExtraBarSymbols` | (vacío) | Símbolos extra de los que enviar velas, separados por coma. |
| `SendBars` / `SendDeals` | true / true | Para repetir solo una de las dos partes. |
| `DealsPerPost` | 500 | Deals por envío (máximo 500, el límite del servidor). |
| `HttpTimeoutMs` | 30000 | Espera máxima por envío. |
| `MaxRetries` | 3 | Reintentos si falla la red o el servidor (5xx). Un 4xx no se reintenta. |
| `VerboseLog` | false | Escribe cada envío en el diario. |

### Horas e importación

El script usa el mismo método que el EA para pasar la hora del servidor a UTC
(`TimeTradeServer() - TimeGMT()`, redondeado a 15 min) y escribe el desfase en el diario.
Exness suele tener el servidor en **GMT+0**, sin horario de verano: entonces las horas son
exactas. Si el diario dice otra cosa, el desfase de **hoy** se aplica a todo el historial, y si
el broker cambia de hora en verano las operaciones del otro periodo quedan desplazadas 1 hora
(afecta a la sesión y a la hora del DNA). El supervisor guarda también la hora original del
broker (`time_server_msc`) para poder corregirlo más adelante.

## Limitaciones conocidas

- **Hora:** MT5 da la hora del servidor del broker. El EA calcula el desfase con UTC
  (`TimeTradeServer() - TimeGMT()`, redondeado a 15 min) y lo aplica también al historial de
  48 h. Si en esas 48 h hubo cambio de horario de verano, las horas anteriores al cambio
  quedan desplazadas una hora. El servidor guarda además la hora original del broker en
  milisegundos para poder corregirlo.
- **SL/TP:** un cambio se detecta revisando las posiciones cada segundo. Si el SL se mueve
  dos veces dentro del mismo segundo, solo llega el último valor. Los cambios ocurridos
  mientras el EA estaba apagado no generan evento; la foto de posiciones sí refleja el valor
  actual.
- **MFE/MAE:** no los calcula el EA; el servidor los obtiene de las velas M1 (fase 5).
- **Importación:** las operaciones importadas no tienen cambios de SL/TP intermedios (el
  historial de MT5 solo guarda el SL/TP de cada deal) ni fotos de balance; quedan marcadas con
  `data_quality.importado = true` y cuentan en las estadísticas como reales (lo son).
- **Compilación:** el código se escribió y se validó contra la API con pruebas que reproducen
  sus mensajes exactos, pero no se pudo compilar fuera de MetaEditor. Si MetaEditor marca algún
  error al compilar, pásame el mensaje y lo corrijo.
