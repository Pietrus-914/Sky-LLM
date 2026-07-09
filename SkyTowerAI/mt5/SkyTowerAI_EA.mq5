//+------------------------------------------------------------------+
//|                                              SkyTowerAI_EA.mq5   |
//|                                    SkyTower-AI Trading System    |
//|                                   AI-Enhanced News Trading       |
//+------------------------------------------------------------------+
#property copyright "SkyTower-AI"
#property version   "5.00"
#property description "AI-Enhanced News Trading Expert Advisor"
#property description "Works with SkyTower-AI Python Server"
#property description "Smart Exit with Zone-Based Targets"

#include <Trade\Trade.mqh>
#include <Trade\PositionInfo.mqh>
#include "SkyTowerAI_Zones.mqh"
#include "SkyTowerAI_Panel.mqh"

//--- Input parameters
input group "=== Panel Settings ==="
input bool     InpShowPanel = true;              // Show Visual Panel
input ENUM_TRADE_MODE InpDefaultMode = MODE_LLM_AUTO; // Default Trade Mode

input group "=== Server Settings ==="
input string   InpServerHost = "127.0.0.1";     // Server Host
input int      InpServerPort = 5555;             // Server Port
input int      InpCheckInterval = 60;            // Signal Check Interval (seconds)

input group "=== Trading Settings ==="
input double   InpRiskPercent = 10.0;            // Risk % per trade (of balance)
input double   InpMaxLotPercent = 80.0;          // Max Lot % (default from server)
input int      InpSlippage = 50;                 // Max Slippage (points)
input double   InpMinConfidence = 0.5;           // Min Confidence to trade

input group "=== Safety Settings ==="
input double   InpMaxSpreadPips = 10.0;          // Max Spread (pips)
input bool     InpUseStopLoss = true;            // Use Stop Loss
input double   InpDefaultSLPercent = 40.0;       // Default SL % of risk
input int      InpMaxDailyTrades = 3;            // Max Trades per Day

input group "=== Timing Settings ==="
input int      InpEntrySecondsBefore = 15;       // Entry seconds before event
input int      InpExitMinutesAfter = 10;         // Exit minutes after event (fallback)

input group "=== Smart Exit Settings ==="
input ENUM_EXIT_STRATEGY InpExitStrategy = EXIT_HYBRID;  // Exit Strategy
input bool     InpUseZoneTargets = true;         // Use Zone-Based Targets
input bool     InpPartialCloseTP1 = true;        // Partial Close at TP1
input int      InpTP1ClosePercent = 50;          // TP1 Close Percentage
input bool     InpMoveSLToBreakeven = true;      // Move SL to Break-Even after TP1
input bool     InpTrailAfterTP1 = true;          // Enable Trailing Stop after TP1
input double   InpTrailDistancePips = 10.0;      // Trailing Stop Distance (pips)
input int      InpMaxHoldMinutes = 30;           // Maximum Position Hold Time (minutes)
input int      InpFallbackExitMinutes = 15;      // Fallback Exit Time (minutes)

input group "=== Zone Indicator Settings ==="
input bool     InpUseZoneIndicator = true;       // Use SkyTower_Zones Indicator
input bool     InpUseZoneBiasForDirection = true;// Use Zone Bias for Direction

input group "=== AI Position Management ==="
input double   InpMaxLossUSD = 100.0;            // Max loss $ before forced close (EA safety)
input double   InpEmergencySpreadPips = 15.0;    // Close if spread exceeds this

input group "=== Multi-Instance Mode ==="
input bool     InpMultiInstance = false;         // Enable Multi-Instance Mode
input int      InpRegisterMinBefore = 5;         // Register pair X minutes before event
input int      InpMagicNumber = 0;               // Magic Number (0=auto from symbol)

input group "=== Market Data & Reaction Reporting ==="
input bool     InpPushMarketData = true;         // Push OHLC to server (LLM market context)
input int      InpMarketDataSeconds = 60;        // Market data push interval (seconds)
input bool     InpReportReactions = true;        // Report post-event price reactions

//--- Global variables
CTrade         trade;
CPositionInfo  positionInfo;
CSmartExitManager g_smartExit;    // Smart exit manager

datetime       g_lastCheckTime = 0;
datetime       g_lastTradeTime = 0;
int            g_todayTrades = 0;
datetime       g_currentDay = 0;
bool           g_waitingForEvent = false;
datetime       g_eventTime = 0;
string         g_eventPair = "";
string         g_eventDirection = "";
double         g_eventLotPercent = 0;
int            g_eventExitMinutes = 0;
double         g_eventSLPercent = 0;
double         g_eventSLPips = 0;       // SL in pips from LLM
double         g_eventTPPips = 0;       // TP in pips from LLM
ulong          g_currentTicket = 0;

// Smart exit state
bool           g_tp1Hit = false;
bool           g_slMovedToBE = false;
double         g_originalLots = 0;

// Zone indicator handle and data
int            g_zoneIndicatorHandle = INVALID_HANDLE;
double         g_zoneBias = 0;           // -1 to +1 from indicator
double         g_nearestLiqHigh = 0;     // Nearest liquidity above
double         g_nearestLiqLow = 0;      // Nearest liquidity below
double         g_nearestFVGHigh = 0;     // Nearest FVG above
double         g_nearestFVGLow = 0;      // Nearest FVG below

// Panel
CSkyTowerPanel g_panel;
bool           g_panelCreated = false;
string         g_currentEventName = "";
double         g_currentConfidence = 0;

// AI Position Management
datetime       g_lastPositionReport = 0;    // Last time we reported to server
bool           g_aiManagementActive = false; // AI is managing the position

// Multi-instance mode
bool           g_pairRegistered = false;
string         g_registeredEventKey = "";
datetime       g_lastRegisterAttempt = 0;

// Market data push (LLM market context)
datetime       g_lastMarketDataPush = 0;

// Event reaction tracking (post-release price snapshots for the server).
// Slot array: clustered releases (e.g. 13:30 + 13:33) may overlap within the
// 5-minute measurement window — one slot per pending event, no overwrites.
#define REACTION_SLOTS 4
bool           g_reactionPending[REACTION_SLOTS];
datetime       g_reactionEventTime[REACTION_SLOTS];   // broker time of the event
string         g_reactionEventName[REACTION_SLOTS];
string         g_reactionCurrency[REACTION_SLOTS];
string         g_reactionEventTimeUTC[REACTION_SLOTS]; // ISO UTC string from the server signal
double         g_reactionPrice0[REACTION_SLOTS];       // bid at event time
double         g_reactionPrice1[REACTION_SLOTS];       // bid at T+60s

