//+------------------------------------------------------------------+
//| SupervisorMonitor.mq5                                            |
//| Trading Supervisor - EA Monitor de SOLO LECTURA                  |
//|                                                                  |
//| Observa toda la cuenta y envía al Trading Supervisor:            |
//|   - cada deal (aperturas, aumentos, parciales, cierres)          |
//|   - cambios de SL/TP de posiciones abiertas                      |
//|   - foto de posiciones abiertas cada 5 min (reconciliación)      |
//|   - latido cada 30 s y balance/equity cada minuto                |
//|   - velas M1 cerradas de los símbolos operados                   |
//|                                                                  |
//| Este EA NO abre, modifica ni cierra operaciones. No incluye la   |
//| clase CTrade ni llama a OrderSend en ningún punto.               |
//|                                                                  |
//| Los eventos de trading se escriben primero en una cola en disco  |
//| (MQL5\Files\Supervisor\outbox) y solo se borran cuando el        |
//| servidor confirma "accepted" o "duplicate". Si internet o el     |
//| supervisor caen, nada se pierde: se reenvía al volver.           |
//|                                                                  |
//| Necesita MQL5\Include\Supervisor\SupervisorComun.mqh: el JSON    |
//| de cada deal es el mismo que envía el script de importación.     |
//+------------------------------------------------------------------+
#property copyright "Trading Supervisor"
#property version   "1.01"
#property description "Monitor de solo lectura: envía operaciones, SL/TP, balance y velas al Trading Supervisor."

#include <Supervisor\SupervisorComun.mqh>

#define EA_VERSION        "1.0.1"
#define OUTBOX_DIR        "Supervisor\\outbox\\"
#define REJECTED_DIR      "Supervisor\\rejected\\"
#define MAX_BATCH         100
#define MAX_BARS_PER_POST 1000

//--- Parámetros
input string ApiBaseUrl          = "https://johntrading.duckdns.org"; // URL del supervisor (sin / final)
input string ApiKey              = "";      // API key de este terminal (supervisor-cli create-api-key)
input int    BackfillHours       = 48;      // Horas de historial a revisar al arrancar
input int    BarsBackfillHours   = 24;      // Horas de velas M1 a enviar la primera vez
input string ExtraBarSymbols     = "";      // Símbolos extra para velas, separados por coma
input int    HttpTimeoutMs       = 5000;    // Tiempo máximo por petición HTTP
input bool   VerboseLog          = false;   // Escribir cada envío en el diario

//--- Estado
long     g_login = 0;
int      g_tzOffset = 0;            // segundos: hora del servidor - UTC
datetime g_lastDealScan = 0;        // hora de servidor del último barrido de deals
ulong    g_seenDeals[];             // deals ya encolados en esta sesión
ulong    g_posIds[];                // posiciones abiertas conocidas
double   g_posSl[];
double   g_posTp[];
string   g_barSymbols[];            // símbolos de los que se envían velas
datetime g_nextSend = 0;
int      g_backoff = 0;             // segundos de espera tras un fallo
datetime g_lastHeartbeat = 0;
datetime g_lastSnapshotMinute = 0;
datetime g_lastPositionsSnapshot = 0;
datetime g_lastBars = 0;
bool     g_authFailed = false;

//+------------------------------------------------------------------+
//| Utilidades de tiempo                                             |
//+------------------------------------------------------------------+
void RefreshTzOffset()
  {
   // Desfase entre la hora del servidor del broker y UTC, redondeado a 15 min.
   g_tzOffset = SupervisorTzOffset();
  }

datetime ServerToUtc(datetime server_time) { return server_time - g_tzOffset; }

//+------------------------------------------------------------------+
//| Archivos (UTF-8)                                                 |
//+------------------------------------------------------------------+
bool WriteText(string path, string text)
  {
   int h = FileOpen(path, FILE_WRITE | FILE_BIN);
   if(h == INVALID_HANDLE)
     {
      PrintFormat("Supervisor: no se pudo escribir %s (error %d)", path, GetLastError());
      return false;
     }
   uchar bytes[];
   int n = StringToCharArray(text, bytes, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0)
      FileWriteArray(h, bytes, 0, n - 1);   // sin el \0 final
   FileClose(h);
   return true;
  }

