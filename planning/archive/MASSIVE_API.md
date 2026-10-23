# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

*Last verified: 2026-10-23 against the `massive` 2.9.0 SDK source and massive.com docs/pricing. The project lockfile pins `massive` 2.2.0; the models used below are identical in both versions.*

## Overview

- **Rebrand**: Polygon.io became Massive on 2025-10-30. Existing API keys stay valid
- **Base URL**: `https://api.massive.com` (the SDK default). Legacy `https://api.polygon.io` still works in parallel
- **Python package**: `massive` (`uv add massive`). Latest is 2.9.0
- **Min Python version**: 3.10+ (as of 2.9.0; FinAlly uses 3.12)
- **Auth**: API key via the `MASSIVE_API_KEY` env var or `RESTClient(api_key=...)`. If neither is set, the constructor raises `massive.exceptions.AuthError`
- **Auth header**: `Authorization: Bearer <API_KEY>` (the client adds this automatically)

## Plans, Rate Limits and Endpoint Access

Individual stock plans (from massive.com/pricing):

| Plan | Price | API calls | Data recency | Snapshot | Last Trade | Last Quote |
|------|-------|-----------|--------------|----------|------------|------------|
| Basic | Free | 5 / minute | End of day | ✗ | ✗ | ✗ |
| Starter | $29/mo | Unlimited | 15-min delayed | ✓ | ✗ | ✗ |
| Developer | $79/mo | Unlimited | 15-min delayed | ✓ | ✓ | ✗ |
| Advanced | $199/mo | Unlimited | Real-time | ✓ | ✓ | ✓ |

Previous Close and Aggregates are included in every plan, Basic included. For unlimited plans, staying under ~100 req/s is recommended.