//+------------------------------------------------------------------+
//| Expert initialization function                                     |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Generate unique magic number per symbol if auto mode
   int magicNumber = InpMagicNumber;
   if(magicNumber == 0)
   {
      // Generate from symbol name hash for uniqueness per pair
      // Base: 20240116, add hash of first 6 chars of symbol
      magicNumber = 20240116;
      string sym = _Symbol;
      for(int i = 0; i < MathMin(6, StringLen(sym)); i++)
      {
         magicNumber += StringGetCharacter(sym, i) * (i + 1) * 100;
      }
   }

   //--- Setup trade object
   trade.SetExpertMagicNumber(magicNumber);
   trade.SetDeviationInPoints(InpSlippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);
   Print("Magic Number: ", magicNumber, " (Symbol: ", _Symbol, ")");

   //--- Display timezone info for debugging
   int brokerOffset = GetBrokerTimezoneOffset();
   int offsetHours = brokerOffset / 3600;
   int offsetMinutes = (MathAbs(brokerOffset) % 3600) / 60;
   string offsetSign = (brokerOffset >= 0) ? "+" : "-";
   Print("=== Timezone Info ===");
   Print("Broker Server Time: ", TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS));
   Print("UTC Time (TimeGMT): ", TimeToString(TimeGMT(), TIME_DATE | TIME_SECONDS));
   Print("Broker Offset: UTC", offsetSign, IntegerToString(MathAbs(offsetHours)), ":",
         StringFormat("%02d", offsetMinutes), " (", brokerOffset, " seconds)");
   Print("Events from server are in UTC - will be automatically converted");
   Print("=====================");

   //--- Initialize daily counter
   g_currentDay = TimeCurrent() - TimeCurrent() % 86400;
   g_todayTrades = 0;

   //--- Initialize Smart Exit Manager
   g_smartExit.Init(
      InpServerHost,
      InpServerPort,
      InpExitStrategy,
      InpFallbackExitMinutes,
      InpMaxHoldMinutes,
      InpUseZoneTargets,
      InpPartialCloseTP1,
      InpMoveSLToBreakeven,
      InpTrailAfterTP1,
      InpTrailDistancePips
   );

   //--- Initialize Zone Indicator
   if(InpUseZoneIndicator)
   {
      g_zoneIndicatorHandle = iCustom(_Symbol, PERIOD_CURRENT, "SkyTower_Zones");
      if(g_zoneIndicatorHandle == INVALID_HANDLE)
      {
         Print("WARNING: Could not load SkyTower_Zones indicator!");
         Print("Zone-based targets will use server fallback");
      }
      else
      {
         Print("SkyTower_Zones indicator loaded successfully");
      }
   }

   //--- Create Visual Panel
   if(InpShowPanel)
   {
      if(!g_panel.Create(0, "SkyTowerPanel", 0, PANEL_X, PANEL_Y))
      {
         Print("WARNING: Could not create panel!");
      }
      else
      {
         g_panel.Run();
         g_panelCreated = true;
         Print("Panel created successfully");
      }
   }

   Print("==============================================");
   Print("SkyTower-AI EA v5.0 Initialized");
   Print("Server: ", InpServerHost, ":", InpServerPort);
   Print("Risk: ", InpRiskPercent, "%");
   Print("Min Confidence: ", InpMinConfidence);
   Print("Trade Mode: ", EnumToString(InpDefaultMode));
   Print("Exit Strategy: ", EnumToString(InpExitStrategy));
   Print("Zone Targets: ", InpUseZoneTargets ? "Enabled" : "Disabled");
   Print("Zone Indicator: ", InpUseZoneIndicator ? "Enabled" : "Disabled");
   Print("Zone Bias for Direction: ", InpUseZoneBiasForDirection ? "Enabled" : "Disabled");
   Print("Partial TP1: ", InpPartialCloseTP1 ? "Enabled" : "Disabled");
   Print("Trailing Stop: ", InpTrailAfterTP1 ? "Enabled" : "Disabled");
   Print("Visual Panel: ", InpShowPanel ? "Enabled" : "Disabled");
   Print("==============================================");

   //--- Check server connection
   if(!TestServerConnection())
   {
      Print("WARNING: Could not connect to SkyTower-AI server!");
      Print("Make sure server.py is running");
      if(g_panelCreated)
         g_panel.SetStatus("Server offline!", clrRed);
   }
   else
   {
      Print("Server connection OK");
      if(g_panelCreated)
         g_panel.SetStatus("Connected", clrLime);
   }

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Expert deinitialization function                                   |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   //--- Destroy panel
   if(g_panelCreated)
   {
      g_panel.Destroy(reason);
      g_panelCreated = false;
   }

   //--- Release indicator handle
   if(g_zoneIndicatorHandle != INVALID_HANDLE)
   {
      IndicatorRelease(g_zoneIndicatorHandle);
      g_zoneIndicatorHandle = INVALID_HANDLE;
   }

   Print("SkyTower-AI EA Deinitialized. Reason: ", reason);
}

//+------------------------------------------------------------------+
//| Chart event handler (for panel)                                   |
//+------------------------------------------------------------------+
void OnChartEvent(const int id,
                  const long &lparam,
                  const double &dparam,
                  const string &sparam)
{
   if(g_panelCreated)
   {
      g_panel.ChartEvent(id, lparam, dparam, sparam);
   }
}

//+------------------------------------------------------------------+
//| Read zone data from indicator                                     |
//+------------------------------------------------------------------+
bool ReadZoneIndicatorData()
{
   if(g_zoneIndicatorHandle == INVALID_HANDLE || !InpUseZoneIndicator)
      return false;

   double bufLiqHigh[], bufLiqLow[], bufFVGHigh[], bufFVGLow[], bufZoneBias[];

   //--- Copy data from indicator buffers
   if(CopyBuffer(g_zoneIndicatorHandle, 0, 0, 1, bufLiqHigh) < 1) return false;
   if(CopyBuffer(g_zoneIndicatorHandle, 1, 0, 1, bufLiqLow) < 1) return false;
   if(CopyBuffer(g_zoneIndicatorHandle, 2, 0, 1, bufFVGHigh) < 1) return false;
   if(CopyBuffer(g_zoneIndicatorHandle, 3, 0, 1, bufFVGLow) < 1) return false;
   if(CopyBuffer(g_zoneIndicatorHandle, 6, 0, 1, bufZoneBias) < 1) return false;

   //--- Store values
   g_nearestLiqHigh = bufLiqHigh[0];
   g_nearestLiqLow = bufLiqLow[0];
   g_nearestFVGHigh = bufFVGHigh[0];
   g_nearestFVGLow = bufFVGLow[0];
   g_zoneBias = bufZoneBias[0];

   return true;
}

//+------------------------------------------------------------------+
//| Get TP/SL from zone indicator                                     |
//+------------------------------------------------------------------+
void GetZoneBasedTargets(string direction, double entryPrice, double &tp1, double &tp2, double &sl)
{
   if(!ReadZoneIndicatorData())
   {
      //--- Fallback to default values
      double pip = _Point * 10;
      if(direction == "BUY")
      {
         tp1 = entryPrice + 30 * pip;
         tp2 = entryPrice + 50 * pip;
         sl = entryPrice - 25 * pip;
      }
      else
      {
         tp1 = entryPrice - 30 * pip;
         tp2 = entryPrice - 50 * pip;
         sl = entryPrice + 25 * pip;
      }
      return;
   }

   double pip = _Point * 10;

   if(direction == "BUY")
   {
      //--- TP targets: liquidity above, then FVG
      if(g_nearestLiqHigh > 0)
         tp1 = g_nearestLiqHigh;
      else if(g_nearestFVGHigh > 0)
         tp1 = g_nearestFVGHigh;
      else
         tp1 = entryPrice + 30 * pip;

      tp2 = tp1 + 20 * pip; // TP2 beyond TP1

      //--- SL: below nearest support (liquidity low or FVG low)
      if(g_nearestLiqLow > 0)
         sl = g_nearestLiqLow - 5 * pip; // Below the liquidity zone
      else if(g_nearestFVGLow > 0)
         sl = g_nearestFVGLow - 5 * pip;
      else
         sl = entryPrice - 25 * pip;
   }
   else // SELL
   {
      //--- TP targets: liquidity below, then FVG
      if(g_nearestLiqLow > 0)
         tp1 = g_nearestLiqLow;
      else if(g_nearestFVGLow > 0)
         tp1 = g_nearestFVGLow;
      else
         tp1 = entryPrice - 30 * pip;

      tp2 = tp1 - 20 * pip; // TP2 beyond TP1

      //--- SL: above nearest resistance (liquidity high or FVG high)
      if(g_nearestLiqHigh > 0)
         sl = g_nearestLiqHigh + 5 * pip; // Above the liquidity zone
      else if(g_nearestFVGHigh > 0)
         sl = g_nearestFVGHigh + 5 * pip;
      else
         sl = entryPrice + 25 * pip;
   }

   //--- Ensure minimum risk/reward
   double tpDist = MathAbs(tp1 - entryPrice);
   double slDist = MathAbs(sl - entryPrice);

   if(tpDist < slDist * 1.0) // At least 1:1
   {
      if(direction == "BUY")
         tp1 = entryPrice + slDist * 1.5;
      else
         tp1 = entryPrice - slDist * 1.5;
   }

   Print("Zone-based targets: TP1=", tp1, " TP2=", tp2, " SL=", sl, " (Bias=", g_zoneBias, ")");
}