string ReadText(string path)
  {
   int h = FileOpen(path, FILE_READ | FILE_BIN);
   if(h == INVALID_HANDLE)
      return "";
   uchar bytes[];
   int size = (int)FileSize(h);
   if(size > 0)
      FileReadArray(h, bytes, 0, size);
   FileClose(h);
   return size > 0 ? CharArrayToString(bytes, 0, size, CP_UTF8) : "";
  }

string SafeName(string key)
  {
   string s = key;
   StringReplace(s, ":", "_");
   StringReplace(s, ".", "p");
   StringReplace(s, "-", "m");
   return s;
  }

void Enqueue(string key, string json)
  {
   // El nombre del archivo es la clave: volver a encolar el mismo hecho sobrescribe el
   // mismo archivo, nunca crea un duplicado.
   if(WriteText(OUTBOX_DIR + SafeName(key) + ".json", json) && VerboseLog)
      PrintFormat("Supervisor: encolado %s", key);
  }

//+------------------------------------------------------------------+
//| Listas simples                                                   |
//+------------------------------------------------------------------+
bool SeenDeal(ulong ticket)
  {
   for(int i = ArraySize(g_seenDeals) - 1; i >= 0; i--)
      if(g_seenDeals[i] == ticket)
         return true;
   return false;
  }

void RememberDeal(ulong ticket)
  {
   int n = ArraySize(g_seenDeals);
   if(n >= 5000)   // conservar solo los más recientes
     {
      ArrayRemove(g_seenDeals, 0, 1000);
      n = ArraySize(g_seenDeals);
     }
   ArrayResize(g_seenDeals, n + 1);
   g_seenDeals[n] = ticket;
  }

void AddBarSymbol(string symbol)
  {
   if(symbol == "")
      return;
   for(int i = 0; i < ArraySize(g_barSymbols); i++)
      if(g_barSymbols[i] == symbol)
         return;
   int n = ArraySize(g_barSymbols);
   ArrayResize(g_barSymbols, n + 1);
   g_barSymbols[n] = symbol;
   SymbolSelect(symbol, true);
  }

//+------------------------------------------------------------------+
//| Eventos de trading                                               |
//+------------------------------------------------------------------+
void ProcessDeal(ulong ticket)
  {
   if(ticket == 0 || SeenDeal(ticket))
      return;
   // El deal debe estar en la lista de HistorySelect. No se usa HistoryDealSelect: vacía esa
   // lista y el barrido de ScanDeals se quedaría en el primer deal nuevo.
   long found = 0;
   if(!HistoryDealGetInteger(ticket, DEAL_TICKET, found))
      return;
   string symbol;
   string json = SupervisorDealJson(ticket, g_login, g_tzOffset, "", symbol);
   RememberDeal(ticket);   // depósitos, comisiones sueltas, etc. tampoco se vuelven a mirar
   if(json == "")
      return;
   Enqueue("deal:" + IntegerToString(g_login) + ":" + IntegerToString((long)ticket), json);
   AddBarSymbol(symbol);
  }

void ScanDeals(datetime from_server)
  {
   datetime to_server = TimeTradeServer() + 3600;
   if(!HistorySelect(from_server, to_server))
      return;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
      ProcessDeal(HistoryDealGetTicket(i));
  }

int FindPosition(ulong id)
  {
   for(int i = 0; i < ArraySize(g_posIds); i++)
      if(g_posIds[i] == id)
         return i;
   return -1;
  }

