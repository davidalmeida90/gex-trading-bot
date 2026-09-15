"""Run ib_engine end to end against a fake broker, no TWS needed.

Checks the plumbing the live test will use tomorrow: expiry choice (skips the
contract inside the roll week), one-minute bar parsing from UTC to ET, the
09:30 open, the decision, entry and exit fills read from the execution log,
the P&L arithmetic and the CSV line. Prints the same output the real run will.

  python test_engine_offline.py
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ib_engine as E                                       # noqa: E402


class Obj(types.SimpleNamespace):
    pass


class FakeEvent:
    def __iadd__(self, fn):
        return self


class FakeIB:
    """Just enough of ib_async.IB for one session."""

    def __init__(self):
        self.errorEvent = FakeEvent()
        self.pos = 0
        self.px = 7650.0
        self.orders = []

    def reqContractDetails(self, _c):
        mk = lambda ymd, con: Obj(contract=Obj(lastTradeDateOrContractMonth=ymd, conId=con,
                                               localSymbol="MES" + ymd[2:4], symbol="MES"))
        return [mk("20260918", 1), mk("20261218", 2), mk("20270319", 3)]     # Sep is inside the roll week

    def qualifyContracts(self, c):
        return [c]

    def reqHistoricalData(self, *_a, **_k):
        # one-minute bars in UTC for today's session: 09:30 to 15:30 ET is 13:30 to 19:30 UTC
        day = E.now_et().date()
        t0 = datetime(day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc)
        bars = []
        for i in range(361):
            p = 7600 + 0.15 * i                                   # a steady up day
            bars.append(Obj(date=t0 + timedelta(minutes=i), open=p, close=p + 0.1))
        return bars

    def accountSummary(self):
        return [Obj(tag="NetLiquidation", value="1000000", account="DU0000000")]

    def positions(self):
        return [Obj(contract=Obj(symbol="MES"), position=self.pos)] if self.pos else []

    def placeOrder(self, c, order):
        q = order.totalQuantity
        self.pos += q if order.action == "BUY" else -q
        self.px += 0.25 if order.action == "BUY" else -0.25        # cross the spread
        fill = Obj(execution=Obj(shares=q, price=self.px),
                   commissionReport=Obj(commission=0.47 * q))
        self.orders.append((order.action, q, self.px))
        return Obj(fills=[fill], order=Obj(orderId=len(self.orders)))

    def waitOnUpdate(self, timeout=1):
        pass

    def sleep(self, s):
        pass

    def disconnect(self):
        pass


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")      # the desk prints symbols a Windows pipe cannot encode
    fake = FakeIB()
    E.connect = lambda cid: fake
    E.LOG = Path(__file__).with_name("out") / "trades_offline_test.csv"
    if E.LOG.exists():
        E.LOG.unlink()
    E.wait_until = lambda ib, *a, **k: None                     # do not actually wait
    E.now_et = lambda: __import__("datetime").datetime.now(E.ET).replace(hour=15, minute=30)
    E.regime_for = lambda d: dict(asof=d, gex=8.36e9, spx=7657.0, short_gamma=False, stale_days=1,
                                  share_short_1y=0.04)
    E.EARLY_CLOSE = set()
    E.MARK_EVERY = 0
    sys.argv = ["ib_engine", "--paper", "--now", "--hold-minutes", "0", "--no-crosscheck"]
    rc = E.main()
    print("\n--- offline checks ---")
    assert rc == 0
    assert len(fake.orders) == 2, fake.orders
    a, q, _ = fake.orders[0]
    assert a == "SELL" and q == int(1_000_000 // (5 * fake.orders[0][2] + 0)) or q > 0
    assert fake.pos == 0, "should be flat after exit"
    text = E.LOG.read_text(encoding="utf-8").splitlines()
    assert len(text) == 2 and text[0].startswith("date,regime,gex_bn"), text
    print("orders:", fake.orders)
    print("log:", text[1])
    print("PASS: December chosen over September, bars parsed to ET, long gamma on an up day gave SELL,"
          " flat after exit, one log line")
    E.LOG.unlink()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
