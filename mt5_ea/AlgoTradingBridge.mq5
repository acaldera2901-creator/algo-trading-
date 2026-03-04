//+------------------------------------------------------------------+
//| AlgoTradingBridge.mq5                                            |
//| Space Traders Academy - Algo Trading Agent Bridge                |
//|                                                                  |
//| Questo EA gira nel tuo MT5 su Mac/Windows.                       |
//| Comunica con il bot Python su Linux via HTTP.                    |
//|                                                                  |
//| INSTALLAZIONE:                                                   |
//| 1. Copia questo file in: MT5/MQL5/Experts/                       |
//| 2. In MT5: Strumenti -> Opzioni -> Consulenti Esperti            |
//|    -> Abilita "Consenti WebRequest per i seguenti URL"           |
//|    -> Aggiungi l'URL del tuo server Linux (es. http://IP:8765)   |
//| 3. Compila l'EA (F7) e trascinalo su un grafico EURUSD H1        |
//| 4. Nei parametri EA, inserisci l'IP del server Linux             |
//+------------------------------------------------------------------+
#property copyright "Algo Trading Agent"
#property version   "1.0"
#property strict

#include <Trade\Trade.mqh>
CTrade trade;

// ─── Parametri EA ───────────────────────────────────────────────────────────
input string ServerIP      = "YOUR_LINUX_SERVER_IP";  // IP del server Linux
input int    ServerPort    = 8765;                     // Porta del bot Python
input int    PollInterval  = 5;                        // Secondi tra ogni poll
input bool   EnableTrading = true;                     // Abilita esecuzione ordini

// ─── Variabili globali ───────────────────────────────────────────────────────
string BaseURL;
datetime lastCandle = 0;
int timerCount = 0;

//+------------------------------------------------------------------+
int OnInit()
{
   BaseURL = "http://" + ServerIP + ":" + IntegerToString(ServerPort);
   EventSetTimer(PollInterval);
   Print("[Bridge] Avviato. Server: ", BaseURL);
   SendHeartbeat();
   return(INIT_SUCCEEDED);
}

void OnDeinit(const int reason)
{
   EventKillTimer();
   Print("[Bridge] EA fermato.");
}

//+------------------------------------------------------------------+
void OnTimer()
{
   // Ogni PollInterval secondi:
   // 1. Invia candela corrente al bot Python
   // 2. Richiede comandi di trading

   SendCandles();
   CheckCommands();
}

void OnTick()
{
   // Su ogni tick invia il prezzo corrente
   SendTick();
}

//+------------------------------------------------------------------+
// Invia i dati delle ultime N candele al server Python
//+------------------------------------------------------------------+
void SendCandles()
{
   string symbols[] = {"EURUSD", "GBPUSD", "USDJPY"};
   ENUM_TIMEFRAMES tf = PERIOD_H1;
   int count = 100;

   for(int s = 0; s < ArraySize(symbols); s++)
   {
      string sym = symbols[s];
      MqlRates rates[];
      int copied = CopyRates(sym, tf, 0, count, rates);
      if(copied <= 0) continue;

      // Build JSON array of candles
      string json = "{\"symbol\":\"" + sym + "\",\"timeframe\":\"H1\",\"candles\":[";
      for(int i = 0; i < copied; i++)
      {
         if(i > 0) json += ",";
         json += StringFormat(
            "{\"time\":%d,\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f,\"volume\":%d}",
            (int)rates[i].time, rates[i].open, rates[i].high,
            rates[i].low, rates[i].close, (int)rates[i].tick_volume
         );
      }
      json += "]}";

      PostToServer("/candles", json);
   }

   // Send account info
   string accJson = StringFormat(
      "{\"balance\":%.2f,\"equity\":%.2f,\"margin\":%.2f,\"free_margin\":%.2f}",
      AccountInfoDouble(ACCOUNT_BALANCE),
      AccountInfoDouble(ACCOUNT_EQUITY),
      AccountInfoDouble(ACCOUNT_MARGIN),
      AccountInfoDouble(ACCOUNT_MARGIN_FREE)
   );
   PostToServer("/account", accJson);

   // Send open positions
   SendPositions();
}

//+------------------------------------------------------------------+
// Invia il tick corrente (prezzo bid/ask)
//+------------------------------------------------------------------+
void SendTick()
{
   string symbols[] = {"EURUSD", "GBPUSD", "USDJPY"};
   for(int s = 0; s < ArraySize(symbols); s++)
   {
      MqlTick tick;
      if(!SymbolInfoTick(symbols[s], tick)) continue;
      string json = StringFormat(
         "{\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,\"time\":%d}",
         symbols[s], tick.bid, tick.ask, (int)tick.time
      );
      PostToServer("/tick", json);
   }
}

//+------------------------------------------------------------------+
// Invia le posizioni aperte
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
         "\"open_price\":%.5f,\"sl\":%.5f,\"tp\":%.5f,\"profit\":%.2f}",
         (int)ticket,
         PositionGetString(POSITION_SYMBOL),
         PositionGetInteger(POSITION_TYPE) == POSITION_TYPE_BUY ? "buy" : "sell",
         PositionGetDouble(POSITION_VOLUME),
         PositionGetDouble(POSITION_PRICE_OPEN),
         PositionGetDouble(POSITION_SL),
         PositionGetDouble(POSITION_TP),
         PositionGetDouble(POSITION_PROFIT)
      );
   }
   json += "]}";
   PostToServer("/positions", json);
}