void CheckPositions()
  {
   ulong ids[];
   double sls[];
   double tps[];
   int total = PositionsTotal();
   ArrayResize(ids, total);
   ArrayResize(sls, total);
   ArrayResize(tps, total);
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      ulong id = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
      string symbol = PositionGetString(POSITION_SYMBOL);
      double sl = PositionGetDouble(POSITION_SL);
      double tp = PositionGetDouble(POSITION_TP);
      ids[i] = id;
      sls[i] = sl;
      tps[i] = tp;
      AddBarSymbol(symbol);

      int k = FindPosition(id);
      if(k < 0)
         continue;   // posición nueva: su SL/TP inicial viaja con el deal de apertura
      if(MathAbs(g_posSl[k] - sl) < 1e-10 && MathAbs(g_posTp[k] - tp) < 1e-10)
         continue;

      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long now_msc = (long)TimeTradeServer() * 1000 + (long)(GetTickCount() % 1000);
      string sl_txt = sl > 0 ? JNum(sl, digits) : "0";
      string tp_txt = tp > 0 ? JNum(tp, digits) : "0";
      string key = "mod:" + IntegerToString(g_login) + ":" + IntegerToString((long)id) + ":" +
                   IntegerToString(now_msc) + ":" + sl_txt + ":" + tp_txt;
      string json = "{\"type\":\"POSITION_MODIFY\"" +
         ",\"account_login\":" + IntegerToString(g_login) +
         ",\"symbol\":" + JStr(symbol) +
         ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
         ",\"position_id\":" + IntegerToString((long)id) +
         ",\"time_utc\":" + JStr(IsoUtc(TimeGMT())) +
         ",\"time_server_msc\":" + IntegerToString(now_msc) +
         ",\"sl\":" + JLevel(sl, digits) +
         ",\"tp\":" + JLevel(tp, digits) + "}";
      Enqueue(key, json);
     }
   ArrayCopy(g_posIds, ids);
   ArrayResize(g_posIds, total);
   ArrayCopy(g_posSl, sls);
   ArrayResize(g_posSl, total);
   ArrayCopy(g_posTp, tps);
   ArrayResize(g_posTp, total);
  }

void EnqueuePositionsSnapshot()
  {
   datetime now_utc = TimeGMT();
   string items = "";
   int total = PositionsTotal();
   for(int i = 0; i < total; i++)
     {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      string symbol = PositionGetString(POSITION_SYMBOL);
      int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
      long ptype = PositionGetInteger(POSITION_TYPE);
      datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      if(items != "")
         items += ",";
      items += "{\"position_id\":" + IntegerToString(PositionGetInteger(POSITION_IDENTIFIER)) +
               ",\"symbol\":" + JStr(symbol) +
               ",\"magic\":" + IntegerToString(PositionGetInteger(POSITION_MAGIC)) +
               ",\"direction\":" + JStr(ptype == POSITION_TYPE_BUY ? "BUY" : "SELL") +
               ",\"volume\":" + JNum(PositionGetDouble(POSITION_VOLUME), 4) +
               ",\"price_open\":" + JNum(PositionGetDouble(POSITION_PRICE_OPEN), digits) +
               ",\"sl\":" + JLevel(PositionGetDouble(POSITION_SL), digits) +
               ",\"tp\":" + JLevel(PositionGetDouble(POSITION_TP), digits) +
               ",\"time_open_utc\":" + JStr(IsoUtc(ServerToUtc(opened))) +
               ",\"profit\":" + JNum(PositionGetDouble(POSITION_PROFIT), 4) + "}";
     }
   string minute = TimeToString(now_utc, TIME_DATE | TIME_MINUTES);
   StringReplace(minute, ".", "");
   StringReplace(minute, " ", "T");
   StringReplace(minute, ":", "");
   string key = "possnap:" + IntegerToString(g_login) + ":" + minute;
   string json = "{\"type\":\"POSITIONS_SNAPSHOT\"" +
      ",\"account_login\":" + IntegerToString(g_login) +
      ",\"time_utc\":" + JStr(IsoUtc(now_utc)) +
      ",\"positions\":[" + items + "]}";
   Enqueue(key, json);
  }

