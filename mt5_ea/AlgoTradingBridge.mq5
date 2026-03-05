//+------------------------------------------------------------------+
//| AlgoTradingBridge.mq5                                            |
//| Space Traders Academy - Algo Trading Agent Bridge v2.0           |
//|                                                                  |
//| Questo EA gira nel tuo MT5 su Mac/Windows.                       |
//| Comunica con il bot Python su Linux via HTTP (tunnel ngrok).     |
//|                                                                  |
//| INSTALLAZIONE:                                                   |
//| 1. Copia in: MT5/MQL5/Experts/AlgoTradingBridge.mq5             |
//| 2. Strumenti → Opzioni → Consulenti Esperti                      |
//|    → Abilita "Consenti WebRequest per i seguenti URL"            |
//|    → Aggiungi l'URL del tunnel (es. https://xxxx.ngrok-free.app) |
//| 3. Compila (F7) e trascina su grafico XAUUSD H1                  |
//| 4. Nei parametri EA inserisci l'URL completo del server          |
//|                                                                  |
//| TARGET: XAUUSD (Gold CFD) e BTCUSD (Bitcoin CFD)                 |
//+------------------------------------------------------------------+
#property copyright "Algo Trading Agent v2.0"
#property version   "2.00"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

// ─── Parametri EA ───────────────────────────────────────────────────────────
input string ServerURL     = "http://YOUR_TUNNEL_URL";  // URL completo server (con http://)
input int    PollInterval  = 5;                          // Secondi tra poll
input bool   EnableTrading = true;                       // Abilita ordini reali
input bool   SendXAUUSD    = true;                       // Invia dati XAUUSD (Gold)
input bool   SendBTCUSD    = true;                       // Invia dati BTCUSD (Bitcoin)
input bool   SendEURUSD    = false;                      // Invia dati EURUSD (opzionale)
input ENUM_TIMEFRAMES TF   = PERIOD_H1;                  // Timeframe candles

// ─── Variabili globali ───────────────────────────────────────────────────────
string BaseURL;
int    timerCount    = 0;
int    heartbeatEvery = 12;  // Ogni 12 tick = ogni 60s con PollInterval=5

//+------------------------------------------------------------------+
int OnInit()
{
   BaseURL = ServerURL;
   // Rimuovi slash finale
   if(StringLen(BaseURL) > 0 && StringSubstr(BaseURL, StringLen(BaseURL)-1, 1) == "/")
      BaseURL = StringSubstr(BaseURL, 0, StringLen(BaseURL)-1);

   EventSetTimer(PollInterval);
   Print("[Bridge] v2.0 avviato. Server: ", BaseURL);
   Print("[Bridge] Symbols: ",
         (SendXAUUSD ? "XAUUSD " : ""),
         (SendBTCUSD ? "BTCUSD " : ""),
         (SendEURUSD ? "EURUSD" : ""));

   // Heartbeat iniziale per verificare connessione
   SendHeartbeat();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[Bridge] EA fermato. Reason=", reason);
}

//+------------------------------------------------------------------+
void OnTimer()
{
   timerCount++;
   SendMarketData();
   CheckCommands();
   if(timerCount % heartbeatEvery == 0)
      SendHeartbeat();
}

void OnTick()
{
   if(SendXAUUSD) PostTickIfAvailable("XAUUSD");
   if(SendBTCUSD) PostTickIfAvailable("BTCUSD");
   if(SendEURUSD) PostTickIfAvailable("EURUSD");
}

