---
name: mql5-indicator-patterns
description: MQL5 indicator patterns (manual load only)
allowed-tools: Read, Write, Edit, Grep, Glob, Bash
autoActivate: false
---

# MQL5 Visual Indicator Patterns

**Loaded via:** `/sky_tower skill indicator`

Essential patterns for MetaTrader 5 custom indicator development.

## This Skill Covers

- Display scaling for small values
- Buffer architecture (visible + hidden)
- New bar detection and recalculation
- Warmup period handling
- Array direction configuration

## Core Patterns

### 1. Visual Display Configuration

**Problem:** Indicators with values below 1.0 show blank windows.

**Solution:** Set explicit scale boundaries:
```mql5
// In OnInit()
IndicatorSetInteger(INDICATOR_DIGITS, 4);
IndicatorSetDouble(INDICATOR_MINIMUM, 0.0);
IndicatorSetDouble(INDICATOR_MAXIMUM, 1.0);
IndicatorSetString(INDICATOR_SHORTNAME, "MyIndicator");
```

### 2. Buffer Architecture

**Pattern:** Separate visible and hidden buffers:
```mql5
// Visible buffer for display
SetIndexBuffer(0, BufVisible, INDICATOR_DATA);
PlotIndexSetInteger(0, PLOT_DRAW_TYPE, DRAW_LINE);
PlotIndexSetInteger(0, PLOT_LINE_COLOR, clrBlue);

// Hidden buffer for calculations
SetIndexBuffer(1, BufHidden, INDICATOR_CALCULATIONS);
```

### 3. New Bar Detection

**Pattern:** Prevent calculation drift with static variables:
```mql5
int OnCalculate(const int rates_total, const int prev_calculated, ...)
{
   static datetime lastBarTime = 0;
   datetime currentBarTime = iTime(_Symbol, PERIOD_CURRENT, 0);

   bool isNewBar = (currentBarTime != lastBarTime);
   if(isNewBar)
   {
      lastBarTime = currentBarTime;
      // Recalculate from beginning or specific point
   }

   // Calculate only new bars
   int start = (prev_calculated == 0) ? 0 : prev_calculated - 1;
   for(int i = start; i < rates_total; i++)
   {
      // Your calculation logic
   }

   return rates_total;
}
```

### 4. Warmup Handling

**Pattern:** Proper initialization before display:
```mql5
input int InpPeriod = 14;  // Indicator period

int OnCalculate(...)
{
   // Warmup: need InpPeriod bars before valid output
   int warmup = InpPeriod;

   if(rates_total < warmup)
      return 0;

   int start = (prev_calculated == 0) ? warmup : prev_calculated - 1;

   for(int i = start; i < rates_total; i++)
   {
      // Calculate with full warmup data available
      Buffer[i] = CalculateValue(i, close);
   }

   return rates_total;
}
```

### 5. Array Direction

**Pattern:** Configure arrays for forward indexing:
```mql5
int OnInit()
{
   // Set as series (newest = index 0) for price arrays
   ArraySetAsSeries(close, true);
   ArraySetAsSeries(high, true);
   ArraySetAsSeries(low, true);

   // Or standard indexing (oldest = index 0) for buffers
   ArraySetAsSeries(Buffer, false);

   return INIT_SUCCEEDED;
}
```

## Common Issues & Solutions

| Issue | Cause | Solution |
|-------|-------|----------|
| Blank window | No scale set | Use `IndicatorSetDouble(INDICATOR_MINIMUM/MAXIMUM)` |
| Rolling drift | No bar tracking | Add static `lastBarTime` check |
| Plot misalignment | Wrong warmup | Calculate correct warmup period |
| Wrong values | Array direction | Check `ArraySetAsSeries()` settings |

## Template: Complete Indicator

```mql5
#property indicator_separate_window
#property indicator_buffers 2
#property indicator_plots   1

#property indicator_label1  "Signal"
#property indicator_type1   DRAW_LINE
#property indicator_color1  clrDodgerBlue
#property indicator_style1  STYLE_SOLID
#property indicator_width1  2

input int InpPeriod = 14;

double SignalBuffer[];
double CalcBuffer[];

int OnInit()
{
   SetIndexBuffer(0, SignalBuffer, INDICATOR_DATA);
   SetIndexBuffer(1, CalcBuffer, INDICATOR_CALCULATIONS);

   IndicatorSetInteger(INDICATOR_DIGITS, 4);
   IndicatorSetDouble(INDICATOR_MINIMUM, 0.0);
   IndicatorSetDouble(INDICATOR_MAXIMUM, 100.0);
   IndicatorSetString(INDICATOR_SHORTNAME, "MyIndicator(" + IntegerToString(InpPeriod) + ")");

   return INIT_SUCCEEDED;
}

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
   if(rates_total < InpPeriod)
      return 0;

   int start = (prev_calculated == 0) ? InpPeriod : prev_calculated - 1;

   for(int i = start; i < rates_total; i++)
   {
      // Your calculation
      SignalBuffer[i] = 50.0; // Example
   }

   return rates_total;
}
```

## Project Context

This skill is part of SkyTower-AI project. The Expert Advisor is located at:
`C:\Users\pietr\Documents\Sky tower\SkyTowerAI\mt5\SkyTowerAI_EA.mq5`

For SkyTower-specific MQ5 questions, also read:
- `SkyTowerAI/AGENTS.md` - Project context
- `SkyTowerAI/DOCUMENTATION.md` - Full documentation