//+------------------------------------------------------------------+
//| HTTP                                                             |
//+------------------------------------------------------------------+
int HttpPost(string path, string body, string &response)
  {
   string headers = "Content-Type: application/json\r\nX-API-Key: " + ApiKey + "\r\n";
   char data[];
   char result[];
   string result_headers;
   int n = StringToCharArray(body, data, 0, WHOLE_ARRAY, CP_UTF8);
   if(n > 0)
      ArrayResize(data, n - 1);   // sin el \0 final
   ResetLastError();
   int status = WebRequest("POST", ApiBaseUrl + path, headers, HttpTimeoutMs, data, result,
                           result_headers);
   if(status == -1)
     {
      int err = GetLastError();
      if(err == 4014)
         PrintFormat("Supervisor: añade %s en Herramientas > Opciones > Asesores > "
                     "'Permitir WebRequest para las URL listadas'", ApiBaseUrl);
      else
         PrintFormat("Supervisor: fallo de red en %s (error %d)", path, err);
      response = "";
      return -1;
     }
   response = CharArrayToString(result, 0, WHOLE_ARRAY, CP_UTF8);
   if(status == 401)
     {
      if(!g_authFailed)
         Print("Supervisor: API key rechazada (401). Revisa el parámetro ApiKey.");
      g_authFailed = true;
     }
   else if(status >= 200 && status < 300)
      g_authFailed = false;
   return status;
  }

void FlushOutbox()
  {
   string files[];
   string name;
   long search = FileFindFirst(OUTBOX_DIR + "*.json", name);
   if(search == INVALID_HANDLE)
      return;
   do
     {
      int n = ArraySize(files);
      ArrayResize(files, n + 1);
      files[n] = name;
     }
   while(ArraySize(files) < MAX_BATCH && FileFindNext(search, name));
   FileFindClose(search);
   if(ArraySize(files) == 0)
      return;

   string events = "";
   string included[];
   for(int i = 0; i < ArraySize(files); i++)
     {
      string text = ReadText(OUTBOX_DIR + files[i]);
      if(text == "")
        {
         FileDelete(OUTBOX_DIR + files[i]);   // archivo vacío o corrupto
         continue;
        }
      if(events != "")
         events += ",";
      events += text;
      int k = ArraySize(included);
      ArrayResize(included, k + 1);
      included[k] = files[i];
     }
   if(ArraySize(included) == 0)
      return;

   string body = "{\"schema_version\":1,\"sent_at\":" + JStr(IsoUtc(TimeGMT())) +
                 ",\"ea_version\":" + JStr(EA_VERSION) + ",\"events\":[" + events + "]}";
   string response;
   int status = HttpPost("/v1/ingest/events", body, response);
   if(status != 200)
     {
      g_backoff = (int)MathMin(MathMax(g_backoff * 2, 2), 120);
      if(status > 0)
         PrintFormat("Supervisor: lote no aceptado (HTTP %d): %s", status,
                     StringSubstr(response, 0, 300));
      return;
     }
   g_backoff = 0;

   int accepted = 0, duplicate = 0, rejected = 0, cursor = 0;
   for(int i = 0; i < ArraySize(included); i++)
     {
      string st = StatusForIndex(response, i, cursor);
      if(st == "accepted" || st == "duplicate")
        {
         FileDelete(OUTBOX_DIR + included[i]);
         if(st == "accepted")
            accepted++;
         else
            duplicate++;
        }
      else if(st == "rejected")
        {
         // Se aparta para revisarlo a mano; no se reintenta en bucle.
         FileMove(OUTBOX_DIR + included[i], 0, REJECTED_DIR + included[i], FILE_REWRITE);
         rejected++;
        }
     }
   if(rejected > 0)
      PrintFormat("Supervisor: %d evento(s) rechazados, apartados en MQL5\\Files\\%s. Respuesta: %s",
                  rejected, REJECTED_DIR, StringSubstr(response, 0, 500));
   if(VerboseLog || rejected > 0)
      PrintFormat("Supervisor: lote enviado: %d aceptados, %d duplicados, %d rechazados",
                  accepted, duplicate, rejected);
  }

