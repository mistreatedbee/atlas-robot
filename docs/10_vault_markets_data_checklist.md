# 10 — Getting XAU/USD data from Vault Markets (step by step)

Phase 2 will be built from what you collect here, so read the rules first.

**Ground rules**
- **Demo only.** Nothing here needs a live account, a deposit, or a trade on real money.
- **Never share or save passwords.** Atlas does not need your MT5 login, password, investor password or account number.
  If a screenshot shows your account number (MT5 shows it in the window title and the Navigator panel), crop it or blur it
  first.
- **Never edit exported files.** Don't open the CSVs in Excel and save them, because Excel silently changes dates and decimals.
  To look inside, open a copy in a text editor.
- Exact menu labels can differ slightly between MT5 versions. If something looks different, take a screenshot and send it.

---

## Step 1 — Ask Vault Markets support (do this first)

Send the message at the bottom of this page through Vault's contact page or live chat.

**Save their reply exactly as received** (PDF or copied text) as `support_reply_YYYYMMDD.pdf` / `.txt`.

What you need back, and why:

| # | Question | Why Atlas needs it |
|---|---|---|
| S1 | Is an MT5 **demo** available for the account type you'd trade (e.g. VAULT NO BONUS or VAULT MICRO)? | Research must happen on the same conditions you'd trade |
| S2 | Does the demo use the **same XAUUSD specification and price feed** as live? | If it differs, demo results don't transfer |
| S3 | Exact **XAUUSD symbol name** on that account type | Broker symbols vary (`XAUUSD`, `XAUUSD.m`, `GOLD`…) |
| S4 | **Commission** on XAUUSD, if any | Part of the cost of every trade |
| S5 | **Margin-call and stop-out levels** (%) | Needed for the risk rules |
| S6 | **Server time zone** and how it changes with daylight saving | Every timestamp is converted to UTC using this |
| S7 | Gold **trading hours**, the daily break, and the **holiday schedule** | To tell real data gaps from market closures |
| S8 | **Execution model** (market maker or passed to a liquidity provider) and which legal entity is your counterparty | Affects slippage, requotes and your protections |
| S9 | Confirmation of the FSCA licence (FSP number) of the entity you contract with | You can then check it yourself on the FSCA register |

---

## Step 2 — Install MT5 desktop and log in to the demo

1. Download MT5 from Vault Markets' platform page or client area. You need the **desktop** app (Windows or Mac); the web
   terminal cannot export history.
2. Log in with the **demo** credentials Vault gives you. Keep these to yourself, because Atlas never needs them.
3. Write down the **server name** shown on the login screen (File → Login to Trade Account). The server name is not secret,
   but it identifies which price feed the data came from.

## Step 3 — XAUUSD symbol specification

1. Open **Market Watch** with View → Market Watch, or Ctrl+M.
2. If XAUUSD isn't listed, right-click → **Symbols**, find it under Metals, and click **Show Symbol**.
3. Right-click **XAUUSD** → **Specification**.
4. Capture **every** field. Copying the text is best; a screenshot works too. Fields to look for:
   - Contract size, Digits, Tick size, Tick value
   - Spread (and whether it says *floating*)
   - Stops level, Freeze level
   - Minimal volume, Maximal volume, Volume step
   - Margin currency, Initial margin, Maintenance margin (plus any margin rates or hedged margin)
   - Swap type, Swap long, Swap short, 3-day swap (the weekday)
   - Trade execution mode, Filling mode
   - **Quotes** sessions and **Trades** sessions for each weekday
5. Save as `XAUUSD_spec_YYYYMMDD.txt` (copied text) or `XAUUSD_spec_YYYYMMDD.png` (screenshot).

## Step 4 — Server time check

1. Keep Market Watch visible. Its header shows the **server time**.
2. At the same moment, open <https://time.is/UTC> in a browser.
3. Take one screenshot showing both, and save it as `server_time_check_YYYYMMDD.png`.