//+------------------------------------------------------------------+
//| Expert tick function                                               |
//+------------------------------------------------------------------+
void OnTick()
{
   //--- Check if new day
   datetime today = TimeCurrent() - TimeCurrent() % 86400;
   if(today != g_currentDay)
   {
      g_currentDay = today;
      g_todayTrades = 0;
      Print("New trading day. Trades reset.");
   }

   //--- Check for open positions that need to be closed
   ManageOpenPositions();

   //--- Track post-event price reaction (runs independently of trade state)
   if(InpReportReactions)
      HandleEventReaction();

   //--- If we're waiting for an event, check if it's time to trade
   if(g_waitingForEvent)
   {
      int secondsUntilEvent = (int)(g_eventTime - TimeCurrent());

      //--- Update panel countdown
      if(g_panelCreated)
      {
         g_panel.SetCountdown(secondsUntilEvent);
      }

      //--- Time to enter
      if(secondsUntilEvent <= InpEntrySecondsBefore && secondsUntilEvent > 0)
      {
         ExecuteEventTrade();
         return;
      }

      //--- Event passed without trade
      if(secondsUntilEvent < -60)
      {
         Print("Event window passed. Resetting.");
         if(g_panelCreated)
         {
            g_panel.SetStatus("Event passed", clrGray);
            g_panel.ClearSignal();
         }
         ResetEventWait();
      }

      return;
   }

   //--- Regular signal check
   datetime currentTime = TimeCurrent();
   if(currentTime - g_lastCheckTime < InpCheckInterval)
      return;

   g_lastCheckTime = currentTime;

   //--- Check if we can trade today
   if(g_todayTrades >= InpMaxDailyTrades)
   {
      // Silent - don't spam logs
      return;
   }

   //--- Multi-instance mode: register pair with server for upcoming events
   if(InpMultiInstance && !g_pairRegistered)
   {
      // Check if we should try to register (every 60 seconds)
      if(currentTime - g_lastRegisterAttempt >= 60)
      {
         g_lastRegisterAttempt = currentTime;

         // First get next event info from server
         string eventUrl = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/next-event";
         string eventResult = "";

         if(WebRequest(eventUrl, eventResult))
         {
            if(StringFind(eventResult, "\"event\":") >= 0 && StringFind(eventResult, "\"event\":null") < 0)
            {
               // Extract event info
               string eventCurrency = ExtractJsonString(eventResult, "currency");
               string eventTimeStr = ExtractJsonString(eventResult, "datetime_utc");

               // Check if this symbol contains the event currency
               string symbol = _Symbol;
               if(StringFind(symbol, eventCurrency) >= 0)
               {
                  // Parse event time (ISO format from server - this is UTC!)
                  datetime eventTimeUTC = ParseISODateTime(eventTimeStr);
                  // Compare UTC with UTC using TimeGMT()
                  int minutesUntil = (int)((eventTimeUTC - TimeGMT()) / 60);

                  // Register if within registration window
                  if(minutesUntil > 0 && minutesUntil <= InpRegisterMinBefore)
                  {
                     Print("Event for ", eventCurrency, " in ", minutesUntil, " min (UTC) - registering pair ", symbol);
                     RegisterPairWithServer(eventCurrency, eventTimeUTC);
                  }
               }
            }
         }
      }
   }

   //--- Push current OHLC so the server's LLM sees fresh market structure
   if(InpPushMarketData && currentTime - g_lastMarketDataPush >= InpMarketDataSeconds)
   {
      g_lastMarketDataPush = currentTime;
      PushMarketData();
   }

   //--- Check for new signals from server
   CheckForSignals();
}

//+------------------------------------------------------------------+
//| Test server connection                                             |
//+------------------------------------------------------------------+
bool TestServerConnection()
{
   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/health";
   string result = "";

   if(!WebRequest(url, result))
      return false;

   return (StringFind(result, "\"status\":\"ok\"") >= 0);
}

//+------------------------------------------------------------------+
//| Make HTTP request to server                                        |
//+------------------------------------------------------------------+
bool WebRequest(string url, string &result)
{
   char data[];
   char res[];
   string headers;
   string resultHeaders;

   ResetLastError();

   int timeout = 5000; // 5 seconds

   int code = WebRequest("GET", url, headers, timeout, data, res, resultHeaders);

   if(code == -1)
   {
      int error = GetLastError();
      if(error == 4014)
         Print("Add URL to allowed list in Tools->Options->Expert Advisors: ", url);
      else
         Print("WebRequest error: ", error);
      return false;
   }

   result = CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8);
   return true;
}

//+------------------------------------------------------------------+
//| Make HTTP POST request to server                                   |
//+------------------------------------------------------------------+
bool WebRequestPost(string url, string jsonBody, string &result)
{
   char data[];
   char res[];
   string headers = "Content-Type: application/json\r\n";
   string resultHeaders;

   // Convert JSON body to char array
   StringToCharArray(jsonBody, data, 0, StringLen(jsonBody), CP_UTF8);
   ArrayResize(data, StringLen(jsonBody)); // Remove null terminator

   ResetLastError();

   int timeout = 10000; // 10 seconds for POST

   int code = WebRequest("POST", url, headers, timeout, data, res, resultHeaders);

   if(code == -1)
   {
      int error = GetLastError();
      if(error == 4014)
         Print("Add URL to allowed list in Tools->Options->Expert Advisors: ", url);
      else
         Print("WebRequest POST error: ", error);
      return false;
   }

   result = CharArrayToString(res, 0, WHOLE_ARRAY, CP_UTF8);
   return true;
}

//+------------------------------------------------------------------+
//| Register this pair with the server for multi-instance mode        |
//+------------------------------------------------------------------+
bool RegisterPairWithServer(string eventCurrency, datetime eventTime)
{
   if(!InpMultiInstance)
      return true;  // Not in multi-instance mode

   string pair = _Symbol;
   double currentPrice = SymbolInfoDouble(pair, SYMBOL_BID);
   long spread = SymbolInfoInteger(pair, SYMBOL_SPREAD);
   double point = SymbolInfoDouble(pair, SYMBOL_POINT);
   double spreadPoints = (double)spread;

   // Get zone data from indicator
   string directionBias = "neutral";
   double biasStrength = 0.0;

   if(InpUseZoneIndicator && g_zoneIndicatorHandle != INVALID_HANDLE)
   {
      if(ReadZoneIndicatorData())
      {
         if(g_zoneBias > 0.2)
         {
            directionBias = "bullish";
            biasStrength = g_zoneBias;
         }
         else if(g_zoneBias < -0.2)
         {
            directionBias = "bearish";
            biasStrength = MathAbs(g_zoneBias);
         }
      }
   }

   // Format event time as ISO string
   MqlDateTime dt;
   TimeToStruct(eventTime, dt);
   string eventTimeStr = StringFormat("%04d-%02d-%02dT%02d:%02d:00",
      dt.year, dt.mon, dt.day, dt.hour, dt.min);

   // Build JSON body
   string json = StringFormat(
      "{\"pair\":\"%s\",\"event_currency\":\"%s\",\"event_time\":\"%s\","
      "\"current_price\":%.5f,\"spread_points\":%.0f,"
      "\"zones\":{\"direction_bias\":\"%s\",\"bias_strength\":%.2f}}",
      pair, eventCurrency, eventTimeStr, currentPrice, spreadPoints, directionBias, biasStrength
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/register-pair";
   string result = "";

   if(!WebRequestPost(url, json, result))
   {
      Print("Failed to register pair with server");
      return false;
   }

   // Check response
   if(StringFind(result, "\"status\":\"ok\"") >= 0)
   {
      // Extract event_key from response
      g_registeredEventKey = ExtractJsonString(result, "event_key");
      g_pairRegistered = true;
      Print("Pair ", pair, " registered for event: ", g_registeredEventKey);
      return true;
   }

   Print("Pair registration failed: ", result);
   return false;
}

//+------------------------------------------------------------------+
//| Report zone data to server (for multi-pair analysis)               |
//+------------------------------------------------------------------+
void ReportZoneToServer()
{
   // Read current zone indicator data
   if(!ReadZoneIndicatorData())
      return;  // No zone data available

   string pair = _Symbol;
   double currentPrice = SymbolInfoDouble(pair, SYMBOL_BID);
   long spread = SymbolInfoInteger(pair, SYMBOL_SPREAD);

   // Determine direction bias string
   string directionBias = "neutral";
   if(g_zoneBias > 0.2)
      directionBias = "bullish";
   else if(g_zoneBias < -0.2)
      directionBias = "bearish";

   // Build JSON body
   string json = StringFormat(
      "{\"pair\":\"%s\",\"zone_bias\":%.3f,\"direction_bias\":\"%s\","
      "\"nearest_resistance\":%.5f,\"nearest_support\":%.5f,"
      "\"current_price\":%.5f,\"spread_points\":%d}",
      pair, g_zoneBias, directionBias,
      g_nearestLiqHigh, g_nearestLiqLow,
      currentPrice, (int)spread
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/report-zone";
   string result = "";

   // Send zone report (don't fail if it doesn't work)
   if(WebRequestPost(url, json, result))
   {
      // Zone data sent successfully (don't spam logs)
   }
}

//+------------------------------------------------------------------+
//| Append one timeframe's OHLC as JSON array to the payload           |
//+------------------------------------------------------------------+
bool AppendOhlcJson(string &json, string tfName, ENUM_TIMEFRAMES period, int count)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, false);  // index 0 = oldest (server expects chronological order)
   int copied = CopyRates(_Symbol, period, 0, count, rates);
   if(copied <= 0)
      return false;

   if(StringLen(json) > 0)
      json += ",";  // separator handled here — callers just chain appends
   json += "\"" + tfName + "\":[";
   for(int i = 0; i < copied; i++)
   {
      if(i > 0)
         json += ",";
      json += "{\"time\":" + IntegerToString((long)rates[i].time) +
              StringFormat(",\"open\":%.5f,\"high\":%.5f,\"low\":%.5f,\"close\":%.5f}",
                           rates[i].open, rates[i].high, rates[i].low, rates[i].close);
   }
   json += "]";
   return true;
}