void SendHeartbeat()
  {
   string body = "{\"sent_at\":" + JStr(IsoUtc(TimeGMT())) +
      ",\"ea_version\":" + JStr(EA_VERSION) +
      ",\"terminal_connected\":" + (TerminalInfoInteger(TERMINAL_CONNECTED) ? "true" : "false") +
      ",\"trade_allowed\":" + (TerminalInfoInteger(TERMINAL_TRADE_ALLOWED) ? "true" : "false") +
      ",\"info\":{\"build\":" + IntegerToString(TerminalInfoInteger(TERMINAL_BUILD)) +
      ",\"tz_offset_seconds\":" + IntegerToString(g_tzOffset) +
      ",\"open_positions\":" + IntegerToString(PositionsTotal()) + "}}";
   string response;
   HttpPost("/v1/ingest/heartbeat", body, response);
  }

void SendAccountSnapshot(datetime minute_utc)
  {
   string body = "{\"account_login\":" + IntegerToString(g_login) +
      ",\"time_utc\":" + JStr(IsoUtc(minute_utc)) +
      ",\"balance\":" + JNum(AccountInfoDouble(ACCOUNT_BALANCE), 2) +
      ",\"equity\":" + JNum(AccountInfoDouble(ACCOUNT_EQUITY), 2) +
      ",\"margin\":" + JNum(AccountInfoDouble(ACCOUNT_MARGIN), 2) +
      ",\"free_margin\":" + JNum(AccountInfoDouble(ACCOUNT_MARGIN_FREE), 2) +
      ",\"open_positions\":" + IntegerToString(PositionsTotal()) + "}";
   string response;
   HttpPost("/v1/ingest/account-snapshot", body, response);
  }

void SendBars(string symbol)
  {
   string gv = "TS_LB_" + IntegerToString(g_login) + "_" + symbol;
   if(StringLen(gv) > 63)
      gv = StringSubstr(gv, 0, 63);
   datetime last_sent = GlobalVariableCheck(gv) ? (datetime)GlobalVariableGet(gv) : 0;
   datetime last_closed = iTime(symbol, PERIOD_M1, 1);   // la vela 0 sigue abierta
   if(last_closed <= 0 || last_closed <= last_sent)
      return;
   datetime from = last_sent > 0 ? last_sent + 60 : last_closed - BarsBackfillHours * 3600;

   MqlRates rates[];
   int copied = CopyRates(symbol, PERIOD_M1, from, last_closed, rates);
   if(copied <= 0)
      return;
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   for(int start = 0; start < copied; start += MAX_BARS_PER_POST)
     {
      int stop = (int)MathMin(start + MAX_BARS_PER_POST, copied);
      string items = "";
      for(int i = start; i < stop; i++)
        {
         if(items != "")
            items += ",";
         items += "{\"time_utc\":" + JStr(IsoUtc(ServerToUtc(rates[i].time))) +
                  ",\"open\":" + JNum(rates[i].open, digits) +
                  ",\"high\":" + JNum(rates[i].high, digits) +
                  ",\"low\":" + JNum(rates[i].low, digits) +
                  ",\"close\":" + JNum(rates[i].close, digits) +
                  ",\"tick_volume\":" + IntegerToString(rates[i].tick_volume) +
                  ",\"spread\":" + IntegerToString(rates[i].spread) + "}";
        }
      string body = "{\"symbol\":" + JStr(symbol) + ",\"timeframe\":\"M1\",\"bars\":[" + items + "]}";
      string response;
      int status = HttpPost("/v1/ingest/bars", body, response);
      if(status != 200)
        {
         if(status > 0)
            PrintFormat("Supervisor: velas %s no aceptadas (HTTP %d): %s", symbol, status,
                        StringSubstr(response, 0, 300));
         return;   // se reintenta en el próximo ciclo desde la última vela confirmada
        }
      GlobalVariableSet(gv, (double)rates[stop - 1].time);
     }
  }

