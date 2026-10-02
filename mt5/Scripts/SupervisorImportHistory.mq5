//+------------------------------------------------------------------+
//| SupervisorImportHistory.mq5                                      |
//| Trading Supervisor - importación del historial, SOLO LECTURA     |
//|                                                                  |
//| Script (no EA): se ejecuta una vez y termina. Envía al           |
//| Trading Supervisor, en este orden:                               |
//|   1. Las velas M1 de cada símbolo operado (más los extra), desde |
//|      el primer deal menos WarmupDays días hasta ahora, para que  |
//|      el Trading DNA y el análisis tengan EMAs y ATR al entrar.   |
//|   2. Todos los deals cerrados del historial, con el mismo JSON   |
//|      que el EA Monitor (Include\Supervisor\SupervisorComun.mqh), |
//|      más el balance tras cada deal reconstruido desde el balance |
//|      actual, en lotes marcados "origin":"history_import".       |
//|                                                                  |
//| No envía fotos de cuenta: no existen en el historial y no se     |
//| inventan (el riesgo usa balance_after, como en la fase 5).       |
//|                                                                  |
//| Repetirlo es seguro: el servidor reconoce lo que ya tenía y lo   |
//| cuenta como duplicado.                                           |
//|                                                                  |
//| Este script NO abre, modifica ni cierra operaciones. No incluye  |
//| la clase CTrade ni llama a OrderSend en ningún punto.            |
//+------------------------------------------------------------------+
#property copyright "Trading Supervisor"
#property version   "1.00"
#property description "Importa al Trading Supervisor las velas M1 y los deals del historial. Solo lectura."
#property script_show_inputs

#include <Supervisor\SupervisorComun.mqh>

#define IMPORT_VERSION    "import-1.0.0"
#define MAX_BODY_CHARS    1800000   // Caddy corta los cuerpos de más de 2 MB
#define SERVER_MAX_BARS   5000      // ingest_max_bars del servidor
#define SERVER_MAX_DEALS  500       // ingest_max_batch del servidor
#define BARS_WINDOW_DAYS  3         // 3 días de M1 = 4320 velas como mucho por envío
#define WHOLE_HISTORY     D'2000.01.02 00:00'

//--- Parámetros
input string   ApiBaseUrl      = "https://johntrading.duckdns.org"; // URL del supervisor (sin / final)
input string   ApiKey          = "";                  // API key de este terminal (la misma del EA)
input datetime HistoryFrom     = D'2000.01.01 00:00'; // Deals desde (hora del servidor; por defecto todo)
input datetime HistoryTo       = 0;                   // Deals hasta (0 = ahora)
input int      WarmupDays      = 45;                  // Días de velas antes del primer deal
input string   ExtraBarSymbols = "";                  // Símbolos extra para velas, separados por coma
input bool     SendBars        = true;                // Enviar velas M1 (primero)
input bool     SendDeals       = true;                // Enviar deals (después)
input int      DealsPerPost    = 500;                 // Deals por envío (máximo 500)
input int      HttpTimeoutMs   = 30000;               // Tiempo máximo por petición HTTP
input int      MaxRetries      = 3;                   // Reintentos por envío si falla la red
input bool     VerboseLog      = false;               // Escribir cada envío en el diario

//--- Estado
long     g_login = 0;
int      g_tzOffset = 0;
string   g_symbols[];          // símbolos con velas a enviar
datetime g_symbolFrom[];       // primer deal de cada símbolo (hora del servidor)
bool     g_aborted = false;

long     g_barsSent = 0, g_barsInserted = 0;
long     g_dealsSent = 0, g_dealsAccepted = 0, g_dealsDuplicate = 0, g_dealsRejected = 0;
int      g_symbolsIncomplete = 0;

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
int HttpRequest(string method, string path, string body, string &response)
  {
   string headers = "Content-Type: application/json\r\nX-API-Key: " + ApiKey + "\r\n";
   char data[];
   char result[];
   string result_headers;
   if(body != "")
     {
      int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
      if(n > 0)
         ArrayResize(data, n - 1);   // sin el \0 final
     }
   ResetLastError();
   int status = WebRequest(method, ApiBaseUrl + path, headers, HttpTimeoutMs, data, result,
                           result_headers);
   if(status == -1)
     {
      int err = GetLastError();
      if(err == 4014)
         PrintFormat("Supervisor: añade %s en Herramientas > Opciones > Asesores Expertos > "
                     "'Permitir WebRequest para las URL listadas'", ApiBaseUrl);
      else
         PrintFormat("Supervisor: fallo de red en %s (error %d)", path, err);
      response = "";
      return -1;
     }
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   return status;
  }