//+------------------------------------------------------------------+
// Invia market data al server Python
//+------------------------------------------------------------------+
void SendMarketData()
{
   string symbols[];
   int count = 0;
   if(SendXAUUSD) { ArrayResize(symbols, count+1); symbols[count++] = "XAUUSD"; }
   if(SendBTCUSD) { ArrayResize(symbols, count+1); symbols[count++] = "BTCUSD"; }
   if(SendEURUSD) { ArrayResize(symbols, count+1); symbols[count++] = "EURUSD"; }

   for(int s = 0; s < count; s++)
   {
      string sym = symbols[s];
      if(!SymbolSelect(sym, true))
      {
         Print("[Bridge] SIMBOLO NON DISPONIBILE: ", sym,
               " → Aprilo nel Market Watch (View→Market Watch→cerca ", sym, ")");
         continue;
      }

      MqlRates rates[];
      int copied = CopyRates(sym, TF, 0, 150, rates);
      if(copied <= 0) continue;

      string json = "{\"symbol\":\"" + sym + "\",\"timeframe\":\"H1\",\"candles\":[";
      for(int i = 0; i < copied; i++)
      {
         if(i > 0) json += ",";
         json += StringFormat(
            "{\"time\":%d,\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d}",
            (int)rates[i].time,
            rates[i].open, rates[i].high, rates[i].low, rates[i].close,
            (int)rates[i].tick_volume
         );
      }
      json += "]}";
      PostToServer("/candles", json);
   }

   // Account info
   string accJson = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f}",
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE)
   );
   PostToServer("/account", accJson);

   SendPositions();
}

//+------------------------------------------------------------------+
void PostTickIfAvailable(string sym)
{
   MqlTick tick;
   if(!SymbolInfoTick(sym, tick)) return;
   string json = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,\"spread\":%.5f,\"time\":%d}",
      sym, tick.bid, tick.ask, (tick.ask - tick.bid), (int)tick.time
   );
   PostToServer("/tick", json);
}

//+------------------------------------------------------------------+
void SendPositions()
{
   string json = "{\"positions\":[";
   bool first = true;
   for(int i = 0; i < PositionsTotal(); i++)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(!first) json += ",";
      first = false;
      json += StringFormat(
         "{\"ticket\":%d,\"symbol\":\"%s\",\"type\":\"%s\",\"volume\":%.2f,"
         "\"open_price\":%.5f,\"current_price\":%.5f,"
         "\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f}",
         (int)ticket,
         PositionGetString(POSITION_SYMBOL),
         PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell",
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN),
         PositionGetDouble(POSITION_PRICE_CURRENT),
         PositionGetDouble(POSITION_SL),
         PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT)
      );
   }
   json += "]}";
   PostToServer("/positions", json);
}

//+------------------------------------------------------------------+
void CheckCommands()
{
   char getData[];
   char result[];
   string headers;
   int res = WebRequest("GET", BaseURL + "/commands", "", "", 5000, getData, 0, result, headers);

   if(res == -1)
   {
      int errCode = GetLastError();
      if(errCode == 4014)
         Print("[Bridge] ERRORE 4014: Aggiungi URL nella whitelist MT5: Strumenti→Opzioni→EA→WebRequest: ", BaseURL);
      return;
   }
   if(ArraySize(result) == 0) return;

   string response = CharArrayToString(result);
   if(StringLen(response) < 3 || response == "[]") return;

   Print("[Bridge] Comandi: ", response);

   // Parse JSON array di comandi
   int depth = 0;
   int cmdStart = -1;
   for(int i = 0; i < StringLen(response); i++)
   {
      string ch = StringSubstr(response, i, 1);
      if(ch == "{") {
         depth++;
         if(depth == 1) cmdStart = i;
      } else if(ch == "}") {
         depth--;
         if(depth == 0 && cmdStart >= 0)
         {
            string cmd = StringSubstr(response, cmdStart, i - cmdStart + 1);
            ExecuteCommand(cmd);
            cmdStart = -1;
         }
      }
   }
}

