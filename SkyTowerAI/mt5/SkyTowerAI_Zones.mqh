//+------------------------------------------------------------------+
//|                                           SkyTowerAI_Zones.mqh   |
//|                                    SkyTower-AI Trading System    |
//|                Smart Exit Module - Zone-Based Target Management  |
//+------------------------------------------------------------------+
#property copyright "SkyTower-AI"
#property version   "1.00"

//+------------------------------------------------------------------+
//| Enumerations                                                      |
//+------------------------------------------------------------------+
enum ENUM_EXIT_STRATEGY
{
   EXIT_ZONE_BASED,     // Zone-Based Exit (use detected zones)
   EXIT_TIME_BASED,     // Time-Based Exit (legacy - exit after X minutes)
   EXIT_HYBRID,         // Hybrid (zones with time fallback)
   EXIT_PARTIAL_TP      // Partial TP (close portions at multiple levels)
};

enum ENUM_ZONE_TYPE
{
   ZONE_LIQUIDITY_HIGH,   // Liquidity Pool (equal highs)
   ZONE_LIQUIDITY_LOW,    // Liquidity Pool (equal lows)
   ZONE_FVG_BULLISH,      // Fair Value Gap (bullish)
   ZONE_FVG_BEARISH,      // Fair Value Gap (bearish)
   ZONE_ORDER_BLOCK_BUY,  // Order Block (bullish)
   ZONE_ORDER_BLOCK_SELL  // Order Block (bearish)
};

//+------------------------------------------------------------------+
//| Zone structure                                                    |
//+------------------------------------------------------------------+
struct SZone
{
   ENUM_ZONE_TYPE type;
   double         price_high;
   double         price_low;
   double         midpoint;
   double         size_pips;
   int            strength;      // 1=weak, 2=moderate, 3=strong
   int            touches;
   bool           is_filled;
   datetime       creation_time;

   void Clear()
   {
      type = ZONE_LIQUIDITY_HIGH;
      price_high = 0;
      price_low = 0;
      midpoint = 0;
      size_pips = 0;
      strength = 0;
      touches = 0;
      is_filled = false;
      creation_time = 0;
   }
};

//+------------------------------------------------------------------+
//| Trade targets structure                                           |
//+------------------------------------------------------------------+
struct STradeTargets
{
   double tp1;                    // First take profit
   double tp2;                    // Second take profit
   double sl;                     // Stop loss
   double tp1_pips;               // TP1 distance in pips
   double tp2_pips;               // TP2 distance in pips
   double sl_pips;                // SL distance in pips
   double risk_reward;            // Risk/Reward ratio
   int    tp1_close_percent;      // % to close at TP1
   int    tp2_close_percent;      // % to close at TP2
   bool   move_sl_to_be_at_tp1;   // Move SL to break-even after TP1
   string tp1_zone_type;          // Zone type for TP1
   string tp2_zone_type;          // Zone type for TP2
   string sl_zone_type;           // Zone type for SL
   double confidence;             // Target confidence (0-1)
   bool   valid;                  // Whether targets are valid

   void Clear()
   {
      tp1 = 0;
      tp2 = 0;
      sl = 0;
      tp1_pips = 0;
      tp2_pips = 0;
      sl_pips = 0;
      risk_reward = 0;
      tp1_close_percent = 50;
      tp2_close_percent = 100;
      move_sl_to_be_at_tp1 = true;
      tp1_zone_type = "";
      tp2_zone_type = "";
      sl_zone_type = "";
      confidence = 0;
      valid = false;
   }
};

//+------------------------------------------------------------------+
//| Zone analysis result                                              |
//+------------------------------------------------------------------+
struct SZoneAnalysis
{
   string symbol;
   double current_price;
   SZone  liquidity_above[];
   SZone  liquidity_below[];
   SZone  fvg_above[];
   SZone  fvg_below[];
   string direction_bias;         // "BUY", "SELL", or ""
   int    bias_strength;          // 0-3

   void Clear()
   {
      symbol = "";
      current_price = 0;
      ArrayResize(liquidity_above, 0);
      ArrayResize(liquidity_below, 0);
      ArrayResize(fvg_above, 0);
      ArrayResize(fvg_below, 0);
      direction_bias = "";
      bias_strength = 0;
   }
};

//+------------------------------------------------------------------+
//| Position state for smart exit management                          |
//+------------------------------------------------------------------+
struct SPositionState
{
   ulong  ticket;
   string symbol;
   string direction;              // "BUY" or "SELL"
   double entry_price;
   double initial_lots;
   double current_lots;
   datetime open_time;

   // Targets
   STradeTargets targets;

   // State tracking
   bool   tp1_hit;                // Has TP1 been reached?
   bool   sl_moved_to_be;         // Has SL been moved to break-even?
   double highest_price;          // Highest price since entry (for trailing)
   double lowest_price;           // Lowest price since entry (for trailing)
   bool   trailing_active;        // Is trailing stop active?