// POST con reintentos para fallos de red o del servidor (5xx). Un 4xx no se reintenta.
int PostJson(string path, string body, string &response)
  {
   int status = -1;
   for(int attempt = 1; attempt <= MathMax(MaxRetries, 1); attempt++)
     {
      if(IsStopped())
         return -1;
      status = HttpRequest("POST", path, body, response);
      if(status == 200)
         return status;
      if(status == 401)
        {
         Print("Supervisor: API key rechazada (401). Revisa el parámetro ApiKey.");
         return status;
        }
      if(status >= 400 && status < 500)
        {
         PrintFormat("Supervisor: envío rechazado en %s (HTTP %d): %s", path, status,
                     StringSubstr(response, 0, 500));
         return status;
        }
      if(status > 0)
         PrintFormat("Supervisor: HTTP %d en %s, reintento %d de %d", status, path, attempt,
                     MaxRetries);
      Sleep(2000 * attempt);
     }
   return status;
  }

long JsonLong(string text, string key)
  {
   string marker = "\"" + key + "\":";
   int p = StringFind(text, marker);
   if(p < 0)
      return -1;
   p += StringLen(marker);
   int e = p;
   int len = StringLen(text);
   while(e < len)
     {
      ushort c = StringGetCharacter(text, e);
      if(c < '0' || c > '9')
         break;
      e++;
     }
   if(e == p)
      return -1;
   return StringToInteger(StringSubstr(text, p, e - p));
  }

string ErrorForIndex(string response, int index)
  {
   string marker = "\"index\":" + IntegerToString(index) + ",\"status\":\"rejected\"";
   int p = StringFind(response, marker);
   if(p < 0)
      return "";
   int e = StringFind(response, "\"error\":\"", p);
   if(e < 0)
      return "";
   e += 9;
   int end = StringFind(response, "\"}", e);
   if(end < 0)
      end = StringLen(response);
   return StringSubstr(response, e, end - e);
  }

//+------------------------------------------------------------------+
//| Símbolos                                                         |
//+------------------------------------------------------------------+
void AddSymbol(string symbol, datetime first_deal)
  {
   if(symbol == "")
      return;
   for(int i = 0; i < ArraySize(g_symbols); i++)
      if(g_symbols[i] == symbol)
        {
         if(first_deal > 0 && (g_symbolFrom[i] == 0 || first_deal < g_symbolFrom[i]))
            g_symbolFrom[i] = first_deal;
         return;
        }
   int n = ArraySize(g_symbols);
   ArrayResize(g_symbols, n + 1);
   ArrayResize(g_symbolFrom, n + 1);
   g_symbols[n] = symbol;
   g_symbolFrom[n] = first_deal;
   SymbolSelect(symbol, true);
  }

datetime DealsTo()
  {
   return HistoryTo > 0 ? HistoryTo : TimeTradeServer() + 86400;
  }

// Símbolos de los deals de compra/venta del rango y la hora de su primer deal.
datetime CollectSymbols()
  {
   datetime first = 0;
   if(!HistorySelect(HistoryFrom, DealsTo()))
     {
      Print("Supervisor: no se pudo leer el historial (HistorySelect).");
      return 0;
     }
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      long type = HistoryDealGetInteger(ticket, DEAL_TYPE);
      if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
         continue;
      datetime t = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      AddSymbol(HistoryDealGetString(ticket, DEAL_SYMBOL), t);
      if(first == 0 || t < first)
         first = t;
     }
   return first;
  }

//+------------------------------------------------------------------+
//| 1. Velas M1                                                      |
//+------------------------------------------------------------------+
bool PostBars(string symbol, MqlRates &rates[], int start, int count, int digits)
  {
   string items = "";
   for(int i = start; i < start + count; i++)
     {
      if(items != "")
         items += ",";
      items += "{\"time_utc\":" + JStr(IsoUtc((datetime)(rates[i].time - g_tzOffset))) +
               ",\"open\":" + JNum(rates[i].open, digits) +
               ",\"high\":" + JNum(rates[i].high, digits) +
               ",\"low\":" + JNum(rates[i].low, digits) +
               ",\"close\":" + JNum(rates[i].close, digits) +
               ",\"tick_volume\":" + IntegerToString(rates[i].tick_volume) +
               ",\"spread\":" + IntegerToString(rates[i].spread) + "}";
     }
   string body = "{\"symbol\":" + JStr(symbol) + ",\"timeframe\":\"M1\",\"bars\":[" + items + "]}";
   if(StringLen(body) > MAX_BODY_CHARS && count > 1)
     {
      // No pasa con 4320 velas (~150 bytes cada una), pero el límite de Caddy manda.
      int half = count / 2;
      return PostBars(symbol, rates, start, half, digits) &&
             PostBars(symbol, rates, start + half, count - half, digits);
     }
   string response;
   int status = PostJson("/v1/ingest/bars", body, response);
   if(status != 200)
      return false;
   long inserted = JsonLong(response, "inserted");
   g_barsSent += count;
   if(inserted > 0)
      g_barsInserted += inserted;
   return true;
  }