//+------------------------------------------------------------------+
//| Ciclo de vida                                                    |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(ApiKey == "" || StringFind(ApiKey, "tsk_") != 0)
     {
      Print("Supervisor: falta la ApiKey (debe empezar por tsk_).");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(StringFind(ApiBaseUrl, "https://") != 0)
     {
      Print("Supervisor: ApiBaseUrl debe empezar por https:// para proteger la API key.");
      return INIT_PARAMETERS_INCORRECT;
     }
   g_login = AccountInfoInteger(ACCOUNT_LOGIN);
   RefreshTzOffset();
   FolderCreate("Supervisor");
   FolderCreate("Supervisor\\outbox");
   FolderCreate("Supervisor\\rejected");

   string extra[];
   int n = StringSplit(ExtraBarSymbols, ',', extra);
   for(int i = 0; i < n; i++)
     {
      string s = extra[i];
      StringTrimLeft(s);
      StringTrimRight(s);
      AddBarSymbol(s);
     }

   // Al arrancar se revisa el historial reciente: el servidor descarta lo ya recibido.
   ScanDeals(TimeTradeServer() - BackfillHours * 3600);
   g_lastDealScan = TimeTradeServer();
   CheckPositions();
   EnqueuePositionsSnapshot();
   g_lastPositionsSnapshot = TimeGMT();

   EventSetTimer(1);
   PrintFormat("Supervisor Monitor %s iniciado: cuenta %I64d, desfase servidor-UTC %d s, solo lectura.",
               EA_VERSION, g_login, g_tzOffset);
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTradeTransaction(const MqlTradeTransaction &trans, const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   // Reacción inmediata; el barrido del temporizador es la red de seguridad.
   if(trans.type == TRADE_TRANSACTION_DEAL_ADD && trans.deal > 0)
     {
      if(HistorySelect(TimeTradeServer() - 86400, TimeTradeServer() + 3600))
         ProcessDeal(trans.deal);
     }
   else if(trans.type == TRADE_TRANSACTION_POSITION)
      CheckPositions();
  }

void OnTimer()
  {
   datetime now = TimeGMT();
   if(now % 60 == 0)
      RefreshTzOffset();

   // 1. Deals: ventana con margen de 10 min por si el historial llega con retraso.
   ScanDeals(g_lastDealScan - 600);
   g_lastDealScan = TimeTradeServer();

   // 2. Cambios de SL/TP.
   CheckPositions();

   // 3. Foto de posiciones cada 5 min.
   if(now - g_lastPositionsSnapshot >= 300)
     {
      EnqueuePositionsSnapshot();
      g_lastPositionsSnapshot = now;
     }

   // Con la API key rechazada no tiene sentido insistir cada segundo.
   if(g_authFailed && now < g_nextSend)
      return;

   // 4. Enviar la cola.
   if(now >= g_nextSend)
     {
      FlushOutbox();
      g_nextSend = now + (g_authFailed ? 60 : (int)MathMax(g_backoff, 2));
     }

   // 5. Latido cada 30 s.
   if(now - g_lastHeartbeat >= 30)
     {
      SendHeartbeat();
      g_lastHeartbeat = now;
     }

   // 6. Balance/equity una vez por minuto.
   datetime minute = now - (now % 60);
   if(minute != g_lastSnapshotMinute)
     {
      SendAccountSnapshot(minute);
      g_lastSnapshotMinute = minute;
     }

   // 7. Velas M1 cerradas, una vez por minuto (a los 5 s para que la vela ya esté cerrada).
   if(now % 60 >= 5 && now - g_lastBars >= 55)
     {
      for(int i = 0; i < ArraySize(g_barSymbols); i++)
         SendBars(g_barSymbols[i]);
      g_lastBars = now;
     }
  }
//+------------------------------------------------------------------+
