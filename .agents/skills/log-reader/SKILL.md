---
name: log-reader
description: MT5 log reader (manual load only)
allowed-tools: Read, Bash, Grep
autoActivate: false
---

# MT5 Log Reader

**Loaded via:** `/sky_tower skill logs`

Automated reading of MetaTrader 5 log files to validate indicator and EA execution.

## This Skill Covers

- MT5 log file locations and formats
- Filtering for errors/warnings
- Debugging EA and indicator output
- Analyzing Print() statements

## Log File Location

**Standard MT5 installation:**
```
C:\Program Files\MetaTrader 5\MQL5\Logs\YYYYMMDD.log
```

**Purple Trading MT5 (SkyTower project):**
```
C:\Program Files\Purple Trading MT5\MQL5\Logs\YYYYMMDD.log
```

**Alternative locations:**
```
C:\Users\{USER}\AppData\Roaming\MetaQuotes\Terminal\{ID}\MQL5\Logs\
```

## Log Format

- **Encoding:** UTF-16LE
- **Separator:** Tab-delimited
- **Fields:** Timestamp | Source | Message

Example:
```
2026.01.20 10:15:32.456	SkyTowerAI_EA (NZDUSD,M1)	Signal received: BUY
2026.01.20 10:15:32.457	SkyTowerAI_EA (NZDUSD,M1)	Lot size: 0.15
2026.01.20 10:15:32.789	SkyTowerAI_EA (NZDUSD,M1)	Order opened: #12345
```

## Workflow

### Step 1: Construct Today's Log Path
```bash
# Get today's date in YYYYMMDD format
date +%Y%m%d
# Result: 20260120
```

### Step 2: Read Log File
```bash
# Read entire log (may be large)
cat "/mnt/c/Program Files/Purple Trading MT5/MQL5/Logs/20260120.log"

# Or use Read tool with encoding handling
iconv -f UTF-16LE -t UTF-8 "/path/to/log.log"
```

### Step 3: Filter Specific Content
```bash
# Find errors
grep -i "error\|warning\|failed" log.log

# Find SkyTower EA messages
grep "SkyTowerAI" log.log

# Find specific symbol
grep "NZDUSD" log.log

# Last 50 lines
tail -50 log.log
```

### Step 4: Analyze Results
Look for:
- Initialization messages (OnInit)
- Signal processing
- Order execution
- Error messages
- WebRequest responses

## Common Use Cases

### Check EA Initialization
```bash
grep "OnInit\|Initialized\|INIT" log.log
```

### Monitor Trading Signals
```bash
grep -i "signal\|buy\|sell\|skip" log.log | tail -20
```

### Find Server Communication
```bash
grep -i "WebRequest\|http\|server\|response" log.log
```

### Debug Errors
```bash
grep -i "error\|fail\|invalid" log.log -A 2 -B 2
```

### Check Order Execution
```bash
grep -i "order\|position\|ticket" log.log
```

## SkyTower-AI Specific

### Expected Log Messages

**Startup:**
```
SkyTower-AI EA Initialized
Server: 127.0.0.1:5555
Risk: 10%
Min Confidence: 0.5
Server connection OK
```

**Signal Check:**
```
Checking for signal...
Signal response: {"signal":true,"direction":"BUY",...}
```

**Trade Execution:**
```
Opening BUY position on NZDUSD
Lot size calculated: 0.15
Position opened successfully: #12345
```

**Errors to Watch:**
```
WARNING: Could not connect to SkyTower-AI server!
ERROR: Spread too high (15 pips > 10 max)
ERROR: WebRequest failed: 404
```

### Quick Debug Command
```bash
# Get last 100 SkyTower messages
grep "SkyTowerAI" "/mnt/c/Program Files/Purple Trading MT5/MQL5/Logs/$(date +%Y%m%d).log" | tail -100
```

## Security Notes

- This skill only reads logs, no network operations
- Filter sensitive data (account numbers, balances) from shared output
- Avoid exposing full file paths in reports

## Project Context

Part of SkyTower-AI project. Related files:
- `SkyTowerAI/mt5/SkyTowerAI_EA.mq5` - Expert Advisor source
- `SkyTowerAI/python/server.py` - Python server (generates signals)
