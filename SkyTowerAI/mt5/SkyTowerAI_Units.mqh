//+------------------------------------------------------------------+
//| Shared broker-unit helpers for SkyTowerAI                         |
//+------------------------------------------------------------------+
#ifndef SKYTOWERAI_UNITS_MQH
#define SKYTOWERAI_UNITS_MQH

// Optional pip-size override in PRICE units (0 = auto forex rule below).
// Set once from the EA input in OnInit (InpPipSizeOverride) so a non-forex
// CFD chart (XAUUSD 0.10, GER40/US500 1.0) counts every downstream pip
// quantity — spread gates, SL/TP distances, BE detection, reports — in the
// instrument's own unit instead of point*10.
double g_skyPipSizeOverride = 0.0;

double SkyPipSize(string symbol)
{
   if(g_skyPipSizeOverride > 0.0)
      return g_skyPipSizeOverride;
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(point <= 0 || !MathIsValidNumber(point))
      return 0.0;
   return point * ((digits == 3 || digits == 5) ? 10.0 : 1.0);
}

double SkyPipsToPrice(string symbol, double pips)
{
   double pip = SkyPipSize(symbol);
   if(pip <= 0 || !MathIsValidNumber(pips))
      return 0.0;
   return pips * pip;
}

double SkyPriceToPips(string symbol, double priceDistance)
{
   double pip = SkyPipSize(symbol);
   if(pip <= 0 || !MathIsValidNumber(priceDistance))
      return 0.0;
   return priceDistance / pip;
}

double SkySpreadPips(string symbol)
{
   double pip = SkyPipSize(symbol);
   double ask = SymbolInfoDouble(symbol, SYMBOL_ASK);
   double bid = SymbolInfoDouble(symbol, SYMBOL_BID);
   if(pip <= 0 || ask <= 0 || bid <= 0 || ask < bid)
      return -1.0;
   return (ask - bid) / pip;
}

// Normalize a stop-order price to the broker tick grid: BUY rounds down,
// SELL rounds up. For a protective stop (below/above entry) this never moves
// it closer to the entry; for a take-profit the SAME rounding moves it
// TOWARD the entry, i.e. the target only gets easier to reach — both are the
// conservative direction. Do not "fix" the rounding for one use case.
double SkyNormalizeStopPrice(string symbol, double price, string direction)
{
   double tickSize = SymbolInfoDouble(symbol, SYMBOL_TRADE_TICK_SIZE);
   int digits = (int)SymbolInfoInteger(symbol, SYMBOL_DIGITS);
   if(!MathIsValidNumber(price) || price <= 0
      || !MathIsValidNumber(tickSize) || tickSize <= 0)
      return 0.0;

   double ticks = price / tickSize;
   double normalized = (direction == "BUY")
      ? MathFloor(ticks + 1e-10) * tickSize
      : MathCeil(ticks - 1e-10) * tickSize;
   return NormalizeDouble(normalized, digits);
}

int SkyVolumeDigits(double step)
{
   if(step <= 0 || !MathIsValidNumber(step))
      return 0;
   for(int digits = 0; digits <= 8; digits++)
   {
      if(MathAbs(NormalizeDouble(step, digits) - step) < 1e-10)
         return digits;
   }
   return 8;
}

// Normalize downward so rounding can never exceed the requested risk.
// A value below the broker minimum stays zero instead of being promoted.
double SkyNormalizeVolumeDown(string symbol, double requested)
{
   double step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   double minVolume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double maxVolume = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   if(!MathIsValidNumber(requested) || requested <= 0
      || step <= 0 || minVolume <= 0 || maxVolume <= 0)
      return 0.0;

   double epsilon = step * 1e-8;
   if(requested < minVolume - epsilon)
      return 0.0;

   double capped = MathMin(requested, maxVolume);
   double normalized = MathFloor((capped + epsilon) / step) * step;
   normalized = NormalizeDouble(normalized, SkyVolumeDigits(step));
   if(normalized < minVolume - epsilon)
      return 0.0;
   return normalized;
}

#endif
