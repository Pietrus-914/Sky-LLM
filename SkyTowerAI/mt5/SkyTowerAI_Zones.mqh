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
   ENUM_EXIT_STRATEGY m_exit_strategy;
   int               m_fallback_exit_minutes;
   int               m_max_hold_minutes;
   bool              m_use_zone_targets;
   bool              m_partial_close_at_tp1;
   bool              m_move_sl_to_be;
   bool              m_trail_after_tp1;
   double            m_trail_distance_pips;

   // Current position state
   SPositionState    m_position;

   // Helper methods
   bool              SendOHLCToServer(string symbol, string direction, STradeTargets &targets);
   string            BuildOHLCJson(string symbol, int bars_count);
   bool              ParseTargetsResponse(string json, STradeTargets &targets);
   double            PipsToPrice(double pips, string symbol);
   double            PriceToPips(double price_diff, string symbol);

public:
                     CSmartExitManager();
                    ~CSmartExitManager();

   // Initialization
   void              Init(string host, int port,
                         ENUM_EXIT_STRATEGY strategy,
                         int fallback_minutes,
                         int max_hold_minutes,
                         bool use_zone_targets,
                         bool partial_close,
                         bool move_sl_to_be,
                         bool trail_after_tp1,
                         double trail_pips);

   // Position management
   bool              OnNewPosition(ulong ticket, string symbol, string direction,
                                  double entry_price, double lots);
   bool              OnTick(double bid, double ask);
   bool              OnPositionClosed();

   // Zone/Target retrieval
   bool              GetTargetsFromServer(string symbol, string direction, STradeTargets &targets);
   bool              GetZoneAnalysisFromServer(string symbol, SZoneAnalysis &analysis);

   // Exit logic
   bool              ShouldClosePartial(double current_price);
   bool              ShouldMoveSLToBreakeven(double current_price);
   bool              ShouldTrailStop(double current_price, double &new_sl);
   bool              ShouldExitByTime();
   bool              ShouldExitByTarget(double current_price);

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

   m_exit_strategy = EXIT_HYBRID;
   m_fallback_exit_minutes = 15;
   m_max_hold_minutes = 30;
   m_use_zone_targets = true;
   m_partial_close_at_tp1 = true;
   m_move_sl_to_be = true;
   m_trail_after_tp1 = true;
   m_trail_distance_pips = 10;

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
void CSmartExitManager::Init(string host, int port,
                             ENUM_EXIT_STRATEGY strategy,
                             int fallback_minutes,
                             int max_hold_minutes,
                             bool use_zone_targets,
                             bool partial_close,
                             bool move_sl_to_be,
                             bool trail_after_tp1,
                             double trail_pips)
{
   m_server_host = host;
   m_server_port = port;
   m_exit_strategy = strategy;
   m_fallback_exit_minutes = fallback_minutes;
   m_max_hold_minutes = max_hold_minutes;
   m_use_zone_targets = use_zone_targets;
   m_partial_close_at_tp1 = partial_close;
   m_move_sl_to_be = move_sl_to_be;
   m_trail_after_tp1 = trail_after_tp1;
   m_trail_distance_pips = trail_pips;

   Print("SmartExitManager initialized: strategy=", EnumToString(strategy),
         ", fallback=", fallback_minutes, "min, max_hold=", max_hold_minutes, "min");
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
//| Called on each tick to update position state                      |
//+------------------------------------------------------------------+
bool CSmartExitManager::OnTick(double bid, double ask)
{
   if(m_position.ticket == 0)
      return false;

   double current_price = (m_position.direction == "BUY") ? bid : ask;

   // Update high/low tracking
   if(current_price > m_position.highest_price)
      m_position.highest_price = current_price;
   if(current_price < m_position.lowest_price)
      m_position.lowest_price = current_price;

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
//| Build OHLC JSON from chart data                                   |
//+------------------------------------------------------------------+
string CSmartExitManager::BuildOHLCJson(string symbol, int bars_count)
{
   MqlRates rates[];
   ArraySetAsSeries(rates, true);

   int copied = CopyRates(symbol, PERIOD_M1, 0, bars_count, rates);
   if(copied < 10)
   {
      Print("Failed to copy rates: ", copied);
      return "";
   }

   string json = "[";

   for(int i = copied - 1; i >= 0; i--)  // Oldest first
   {
      if(i < copied - 1)
         json += ",";

      json += "{";
      json += "\"time\":" + IntegerToString((long)rates[i].time) + ",";
      json += "\"open\":" + DoubleToString(rates[i].open, 5) + ",";
      json += "\"high\":" + DoubleToString(rates[i].high, 5) + ",";
      json += "\"low\":" + DoubleToString(rates[i].low, 5) + ",";
      json += "\"close\":" + DoubleToString(rates[i].close, 5) + ",";
      json += "\"volume\":" + IntegerToString(rates[i].tick_volume);
      json += "}";
   }

   json += "]";
   return json;
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
//| Check if should close partial position                            |
//+------------------------------------------------------------------+
bool CSmartExitManager::ShouldClosePartial(double current_price)
{
   if(!m_partial_close_at_tp1 || m_position.tp1_hit)
      return false;

   if(m_position.direction == "BUY")
   {
      if(current_price >= m_position.targets.tp1)
      {
         m_position.tp1_hit = true;
         return true;
      }
   }
   else // SELL
   {
      if(current_price <= m_position.targets.tp1)
      {
         m_position.tp1_hit = true;
         return true;
      }
   }

   return false;
}

//+------------------------------------------------------------------+
//| Check if should move SL to break-even                             |
//+------------------------------------------------------------------+
bool CSmartExitManager::ShouldMoveSLToBreakeven(double current_price)
{
   if(!m_move_sl_to_be || m_position.sl_moved_to_be || !m_position.tp1_hit)
      return false;

   // Already past TP1, move SL to break-even
   return true;
}

//+------------------------------------------------------------------+
//| Check if should trail stop                                        |
//+------------------------------------------------------------------+
bool CSmartExitManager::ShouldTrailStop(double current_price, double &new_sl)
{
   if(!m_trail_after_tp1 || !m_position.tp1_hit)
      return false;

   double trail_distance = PipsToPrice(m_trail_distance_pips, m_position.symbol);

   if(m_position.direction == "BUY")
   {
      double potential_sl = m_position.highest_price - trail_distance;
      if(potential_sl > m_position.targets.sl)
      {
         new_sl = potential_sl;
         return true;
      }
   }
   else // SELL
   {
      double potential_sl = m_position.lowest_price + trail_distance;
      if(potential_sl < m_position.targets.sl)
      {
         new_sl = potential_sl;
         return true;
      }
   }

   return false;
}

//+------------------------------------------------------------------+
//| Check if should exit by time (fallback/max hold)                  |
//+------------------------------------------------------------------+
bool CSmartExitManager::ShouldExitByTime()
{
   int minutes_open = (int)((TimeCurrent() - m_position.open_time) / 60);

   // Max hold time exceeded
   if(minutes_open >= m_max_hold_minutes)
      return true;

   // Hybrid mode: fallback exit if no TP1 hit and time exceeded
   if(m_exit_strategy == EXIT_HYBRID && !m_position.tp1_hit)
   {
      if(minutes_open >= m_fallback_exit_minutes)
         return true;
   }

   // Time-based mode: always exit after fallback time
   if(m_exit_strategy == EXIT_TIME_BASED)
   {
      if(minutes_open >= m_fallback_exit_minutes)
         return true;
   }

   return false;
}

//+------------------------------------------------------------------+
//| Check if should exit by reaching TP2                              |
//+------------------------------------------------------------------+
bool CSmartExitManager::ShouldExitByTarget(double current_price)
{
   if(m_exit_strategy == EXIT_TIME_BASED)
      return false;

   if(m_position.direction == "BUY")
   {
      if(current_price >= m_position.targets.tp2)
         return true;
   }
   else // SELL
   {
      if(current_price <= m_position.targets.tp2)
         return true;
   }

   return false;
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
//| Convert price difference to pips                                  |
//+------------------------------------------------------------------+
double CSmartExitManager::PriceToPips(double price_diff, string symbol)
{
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);

   if(digits == 3 || digits == 5)
      return MathAbs(price_diff) / (point * 10);
   else
      return MathAbs(price_diff) / point;
}
//+------------------------------------------------------------------+
