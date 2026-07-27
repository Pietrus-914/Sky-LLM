# SkyTower-AI Installation Guide

## Quick Setup (New Computer)

### Step 1: Copy Files

Copy the entire `SkyTowerAI` folder to the new computer:
```
C:\Users\YOUR_NAME\Documents\SkyTowerAI\
```

### Step 2: Install Python Dependencies

```bash
cd C:\Users\YOUR_NAME\Documents\SkyTowerAI\python
pip install -r requirements.txt
```

**Required packages:**
- flask
- flask-cors
- requests
- pandas
- loguru
- openai (for OpenRouter API)
- python-dotenv

Or install manually:
```bash
pip install flask flask-cors requests pandas loguru openai python-dotenv
```

### Step 3: Configure API Key

Create `.env` file in `SkyTowerAI/python/`:
```
OPENROUTER_API_KEY=sk-or-v1-your-key-here
```

Or edit `config.py` directly (line 12).

### Step 4: Setup MT5

1. Copy `mt5/*.mq5` and `mt5/*.mqh` files to:
   ```
   C:\Users\YOUR_NAME\AppData\Roaming\MetaQuotes\Terminal\YOUR_TERMINAL_ID\MQL5\Experts\
   ```

2. Open MetaEditor (F4 in MT5)

3. Compile files:
   - `SkyTowerAI_EA.mq5`
   - `SkyTowerAI_Zones.mqh`
   - `SkyTower_Zones.mq5` (indicator)

4. Allow WebRequest in MT5:
   - Tools → Options → Expert Advisors
   - Check "Allow WebRequest for listed URL"
   - Add: `http://127.0.0.1:5555`

### Step 5: Start Server

```bash
cd C:\Users\YOUR_NAME\Documents\SkyTowerAI\python
python server.py
```

Or double-click `start_server.bat`

### Step 6: Attach EA

1. Open chart (any pair, M1 timeframe recommended)
2. Drag `SkyTowerAI_EA` onto chart
3. Enable "Allow Algo Trading"
4. Verify connection in Experts tab

## Verification

Test server is running:
```bash
curl http://127.0.0.1:5555/health
```

Should return:
```json
{"status":"ok","timestamp":"...","version":"4.0.0"}
```

## File Structure

```
SkyTowerAI/
├── python/
│   ├── server.py           # Main server (run this!)
│   ├── config.py           # All settings
│   ├── llm_decision_engine.py
│   ├── calendar_fetcher.py
│   ├── cot_analyzer.py
│   ├── sentiment_analyzer.py
│   ├── zone_analyzer.py
│   └── requirements.txt
├── mt5/
│   ├── SkyTowerAI_EA.mq5   # Expert Advisor
│   ├── SkyTowerAI_Zones.mqh # Include file
│   └── SkyTower_Zones.mq5  # Zone indicator
└── config/
    └── pairs.json
```

## Troubleshooting

### "WebRequest failed"
- Add URL to MT5 allowed list (see Step 4)
- Check firewall isn't blocking port 5555

### "Could not connect to server"
- Make sure `python server.py` is running
- Check if port 5555 is free: `netstat -an | findstr 5555`

### "OpenRouter error 402"
- Recharge OpenRouter credits at https://openrouter.ai/credits

### EA not trading
- Check Experts tab for errors
- Verify Auto Trading is enabled (green button in toolbar)
- Check that server returns signals: `curl http://127.0.0.1:5555/api/signal`
