# GEX trading bot for Interactive Brokers

An automated gamma exposure (GEX) strategy in Python. Once a day it reads dealer gamma and how the S&P 500 has moved since the open, then trades Micro E-mini S&P 500 futures (MES) for the last half hour through the Interactive Brokers TWS API. Paper accounts only.

A terminal desk prints every input and the reasoning behind the trade as it happens: the regime, the rule, the account, the decision, the fills and the result.

## The rule

At 15:30 New York time, compare MES with the 09:30 open.

| Dealer gamma at the previous close | Market up since the open | Market down since the open |
|---|---|---|
| Short gamma (net gamma below zero) | buy | sell |
| Long gamma (net gamma above zero) | sell | buy |

A flat day means no trade. Every position is closed at 15:59:30.

Size is notional equal to equity: net liquidation divided by 5 times the MES price, rounded down. On a $1m paper account that is about 26 contracts.

**Why it works this way.** Option dealers hedge their gamma. When they are short gamma they have to trade with the move, so late moves tend to continue into the close. When they are long gamma they trade against it, so late moves tend to fade.

## Where the idea comes from

1. Baltussen, Da, Lammers and Martens, *Hedging demand and market intraday momentum*, Journal of Financial Economics, 2021 ([author PDF](https://academicweb.nd.edu/~zda/intramom.pdf)). The last half hour continues the day, and on the S&P 500 only when dealers are short gamma.
2. Barbon and Buraschi, *Gamma Fragility*, 2021. Positive dealer gamma goes with intraday reversal.

The regime uses the same series as that research: the SqueezeMetrics daily SPX net gamma, free at `https://squeezemetrics.com/monitor/static/DIX.csv`. It is downloaded at run time and not redistributed here.

## Backtest context

These tests were run while designing the rule. They are not part of this repository.

| Test, SPY last half hour, October 2023 to September 2026 | Sharpe |
|---|---|
| Rule above, 1 basis point of costs per day | 1.37 |
| Same trade always with the day, ignoring gamma | minus 2.00 |
| Same rule on QQQ | 0.88 |

**Read it with care:**
- Gross edge is about 3 basis points a day, and it disappears at 3 basis points of costs.
- Only 26 of the 723 sessions opened in short gamma.
- The worst days were long gamma fades on event days, when the book flipped during the session.

## Files

| File | What it does |
|---|---|
| `ib_engine.py` | the engine and the terminal desk: regime, decision, orders, fills, result, log |
| `last_hour.py` | the 15:30 decision, no broker involved |
| `regime.py` | today's regime from SqueezeMetrics, plus a cross-check from the Cboe chain |
| `gex.py` | full SPX chain from Cboe delayed quotes and its gamma exposure, used for the cross-check |
| `flatten_paper.py` | closes every position on the paper account so a session starts flat |
| `test_engine_offline.py` | runs a whole session against a fake broker, no TWS needed |

## Setup

1. **Open TWS or IB Gateway** logged into a **paper** account. Enable the API under Global Configuration, API, Settings. Paper ports are 7497 (TWS) and 4002 (Gateway).
2. **Install Python 3.11 or newer**, then `pip install -r requirements.txt`.
3. **Optional:** copy `.env.example` to `.env` to change host, port or client id.

## Run

```
python test_engine_offline.py                 # full session against a fake broker
python regime.py                              # today's regime and the Cboe cross-check
python ib_engine.py --check                   # inputs and the decision it would take, no orders
python ib_engine.py --paper --now --hold-minutes 3 --contracts 1
                                              # plumbing test while the market is open
python ib_engine.py --paper                   # real session: waits for 15:30, trades, flattens at 15:59:30
python flatten_paper.py --go                  # close everything on the paper account
```

Every session appends one line to `out/trades.csv`: regime, leg, action, contracts, entry, exit, points, net.

## Safety

- Without `--paper` it only computes and prints.
- It refuses any port that is not a paper port.
- It stands down on early close days and when the regime file is more than four days old.
- It refuses to trade on top of an existing MES position unless started with `--flatten-first`.
- Contract: the nearest MES expiry with more than seven days left, so it rolls a week before expiry.
- Fills and commissions are read back from the execution log. On a paper account IB simulates the fills; commissions follow IB's real schedule.

## Disclaimer

Research and education only, not investment advice. Tested on paper, never with real money. Futures carry substantial risk.

## License

MIT
