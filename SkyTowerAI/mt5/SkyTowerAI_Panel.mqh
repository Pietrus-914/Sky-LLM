//+------------------------------------------------------------------+
//|                                           SkyTowerAI_Panel.mqh   |
//|                                    SkyTower-AI Trading System    |
//|                              Visual Control Panel for EA         |
//+------------------------------------------------------------------+
#property copyright "SkyTower-AI"

#include <Controls\Dialog.mqh>
#include <Controls\Button.mqh>
#include <Controls\Label.mqh>

//+------------------------------------------------------------------+
//| Trading Mode Enum                                                 |
//+------------------------------------------------------------------+
enum ENUM_TRADE_MODE
{
   MODE_LLM_AUTO,      // LLM Auto - AI decides everything
   MODE_MANUAL,        // Manual - You set direction
   MODE_CONFIRM        // Confirm - LLM suggests, you approve
};

//+------------------------------------------------------------------+
//| Panel Constants                                                   |
//+------------------------------------------------------------------+
#define PANEL_WIDTH     280
#define PANEL_HEIGHT    340
#define PANEL_X         20
#define PANEL_Y         50
#define BUTTON_HEIGHT   25
#define LABEL_HEIGHT    18
#define MARGIN          8

//+------------------------------------------------------------------+
//| SkyTower Control Panel Class                                      |
//+------------------------------------------------------------------+
class CSkyTowerPanel : public CAppDialog
{
private:
   // Labels
   CLabel            m_lblTitle;
   CLabel            m_lblMode;
   CLabel            m_lblModeValue;
   CLabel            m_lblStatus;
   CLabel            m_lblEvent;
   CLabel            m_lblDirection;
   CLabel            m_lblConfidence;
   CLabel            m_lblPair;
   CLabel            m_lblCountdown;

   // Status labels (values)
   CLabel            m_valStatus;
   CLabel            m_valEvent;
   CLabel            m_valDirection;
   CLabel            m_valConfidence;
   CLabel            m_valPair;
   CLabel            m_valCountdown;

   // Controls
   CButton           m_btnMode;      // Toggle mode button
   CButton           m_btnBuy;
   CButton           m_btnSell;
   CButton           m_btnSkip;
   CButton           m_btnRefresh;

   // State
   ENUM_TRADE_MODE   m_tradeMode;
   string            m_manualDirection;
   bool              m_signalApproved;

public:
                     CSkyTowerPanel();
                    ~CSkyTowerPanel();

   // Create/Destroy
   bool              Create(const long chart, const string name, const int subwin, const int x, const int y);
   virtual bool      OnEvent(const int id, const long &lparam, const double &dparam, const string &sparam);

   // Getters
   ENUM_TRADE_MODE   GetTradeMode()        { return m_tradeMode; }
   string            GetManualDirection()  { return m_manualDirection; }
   bool              IsSignalApproved()    { return m_signalApproved; }

   // Setters / Updates
   void              SetStatus(string status, color clr = clrWhite);
   void              SetEvent(string eventName);
   void              SetDirection(string dir, double confidence);
   void              SetPair(string pair);
   void              SetCountdown(int seconds);
   void              ResetManualDirection() { m_manualDirection = ""; m_signalApproved = false; }
   void              ClearSignal();

private:
   // Event handlers
   void              OnClickMode();
   void              OnClickBuy();
   void              OnClickSell();
   void              OnClickSkip();
   void              OnClickRefresh();

   // Helpers
   bool              CreateLabel(CLabel &lbl, const string name, const string text, int x, int y, int width, color clr = clrWhite);
   bool              CreateButton(CButton &btn, const string name, const string text, int x, int y, int width, int height, color clr);
   void              UpdateModeDisplay();
};

//+------------------------------------------------------------------+
//| Constructor                                                       |
//+------------------------------------------------------------------+
CSkyTowerPanel::CSkyTowerPanel()
{
   m_tradeMode = MODE_LLM_AUTO;
   m_manualDirection = "";
   m_signalApproved = false;
}

//+------------------------------------------------------------------+
//| Destructor                                                        |
//+------------------------------------------------------------------+
CSkyTowerPanel::~CSkyTowerPanel()
{
}

