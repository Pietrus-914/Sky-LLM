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
#include "SkyTowerAI_Units.mqh"
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
input double   InpMaxMarginUsePercent = 0.0;     // Max % of free margin per position (0 = no cap = today's behaviour; set ~50 on gold/index charts at 1:100)
input double   InpMaxLotPercent = 80.0;          // Max Lot % (default from server)
input int      InpSlippage = 50;                 // Max Slippage (points)
input double   InpMinConfidence = 0.5;           // Min Confidence to trade
input bool     InpUseConfidenceLot = true;       // Reduce lot by decision confidence (server lot%)
input bool     InpUseSpreadLotReduction = true;  // Reduce lot by spread level

input group "=== Safety Settings ==="
input double   InpMaxSpreadPips = 10.0;          // Max Spread (pips)
input double   InpPipSizeOverride = 0.0;         // Pip size in price units (0 = auto forex rule; XAUUSD 0.10, GER40/US500 1.0)
input bool     InpUseStopLoss = true;            // Use Stop Loss
input double   InpDefaultSLPercent = 40.0;       // Default SL % of risk
// NOTE: the daily trade limit lives ONLY on the server (dashboard →
// Event Config → "Max trades per day") — the server stops serving
// signals once the limit is hit, so there is no per-chart input.

input group "=== Timing Settings ==="
input int      InpEntrySecondsBefore = 15;       // Entry seconds before event
input int      InpExitMinutesAfter = 10;         // Exit minutes after event (fallback)

input group "=== Smart Exit Settings ==="
input bool     InpUseZoneTargets = true;         // Use Zone-Based Targets
input int      InpMaxHoldMinutes = 30;           // Maximum Position Hold Time (minutes)

input group "=== Zone Indicator Settings ==="
input bool     InpUseZoneIndicator = true;       // Use SkyTower_Zones Indicator
input bool     InpUseZoneBiasForDirection = true;// Use Zone Bias for Direction

input group "=== AI Position Management ==="
// NOTE: the per-trade max loss (risk budget) lives ONLY on the server
// (dashboard → Event Config → "Max loss per trade USD"). It arrives in
// every /api/signal response as max_loss_usd and is stored in
// g_maxLossUSD — no per-chart input to keep in sync.
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

enum ENUM_RECOVERY_STATE
{
   RECOVERY_PENDING = 0,
   RECOVERY_NONE,
   RECOVERY_ACTIVE,
   RECOVERY_BLOCKED
};

long           g_magicNumber = 0;
ENUM_RECOVERY_STATE g_recoveryState = RECOVERY_PENDING;
bool           g_recoveryMetadataTrusted = false;
bool           g_positionRecovered = false;
bool           g_pendingOpenOutcome = false;
datetime       g_pendingOpenUntil = 0;

datetime       g_lastCheckTime = 0;
datetime       g_lastTradeTime = 0;
bool           g_waitingForEvent = false;
datetime       g_eventTime = 0;
string         g_eventPair = "";
string         g_eventDirection = "";
double         g_eventLotPercent = 0;
int            g_eventExitMinutes = 0;
double         g_eventSLPercent = 0;
double         g_eventSLPips = 0;       // SL in pips from LLM
double         g_eventTPPips = 0;       // TP in pips from LLM
// Per-trade risk budget in USD — set from the server signal (max_loss_usd,
// panel "Max loss per trade USD"). Sizes the lot AND arms the offline
// max-loss guardrail. Signals without the field are REJECTED (no silent
// fallback), so the value is always fresh for the armed trade; the
// initializer below is never traded on.
double         g_maxLossUSD = 100.0;
bool           g_maxLossGuardEnabled = false;
ulong          g_currentTicket = 0;
// Realized-P/L tracking: POSITION_IDENTIFIER keys the deal history
// (HistorySelectByPosition); the last floating profit is the fallback when
// an externally-closed position's deals are not yet queryable
ulong          g_currentPositionId = 0;
double         g_lastKnownProfit = 0.0;
// Whole-trade accounting for the offline max-loss guard (invariant: guards
// use floating + realized). Realized legs appear after partial closes;
// refreshed from deal history whenever the broker volume drops.
double         g_realizedPnL = 0.0;
double         g_lastSeenLots = 0.0;

// Re-read the booked (realized) legs of the ACTIVE position from deal
// history. Incomplete history is fine here: the sum of the legs booked so
// far is exactly what the whole-trade guard needs while the position lives.
void RefreshRealizedPnL()
{
   ulong posId = (g_currentPositionId > 0) ? g_currentPositionId : g_currentTicket;
   if(posId == 0)
      return;
   double bookedRealized = 0.0, bookedPrice = 0.0;
   string bookedDetail;
   bool bookedComplete = false;
   if(GetRealizedPnL(posId, bookedRealized, bookedPrice, bookedDetail, bookedComplete))
      g_realizedPnL = bookedRealized;
}
int            g_closeRetryCount = 0;

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

// Decision lineage (F2): the server sends decision_id with every signal and
// the EA echoes it back in opened/closed/event-reaction reports, joining the
// trade and the measured reaction to the exact decision_history row even
// across a server restart. Empty from an old server — all consumers tolerate.
string         g_signalDecisionId = "";  // id of the latest parsed signal
string         g_tradeDecisionId = "";   // id bound to the OPEN position
// FORCE_DECISION test-mode marker, carried with the trade the same way as
// decision_id. The server owns this flag, but on a reconcile report the EA is
// the only source of truth: without it a demo coin-flip trade re-entered the
// learning data as a genuine outcome after a restart.
bool           g_signalForced = false;   // forced flag of the latest signal
bool           g_tradeForced = false;    // forced flag of the OPEN position

// AI Position Management
datetime       g_lastPositionReport = 0;    // Last time we reported to server
bool           g_aiManagementActive = false; // AI is managing the position

// Consecutive ticks whose spread exceeded InpEmergencySpreadPips. Debounces the
// emergency-spread exit so one wide tick at the release cannot liquidate the
// position at the worst quote of the session (see Guardrail 3).
int            g_spreadBreachTicks = 0;
#define SPREAD_BREACH_TICKS_TO_CLOSE 3

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
string         g_reactionDecisionId[REACTION_SLOTS];   // lineage echo (F2)

//+------------------------------------------------------------------+
//| Persistent recovery metadata (broker position remains canonical)  |
//+------------------------------------------------------------------+
string RecoveryStateFileName()
{
   string safeSymbol = _Symbol;
   StringReplace(safeSymbol, "\\", "_");
   StringReplace(safeSymbol, "/", "_");
   StringReplace(safeSymbol, ":", "_");
   return "SkyTowerAI_position_"
          + StringFormat("%I64d", AccountInfoInteger(ACCOUNT_LOGIN)) + "_"
          + StringFormat("%I64d", g_magicNumber) + "_"
          + safeSymbol + ".csv";
}

bool PersistRecoveryMetadata()
{
   if(g_currentTicket == 0 || g_currentPositionId == 0 || g_maxLossUSD <= 0)
      return false;

   string fileName = RecoveryStateFileName();
   int handle = FileOpen(fileName,
                         FILE_WRITE | FILE_CSV | FILE_ANSI | FILE_COMMON,
                         '\t');
   if(handle == INVALID_HANDLE)
   {
      Print("ERROR: Cannot persist recovery metadata. MQL error=", GetLastError());
      return false;
   }

   // Version 2 appends the forced marker. Column ADDED at the end so a v1
   // file written by an older build still parses (its missing field reads as
   // empty = not forced), which is also why v1 stays acceptable on load.
   FileWrite(handle,
             "2",
             StringFormat("%I64u", g_currentTicket),
             StringFormat("%I64u", g_currentPositionId),
             StringFormat("%I64d", g_magicNumber),
             _Symbol,
             g_eventDirection,
             DoubleToString(g_maxLossUSD, 2),
             DoubleToString(g_originalLots, 8),
             g_currentEventName,
             g_tradeDecisionId,
             g_tradeForced ? "1" : "0");
   FileFlush(handle);
   FileClose(handle);
   return true;
}

bool LoadRecoveryMetadata(ulong expectedPositionId, bool brokerSelected = true)
{
   string fileName = RecoveryStateFileName();
   int handle = FileOpen(fileName,
                         FILE_READ | FILE_CSV | FILE_ANSI | FILE_COMMON,
                         '\t');
   if(handle == INVALID_HANDLE)
      return false;

   string version = FileReadString(handle);
   string savedTicket = FileReadString(handle);
   string savedPositionId = FileReadString(handle);
   string savedMagic = FileReadString(handle);
   string savedSymbol = FileReadString(handle);
   string savedDirection = FileReadString(handle);
   string savedMaxLoss = FileReadString(handle);
   string savedOriginalLots = FileReadString(handle);
   string savedEventName = FileReadString(handle);
   string savedDecisionId = FileReadString(handle);
   string savedForced = FileIsEnding(handle) ? "" : FileReadString(handle);
   FileClose(handle);

   ulong positionId = (ulong)StringToInteger(savedPositionId);
   long magic = StringToInteger(savedMagic);
   double maxLoss = StringToDouble(savedMaxLoss);
   double originalLots = StringToDouble(savedOriginalLots);
   if((version != "1" && version != "2")
      || positionId != expectedPositionId
      || magic != g_magicNumber
      || savedSymbol != _Symbol
      || (savedDirection != "BUY" && savedDirection != "SELL")
      || (brokerSelected && savedDirection != g_eventDirection)
      || maxLoss <= 0
      || originalLots <= 0)
   {
      Print("ERROR: Recovery metadata is missing, stale, or invalid for position ",
            expectedPositionId, ". New entries will remain blocked.");
      return false;
   }

   g_maxLossUSD = maxLoss;
   g_maxLossGuardEnabled = true;
   g_originalLots = originalLots;
   if(!brokerSelected)
   {
      g_currentTicket = (ulong)StringToInteger(savedTicket);
      g_currentPositionId = positionId;
      g_eventDirection = savedDirection;
   }
   g_currentEventName = savedEventName;
   g_tradeDecisionId = savedDecisionId;
   g_tradeForced = (savedForced == "1");
   return true;
}

