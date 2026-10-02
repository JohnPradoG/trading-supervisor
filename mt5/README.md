# EA Monitor (SupervisorMonitor.mq5)

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
2. **Copiar el EA:** en MT5, Archivo → Abrir carpeta de datos → `MQL5\Experts\` y pega
   `SupervisorMonitor.mq5`. Ábrelo en MetaEditor (F4) y compílalo (F7). Debe terminar con
   `0 errors`.
3. **Permitir la conexión:** Herramientas → Opciones → Asesores Expertos → marca
   *Permitir WebRequest para las URL listadas* y añade `https://johntrading.duckdns.org`.
4. **Activarlo:** abre un gráfico cualquiera (uno que no uses para un bot, por ejemplo
   EURUSD M1) y arrastra `SupervisorMonitor` encima. En Parámetros de entrada pega la
   `ApiKey`. No hace falta activar "Permitir trading algorítmico" para este EA: no opera.
5. **Comprobar:** en la pestaña Expertos debe aparecer
   `Supervisor Monitor 1.0.0 iniciado: cuenta ..., solo lectura.` y en el VPS
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
- **Compilación:** el código se escribió y se validó contra la API con pruebas que reproducen
  sus mensajes exactos, pero no se pudo compilar fuera de MetaEditor. Si MetaEditor marca algún
  error al compilar, pásame el mensaje y lo corrijo.
