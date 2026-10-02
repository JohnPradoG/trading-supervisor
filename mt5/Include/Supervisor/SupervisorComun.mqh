//+------------------------------------------------------------------+
//| SupervisorComun.mqh                                              |
//| Trading Supervisor - código común de SOLO LECTURA                |
//|                                                                  |
//| Lo usan el EA Monitor (Experts\SupervisorMonitor.mq5) y el       |
//| script de importación (Scripts\SupervisorImportHistory.mq5) para |
//| que los dos construyan exactamente el mismo JSON de cada deal.   |
//|                                                                  |
//| Copiar en MQL5\Include\Supervisor\SupervisorComun.mqh.           |
//|                                                                  |
//| No incluye la clase CTrade ni llama a OrderSend: solo lee el     |
//| historial y da formato a los datos.                              |
//+------------------------------------------------------------------+
#ifndef SUPERVISOR_COMUN_MQH
#define SUPERVISOR_COMUN_MQH

//+------------------------------------------------------------------+
//| Tiempo                                                           |
//+------------------------------------------------------------------+
int SupervisorTzOffset()
  {
   // Desfase entre la hora del servidor del broker y UTC, redondeado a 15 min.
   long raw = (long)TimeTradeServer() - (long)TimeGMT();
   return (int)(MathRound(raw / 900.0) * 900);
  }

string IsoUtc(datetime utc, int ms = 0)
  {
   string s = TimeToString(utc, TIME_DATE | TIME_SECONDS);   // 2026.10.02 12:00:00
   StringReplace(s, ".", "-");
   StringReplace(s, " ", "T");
   if(ms > 0)
      s += StringFormat(".%03d", ms);
   return s + "Z";
  }

//+------------------------------------------------------------------+
//| JSON                                                             |
//+------------------------------------------------------------------+
string JStr(string value)
  {
   string out = value;
   StringReplace(out, "\\", "\\\\");
   StringReplace(out, "\"", "\\\"");
   StringReplace(out, "\r", "\\r");
   StringReplace(out, "\n", "\\n");
   StringReplace(out, "\t", "\\t");
   return "\"" + out + "\"";
  }

string JNum(double value, int digits)
  {
   string s = DoubleToString(value, digits);
   // Quitar ceros finales: 20000.50000 -> 20000.5
   if(StringFind(s, ".") >= 0)
     {
      while(StringLen(s) > 1 && StringSubstr(s, StringLen(s) - 1) == "0")
         s = StringSubstr(s, 0, StringLen(s) - 1);
      if(StringSubstr(s, StringLen(s) - 1) == ".")
         s = StringSubstr(s, 0, StringLen(s) - 1);
     }
   return s;
  }

string JLevel(double value, int digits) { return value > 0 ? JNum(value, digits) : "null"; }

//+------------------------------------------------------------------+
//| Mapeo de enumeraciones de MT5                                    |
//+------------------------------------------------------------------+
string EntryName(long entry)
  {
   switch((int)entry)
     {
      case DEAL_ENTRY_IN:    return "IN";
      case DEAL_ENTRY_OUT:   return "OUT";
      case DEAL_ENTRY_INOUT: return "INOUT";
      case DEAL_ENTRY_OUT_BY:return "OUT_BY";
     }
   return "";
  }

string ReasonName(long reason)
  {
   switch((int)reason)
     {
      case DEAL_REASON_CLIENT:   return "CLIENT";
      case DEAL_REASON_MOBILE:   return "MOBILE";
      case DEAL_REASON_WEB:      return "WEB";
      case DEAL_REASON_EXPERT:   return "EXPERT";
      case DEAL_REASON_SL:       return "SL";
      case DEAL_REASON_TP:       return "TP";
      case DEAL_REASON_SO:       return "SO";
      case DEAL_REASON_ROLLOVER: return "ROLLOVER";
      case DEAL_REASON_VMARGIN:  return "VMARGIN";
      case DEAL_REASON_SPLIT:    return "SPLIT";
     }
   return "OTHER";
  }

