//+------------------------------------------------------------------+
//|                                              SkyTower_Zones.mq5  |
//|                                     SkyTower-AI Zone Detector    |
//|                                                                  |
//| Detects market structure zones for smart TP/SL placement:        |
//| - Liquidity Pools (equal highs/lows)                            |
//| - Fair Value Gaps (FVG/Imbalance)                               |
//| - Order Blocks                                                   |
//|                                                                  |
//| Multi-timeframe: H1 primary, H4 confirmation                    |
//+------------------------------------------------------------------+
#property copyright "SkyTower-AI"
#property link      "https://skytower-ai.local"
#property version   "1.00"
#property description "Smart Money Zone Detector for SkyTower-AI"

#property indicator_chart_window
#property indicator_buffers 8
#property indicator_plots   0  // No line plots, only objects (boxes)

//--- Input parameters
input group "=== Analysis Settings ==="
input int      InpLookbackH1 = 200;           // H1 Lookback (bars)
input int      InpLookbackH4 = 200;           // H4 Lookback (bars)
input double   InpEqualLevelPips = 5.0;       // Equal Level Tolerance (pips)
input int      InpMinTouches = 2;             // Min Touches for Liquidity
input double   InpMinFVGPips = 3.0;           // Min FVG Size (pips)
input double   InpImpulseMultiplier = 2.0;    // Impulse Multiplier for OB

input group "=== Visual Settings ==="
input color    InpLiquidityHighColor = clrCrimson;      // Liquidity High Color
input color    InpLiquidityLowColor = clrDodgerBlue;    // Liquidity Low Color
input color    InpFVGBullishColor = clrLimeGreen;       // FVG Bullish Color
input color    InpFVGBearishColor = clrOrangeRed;       // FVG Bearish Color
input color    InpOBBullishColor = clrGold;             // Order Block Bullish Color
input color    InpOBBearishColor = clrMagenta;          // Order Block Bearish Color
input int      InpBoxTransparency = 70;                 // Box Transparency (0-100)
input bool     InpShowH4Zones = true;                   // Show H4 Zones
input bool     InpShowLabels = true;                    // Show Zone Labels

input group "=== Zone Bias Output ==="
input bool     InpUseGlobalVars = true;                 // Save to Global Variables

//--- Indicator buffers (for EA access via iCustom)
double BufLiquidityHighH1[];   // 0: Nearest liquidity high H1
double BufLiquidityLowH1[];    // 1: Nearest liquidity low H1
double BufFVGHighH1[];         // 2: Nearest FVG high boundary
double BufFVGLowH1[];          // 3: Nearest FVG low boundary
double BufOBHighH1[];          // 4: Nearest order block high
double BufOBLowH1[];           // 5: Nearest order block low
double BufZoneBias[];          // 6: Zone bias (-1 to +1)
double BufZoneCount[];         // 7: Number of zones detected

//--- Zone structures
struct SZone
{
   string   name;
   int      type;        // 0=LiqHigh, 1=LiqLow, 2=FVGBull, 3=FVGBear, 4=OBBull, 5=OBBear
   double   price_high;
   double   price_low;
   datetime time_start;
   datetime time_end;
   int      touches;
   int      timeframe;   // PERIOD_H1 or PERIOD_H4
   bool     is_valid;
   bool     is_filled;
};

//--- Global arrays
SZone g_zones[];
int   g_zone_count = 0;
string g_prefix = "SKT_Zone_";