bool SendSymbolBars(string symbol, datetime from_server)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits <= 0)
      digits = 5;
   datetime last_closed = iTime(symbol, PERIOD_M1, 1);   // la vela 0 sigue abierta
   if(last_closed <= 0)
     {
      PrintFormat("Supervisor: %s sin velas M1 en el terminal. Abre un gráfico M1 de %s, "
                  "espera a que cargue y vuelve a ejecutar el script.", symbol, symbol);
      g_symbolsIncomplete++;
      return true;
     }
   datetime local_first = (datetime)SeriesInfoInteger(symbol, PERIOD_M1, SERIES_FIRSTDATE);
   datetime server_first = (datetime)SeriesInfoInteger(symbol, PERIOD_M1, SERIES_SERVER_FIRSTDATE);
   PrintFormat("Supervisor: velas %s desde %s hasta %s (hora del servidor). El terminal tiene "
               "M1 desde %s; el broker, desde %s.", symbol, TimeToString(from_server),
               TimeToString(last_closed), TimeToString(local_first), TimeToString(server_first));

   long sent_before = g_barsSent;
   long inserted_before = g_barsInserted;
   datetime first_bar = 0;
   int windows = 0;
   for(datetime t0 = from_server; t0 <= last_closed; t0 += BARS_WINDOW_DAYS * 86400)
     {
      if(IsStopped())
         return false;
      datetime t1 = t0 + BARS_WINDOW_DAYS * 86400 - 1;
      if(t1 > last_closed)
         t1 = last_closed;
      MqlRates rates[];
      int copied = -1;
      for(int attempt = 0; attempt < 3 && copied < 0; attempt++)
        {
         // La primera petición puede lanzar la descarga del broker: se reintenta.
         ResetLastError();
         copied = CopyRates(symbol, PERIOD_M1, t0, t1, rates);
         if(copied < 0)
            Sleep(500);
        }
      if(copied <= 0)
         continue;   // fin de semana, festivo o tramo que el terminal no tiene
      if(first_bar == 0)
         first_bar = rates[0].time;
      for(int start = 0; start < copied; start += SERVER_MAX_BARS)
        {
         int count = (int)MathMin(SERVER_MAX_BARS, copied - start);
         if(!PostBars(symbol, rates, start, count, digits))
           {
            PrintFormat("Supervisor: velas %s detenidas en %s. Vuelve a ejecutar el script: "
                        "lo ya enviado contará como duplicado.", symbol, TimeToString(t0));
            return false;
           }
        }
      windows++;
      if(VerboseLog || windows % 20 == 0)
         PrintFormat("Supervisor: velas %s hasta %s: %I64d enviadas", symbol, TimeToString(t1),
                     g_barsSent - sent_before);
     }
   long sent = g_barsSent - sent_before;
   long inserted = g_barsInserted - inserted_before;
   PrintFormat("Supervisor: velas %s: %I64d enviadas, %I64d nuevas, %I64d ya estaban.", symbol, sent,
               inserted, sent - inserted);
   // Menos de 4 días de diferencia se explican por fines de semana y festivos.
   if(first_bar == 0 || first_bar > from_server + 4 * 86400)
     {
      g_symbolsIncomplete++;
      PrintFormat("Supervisor: AVISO: faltan velas M1 de %s desde %s hasta %s. El terminal solo "
                  "da las velas que tiene descargadas (máximo %d por gráfico). Para completarlas: "
                  "Herramientas > Opciones > Gráficos > 'Máx. barras en gráfico' = Ilimitado, "
                  "abre un gráfico M1 de %s, pulsa Inicio para desplazarte al principio hasta que "
                  "deje de cargar y vuelve a ejecutar el script.",
                  symbol, TimeToString(from_server),
                  first_bar == 0 ? TimeToString(last_closed) : TimeToString(first_bar),
                  TerminalInfoInteger(TERMINAL_MAXBARS), symbol);
     }
   return true;
  }