//+------------------------------------------------------------------+
//| Create Panel                                                      |
//+------------------------------------------------------------------+
bool CSkyTowerPanel::Create(const long chart, const string name, const int subwin, const int x, const int y)
{
   //--- Create dialog
   if(!CAppDialog::Create(chart, name, subwin, x, y, x + PANEL_WIDTH, y + PANEL_HEIGHT))
      return false;

   int yPos = MARGIN;
   int colWidth = (PANEL_WIDTH - 3 * MARGIN) / 2;

   //--- Title
   if(!CreateLabel(m_lblTitle, "Title", "SkyTower-AI v5.0", MARGIN, yPos, PANEL_WIDTH - 2*MARGIN, clrDodgerBlue))
      return false;
   yPos += LABEL_HEIGHT + 5;

   //--- Mode row with toggle button
   if(!CreateLabel(m_lblMode, "ModeLabel", "Mode:", MARGIN, yPos, 50, clrGray))
      return false;

   if(!CreateButton(m_btnMode, "ModeBtn", "LLM Auto", MARGIN + 55, yPos - 3, 120, BUTTON_HEIGHT, clrDodgerBlue))
      return false;

   yPos += BUTTON_HEIGHT + 10;

   //--- Separator line
   yPos += 3;

   //--- Status row
   if(!CreateLabel(m_lblStatus, "StatusLabel", "Status:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valStatus, "StatusVal", "Waiting...", MARGIN + 75, yPos, colWidth + 50, clrYellow))
      return false;
   yPos += LABEL_HEIGHT + 3;

   //--- Event row
   if(!CreateLabel(m_lblEvent, "EventLabel", "Event:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valEvent, "EventVal", "-", MARGIN + 75, yPos, colWidth + 50, clrWhite))
      return false;
   yPos += LABEL_HEIGHT + 3;

   //--- Pair row
   if(!CreateLabel(m_lblPair, "PairLabel", "Pair:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valPair, "PairVal", "-", MARGIN + 75, yPos, colWidth, clrWhite))
      return false;
   yPos += LABEL_HEIGHT + 3;

   //--- Direction row
   if(!CreateLabel(m_lblDirection, "DirLabel", "Direction:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valDirection, "DirVal", "-", MARGIN + 75, yPos, colWidth, clrWhite))
      return false;
   yPos += LABEL_HEIGHT + 3;

   //--- Confidence row
   if(!CreateLabel(m_lblConfidence, "ConfLabel", "Confidence:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valConfidence, "ConfVal", "-", MARGIN + 75, yPos, colWidth, clrWhite))
      return false;
   yPos += LABEL_HEIGHT + 3;

   //--- Countdown row
   if(!CreateLabel(m_lblCountdown, "CountLabel", "Countdown:", MARGIN, yPos, 70, clrGray))
      return false;
   if(!CreateLabel(m_valCountdown, "CountVal", "-", MARGIN + 75, yPos, colWidth, clrLime))
      return false;
   yPos += LABEL_HEIGHT + 15;

   //--- Manual Buttons (for Manual and Confirm modes)
   int btnWidth = (PANEL_WIDTH - 4 * MARGIN) / 3;

   if(!CreateButton(m_btnBuy, "BuyBtn", "BUY", MARGIN, yPos, btnWidth, BUTTON_HEIGHT + 5, clrGreen))
      return false;
   if(!CreateButton(m_btnSell, "SellBtn", "SELL", MARGIN + btnWidth + MARGIN, yPos, btnWidth, BUTTON_HEIGHT + 5, clrRed))
      return false;
   if(!CreateButton(m_btnSkip, "SkipBtn", "SKIP", MARGIN + 2*(btnWidth + MARGIN), yPos, btnWidth, BUTTON_HEIGHT + 5, clrGray))
      return false;
   yPos += BUTTON_HEIGHT + 15;

   //--- Refresh button
   if(!CreateButton(m_btnRefresh, "RefreshBtn", "Refresh Signal", MARGIN, yPos, PANEL_WIDTH - 2*MARGIN, BUTTON_HEIGHT, clrDodgerBlue))
      return false;

   //--- Initial state - hide manual buttons in LLM Auto mode
   m_btnBuy.Hide();
   m_btnSell.Hide();
   m_btnSkip.Hide();

   return true;
}

//+------------------------------------------------------------------+
//| Create Label Helper                                               |
//+------------------------------------------------------------------+
bool CSkyTowerPanel::CreateLabel(CLabel &lbl, const string name, const string text, int x, int y, int width, color clr)
{
   if(!lbl.Create(m_chart_id, m_name + name, m_subwin, x, y, x + width, y + LABEL_HEIGHT))
      return false;
   if(!lbl.Text(text))
      return false;
   if(!lbl.Color(clr))
      return false;
   if(!Add(lbl))
      return false;
   return true;
}

//+------------------------------------------------------------------+
//| Create Button Helper                                              |
//+------------------------------------------------------------------+
bool CSkyTowerPanel::CreateButton(CButton &btn, const string name, const string text, int x, int y, int width, int height, color clr)
{
   if(!btn.Create(m_chart_id, m_name + name, m_subwin, x, y, x + width, y + height))
      return false;
   if(!btn.Text(text))
      return false;
   if(!btn.ColorBackground(clr))
      return false;
   if(!Add(btn))
      return false;
   return true;
}

//+------------------------------------------------------------------+
//| Event Handler                                                     |
//+------------------------------------------------------------------+
EVENT_MAP_BEGIN(CSkyTowerPanel)
   ON_EVENT(ON_CLICK, m_btnMode, OnClickMode)
   ON_EVENT(ON_CLICK, m_btnBuy, OnClickBuy)
   ON_EVENT(ON_CLICK, m_btnSell, OnClickSell)
   ON_EVENT(ON_CLICK, m_btnSkip, OnClickSkip)
   ON_EVENT(ON_CLICK, m_btnRefresh, OnClickRefresh)
EVENT_MAP_END(CAppDialog)

//+------------------------------------------------------------------+
//| Mode Button Click - Toggle between modes                          |
//+------------------------------------------------------------------+
void CSkyTowerPanel::OnClickMode()
{
   // Cycle through modes: LLM_AUTO -> MANUAL -> CONFIRM -> LLM_AUTO
   switch(m_tradeMode)
   {
      case MODE_LLM_AUTO:
         m_tradeMode = MODE_MANUAL;
         break;
      case MODE_MANUAL:
         m_tradeMode = MODE_CONFIRM;
         break;
      case MODE_CONFIRM:
         m_tradeMode = MODE_LLM_AUTO;
         break;
   }

   UpdateModeDisplay();
   m_manualDirection = "";
   m_signalApproved = false;
}

//+------------------------------------------------------------------+
//| Update mode button display                                        |
//+------------------------------------------------------------------+
void CSkyTowerPanel::UpdateModeDisplay()
{
   switch(m_tradeMode)
   {
      case MODE_LLM_AUTO:
         m_btnMode.Text("LLM Auto");
         m_btnMode.ColorBackground(clrDodgerBlue);
         m_btnBuy.Hide();
         m_btnSell.Hide();
         m_btnSkip.Hide();
         Print("PANEL: Mode = LLM Auto");
         break;

      case MODE_MANUAL:
         m_btnMode.Text("Manual");
         m_btnMode.ColorBackground(clrOrange);
         m_btnBuy.Show();
         m_btnSell.Show();
         m_btnSkip.Show();
         Print("PANEL: Mode = Manual Override");
         break;

      case MODE_CONFIRM:
         m_btnMode.Text("LLM+Confirm");
         m_btnMode.ColorBackground(clrGold);
         m_btnBuy.Show();
         m_btnSell.Show();
         m_btnSkip.Show();
         Print("PANEL: Mode = LLM + Confirm");
         break;
   }
}

//+------------------------------------------------------------------+
//| Buy Button Click                                                  |
//+------------------------------------------------------------------+
void CSkyTowerPanel::OnClickBuy()
{
   m_manualDirection = "BUY";
   m_signalApproved = true;
   SetDirection("BUY (Manual)", 1.0);
   Print("PANEL: Manual BUY selected");
}

//+------------------------------------------------------------------+
//| Sell Button Click                                                 |
//+------------------------------------------------------------------+
void CSkyTowerPanel::OnClickSell()
{
   m_manualDirection = "SELL";
   m_signalApproved = true;
   SetDirection("SELL (Manual)", 1.0);
   Print("PANEL: Manual SELL selected");
}

//+------------------------------------------------------------------+
//| Skip Button Click                                                 |
//+------------------------------------------------------------------+
void CSkyTowerPanel::OnClickSkip()
{
   m_manualDirection = "SKIP";
   m_signalApproved = true;
   SetDirection("SKIP", 0.0);
   Print("PANEL: Trade skipped by user");
}

//+------------------------------------------------------------------+
//| Refresh Button Click                                              |
//+------------------------------------------------------------------+
void CSkyTowerPanel::OnClickRefresh()
{
   Print("PANEL: Refreshing signal from server...");
   SetStatus("Refreshing...", clrOrange);
   // EA will handle the actual refresh
}

//+------------------------------------------------------------------+
//| Update Status                                                     |
//+------------------------------------------------------------------+
void CSkyTowerPanel::SetStatus(string status, color clr)
{
   m_valStatus.Text(status);
   m_valStatus.Color(clr);
}

//+------------------------------------------------------------------+
//| Update Event                                                      |
//+------------------------------------------------------------------+
void CSkyTowerPanel::SetEvent(string eventName)
{
   // Truncate if too long
   if(StringLen(eventName) > 25)
      eventName = StringSubstr(eventName, 0, 22) + "...";
   m_valEvent.Text(eventName);
}

//+------------------------------------------------------------------+
//| Update Direction and Confidence                                   |
//+------------------------------------------------------------------+
void CSkyTowerPanel::SetDirection(string dir, double confidence)
{
   m_valDirection.Text(dir);

   // Color based on direction
   if(StringFind(dir, "BUY") >= 0)
      m_valDirection.Color(clrLime);
   else if(StringFind(dir, "SELL") >= 0)
      m_valDirection.Color(clrRed);
   else
      m_valDirection.Color(clrGray);

   // Confidence
   m_valConfidence.Text(DoubleToString(confidence * 100, 0) + "%");

   if(confidence >= 0.7)
      m_valConfidence.Color(clrLime);
   else if(confidence >= 0.5)
      m_valConfidence.Color(clrYellow);
   else
      m_valConfidence.Color(clrOrange);
}

//+------------------------------------------------------------------+
//| Update Pair                                                       |
//+------------------------------------------------------------------+
void CSkyTowerPanel::SetPair(string pair)
{
   m_valPair.Text(pair);
}

//+------------------------------------------------------------------+
//| Update Countdown                                                  |
//+------------------------------------------------------------------+
void CSkyTowerPanel::SetCountdown(int seconds)
{
   if(seconds < 0)
   {
      m_valCountdown.Text("Event passed");
      m_valCountdown.Color(clrGray);
   }
   else if(seconds <= 15)
   {
      m_valCountdown.Text("TRADING NOW!");
      m_valCountdown.Color(clrLime);
   }
   else if(seconds <= 60)
   {
      m_valCountdown.Text(IntegerToString(seconds) + "s - READY");
      m_valCountdown.Color(clrYellow);
   }
   else
   {
      int mins = seconds / 60;
      int secs = seconds % 60;
      m_valCountdown.Text(IntegerToString(mins) + "m " + IntegerToString(secs) + "s");
      m_valCountdown.Color(clrWhite);
   }
}

//+------------------------------------------------------------------+
//| Clear Signal                                                      |
//+------------------------------------------------------------------+
void CSkyTowerPanel::ClearSignal()
{
   m_valEvent.Text("-");
   m_valPair.Text("-");
   m_valDirection.Text("-");
   m_valDirection.Color(clrWhite);
   m_valConfidence.Text("-");
   m_valConfidence.Color(clrWhite);
   m_valCountdown.Text("-");
   m_valCountdown.Color(clrWhite);
   m_manualDirection = "";
   m_signalApproved = false;
}
//+------------------------------------------------------------------+