//+------------------------------------------------------------------+
//| Push current OHLC (M5/M15/H1) to the server for LLM market context |
//+------------------------------------------------------------------+
void PushMarketData()
{
   double currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);

   string json = StringFormat(
      "{\"pair\":\"%s\",\"current_price\":%.5f,\"spread_points\":%d,\"ohlc_multi\":{",
      _Symbol, currentPrice, (int)spread);

   string ohlcPart = "";
   AppendOhlcJson(ohlcPart, "M5", PERIOD_M5, 60);
   AppendOhlcJson(ohlcPart, "M15", PERIOD_M15, 40);
   AppendOhlcJson(ohlcPart, "H1", PERIOD_H1, 48);

   if(StringLen(ohlcPart) == 0)
      return;  // No history available yet (e.g. right after terminal start)

   json += ohlcPart + "}}";

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/market-data";
   string result = "";
   WebRequestPost(url, json, result);  // best effort — don't spam logs on failure
}

//+------------------------------------------------------------------+
//| Snapshot bid at T0/T+60/T+300 after event, then report to server   |
//+------------------------------------------------------------------+
void HandleEventReaction()
{
   datetime now = TimeCurrent();
   double bid = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   for(int s = 0; s < REACTION_SLOTS; s++)
   {
      if(!g_reactionPending[s])
         continue;

      if(now >= g_reactionEventTime[s] && g_reactionPrice0[s] == 0)
         g_reactionPrice0[s] = bid;

      if(now >= g_reactionEventTime[s] + 60 && g_reactionPrice1[s] == 0)
         g_reactionPrice1[s] = bid;

      if(now >= g_reactionEventTime[s] + 300)
      {
         if(g_reactionPrice0[s] > 0)
            SendEventReaction(s, bid);
         else
            Print("Event reaction NOT reported (no tick captured at event time)");
         g_reactionPending[s] = false;
      }
   }
}

//+------------------------------------------------------------------+
//| Escape a string for embedding inside a JSON string literal         |
//+------------------------------------------------------------------+
string EscapeJson(string value)
{
   StringReplace(value, "\\", "\\\\");
   StringReplace(value, "\"", "\\\"");
   return value;
}

//+------------------------------------------------------------------+
//| POST the measured reaction to /api/event-reaction                  |
//+------------------------------------------------------------------+
void SendEventReaction(int slot, double priceAfter5Min)
{
   string json = StringFormat(
      "{\"pair\":\"%s\",\"event_name\":\"%s\",\"currency\":\"%s\",\"event_time\":\"%s\","
      "\"price_at_event\":%.5f,\"price_after_1min\":%.5f,\"price_after_5min\":%.5f}",
      _Symbol, EscapeJson(g_reactionEventName[slot]), EscapeJson(g_reactionCurrency[slot]),
      EscapeJson(g_reactionEventTimeUTC[slot]),
      g_reactionPrice0[slot], g_reactionPrice1[slot], priceAfter5Min);

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/event-reaction";
   string result = "";

   if(WebRequestPost(url, json, result))
      Print("Event reaction reported: ", g_reactionEventName[slot], " (", g_reactionCurrency[slot], ") ",
            g_reactionPrice0[slot], " -> ", priceAfter5Min);
   else
      Print("Failed to report event reaction for ", g_reactionEventName[slot]);
}