   void Clear()
   {
      ticket = 0;
      symbol = "";
      direction = "";
      entry_price = 0;
      initial_lots = 0;
      current_lots = 0;
      open_time = 0;
      targets.Clear();
      tp1_hit = false;
      sl_moved_to_be = false;
      highest_price = 0;
      lowest_price = DBL_MAX;
      trailing_active = false;
   }
};

//+------------------------------------------------------------------+
//| Class for zone-based exit management                              |
//+------------------------------------------------------------------+
class CSmartExitManager
{
private:
   string            m_server_host;
   int               m_server_port;
   int               m_timeout_ms;

   // Exit settings (from EA inputs)
   bool              m_use_zone_targets;

   // Current position state
   SPositionState    m_position;

   // Helper methods
   bool              ParseTargetsResponse(string json, STradeTargets &targets);
   double            PipsToPrice(double pips, string symbol);

public:
                     CSmartExitManager();
                    ~CSmartExitManager();

   // Initialization
   void              Init(string host, int port, bool use_zone_targets);

   // Position management
   bool              OnNewPosition(ulong ticket, string symbol, string direction,
                                  double entry_price, double lots);
   bool              OnRecoveredPosition(ulong ticket, string symbol, string direction,
                                         double entry_price, double original_lots,
                                         double current_lots, datetime open_time,
                                         double sl, double tp);
   bool              OnPositionClosed();

   // Zone/Target retrieval
   bool              GetTargetsFromServer(string symbol, string direction, STradeTargets &targets);


