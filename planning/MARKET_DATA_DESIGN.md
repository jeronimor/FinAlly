# Market Data Backend — Design

Implementation design for the FinAlly market data subsystem (`backend/app/market/`): the unified source interface, the shared price cache, the GBM simulator, the Massive (Polygon.io) poller, the SSE stream, and how the rest of the backend wires into it.

This document describes the **target state**. Most of it already exists (see `MARKET_DATA_SUMMARY.md`); every place where the target differs from the current code is marked **Δ CHANGE** and gathered into a checklist in §12.

Source documents (all in `planning/`): `PLAN.md` §3, §5, §6, §7, §12, §13 · `MARKET_DATA_SUMMARY.md` · `REVIEW.md` · `MARKET_INTERFACE.md` · `MARKET_SIMULATOR.md` · `MASSIVE_API.md`. Where they disagree, §1.1 says which one this design follows.

---

## Contents

1. [Requirements](#1-requirements) · [1.1 Reconciling the planning docs](#11-reconciling-the-planning-docs)
2. [Architecture](#2-architecture)
3. [Data model — `models.py`](#3-data-model--modelspy)
4. [Price cache — `cache.py`](#4-price-cache--cachepy)
5. [Unified interface — `interface.py`](#5-unified-interface--interfacepy)
6. [Simulator — `seed_prices.py`, `simulator.py`](#6-simulator)
7. [Massive API — `massive_client.py`](#7-massive-api--massive_clientpy)
8. [Factory — `factory.py`](#8-factory--factorypy)
9. [SSE stream — `stream.py`](#9-sse-stream--streampy)
10. [Integration: app lifespan and tracked tickers](#10-integration)
11. [Testing](#11-testing)
12. [Change checklist](#12-change-checklist)

---

## 1. Requirements

Distilled from PLAN.md:

| # | Requirement | Source |
|---|---|---|
| R1 | Two price sources behind one interface; downstream code is source-agnostic | §6 |
| R2 | `MASSIVE_API_KEY` non-empty → Massive; absent/empty → simulator | §5 |
| R3 | Massive polls every `MASSIVE_POLL_INTERVAL` s (default 15) | §5, §13.3 |
| R4 | Simulator: GBM, ~500 ms ticks, sector-correlated moves, rare 2–5 % shocks, realistic seeds, unknown tickers seeded $50–300 | §6 |
| R5 | Simulator prices a newly added ticker immediately; Massive on its next poll | §6 |
| R6 | Tracked tickers = watchlist ∪ open positions; bookkeeping in the service layer | §6, §3 |
| R7 | Cache holds latest price, previous price, timestamp and **session open** per ticker | §6, §13.1 |
| R8 | `GET /api/stream/prices`: first message `retry: 1000`; check every ~500 ms; emit **only on change**; one event with **all** tickers | §6 |
| R9 | Event fields: `ticker, price, previous_price, session_open, timestamp (unix float), change, change_percent, session_change_percent, direction` | §6 |
| R10 | A ticker removal must reach connected SSE clients | REVIEW.md P1 |
| R11 | Router factory must be callable more than once | §13.2 |
| R12 | Cache `timestamp` is always Unix **seconds** (float); Massive's is the exchange trade time, never wall clock | `MARKET_SIMULATOR.md` output contract, `MARKET_INTERFACE.md` |
| R13 | Tickers in the cache are uppercase, whichever source wrote them | `MARKET_SIMULATOR.md` output contract |
| R14 | A missing or failed Massive poll keeps the last cached values; downstream code never assumes every tracked ticker is priced | `MARKET_INTERFACE.md` |
| R15 | Massive needs a Starter+ plan (snapshot endpoint); a free Basic key selects Massive but never prices anything | `MASSIVE_API.md` |

### 1.1 Reconciling the planning docs

`MARKET_INTERFACE.md` and `MARKET_SIMULATOR.md` were written as design sketches before the code; `MASSIVE_API.md` was verified against the SDK on 2026-10-23. Where they differ from each other or from the code, this design takes the following position:

| Topic | What the docs say | This design |
|---|---|---|
| `PriceUpdate` shape | `MARKET_INTERFACE.md`: mutable dataclass with stored `change`/`direction` fields | Keep the code's frozen dataclass with computed properties; add `session_open` (§3) |
| Where `PriceCache` lives | `MARKET_INTERFACE.md` file tree: `interface.py` | Keep the code's `cache.py` |
| SSE cadence | `MARKET_INTERFACE.md` sketch: push every 500 ms unconditionally | PLAN §6 wins: check every 500 ms, push only on version change (§9) |
| Massive timestamp | `MASSIVE_API.md` / `MARKET_INTERFACE.md`: `last_trade.sip_timestamp / 1e9`; code: `last_trade.timestamp / 1000` | Docs are right, code is broken (§7.1) |
| Massive missing timestamp | `MARKET_INTERFACE.md`: skip the snapshot | Fall back to `snap.updated` (also exchange-side ns), skip only if both are missing; never substitute wall clock (R12) |
| Massive errors | `MARKET_INTERFACE.md`: catch `BadResponse` | Catch `Exception`: network failures surface as urllib3 errors (e.g. `MaxRetryError`), not `BadResponse`, and must not kill the loop (§7.4) |
| `RESTClient` construction | `MARKET_INTERFACE.md`: in `__init__` | Keep the code's: in `start()`, so constructing an unstarted source does no I/O and never raises `AuthError` |
| Ticker case in the simulator | `MARKET_SIMULATOR.md`: stores tickers as given; callers must uppercase | Simulator normalizes too, matching Massive (§6.4); the service layer still normalizes first |
| `GBMSimulator.add_ticker` in `__init__` | `MARKET_SIMULATOR.md`: rebuilds Cholesky per ticker | Keep the code's single rebuild after batch init |
| Seed refresh from Massive Previous Close | `MARKET_SIMULATOR.md`: optional, not implemented | Out of scope; the simulator stays fully offline |
| Free Basic Massive key | `MARKET_INTERFACE.md`, `MASSIVE_API.md`: factory can't detect the plan | Same: no plan detection. The factory's log line and `.env.example` comment tell free-plan users to leave the key empty (§8) |

`CLAUDE.md` at the repo root still points to a `planning/archive` folder, which no longer exists; it should point to `planning/` (checklist item 7).

---

## 2. Architecture

```
                     create_market_data_source(cache)        ← reads env (R2, R3)
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
   SimulatorDataSource                        MassiveDataSource
   (GBMSimulator, 0.5 s loop)                 (RESTClient, N s poll, to_thread)
              │                                         │
              └──────────── cache.update() ─────────────┘
                                   ▼
                     PriceCache  (thread-safe, versioned)
                     ticker → PriceUpdate(price, previous_price,
                                          session_open, timestamp)
                                   │ read-only
         ┌─────────────────────────┼──────────────────────────┐
         ▼                         ▼                          ▼
  SSE /api/stream/prices    get_portfolio()            execute_trade()
  (version-diffed push)     watchlist GET, snapshots   (requires cached price)
```

Principles:

- **Producers write, consumers read.** Nothing outside the source calls the source for prices; everything reads `PriceCache`. The only calls into the source are lifecycle (`start/stop`) and the tracked-set mutations (`add_ticker/remove_ticker`), and only the service layer makes those.
- **The cache is the contract.** Both sources produce the same `PriceUpdate` via `cache.update(ticker, price, timestamp)`. Session-open bookkeeping lives in the cache, so it is identical for both sources.
- **One event loop, one writer.** Both sources run as asyncio tasks. The Massive SDK call runs in a worker thread but the cache write happens back on the loop. The lock in `PriceCache` makes the cache safe regardless.

File layout (unchanged):

```
backend/app/market/
  __init__.py        public re-exports
  models.py          PriceUpdate
  cache.py           PriceCache
  interface.py       MarketDataSource (ABC)
  seed_prices.py     seed prices, GBM params, correlation groups
  simulator.py       GBMSimulator + SimulatorDataSource
  massive_client.py  MassiveDataSource
  factory.py         create_market_data_source()
  stream.py          create_stream_router()
```

---

## 3. Data model — `models.py`

`PriceUpdate` is the only type that leaves the market layer. **Δ CHANGE:** add `session_open`, `session_change_percent`, and both fields in `to_dict()` (§13.1).

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    session_open: float  # First price seen for this ticker since server start
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def session_change_percent(self) -> float:
        """Percentage change since the session open (watchlist change % column)."""
        if self.session_open == 0:
            return 0.0
        return round((self.price - self.session_open) / self.session_open * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "session_open": self.session_open,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "session_change_percent": self.session_change_percent,
            "direction": self.direction,
        }
```

`session_open` is a required positional field placed before `timestamp` (fields with defaults must come last). All construction goes through `PriceCache.update()`, so the only other call sites to update are tests (`test_models.py` builds `PriceUpdate` directly).

Example serialized value (matches PLAN §6):

```python
>>> PriceUpdate("AAPL", 190.52, 190.41, 190.00, 1760600000.123).to_dict()
{'ticker': 'AAPL', 'price': 190.52, 'previous_price': 190.41, 'session_open': 190.0,
 'timestamp': 1760600000.123, 'change': 0.11, 'change_percent': 0.0578,
 'session_change_percent': 0.2737, 'direction': 'up'}
```

---

## 4. Price cache — `cache.py`

Responsibilities: store the latest `PriceUpdate` per ticker, remember the session open, and expose a monotonically increasing `version` that bumps on **every observable change** — updates and removals.

**Δ CHANGES:**
1. Track `session_open` per ticker (first price seen) and pass it into `PriceUpdate`.
2. `remove()` bumps `version` when it actually removed something (REVIEW.md P1 — otherwise SSE clients keep showing a removed ticker until the next price tick, and under Massive that can be 15 s; with an empty tracked set, forever).
3. `timestamp if timestamp is not None else time.time()` instead of `timestamp or time.time()` (a `0.0` timestamp is a value, not "missing").
4. Read `version` under the lock (cheap, removes any doubt about torn reads on other Python implementations).

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._session_open: dict[str, float] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update and every actual removal

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        The first update for a ticker sets its session open and has
        previous_price == price (direction 'flat').
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            price = round(price, 2)
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            session_open = self._session_open.setdefault(ticker, price)

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                session_open=session_open,
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Drop a ticker that is no longer tracked. Bumps version if it was present."""
        with self._lock:
            removed = self._prices.pop(ticker, None)
            self._session_open.pop(ticker, None)
            if removed is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter, used by SSE for change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Session open semantics

- The session open is the first price the cache sees for a ticker: the simulator's seed price, or Massive's first polled price. Source-agnostic because it lives here.
- **Removal resets it.** A ticker that stops being tracked and is later re-added starts a new session. Rationale: the simulator re-seeds a re-added unknown ticker at a fresh random price, and Massive may have moved a lot in between, so carrying the old open forward would show a meaningless change %. This is the one interpretation choice in this design; it matches "first price seen" for every ticker that stays tracked.

### Example

```python
cache = PriceCache()
cache.update("AAPL", 190.00)          # session_open=190.00, direction='flat', version=1
u = cache.update("AAPL", 190.52)      # previous=190.00, session_change_percent=0.2737
cache.version                          # 2
cache.remove("AAPL")                   # version=3 → SSE emits a snapshot without AAPL
cache.remove("AAPL")                   # no-op, version stays 3
```

---

## 5. Unified interface — `interface.py`

The ABC is unchanged in shape. **Δ CHANGE (doc only):** spell out that sources normalize tickers and that `remove_ticker` removes the ticker from the cache, since the service layer relies on both.

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never asks the source for prices — it reads the
    cache. Tickers passed in are normalized (strip + upper) by the source.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that writes to the PriceCache. Call once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Idempotent; no cache writes afterwards."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a ticker. No-op if already tracked.

        Simulator: priced in the cache before this returns.
        Massive: priced after the next poll.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the PriceCache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the currently tracked tickers."""
```

Differences downstream code must tolerate (and nothing else):

| Behavior | Simulator | Massive |
|---|---|---|
| Update cadence | every 0.5 s | every `MASSIVE_POLL_INTERVAL` s |
| Newly added ticker | priced immediately | `None` until next poll |
| Unknown / invalid symbol | priced (random seed) | stays `None` forever |
| `timestamp` | wall clock (`time.time()`) | exchange SIP trade time; 15 min old on Starter/Developer, hours old when the market is closed |
| Missing data | never | ticker absent from the response or `last_trade=None` → previous cached value kept |
| Failure mode | none | poll error logged; whole cache keeps its last values |
| Outside market hours | keeps moving | same last trade re-sent each poll (flat); snapshots reset at 12am ET and repopulate from ~4am ET |
| Plan needed | — | Starter or above |

Consequences for downstream code:

- Never assume a tracked ticker has a cached price: trades take the "No price available" path, the UI shows "—".
- Never judge staleness by comparing `timestamp` with `time.time()`; Massive timestamps are legitimately old.
- Tests written only against the simulator must not rely on "every ticker priced every tick" — that holds for the simulator alone.

---

## 6. Simulator

### 6.1 Parameters — `seed_prices.py` (unchanged)

```python
SEED_PRICES = {"AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
               "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
               "V": 280.00, "NFLX": 600.00}

TICKER_PARAMS = {"AAPL": {"sigma": 0.22, "mu": 0.05}, ..., "TSLA": {"sigma": 0.50, "mu": 0.03},
                 "NVDA": {"sigma": 0.40, "mu": 0.08}, "JPM": {"sigma": 0.18, "mu": 0.04}, ...}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}           # unknown tickers

CORRELATION_GROUPS = {"tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
                      "finance": {"JPM", "V"}}
INTRA_TECH_CORR, INTRA_FINANCE_CORR, CROSS_GROUP_CORR, TSLA_CORR = 0.6, 0.5, 0.3, 0.3
```

Seeds are static, so real prices drift away from them; switching from the simulator to a Massive key shows a jump. Refreshing seeds from Massive's Previous Close endpoint (available on the free plan) is possible but out of scope — see `MARKET_SIMULATOR.md`.

Housekeeping: the comment in `GBMSimulator._pairwise_correlation` says "TSLA is in tech set", but `CORRELATION_GROUPS["tech"]` doesn't include TSLA. Behavior is correct (TSLA gets 0.3 with everything); fix the comment.

### 6.2 Math

Per tick, for each ticker *i*:

```
S_i(t+dt) = S_i(t) · exp((μ_i − σ_i²/2)·dt + σ_i·√dt·Z_i)
dt        = 0.5 s / (252 · 6.5 · 3600 s) ≈ 8.48e-8 trading years
Z         = L · ε,  ε ~ N(0, I),  L = cholesky(C)
```

`C` is the pairwise correlation matrix built from the groups above (tech–tech 0.6, finance–finance 0.5, anything involving TSLA 0.3, otherwise 0.3). Every off-diagonal value is ≥ 0.3 and ≤ 0.6 with only two blocks, so `C` is positive definite for any ticker set and Cholesky never fails.

Shock events: with probability 0.001 per ticker per tick, multiply by `1 ± U(0.02, 0.05)`. With 10 tickers at 2 ticks/s that is one event roughly every 50 s.

Sanity check on the scale: for σ = 0.22, the per-tick standard deviation is `0.22·√8.48e-8 ≈ 6.4e-5`, i.e. about 1.2 ¢ on a $190 stock — enough that most ticks change the 2-dp price and flash the UI, small enough to look realistic.

### 6.3 `GBMSimulator` (unchanged — pure, synchronous, no I/O)

Key structure (full code is in `simulator.py`):

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt, self._event_prob = dt, event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for t in tickers:
            self._add_ticker_internal(t)     # no rebuild per ticker
        self._rebuild_cholesky()             # one O(n²) rebuild

    def step(self) -> dict[str, float]:
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z
        out = {}
        for i, t in enumerate(self._tickers):
            mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
            self._prices[t] *= math.exp((mu - 0.5 * sigma**2) * self._dt
                                        + sigma * math.sqrt(self._dt) * z[i])
            if random.random() < self._event_prob:
                self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
            out[t] = round(self._prices[t], 2)
        return out

    def add_ticker(self, ticker): ...     # seed from SEED_PRICES or U(50, 300); rebuild Cholesky
    def remove_ticker(self, ticker): ...  # drop state; rebuild Cholesky
    def get_price(self, ticker): ...
    def get_tickers(self): ...
```

The simulator keeps full-precision internal prices and only rounds what it emits, so rounding never accumulates.

### 6.4 `SimulatorDataSource`

**Δ CHANGE:** normalize tickers (`strip().upper()`) in `start/add_ticker/remove_ticker`, matching `MassiveDataSource`. `MARKET_SIMULATOR.md` notes that today the simulator stores tickers exactly as given and relies on callers to uppercase; this closes that gap (R13). The service layer still normalizes first, so this is defense in depth that guarantees the cache never holds two spellings of one ticker.

The simulator passes no timestamp, so the cache stamps each update with `time.time()` (Unix seconds, R12).

```python
def _normalize(ticker: str) -> str:
    return ticker.strip().upper()


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator."""

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        tickers = list(dict.fromkeys(_normalize(t) for t in tickers))  # dedupe, keep order
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache so SSE and trades have prices before the first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = _normalize(ticker)
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)   # R5: priced immediately

    async def remove_ticker(self, ticker: str) -> None:
        ticker = _normalize(ticker)
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")   # never let the loop die
            await asyncio.sleep(self._interval)
```

Note on `add_ticker` for an existing ticker: `GBMSimulator.add_ticker` is a no-op, but the cache is still updated with the current price. That bumps `version` and makes `previous_price == price` for one tick (a "flat" event). Harmless; the alternative check costs more than it saves. If it ever matters, guard with `if ticker in self._sim.get_tickers(): return`.

Concurrency: `add_ticker`/`remove_ticker` and `_run_loop` run on the same event loop and `step()` has no `await`, so the simulator's lists are never mutated mid-step.

---

## 7. Massive API — `massive_client.py`

Uses the `massive` SDK (lockfile pins 2.2.0) and the multi-ticker snapshot endpoint, `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=...`: one HTTP call per poll for all tracked tickers. Endpoint details, plan limits and SDK field names are in `MASSIVE_API.md`.

### 7.1 Current defects

**Δ CHANGE — price extraction is broken against the real SDK.** The current code reads `snap.last_trade.timestamp / 1000.0`. The SDK's `LastTrade` dataclass has no `timestamp` field (verified against the installed 2.2.0: fields are `ticker, trf_timestamp, sequence_number, sip_timestamp, participant_timestamp, conditions, correction, id, price, trf_id, size, exchange, tape`). Every real snapshot therefore raises `AttributeError`, is logged as "Skipping snapshot" and dropped — **Massive mode never prices anything**. The unit tests pass only because `MagicMock` invents a `timestamp` attribute. Fix: use `sip_timestamp`, which is in **nanoseconds** (`/ 1e9`), and build test snapshots from the real SDK models so this can't recur.

Other changes:

- **Δ** Snapshot the ticker list on the event loop and pass it to the worker thread; today `_fetch_snapshots` reads `self._tickers` from the thread while `add_ticker` may be appending to it.
- **Δ** Bound the initial poll in `start()` with a timeout so a slow or unreachable API can't stall app startup (the SDK retries up to 3× internally).
- **Δ** Treat `last_trade is None` / `price is None` explicitly (thin or overnight tickers) instead of relying on an exception.
- **Δ** Fall back to `snap.updated` (also ns, exchange-side) when `sip_timestamp` is missing; skip the snapshot if both are missing. Never substitute wall clock (R12).
- **Δ** Log `BadResponse` (key / plan / rate limit) separately from network errors, so a free-plan key is easy to diagnose.
- **Δ** Reject a non-positive poll interval.

### 7.2 Target implementation

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

NS_PER_SECOND = 1_000_000_000


def parse_snapshot(snap: TickerSnapshot) -> tuple[str, float, float] | None:
    """Extract (ticker, price, unix_seconds) from a snapshot, or None if unusable.

    Every SDK field is Optional: last_trade is None for thinly traded tickers and
    overnight (snapshots are cleared at 12am ET). Trade timestamps are nanoseconds
    (aggregate bars use milliseconds — not used here).
    """
    trade = snap.last_trade
    if not snap.ticker or trade is None or trade.price is None or trade.price <= 0:
        return None
    ts_ns = trade.sip_timestamp or snap.updated
    if not ts_ns:
        return None
    return snap.ticker, float(trade.price), ts_ns / NS_PER_SECOND


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    One snapshot call per poll covers every tracked ticker. The SDK is synchronous
    (urllib3), so the call runs in a worker thread; the cache is written on the loop.

    Plans: the snapshot endpoint needs Starter or above. A free Basic key gets a
    403 (BadResponse) on every poll — logged, and prices never appear.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        start_timeout: float = 10.0,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._start_timeout = start_timeout
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(t.strip().upper() for t in tickers))

        # Immediate first poll so the cache is warm, but never block startup for long
        try:
            await asyncio.wait_for(self._poll_once(), timeout=self._start_timeout)
        except TimeoutError:
            logger.warning("Massive: initial poll timed out; continuing in background")

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval",
                    len(self._tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.strip().upper()
        if ticker not in self._tickers:
            self._tickers.append(ticker)   # priced on the next poll (R5)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.strip().upper()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        tickers = list(self._tickers)       # snapshot on the loop, not in the thread
        if not tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except BadResponse as e:  # non-200 after the SDK's 3 retries: 401 / 403 / 429
            logger.error("Massive poll rejected (check key; snapshots need Starter+): %s", e)
            return
        except Exception as e:  # network: urllib3 MaxRetryError, timeouts, DNS
            logger.error("Massive poll failed: %s", e)
            return

        tracked = set(self._tickers)        # a ticker removed mid-poll must not reappear
        updated = 0
        for snap in snapshots:
            parsed = parse_snapshot(snap)
            if parsed is None:
                logger.debug("Massive: no usable trade for %s", getattr(snap, "ticker", "?"))
                continue
            ticker, price, timestamp = parsed
            if ticker in tracked:
                self._cache.update(ticker=ticker, price=price, timestamp=timestamp)
                updated += 1
        logger.debug("Massive poll: updated %d/%d tickers", updated, len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list[TickerSnapshot]:
        """Synchronous SDK call. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
```

Why the `tracked` re-check: a poll takes up to a few hundred ms in a thread. If the user removes a ticker during that window, `remove_ticker` clears it from the cache, and without the check the in-flight response would immediately re-add it — a ghost ticker that never updates again.

Why the cache dedupes nothing for unchanged prices: outside market hours Massive returns the same last trade every poll. `cache.update()` still bumps `version`, so SSE re-sends identical data every poll (every 15 s by default). That is cheap and keeps "flat" semantics honest; skipping unchanged `(price, timestamp)` pairs is an optional optimization:

```python
prev = self._cache.get(ticker)
if prev and prev.price == round(price, 2) and prev.timestamp == timestamp:
    continue  # nothing new since last poll
```

### 7.3 Example: one poll, end to end

Raw API (abbreviated keys) → SDK model → cache:

```json
{"ticker": "AAPL", "lastTrade": {"p": 191.74, "s": 100, "x": 4, "t": 1760600000123000000}, ...}
```
```python
snap.last_trade.price          # 191.74
snap.last_trade.sip_timestamp  # 1760600000123000000 (ns)
parse_snapshot(snap)           # ("AAPL", 191.74, 1760600000.123)
cache.get("AAPL").to_dict()    # {"price": 191.74, "timestamp": 1760600000.123, ...}
```

### 7.4 Error behavior

| Situation | What happens |
|---|---|
| Invalid key (401) | `BadResponse` → "poll rejected" logged every poll; app keeps running; cache keeps its last values (none, if it never succeeded) |
| Free Basic key (403: plan lacks snapshots) | Same as 401 — the most likely misconfiguration. Fix: leave `MASSIVE_API_KEY` empty and use the simulator, or upgrade to Starter+ |
| Rate limited (429) | SDK retries 3× with backoff (urllib3 `Retry(total=3, backoff_factor=0.1)`), then as above; next poll retries |
| Network down / timeout | urllib3 error → "poll failed" logged; next poll retries |
| Unknown symbol (`ZZZZ`) | Absent from the response → never priced; watchlist shows "—" (intended, PLAN §8) |
| Ticker with no trade / overnight | `parse_snapshot` → `None`, skipped; previous cached value kept |
| `RESTClient()` with empty key | `AuthError` — cannot happen, the factory only builds Massive for a non-empty key |

Polls are sequential: a slow request delays the next poll rather than overlapping it, which also guarantees at most one in-flight request (good for rate limits). The SDK's urllib3 timeouts bound how long one poll can take.

---

## 8. Factory — `factory.py`

**Δ CHANGE (§13.3):** read `MASSIVE_POLL_INTERVAL` (default 15) and pass it through. An invalid value logs a warning and falls back to the default rather than crashing startup. (`MARKET_INTERFACE.md`'s sketch calls `float(...)` directly, which would crash on a typo.)

The factory cannot tell a free Basic key from a paid one, so a Basic key still selects Massive and then never prices anything (R15). Rather than probing the API at startup, the selection log line says so, and `.env.example` repeats it next to `MASSIVE_API_KEY`:

```bash
# Optional: Massive (Polygon.io) API key for real market data.
# Needs a Starter plan or above — the free Basic plan has no snapshot access.
# Leave empty to use the built-in simulator (recommended).
MASSIVE_API_KEY=
```

```python
"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)

DEFAULT_MASSIVE_POLL_INTERVAL = 15.0


def _poll_interval_from_env() -> float:
    raw = os.environ.get("MASSIVE_POLL_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_MASSIVE_POLL_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if value <= 0:
        logger.warning("Invalid MASSIVE_POLL_INTERVAL=%r; using %.0fs",
                       raw, DEFAULT_MASSIVE_POLL_INTERVAL)
        return DEFAULT_MASSIVE_POLL_INTERVAL
    return value


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the market data source selected by environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource, polling every
      MASSIVE_POLL_INTERVAL seconds (default 15)
    - Otherwise → SimulatorDataSource

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = _poll_interval_from_env()
        logger.info("Market data source: Massive API (poll every %.1fs; requires a "
                    "Starter+ plan — free keys cannot read snapshots)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache,
                                 poll_interval=interval)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

Configuration summary:

| Variable | Effect | Default |
|---|---|---|
| `MASSIVE_API_KEY` | non-empty → Massive | empty → simulator |
| `MASSIVE_POLL_INTERVAL` | seconds between Massive polls | `15` (free-tier rate) |

The factory reads `os.environ` only; `load_dotenv()` is called once by the app entry point (§10) before the factory runs.

---

## 9. SSE stream — `stream.py`

**Δ CHANGES:**
1. Create the `APIRouter` **inside** the factory (§13.2). Today the module-level `router` gets a second `/prices` route each time `create_stream_router()` is called (e.g. once per test app).
2. Emit on every version change **including when the cache becomes empty** (`data: {}`), so removing the last ticker clears the client. Today an empty cache emits nothing.
3. Expose the poll interval as a factory argument so tests can run fast.
4. Stop swallowing `CancelledError` (see notes below).

```python
"""SSE streaming endpoint for live price updates."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache, interval: float = 0.5) -> APIRouter:
    """Create a fresh SSE router bound to the given price cache."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """Live prices for all tracked tickers, sent only when something changed."""
        return StreamingResponse(
            _generate_events(price_cache, request, interval),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"   # browser reconnects after 1s

    last_version = -1
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(interval)
    finally:  # let CancelledError propagate — swallowing it breaks server shutdown
        logger.info("SSE client disconnected: %s", client)
```

Notes:

- **Δ** The current generator catches and swallows `asyncio.CancelledError`. Don't: cancellation must propagate so uvicorn shutdown and `wait_for` timeouts work. The `finally` block is enough for logging.
- The initial `last_version = -1` guarantees a full snapshot as the first data event, so a (re)connecting client is immediately consistent. On a fresh server with an empty cache the first event is `data: {}`, which is valid and harmless.
- Reading `version` and then `get_all()` is not atomic, but the race is benign: a concurrent update lands in this snapshot early and then triggers one extra (identical-or-newer) event next cycle. Nothing is ever lost.
- The frontend treats each event as the **complete** set of tracked tickers (tickers missing from an event are no longer tracked); it appends each ticker's `price` to its ring buffer only when `timestamp` changed, so repeated Massive snapshots don't add duplicate sparkline points.
- Event cost: 10 tickers × ~250 bytes ≈ 2.5 KB every 0.5 s ≈ 5 KB/s per client. Fine for a single-user app.

Wire example:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.0, "previous_price": 190.0, "session_open": 190.0, "timestamp": 1760600000.0, "change": 0.0, "change_percent": 0.0, "session_change_percent": 0.0, "direction": "flat"}, "GOOGL": {...}}

data: {"AAPL": {"ticker": "AAPL", "price": 190.02, "previous_price": 190.0, ...,"direction": "up"}, ...}
```

Client (for reference; frontend owns this):

```ts
const es = new EventSource("/api/stream/prices");
es.onmessage = (e) => priceStore.applySnapshot(JSON.parse(e.data)); // Record<string, PriceUpdate>
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
```

---

## 10. Integration

These pieces live outside `app/market/` but define how the market layer is used. They are listed here so the backend agent builds them consistently with this design.

### 10.1 App lifespan — `app/main.py`

```python
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI

from app.db import init_db, get_tracked_tickers
from app.market import PriceCache, create_market_data_source, create_stream_router

load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # real env vars take precedence


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()                                    # create tables + seed if needed
    cache = app.state.price_cache
    source = create_market_data_source(cache)
    await source.start(get_tracked_tickers())    # watchlist ∪ open positions (R6)
    app.state.market_source = source
    # ... start snapshot task (record one now, then every 30 s) ...
    try:
        yield
    finally:
        # ... cancel snapshot task ...
        await source.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.state.price_cache = PriceCache()          # exists before lifespan → router can bind it
    app.include_router(create_stream_router(app.state.price_cache))
    # ... include portfolio / watchlist / chat / health routers ...
    # ... mount StaticFiles("static", html=True) at "/" LAST ...
    return app


app = create_app()
```

`python-dotenv` is not yet a dependency: `uv add python-dotenv`.

`get_tracked_tickers()` in SQL:

```sql
SELECT ticker FROM watchlist WHERE user_id = :uid
UNION
SELECT ticker FROM positions WHERE user_id = :uid
```

Order is irrelevant to the source; the watchlist endpoint orders by `added_at` itself.

Route handlers reach the market layer through `request.app.state.price_cache` / `request.app.state.market_source` (or small `Depends` helpers wrapping those). No module globals, so each test can build its own app.

### 10.2 Tracked-ticker bookkeeping — service layer

Only the service functions call `add_ticker` / `remove_ticker`, and they do so **after** the DB transaction commits (a failed transaction must not change what is priced).

```python
async def add_to_watchlist(db, source: MarketDataSource, ticker: str) -> bool:
    ticker = normalize_ticker(ticker)              # strip/upper, ^[A-Z.]{1,10}$ else ValueError("Invalid ticker")
    added = db.insert_watchlist(ticker)            # INSERT ... ON CONFLICT DO NOTHING → rowcount
    await source.add_ticker(ticker)                # idempotent; also covers "held but unwatched"
    return added                                   # route: 201 if added else 200


async def remove_from_watchlist(db, source: MarketDataSource, ticker: str) -> None:
    ticker = normalize_ticker(ticker)
    db.delete_watchlist(ticker)
    if not db.has_position(ticker):                # keep held tickers priced
        await source.remove_ticker(ticker)


async def execute_trade(db, cache: PriceCache, source: MarketDataSource,
                        ticker: str, side: str, quantity: float) -> Trade:
    ticker = normalize_ticker(ticker)
    price = cache.get_price(ticker)
    if price is None:
        raise TradeError(f"No price available for {ticker} — add it to the watchlist first")
    ...                                            # validate, one transaction, snapshot
    if side == "sell" and position_closed and not db.on_watchlist(ticker):
        await source.remove_ticker(ticker)
    return trade
```

A buy never needs `add_ticker`: it requires a cached price, which means the ticker is already tracked.

### 10.3 Readers

- **Valuation:** `current_price = cache.get_price(t) or avg_cost` (PLAN §8 fallback).
- **Watchlist GET:** for each row, `u = cache.get(t)`; emit `price`, `previous_price`, `session_change_percent`, `direction` from `u`, or all `null` if `u is None`.
- **Trade:** reads the price once, at validation time, and uses that same value for the fill.

---

## 11. Testing

Existing: 73 tests in `backend/tests/market/`, all passing. Changes and additions:

### 11.1 Cache / model (`test_cache.py`, `test_models.py`)

```python
def test_session_open_is_first_price():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 191.00)
    u = cache.update("AAPL", 189.10)
    assert u.session_open == 190.00
    assert u.session_change_percent == -0.4737
    assert u.to_dict()["session_open"] == 190.00


def test_remove_bumps_version_only_when_present():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v = cache.version
    cache.remove("AAPL")
    assert cache.version == v + 1
    cache.remove("AAPL")
    assert cache.version == v + 1


def test_readd_after_remove_starts_new_session():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.update("AAPL", 200.0).session_open == 200.0


def test_zero_timestamp_is_kept():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0
```

Update existing `PriceUpdate(...)` constructions in `test_models.py` to pass `session_open`.

### 11.2 Massive (`test_massive.py`) — use real SDK models, not `MagicMock`

```python
from massive.rest.models import LastTrade, TickerSnapshot


def _snap(ticker: str, price: float | None, sip_ns: int | None = 1_760_600_000_123_000_000):
    trade = None if price is None else LastTrade(price=price, sip_timestamp=sip_ns)
    return TickerSnapshot(ticker=ticker, last_trade=trade)


def test_parse_snapshot_converts_nanoseconds():
    assert parse_snapshot(_snap("AAPL", 191.74)) == ("AAPL", 191.74, 1760600000.123)


def test_parse_snapshot_missing_trade():
    assert parse_snapshot(_snap("AAPL", None)) is None


def test_parse_snapshot_without_any_timestamp_is_skipped():
    assert parse_snapshot(_snap("AAPL", 191.74, sip_ns=None)) is None


def test_parse_snapshot_from_raw_json():
    # Raw API keys (abbreviated), exactly as MASSIVE_API.md documents them
    snap = TickerSnapshot.from_dict({
        "ticker": "AAPL",
        "lastTrade": {"p": 191.74, "s": 100, "x": 4, "t": 1760600000123000000},
        "updated": 1760600000500000000,
    })
    assert parse_snapshot(snap) == ("AAPL", 191.74, 1760600000.123)


async def test_removed_mid_poll_not_reinserted():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._client, src._tickers = MagicMock(), ["AAPL"]

    def fetch(tickers):
        src._tickers = []          # simulate remove_ticker racing the thread
        return [_snap("AAPL", 190.0)]

    with patch.object(src, "_fetch_snapshots", side_effect=fetch):
        await src._poll_once()
    assert "AAPL" not in cache


@pytest.mark.parametrize("error", [BadResponse('{"status":"NOT_AUTHORIZED"}'),
                                   ConnectionError("network down")])
async def test_failed_poll_keeps_last_values(error):
    cache = PriceCache()
    cache.update("AAPL", 190.0, timestamp=1.0)
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._client, src._tickers = MagicMock(), ["AAPL"]
    with patch.object(src, "_fetch_snapshots", side_effect=error):
        await src._poll_once()     # must not raise
    assert cache.get_price("AAPL") == 190.0   # R14


async def test_ticker_missing_from_response_keeps_last_value():
    cache = PriceCache()
    cache.update("MSFT", 420.0, timestamp=1.0)
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._client, src._tickers = MagicMock(), ["AAPL", "MSFT"]
    with patch.object(src, "_fetch_snapshots", return_value=[_snap("AAPL", 190.0)]):
        await src._poll_once()
    assert cache.get_price("MSFT") == 420.0
```

Rewrite the existing `_make_snapshot` helper (`MagicMock` with `last_trade.timestamp` in ms) on top of `_snap`; it is the reason the §7.1 bug went unnoticed. As `MARKET_INTERFACE.md` puts it: never build snapshots from a bare `MagicMock()`. Use the real models, `TickerSnapshot.from_dict(...)` on raw JSON, or at minimum `MagicMock(spec=TickerSnapshot)`. `_fetch_snapshots` now takes the ticker list, so patched `return_value` mocks keep working but `side_effect` functions take one argument.

### 11.3 Factory (`test_factory.py`)

```python
def test_poll_interval_from_env(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MASSIVE_POLL_INTERVAL", "5")
    assert create_market_data_source(PriceCache())._interval == 5.0


@pytest.mark.parametrize("raw", ["", "abc", "0", "-3"])
def test_poll_interval_fallback(monkeypatch, raw):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MASSIVE_POLL_INTERVAL", raw)
    assert create_market_data_source(PriceCache())._interval == 15.0
```

### 11.4 SSE (`test_stream.py`, new)

Drive the generator directly with a fake request — no server needed:

```python
class FakeRequest:
    client = None
    def __init__(self):
        self.disconnected = False
    async def is_disconnected(self):
        return self.disconnected


async def test_stream_emits_retry_then_snapshot_then_removal():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    req = FakeRequest()
    gen = _generate_events(cache, req, interval=0.01)

    assert await anext(gen) == "retry: 1000\n\n"
    first = json.loads((await anext(gen)).removeprefix("data: "))
    assert first["AAPL"]["session_open"] == 190.0

    cache.remove("AAPL")
    assert json.loads((await anext(gen)).removeprefix("data: ")) == {}
    req.disconnected = True


async def test_no_event_without_change():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)
    await anext(gen); await anext(gen)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(gen), timeout=0.05)


def test_router_factory_is_reentrant():
    cache = PriceCache()
    r1, r2 = create_stream_router(cache), create_stream_router(cache)
    assert r1 is not r2
    assert len(r1.routes) == len(r2.routes) == 1
```

### 11.5 Simulator

Existing coverage is good. Add: `start([" aapl", "AAPL"])` tracks one ticker `AAPL`; a statistical check that the empirical correlation of log returns between AAPL and MSFT over ~2,000 steps (shocks disabled) is in `[0.5, 0.7]`; every price stays positive over a long run (GBM is multiplicative); an event rate near `event_probability` over many steps.

Keep simulator-specific guarantees (every tracked ticker priced on every tick) inside these simulator tests. Tests for anything downstream of the cache must also hold for Massive, where tickers can be missing (`MARKET_SIMULATOR.md`, R14).

### 11.6 Service-layer (lives with the portfolio/watchlist tests, PLAN §12)

- Remove a held ticker from the watchlist → still in `source.get_tickers()` and the cache.
- Sell an unwatched position to zero → removed from source and cache.
- Buy a ticker with no cached price → `"No price available for X — add it to the watchlist first"`.
- Run the watchlist/portfolio tests against a stub `MarketDataSource` that tracks tickers but never prices newly added ones (Massive-like), so `null` prices and the `avg_cost` fallback are covered.

---

## 12. Change checklist

Ordered for implementation; each item lists its tests.

| # | File | Change | Tests |
|---|---|---|---|
| 1 | `models.py` | add `session_open`, `session_change_percent`; extend `to_dict()` | 11.1, update `test_models.py` |
| 2 | `cache.py` | track session open; `remove()` bumps version + clears open; `is not None` timestamp; locked `version` | 11.1 |
| 3 | `massive_client.py` | **fix `sip_timestamp` (ns)**; `parse_snapshot()` (no wall-clock fallback); `BadResponse` vs network logging; ticker snapshot passed to thread; tracked re-check; start timeout; interval validation | 11.2 (real SDK models / `from_dict`) |
| 4 | `factory.py` | `MASSIVE_POLL_INTERVAL` with fallback; Starter+ hint in log | 11.3 |
| 5 | `stream.py` | router created per call; emit empty snapshot; `interval` param; let `CancelledError` propagate | 11.4 |
| 6 | `simulator.py` | normalize + dedupe tickers; fix TSLA comment | 11.5 |
| 7 | docs | `interface.py` and `backend/CLAUDE.md` (normalization, removal semantics, new fields, `remove()` bumps version); root `CLAUDE.md` points to `planning/` instead of the removed `planning/archive`; `MARKET_DATA_SUMMARY.md` mentions this design | — |
| 8 | repo root | `db/.gitkeep`; `db/*.db` in `.gitignore`; commit `.env.example` with the Starter+ note (PLAN §5, §8 here) | — |
| 9 | `pyproject.toml` | `uv add python-dotenv` (used by `main.py`, §10.1) | — |

Then run:

```bash
cd backend
uv run --extra dev pytest -q
uv run --extra dev ruff check app/ tests/
```

And a manual Massive smoke test with a Starter+ key (the free Basic plan cannot use the snapshot endpoint — see `MASSIVE_API.md`). Outside US market hours expect old timestamps or, between 12am and ~4am ET, no prices at all:

```bash
MASSIVE_API_KEY=... MASSIVE_POLL_INTERVAL=5 uv run python -c "
import asyncio
from app.market import PriceCache, create_market_data_source
async def main():
    c = PriceCache(); s = create_market_data_source(c)
    await s.start(['AAPL', 'MSFT']); await asyncio.sleep(6)
    print({t: u.to_dict() for t, u in c.get_all().items()}); await s.stop()
asyncio.run(main())"
```