//+------------------------------------------------------------------+
//| Check for trading signals from server                              |
//+------------------------------------------------------------------+
void CheckForSignals()
{
   // First, report our zone data to server for multi-pair analysis
   if(InpUseZoneIndicator && g_zoneIndicatorHandle != INVALID_HANDLE)
   {
      ReportZoneToServer();
   }

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/signal";

   // Add pair parameter so server knows which EA is asking
   url += "?pair=" + _Symbol;

   string result = "";

   if(!WebRequest(url, result))
   {
      Print("Failed to get signal from server");
      return;
   }

   //--- Parse JSON response
   if(StringFind(result, "\"signal\":true") < 0)
   {
      // No signal
      return;
   }

   //--- Extract signal data
   string direction = ExtractJsonString(result, "direction");
   string pair = ExtractJsonString(result, "pair");
   double confidence = ExtractJsonDouble(result, "confidence");
   double lotPercent = ExtractJsonDouble(result, "lot_percent");
   int exitMinutes = (int)ExtractJsonDouble(result, "exit_minutes");
   double slPercent = ExtractJsonDouble(result, "stop_loss_percent");
   double slPips = ExtractJsonDouble(result, "stop_loss_pips");
   double tpPips = ExtractJsonDouble(result, "take_profit_pips");
   int timeUntilEvent = (int)ExtractJsonDouble(result, "time_until_event");
   string eventName = ExtractJsonString(result, "event_name");
   string reasoning = ExtractJsonString(result, "reasoning");

   //--- Validate signal
   //--- forced:true = server test mode (FORCE_DECISION): decisions honestly
   //--- report low confidence, so the confidence gate must not reject them
   bool forcedSignal = (StringFind(result, "\"forced\":true") >= 0);
   if(confidence < InpMinConfidence)
   {
      if(forcedSignal)
      {
         Print("Forced test-mode signal — bypassing confidence gate (", confidence, " < ", InpMinConfidence, ")");
      }
      else
      {
         Print("Signal confidence too low: ", confidence);
         return;
      }
   }

   if(direction != "BUY" && direction != "SELL")
   {
      Print("Invalid direction: ", direction);
      return;
   }

   //--- Check if event is within reasonable time window (1 hour)
   if(timeUntilEvent > 3600 || timeUntilEvent < 0)
   {
      // Event too far or passed
      return;
   }

   //--- Set up for event
   g_waitingForEvent = true;
   g_eventTime = TimeCurrent() + timeUntilEvent;
   g_eventPair = pair;
   g_eventDirection = direction;
   g_eventLotPercent = (lotPercent > 0) ? lotPercent : InpMaxLotPercent;
   g_eventExitMinutes = (exitMinutes > 0) ? exitMinutes : InpExitMinutesAfter;
   g_eventSLPercent = (slPercent > 0) ? slPercent : InpDefaultSLPercent;
   g_eventSLPips = slPips;  // SL in pips from LLM (0 if not provided)
   g_eventTPPips = tpPips;  // TP in pips from LLM (0 if not provided)

   //--- Arm post-event reaction tracking (snapshots at T0 / T+60s / T+300s)
   if(InpReportReactions)
   {
      int slot = -1;
      for(int s = 0; s < REACTION_SLOTS; s++)
      {
         // Skip if a slot already tracks this event (signal re-delivery after
         // e.g. a spread-rejected entry; ±2s tolerance for countdown rounding)
         if(g_reactionPending[s] && g_reactionEventName[s] == eventName &&
            MathAbs((long)(g_reactionEventTime[s] - g_eventTime)) <= 2)
         {
            slot = -2;  // already armed
            break;
         }
         if(slot == -1 && !g_reactionPending[s])
            slot = s;
      }
      if(slot >= 0)
      {
         g_reactionPending[slot] = true;
         g_reactionEventTime[slot] = g_eventTime;
         g_reactionEventName[slot] = eventName;
         g_reactionCurrency[slot] = ExtractJsonString(result, "event_currency");
         g_reactionEventTimeUTC[slot] = ExtractJsonString(result, "event_time");
         g_reactionPrice0[slot] = 0;
         g_reactionPrice1[slot] = 0;
      }
      else if(slot == -1)
         Print("WARNING: all reaction slots busy — reaction for ", eventName, " will not be tracked");
   }

   //--- Check zone bias and potentially adjust direction
   string zoneBiasInfo = "";
   if(InpUseZoneBiasForDirection && InpUseZoneIndicator)
   {
      if(ReadZoneIndicatorData())
      {
         zoneBiasInfo = StringFormat(" | Zone Bias: %.2f", g_zoneBias);

         //--- If zone bias strongly disagrees with direction, reduce confidence/lot
         if((direction == "BUY" && g_zoneBias < -0.5) ||
            (direction == "SELL" && g_zoneBias > 0.5))
         {
            Print("WARNING: Zone bias (", g_zoneBias, ") conflicts with signal direction (", direction, ")");
            Print("Reducing lot size by 30% due to conflicting bias");
            g_eventLotPercent *= 0.7;  // Reduce lot if bias conflicts
         }
         else if((direction == "BUY" && g_zoneBias > 0.3) ||
                 (direction == "SELL" && g_zoneBias < -0.3))
         {
            Print("Zone bias (", g_zoneBias, ") CONFIRMS direction (", direction, ")");
            zoneBiasInfo += " [CONFIRMS]";
         }
      }
   }

   //--- Store for panel display
   g_currentEventName = eventName;
   g_currentConfidence = confidence;

   //--- Update panel
   if(g_panelCreated)
   {
      g_panel.SetEvent(eventName);
      g_panel.SetPair(pair);
      g_panel.SetDirection(direction, confidence);
      g_panel.SetCountdown(timeUntilEvent);
      g_panel.SetStatus("Signal received", clrYellow);

      //--- Check trade mode
      ENUM_TRADE_MODE mode = g_panel.GetTradeMode();

      if(mode == MODE_MANUAL)
      {
         //--- In manual mode, ignore LLM direction - wait for user
         g_panel.SetStatus("Waiting for manual...", clrOrange);
         g_panel.ResetManualDirection();
         g_eventDirection = "";  // Clear - will be set by user
         Print("Manual mode - waiting for user to select direction");
      }
      else if(mode == MODE_CONFIRM)
      {
         //--- In confirm mode, show LLM suggestion but wait for approval
         g_panel.SetStatus("Confirm trade?", clrOrange);
         g_panel.ResetManualDirection();
         Print("Confirm mode - LLM suggests ", direction, ", waiting for user approval");
      }
      // MODE_LLM_AUTO - use direction from server as-is
   }

   Print("==============================================");
   Print("NEW SIGNAL RECEIVED");
   Print("Event: ", eventName);
   Print("Pair: ", pair, " | Direction: ", direction, zoneBiasInfo);
   Print("Confidence: ", DoubleToString(confidence * 100, 1), "%");
   Print("Event in: ", timeUntilEvent, " seconds");
   if(slPips > 0 || tpPips > 0)
      Print("LLM Targets - SL: ", slPips, " pips | TP: ", tpPips, " pips");
   Print("Reasoning: ", reasoning);
   Print("==============================================");
}