string SymbolSpecJson(string symbol)
  {
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double tick_size = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double tick_value = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double contract = SymbolInfoDouble(symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(point <= 0 || tick_size <= 0 || contract <= 0)
      return "null";
   return "{\"digits\":" + IntegerToString(digits) +
          ",\"point\":" + JNum(point, 10) +
          ",\"tick_size\":" + JNum(tick_size, 10) +
          ",\"tick_value\":" + JNum(tick_value, 4) +
          ",\"contract_size\":" + JNum(contract, 4) + "}";
  }

//+------------------------------------------------------------------+
//| Deal -> JSON (contrato DEAL de /v1/ingest/events)                |
//|                                                                  |
//| El deal tiene que estar en la lista cargada con HistorySelect.   |
//| No se llama a HistoryDealSelect: esa función vacía la lista de   |
//| HistorySelect y dejaría sin recorrer el resto de deals.          |
//|                                                                  |
//| Devuelve "" si no es una operación de compra/venta con posición  |
//| (depósitos, comisiones sueltas, etc.) o si el deal no está en la |
//| lista. `symbol` devuelve el símbolo del deal.                    |
//| `balance_after`: "" en el EA (envía fotos de cuenta); el script  |
//| de importación pasa el balance reconstruido tras el deal.        |
//+------------------------------------------------------------------+
string SupervisorDealJson(ulong ticket, long login, int tz_offset, string balance_after,
                          string &symbol)
  {
   symbol = "";
   long type = 0;
   if(ticket == 0 || !HistoryDealGetInteger(ticket, DEAL_TYPE, type))
      return "";
   if(type != DEAL_TYPE_BUY && type != DEAL_TYPE_SELL)
      return "";
   string entry = EntryName(HistoryDealGetInteger(ticket, DEAL_ENTRY));
   if(entry == "")
      return "";
   long position_id = HistoryDealGetInteger(ticket, DEAL_POSITION_ID);
   if(position_id <= 0)
      return "";
   symbol = HistoryDealGetString(ticket, DEAL_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(digits <= 0)
      digits = 5;
   long time_msc = HistoryDealGetInteger(ticket, DEAL_TIME_MSC);
   datetime time_server = (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);

   string json = "{\"type\":\"DEAL\"" +
      ",\"account_login\":" + IntegerToString(login) +
      ",\"symbol\":" + JStr(symbol) +
      ",\"magic\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_MAGIC)) +
      ",\"position_id\":" + IntegerToString(position_id) +
      ",\"time_utc\":" + JStr(IsoUtc((datetime)(time_server - tz_offset), (int)(time_msc % 1000))) +
      ",\"time_server_msc\":" + IntegerToString(time_msc) +
      ",\"deal_ticket\":" + IntegerToString((long)ticket) +
      ",\"order_ticket\":" + IntegerToString(HistoryDealGetInteger(ticket, DEAL_ORDER)) +
      ",\"deal_type\":" + JStr(type == DEAL_TYPE_BUY ? "BUY" : "SELL") +
      ",\"entry\":" + JStr(entry) +
      ",\"reason\":" + JStr(ReasonName(HistoryDealGetInteger(ticket, DEAL_REASON))) +
      ",\"volume\":" + JNum(HistoryDealGetDouble(ticket, DEAL_VOLUME), 4) +
      ",\"price\":" + JNum(HistoryDealGetDouble(ticket, DEAL_PRICE), digits) +
      ",\"profit\":" + JNum(HistoryDealGetDouble(ticket, DEAL_PROFIT), 4) +
      ",\"commission\":" + JNum(HistoryDealGetDouble(ticket, DEAL_COMMISSION), 4) +
      ",\"swap\":" + JNum(HistoryDealGetDouble(ticket, DEAL_SWAP), 4) +
      ",\"fee\":" + JNum(HistoryDealGetDouble(ticket, DEAL_FEE), 4) +
      ",\"comment\":" + JStr(StringSubstr(HistoryDealGetString(ticket, DEAL_COMMENT), 0, 64)) +
      ",\"sl\":" + JLevel(HistoryDealGetDouble(ticket, DEAL_SL), digits) +
      ",\"tp\":" + JLevel(HistoryDealGetDouble(ticket, DEAL_TP), digits);
   if(balance_after != "")
      json += ",\"balance_after\":" + balance_after;
   json += ",\"symbol_spec\":" + SymbolSpecJson(symbol) + "}";
   return json;
  }

//+------------------------------------------------------------------+
//| Respuesta de /v1/ingest/events                                   |
//+------------------------------------------------------------------+
string StatusForIndex(string response, int index, int &search_from)
  {
   // La API responde {"index":N,"status":"accepted|duplicate|rejected",...} en orden.
   string marker = "\"index\":" + IntegerToString(index) + ",\"status\":\"";
   int p = StringFind(response, marker, search_from);
   if(p < 0)
      return "";
   int start = p + StringLen(marker);
   int end = StringFind(response, "\"", start);
   if(end < 0)
      return "";
   search_from = end;
   return StringSubstr(response, start, end - start);
  }

#endif
