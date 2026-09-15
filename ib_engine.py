"""The last half hour, on the Interactive Brokers paper account, with a desk view.

One decision a day. At 15:30 ET read the regime (regime.py) and how far the
S&P has moved since the 09:30 open, take the position last_hour.decide()
gives, in Micro E-mini S&P 500 futures, and flatten just before the bell.
The terminal shows every input and the reasoning behind the trade as it happens.

Needs ib_async and rich (requirements.txt). Safe by default: without --paper
it computes and prints, and places nothing. It refuses live ports.

  python ib_engine.py --check                       inputs and the decision it would take; no orders
  python ib_engine.py                               wait for 15:30 ET, decide, print; no orders
  python ib_engine.py --paper                       wait for 15:30 ET, decide, trade, flatten at 15:59:30
  python ib_engine.py --paper --now --hold-minutes 3 --contracts 1
                                                   plumbing test any time the market is open

Contract: the nearest MES expiry with more than seven days left, so the roll
happens a week before expiry on its own. Prices come from IB one-minute bars of
that contract, which need no market data subscription on this account. Fills are
read from the execution log, not from order status.
"""

from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import logging

from ib_async import IB, Future, MarketOrder
from rich.console import Console
from rich.padding import Padding
from rich.panel import Panel
from rich.text import Text

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from last_hour import MES_MULT, decide          # noqa: E402
from regime import cboe_crosscheck, regime_for   # noqa: E402

ET = ZoneInfo("America/New_York")
MADRID = ZoneInfo("Europe/Madrid")
LOG = HERE / "out" / "trades.csv"
ENV = HERE / ".env"
PAPER_PORTS = {7497, 4002}
QUIET = {2104, 2106, 2107, 2108, 2119, 2158, 10349}   # 10349: "TIF set to DAY based on preset", a notice, not an error
EARLY_CLOSE = {date(2026, 11, 27), date(2026, 12, 24)}     # 13:00 ET sessions, stand down
DECIDE_AT = (15, 30)
FLATTEN_AT = (15, 59, 30)
ROLL_DAYS = 7
MARK_EVERY = 10                                            # seconds between live marks while holding

con = Console(highlight=False)                             # follows the terminal's own width
LABEL = 26
BLUE, RED, GOLD, DIM = "#6FA8DC", "#E06C66", "#E5B93C", "grey62"


# ---------------------------------------------------------------- screen
def now_et() -> datetime:
    return datetime.now(ET)


def stamp() -> str:
    return now_et().strftime("%H:%M:%S")


def section(n: int, title: str, right: str = "") -> None:
    t = Text()
    t.append(f"\n {n}  ", style=GOLD)
    t.append(title.upper(), style="bold")
    if right:
        t.append("  " + right, style=DIM)
    con.print(t)


def row(label: str, value, style: str = "", note: str = "") -> None:
    """Label, value, and a note that drops to its own line when the terminal is narrow."""
    width = con.width
    t = Text()
    t.append(f"{label:<{LABEL}}", style=DIM)
    t.append(str(value), style=style or "bold")
    fits = 4 + LABEL + len(str(value)) + 3 + len(note) <= width
    if note and fits:
        t.append("   " + note, style=DIM)
    con.print(Padding(t, (0, 0, 0, 4)))
    if note and not fits:
        con.print(Padding(Text(note, style=DIM), (0, 0, 0, 4 + LABEL)))


def say(text: str, style: str = "") -> None:
    con.print(Padding(Text(text, style=style), (0, 0, 0, 4)))


def event(text: str, style: str = "") -> None:
    t = Text("    ")
    t.append(stamp() + "  ", style=DIM)
    t.append(text, style=style)
    con.print(t)


def mask(account: str) -> str:
    return account[:3] + "•••" + account[-3:] if len(account) > 6 else account


def money(v: float, d: int = 2) -> str:
    return ("−" if v < 0 else "+") + "$" + f"{abs(v):,.{d}f}"


# ---------------------------------------------------------------- IB plumbing
def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    return out


def connect(client_id: int) -> IB:
    logging.getLogger("ib_async").setLevel(logging.CRITICAL)   # the desk prints its own lines; no library dumps on screen
    cfg = load_env()
    host, port = cfg.get("IB_HOST", "127.0.0.1"), int(cfg.get("IB_PORT", "7497"))
    if port not in PAPER_PORTS:
        raise SystemExit(f"port {port} is not a paper port, refusing")
    ib = IB()
    errors: list[int] = []
    ib.errorEvent += lambda rid, code, msg, c: errors.append(code) or (
        event(f"TWS {code}  {msg[:70]}", DIM) if code not in QUIET else None)
    try:
        ib.connect(host, port, clientId=int(cfg.get("IB_CLIENT_ID", client_id)), timeout=20)
    except Exception as e:                                          # noqa: BLE001
        if 10141 in errors:
            raise SystemExit("TWS is waiting for the paper trading disclaimer: accept it in TWS, then run again")
        raise SystemExit(f"cannot reach TWS on {host}:{port}: {e}\n"
                         "open TWS, log into the paper account, enable API socket clients")
    return ib