//+------------------------------------------------------------------+
//| Execute the event trade                                            |
//+------------------------------------------------------------------+
void ExecuteEventTrade()
{
   //--- Check trade mode from panel
   if(g_panelCreated)
   {
      ENUM_TRADE_MODE mode = g_panel.GetTradeMode();

      if(mode == MODE_MANUAL || mode == MODE_CONFIRM)
      {
         //--- Need user input
         if(!g_panel.IsSignalApproved())
         {
            //--- Still waiting for user - don't trade yet
            Print("Waiting for user to select direction...");
            g_panel.SetStatus("Select direction!", clrRed);
            return;  // Don't execute - wait for user
         }

         //--- User made a selection
         string manualDir = g_panel.GetManualDirection();

         if(manualDir == "SKIP")
         {
            Print("User chose to SKIP this trade");
            g_panel.SetStatus("Trade skipped", clrGray);
            ResetEventWait();
            return;
         }

         if(manualDir == "BUY" || manualDir == "SELL")
         {
            g_eventDirection = manualDir;
            Print("Using manual direction: ", manualDir);
         }
      }

      //--- Update panel - entering trade
      g_panel.SetStatus("TRADING!", clrLime);
   }

   //--- Validate direction
   if(g_eventDirection != "BUY" && g_eventDirection != "SELL")
   {
      Print("ERROR: No valid direction set. Cannot trade.");
      ResetEventWait();
      return;
   }

   //--- Check spread
   string symbol = ConvertPairToSymbol(g_eventPair);
   if(!SymbolSelect(symbol, true))
   {
      Print("Cannot select symbol: ", symbol);
      ResetEventWait();
      return;
   }

   // Convert spread from points to pips
   // For 5-digit brokers: 1 pip = 10 points (standard pairs like EURUSD: 0.00010)
   // For 3-digit brokers: 1 pip = 10 points (JPY pairs like USDJPY: 0.010)
   long spreadPoints = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   double spread = (double)spreadPoints / 10.0;  // Universal conversion: points to pips

   //--- Dynamic lot reduction based on spread (from python-workspace skill)
   double spreadMultiplier = GetSpreadLotMultiplier(spread);

   if(spreadMultiplier <= 0)
   {
      Print("Spread EXTREME: ", DoubleToString(spread, 1), " pips. No trade.");
      ResetEventWait();
      return;
   }

   if(spread > InpMaxSpreadPips)
   {
      Print("Spread too high: ", DoubleToString(spread, 1), " pips. Max: ", InpMaxSpreadPips);
      ResetEventWait();
      return;
   }

   //--- Calculate lot size with spread adjustment
   double baseLotPercent = g_eventLotPercent * spreadMultiplier;
   double lots = CalculateLotSize(symbol, baseLotPercent);

   if(spreadMultiplier < 1.0)
   {
      Print("Spread warning: ", DoubleToString(spread, 1), " pips. Lot reduced to ", DoubleToString(spreadMultiplier * 100, 0), "%");
   }
   if(lots <= 0)
   {
      Print("Invalid lot size calculated");
      ResetEventWait();
      return;
   }

   //--- Get current prices
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);

   //--- Calculate Stop Loss and TP from Zone Indicator or fallback
   double sl = 0;
   double tp1 = 0, tp2 = 0;
   double entryPrice = (g_eventDirection == "BUY") ? ask : bid;

   if(InpUseStopLoss)
   {
      //--- Priority 1: Use LLM-provided SL/TP in pips
      if(g_eventSLPips > 0)
      {
         double pip = point * 10;
         if(g_eventDirection == "BUY")
            sl = entryPrice - g_eventSLPips * pip;
         else
            sl = entryPrice + g_eventSLPips * pip;

         //--- Also set TP1 if LLM provided it
         if(g_eventTPPips > 0)
         {
            if(g_eventDirection == "BUY")
               tp1 = entryPrice + g_eventTPPips * pip;
            else
               tp1 = entryPrice - g_eventTPPips * pip;
         }

         Print("Using LLM targets - SL: ", g_eventSLPips, " pips, TP: ", g_eventTPPips, " pips");
      }
      //--- Priority 2: Try to get zone-based targets
      else if(InpUseZoneIndicator && g_zoneIndicatorHandle != INVALID_HANDLE)
      {
         GetZoneBasedTargets(g_eventDirection, entryPrice, tp1, tp2, sl);
         Print("Using ZONE INDICATOR targets");
      }
      else
      {
         //--- Fallback: SL based on pips (default 25 pips)
         double pip = point * 10;
         double slPips = 25;  // Default 25 pips SL

         if(g_eventDirection == "BUY")
            sl = entryPrice - slPips * pip;
         else
            sl = entryPrice + slPips * pip;

         Print("Using DEFAULT pip-based SL: ", slPips, " pips");
      }

      //--- Safety: Ensure SL is within reasonable bounds (20-100 pips)
      double slDistance = MathAbs(sl - entryPrice);
      double minSL = 20 * point * 10;
      double maxSL = 100 * point * 10;

      if(slDistance < minSL || slDistance > maxSL)
      {
         slDistance = MathMax(minSL, MathMin(maxSL, slDistance));
         if(g_eventDirection == "BUY")
            sl = entryPrice - slDistance;
         else
            sl = entryPrice + slDistance;
         Print("SL adjusted to bounds: ", sl);
      }

      sl = NormalizeDouble(sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
   }

   //--- Execute trade
   bool success = false;
   if(g_eventDirection == "BUY")
   {
      success = trade.Buy(lots, symbol, ask, sl, 0, "SkyTower-AI");
   }
   else
   {
      success = trade.Sell(lots, symbol, bid, sl, 0, "SkyTower-AI");
   }

   if(success)
   {
      g_currentTicket = trade.ResultOrder();
      g_lastTradeTime = TimeCurrent();
      g_todayTrades++;
      g_originalLots = lots;  // Store for partial close calculation

      Print("==============================================");
      Print("TRADE EXECUTED");
      Print("Symbol: ", symbol);
      Print("Direction: ", g_eventDirection);
      Print("Lots: ", DoubleToString(lots, 2));
      Print("Price: ", (g_eventDirection == "BUY") ? ask : bid);
      Print("Spread: ", DoubleToString(spread, 1), " pips (", GetSpreadStatus(spread), ")");
      Print("Lot Multiplier: ", DoubleToString(spreadMultiplier * 100, 0), "%");
      if(sl > 0) Print("Stop Loss: ", sl);
      Print("Ticket: ", g_currentTicket);
      Print("==============================================");

      //--- Initialize Smart Exit Manager with new position
      double entry_price = (g_eventDirection == "BUY") ? ask : bid;
      g_smartExit.OnNewPosition(g_currentTicket, symbol, g_eventDirection, entry_price, lots);

      //--- Print smart exit targets and apply SL from server
      STradeTargets targets = g_smartExit.GetCurrentTargets();
      if(targets.valid)
      {
         Print("--- Smart Exit Targets ---");
         Print("TP1: ", targets.tp1, " (", targets.tp1_pips, " pips) - ", targets.tp1_zone_type);
         Print("TP2: ", targets.tp2, " (", targets.tp2_pips, " pips) - ", targets.tp2_zone_type);
         Print("SL: ", targets.sl, " (", targets.sl_pips, " pips) - ", targets.sl_zone_type);
         Print("Risk/Reward: ", targets.risk_reward);
         Print("--------------------------");

         //--- Apply SL from server targets (override initial SL if server provided better one)
         if(targets.sl > 0 && InpUseStopLoss)
         {
            double new_sl = NormalizeDouble(targets.sl, (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS));
            double current_sl = PositionGetDouble(POSITION_SL);

            // Only modify if different from current SL
            if(MathAbs(new_sl - current_sl) > point)
            {
               if(trade.PositionModify(g_currentTicket, new_sl, 0))
               {
                  Print("SL updated from server targets: ", new_sl);
               }
               else
               {
                  Print("WARNING: Could not update SL: ", trade.ResultRetcodeDescription());
               }
            }
         }
      }

      //--- Notify server (new AI position management)
      NotifyPositionOpened();
   }
   else
   {
      Print("Trade execution failed: ", trade.ResultRetcodeDescription());
   }

   //--- Keep waiting for exit
   g_waitingForEvent = false;
}

//+------------------------------------------------------------------+
//| Manage open positions with AI-driven decisions                     |
//+------------------------------------------------------------------+
void ManageOpenPositions()
{
   if(g_currentTicket == 0)
      return;

   if(!PositionSelectByTicket(g_currentTicket))
   {
      // Position was closed externally (SL hit, manual close, etc.)
      Print("Position ", g_currentTicket, " no longer exists - notifying server");
      NotifyPositionClosed(0, 0.0, "Position closed externally (SL/TP/manual)");
      g_currentTicket = 0;
      g_smartExit.OnPositionClosed();
      g_aiManagementActive = false;
      ResetSmartExitState();
      return;
   }

   string symbol = PositionGetString(POSITION_SYMBOL);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double profit = PositionGetDouble(POSITION_PROFIT);
   long spreadPoints = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   double spreadPips = (double)spreadPoints / 10.0;

   //--- EA-side safety guardrails (immediate, no server needed) ---

   // Guardrail 1: Max loss in USD
   if(profit < -InpMaxLossUSD)
   {
      Print("=== EA GUARDRAIL: MAX LOSS $", InpMaxLossUSD, " ===");
      Print("Current P/L: $", DoubleToString(profit, 2));
      ClosePosition("EA guardrail: max loss $" + DoubleToString(InpMaxLossUSD, 0) + " exceeded");
      return;
   }

   // Guardrail 2: Max hold time
   datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
   int minutesOpen = (int)((TimeCurrent() - openTime) / 60);
   if(minutesOpen >= InpMaxHoldMinutes)
   {
      Print("=== EA GUARDRAIL: MAX HOLD TIME ", InpMaxHoldMinutes, " min ===");
      ClosePosition("EA guardrail: max hold " + IntegerToString(InpMaxHoldMinutes) + "min");
      return;
   }

   // Guardrail 3: Emergency spread
   if(spreadPips >= InpEmergencySpreadPips)
   {
      Print("=== EA GUARDRAIL: EMERGENCY SPREAD ", DoubleToString(spreadPips, 1), " pips ===");
      ClosePosition("EA guardrail: emergency spread " + DoubleToString(spreadPips, 1) + " pips");
      return;
   }

   //--- AI Position Management: report status and get command ---
   int reportInterval = GetReportInterval();
   if(TimeCurrent() - g_lastPositionReport >= reportInterval)
   {
      g_lastPositionReport = TimeCurrent();
      ReportAndGetCommand(symbol, profit, spreadPips);
   }
}

//+------------------------------------------------------------------+
//| Get adaptive report interval based on position age                 |
//+------------------------------------------------------------------+
int GetReportInterval()
{
   if(g_currentTicket == 0)
      return InpCheckInterval; // 60s when no position

   if(!PositionSelectByTicket(g_currentTicket))
      return InpCheckInterval;

   datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
   int secondsOpen = (int)(TimeCurrent() - openTime);

   if(secondsOpen < 120) return 5;   // First 2 minutes: every 5s (hot period)
   return 15;                          // After: every 15s
}

//+------------------------------------------------------------------+
//| Report position status to server and process AI command            |
//+------------------------------------------------------------------+
void ReportAndGetCommand(string symbol, double profit, double spreadPips)
{
   double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double currentPrice = (g_eventDirection == "BUY") ?
      SymbolInfoDouble(symbol, SYMBOL_BID) : SymbolInfoDouble(symbol, SYMBOL_ASK);
   double lots = PositionGetDouble(POSITION_VOLUME);
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);

   // Read zone data
   ReadZoneIndicatorData();

   // Build JSON report
   string json = StringFormat(
      "{\"ticket\":%d,\"symbol\":\"%s\",\"direction\":\"%s\","
      "\"entry_price\":%.5f,\"current_price\":%.5f,"
      "\"lots\":%.2f,\"remaining_lots\":%.2f,"
      "\"sl\":%.5f,\"tp\":%.5f,"
      "\"profit_usd\":%.2f,\"tick_value\":%.4f,"
      "\"spread_pips\":%.1f,\"account_balance\":%.2f,"
      "\"zone_bias\":%.3f,\"nearest_resistance\":%.5f,\"nearest_support\":%.5f}",
      (int)g_currentTicket, symbol, g_eventDirection,
      entryPrice, currentPrice,
      g_originalLots, lots,
      sl, tp,
      profit, tickValue,
      spreadPips, balance,
      g_zoneBias, g_nearestLiqHigh, g_nearestLiqLow
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/position/report";
   string result = "";

   if(!WebRequestPost(url, json, result))
   {
      Print("WARNING: Failed to report position status to server");
      return;
   }

   // Process server response (contains AI command)
   ProcessServerCommand(result, symbol);
}