   // Getters
   STradeTargets     GetCurrentTargets() { return m_position.targets; }
   SPositionState    GetPositionState() { return m_position; }
   bool              HasPosition() { return m_position.ticket > 0; }
   bool              IsTP1Hit() { return m_position.tp1_hit; }
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CSmartExitManager::CSmartExitManager()
{
   m_server_host = "127.0.0.1";
   m_server_port = 5555;
   m_timeout_ms = 5000;

   m_use_zone_targets = true;

   m_position.Clear();
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CSmartExitManager::~CSmartExitManager()
{
}

//+------------------------------------------------------------------+
//| Initialize with settings                                          |
//+------------------------------------------------------------------+
void CSmartExitManager::Init(string host, int port, bool use_zone_targets)
{
   m_server_host = host;
   m_server_port = port;
   m_use_zone_targets = use_zone_targets;

   Print("SmartExitManager initialized: zone targets=",
         m_use_zone_targets ? "on" : "off",
         " (exit management is server-owned: MODIFY_SL / CLOSE commands)");
}

//+------------------------------------------------------------------+
//| Called when new position is opened                                |
//+------------------------------------------------------------------+
bool CSmartExitManager::OnNewPosition(ulong ticket, string symbol, string direction,
                                      double entry_price, double lots)
{
   m_position.Clear();

   m_position.ticket = ticket;
   m_position.symbol = symbol;
   m_position.direction = direction;
   m_position.entry_price = entry_price;
   m_position.initial_lots = lots;
   m_position.current_lots = lots;
   m_position.open_time = TimeCurrent();
   m_position.highest_price = entry_price;
   m_position.lowest_price = entry_price;

   // Get targets from server
   if(m_use_zone_targets)
   {
      if(GetTargetsFromServer(symbol, direction, m_position.targets))
      {
         Print("Smart targets received: TP1=", m_position.targets.tp1,
               " (", m_position.targets.tp1_pips, "p), TP2=", m_position.targets.tp2,
               " (", m_position.targets.tp2_pips, "p), SL=", m_position.targets.sl,
               " (", m_position.targets.sl_pips, "p), RR=", m_position.targets.risk_reward);
         return true;
      }
      else
      {
         Print("WARNING: Could not get zone targets, using defaults");
         // Set default targets
         double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
         double default_tp_pips = 40;
         double default_sl_pips = 30;

         if(direction == "BUY")
         {
            m_position.targets.tp1 = entry_price + PipsToPrice(default_tp_pips, symbol);
            m_position.targets.tp2 = entry_price + PipsToPrice(default_tp_pips * 1.5, symbol);
            m_position.targets.sl = entry_price - PipsToPrice(default_sl_pips, symbol);
         }
         else
         {
            m_position.targets.tp1 = entry_price - PipsToPrice(default_tp_pips, symbol);
            m_position.targets.tp2 = entry_price - PipsToPrice(default_tp_pips * 1.5, symbol);
            m_position.targets.sl = entry_price + PipsToPrice(default_sl_pips, symbol);
         }

         m_position.targets.tp1_pips = default_tp_pips;
         m_position.targets.tp2_pips = default_tp_pips * 1.5;
         m_position.targets.sl_pips = default_sl_pips;
         m_position.targets.risk_reward = default_tp_pips / default_sl_pips;
         m_position.targets.tp1_zone_type = "default";
         m_position.targets.valid = true;
      }
   }

   return true;
}

//+------------------------------------------------------------------+
//| Restore local state without fetching or applying new trade targets |
//+------------------------------------------------------------------+
bool CSmartExitManager::OnRecoveredPosition(ulong ticket, string symbol,
                                             string direction, double entry_price,
                                             double original_lots,
                                             double current_lots,
                                             datetime open_time, double sl, double tp)
{
   m_position.Clear();
   m_position.ticket = ticket;
   m_position.symbol = symbol;
   m_position.direction = direction;
   m_position.entry_price = entry_price;
   m_position.initial_lots = original_lots;
   m_position.current_lots = current_lots;
   m_position.open_time = open_time;
   m_position.highest_price = entry_price;
   m_position.lowest_price = entry_price;
   m_position.targets.Clear();
   m_position.targets.sl = sl;
   m_position.targets.tp1 = tp;
   m_position.targets.valid = false;
   return true;
}

//+------------------------------------------------------------------+
//| Called when position is closed                                    |
//+------------------------------------------------------------------+
bool CSmartExitManager::OnPositionClosed()
{
   m_position.Clear();
   return true;
}

//+------------------------------------------------------------------+
//| Get targets from Python server                                    |
//+------------------------------------------------------------------+
bool CSmartExitManager::GetTargetsFromServer(string symbol, string direction, STradeTargets &targets)
{
   targets.Clear();

   // Use GET request with URL parameters (avoids WebRequest POST data limits)
   string url = "http://" + m_server_host + ":" + IntegerToString(m_server_port) + "/api/targets";
   url += "?symbol=" + symbol;
   url += "&direction=" + direction;
   url += "&entry_price=" + DoubleToString(m_position.entry_price, 5);

   // Make GET request (empty post data = GET)
   char post_data[];
   char result_data[];
   string result_headers;
   string headers = "";

   ResetLastError();
   int response = WebRequest("GET", url, headers, m_timeout_ms, post_data, result_data, result_headers);

   if(response == -1)
   {
      int error = GetLastError();
      if(error == 4014)
         Print("Add URL to allowed list: ", url);
      else
         Print("WebRequest error: ", error);
      return false;
   }

   if(response != 200)
   {
      Print("Server returned error: ", response);
      return false;
   }

   string result = CharArrayToString(result_data, 0, WHOLE_ARRAY, CP_UTF8);
   return ParseTargetsResponse(result, targets);
}

//+------------------------------------------------------------------+
//| Parse targets response JSON                                       |
//+------------------------------------------------------------------+
bool CSmartExitManager::ParseTargetsResponse(string json, STradeTargets &targets)
{
   // Check status
   if(StringFind(json, "\"status\":\"ok\"") < 0)
   {
      Print("Server returned error status");
      return false;
   }

   // Check validity
   if(StringFind(json, "\"valid\":true") < 0)
   {
      Print("Targets not valid according to server");
      // Continue anyway but mark as not valid
   }
   else
   {
      targets.valid = true;
   }

   // Parse targets
   targets.tp1 = ExtractJsonDouble(json, "tp1");
   targets.tp2 = ExtractJsonDouble(json, "tp2");
   targets.sl = ExtractJsonDouble(json, "sl");
   targets.tp1_pips = ExtractJsonDouble(json, "tp1_pips");
   targets.tp2_pips = ExtractJsonDouble(json, "tp2_pips");
   targets.sl_pips = ExtractJsonDouble(json, "sl_pips");

   // risk_reward can be "risk_reward" or "risk_reward_tp1"
   targets.risk_reward = ExtractJsonDouble(json, "risk_reward");
   if(targets.risk_reward == 0)
      targets.risk_reward = ExtractJsonDouble(json, "risk_reward_tp1");

   targets.confidence = ExtractJsonDouble(json, "confidence");

   // Parse percentages
   targets.tp1_close_percent = (int)ExtractJsonDouble(json, "tp1_close_percent");
   if(targets.tp1_close_percent == 0) targets.tp1_close_percent = 50;

   targets.tp2_close_percent = (int)ExtractJsonDouble(json, "tp2_close_percent");
   if(targets.tp2_close_percent == 0) targets.tp2_close_percent = 100;

   // Parse move SL flag (handles "move_sl_to_be" or "move_sl_to_be_at_tp1")
   targets.move_sl_to_be_at_tp1 = (StringFind(json, "\"move_sl_to_be\":true") >= 0 ||
                                   StringFind(json, "\"move_sl_to_be\": true") >= 0 ||
                                   StringFind(json, "\"move_sl_to_be_at_tp1\":true") >= 0 ||
                                   StringFind(json, "\"move_sl_to_be_at_tp1\": true") >= 0);

   // Parse zone types
   targets.tp1_zone_type = ExtractJsonString(json, "tp1_zone_type");
   targets.tp2_zone_type = ExtractJsonString(json, "tp2_zone_type");
   targets.sl_zone_type = ExtractJsonString(json, "sl_zone_type");

   return (targets.tp1 > 0 && targets.sl > 0);
}

//+------------------------------------------------------------------+
//| Convert pips to price                                             |
//+------------------------------------------------------------------+
double CSmartExitManager::PipsToPrice(double pips, string symbol)
{
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(digits == 3 || digits == 5)
      return pips * point * 10;
   else
      return pips * point;
}

//+------------------------------------------------------------------+