//+------------------------------------------------------------------+
void ExecuteCommand(string cmd)
{
   string action = ExtractField(cmd, "action");
   string symbol = ExtractField(cmd, "symbol");
   double volume = StringToDouble(ExtractField(cmd, "volume"));
   double sl     = StringToDouble(ExtractField(cmd, "sl"));
   double tp     = StringToDouble(ExtractField(cmd, "tp"));
   string cmdId  = ExtractField(cmd, "id");

   if(StringLen(action) == 0 || StringLen(symbol) == 0)
   {
      SendCommandResult(cmdId, action, symbol, false, 0, "invalid_command");
      return;
   }

   if(!EnableTrading)
   {
      Print("[Bridge] Trading disabilitato. Ignorato: ", action, " ", symbol);
      SendCommandResult(cmdId, action, symbol, false, 0, "trading_disabled");
      return;
   }

   if(volume < 0.01) volume = 0.01;

   bool ok = false;
   string errMsg = "";

   SymbolSelect(symbol, true);

   if(action == "buy")
   {
      ok = trade.Buy(volume, symbol, 0, sl, tp, "algo-" + cmdId);
      if(!ok) errMsg = trade.ResultRetcodeDescription();
   }
   else if(action == "sell")
   {
      ok = trade.Sell(volume, symbol, 0, sl, tp, "algo-" + cmdId);
      if(!ok) errMsg = trade.ResultRetcodeDescription();
   }
   else if(action == "close")
   {
      ok = CloseSymbol(symbol);
   }
   else
   {
      errMsg = "unknown_action";
   }

   SendCommandResult(cmdId, action, symbol, ok, (int)trade.ResultOrder(), errMsg);
   Print("[Bridge] ", action, " ", symbol, " vol=", volume,
         " → ", ok ? "OK #" + IntegerToString((int)trade.ResultOrder()) : "FAIL: " + errMsg);
}

void SendCommandResult(string id, string action, string symbol,
                       bool success, int order, string err)
{
   string json = StringFormat(
      "{\"id\":\"%s\",\"action\":\"%s\",\"symbol\":\"%s\","
      "\"success\":%s,\"order\":%d,\"error\":\"%s\"}",
      id, action, symbol,
      success ? "true" : "false",
      order, err
   );
   PostToServer("/command_result", json);
}

bool CloseSymbol(string symbol)
{
   bool ok = true;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0) continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol) continue;
      if(!trade.PositionClose(ticket)) ok = false;
   }
   return ok;
}

//+------------------------------------------------------------------+
void SendHeartbeat()
{
   string body = StringFormat(
      "{\"status\":\"online\",\"account\":\"%d\","
      "\"symbols\":\"%s%s%s\","
      "\"balance\":%.2f,\"version\":\"2.0\"}",
      (int)AccountInfoInteger(ACCOUNT_LOGIN),
      (SendXAUUSD ? "XAUUSD " : ""),
      (SendBTCUSD ? "BTCUSD " : ""),
      (SendEURUSD ? "EURUSD" : ""),
      AccountInfoDouble(ACCOUNT_BALANCE)
   );

   char postData[];
   char result[];
   string headers;
   StringToCharArray(body, postData, 0, StringLen(body));
   int res = WebRequest(
      "POST", BaseURL + "/heartbeat",
      "Content-Type: application/json\r\n",
      "", 5000, postData, ArraySize(postData) - 1, result, headers
   );

   if(res == 200)
      Print("[Bridge] OK — server raggiunto. Account: ",
            (int)AccountInfoInteger(ACCOUNT_LOGIN),
            " Balance: $", AccountInfoDouble(ACCOUNT_BALANCE));
   else
   {
      int err = GetLastError();
      Print("[Bridge] ATTENZIONE: server non raggiunto (HTTP=", res, " err=", err, ")");
      if(err == 4014)
         Print("[Bridge] → Vai in: Strumenti→Opzioni→Consulenti Esperti→WebRequest");
      else
         Print("[Bridge] → Verifica URL: ", BaseURL);
   }
}

//+------------------------------------------------------------------+
string ExtractField(string json, string field)
{
   string search = "\"" + field + "\":\"";
   int pos = StringFind(json, search);
   if(pos >= 0)
   {
      int start = pos + StringLen(search);
      int end   = StringFind(json, "\"", start);
      if(end > start) return StringSubstr(json, start, end - start);
   }
   search = "\"" + field + "\":";
   pos = StringFind(json, search);
   if(pos >= 0)
   {
      int start = pos + StringLen(search);
      int end1  = StringFind(json, ",", start);
      int end2  = StringFind(json, "}", start);
      int end   = (end1 < 0 || (end2 >= 0 && end2 < end1)) ? end2 : end1;
      if(end > start) return StringSubstr(json, start, end - start);
   }
   return "";
}

void PostToServer(string path, string body)
{
   char postData[];
   char result[];
   string headers;
   StringToCharArray(body, postData, 0, StringLen(body));
   WebRequest(
      "POST", BaseURL + path,
      "Content-Type: application/json\r\n",
      "", 3000, postData, ArraySize(postData) - 1, result, headers
   );
}