def pick_contract(ib: IB):
    """Nearest MES expiry with more than ROLL_DAYS left."""
    details = ib.reqContractDetails(Future("MES", exchange="CME", currency="USD"))
    if not details:
        raise SystemExit("IB returned no MES contracts")
    today = now_et().date()
    cands = []
    for d in details:
        s = d.contract.lastTradeDateOrContractMonth[:8]
        exp = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
        if (exp - today).days > ROLL_DAYS:
            cands.append((exp, d.contract))
    if not cands:
        raise SystemExit("no MES expiry more than a week out")
    cands.sort(key=lambda t: t[0])
    exp, c = cands[0]
    ib.qualifyContracts(c)
    return c, exp


def bars_today(ib: IB, contract) -> list:
    bars = ib.reqHistoricalData(contract, "", "1 D", "1 min", "TRADES", useRTH=False, formatDate=2)
    today = now_et().date()
    rows = []
    for b in bars or []:
        t = b.date if isinstance(b.date, datetime) else datetime.combine(b.date, datetime.min.time())
        t = (t if t.tzinfo else t.replace(tzinfo=ZoneInfo("UTC"))).astimezone(ET)
        if t.date() == today:
            rows.append((t, b))
    return rows


def day_so_far(ib: IB, contract) -> dict:
    """09:30 ET open and the latest one-minute close of the contract, today."""
    rows = bars_today(ib, contract)
    opens = [b for t, b in rows if (t.hour, t.minute) >= (9, 30)]
    if not opens:
        raise SystemExit("no bar at or after 09:30 ET yet, the session has not opened")
    o = opens[0].open
    t_last, last = rows[-1]
    return dict(open=o, last=last.close, sofar=math.log(last.close / o), stamp=t_last)


def last_price(ib: IB, contract) -> float | None:
    rows = bars_today(ib, contract)
    return rows[-1][1].close if rows else None


def account_info(ib: IB) -> tuple[str, float]:
    s = {r.tag: r for r in ib.accountSummary()}
    acct = next((r.account for r in ib.accountSummary()), "")
    return acct, float(s["NetLiquidation"].value)


def mes_position(ib: IB) -> float:
    return sum(p.position for p in ib.positions() if p.contract.symbol == "MES")


def place(ib: IB, contract, action: str, qty: int) -> tuple[float, float]:
    """Market order, wait on filled quantity, return (average fill, commission)."""
    order = MarketOrder(action, qty)
    order.tif = "DAY"                    # explicit, so TWS does not send notice 10349 (ib_async would log it as a cancel)
    trade = ib.placeOrder(contract, order)
    event(f"{action} {qty} {contract.localSymbol}  market order sent  (id {trade.order.orderId})",
          BLUE if action == "BUY" else RED)
    for _ in range(120):
        if sum(f.execution.shares for f in trade.fills) >= qty:
            break
        ib.waitOnUpdate(timeout=1)
    ib.sleep(1.5)
    shares = sum(f.execution.shares for f in trade.fills)
    cash = sum(f.execution.shares * f.execution.price for f in trade.fills)
    comm = sum((f.commissionReport.commission or 0.0) for f in trade.fills if f.commissionReport)
    avg = cash / shares if shares else float("nan")
    if shares < qty:
        event(f"only {shares:.0f} of {qty} filled, check TWS", RED)
    event(f"filled {shares:.0f} @ {avg:,.2f}   commission ${comm:,.2f}", "bold")
    return avg, comm


def wait_until(ib: IB, target: datetime, label: str) -> None:
    with con.status("", spinner="dots") as st:
        while True:
            left = (target - now_et()).total_seconds()
            if left <= 0:
                break
            h, rem = divmod(int(left), 3600)
            m, s = divmod(rem, 60)
            st.update(Text(f"  waiting for {label}   {h}h {m:02d}m {s:02d}s", style=DIM))
            ib.sleep(min(1.0, max(0.2, left)))