//+------------------------------------------------------------------+
//| Process AI command from server response                            |
//+------------------------------------------------------------------+
void ProcessServerCommand(string response, string symbol)
{
   // Check if server has a command
   if(StringFind(response, "\"has_command\":true") < 0)
      return;  // No command, HOLD

   string action = ExtractJsonString(response, "action");
   if(action == "")
      return;

   // Extract command from nested "command" object
   // The response format is: {"has_command":true,"command":{"action":"...","sl_price":...}}
   int cmdStart = StringFind(response, "\"command\":");
   if(cmdStart < 0) return;
   string cmdJson = StringSubstr(response, cmdStart);

   action = ExtractJsonString(cmdJson, "action");
   string reason = ExtractJsonString(cmdJson, "reason");

   if(action == "HOLD" || action == "")
      return;

   Print("=== AI COMMAND: ", action, " ===");
   Print("Reason: ", reason);

   if(action == "CLOSE")
   {
      ClosePosition(reason);
   }
   else if(action == "MODIFY_SL")
   {
      double newSL = ExtractJsonDouble(cmdJson, "sl_price");
      if(newSL > 0)
      {
         if(ModifyPositionSL(g_currentTicket, newSL))
         {
            Print("AI: SL modified to ", newSL);
            // Track that AI moved SL to BE if applicable
            double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
            double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
            if(MathAbs(newSL - entryPrice) < point * 20) // within ~2 pips of entry
               g_slMovedToBE = true;
         }
         else
         {
            Print("AI: Failed to modify SL: ", trade.ResultRetcodeDescription());
         }
      }
   }
   else if(action == "MODIFY_TP")
   {
      double newTP = ExtractJsonDouble(cmdJson, "tp_price");
      if(newTP > 0)
      {
         double currentSL = PositionGetDouble(POSITION_SL);
         if(trade.PositionModify(g_currentTicket, currentSL, newTP))
            Print("AI: TP modified to ", newTP);
         else
            Print("AI: Failed to modify TP: ", trade.ResultRetcodeDescription());
      }
   }
   else if(action == "PARTIAL_CLOSE")
   {
      double closePercent = ExtractJsonDouble(cmdJson, "close_percent");
      if(closePercent > 0)
      {
         double currentLots = PositionGetDouble(POSITION_VOLUME);
         double closeLots = currentLots * closePercent / 100.0;
         closeLots = NormalizeVolume(closeLots, symbol);

         if(closeLots > 0)
         {
            if(trade.PositionClosePartial(g_currentTicket, closeLots))
            {
               Print("AI: Partial close ", DoubleToString(closePercent, 0), "% (", closeLots, " lots)");
               g_tp1Hit = true; // Mark partial close done
            }
            else
            {
               Print("AI: Partial close failed: ", trade.ResultRetcodeDescription());
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Notify server that position was opened (replaces old notify)       |
//+------------------------------------------------------------------+
void NotifyPositionOpened()
{
   if(g_currentTicket == 0) return;
   if(!PositionSelectByTicket(g_currentTicket)) return;

   string symbol = PositionGetString(POSITION_SYMBOL);
   double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double lots = PositionGetDouble(POSITION_VOLUME);
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);

   string json = StringFormat(
      "{\"ticket\":%d,\"symbol\":\"%s\",\"direction\":\"%s\","
      "\"entry_price\":%.5f,\"lots\":%.2f,\"sl\":%.5f,\"tp\":%.5f,"
      "\"tick_value\":%.4f,\"account_balance\":%.2f,"
      "\"event_name\":\"%s\"}",
      (int)g_currentTicket, symbol, g_eventDirection,
      entryPrice, lots, sl, tp,
      tickValue, balance,
      g_currentEventName
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/position/opened";
   string result = "";

   if(WebRequestPost(url, json, result))
   {
      Print("Server notified: position opened (AI management active)");
      g_aiManagementActive = true;
      g_lastPositionReport = TimeCurrent();
   }
   else
   {
      Print("WARNING: Could not notify server of position open - falling back");
      // Still notify via old endpoint as fallback
      NotifyTradeExecuted();
   }
}

//+------------------------------------------------------------------+
//| Notify server that position was closed                             |
//+------------------------------------------------------------------+
void NotifyPositionClosed(double closePrice, double profit, string reason)
{
   string json = StringFormat(
      "{\"ticket\":%d,\"close_price\":%.5f,\"profit\":%.2f,\"reason\":\"%s\"}",
      (int)g_currentTicket, closePrice, profit, reason
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/position/closed";
   string result = "";

   if(WebRequestPost(url, json, result))
   {
      Print("Server notified: position closed (P/L: $", DoubleToString(profit, 2), ")");
   }
   else
   {
      Print("WARNING: Could not notify server of position close");
   }

   g_aiManagementActive = false;
}

//+------------------------------------------------------------------+
//| Move SL to break-even                                              |
//+------------------------------------------------------------------+
void MoveSLToBreakeven(string symbol)
{
   if(!PositionSelectByTicket(g_currentTicket))
      return;

   double entry_price = PositionGetDouble(POSITION_PRICE_OPEN);
   double current_sl = PositionGetDouble(POSITION_SL);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);

   //--- Add small buffer (1 pip) to ensure we don't get stopped out at exact entry
   double buffer = point * 10;  // 1 pip for 5-digit broker

   double new_sl;
   if(g_eventDirection == "BUY")
   {
      new_sl = entry_price + buffer;
      if(new_sl > current_sl)
      {
         if(ModifyPositionSL(g_currentTicket, new_sl))
         {
            g_slMovedToBE = true;
            Print("SL moved to break-even: ", new_sl);
         }
      }
   }
   else // SELL
   {
      new_sl = entry_price - buffer;
      if(new_sl < current_sl || current_sl == 0)
      {
         if(ModifyPositionSL(g_currentTicket, new_sl))
         {
            g_slMovedToBE = true;
            Print("SL moved to break-even: ", new_sl);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Modify position stop loss                                          |
//+------------------------------------------------------------------+
bool ModifyPositionSL(ulong ticket, double new_sl)
{
   if(!PositionSelectByTicket(ticket))
      return false;

   double current_tp = PositionGetDouble(POSITION_TP);
   string symbol = PositionGetString(POSITION_SYMBOL);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   new_sl = NormalizeDouble(new_sl, digits);

   return trade.PositionModify(ticket, new_sl, current_tp);
}

//+------------------------------------------------------------------+
//| Close position with reason                                         |
//+------------------------------------------------------------------+
void ClosePosition(string reason)
{
   if(g_currentTicket == 0)
      return;

   if(!PositionSelectByTicket(g_currentTicket))
   {
      g_currentTicket = 0;
      return;
   }

   double profit = PositionGetDouble(POSITION_PROFIT);
   double closePrice = (g_eventDirection == "BUY") ?
      SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_BID) :
      SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_ASK);

   Print("Closing position - Reason: ", reason);
   Print("P/L: $", DoubleToString(profit, 2));

   if(trade.PositionClose(g_currentTicket))
   {
      Print("Position closed successfully");

      // Notify server about close
      NotifyPositionClosed(closePrice, profit, reason);

      g_currentTicket = 0;
      g_smartExit.OnPositionClosed();
      g_aiManagementActive = false;
      ResetEventWait();
      ResetSmartExitState();
   }
   else
   {
      Print("Failed to close position: ", trade.ResultRetcodeDescription());
   }
}

//+------------------------------------------------------------------+
//| Normalize volume to symbol specifications                          |
//+------------------------------------------------------------------+
double NormalizeVolume(double lots, string symbol)
{
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);

   lots = MathFloor(lots / lotStep) * lotStep;
   lots = MathMax(lots, minLot);
   lots = MathMin(lots, maxLot);

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Reset smart exit state                                             |
//+------------------------------------------------------------------+
void ResetSmartExitState()
{
   g_tp1Hit = false;
   g_slMovedToBE = false;
   g_originalLots = 0;
}

//+------------------------------------------------------------------+
//| Calculate lot size based on risk                                   |
//+------------------------------------------------------------------+
double CalculateLotSize(string symbol, double lotPercent)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   double riskAmount = balance * InpRiskPercent / 100.0;

   //--- Get symbol info
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   double lotStep = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxLot = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);

   //--- Calculate base lots
   double lots = riskAmount / (tickValue * 100);

   //--- Apply lot percent
   lots = lots * lotPercent / 100.0;

   //--- Normalize to lot step
   lots = MathFloor(lots / lotStep) * lotStep;

   //--- Apply limits
   lots = MathMax(lots, minLot);
   lots = MathMin(lots, maxLot);

   return NormalizeDouble(lots, 2);
}

//+------------------------------------------------------------------+
//| Convert pair format (EURUSD -> EURUSD or EUR/USD -> EURUSD)        |
//+------------------------------------------------------------------+
string ConvertPairToSymbol(string pair)
{
   string symbol = pair;
   StringReplace(symbol, "/", "");

   //--- Try with different suffixes for Purple Trading
   string suffixes[] = {"", ".a", ".r", "_SB", ".pro"};

   for(int i = 0; i < ArraySize(suffixes); i++)
   {
      string testSymbol = symbol + suffixes[i];
      if(SymbolSelect(testSymbol, true))
         return testSymbol;
   }

   return symbol;
}

//+------------------------------------------------------------------+
//| Notify server that trade was executed                              |
//+------------------------------------------------------------------+
void NotifyTradeExecuted()
{
   // Include pair in URL for multi-instance coordination
   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort)
              + "/api/trade-executed?pair=" + _Symbol;

   string result = "";
   if(WebRequest(url, result))
   {
      Print("Server notified of trade execution for ", _Symbol);
   }
   else
   {
      Print("WARNING: Could not notify server of trade execution");
   }
}

//+------------------------------------------------------------------+
//| Reset event waiting state                                          |
//+------------------------------------------------------------------+
void ResetEventWait()
{
   g_waitingForEvent = false;
   g_eventTime = 0;
   g_eventPair = "";
   g_eventDirection = "";
   g_eventLotPercent = 0;
   g_eventExitMinutes = 0;
   g_eventSLPercent = 0;
   g_eventSLPips = 0;
   g_eventTPPips = 0;

   // Reset multi-instance registration for next event
   g_pairRegistered = false;
   g_registeredEventKey = "";
}

//+------------------------------------------------------------------+
//| Extract string value from JSON                                     |
//+------------------------------------------------------------------+
string ExtractJsonString(string json, string key)
{
   string searchKey = "\"" + key + "\":\"";
   int startPos = StringFind(json, searchKey);
   if(startPos < 0)
      return "";

   startPos += StringLen(searchKey);
   int endPos = StringFind(json, "\"", startPos);
   if(endPos < 0)
      return "";

   return StringSubstr(json, startPos, endPos - startPos);
}

//+------------------------------------------------------------------+
//| Parse ISO datetime string to MQL5 datetime (returns UTC time)      |
//| Handles: 2026-01-23T12:00:00, 2026-01-23T12:00:00+00:00, etc      |
//| IMPORTANT: This returns UTC datetime - use with TimeGMT() not     |
//|            TimeCurrent() for comparison!                           |
//+------------------------------------------------------------------+
datetime ParseISODateTime(string isoStr)
{
   // Remove timezone info if present (we assume all input is UTC)
   int tzPos = StringFind(isoStr, "+");
   if(tzPos > 0) isoStr = StringSubstr(isoStr, 0, tzPos);
   tzPos = StringFind(isoStr, "Z");
   if(tzPos > 0) isoStr = StringSubstr(isoStr, 0, tzPos);

   // Replace T with space for StringToTime
   StringReplace(isoStr, "T", " ");

   // Handle format YYYY-MM-DD HH:MM:SS
   // MQL5 StringToTime expects YYYY.MM.DD HH:MM:SS
   StringReplace(isoStr, "-", ".");

   return StringToTime(isoStr);
}

//+------------------------------------------------------------------+
//| Get broker timezone offset in seconds (broker time - UTC)          |
//| Positive = broker is ahead of UTC, Negative = broker behind UTC    |
//+------------------------------------------------------------------+
int GetBrokerTimezoneOffset()
{
   // TimeCurrent() = broker server time
   // TimeGMT() = UTC time
   // Offset = broker - UTC
   return (int)(TimeCurrent() - TimeGMT());
}

//+------------------------------------------------------------------+
//| Convert UTC datetime to broker server datetime                     |
//+------------------------------------------------------------------+
datetime UTCToBrokerTime(datetime utcTime)
{
   return utcTime + GetBrokerTimezoneOffset();
}

//+------------------------------------------------------------------+
//| Convert broker server datetime to UTC datetime                     |
//+------------------------------------------------------------------+
datetime BrokerTimeToUTC(datetime brokerTime)
{
   return brokerTime - GetBrokerTimezoneOffset();
}

//+------------------------------------------------------------------+
//| Extract double value from JSON                                     |
//+------------------------------------------------------------------+
double ExtractJsonDouble(string json, string key)
{
   string patterns[] = {
      "\"" + key + "\":",
      "\"" + key + "\": "
   };

   for(int p = 0; p < ArraySize(patterns); p++)
   {
      int startPos = StringFind(json, patterns[p]);
      if(startPos >= 0)
      {
         startPos += StringLen(patterns[p]);

         // Skip whitespace
         while(startPos < StringLen(json) && StringGetCharacter(json, startPos) == ' ')
            startPos++;

         // Find end of number
         int endPos = startPos;
         while(endPos < StringLen(json))
         {
            int ch = StringGetCharacter(json, endPos);
            if((ch >= '0' && ch <= '9') || ch == '.' || ch == '-')
               endPos++;
            else
               break;
         }

         if(endPos > startPos)
         {
            string numStr = StringSubstr(json, startPos, endPos - startPos);
            return StringToDouble(numStr);
         }
      }
   }

   return 0;
}

//+------------------------------------------------------------------+
//| Get lot multiplier based on spread level                           |
//| Based on SPREAD_LOT_REDUCTION from config.py                       |
//+------------------------------------------------------------------+
double GetSpreadLotMultiplier(double spreadPips)
{
   //--- Spread thresholds (from python config)
   //--- low: < 3 pips = 100%
   //--- medium: < 6 pips = 80%
   //--- high: < 10 pips = 60%
   //--- extreme: >= 15 pips = 0% (no trade)

   if(spreadPips < 3.0)
      return 1.0;      // 100% lot
   else if(spreadPips < 6.0)
      return 0.8;      // 80% lot
   else if(spreadPips < 10.0)
      return 0.6;      // 60% lot
   else if(spreadPips < 15.0)
      return 0.4;      // 40% lot (additional level)
   else
      return 0.0;      // No trade - spread extreme
}

//+------------------------------------------------------------------+
//| Get spread status string for logging                               |
//+------------------------------------------------------------------+
string GetSpreadStatus(double spreadPips)
{
   if(spreadPips < 3.0)
      return "OK";
   else if(spreadPips < 6.0)
      return "MEDIUM";
   else if(spreadPips < 10.0)
      return "HIGH";
   else if(spreadPips < 15.0)
      return "VERY_HIGH";
   else
      return "EXTREME";
}

//+------------------------------------------------------------------+
//| Timer function for periodic checks                                 |
//+------------------------------------------------------------------+
void OnTimer()
{
   // Can be used for additional periodic tasks
}
//+------------------------------------------------------------------+
