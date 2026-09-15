"""Close every position on the IB paper account with market orders, so a session starts flat.

  python flatten_paper.py          list positions, place nothing
  python flatten_paper.py --go     close them all and wait for the fills

Paper ports only (the engine's connect() refuses live ports). Cash cannot be reset from
the API; that is done in IB Account Management under Paper Trading.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ib_async import MarketOrder, Stock  # noqa: E402

from ib_engine import connect  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--go", action="store_true", help="place the closing orders")
    ap.add_argument("--client-id", type=int, default=42)
    a = ap.parse_args()

    ib = connect(a.client_id)
    acct = ib.managedAccounts()[0]
    if not acct.startswith("DU"):
        ib.disconnect()
        raise SystemExit(f"{acct} does not look like a paper account (DU...), refusing")
    positions = [p for p in ib.positions() if p.position]
    print(f"account {acct}: {len(positions)} open position(s)")
    for p in positions:
        c = p.contract
        print(f"  {c.secType:<5} {c.localSymbol or c.symbol:<10} qty {p.position:>10,.0f}  avg {p.avgCost:,.2f}")
    if not positions:
        ib.disconnect()
        return 0
    if not a.go:
        print("nothing placed (add --go)")
        ib.disconnect()
        return 0

    trades = []
    for p in positions:
        c = p.contract
        if c.secType == "STK":
            c = Stock(c.symbol, "SMART", c.currency or "USD", primaryExchange=c.primaryExchange or c.exchange)
        ib.qualifyContracts(c)
        side = "SELL" if p.position > 0 else "BUY"
        order = MarketOrder(side, abs(p.position))
        order.tif = "DAY"
        trades.append((p, ib.placeOrder(c, order)))
        print(f"  placed {side} {abs(p.position):,.0f} {c.symbol}")

    deadline = time.time() + 90
    while time.time() < deadline and any(not t.isDone() for _, t in trades):
        ib.sleep(1)
    print("\nresult:")
    for p, t in trades:
        fills = sum(f.execution.shares for f in t.fills)
        avg = (sum(f.execution.shares * f.execution.price for f in t.fills) / fills) if fills else 0.0
        print(f"  {p.contract.symbol:<8} {t.orderStatus.status:<12} filled {fills:>8,.0f} / {abs(p.position):,.0f}  avg {avg:,.2f}")
    left = [p for p in ib.positions() if p.position]
    print(f"\npositions left: {len(left)}")
    for v in ib.accountSummary():
        if v.tag in ("NetLiquidation", "TotalCashValue", "GrossPositionValue"):
            print(f"  {v.tag:<20} {float(v.value):,.2f} {v.currency}")
    ib.disconnect()
    return 0 if not left else 1


if __name__ == "__main__":
    raise SystemExit(main())