//+------------------------------------------------------------------+
// Richiede comandi al bot Python e li esegue
//+------------------------------------------------------------------+
void CheckCommands()
{
   char getData[];   // empty body for GET
   char result[];
   string headers;
   int res = WebRequest("GET", BaseURL + "/commands", "", "", 5000, getData, 0, result, headers);
   if(res == -1 || ArraySize(result) == 0) return;

   string response = CharArrayToString(result);
   if(StringLen(response) < 3 || response == "[]") return;

   Print("[Bridge] Comandi ricevuti: ", response);

   // Parse simple JSON commands
   // Expected format: [{"action":"buy","symbol":"EURUSD","volume":0.01,"sl":1.0800,"tp":1.1000}]
   // Simple string parsing (no full JSON lib needed)
   int start = 0;
   while(true)
   {
      int cmdStart = StringFind(response, "{", start);
      if(cmdStart < 0) break;
      int cmdEnd = StringFind(response, "}", cmdStart);
      if(cmdEnd < 0) break;

      string cmd = StringSubstr(response, cmdStart, cmdEnd - cmdStart + 1);
      ExecuteCommand(cmd);
      start = cmdEnd + 1;
   }
}

//+------------------------------------------------------------------+
// Esegue un singolo comando di trading
//+------------------------------------------------------------------+
void ExecuteCommand(string cmd)
{
   if(!EnableTrading)
   {
      Print("[Bridge] Trading disabilitato. Comando ignorato: ", cmd);
      return;
   }

   // Extract fields
   string action = ExtractField(cmd, "action");
   string symbol = ExtractField(cmd, "symbol");
   double volume = StringToDouble(ExtractField(cmd, "volume"));
   double sl     = StringToDouble(ExtractField(cmd, "sl"));
   double tp     = StringToDouble(ExtractField(cmd, "tp"));
   string cmdId  = ExtractField(cmd, "id");

   if(StringLen(action) == 0 || StringLen(symbol) == 0 || volume <= 0)
   {
      Print("[Bridge] Comando non valido: ", cmd);
      return;
   }

   bool result = false;

   if(action == "buy")
   {
      result = trade.Buy(volume, symbol, 0, sl, tp, "algo-agent-" + cmdId);
   }
   else if(action == "sell")
   {
      result = trade.Sell(volume, symbol, 0, sl, tp, "algo-agent-" + cmdId);
   }
   else if(action == "close")
   {
      result = CloseSymbol(symbol);
   }

   // Report result back to server
   string resultJson = StringFormat(
      "{\"id\":\"%s\",\"action\":\"%s\",\"symbol\":\"%s\",\"success\":%s,"
      "\"order\":%d,\"error\":\"%s\"}",
      cmdId, action, symbol,
      result ? "true" : "false",
      (int)trade.ResultOrder(),
      trade.ResultRetcodeDescription()
   );
   PostToServer("/command_result", resultJson);
   Print("[Bridge] Eseguito ", action, " su ", symbol, " -> ", result ? "OK" : "FAIL");
}

//+------------------------------------------------------------------+
// Chiude tutte le posizioni su un simbolo
//+------------------------------------------------------------------+
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
// Helper: estrae il valore di un campo da JSON semplice
//+------------------------------------------------------------------+
string ExtractField(string json, string field)
{
   string search = "\"" + field + "\":\"";
   int pos = StringFind(json, search);
   if(pos >= 0)
   {
      int start = pos + StringLen(search);
      int end = StringFind(json, "\"", start);
      if(end > start) return StringSubstr(json, start, end - start);
   }
   // Try numeric (no quotes)
   search = "\"" + field + "\":";
   pos = StringFind(json, search);
   if(pos >= 0)
   {
      int start = pos + StringLen(search);
      int end = StringFind(json, ",", start);
      int end2 = StringFind(json, "}", start);
      if(end < 0 || (end2 > 0 && end2 < end)) end = end2;
      if(end > start) return StringSubstr(json, start, end - start);
   }
   return "";
}

//+------------------------------------------------------------------+
// POST helper
//+------------------------------------------------------------------+
void PostToServer(string path, string body)
{
   char postData[];
   char result[];
   string headers;
   StringToCharArray(body, postData, 0, StringLen(body));
   WebRequest("POST", BaseURL + path, "Content-Type: application/json\r\n", "", 3000,
              postData, ArraySize(postData) - 1, result, headers);
}

//+------------------------------------------------------------------+
// Heartbeat al server (verifica connessione)
//+------------------------------------------------------------------+
void SendHeartbeat()
{
   char postData[];
   char result[];
   string headers;
   string body = "{\"status\":\"online\",\"account\":\"" + IntegerToString((int)AccountInfoInteger(ACCOUNT_LOGIN)) + "\"}";
   StringToCharArray(body, postData, 0, StringLen(body));
   int res = WebRequest("POST", BaseURL + "/heartbeat", "Content-Type: application/json\r\n", "", 5000,
                        postData, ArraySize(postData) - 1, result, headers);
   if(res == 200)
      Print("[Bridge] Server raggiunto: ", BaseURL);
   else
      Print("[Bridge] ATTENZIONE: server non raggiunto (code=", res, "). Verifica IP e porta.");
}