bool SendAllBars(datetime first_deal)
  {
   string extra[];
   int n = StringSplit(ExtraBarSymbols, ',', extra);
   for(int i = 0; i < n; i++)
     {
      string s = extra[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      AddSymbol(s, 0);
     }
   datetime fallback = first_deal > 0 ? first_deal : (HistoryFrom > WHOLE_HISTORY ? HistoryFrom
                                                                                  : TimeTradeServer());
   for(int i = 0; i < ArraySize(g_symbols); i++)
     {
      datetime from_deal = g_symbolFrom[i] > 0 ? g_symbolFrom[i] : fallback;
      datetime from_server = from_deal - WarmupDays * 86400;
      from_server -= from_server % 60;
      if(!SendSymbolBars(g_symbols[i], from_server))
         return false;
     }
   return true;
  }

//+------------------------------------------------------------------+
//| 2. Deals                                                         |
//+------------------------------------------------------------------+
double BalanceDelta(ulong ticket)
  {
   // El crédito no forma parte del balance; todo lo demás (operaciones, depósitos,
   // retiradas, comisiones, swaps, correcciones) sí.
   if(HistoryDealGetInteger(ticket, DEAL_TYPE) == DEAL_TYPE_CREDIT)
      return 0.0;
   return HistoryDealGetDouble(ticket, DEAL_PROFIT) + HistoryDealGetDouble(ticket, DEAL_COMMISSION) +
          HistoryDealGetDouble(ticket, DEAL_SWAP) + HistoryDealGetDouble(ticket, DEAL_FEE);
  }

bool PostDeals(string events, ulong &tickets[], int count)
  {
   string body = "{\"schema_version\":1,\"sent_at\":" + JStr(IsoUtc(TimeGMT())) +
                 ",\"ea_version\":" + JStr(IMPORT_VERSION) +
                 ",\"origin\":\"history_import\",\"events\":[" + events + "]}";
   string response;
   int status = PostJson("/v1/ingest/events", body, response);
   if(status != 200)
      return false;
   int cursor = 0;
   for(int i = 0; i < count; i++)
     {
      string st = StatusForIndex(response, i, cursor);
      if(st == "accepted")
         g_dealsAccepted++;
      else if(st == "duplicate")
         g_dealsDuplicate++;
      else
        {
         g_dealsRejected++;
         PrintFormat("Supervisor: deal %I64u rechazado: %s", tickets[i], ErrorForIndex(response, i));
        }
     }
   g_dealsSent += count;
   if(VerboseLog)
      PrintFormat("Supervisor: deals: %I64d enviados (%I64d nuevos, %I64d duplicados, %I64d rechazados)",
                  g_dealsSent, g_dealsAccepted, g_dealsDuplicate, g_dealsRejected);
   return true;
  }

bool SendAllDeals()
  {
   // Se lee hasta ahora aunque HistoryTo sea anterior: el balance tras cada deal se
   // reconstruye hacia atrás desde el balance actual.
   if(!HistorySelect(HistoryFrom, TimeTradeServer() + 86400))
     {
      Print("Supervisor: no se pudo leer el historial (HistorySelect).");
      return false;
     }
   int total = HistoryDealsTotal();
   long keys[][2];
   ArrayResize(keys, total);
   for(int i = 0; i < total; i++)
     {
      ulong ticket = HistoryDealGetTicket(i);
      keys[i][0] = HistoryDealGetInteger(ticket, DEAL_TIME_MSC);
      keys[i][1] = (long)ticket;
     }
   ArraySort(keys);   // por hora en milisegundos

   double after[];
   ArrayResize(after, total);
   double running = AccountInfoDouble(ACCOUNT_BALANCE);
   for(int i = total - 1; i >= 0; i--)
     {
      after[i] = running;
      running -= BalanceDelta((ulong)keys[i][1]);
     }
   if(HistoryFrom <= WHOLE_HISTORY && total > 0 && MathAbs(running) > 0.01)
      PrintFormat("Supervisor: AVISO: el balance reconstruido antes del primer deal es %.2f y "
                  "debería ser 0. El %% de riesgo de las operaciones importadas puede estar "
                  "desplazado (el riesgo en dinero no cambia).", running);

   int limit = (int)MathMax(1, MathMin(DealsPerPost, SERVER_MAX_DEALS));
   datetime to_server = DealsTo();
   string events = "";
   ulong batch[];
   ArrayResize(batch, limit);
   int count = 0;
   int skipped = 0;
   for(int i = 0; i < total; i++)
     {
      if(IsStopped())
         return false;
      ulong ticket = (ulong)keys[i][1];
      datetime t = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      if(t < HistoryFrom || t > to_server)
         continue;
      string symbol;
      string json = SupervisorDealJson(ticket, g_login, g_tzOffset, JNum(after[i], 2), symbol);
      if(json == "")
        {
         skipped++;   // depósitos, retiradas, comisiones sueltas: no son operaciones
         continue;
        }
      if(events != "")
         events += ",";
      events += json;
      batch[count] = ticket;
      count++;
      if(count == limit)
        {
         if(!PostDeals(events, batch, count))
            return false;
         PrintFormat("Supervisor: deals enviados hasta %s: %I64d", TimeToString(t), g_dealsSent);
         events = "";
         count = 0;
        }
     }
   if(count > 0 && !PostDeals(events, batch, count))
      return false;
   PrintFormat("Supervisor: %d movimientos que no son operaciones (depósitos, etc.) no se envían.",
               skipped);
   return true;
  }

//+------------------------------------------------------------------+
//| Programa                                                         |
//+------------------------------------------------------------------+
void PrintSummary()
  {
   PrintFormat("Supervisor: RESUMEN velas: %I64d enviadas, %I64d nuevas, %I64d ya estaban; "
               "%d símbolo(s) con velas incompletas.", g_barsSent, g_barsInserted,
               g_barsSent - g_barsInserted, g_symbolsIncomplete);
   PrintFormat("Supervisor: RESUMEN deals: %I64d enviados, %I64d nuevos, %I64d duplicados, "
               "%I64d rechazados.", g_dealsSent, g_dealsAccepted, g_dealsDuplicate,
               g_dealsRejected);
   if(g_aborted)
      Print("Supervisor: la importación NO terminó. Vuelve a ejecutar el script cuando se "
            "resuelva el problema: lo ya enviado contará como duplicado.");
   else
      Print("Supervisor: importación terminada. El supervisor procesará los deals en segundo "
            "plano. Registra los despliegues de cada magic con sus fechas y ejecuta "
            "'supervisor-cli backfill' en el VPS (ver mt5/README.md).");
  }

void OnStart()
  {
   if(ApiKey == "" || StringFind(ApiKey, "tsk_") != 0)
     {
      Print("Supervisor: falta la ApiKey (debe empezar por tsk_).");
      return;
     }
   if(StringFind(ApiBaseUrl, "https://") != 0)
     {
      Print("Supervisor: ApiBaseUrl debe empezar por https:// para proteger la API key.");
      return;
     }
   g_login = AccountInfoInteger(ACCOUNT_LOGIN);
   g_tzOffset = SupervisorTzOffset();
   PrintFormat("Supervisor Import %s: cuenta %I64d, solo lectura. Desfase servidor-UTC: %d s "
               "(GMT%+.1f).", IMPORT_VERSION, g_login, g_tzOffset, g_tzOffset / 3600.0);
   if(g_tzOffset == 0)
      Print("Supervisor: el servidor está en GMT+0 (lo normal en Exness): las horas del "
            "historial son exactas, sin horario de verano.");
   else
      Print("Supervisor: AVISO: el servidor no está en GMT+0. Se aplica el desfase de HOY a "
            "todo el historial: si el broker cambia de horario en verano, las operaciones del "
            "otro periodo quedarán desplazadas 1 hora. El supervisor guarda también la hora "
            "original del broker (time_server_msc) para poder corregirlo.");

   string response;
   int status = HttpRequest("GET", "/health", "", response);
   if(status != 200)
     {
      PrintFormat("Supervisor: el supervisor no responde en %s/health (HTTP %d). Revisa la URL "
                  "y la lista de WebRequest.", ApiBaseUrl, status);
      return;
     }

   datetime first_deal = CollectSymbols();
   PrintFormat("Supervisor: %d símbolo(s) operados; primer deal %s (hora del servidor).",
               ArraySize(g_symbols), first_deal > 0 ? TimeToString(first_deal) : "ninguno");

   // Primero las velas: cuando el worker procese los deals, el DNA y el análisis ya
   // encuentran el contexto de mercado anterior a cada entrada.
   if(SendBars && !SendAllBars(first_deal))
      g_aborted = true;
   if(!g_aborted && SendDeals && !SendAllDeals())
      g_aborted = true;
   PrintSummary();
  }
//+------------------------------------------------------------------+