void ClearRecoveryMetadata()
{
   string fileName = RecoveryStateFileName();
   if(FileIsExist(fileName, FILE_COMMON))
      FileDelete(fileName, FILE_COMMON);
   g_recoveryMetadataTrusted = false;
}

bool RecoverClosedPositionReport()
{
   string fileName = RecoveryStateFileName();
   if(!FileIsExist(fileName, FILE_COMMON))
      return true;

   // Read the saved position id first, then validate and restore the whole
   // metadata row without a live broker direction to compare against.
   int handle = FileOpen(fileName,
                         FILE_READ | FILE_CSV | FILE_ANSI | FILE_COMMON,
                         '\t');
   if(handle == INVALID_HANDLE)
      return false;
   FileReadString(handle); // version
   FileReadString(handle); // ticket
   ulong savedPositionId = (ulong)StringToInteger(FileReadString(handle));
   FileClose(handle);
   if(savedPositionId == 0
      || !LoadRecoveryMetadata(savedPositionId, false))
   {
      // Deterministic parse/validation failure (e.g. a row truncated by a
      // power loss). Retrying every tick would stall this chart FOREVER in
      // RECOVERY_PENDING with no panel indication and no signal polling.
      // Quarantine the file and block visibly: operator attention required
      // (one close may be missing from the daily P/L accounting).
      string quarantined = fileName + ".invalid";
      if(FileMove(fileName, FILE_COMMON, quarantined,
                  FILE_COMMON | FILE_REWRITE))
      {
         g_recoveryState = RECOVERY_BLOCKED;
         Print("CRITICAL: recovery metadata file is invalid and was ",
               "quarantined as ", quarantined, ". A close report may be ",
               "missing. New entries stay blocked until reviewed.");
         if(g_panelCreated)
            g_panel.SetStatus("Recovery file invalid", clrRed);
      }
      return false;
   }

   double realized = 0.0, closePrice = 0.0;
   string closeDetail;
   bool complete = false;
   bool hasDeals = GetRealizedPnL(
      savedPositionId, realized, closePrice, closeDetail, complete
   );
   if(hasDeals && complete)
   {
      g_positionRecovered = true;
      bool reported = NotifyPositionClosed(
         closePrice, realized,
         "Recovered close after EA/server restart (" + closeDetail + ")",
         "history"
      );
      if(reported)
      {
         ClearRecoveryMetadata();
         g_currentTicket = 0;
         g_currentPositionId = 0;
         g_realizedPnL = 0.0;
         g_lastSeenLots = 0.0;
         g_recoveryState = RECOVERY_NONE;
         g_positionRecovered = false;
         return true;
      }
      return false;
   }

   // Never discard the durable identity without a matching close ACK.
   // MT5 deal history may lag immediately after a fill, so keep retrying
   // fail-closed until the complete realized P/L can be reported.
   Print("Recovery close is still waiting for complete MT5 deal history. "
         "Saved metadata remains intact.");
   return false;
}