//+------------------------------------------------------------------+
//| Custom indicator initialization function                          |
//+------------------------------------------------------------------+
int OnInit()
{
   //--- Set up buffers
   SetIndexBuffer(0, BufLiquidityHighH1, INDICATOR_DATA);
   SetIndexBuffer(1, BufLiquidityLowH1, INDICATOR_DATA);
   SetIndexBuffer(2, BufFVGHighH1, INDICATOR_DATA);
   SetIndexBuffer(3, BufFVGLowH1, INDICATOR_DATA);
   SetIndexBuffer(4, BufOBHighH1, INDICATOR_DATA);
   SetIndexBuffer(5, BufOBLowH1, INDICATOR_DATA);
   SetIndexBuffer(6, BufZoneBias, INDICATOR_DATA);
   SetIndexBuffer(7, BufZoneCount, INDICATOR_DATA);

   //--- Set empty values
   PlotIndexSetDouble(0, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(1, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(2, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(3, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(4, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(5, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(6, PLOT_EMPTY_VALUE, 0);
   PlotIndexSetDouble(7, PLOT_EMPTY_VALUE, 0);

   //--- Indicator name
   IndicatorSetString(INDICATOR_SHORTNAME, "SkyTower Zones");

   //--- Clean old objects
   DeleteAllZoneObjects();

   Print("SkyTower_Zones initialized: H1=", InpLookbackH1, " bars, H4=", InpLookbackH4, " bars");

   return(INIT_SUCCEEDED);
}

//+------------------------------------------------------------------+
//| Custom indicator deinitialization                                 |
//+------------------------------------------------------------------+
void OnDeinit(const int reason)
{
   DeleteAllZoneObjects();

   if(InpUseGlobalVars)
   {
      GlobalVariableDel(g_prefix + _Symbol + "_ZoneBias");
      GlobalVariableDel(g_prefix + _Symbol + "_LiqHigh");
      GlobalVariableDel(g_prefix + _Symbol + "_LiqLow");
      GlobalVariableDel(g_prefix + _Symbol + "_FVGHigh");
      GlobalVariableDel(g_prefix + _Symbol + "_FVGLow");
   }
}

//+------------------------------------------------------------------+
//| Delete all zone objects                                           |
//+------------------------------------------------------------------+
void DeleteAllZoneObjects()
{
   int total = ObjectsTotal(0, 0, OBJ_RECTANGLE);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, OBJ_RECTANGLE);
      if(StringFind(name, g_prefix) >= 0)
         ObjectDelete(0, name);
   }

   total = ObjectsTotal(0, 0, OBJ_TEXT);
   for(int i = total - 1; i >= 0; i--)
   {
      string name = ObjectName(0, i, 0, OBJ_TEXT);
      if(StringFind(name, g_prefix) >= 0)
         ObjectDelete(0, name);
   }
}

//+------------------------------------------------------------------+
//| Custom indicator iteration function                               |
//+------------------------------------------------------------------+
int OnCalculate(const int rates_total,
                const int prev_calculated,
                const datetime &time[],
                const double &open[],
                const double &high[],
                const double &low[],
                const double &close[],
                const long &tick_volume[],
                const long &volume[],
                const int &spread[])
{
   //--- Check for new bar (recalculate only on new bar)
   static datetime lastBarTime = 0;
   datetime currentBarTime = iTime(_Symbol, PERIOD_CURRENT, 0);

   if(currentBarTime == lastBarTime && prev_calculated > 0)
      return rates_total;

   lastBarTime = currentBarTime;

   //--- Clear zones array
   ArrayResize(g_zones, 0);
   g_zone_count = 0;
   DeleteAllZoneObjects();

   //--- Analyze H1 timeframe (primary)
   AnalyzeTimeframe(PERIOD_H1, InpLookbackH1);

   //--- Analyze H4 timeframe (confirmation)
   if(InpShowH4Zones)
      AnalyzeTimeframe(PERIOD_H4, InpLookbackH4);

   //--- Calculate zone bias
   double currentPrice = close[rates_total - 1];
   double zoneBias = CalculateZoneBias(currentPrice);

   //--- Update buffers (last bar)
   UpdateBuffers(rates_total - 1, currentPrice);

   //--- Draw zones
   DrawAllZones();

   //--- Save to global variables
   if(InpUseGlobalVars)
   {
      GlobalVariableSet(g_prefix + _Symbol + "_ZoneBias", zoneBias);
      GlobalVariableSet(g_prefix + _Symbol + "_ZoneCount", (double)g_zone_count);

      if(BufLiquidityHighH1[rates_total - 1] > 0)
         GlobalVariableSet(g_prefix + _Symbol + "_LiqHigh", BufLiquidityHighH1[rates_total - 1]);
      if(BufLiquidityLowH1[rates_total - 1] > 0)
         GlobalVariableSet(g_prefix + _Symbol + "_LiqLow", BufLiquidityLowH1[rates_total - 1]);
      if(BufFVGHighH1[rates_total - 1] > 0)
         GlobalVariableSet(g_prefix + _Symbol + "_FVGHigh", BufFVGHighH1[rates_total - 1]);
      if(BufFVGLowH1[rates_total - 1] > 0)
         GlobalVariableSet(g_prefix + _Symbol + "_FVGLow", BufFVGLowH1[rates_total - 1]);
   }

   //--- Debug output
   if(g_zone_count > 0)
      Print("SkyTower Zones: ", g_zone_count, " zones detected, bias=", DoubleToString(zoneBias, 2));

   return(rates_total);
}

//+------------------------------------------------------------------+
//| Analyze a specific timeframe                                      |
//+------------------------------------------------------------------+
void AnalyzeTimeframe(ENUM_TIMEFRAMES tf, int lookback)
{
   //--- Get OHLC data
   double highs[], lows[], opens[], closes[];
   datetime times[];

   int copied = CopyHigh(_Symbol, tf, 0, lookback, highs);
   if(copied < lookback) return;

   CopyLow(_Symbol, tf, 0, lookback, lows);
   CopyOpen(_Symbol, tf, 0, lookback, opens);
   CopyClose(_Symbol, tf, 0, lookback, closes);
   CopyTime(_Symbol, tf, 0, lookback, times);

   //--- Set as series (newest = 0)
   ArraySetAsSeries(highs, true);
   ArraySetAsSeries(lows, true);
   ArraySetAsSeries(opens, true);
   ArraySetAsSeries(closes, true);
   ArraySetAsSeries(times, true);

   //--- Detect zones
   DetectLiquidityPools(highs, lows, times, lookback, tf);
   DetectFVG(highs, lows, opens, closes, times, lookback, tf);
   DetectOrderBlocks(highs, lows, opens, closes, times, lookback, tf);
}

//+------------------------------------------------------------------+
//| Detect Liquidity Pools (equal highs/lows)                        |
//+------------------------------------------------------------------+
void DetectLiquidityPools(const double &highs[], const double &lows[],
                          const datetime &times[], int count, ENUM_TIMEFRAMES tf)
{
   double tolerance = InpEqualLevelPips * _Point * 10;

   //--- Find swing highs
   for(int i = 2; i < count - 2; i++)
   {
      //--- Swing high detection
      if(highs[i] > highs[i-1] && highs[i] > highs[i-2] &&
         highs[i] > highs[i+1] && highs[i] > highs[i+2])
      {
         //--- Check for equal highs (liquidity)
         int touches = 1;
         double maxHigh = highs[i];
         double minHigh = highs[i];

         for(int j = i + 3; j < count - 2; j++)
         {
            if(highs[j] > highs[j-1] && highs[j] > highs[j-2] &&
               highs[j] > highs[j+1] && highs[j] > highs[j+2])
            {
               if(MathAbs(highs[j] - highs[i]) <= tolerance)
               {
                  touches++;
                  maxHigh = MathMax(maxHigh, highs[j]);
                  minHigh = MathMin(minHigh, highs[j]);
               }
            }
         }

         if(touches >= InpMinTouches)
         {
            SZone zone;
            zone.name = StringFormat("LiqHigh_%d_%d", tf, i);
            zone.type = 0; // Liquidity High
            zone.price_high = maxHigh + tolerance;
            zone.price_low = minHigh - tolerance;
            zone.time_start = times[i];
            zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
            zone.touches = touches;
            zone.timeframe = tf;
            zone.is_valid = true;
            zone.is_filled = false;

            AddZone(zone);
         }
      }

      //--- Swing low detection
      if(lows[i] < lows[i-1] && lows[i] < lows[i-2] &&
         lows[i] < lows[i+1] && lows[i] < lows[i+2])
      {
         //--- Check for equal lows (liquidity)
         int touches = 1;
         double maxLow = lows[i];
         double minLow = lows[i];

         for(int j = i + 3; j < count - 2; j++)
         {
            if(lows[j] < lows[j-1] && lows[j] < lows[j-2] &&
               lows[j] < lows[j+1] && lows[j] < lows[j+2])
            {
               if(MathAbs(lows[j] - lows[i]) <= tolerance)
               {
                  touches++;
                  maxLow = MathMax(maxLow, lows[j]);
                  minLow = MathMin(minLow, lows[j]);
               }
            }
         }

         if(touches >= InpMinTouches)
         {
            SZone zone;
            zone.name = StringFormat("LiqLow_%d_%d", tf, i);
            zone.type = 1; // Liquidity Low
            zone.price_high = maxLow + tolerance;
            zone.price_low = minLow - tolerance;
            zone.time_start = times[i];
            zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
            zone.touches = touches;
            zone.timeframe = tf;
            zone.is_valid = true;
            zone.is_filled = false;

            AddZone(zone);
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Detect Fair Value Gaps (FVG/Imbalance)                           |
//+------------------------------------------------------------------+
void DetectFVG(const double &highs[], const double &lows[],
               const double &opens[], const double &closes[],
               const datetime &times[], int count, ENUM_TIMEFRAMES tf)
{
   double minGap = InpMinFVGPips * _Point * 10;
   double currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   for(int i = 1; i < count - 1; i++)
   {
      //--- Bullish FVG: gap between bar[i-1] high and bar[i+1] low
      //    (candle i is the impulse, gap forms when prev high < next low)
      if(lows[i-1] > highs[i+1])
      {
         double gapSize = lows[i-1] - highs[i+1];
         if(gapSize >= minGap)
         {
            //--- Check if FVG is still unfilled
            bool filled = false;
            double midpoint = (lows[i-1] + highs[i+1]) / 2;

            for(int j = i - 2; j >= 0; j--)
            {
               if(lows[j] <= midpoint)
               {
                  filled = true;
                  break;
               }
            }

            if(!filled && midpoint < currentPrice) // FVG below price (bullish target)
            {
               SZone zone;
               zone.name = StringFormat("FVG_Bull_%d_%d", tf, i);
               zone.type = 2; // FVG Bullish
               zone.price_high = lows[i-1];
               zone.price_low = highs[i+1];
               zone.time_start = times[i];
               zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
               zone.touches = 0;
               zone.timeframe = tf;
               zone.is_valid = true;
               zone.is_filled = false;

               AddZone(zone);
            }
         }
      }

      //--- Bearish FVG: gap between bar[i-1] low and bar[i+1] high
      if(highs[i-1] < lows[i+1])
      {
         double gapSize = lows[i+1] - highs[i-1];
         if(gapSize >= minGap)
         {
            //--- Check if FVG is still unfilled
            bool filled = false;
            double midpoint = (highs[i-1] + lows[i+1]) / 2;

            for(int j = i - 2; j >= 0; j--)
            {
               if(highs[j] >= midpoint)
               {
                  filled = true;
                  break;
               }
            }

            if(!filled && midpoint > currentPrice) // FVG above price (bearish target)
            {
               SZone zone;
               zone.name = StringFormat("FVG_Bear_%d_%d", tf, i);
               zone.type = 3; // FVG Bearish
               zone.price_high = lows[i+1];
               zone.price_low = highs[i-1];
               zone.time_start = times[i];
               zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
               zone.touches = 0;
               zone.timeframe = tf;
               zone.is_valid = true;
               zone.is_filled = false;

               AddZone(zone);
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Detect Order Blocks                                               |
//+------------------------------------------------------------------+
void DetectOrderBlocks(const double &highs[], const double &lows[],
                       const double &opens[], const double &closes[],
                       const datetime &times[], int count, ENUM_TIMEFRAMES tf)
{
   //--- Calculate average range
   double sumRange = 0;
   for(int i = 0; i < count; i++)
      sumRange += highs[i] - lows[i];
   double avgRange = sumRange / count;
   double impulseThreshold = avgRange * InpImpulseMultiplier;

   double currentPrice = SymbolInfoDouble(_Symbol, SYMBOL_BID);

   for(int i = 2; i < count - 2; i++)
   {
      bool isBearish = closes[i] < opens[i];
      bool isBullish = closes[i] > opens[i];

      //--- Bullish Order Block: last bearish candle before strong bullish move
      if(isBearish)
      {
         //--- Check for strong bullish move after
         double moveUp = highs[i-1] - lows[i];
         if(moveUp > impulseThreshold)
         {
            //--- Verify it's still valid (not mitigated)
            bool mitigated = false;
            for(int j = i - 2; j >= 0; j--)
            {
               if(lows[j] < lows[i])
               {
                  mitigated = true;
                  break;
               }
            }

            if(!mitigated && highs[i] < currentPrice) // OB below price
            {
               SZone zone;
               zone.name = StringFormat("OB_Bull_%d_%d", tf, i);
               zone.type = 4; // Order Block Bullish
               zone.price_high = highs[i];
               zone.price_low = lows[i];
               zone.time_start = times[i];
               zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
               zone.touches = 0;
               zone.timeframe = tf;
               zone.is_valid = true;
               zone.is_filled = false;

               AddZone(zone);
            }
         }
      }

      //--- Bearish Order Block: last bullish candle before strong bearish move
      if(isBullish)
      {
         //--- Check for strong bearish move after
         double moveDown = highs[i] - lows[i-1];
         if(moveDown > impulseThreshold)
         {
            //--- Verify it's still valid (not mitigated)
            bool mitigated = false;
            for(int j = i - 2; j >= 0; j--)
            {
               if(highs[j] > highs[i])
               {
                  mitigated = true;
                  break;
               }
            }

            if(!mitigated && lows[i] > currentPrice) // OB above price
            {
               SZone zone;
               zone.name = StringFormat("OB_Bear_%d_%d", tf, i);
               zone.type = 5; // Order Block Bearish
               zone.price_high = highs[i];
               zone.price_low = lows[i];
               zone.time_start = times[i];
               zone.time_end = TimeCurrent() + PeriodSeconds(tf) * 20;
               zone.touches = 0;
               zone.timeframe = tf;
               zone.is_valid = true;
               zone.is_filled = false;

               AddZone(zone);
            }
         }
      }
   }
}

//+------------------------------------------------------------------+
//| Add zone to array                                                 |
//+------------------------------------------------------------------+
void AddZone(SZone &zone)
{
   int size = ArraySize(g_zones);
   ArrayResize(g_zones, size + 1);
   g_zones[size] = zone;
   g_zone_count++;
}

//+------------------------------------------------------------------+
//| Calculate zone bias                                               |
//+------------------------------------------------------------------+
double CalculateZoneBias(double currentPrice)
{
   int zonesAbove = 0;
   int zonesBelow = 0;
   double weightAbove = 0;
   double weightBelow = 0;

   for(int i = 0; i < g_zone_count; i++)
   {
      if(!g_zones[i].is_valid)
         continue;

      double midpoint = (g_zones[i].price_high + g_zones[i].price_low) / 2;
      double weight = (g_zones[i].timeframe == PERIOD_H4) ? 1.5 : 1.0;

      //--- Liquidity zones are stronger targets
      if(g_zones[i].type == 0 || g_zones[i].type == 1)
         weight *= 1.5;

      //--- Add touches bonus
      weight += g_zones[i].touches * 0.2;

      if(midpoint > currentPrice)
      {
         zonesAbove++;
         weightAbove += weight;
      }
      else
      {
         zonesBelow++;
         weightBelow += weight;
      }
   }

   //--- Calculate bias: positive = more targets above (bullish bias)
   //                    negative = more targets below (bearish bias)
   double totalWeight = weightAbove + weightBelow;
   if(totalWeight == 0)
      return 0;

   double bias = (weightAbove - weightBelow) / totalWeight;
   return NormalizeDouble(bias, 2);
}

//+------------------------------------------------------------------+
//| Update indicator buffers                                          |
//+------------------------------------------------------------------+
void UpdateBuffers(int bar, double currentPrice)
{
   //--- Initialize
   BufLiquidityHighH1[bar] = 0;
   BufLiquidityLowH1[bar] = 0;
   BufFVGHighH1[bar] = 0;
   BufFVGLowH1[bar] = 0;
   BufOBHighH1[bar] = 0;
   BufOBLowH1[bar] = 0;
   BufZoneBias[bar] = 0;
   BufZoneCount[bar] = g_zone_count;

   double nearestLiqHigh = DBL_MAX;
   double nearestLiqLow = 0;
   double nearestFVGHigh = DBL_MAX;
   double nearestFVGLow = 0;
   double nearestOBHigh = DBL_MAX;
   double nearestOBLow = 0;

   for(int i = 0; i < g_zone_count; i++)
   {
      if(!g_zones[i].is_valid)
         continue;

      double mid = (g_zones[i].price_high + g_zones[i].price_low) / 2;

      switch(g_zones[i].type)
      {
         case 0: // Liquidity High (above price)
            if(mid > currentPrice && mid < nearestLiqHigh)
               nearestLiqHigh = mid;
            break;
         case 1: // Liquidity Low (below price)
            if(mid < currentPrice && mid > nearestLiqLow)
               nearestLiqLow = mid;
            break;
         case 2: // FVG Bullish (below price - support)
            if(mid < currentPrice && mid > nearestFVGLow)
               nearestFVGLow = mid;
            break;
         case 3: // FVG Bearish (above price - resistance)
            if(mid > currentPrice && mid < nearestFVGHigh)
               nearestFVGHigh = mid;
            break;
         case 4: // OB Bullish (below price)
            if(mid < currentPrice && mid > nearestOBLow)
               nearestOBLow = mid;
            break;
         case 5: // OB Bearish (above price)
            if(mid > currentPrice && mid < nearestOBHigh)
               nearestOBHigh = mid;
            break;
      }
   }

   //--- Set buffers
   if(nearestLiqHigh < DBL_MAX)
      BufLiquidityHighH1[bar] = nearestLiqHigh;
   if(nearestLiqLow > 0)
      BufLiquidityLowH1[bar] = nearestLiqLow;
   if(nearestFVGHigh < DBL_MAX)
      BufFVGHighH1[bar] = nearestFVGHigh;
   if(nearestFVGLow > 0)
      BufFVGLowH1[bar] = nearestFVGLow;
   if(nearestOBHigh < DBL_MAX)
      BufOBHighH1[bar] = nearestOBHigh;
   if(nearestOBLow > 0)
      BufOBLowH1[bar] = nearestOBLow;

   BufZoneBias[bar] = CalculateZoneBias(currentPrice);
}

//+------------------------------------------------------------------+
//| Draw all zones as rectangles                                      |
//+------------------------------------------------------------------+
void DrawAllZones()
{
   for(int i = 0; i < g_zone_count; i++)
   {
      if(!g_zones[i].is_valid)
         continue;

      DrawZoneBox(g_zones[i], i);
   }

   ChartRedraw(0);
}

//+------------------------------------------------------------------+
//| Draw single zone box                                              |
//+------------------------------------------------------------------+
void DrawZoneBox(SZone &zone, int index)
{
   string objName = g_prefix + zone.name;
   color zoneColor;
   string label;

   //--- Set color based on type
   switch(zone.type)
   {
      case 0: zoneColor = InpLiquidityHighColor; label = "LIQ HIGH"; break;
      case 1: zoneColor = InpLiquidityLowColor; label = "LIQ LOW"; break;
      case 2: zoneColor = InpFVGBullishColor; label = "FVG BULL"; break;
      case 3: zoneColor = InpFVGBearishColor; label = "FVG BEAR"; break;
      case 4: zoneColor = InpOBBullishColor; label = "OB BULL"; break;
      case 5: zoneColor = InpOBBearishColor; label = "OB BEAR"; break;
      default: zoneColor = clrGray; label = "ZONE"; break;
   }

   //--- Add timeframe to label
   if(zone.timeframe == PERIOD_H4)
      label = "H4 " + label;
   else
      label = "H1 " + label;

   //--- Create rectangle
   if(ObjectCreate(0, objName, OBJ_RECTANGLE, 0,
                   zone.time_start, zone.price_high,
                   zone.time_end, zone.price_low))
   {
      ObjectSetInteger(0, objName, OBJPROP_COLOR, zoneColor);
      ObjectSetInteger(0, objName, OBJPROP_STYLE, STYLE_SOLID);
      ObjectSetInteger(0, objName, OBJPROP_WIDTH, 1);
      ObjectSetInteger(0, objName, OBJPROP_FILL, true);
      ObjectSetInteger(0, objName, OBJPROP_BACK, true);

      //--- Set transparency (convert 0-100 to 0-255)
      uchar alpha = (uchar)(255 * (100 - InpBoxTransparency) / 100);
      color fillColor = (color)((alpha << 24) | (zoneColor & 0xFFFFFF));
      ObjectSetInteger(0, objName, OBJPROP_COLOR, fillColor);

      ObjectSetInteger(0, objName, OBJPROP_SELECTABLE, false);
      ObjectSetInteger(0, objName, OBJPROP_HIDDEN, true);
   }

   //--- Create label
   if(InpShowLabels)
   {
      string labelName = objName + "_lbl";
      double labelPrice = zone.price_high;

      if(ObjectCreate(0, labelName, OBJ_TEXT, 0, zone.time_start, labelPrice))
      {
         ObjectSetString(0, labelName, OBJPROP_TEXT, label);
         ObjectSetInteger(0, labelName, OBJPROP_COLOR, zoneColor);
         ObjectSetInteger(0, labelName, OBJPROP_FONTSIZE, 8);
         ObjectSetString(0, labelName, OBJPROP_FONT, "Arial");
         ObjectSetInteger(0, labelName, OBJPROP_ANCHOR, ANCHOR_LEFT_LOWER);
         ObjectSetInteger(0, labelName, OBJPROP_SELECTABLE, false);
         ObjectSetInteger(0, labelName, OBJPROP_HIDDEN, true);
      }
   }
}

//+------------------------------------------------------------------+