This lets Atlas check S6 independently of what support says.

## Step 5 — One week of M1 bars (a small test file)

1. **Tools → Options → Charts** → set **Max bars in chart** to **Unlimited** → OK. Restart MT5 if it asks.
2. **View → Symbols** (Ctrl+U) → **Bars** tab.
3. Choose **XAUUSD** and timeframe **M1**.
4. Set the dates to the most recent full trading week, Monday to Friday. Click **Request**.
5. Click **Export Bars** and save it with the name from the table below.

## Step 6 — Full M1 history

1. Do Step 5 again with the start date set as early as possible, for example 10 years back. Click **Request**.
2. Note the **earliest date the server actually returns** (the first row of the list) and use that in the file name.
3. Export the file.

## Step 7 — Tick data (bid/ask), one week

1. **View → Symbols → Ticks** tab → choose **XAUUSD**.
2. Pick the option that includes **bid and ask** ticks, not only trades.
3. Choose the same week as Step 5. Click **Request**, then **Export Ticks**.
4. Optionally export a second week that includes a big US release (e.g. a CPI or jobs-report day). This shows how spreads
   behave around news.

## Step 8 (optional) — One demo trade to see real costs

This step is done by you, by hand, on the **demo** account only. Atlas does not place orders.

1. Place a **0.01** lot market buy on XAUUSD, then close it a minute later.
2. Open the **History** tab and screenshot the deal rows, showing open/close price, commission and swap. Crop out the
   account number.
3. Save as `demo_trade_history_YYYYMMDD.png`.

---

## What to export first, and how to name it

Put everything in **`data/imports/vault_markets/`**. Dates are `YYYYMMDD`. Bar and tick files use the first and last day they cover.

| Priority | File | Example name |
|---|---|---|
| 1 | Support's reply | `support_reply_20261013.pdf` |
| 2 | Symbol specification | `XAUUSD_spec_20261012.txt` (or `.png`) |
| 3 | Server time check | `server_time_check_20261012.png` |
| 4 | One week of M1 bars (Step 5) | `XAUUSD_M1_bars_20261005_20261009.csv` |
| 5 | One week of ticks (Step 7) | `XAUUSD_ticks_20261005_20261009.csv` |
| 6 | Full M1 history (Step 6) | `XAUUSD_M1_bars_20160104_20261009.csv` |
| 7 | News-week ticks (optional) | `XAUUSD_ticks_20260908_20260912.csv` |
| 8 | Demo trade history (optional) | `demo_trade_history_20261012.png` |

Items 1–5 are enough to start Phase 2. The full history (6) can follow, because it is large and slow to download.

---

## Message to send to Vault Markets support

> Subject: MT5 demo account and XAUUSD trading conditions
>
> Hello,
>
> I'm considering opening an account with Vault Markets to trade XAUUSD on MT5, and I'd like to test on a demo account
> first. Could you please help with the following?
>
> 1. Can I open an MT5 demo account for the VAULT NO BONUS or VAULT MICRO account type? If so, how do I open it?
> 2. Does the demo use the same XAUUSD contract specification, spreads and price feed as the live account of that type?
> 3. What is the exact XAUUSD symbol name on that account type?
> 4. Please send the full XAUUSD contract specification: contract size, minimum lot and lot step, tick size and tick
>    value, typical and minimum spread, commission (if any), swap long/short and triple-swap day, margin requirement and
>    leverage for gold, and the stops level.
> 5. What are the margin-call and stop-out levels for that account type?
> 6. What time zone does the MT5 server use, and does it change for daylight saving (e.g. GMT+2 in winter, GMT+3 in summer)?
> 7. What are the XAUUSD trading hours, including the daily break, and where can I find the holiday schedule?
> 8. How are orders executed: are you the counterparty (market maker), or are orders passed to a liquidity provider? Which
>    legal entity is the counterparty to my trades, and what is its FSCA FSP number?
>
> Thank you,
> [Your name]
