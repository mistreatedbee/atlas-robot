"""Broker integration, split into strictly separate stages (docs/14_vault_mt5_integration.md):

* read-only access (mt5_readonly.py)  - account, symbol, prices, positions, history; cannot trade
* order preparation (orders.py)        - builds and validates an order; has no way to send it
* simulated execution                  - paper trading (next milestone)
* live execution                       - DOES NOT EXIST; requires the owner's explicit authorisation
"""