void TryRecoverOpenPosition()
{
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
   {
      g_recoveryState = RECOVERY_PENDING;
      return;
   }

   ulong ownedTicket = 0;
   int ownedCount = 0;
   int foreignSymbolCount = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol)
         continue;
      if(PositionGetInteger(POSITION_MAGIC) != g_magicNumber)
      {
         foreignSymbolCount++;
         continue;
      }
      ownedTicket = ticket;
      ownedCount++;
   }

   if(ownedCount == 0)
   {
      // A market-order timeout/PLACED response is ambiguous. Keep polling
      // broker state for a bounded grace window before allowing another
      // signal; a late fill must be adopted instead of left unmanaged.
      if(g_pendingOpenOutcome)
      {
         if(TimeCurrent() <= g_pendingOpenUntil)
         {
            g_recoveryState = RECOVERY_PENDING;
            return;
         }
         g_pendingOpenOutcome = false;
         g_pendingOpenUntil = 0;
         ResetEventWait();
      }
      if(foreignSymbolCount > 0)
      {
         g_recoveryState = RECOVERY_BLOCKED;
         Print("CRITICAL: A foreign/manual position already exists on ", _Symbol,
               ". New entries are blocked to prevent netting/ownership conflicts.");
         if(g_panelCreated)
            g_panel.SetStatus("Foreign position", clrRed);
         return;
      }
      if(!RecoverClosedPositionReport())
      {
         // A quarantined-invalid metadata file sets BLOCKED (visible on the
         // panel) — never downgrade it back to silent PENDING retries.
         if(g_recoveryState != RECOVERY_BLOCKED)
            g_recoveryState = RECOVERY_PENDING;
         return;
      }
      g_recoveryState = RECOVERY_NONE;
      g_positionRecovered = false;
      return;
   }

   if(ownedCount > 1)
   {
      g_recoveryState = RECOVERY_BLOCKED;
      Print("CRITICAL: ", ownedCount, " positions match magic/symbol. "
            "This EA supports one position; new entries are blocked.");
      if(g_panelCreated)
         g_panel.SetStatus("Recovery conflict", clrRed);
      return;
   }

   // Re-select immediately before adoption in case the position closed
   // between enumeration and state reconstruction.
   if(!PositionSelectByTicket(ownedTicket))
   {
      g_recoveryState = RECOVERY_PENDING;
      return;
   }

   ResetSmartExitState();
   g_currentTicket = (ulong)PositionGetInteger(POSITION_TICKET);
   g_currentPositionId = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   ENUM_POSITION_TYPE posType =
      (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   g_eventDirection = (posType == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   g_eventPair = PositionGetString(POSITION_SYMBOL);
   g_originalLots = PositionGetDouble(POSITION_VOLUME);
   g_lastKnownProfit = PositionGetDouble(POSITION_PROFIT);
   g_lastTradeTime = (datetime)PositionGetInteger(POSITION_TIME);
   g_closeRetryCount = 0;
   // Every ADOPTION of a position starts the spread debounce from scratch.
   // The counter is manager-global, not per-position: without this reset an
   // adopted position inherits breaches counted on the PREVIOUS trade (or on
   // the last ticks before it closed) and can be liquidated on its very first
   // wide tick — exactly what the debounce exists to prevent.
   g_spreadBreachTicks = 0;
   g_lastPositionReport = 0;
   g_waitingForEvent = false;
   g_positionRecovered = true;

   if(g_pendingOpenOutcome)
   {
      // The fill belongs to the in-memory signal whose open result was
      // ambiguous. Its risk/lineage data is still available and can now be
      // persisted against the canonical POSITION_IDENTIFIER.
      g_tradeDecisionId = g_signalDecisionId;
      g_tradeForced = g_signalForced;
      g_positionRecovered = false;
      g_recoveryMetadataTrusted = PersistRecoveryMetadata();
      g_pendingOpenOutcome = false;
      g_pendingOpenUntil = 0;
   }
   else
   {
      g_recoveryMetadataTrusted = LoadRecoveryMetadata(g_currentPositionId);
   }
   if(!g_recoveryMetadataTrusted)
   {
      g_maxLossGuardEnabled = false;
      // Continue local max-hold/spread management, but do not accept another
      // signal until an operator resolves the missing risk metadata.
      g_recoveryState = RECOVERY_BLOCKED;
      g_currentEventName = "(recovered without trusted metadata)";
   }
   else
   {
      g_recoveryState = RECOVERY_ACTIVE;
   }

   double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double currentLots = PositionGetDouble(POSITION_VOLUME);
   double currentSL = PositionGetDouble(POSITION_SL);
   double currentTP = PositionGetDouble(POSITION_TP);
   // Rebuild whole-trade accounting for the adopted position: partial
   // closes booked before the restart must count toward the max-loss guard.
   g_realizedPnL = 0.0;
   g_lastSeenLots = currentLots;
   RefreshRealizedPnL();
   g_smartExit.OnRecoveredPosition(
      g_currentTicket, _Symbol, g_eventDirection, entryPrice,
      g_originalLots, currentLots, g_lastTradeTime, currentSL, currentTP
   );

   Print("Recovered broker position: ticket=", g_currentTicket,
         " position_id=", g_currentPositionId,
         " direction=", g_eventDirection,
         " remaining_lots=", DoubleToString(currentLots, 4),
         " risk_metadata=", g_recoveryMetadataTrusted ? "trusted" : "BLOCKED");

   if(g_recoveryMetadataTrusted)
      NotifyPositionOpened();
}

//+------------------------------------------------------------------+
//| Expert initialization function                                     |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Pip unit override must be armed before ANY SkyPipSize/spread call
   //--- (recovery, spread gates, reports all run through it).
   g_skyPipSizeOverride = InpPipSizeOverride;
   Print("Pip size override: ",
         (InpPipSizeOverride > 0.0)
            ? DoubleToString(InpPipSizeOverride, 5) + " price units"
            : "0 (auto forex rule)",
         " -> effective pip ", DoubleToString(SkyPipSize(_Symbol), 5));

   //--- Generate unique magic number per symbol if auto mode
   g_magicNumber = InpMagicNumber;
   if(g_magicNumber == 0)
   {
      // Generate from symbol name hash for uniqueness per pair
      // Base: 20240116, add hash of first 6 chars of symbol
      g_magicNumber = 20240116;
      string sym = _Symbol;
      for(int i = 0; i < MathMin(6, StringLen(sym)); i++)
      {
         g_magicNumber += StringGetCharacter(sym, i) * (i + 1) * 100;
      }
   }

   //--- Setup trade object
   trade.SetExpertMagicNumber(g_magicNumber);
   trade.SetDeviationInPoints(InpSlippage);
   trade.SetTypeFilling(ORDER_FILLING_IOC);
   Print("Magic Number: ", g_magicNumber, " (Symbol: ", _Symbol, ")");

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

   //--- Initialize Smart Exit Manager
   g_smartExit.Init(
      InpServerHost,
      InpServerPort,
      InpUseZoneTargets
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
   Print("Risk: ", InpRiskPercent, "% (capped by server max_loss_usd per signal)");
   Print("Risk budget + daily trade limit are server-controlled (dashboard)");
   Print("Min Confidence: ", InpMinConfidence);
   Print("Trade Mode: ", EnumToString(InpDefaultMode));
   Print("Zone Targets: ", InpUseZoneTargets ? "Enabled" : "Disabled");
   Print("Zone Indicator: ", InpUseZoneIndicator ? "Enabled" : "Disabled");
   Print("Zone Bias for Direction: ", InpUseZoneBiasForDirection ? "Enabled" : "Disabled");
   Print("Visual Panel: ", InpShowPanel ? "Enabled" : "Disabled");
   // Broker stop distances: stops level gates TP/SL placement; a non-zero
   // FREEZE level would block modifies/closes near the TP (retcode 10018) —
   // if this ever prints > 0, that failure mode is live on this broker.
   Print("Broker stops level: ",
         SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
         " pts | freeze level: ",
         SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL), " pts");
   // Symbol specification needed to configure a per-instrument profile
   // (pip override, spread gates, budget) — grep the log for "SkyTower SPEC:".
   double specAsk = SymbolInfoDouble(_Symbol, SYMBOL_ASK);
   double specMargin1Lot = -1.0;
   if(specAsk <= 0
      || !OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, 1.0, specAsk, specMargin1Lot)
      || !MathIsValidNumber(specMargin1Lot))
      specMargin1Lot = -1.0;
   PrintFormat("SkyTower SPEC: symbol=%s digits=%d point=%s pip=%s "
               "tick_size=%s tick_value=%.5f contract_size=%.2f "
               "vol_min=%s vol_step=%s vol_max=%s "
               "currency_profit=%s currency_margin=%s leverage=1:%d "
               "calc_mode=%d margin_1lot=%.2f spread_pips=%.2f",
               _Symbol,
               (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS),
               DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_POINT), 8),
               DoubleToString(SkyPipSize(_Symbol), 8),
               DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE), 8),
               SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE),
               SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE),
               DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN), 4),
               DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP), 4),
               DoubleToString(SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX), 2),
               SymbolInfoString(_Symbol, SYMBOL_CURRENCY_PROFIT),
               SymbolInfoString(_Symbol, SYMBOL_CURRENCY_MARGIN),
               (int)AccountInfoInteger(ACCOUNT_LEVERAGE),
               (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_CALC_MODE),
               specMargin1Lot,
               SkySpreadPips(_Symbol));
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

   // Broker state is authoritative. Reconcile it before the first signal
   // poll so an EA/server restart cannot open a second position.
   TryRecoverOpenPosition();

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
void GetZoneBasedTargets(string symbol, string direction, double entryPrice,
                         double &tp1, double &tp2, double &sl)
{
   double pip = SkyPipSize(symbol);
   if(pip <= 0)
   {
      tp1 = 0;
      tp2 = 0;
      sl = 0;
      return;
   }

   if(!ReadZoneIndicatorData())
   {
      //--- Fallback to default values
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
   if(g_recoveryState == RECOVERY_PENDING)
   {
      TryRecoverOpenPosition();
      if(g_recoveryState == RECOVERY_PENDING)
         return;
   }

   //--- Check for open positions that need to be closed
   ManageOpenPositions();

   //--- Track post-event price reaction (runs independently of trade state)
   if(InpReportReactions)
      HandleEventReaction();

   // Never poll or arm a new signal while a broker position is active or
   // recovery is ambiguous. Local guardrails above continue to run.
   if(g_currentTicket != 0 || g_recoveryState == RECOVERY_BLOCKED)
      return;

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

   //--- Daily trade limit is enforced by the SERVER (it stops serving
   //--- signals once the panel's "Max trades per day" is reached), so
   //--- there is no per-chart gate here — no duplicated setting.

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
   if(code < 200 || code >= 300)
   {
      Print("HTTP GET failed: status=", code, " url=", url);
      return false;
   }
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

   // Convert JSON body to char array. Byte length MUST come from the UTF-8
   // conversion itself: StringLen counts characters, and any non-ASCII
   // character (event names, LLM reason text) encodes as 2+ bytes — resizing
   // to StringLen would chop the tail and send malformed JSON.
   StringToCharArray(jsonBody, data, 0, -1, CP_UTF8);
   int bodyBytes = ArraySize(data);
   if(bodyBytes > 0)
      ArrayResize(data, bodyBytes - 1); // drop only the terminal 0

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
   if(code < 200 || code >= 300)
   {
      Print("HTTP POST failed: status=", code, " url=", url,
            " response=", result);
      return false;
   }
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
   double spreadPoints = (double)spread;
   double spreadPips = SkySpreadPips(pair);

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
      "\"current_price\":%.5f,\"spread_points\":%.0f,\"spread_pips\":%.2f,"
      "\"zones\":{\"direction_bias\":\"%s\",\"bias_strength\":%.2f}}",
      pair, eventCurrency, eventTimeStr, currentPrice, spreadPoints, spreadPips,
      directionBias, biasStrength
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
   double spreadPips = SkySpreadPips(pair);

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
      "\"current_price\":%.5f,\"spread_points\":%d,\"spread_pips\":%.2f}",
      pair, g_zoneBias, directionBias,
      g_nearestLiqHigh, g_nearestLiqLow,
      currentPrice, (int)spread, spreadPips
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/report-zone";
   string result = "";

   // Send zone report (don't fail if it doesn't work)
   if(WebRequestPost(url, json, result)
      && StringFind(result, "\"status\":\"ok\"") >= 0)
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
//| Push current OHLC (M1/M5/M15/H1) to server for LLM market context  |
//+------------------------------------------------------------------+
void PushMarketData()
{
   double currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);
   long spread = SymbolInfoInteger(_Symbol, SYMBOL_SPREAD);
   double spreadPips = SkySpreadPips(_Symbol);

   string json = StringFormat(
      "{\"pair\":\"%s\",\"current_price\":%.5f,\"spread_points\":%d,"
      "\"spread_pips\":%.2f,\"ohlc_multi\":{",
      _Symbol, currentPrice, (int)spread, spreadPips);

   string ohlcPart = "";
   AppendOhlcJson(ohlcPart, "M1", PERIOD_M1, 60);   // fine pre-news picture
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
      "\"decision_id\":\"%s\","
      "\"price_at_event\":%.5f,\"price_after_1min\":%.5f,\"price_after_5min\":%.5f}",
      _Symbol, EscapeJson(g_reactionEventName[slot]), EscapeJson(g_reactionCurrency[slot]),
      EscapeJson(g_reactionEventTimeUTC[slot]),
      EscapeJson(g_reactionDecisionId[slot]),
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
   string decisionId = ExtractJsonString(result, "decision_id");  // lineage (F2)

   //--- Per-trade risk budget from the server (panel "Max loss per trade
   //--- USD") — single source of truth for lot sizing + max-loss guardrail.
   //--- A signal WITHOUT a budget is rejected: silently falling back to a
   //--- default here could size the lot 5x off what the panel says (old
   //--- server build, renamed field). Incompatible server = no trade.
   double serverMaxLoss = ExtractJsonDouble(result, "max_loss_usd");
   if(!MathIsValidNumber(serverMaxLoss) || serverMaxLoss <= 0)
   {
      Print("SIGNAL REJECTED: no max_loss_usd risk budget in signal. ",
            "Server build is incompatible (pre-panel-owned-risk) - update the server. NOT trading.");
      return;
   }
   g_maxLossUSD = serverMaxLoss;
   g_maxLossGuardEnabled = true;
   Print("Risk budget from server: $", DoubleToString(g_maxLossUSD, 0), " per trade");

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

   // BUY/SELL with no positive risk allocation is not actionable. In
   // particular, never reinterpret an explicit zero as the maximum lot.
   if(StringFind(result, "\"lot_percent\":") < 0
      || !MathIsValidNumber(lotPercent)
      || lotPercent <= 0 || lotPercent > 100)
   {
      Print("SIGNAL REJECTED: lot_percent must be finite and in (0, 100].");
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
   g_eventLotPercent = lotPercent;
   g_eventExitMinutes = (exitMinutes > 0) ? exitMinutes : InpExitMinutesAfter;
   g_eventSLPercent = (slPercent > 0) ? slPercent : InpDefaultSLPercent;
   g_eventSLPips = slPips;  // SL in pips from LLM (0 if not provided)
   g_eventTPPips = tpPips;  // TP in pips from LLM (0 if not provided)
   g_signalDecisionId = decisionId;
   g_signalForced = forcedSignal;

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
            // Re-delivered signal (e.g. after a spread-rejected entry) may
            // carry a NEWER decision_id (server re-analysis) — refresh so the
            // reaction joins the decision that actually drives the trade
            if(StringLen(decisionId) > 0)
               g_reactionDecisionId[s] = decisionId;
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
         g_reactionDecisionId[slot] = decisionId;
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
bool IsExecutedTradeRetcode(uint retcode)
{
   return (retcode == TRADE_RETCODE_DONE
           || retcode == TRADE_RETCODE_DONE_PARTIAL);
}

bool ConfirmTradeRequest(bool submitted, string operation)
{
   uint retcode = trade.ResultRetcode();
   if(!submitted || !IsExecutedTradeRetcode(retcode))
   {
      Print(operation, " failed: retcode=", retcode,
            " (", trade.ResultRetcodeDescription(), ")");
      return false;
   }
   return true;
}

// SL/TP modifications only: NO_CHANGES means the broker already holds the
// requested levels — that is success, not failure. Treating it as failure
// let the entry postcondition market-close a correctly protected position.
bool ConfirmModifyRequest(bool submitted, string operation)
{
   if(submitted && trade.ResultRetcode() == TRADE_RETCODE_NO_CHANGES)
      return true;
   return ConfirmTradeRequest(submitted, operation);
}

bool SelectSingleOwnedPosition(string symbol, ulong &ticket, ulong &positionId,
                               string &direction, double &volume,
                               double &entryPrice, double &sl, double &tp)
{
   ticket = 0;
   positionId = 0;
   direction = "";
   volume = 0;
   entryPrice = 0;
   sl = 0;
   tp = 0;

   int count = 0;
   ulong foundTicket = 0;
   for(int i = PositionsTotal() - 1; i >= 0; i--)
   {
      ulong candidate = PositionGetTicket(i);
      if(candidate == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != symbol
         || PositionGetInteger(POSITION_MAGIC) != g_magicNumber)
         continue;
      foundTicket = candidate;
      count++;
   }

   if(count != 1 || !PositionSelectByTicket(foundTicket))
      return false;

   ticket = (ulong)PositionGetInteger(POSITION_TICKET);
   positionId = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   ENUM_POSITION_TYPE type =
      (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   direction = (type == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   volume = PositionGetDouble(POSITION_VOLUME);
   entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   sl = PositionGetDouble(POSITION_SL);
   tp = PositionGetDouble(POSITION_TP);
   return (ticket > 0 && positionId > 0 && volume > 0
           && entryPrice > 0 && MathIsValidNumber(volume)
           && MathIsValidNumber(entryPrice));
}

void ExecuteEventTrade()
{
   // Final broker-side ownership check closes the race between signal arming
   // and order submission (including a second EA instance on the symbol).
   TryRecoverOpenPosition();
   if(g_currentTicket != 0 || g_recoveryState == RECOVERY_BLOCKED
      || g_recoveryState == RECOVERY_PENDING)
   {
      Print("Trade blocked: broker position recovery is active or unresolved");
      ResetEventWait();
      return;
   }

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

   // Resolve the broker symbol. Missing quote data is fail-closed for entry;
   // it must never look like a zero spread.
   string symbol = ConvertPairToSymbol(g_eventPair);
   if(!SymbolSelect(symbol, true))
   {
      Print("Cannot select symbol: ", symbol);
      ResetEventWait();
      return;
   }
   if(!trade.SetTypeFillingBySymbol(symbol))
   {
      Print("Cannot configure broker filling mode for ", symbol);
      ResetEventWait();
      return;
   }

   MqlTick tick;
   double pip = SkyPipSize(symbol);
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(!SymbolInfoTick(symbol, tick) || tick.ask <= 0 || tick.bid <= 0
      || tick.ask < tick.bid || pip <= 0 || point <= 0)
   {
      Print("Trade blocked: invalid or missing broker quote for ", symbol);
      ResetEventWait();
      return;
   }

   double spread = SkySpreadPips(symbol);
   if(spread < 0 || !MathIsValidNumber(spread))
   {
      Print("Trade blocked: spread is unavailable");
      ResetEventWait();
      return;
   }

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

   // The reduction inputs are known now, but the final lot is deliberately
   // calculated only after the exact broker-grid SL has been finalized.
   double confLotPercent = InpUseConfidenceLot ? g_eventLotPercent : 100.0;
   double appliedSpreadMult = InpUseSpreadLotReduction ? spreadMultiplier : 1.0;
   double baseLotPercent = confLotPercent * appliedSpreadMult;
   if(!MathIsValidNumber(baseLotPercent)
      || baseLotPercent <= 0 || baseLotPercent > 100)
   {
      Print("Trade blocked: invalid final lot percentage");
      ResetEventWait();
      return;
   }

   double ask = tick.ask;
   double bid = tick.bid;

   //--- Calculate Stop Loss and TP from Zone Indicator or fallback
   double sl = 0;
   double tp1 = 0, tp2 = 0;
   double entryPrice = (g_eventDirection == "BUY") ? ask : bid;

   if(InpUseStopLoss)
   {
      //--- Priority 1: Use LLM-provided SL/TP in pips
      if(g_eventSLPips > 0)
      {
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
         GetZoneBasedTargets(symbol, g_eventDirection, entryPrice, tp1, tp2, sl);
         Print("Using ZONE INDICATOR targets");
      }
      else
      {
         //--- Fallback: SL based on pips (default 25 pips)
         double slPips = 25;  // Default 25 pips SL

         if(g_eventDirection == "BUY")
            sl = entryPrice - slPips * pip;
         else
            sl = entryPrice + slPips * pip;

         Print("Using DEFAULT pip-based SL: ", slPips, " pips");
      }

      // Invalid or stale zone levels must not produce a stop on the wrong
      // side of the market.
      if(!MathIsValidNumber(sl) || sl <= 0
         || (g_eventDirection == "BUY" && sl >= entryPrice)
         || (g_eventDirection == "SELL" && sl <= entryPrice))
      {
         sl = (g_eventDirection == "BUY")
            ? entryPrice - SkyPipsToPrice(symbol, 25)
            : entryPrice + SkyPipsToPrice(symbol, 25);
         Print("Invalid target stop replaced with 25-pip fallback");
      }

      //--- Safety: Ensure SL is within reasonable bounds (20-100 pips)
      double slDistance = MathAbs(sl - entryPrice);
      double minSL = SkyPipsToPrice(symbol, 20);
      double maxSL = SkyPipsToPrice(symbol, 100);
      double brokerMinSL =
         (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
      minSL = MathMax(minSL, brokerMinSL);
      if(minSL > maxSL)
      {
         Print("Trade blocked: broker minimum stop exceeds 100 pips");
         ResetEventWait();
         return;
      }

      if(slDistance < minSL || slDistance > maxSL)
      {
         slDistance = MathMax(minSL, MathMin(maxSL, slDistance));
         if(g_eventDirection == "BUY")
            sl = entryPrice - slDistance;
         else
            sl = entryPrice + slDistance;
         Print("SL adjusted to bounds: ", sl);
      }

      sl = SkyNormalizeStopPrice(symbol, sl, g_eventDirection);
      if(sl <= 0
         || (g_eventDirection == "BUY" && sl >= entryPrice)
         || (g_eventDirection == "SELL" && sl <= entryPrice))
      {
         Print("Trade blocked: final stop loss is invalid");
         ResetEventWait();
         return;
      }
   }

   //--- Finalize the take-profit for the ORDER itself. The server's exit
   //--- engine stays the strategic exit owner, but a broker-side TP is the
   //--- only mechanism that can bank a news spike between two 5-15s position
   //--- reports (2026-08-04 NZD: the favorable spike came and went inside
   //--- one report cycle). Unlike the stop, an unusable TP must never block
   //--- the trade: it degrades to 0 and the server manages the exit alone.
   double tp = 0;
   if(InpUseStopLoss && MathIsValidNumber(tp1) && tp1 > 0)
   {
      // Round toward the entry (BUY floor, SELL ceil) so grid rounding can
      // only make the target easier to reach, never harder.
      tp = SkyNormalizeStopPrice(symbol, tp1, g_eventDirection);
      double brokerMinStop =
         (double)SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL) * point;
      bool tpUsable = (tp > 0)
         && ((g_eventDirection == "BUY" && tp >= ask + brokerMinStop)
             || (g_eventDirection == "SELL" && tp <= bid - brokerMinStop));
      if(!tpUsable)
      {
         Print("Take-profit ", tp1,
               " unusable at current quotes - opening without a broker TP");
         tp = 0;
      }
   }

   // Size only after the exact stop is final. If broker SLs are disabled,
   // retain a synthetic 25-pip distance for the risk calculation.
   double sizingSL = sl;
   if(!InpUseStopLoss)
      sizingSL = (g_eventDirection == "BUY")
         ? entryPrice - SkyPipsToPrice(symbol, 25)
         : entryPrice + SkyPipsToPrice(symbol, 25);
   // Include the configured worst-case adverse slippage in the loss model.
   double sizingEntry = (g_eventDirection == "BUY")
      ? entryPrice + InpSlippage * point
      : entryPrice - InpSlippage * point;
   double slPipsForSizing =
      SkyPriceToPips(symbol, MathAbs(sizingEntry - sizingSL));
   if(!MathIsValidNumber(slPipsForSizing) || slPipsForSizing <= 0)
   {
      Print("Trade blocked: invalid final SL distance");
      ResetEventWait();
      return;
   }

   // Recheck the quote immediately before sizing and submission.
   spread = SkySpreadPips(symbol);
   if(spread < 0 || !MathIsValidNumber(spread))
   {
      Print("Trade blocked: spread is unavailable before submission");
      ResetEventWait();
      return;
   }
   spreadMultiplier = GetSpreadLotMultiplier(spread);
   if(spreadMultiplier <= 0 || spread > InpMaxSpreadPips)
   {
      Print("Trade blocked by final spread check: ",
            DoubleToString(spread, 1), " pips");
      ResetEventWait();
      return;
   }
   appliedSpreadMult = InpUseSpreadLotReduction ? spreadMultiplier : 1.0;
   baseLotPercent = confLotPercent * appliedSpreadMult;
   if(!MathIsValidNumber(baseLotPercent)
      || baseLotPercent <= 0 || baseLotPercent > 100)
   {
      Print("Trade blocked: invalid final lot percentage");
      ResetEventWait();
      return;
   }

   double lots = CalculateLotSize(
      symbol, g_eventDirection, baseLotPercent, sizingEntry, sizingSL
   );
   int volumeDigits =
      SkyVolumeDigits(SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP));
   Print("Lot sizing: risk-based -> ", DoubleToString(lots, volumeDigits),
         " lots (final SL ", DoubleToString(slPipsForSizing, 1),
         " pips, lot% ", DoubleToString(baseLotPercent, 1),
         " = conf ", DoubleToString(confLotPercent, 1),
         "% x spread ", DoubleToString(appliedSpreadMult * 100, 0), "%)");

   if(!InpUseConfidenceLot && g_eventLotPercent < 100.0)
      Print("Confidence lot reduction DISABLED (server suggested ",
            DoubleToString(g_eventLotPercent, 0), "%)");
   if(appliedSpreadMult < 1.0)
      Print("Spread warning: ", DoubleToString(spread, 1),
            " pips. Lot reduced to ",
            DoubleToString(appliedSpreadMult * 100, 0), "%");
   else if(!InpUseSpreadLotReduction && spreadMultiplier < 1.0)
      Print("Spread lot reduction DISABLED (would have been ",
            DoubleToString(spreadMultiplier * 100, 0), "%)");
   if(lots <= 0)
   {
      Print("Trade blocked: risk-based volume is invalid or below broker minimum");
      ResetEventWait();
      return;
   }

   // Recheck broker ownership at the last possible point before submission.
   // This narrows the cross-instance race left after the earlier sizing and
   // target calculations. A durable lease is added with command idempotency.
   TryRecoverOpenPosition();
   if(g_currentTicket != 0 || g_recoveryState == RECOVERY_BLOCKED
      || g_recoveryState == RECOVERY_PENDING)
   {
      Print("Trade blocked by final broker ownership check");
      ResetEventWait();
      return;
   }

   //--- Execute trade
   g_pendingOpenOutcome = true;
   g_pendingOpenUntil = TimeCurrent() + 30;
   bool success = false;
   if(g_eventDirection == "BUY")
   {
      success = trade.Buy(lots, symbol, ask, sl, tp, "SkyTower-AI");
   }
   else
   {
      success = trade.Sell(lots, symbol, bid, sl, tp, "SkyTower-AI");
   }

   // A TP the broker rejects must not cost the entry: retcode 10016
   // (invalid stops) guarantees nothing executed, so one retry without the
   // TP is safe and cannot double-open. Any other failure keeps the old
   // no-retry behavior.
   if(!success && tp > 0
      && trade.ResultRetcode() == TRADE_RETCODE_INVALID_STOPS)
   {
      Print("Order rejected for invalid stops with TP ", tp,
            " - retrying once WITHOUT broker TP");
      tp = 0;
      if(g_eventDirection == "BUY")
         success = trade.Buy(lots, symbol, ask, sl, 0, "SkyTower-AI");
      else
         success = trade.Sell(lots, symbol, bid, sl, 0, "SkyTower-AI");
   }

   // 10019 (no money): the free-margin cap in CalculateLotSize should keep
   // this from happening; if it still does, no retry — sizing must change.
   if(!success && trade.ResultRetcode() == TRADE_RETCODE_NO_MONEY)
      Print("Order rejected: not enough margin (retcode 10019) for ",
            DoubleToString(lots, volumeDigits), " lots - lower ",
            "InpMaxMarginUsePercent/budget or raise leverage");

   bool requestConfirmed = ConfirmTradeRequest(success, "Trade execution");

   // Bind only the canonical live position. ResultOrder() is an order ticket,
   // not a reliable POSITION_TICKET.
   ulong liveTicket = 0, livePositionId = 0;
   string liveDirection = "";
   double liveLots = 0, liveEntry = 0, liveSL = 0, liveTP = 0;
   bool bound = false;
   for(int attempt = 0; attempt < 20 && !bound; attempt++)
   {
      bound = SelectSingleOwnedPosition(
         symbol, liveTicket, livePositionId, liveDirection,
         liveLots, liveEntry, liveSL, liveTP
      );
      if(!bound)
         Sleep(50);
   }
   if(!bound || liveDirection != g_eventDirection)
   {
      Print("CRITICAL: open outcome is not safely bound (confirmed=",
            requestConfirmed ? "true" : "false",
            "). Broker recovery will retry for 30 seconds.");
      g_recoveryState = RECOVERY_PENDING;
      g_waitingForEvent = false;
      return;
   }

   g_currentTicket = liveTicket;
   g_currentPositionId = livePositionId;
   g_lastTradeTime = TimeCurrent();
   g_originalLots = liveLots;
   g_lastKnownProfit = 0.0;
   g_realizedPnL = 0.0;
   g_lastSeenLots = liveLots;
   g_closeRetryCount = 0;
   g_spreadBreachTicks = 0;
   g_tradeDecisionId = g_signalDecisionId;
   g_tradeForced = g_signalForced;
   g_positionRecovered = false;
   g_recoveryState = RECOVERY_ACTIVE;
   g_recoveryMetadataTrusted = PersistRecoveryMetadata();
   g_pendingOpenOutcome = false;
   g_pendingOpenUntil = 0;

   // Confirm the broker applied the protective stop. One corrective modify is
   // allowed; an unprotected position is closed immediately if that fails.
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   bool liveStopProtectsRisk =
      (liveSL > 0
       && ((liveDirection == "BUY"
            && liveSL >= sl - tickSize * 1.1)
           || (liveDirection == "SELL"
               && liveSL <= sl + tickSize * 1.1)));
   if(InpUseStopLoss && !liveStopProtectsRisk)
   {
      Print("WARNING: broker SL postcondition failed; correcting to ", sl);
      if(!ModifyPositionSL(g_currentTicket, sl))
      {
         Print("CRITICAL: required SL could not be applied; closing position");
         ClosePosition("Safety: broker did not apply required stop loss");
         g_waitingForEvent = false;
         return;
      }
      liveSL = sl;
   }

   Print("==============================================");
   Print("TRADE EXECUTED");
   Print("Symbol: ", symbol);
   Print("Direction: ", liveDirection);
   Print("Lots: ", DoubleToString(liveLots, volumeDigits));
   Print("Price: ", liveEntry);
   Print("Spread: ", DoubleToString(spread, 1), " pips (",
         GetSpreadStatus(spread), ")");
   Print("Lot Multiplier: ",
         DoubleToString(appliedSpreadMult * 100, 0), "%");
   // TP postcondition is best-effort, unlike the SL's: a missing TP is a
   // lost opportunity, not unbounded risk, so one corrective modify and a
   // warning — never a close.
   if(tp > 0 && liveTP <= 0)
   {
      Print("WARNING: broker did not apply TP ", tp, " - one corrective try");
      if(ModifyPositionTP(g_currentTicket, tp))
         liveTP = tp;
      else
         Print("WARNING: TP could not be applied "
               "(server exit engine still manages the position)");
   }
   else if(tp > 0 && MathAbs(liveTP - tp) > tickSize * 1.1)
      Print("WARNING: broker adjusted TP: requested ", tp, " applied ", liveTP);
   if(liveSL > 0) Print("Stop Loss: ", liveSL);
   if(liveTP > 0) Print("Take Profit: ", liveTP);
   Print("Ticket: ", g_currentTicket,
         " | Position ID: ", g_currentPositionId);
   Print("==============================================");

   g_smartExit.OnNewPosition(
      g_currentTicket, symbol, liveDirection, liveEntry, liveLots,
      liveSL, tp1, tp2
   );
   NotifyPositionOpened();

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
      // Position was closed externally (SL hit, manual close, etc.).
      // Report the REALIZED P/L from the deal history — the old hardcoded
      // 0.0 silently corrupted the daily P/L statistic and the daily
      // loss-limit check on every SL hit.
      double realized, histClosePrice;
      string closeDetail;
      bool complete = false;
      ulong posId = (g_currentPositionId > 0) ? g_currentPositionId : g_currentTicket;
      bool hasDeals = GetRealizedPnL(posId, realized, histClosePrice, closeDetail, complete);
      bool closeReported = false;

      if(hasDeals && complete)
      {
         Print("Position ", g_currentTicket, " closed externally (", closeDetail,
               ") - realized P/L: $", DoubleToString(realized, 2));
         closeReported = NotifyPositionClosed(
            histClosePrice, realized,
            "Position closed externally (" + closeDetail + ")",
            "history"
         );
      }
      else
      {
         // Never make an irreversible accounting decision from an estimate.
         // Keep the broker identity and metadata until MT5 exposes the full
         // IN/OUT deal set; the next tick/restart resumes this recovery.
         g_closeRetryCount++;
         g_recoveryState = RECOVERY_PENDING;
         g_aiManagementActive = false;
         Print("Close accounting pending for position ", posId,
               ": MT5 deal history is not complete (attempt ",
               g_closeRetryCount, ").");
         return;
      }

      if(closeReported)
      {
         ClearRecoveryMetadata();
         g_recoveryState = RECOVERY_NONE;
      }
      else
      {
         g_recoveryState = RECOVERY_PENDING;
         // Preserve identity and durable metadata for an exact retry.
         return;
      }
      g_positionRecovered = false;
      g_currentTicket = 0;
      g_currentPositionId = 0;
      g_lastKnownProfit = 0.0;
      g_realizedPnL = 0.0;
      g_lastSeenLots = 0.0;
      g_closeRetryCount = 0;
      g_spreadBreachTicks = 0;
      g_smartExit.OnPositionClosed();
      g_aiManagementActive = false;
      ResetSmartExitState();
      return;
   }

   string symbol = PositionGetString(POSITION_SYMBOL);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double profit = PositionGetDouble(POSITION_PROFIT);
   g_lastKnownProfit = profit;  // fallback if the close is only seen after the fact
   double currentLots = PositionGetDouble(POSITION_VOLUME);
   long spreadPoints = SymbolInfoInteger(symbol, SYMBOL_SPREAD);
   double spreadPips = SkySpreadPips(symbol);

   // Volume dropped (server PARTIAL_CLOSE or a manual partial in MT5):
   // refresh the realized component from deal history so the max-loss guard
   // keeps seeing the WHOLE trade, not just the remaining leg's floating.
   if(g_lastSeenLots > 0 && currentLots < g_lastSeenLots - 1e-8)
      RefreshRealizedPnL();
   g_lastSeenLots = currentLots;

   //--- EA-side safety guardrails (immediate, no server needed) ---

   // Guardrail 1: Max loss in USD (risk budget delivered with the signal).
   // Whole trade = remaining leg's floating + realized partial-close legs.
   double wholeTradePnL = profit + g_realizedPnL;
   if(g_maxLossGuardEnabled && wholeTradePnL < -g_maxLossUSD)
   {
      Print("=== EA GUARDRAIL: MAX LOSS $", g_maxLossUSD, " ===");
      Print("Whole-trade P/L: $", DoubleToString(wholeTradePnL, 2),
            " (floating $", DoubleToString(profit, 2),
            " + realized $", DoubleToString(g_realizedPnL, 2), ")");
      ClosePosition("EA guardrail: max loss $" + DoubleToString(g_maxLossUSD, 0) + " exceeded");
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

   // Guardrail 3: Emergency spread. Entry is already blocked just under this
   // same threshold, and news releases routinely print a few wide ticks at T0
   // — closing on ONE sample liquidated the trade at the spiked bid seconds
   // after entry. Require consecutive breaching ticks; a spread at 2x the
   // threshold still exits at once, where waiting is the bigger risk.
   if(spreadPips >= InpEmergencySpreadPips)
   {
      g_spreadBreachTicks++;
      bool catastrophicSpread = (spreadPips >= InpEmergencySpreadPips * 2.0);
      if(catastrophicSpread || g_spreadBreachTicks >= SPREAD_BREACH_TICKS_TO_CLOSE)
      {
         Print("=== EA GUARDRAIL: EMERGENCY SPREAD ", DoubleToString(spreadPips, 1),
               " pips (", (catastrophicSpread ? "catastrophic" :
               IntegerToString(g_spreadBreachTicks) + " consecutive ticks"), ") ===");
         ClosePosition("EA guardrail: emergency spread " + DoubleToString(spreadPips, 1) + " pips");
         return;
      }
   }
   else
   {
      g_spreadBreachTicks = 0;
   }

   // A RECOVERED position without trusted persisted risk metadata is managed
   // only by local max-hold/spread safety. Sending the default max-loss value
   // would let the server adopt a risk budget that never belonged to it.
   // A FRESH open is different: its risk budget is authoritative in memory
   // (it arrived with the signal), so a failed metadata WRITE must not mute
   // the server-owned exit management for the whole trade.
   if(g_positionRecovered && !g_recoveryMetadataTrusted)
      return;

   //--- AI Position Management: report status and get command ---
   int reportInterval = GetReportInterval();
   if(TimeCurrent() - g_lastPositionReport >= reportInterval)
   {
      g_lastPositionReport = TimeCurrent();
      // Cheap persist retry (at most once per report interval) so a
      // transient FILE_COMMON failure at open self-heals.
      if(!g_recoveryMetadataTrusted)
         g_recoveryMetadataTrusted = PersistRecoveryMetadata();
      // Deal history can lag a partial-close fill; while a partial exists,
      // periodically re-sync the realized component before reporting.
      if(currentLots < g_originalLots - 1e-8)
         RefreshRealizedPnL();
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

   datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
   // POSITION_TIME is BROKER-server time; the server parses open_time as a
   // UTC epoch. Sending it raw made minutes_open negative by the broker
   // offset (~2-3h) for the trade's whole life, so server-side time-phased
   // exits and max-hold never fired.
   long openTimeUtc = (long)openTime - GetBrokerTimezoneOffset();
   string ticketText = StringFormat("%I64u", g_currentTicket);
   string positionIdText = StringFormat("%I64u", g_currentPositionId);
   string magicText = StringFormat("%I64d", g_magicNumber);

   // Read zone data
   ReadZoneIndicatorData();

   // Build JSON report
   string json = StringFormat(
      "{\"ticket\":\"%s\",\"position_id\":\"%s\",\"magic\":\"%s\","
      "\"symbol\":\"%s\",\"direction\":\"%s\","
      "\"entry_price\":%.5f,\"current_price\":%.5f,"
      "\"lots\":%.2f,\"remaining_lots\":%.2f,"
      "\"sl\":%.5f,\"tp\":%.5f,"
      "\"profit_usd\":%.2f,\"realized_usd\":%.2f,\"tick_value\":%.4f,"
      "\"spread_pips\":%.1f,\"account_balance\":%.2f,"
      "\"zone_bias\":%.3f,\"nearest_resistance\":%.5f,\"nearest_support\":%.5f,"
      "\"max_loss_usd\":%.2f,\"open_time\":%I64d,"
      "\"event_name\":\"%s\",\"decision_id\":\"%s\","
      "\"forced\":%s,\"reconcile\":true}",
      ticketText, positionIdText, magicText, symbol, g_eventDirection,
      entryPrice, currentPrice,
      g_originalLots, lots,
      sl, tp,
      profit, g_realizedPnL, tickValue,
      spreadPips, balance,
      g_zoneBias, g_nearestLiqHigh, g_nearestLiqLow,
      g_maxLossUSD, openTimeUtc,
      EscapeJson(g_currentEventName), EscapeJson(g_tradeDecisionId),
      // A reconcile report REBUILDS server state after a restart, so the
      // forced marker must travel with it or the trade is re-registered as
      // genuine and pollutes track record, calibration and reflections.
      (g_tradeForced ? "true" : "false")
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
            if(MathAbs(newSL - entryPrice) < SkyPipsToPrice(symbol, 2))
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
         if(ModifyPositionTP(g_currentTicket, newTP))
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
         double closedLots = 0;
         if(ClosePositionPartialSafe(
               g_currentTicket, closePercent, closedLots))
         {
            Print("AI: Partial close ", DoubleToString(closePercent, 0),
                  "% (", closedLots, " lots)");
            g_tp1Hit = true;
            // Fold the just-realized leg into the whole-trade guard right
            // away (the periodic re-sync covers any history lag).
            RefreshRealizedPnL();
            if(PositionSelectByTicket(g_currentTicket))
               g_lastSeenLots = PositionGetDouble(POSITION_VOLUME);
         }
         else
         {
            Print("AI: Partial close failed or would violate broker volume limits");
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

   // Position is selected — refresh the identifier (the read right after
   // the open can miss it on an async fill)
   g_currentPositionId = (ulong)PositionGetInteger(POSITION_IDENTIFIER);
   // Refresh the metadata after the live position identifier becomes
   // available; ResultOrder() is not a stable substitute on every account.
   g_recoveryMetadataTrusted = PersistRecoveryMetadata();

   string symbol = PositionGetString(POSITION_SYMBOL);
   double entryPrice = PositionGetDouble(POSITION_PRICE_OPEN);
   double lots = PositionGetDouble(POSITION_VOLUME);
   double sl = PositionGetDouble(POSITION_SL);
   double tp = PositionGetDouble(POSITION_TP);
   double tickValue = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_VALUE);
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);
   datetime openTime = (datetime)PositionGetInteger(POSITION_TIME);
   // Broker-time -> UTC before the server parses it as a UTC epoch (see
   // ReportAndGetCommand note).
   long openTimeUtc = (long)openTime - GetBrokerTimezoneOffset();
   string ticketText = StringFormat("%I64u", g_currentTicket);
   string positionIdText = StringFormat("%I64u", g_currentPositionId);
   string magicText = StringFormat("%I64d", g_magicNumber);

   string json = StringFormat(
      "{\"ticket\":\"%s\",\"position_id\":\"%s\",\"magic\":\"%s\","
      "\"symbol\":\"%s\",\"direction\":\"%s\","
      "\"entry_price\":%.5f,\"lots\":%.2f,\"sl\":%.5f,\"tp\":%.5f,"
      "\"tick_value\":%.4f,\"account_balance\":%.2f,"
      "\"max_loss_usd\":%.2f,\"open_time\":%I64d,"
      "\"event_name\":\"%s\",\"decision_id\":\"%s\","
      "\"forced\":%s,\"recovered\":%s,\"reconcile\":true}",
      ticketText, positionIdText, magicText, symbol, g_eventDirection,
      entryPrice, lots, sl, tp,
      tickValue, balance,
      g_maxLossUSD, openTimeUtc,
      EscapeJson(g_currentEventName), EscapeJson(g_tradeDecisionId),
      // THIS is the endpoint used to re-adopt a position after a restart, so
      // the forced marker matters here even more than in the periodic report:
      // after a server restart the served-signal lineage is gone and this
      // flag is the only thing that stops a demo coin flip from being
      // recorded as a genuine trade.
      (g_tradeForced ? "true" : "false"),
      g_positionRecovered ? "true" : "false"
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/position/opened";
   string result = "";

   if(WebRequestPost(url, json, result)
      && StringFind(result, "\"status\":\"ok\"") >= 0)
   {
      Print("Server notified: position opened (AI management active)");
      g_aiManagementActive = true;
      g_lastPositionReport = TimeCurrent();
   }
   else
   {
      Print("WARNING: Could not notify server of position open - falling back");
      if(g_positionRecovered)
         g_recoveryState = RECOVERY_BLOCKED;
      // Still notify via old endpoint as fallback
      NotifyTradeExecuted();
   }
}

//+------------------------------------------------------------------+
//| Realized P/L of a closed position from the deal history            |
//+------------------------------------------------------------------+
//| Sums DEAL_PROFIT + DEAL_SWAP + DEAL_COMMISSION over ALL deals of   |
//| the position (includes earlier partial closes, final swap and      |
//| commission — unlike the floating POSITION_PROFIT read pre-close).  |
//| Returns false when the history is not queryable yet (no OUT deal). |
//| 'complete' turns true only once the OUT volume balances the IN     |
//| volume: right after a close the final deal can lag the history by  |
//| a moment, and after an earlier PARTIAL_CLOSE an OUT deal already   |
//| exists — summing at that instant silently drops the final leg      |
//| (2026-07-22 GBPUSD: $88.20 reported instead of $178.80).           |
//+------------------------------------------------------------------+
bool GetRealizedPnL(ulong positionId, double &realized, double &closePrice, string &closeDetail, bool &complete)
{
   realized = 0.0;
   closePrice = 0.0;
   closeDetail = "closed externally";
   complete = false;

   if(positionId == 0 || !HistorySelectByPosition((long)positionId))
      return false;

   bool hasOutDeal = false;
   double inVolume = 0.0, outVolume = 0.0;
   int total = HistoryDealsTotal();
   for(int i = 0; i < total; i++)
   {
      ulong dealTicket = HistoryDealGetTicket(i);
      if(dealTicket == 0)
         continue;

      realized += HistoryDealGetDouble(dealTicket, DEAL_PROFIT)
                + HistoryDealGetDouble(dealTicket, DEAL_SWAP)
                + HistoryDealGetDouble(dealTicket, DEAL_COMMISSION);

      ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(dealTicket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN)
         inVolume += HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
      if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY)
      {
         outVolume += HistoryDealGetDouble(dealTicket, DEAL_VOLUME);
         hasOutDeal = true;
         // Deals iterate oldest->newest, so this ends on the FINAL close
         closePrice = HistoryDealGetDouble(dealTicket, DEAL_PRICE);
         ENUM_DEAL_REASON dreason = (ENUM_DEAL_REASON)HistoryDealGetInteger(dealTicket, DEAL_REASON);
         if(dreason == DEAL_REASON_SL)      closeDetail = "SL hit";
         else if(dreason == DEAL_REASON_TP) closeDetail = "TP hit";
         else if(dreason == DEAL_REASON_SO) closeDetail = "stop-out";
         else                               closeDetail = "closed externally";
      }
   }

   complete = hasOutDeal && (outVolume >= inVolume - 0.0001);
   return hasOutDeal;
}

//+------------------------------------------------------------------+
//| Notify server that position was closed                             |
//+------------------------------------------------------------------+
bool NotifyPositionClosed(double closePrice, double profit, string reason,
                          string profitSource = "floating")
{
   string ticketText = StringFormat("%I64u", g_currentTicket);
   string positionIdText = StringFormat("%I64u", g_currentPositionId);
   string json = StringFormat(
      "{\"ticket\":\"%s\",\"position_id\":\"%s\","
      "\"close_price\":%.5f,\"profit\":%.2f,\"reason\":\"%s\","
      "\"profit_source\":\"%s\",\"decision_id\":\"%s\"}",
      ticketText, positionIdText, closePrice, profit, EscapeJson(reason),
      profitSource, EscapeJson(g_tradeDecisionId)
   );

   string url = "http://" + InpServerHost + ":" + IntegerToString(InpServerPort) + "/api/position/closed";
   string result = "";

   bool reported = (WebRequestPost(url, json, result)
                    && StringFind(result, "\"status\":\"ok\"") >= 0);
   if(reported)
   {
      Print("Server notified: position closed (P/L: $", DoubleToString(profit, 2), ")");
   }
   else
   {
      Print("WARNING: Could not notify server of position close");
   }

   g_aiManagementActive = false;
   return reported;
}

//+------------------------------------------------------------------+
//| Modify position stop loss                                          |
//+------------------------------------------------------------------+
bool ModifyPositionSL(ulong ticket, double new_sl)
{
   if(!PositionSelectByTicket(ticket))
      return false;

   double current_sl = PositionGetDouble(POSITION_SL);
   double current_tp = PositionGetDouble(POSITION_TP);
   string symbol = PositionGetString(POSITION_SYMBOL);
   string direction =
      ((ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE)
       == POSITION_TYPE_BUY) ? "BUY" : "SELL";
   new_sl = SkyNormalizeStopPrice(symbol, new_sl, direction);
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   if(new_sl <= 0 || tickSize <= 0)
      return false;
   // Once a protective SL exists, AI commands may only tighten it. This
   // preserves the maximum risk used for entry sizing.
   if(current_sl > 0
      && ((direction == "BUY" && new_sl < current_sl - tickSize * 1.1)
          || (direction == "SELL"
              && new_sl > current_sl + tickSize * 1.1)))
   {
      Print("Position SL modify rejected: stop loosening is not allowed");
      return false;
   }

   // Already at the requested stop (e.g. the broker attached it with the
   // order and the entry postcondition raced its visibility): success. A
   // blind re-modify would come back NO_CHANGES on many servers.
   if(current_sl > 0 && MathAbs(current_sl - new_sl) <= tickSize * 1.1)
      return true;

   if(!ConfirmModifyRequest(
         trade.PositionModify(ticket, new_sl, current_tp),
         "Position SL modify"))
      return false;

   for(int attempt = 0; attempt < 30; attempt++)
   {
      if(PositionSelectByTicket(ticket)
         && MathAbs(PositionGetDouble(POSITION_SL) - new_sl)
            <= tickSize * 1.1)
         return true;
      Sleep(100);
   }
   Print("Position SL modify failed postcondition");
   return false;
}

//+------------------------------------------------------------------+
//| Modify position take profit                                        |
//+------------------------------------------------------------------+
bool ModifyPositionTP(ulong ticket, double new_tp)
{
   if(!PositionSelectByTicket(ticket))
      return false;

   double current_sl = PositionGetDouble(POSITION_SL);
   string symbol = PositionGetString(POSITION_SYMBOL);
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(!MathIsValidNumber(new_tp) || new_tp <= 0 || tickSize <= 0)
      return false;
   new_tp = NormalizeDouble(MathRound(new_tp / tickSize) * tickSize, digits);

   if(MathAbs(PositionGetDouble(POSITION_TP) - new_tp) <= tickSize * 1.1)
      return true;

   if(!ConfirmModifyRequest(
         trade.PositionModify(ticket, current_sl, new_tp),
         "Position TP modify"))
      return false;

   for(int attempt = 0; attempt < 30; attempt++)
   {
      if(PositionSelectByTicket(ticket)
         && MathAbs(PositionGetDouble(POSITION_TP) - new_tp)
            <= tickSize * 1.1)
         return true;
      Sleep(100);
   }
   Print("Position TP modify failed postcondition");
   return false;
}

//+------------------------------------------------------------------+
//| Safely close part of a hedging-account position                    |
//+------------------------------------------------------------------+
bool ClosePositionPartialSafe(ulong ticket, double closePercent,
                              double &closedLots)
{
   closedLots = 0;
   if(!MathIsValidNumber(closePercent)
      || closePercent <= 0 || closePercent >= 100)
      return false;
   if(AccountInfoInteger(ACCOUNT_MARGIN_MODE)
      != ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
   {
      Print("Partial close rejected: CTrade partial close requires a hedging account");
      return false;
   }
   if(!PositionSelectByTicket(ticket))
      return false;

   string symbol = PositionGetString(POSITION_SYMBOL);
   double currentLots = PositionGetDouble(POSITION_VOLUME);
   double minVolume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(currentLots <= 0 || minVolume <= 0 || step <= 0)
      return false;

   double closeLots =
      SkyNormalizeVolumeDown(symbol, currentLots * closePercent / 100.0);
   double epsilon = step * 0.1;
   if(closeLots <= 0 || closeLots >= currentLots - epsilon)
      return false;

   double remaining = currentLots - closeLots;
   if(remaining < minVolume - epsilon)
   {
      closeLots =
         SkyNormalizeVolumeDown(symbol, currentLots - minVolume);
      remaining = currentLots - closeLots;
   }
   if(closeLots <= 0 || closeLots >= currentLots - epsilon
      || remaining < minVolume - epsilon)
      return false;

   if(!ConfirmTradeRequest(
         trade.PositionClosePartial(ticket, closeLots),
         "Position partial close"))
      return false;

   for(int attempt = 0; attempt < 10; attempt++)
   {
      if(!PositionSelectByTicket(ticket))
      {
         Print("Partial close postcondition failed: entire position disappeared");
         return false;
      }
      double actualLots = PositionGetDouble(POSITION_VOLUME);
      double actualClosed = currentLots - actualLots;
      if(actualClosed >= closeLots - epsilon
         && actualLots >= minVolume - epsilon)
      {
         closedLots = actualClosed;
         return true;
      }
      if(actualClosed > epsilon)
      {
         Print("Partial close incomplete: requested ", closeLots,
               ", broker closed only ", actualClosed);
         return false;
      }
      Sleep(50);
   }
   Print("Position partial close failed postcondition");
   return false;
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
      Print("WARNING: Position disappeared before close request; "
            "ManageOpenPositions will reconcile deal history on the next tick.");
      return;
   }

   double profit = PositionGetDouble(POSITION_PROFIT);
   double closePrice = (g_eventDirection == "BUY") ?
      SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_BID) :
      SymbolInfoDouble(PositionGetString(POSITION_SYMBOL), SYMBOL_ASK);

   Print("Closing position - Reason: ", reason);
   Print("P/L: $", DoubleToString(profit, 2));

   // Emergency exits must not lose to a requote. InpSlippage is sized for
   // ENTRY quality, but a guardrail close fires when the position is ALREADY
   // past its risk budget — and a rejected attempt only retried on the next
   // tick with the identical 5-pip window, letting the loss run while news
   // prices moved pips per second. Escalate the price tolerance instead:
   // 1x -> 3x -> 6x InpSlippage, and only for tolerance-class rejections
   // (a disabled-trading or invalid-request rejection fails at any deviation).
   int closeDeviations[3];
   closeDeviations[0] = InpSlippage;
   closeDeviations[1] = InpSlippage * 3;
   closeDeviations[2] = InpSlippage * 6;
   bool closeExecuted = false;
   for(int attempt = 0; attempt < 3 && !closeExecuted; attempt++)
   {
      trade.SetDeviationInPoints(closeDeviations[attempt]);
      closeExecuted = ConfirmTradeRequest(
         trade.PositionClose(g_currentTicket),
         "Position close (deviation " +
         IntegerToString(closeDeviations[attempt]) + " pts)"
      );
      if(closeExecuted)
         break;

      uint closeRetcode = trade.ResultRetcode();
      if(closeRetcode != TRADE_RETCODE_REQUOTE
         && closeRetcode != TRADE_RETCODE_PRICE_OFF
         && closeRetcode != TRADE_RETCODE_PRICE_CHANGED)
         break;
      if(attempt < 2)
         Print("Close rejected on price tolerance (retcode ", closeRetcode,
               ") - retrying with a wider deviation");
   }
   // Restore entry-grade tolerance: a wide window must never leak into the
   // next ENTRY, where slippage is also part of the lot-sizing loss model.
   trade.SetDeviationInPoints(InpSlippage);

   if(closeExecuted)
   {
      bool positionGone = false;
      for(int attempt = 0; attempt < 10; attempt++)
      {
         if(!PositionSelectByTicket(g_currentTicket))
         {
            positionGone = true;
            break;
         }
         Sleep(100);
      }
      if(!positionGone)
      {
         Print("Position close failed postcondition: broker position still exists");
         return;
      }
      Print("Position closed successfully");

      // Prefer the REALIZED P/L from the deal history over the floating
      // profit read pre-close (includes closing slippage, swap, commission
      // and any earlier partial-close portion). Bounded wait until the
      // history is COMPLETE — after a partial close an OUT deal already
      // exists, so "any OUT deal" is not proof the final leg was booked.
      ulong posId = (g_currentPositionId > 0) ? g_currentPositionId : g_currentTicket;
      string profitSource = "history";
      double realized = 0.0, histPrice = 0.0;
      string detail;
      bool complete = false, hasDeals = false;
      for(int i = 0; i < 10 && !complete; i++)
      {
         hasDeals = GetRealizedPnL(posId, realized, histPrice, detail, complete);
         if(!complete)
            Sleep(200);
      }
      if(complete)
      {
         profit = realized;
         closePrice = histPrice;
      }
      else
      {
         // Do not acknowledge an estimate as the final trade outcome. Leave
         // identity and recovery metadata intact until the broker history is
         // complete; the next tick or restart continues the same recovery.
         g_recoveryState = RECOVERY_PENDING;
         g_aiManagementActive = false;
         Print("Position is closed, but complete deal history is pending. "
               "Final P/L report will be retried.");
         return;
      }

      // Notify server about close
      bool closeReported = NotifyPositionClosed(
         closePrice, profit, reason, profitSource
      );
      if(closeReported)
      {
         ClearRecoveryMetadata();
         g_recoveryState = RECOVERY_NONE;
      }
      else
      {
         g_recoveryState = RECOVERY_PENDING;
         // Preserve identity and metadata until this exact close is ACKed.
         return;
      }
      g_positionRecovered = false;
      g_currentTicket = 0;
      g_currentPositionId = 0;
      g_lastKnownProfit = 0.0;
      g_realizedPnL = 0.0;
      g_lastSeenLots = 0.0;
      g_closeRetryCount = 0;
      g_spreadBreachTicks = 0;
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
   return SkyNormalizeVolumeDown(symbol, lots);
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
//| Calculate lot size from risk budget and SL distance                |
//+------------------------------------------------------------------+
double CalculateLotSize(string symbol, string direction, double lotPercent,
                        double entryPrice, double stopLoss)
{
   double balance = AccountInfoDouble(ACCOUNT_BALANCE);

   //--- Risk budget: % of balance, but never more than the USD loss
   //--- guardrail (g_maxLossUSD, from the server signal) — the position
   //--- gets force-closed at -g_maxLossUSD anyway, so sizing beyond it only
   //--- guarantees the guardrail fires instantly
   //--- (on the first live event a 50-lot position died to spread in 1s)
   double riskAmount = balance * InpRiskPercent / 100.0;
   if(riskAmount > g_maxLossUSD)
      riskAmount = g_maxLossUSD;
   riskAmount *= lotPercent / 100.0;

   if(!MathIsValidNumber(lotPercent) || lotPercent <= 0
      || lotPercent > 100
      || !MathIsValidNumber(riskAmount) || riskAmount <= 0
      || !MathIsValidNumber(entryPrice) || entryPrice <= 0
      || !MathIsValidNumber(stopLoss) || stopLoss <= 0
      || (direction != "BUY" && direction != "SELL"))
      return 0;

   // Ask the broker to value the exact final stop in account currency.
   // This handles cross-currency conversion and non-standard tick economics.
   ENUM_ORDER_TYPE orderType =
      (direction == "BUY") ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double projectedPnL = 0;
   if(!OrderCalcProfit(
         orderType, symbol, 1.0, entryPrice, stopLoss, projectedPnL))
      return 0;
   double lossPerLot = MathAbs(projectedPnL);
   if(!MathIsValidNumber(lossPerLot) || lossPerLot <= 0)
      return 0;

   double lots = riskAmount / lossPerLot;

   // Free-margin cap. At forex leverage (1:500) the margin per lot is tiny
   // and this never binds; on 1:100 index/gold CFDs the risk-based lot can
   // exceed what the account can carry and the broker answers 10019
   // (no money). Margin is symmetric for CFDs, so the trade's own side and
   // entry price are used only because they are already known here.
   double marginPerLot = 0;
   if(InpMaxMarginUsePercent > 0
      && OrderCalcMargin(orderType, symbol, 1.0, entryPrice, marginPerLot)
      && MathIsValidNumber(marginPerLot) && marginPerLot > 0)
   {
      double freeMargin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
      double maxLotsByMargin =
         (freeMargin * InpMaxMarginUsePercent / 100.0) / marginPerLot;
      if(MathIsValidNumber(maxLotsByMargin) && lots > maxLotsByMargin)
      {
         Print("Lot capped by free margin: risk-based ",
               DoubleToString(lots, 4), " -> ",
               DoubleToString(maxLotsByMargin, 4),
               " lots (free margin ", DoubleToString(freeMargin, 2),
               ", margin/lot ", DoubleToString(marginPerLot, 2),
               ", cap ", DoubleToString(InpMaxMarginUsePercent, 0), "%)");
         lots = maxLotsByMargin;
      }
   }

   // Never promote an under-minimum risk result to the broker minimum.
   return SkyNormalizeVolumeDown(symbol, lots);
}

//+------------------------------------------------------------------+
//| Instrument root: uppercase, "/" removed, cut at the first broker    |
//| suffix separator ('.', '_' or '-'). GER40.cash -> GER40,           |
//| XAUUSD.pro -> XAUUSD, EURUSD_SB -> EURUSD, US500-F -> US500.        |
//+------------------------------------------------------------------+
string SkyRootOf(string s)
{
   string root = s;
   StringReplace(root, "/", "");
   StringToUpper(root);
   int cut = StringLen(root);
   int dot = StringFind(root, ".");
   int under = StringFind(root, "_");
   int dash = StringFind(root, "-");
   if(dot >= 0 && dot < cut) cut = dot;
   if(under >= 0 && under < cut) cut = under;
   if(dash >= 0 && dash < cut) cut = dash;
   return StringSubstr(root, 0, cut);
}

//+------------------------------------------------------------------+
//| Convert pair format (EURUSD -> EURUSD or EUR/USD -> EURUSD)        |
//+------------------------------------------------------------------+
string ConvertPairToSymbol(string pair)
{
   // Root guard FIRST: a server pair whose instrument root equals the
   // chart's root (GER40 vs GER40.cash, XAUUSD vs XAUUSD.pro, and every
   // forex case) always resolves to the chart's own symbol. Recovery
   // filters POSITION_SYMBOL == _Symbol, and 5-char index roots would
   // otherwise fall through to the forex-suffix probe below.
   string pairRoot = SkyRootOf(pair);
   if(StringLen(pairRoot) > 0 && pairRoot == SkyRootOf(_Symbol))
      return _Symbol;

   string symbol = pair;
   StringReplace(symbol, "/", "");

   // Prefer the chart's exact broker symbol when its six-letter FX base
   // matches the server pair. Recovery is keyed by _Symbol, so selecting a
   // different tradable suffix here could otherwise orphan a late fill.
   if(StringLen(symbol) >= 6 && StringLen(_Symbol) >= 6
      && StringSubstr(symbol, 0, 6) == StringSubstr(_Symbol, 0, 6))
      return _Symbol;

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