def log_row(row_: dict) -> None:
    LOG.parent.mkdir(exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row_))
        if new:
            w.writeheader()
        w.writerow(row_)


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", action="store_true", help="place the orders on the paper account")
    ap.add_argument("--check", action="store_true", help="inputs and decision only, then exit")
    ap.add_argument("--now", action="store_true", help="decide immediately instead of waiting for 15:30 ET")
    ap.add_argument("--hold-minutes", type=float, default=None, help="flatten after this many minutes")
    ap.add_argument("--contracts", type=int, default=None, help="override the size, for plumbing tests")
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--no-fade", action="store_true", help="trade the short gamma leg only")
    ap.add_argument("--equity", type=float, default=None, help="override net liquidation for sizing")
    ap.add_argument("--flatten-first", action="store_true", help="close any MES position found on start")
    ap.add_argument("--no-crosscheck", action="store_true")
    ap.add_argument("--client-id", type=int, default=23)
    ap.add_argument("--start-delay", type=float, default=0.0,
                    help="seconds to wait before printing, so a window being placed has its final width")
    a = ap.parse_args()
    time.sleep(a.start_delay)

    today = now_et().date()
    mode = "PAPER ORDERS" if a.paper else ("CHECK, no orders" if a.check else "DRY RUN, no orders")
    head = Text()
    head.append("GEX  LAST HALF HOUR", style="bold")
    head.append(f"      {mode}", style=RED if a.paper else DIM)
    head.append(f"\n{now_et():%a %d %b %Y}   {now_et():%H:%M:%S} New York   "
                f"{datetime.now(MADRID):%H:%M} Madrid", style=DIM)
    con.print(Panel(head, border_style=GOLD, padding=(0, 2)))
    if today in EARLY_CLOSE:
        raise SystemExit("early close today, standing down")

    # 1 regime ------------------------------------------------------------
    section(1, "Regime", "who is hedging, and which way")
    r = regime_for(today)
    short = r["short_gamma"]
    row("SqueezeMetrics gamma", f"{r['gex'] / 1e9:+.2f} bn", RED if short else BLUE,
        f"per 1% move, close of {r['asof']:%d %b}")
    if short:
        say("Dealers are SHORT gamma. To stay hedged they buy rallies and sell falls,", RED)
        say("so moves late in the day tend to keep going.")
        leg = "FOLLOW, with the day"
    else:
        say("Dealers are LONG gamma. To stay hedged they sell rallies and buy dips,", BLUE)
        say("so moves late in the day tend to give some back.")
        leg = "FADE, against the day"
    if a.no_fade and not short:
        leg = "none, fade leg switched off"
    row("Leg for today", leg, GOLD)
    if r["stale_days"] > 4:
        raise SystemExit("regime series more than four days old, standing down")
    if not a.no_crosscheck:
        try:
            c = cboe_crosscheck()
            agree = (c["full_book_bn"] < 0) == short
            row("Cboe full book", f"{c['full_book_bn']:+.1f} bn", "",
                "agrees" if agree else "disagrees, SqueezeMetrics is the tested series")
        except Exception as e:                                      # noqa: BLE001
            row("Cboe full book", "unavailable", DIM, str(e)[:40])

    # 2 rule --------------------------------------------------------------
    section(2, "The rule", "Baltussen, Da, Lammers, Martens, JFE 2021")
    say("At 15:30 New York, look at the S&P move since the 09:30 open.")
    say("Short gamma: go with it. Long gamma: go against it. Flat before the bell.")
    say("Tested 2023 to 2026 on SPY, last half hour: Sharpe 1.37, ignoring gamma -2.00.", DIM)

    # 3 account and contract ----------------------------------------------
    section(3, "Account and contract")
    ib = connect(a.client_id)
    acct, nlv = account_info(ib)
    equity = a.equity or nlv
    contract, exp = pick_contract(ib)
    row("Paper account", mask(acct), "", f"net liquidation ${nlv:,.0f}")
    row("Instrument", f"MES {contract.localSymbol}", "",
        f"Micro E-mini S&P 500, expires {exp:%d %b %Y}, ${MES_MULT:.0f} a point")
    held = mes_position(ib)
    row("MES position now", f"{held:+.0f}" if held else "flat")
    if held:
        if a.paper and a.flatten_first:
            event("closing the position found on start", GOLD)
            place(ib, contract, "SELL" if held > 0 else "BUY", int(abs(held)))
        elif a.paper:
            ib.disconnect()
            raise SystemExit("refusing to trade on top of an open position, use --flatten-first")

    # 4 wait ------------------------------------------------------------------
    if not (a.now or a.check):
        section(4, "Waiting", "one decision a day")
        target = now_et().replace(hour=DECIDE_AT[0], minute=DECIDE_AT[1], second=0, microsecond=0)
        if now_et() > target.replace(minute=55):
            ib.disconnect()
            raise SystemExit("past 15:55 New York, too late for today")
        wait_until(ib, target, "15:30 New York")

    # 5 decision --------------------------------------------------------------
    section(5, "Decision", now_et().strftime("%H:%M:%S New York"))
    try:
        d = day_so_far(ib, contract)
    except SystemExit as e:
        px = last_price(ib, contract)
        row("MES last", f"{px:,.2f}" if px else "no bars yet", "", "the 09:30 open has not printed yet")
        say("Run again after 09:30 New York for a live decision.", DIM)
        ib.disconnect()
        return 0
    up = d["sofar"] > 0
    row("MES at the 09:30 open", f"{d['open']:,.2f}")
    row("MES now", f"{d['last']:,.2f}", "", f"bar {d['stamp']:%H:%M}")
    row("Day so far", f"{100 * d['sofar']:+.2f}%", BLUE if up else RED, "up" if up else "down")
    dec = decide(r["gex"], d["sofar"], equity, d["last"], a.leverage, not a.no_fade)
    n = a.contracts if (a.contracts and dec.action != "FLAT") else dec.contracts
    if dec.action == "FLAT":
        row("Action", "STAND ASIDE", GOLD, dec.why)
    else:
        why = ("short gamma and the day is " if short else "long gamma and the day is ") + ("up" if up else "down")
        so = "go with it" if short else "go against it"
        row("Reasoning", f"{why}, so {so}", GOLD)
        row("Action", f"{'BUY' if dec.action == 'LONG' else 'SELL'} {n} MES", BLUE if dec.action == "LONG" else RED,
            "plumbing test size" if a.contracts else "")
        if not a.contracts:
            row("Size", f"${equity:,.0f} x {a.leverage:.2f} / (${MES_MULT:.0f} x {d['last']:,.2f})",
                "", f"= {dec.contracts} contracts")
        row("Notional", f"${n * MES_MULT * d['last']:,.0f}")
        row("Exit", "market order at 15:59:30" if a.hold_minutes is None else f"market order after {a.hold_minutes:g} min")

    if a.check or not a.paper or dec.action == "FLAT":
        con.print()
        say("No orders placed." + ("" if a.paper else "  Dry run."), DIM)
        ib.disconnect()
        return 0

    # 6 execution -------------------------------------------------------------
    section(6, "Execution", "fills read from the execution log")
    side = "BUY" if dec.action == "LONG" else "SELL"
    entry_px, entry_comm = place(ib, contract, side, n)
    t_entry = now_et()
    sign = 1 if side == "BUY" else -1
    exit_at = (t_entry + timedelta(minutes=a.hold_minutes)) if a.hold_minutes is not None else \
        now_et().replace(hour=FLATTEN_AT[0], minute=FLATTEN_AT[1], second=FLATTEN_AT[2], microsecond=0)
    last_mark = 0.0
    with con.status("", spinner="dots") as st:
        while now_et() < exit_at:
            if time.time() - last_mark >= MARK_EVERY:
                px = last_price(ib, contract)
                if px:
                    pnl = (px - entry_px) * sign * MES_MULT * n
                    event(f"mark {px:,.2f}   open P&L {money(pnl)}", BLUE if pnl >= 0 else RED)
                last_mark = time.time()
            left = int((exit_at - now_et()).total_seconds())
            st.update(Text(f"  holding {side.lower()} {n} MES, exit in {left // 60}m {left % 60:02d}s", style=DIM))
            ib.sleep(1.0)
    exit_px, exit_comm = place(ib, contract, "SELL" if side == "BUY" else "BUY", n)

    # 7 result ----------------------------------------------------------------
    points = (exit_px - entry_px) * sign
    gross = points * MES_MULT * n
    comm = entry_comm + exit_comm
    net = gross - comm
    section(7, "Result")
    row("Entry, exit", f"{entry_px:,.2f}  then  {exit_px:,.2f}")
    row("Points x contracts x $5", f"{points:+.2f} x {n} x 5 = {money(gross)}")
    row("Commission", money(-comm))
    row("Net", money(net), BLUE if net >= 0 else RED)
    log_row(dict(date=str(today), regime="short" if short else "long", gex_bn=round(r["gex"] / 1e9, 3),
                 regime_asof=str(r["asof"]), leg=dec.leg, action=dec.action, contracts=n,
                 contract=contract.localSymbol, open=d["open"], at_decision=d["last"],
                 sofar_pct=round(100 * d["sofar"], 3), entry=round(entry_px, 2), exit=round(exit_px, 2),
                 points=round(points, 2), gross=round(gross, 2), commission=round(comm, 2), net=round(net, 2),
                 entered=t_entry.strftime("%H:%M:%S"), exited=stamp(),
                 mode="paper" + (f" test {a.hold_minutes:g}m" if a.hold_minutes is not None else "")))
    remaining = mes_position(ib)
    if remaining:
        event(f"position after exit is {remaining:+.0f} MES, check TWS", RED)
    else:
        say("Flat. Logged to out/trades.csv.", DIM)
    ib.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
