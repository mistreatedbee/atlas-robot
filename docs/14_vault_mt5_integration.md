# 14 — Vault Markets / MT5 integration, device setup, and the owner checklist

*2026-10-12. Each statement below is marked **[verified]** or **[assumption]**, with its source.*

## How Atlas talks to Vault Markets

Atlas never connects to Vault's servers directly. It talks to the **MetaTrader 5 terminal** installed on your own
computer, which is logged in to your Vault account.

| Need | Source | Status |
|---|---|---|
| Account: balance, equity, margin, leverage, currency, stop-out levels | `account_info()` | read-only adapter built; tested with a fake terminal |
| XAUUSD contract specification | `symbol_info()` → generated `verified` spec file | built; commission comes separately (see below) |
| Current bid/ask, market status | `symbol_info_tick()` | built |
| M1/M5 history | `copy_rates_range()`, or manual export on any OS (doc 10) | adapter built; manual-export importer built (synthetic-tested) |
| Tick history (bid/ask) | `copy_ticks_range()`, or manual export | parser built; storage is the next item |
| Open positions, orders, trade history | `positions_get()`, `orders_get()`, `history_deals_get()` | positions built; history next |
| Commission | Not in `symbol_info`. Comes from deal history after one demo trade, or support's reply | **open** |
| Placing orders | — | **does not exist** (see "Execution stages") |

**[verified]** Facts from the MQL5 and MetaTrader 5 documentation, 2026-10-12:
- **Function list:** the official `MetaTrader5` Python package provides `initialize`, `account_info`, `symbol_info`,
  `symbol_info_tick`, `copy_rates_range`, `copy_ticks_range`, `positions_get`, `history_deals_get` and the other
  functions used above.
- **No credentials needed:** `initialize()` without parameters finds the terminal. When no login or password is
  passed, it uses the account and password **saved in the terminal**, so Atlas never needs your credentials.
- **Investor password = read-only, enforced by the broker:** an MT5 login with the **investor password** can view
  the account and prices but **cannot trade**.
- **Windows only:** the package does not install on macOS. On this Mac, pip finds no distribution for
  macOS arm64. It needs **Windows** running the MT5 terminal.

## Which device does what

| Device | What runs there | Status |
|---|---|---|
| **Windows PC or Windows VPS** | MT5 terminal **plus** Atlas with the `[mt5]` extra: live read-only data, later paper and (if authorised) live trading | ready to install; real-terminal check pending |
| **macOS** (your Mac) | Atlas for research and backtesting. MT5 for manual exports (doc 10). The MT5 Python link is not available | working now |
| **Linux** | Atlas for research and backtesting (e.g. a server for long runs) | working; tested in CI |

The automated test suite runs on **Windows, macOS and Linux** (Python 3.11–3.13) on every push to GitHub
(`.github/workflows/tests.yml`). Everything except the MT5 link is identical on all three.

### Windows setup (PowerShell)

1. **Install the tools:**
   - **Python 3.12** from python.org. Tick *"Add python.exe to PATH"*.
   - **Git for Windows**.
   - **MetaTrader 5**, from Vault's platform page.
2. **Get Atlas and install it:**
   ```powershell
   git clone https://github.com/mistreatedbee/atlas-robot.git
   cd atlas-robot
   py -3.12 -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev,mt5]"
   .venv\Scripts\python -m pytest -q          # should match the CI result
   ```
3. **Log in to MT5 with the investor password:**
   - Open MT5 → *File → Login to Trade Account*.
   - Enter your account number, the **investor (read-only) password** and the Vault server.
   - Tick **Save password**.

   The password is stored only in the MT5 terminal on that machine. Atlas never sees it.
4. **Run the read-only check:** `atlas mt5-check` (next deliverable). It prints the account mode, whether trading
   is allowed, and the XAUUSD specification.
   - It refuses to run against a **real** account that has trading enabled. Log in with the investor password for
     this stage.

### macOS / Linux setup
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest -q
```

## Credentials: rules

- **Never** paste a password, investor password, API key or account number into Claude, ChatGPT, a GitHub issue
  or any file in this repository. The repository is **public**.
- Credentials live **only** inside the MT5 terminal's own login dialog, on the machine that runs it.
- Atlas's code is tested to pass no login, password or server to `initialize()`, and to contain no credential fields.

## Execution stages (separate components, explicit safeguards)

| Stage | Component | Can it trade? | Safeguards |
|---|---|---|---|
| 1. Read-only | `atlas/broker/mt5_readonly.py` | **No** | Proxy exposes only read functions; trading names unreachable; a real account must be on the investor login; dedicated safety tests |
| 2. Order preparation | `atlas/broker/orders.py` | **No** (no send capability) | Requires an approved risk decision and a verified spec. Fresh quote. Volume min/max/step, stop-loss/take-profit side and rounding, stops/freeze distances checked against live bid/ask. Deterministic client order ID |
| 3. Simulated execution | paper executor (next milestone) | Simulated only | Duplicate-order journal, reconnection handling, kill switch, same risk gate |
| 4. Live execution | **not built** | — | Built only after **your explicit written authorisation**, once everything in the live-readiness list below has been verified, and as a separately reviewed module |

**Live-readiness list (all required before stage 4 can even be built):**
- the verified XAUUSD spec from the terminal, including commission;
- the measured bid/ask spread profile;
- the margin requirement and account-currency conversion confirmed;
- stop-loss/take-profit behaviour checked on a demo account;
- position sizing and maximum-loss controls passing at the real account size;
- duplicate-order prevention, reconnection and the kill switch tested in paper mode;
- a paper-trading period with no unexplained differences from the backtester.

## Owner checklist (short, in order)

| # | Action | Where | Needed | How to provide it safely |
|---|---|---|---|---|
| 1 | Find your **investor (read-only) password** | Vault client area or account-creation email; if none exists, set it in MT5: *Tools → Options → Server → Change*, choose *investor* | **Now** | Keep it to yourself. Enter it only in MT5's login dialog |
| 2 | Log MT5 (on your Mac for now) in with the **investor password** and do the manual exports in doc 10: specification screenshot, server-time screenshot, one week of M1 bars, the same week of ticks | MT5 desktop | **Now** | Put the files in `data/imports/vault_markets/` (git-ignored); crop the account number out of screenshots |
| 3 | Send Vault the support message in doc 10 (commission, execution model, counterparty, holidays) | Vault Contact page / live chat | Now (answers needed before stage 4) | Save the reply as a file in the same folder |
| 4 | **Decide on Windows:** a home Windows PC, or a Windows VPS (paid, runs 24/5, needed for unattended operation) | — | Later (before stage 3 on live data) | Tell me which; no credentials involved |
| 5 | On the Windows machine: follow "Windows setup" above with the **investor** login, run `atlas mt5-check`, and tell me the printed result | Windows | Later | The output contains no password. Remove the account number before sharing if you prefer |
| 6 | Optional: a Vault **demo** account for paper trading with real fills, plus one manual 0.01-lot demo trade to see the real commission | Vault client area / MT5 | Later | Same rules |

Having a live account does **not** authorise Atlas to trade. Live execution stays disabled until stage 4 has been
built, reviewed and explicitly approved by you.