**Implication for FinAlly:** the poller uses the snapshot endpoint, which **the free Basic plan does not include**. A free key returns a non-200 response (the SDK raises `BadResponse`), and the poller logs it each cycle. Prices will not update. Live polling needs Starter or higher, and even then data is 15 minutes delayed until Advanced. With the 15s default `MASSIVE_POLL_INTERVAL`, one snapshot call per poll is 4 calls/min, so the interval would fit the Basic limit if the endpoint were available. Paid tiers can poll every 2–5s.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass explicitly (other kwargs: retries=3, base="https://api.massive.com",
# connect_timeout, read_timeout, trace, ...)
client = RESTClient(api_key="your_key_here")
```

The client is **synchronous** (urllib3). In async code, call it via `asyncio.to_thread(...)`.

## Raw JSON vs SDK Models

The REST API returns **abbreviated keys** (`p`, `t`, `c`, `prevDay`, `todaysChangePerc`, ...). The SDK maps them to descriptive attribute names. The tables below give both. Use the **SDK attribute** column in Python code.

## Endpoints Used in FinAlly

### 1. Snapshot — Multiple Tickers (Primary Endpoint)

Gets current data for many tickers in a **single API call**. This is the endpoint the poller uses.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`
- `tickers`: comma-separated list (omitted = the whole market)
- `include_otc`: include OTC securities (default `false`)

**Plans**: Starter and above (not Basic).

**Python client**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

# A list is joined into a comma-separated string by the SDK
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:  # list[TickerSnapshot]
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change: {snap.todays_change_percent}%")
    print(f"  Prev close: {snap.prev_day.close}")
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Volume: {snap.day.volume}")
```

**Raw response** (`tickers` array; one element shown):
```json
{
  "count": 1,
  "status": "OK",
  "tickers": [{
    "ticker": "AAPL",
    "day":       {"o": 129.61, "h": 130.15, "l": 125.07, "c": 125.07, "v": 111237700, "vw": 127.35},
    "prevDay":   {"o": 128.90, "h": 130.00, "l": 128.10, "c": 129.61, "v": 98000000, "vw": 129.20},
    "min":       {"o": 125.10, "h": 125.12, "l": 125.05, "c": 125.07, "v": 5000, "av": 111237700, "t": 1684428600000},
    "lastTrade": {"p": 125.07, "s": 100, "x": 4, "t": 1675190399000000000},
    "lastQuote": {"p": 125.06, "s": 5, "P": 125.08, "S": 10, "t": 1675190399500000000},
    "todaysChange": -4.54,
    "todaysChangePerc": -3.50,
    "updated": 1675190399000000000
  }]
}
```

**`TickerSnapshot` attributes** (SDK model):

| SDK attribute | JSON key | Type / notes |
|---|---|---|
| `ticker` | `ticker` | str |
| `day` | `day` | `Agg`: `open`, `high`, `low`, `close`, `volume`, `vwap`, `timestamp`, `transactions`, `otc` |
| `prev_day` | `prevDay` | `Agg` (same fields): the previous session's bar |
| `min` | `min` | `MinuteSnapshot`: most recent minute bar, plus `accumulated_volume` |
| `last_trade` | `lastTrade` | `LastTrade`: `price`, `size`, `exchange` (int ID), `sip_timestamp`, `participant_timestamp`, `trf_timestamp`, `conditions`, ... |
| `last_quote` | `lastQuote` | `LastQuote`: `bid_price`, `bid_size`, `ask_price`, `ask_size`, `sip_timestamp`, ... |
| `todays_change` | `todaysChange` | float, vs previous close |
| `todays_change_percent` | `todaysChangePerc` | float, percent |
| `updated` | `updated` | int, **nanoseconds** |
| `fair_market_value` | `fmv` | Business plans only |

**Gotchas** (each was wrong in an earlier version of this doc):
- `day` has **no** `previous_close`, `change` or `change_percent`. Use `prev_day.close` and `todays_change` / `todays_change_percent`
- `LastTrade` has **no** `timestamp` attribute. Use `last_trade.sip_timestamp`, which is in **nanoseconds** (divide by `1e9` for Unix seconds)
- `last_trade.exchange` is an integer exchange ID, not a MIC string such as `"XNYS"`
- The quote has no `spread` field. Compute `ask_price - bid_price` if needed
- Every model field is `Optional`, so sub-objects (`last_trade`, `day`, ...) can be `None` for thinly traded or newly listed tickers

### 2. Single Ticker Snapshot

Detailed data for one ticker. Returns one `TickerSnapshot`.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`

```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"Price: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"Day range: ${snapshot.day.low} - ${snapshot.day.high}")
```

### 3. Previous Close

Previous day's OHLC for a ticker. Available on **all plans**, including Basic. Useful for seed prices.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

```python
prev = client.get_previous_close_agg(ticker="AAPL")  # list[PreviousCloseAgg]

for agg in prev:
    print(f"Previous close: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume}  VWAP: {agg.vwap}")
```

**Raw response**:
```json
{
  "adjusted": true,
  "queryCount": 1,
  "resultsCount": 1,
  "status": "OK",
  "ticker": "AAPL",
  "results": [
    {"T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97,
     "v": 131704427, "vw": 116.3058, "t": 1605042000000}
  ]
}
```

### 4. Aggregates (Bars)

Historical OHLCV bars over a date range. Live polling doesn't need it, but it would serve historical charts. Available on all plans; Basic gets 2 years of history.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

```python
aggs = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2026-01-01",
    to="2026-01-31",
    limit=50000,
))  # iterator of Agg; the SDK follows pagination automatically

for a in aggs:
    print(f"t={a.timestamp} O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

**Raw bar**: `{"o": 130.0, "h": 132.5, "l": 129.8, "c": 131.2, "v": 50000000, "vw": 131.0, "n": 412345, "t": 1672531200000}`

### 5. Last Trade / Last Quote

Endpoints for the most recent trade or NBBO quote of a single ticker. Last Trade needs the **Developer** plan or higher. Last Quote needs **Advanced**, the only individual plan that includes quotes.

**REST**: `GET /v2/last/trade/{ticker}`, `GET /v2/last/nbbo/{ticker}`

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price} x {trade.size} @ {trade.sip_timestamp} ns")

quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid_price} x {quote.bid_size}")
print(f"Ask: ${quote.ask_price} x {quote.ask_size}")
```

### Also available: Unified Snapshot

`GET /v3/snapshot` (`client.list_universal_snapshots(ticker_any_of=[...])`) returns snapshots across asset classes (stocks, options, forex, crypto, indices) in one paginated call. FinAlly doesn't need it; `get_snapshot_all` is enough for US stocks.

## How FinAlly Uses the API

Implementation: `backend/app/market/massive_client.py` (`MassiveDataSource`). Each poll cycle:

1. Takes the tracked tickers (watchlist ∪ open positions)
2. Calls `get_snapshot_all()` for all of them in one request, run via `asyncio.to_thread` because the SDK is synchronous
3. Extracts `last_trade.price` and `last_trade.sip_timestamp` (ns → seconds) from each snapshot
4. Writes them to the shared in-memory `PriceCache`. Session open is the first price seen
5. Sleeps `MASSIVE_POLL_INTERVAL` seconds (default 15), then repeats

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    """Poll Massive API and update the price cache."""
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            snapshots = await asyncio.to_thread(
                client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snapshots:
                trade = snap.last_trade
                if trade is None or trade.price is None:
                    continue
                price_cache.update(
                    ticker=snap.ticker,
                    price=trade.price,
                    timestamp=trade.sip_timestamp / 1e9,  # nanoseconds → seconds
                )

        await asyncio.sleep(interval)
```

## Error Handling

- **No API key**: `RESTClient()` raises `massive.exceptions.AuthError` at construction
- **Any non-200 response**: the SDK raises `massive.exceptions.BadResponse` with the response body as the message. There are no per-status exception classes; distinguish by message or catch `BadResponse` broadly. Typical causes:
  - **401**: invalid API key
  - **403**: the plan doesn't include the endpoint, e.g. snapshots on the free Basic plan
  - **429**: rate limit exceeded (Basic: 5 req/min)
- **Built-in retries**: urllib3 `Retry(total=3, backoff_factor=0.1)` on status 413, 429, 499, 500, 502, 503, 504. A `BadResponse` reaches your code only after the retries are exhausted

## Notes

- The multi-ticker snapshot returns **all requested tickers in one call**, which keeps polling inside rate limits
- **Timestamp units differ by field**:
  - trades, quotes and `updated` (`lastTrade.t`, `lastQuote.t`, `sip_timestamp`) are in **nanoseconds**
  - aggregate bars (`day`/`prevDay`/`min.t`, `/prev`, `/range`) are in **milliseconds**
- Snapshot data is **cleared at 12am ET** and repopulates as exchange data arrives, from as early as 4am ET. Overnight, `day` and `last_trade` may be empty or `None`, so handle missing sub-objects
- After hours, `last_trade.price` reflects the last trade, which may be an extended-hours trade
- Starter and Developer data is **15 minutes delayed**. Only Advanced and Business plans get real-time data
