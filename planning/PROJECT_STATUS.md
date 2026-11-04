# FinAlly — Project Status

*Snapshot as of 2026-11-03 (`main` @ `710c13e`). Compared against `PLAN.md`, the market-data planning docs and the code in the repo.*

## TL;DR

Only the **market data subsystem** (`backend/app/market/`) exists. It's in good shape: 96 tests pass and `ruff` is clean. The PLAN §13 follow-ups and the market-data design checklist are applied. Everything else in PLAN.md hasn't been started: the FastAPI app, database, portfolio/watchlist/chat services, LLM integration, frontend, Docker, scripts and E2E tests.

---

## 1. What's done

### Market data backend: `backend/app/market/` (~800 LOC, 8 modules)

| Module | Status |
|---|---|
| `models.py`: `PriceUpdate` (frozen dataclass) | Done. Includes `session_open`, `session_change_percent` and `to_dict()` matching the PLAN §6 SSE shape |
| `cache.py`: `PriceCache` | Done. Thread-safe, version counter, tracks session open per ticker |
| `interface.py`: `MarketDataSource` ABC | Done |
| `seed_prices.py` | Done. Realistic seeds, per-ticker drift/vol, sector correlation groups |
| `simulator.py`: GBM + Cholesky-correlated moves + random 2–5% shocks | Done. New tickers are priced immediately |
| `massive_client.py`: Polygon/Massive REST poller | Done. Parses `sip_timestamp` (ns) via `parse_snapshot()` |
| `factory.py` | Done. Picks the source from `MASSIVE_API_KEY` and reads `MASSIVE_POLL_INTERVAL` (default 15, falls back on invalid values) |
| `stream.py`: `GET /api/stream/prices` SSE | Done. Sends `retry: 1000`, ticks every 500 ms, emits only on version change, and creates the router inside the factory |

**Tests:** `backend/tests/market/` has 7 modules and **96 tests, all passing** (`uv run --extra dev pytest -q`). `ruff check` passes.

**Demo:** `backend/market_data_demo.py` is a Rich terminal dashboard.

### PLAN §13 follow-ups

| # | Item | Status |
|---|---|---|
| 1 | Session open price in cache + `to_dict()` | ✅ Done, with tests |
| 2 | SSE router created inside the factory | ✅ Done (`test_stream.py`) |
| 3 | `MASSIVE_POLL_INTERVAL` read by the factory | ✅ Done |
| 4 | `db/.gitkeep`, `db/*.db` gitignored, `.env.example` committed | ✅ Done |

### Repo / tooling

- `backend/` uv project (`pyproject.toml`, `uv.lock`), plus `backend/CLAUDE.md` and `README.md`
- Root `README.md`, `LICENSE`, `.gitignore`, `.env.example`
- GitHub workflows: `claude.yml` (`@claude` mentions) and `claude-code-review.yml` (PR review)
- Project skill `.claude/skills/cerebras-inference` for the LLM integration
- Planning docs: `PLAN.md`, `MARKET_DATA_DESIGN.md` (target design and change checklist), `MARKET_DATA_SUMMARY.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, `REVIEW.md`

---

## 2. Open issues in the existing market data code

### 2.1 / 2.2 Defects and design checklist: ✅ resolved (2026-11-03, branch `fix/market-data-defects`)

All items from `MARKET_DATA_DESIGN.md` §12 are now applied:

- **Massive parsing fixed.** New `parse_snapshot()` reads `last_trade.sip_timestamp` (ns), falls back to `snap.updated` then wall clock, and explicitly skips a missing trade, a missing or non-positive price, or a missing ticker. Tests now build snapshots from the real SDK models (`TickerSnapshot`, `LastTrade`) instead of `MagicMock`.
- **`PriceCache.remove()` bumps `version`** when the ticker was present, so SSE sends the removal on the next tick.
- `massive_client.py`: the ticker list is copied before going to the worker thread; a ticker removed mid-poll isn't put back into the cache; the initial poll in `start()` has a timeout (`start_timeout`, 10 s); a non-positive poll interval raises `ValueError`; `start()` normalizes and dedupes tickers.
- `cache.py`: `timestamp is not None` (a `0.0` timestamp is kept), and `version` is read under the lock.
- `stream.py`: an empty cache is sent as `{}`; `CancelledError` is re-raised instead of swallowed.
- `simulator.py`: tickers are normalized (stripped, uppercased) and deduped in `start()`, `add_ticker()` and `remove_ticker()`.
- `interface.py` docstrings, `backend/CLAUDE.md` and `backend/README.md` (`uv sync --extra dev`) are updated.
- `python-dotenv` added to `pyproject.toml`. `litellm` comes with the chat work.

**Tests:** 96 passing (was 78). `ruff check` is clean.

Not done: the manual Massive smoke test needs a Starter+ API key. The statistical simulator correlation test (§11.5) was skipped as optional.

### 2.3 Documentation inconsistencies

- `CLAUDE.md` and `MARKET_DATA_DESIGN.md` point to `planning/archive/`, but that folder doesn't exist. The archived docs (`MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`) are in `planning/` directly. Either move them or fix the references.
- `MARKET_DATA_SUMMARY.md` says "73 tests" and "all issues resolved". It's out of date: there are 96 tests now.
- `REVIEW.md`: its P1 about `.env.example` is resolved. Its P2 refers to `.claude/agents/change-reviewer.md`, which no longer exists. Its remaining P1 (cache removal → SSE) is now fixed.
- Root `README.md` doesn't list `MASSIVE_POLL_INTERVAL`.

---

## 3. What's missing (not started)

### 3.1 Backend application (PLAN §3, §7, §8, §9)

- [ ] **`app/main.py`**: FastAPI app factory and lifespan (`load_dotenv`, DB init, start the market source with watchlist ∪ positions, snapshot task, mount static files last). `MARKET_DATA_DESIGN.md` §10 has a ready-made sketch.
- [ ] **Database layer** (`backend/db/` + `app/db`): schema for `users_profile`, `watchlist`, `positions`, `trades`, `portfolio_snapshots`, `chat_messages`; lazy init and seeding ($10k cash and the 10 default tickers); `get_tracked_tickers()`.
- [ ] **Service layer** (PLAN §3): `execute_trade()`, `add_to_watchlist()`, `remove_from_watchlist()`, `get_portfolio()`, plus tracked-ticker bookkeeping (§6) and trade rules (§7):
  - 4-dp quantities
  - ticker regex
  - weighted avg cost
  - delete the position below 1e-9
  - one transaction per trade, including the snapshot
- [ ] **Portfolio snapshots**: one at startup, one every 30 s, one after each trade. `/history` caps at 1,000 rows.
- [ ] **REST routes** (PLAN §8):
  - `GET/POST /api/portfolio`, `/trade`, `/history`
  - `GET/POST/DELETE /api/watchlist`
  - `POST /api/chat`, `GET /api/chat/history`
  - `GET /api/health`
- [ ] **LLM chat** (PLAN §9): LiteLLM → OpenRouter `openai/gpt-oss-120b` via Cerebras (use the `cerebras-inference` skill), structured output, prompt with portfolio context and the last 20 messages including actions, auto-execution (watchlist first, then trades, partial success), persistence, error messages that always return 200, and `LLM_MOCK` regex rules.
- [ ] Dev CORS flag (optional, only if the Next dev proxy buffers SSE).
- [ ] Backend tests: portfolio math and edge cases, tracked tickers, LLM parsing/mock/errors, and the API contract.

### 3.2 Frontend: `frontend/` (PLAN §10), not created

- [ ] Next.js + TypeScript + Tailwind project with `output: 'export'` and dev-only `rewrites()` for `/api/*`
- [ ] Price store: a single `EventSource`, ~500-point ring buffer per ticker, connection status dot
- [ ] Watchlist panel (price flash, session change %, sparklines, add/remove)
- [ ] Main chart for the selected ticker
- [ ] Portfolio heatmap (Recharts `Treemap`), P&L line chart, positions table
- [ ] Trade bar with inline errors
- [ ] AI chat panel (history on load, loading indicator, inline action results)
- [ ] Header (live total value, cash, connection dot)
- [ ] `usePortfolio` store, refetching after trades and chat responses
- [ ] Dark theme using the PLAN §2 colors
- [ ] Frontend unit tests

### 3.3 Packaging and deployment (PLAN §11)

- [ ] Multi-stage `Dockerfile` (Node 20 build → Python 3.12 + uv, static output in `static/`)
- [ ] `scripts/start_mac.sh` / `scripts/stop_mac.sh` (idempotent, container named `finally`, volume `finally-data`)
- [ ] Optional: `deploy/` Terraform for App Runner

### 3.4 E2E tests: `test/` (PLAN §12), not created

- [ ] Playwright project run from the host against the container with `LLM_MOCK=true`
- [ ] Scenarios:
  - fresh start
  - watchlist add/remove
  - buy/sell
  - heatmap and P&L chart
  - mocked chat trade
  - SSE reconnect

---

## 4. Suggested next steps

1. ~~Fix §2.1 and the design checklist (§12).~~ ✅ Done.
2. **Next:** build the backend: DB schema and seed → service layer → routes → `main.py` lifespan → snapshot task, with pytest coverage.
3. Add the LLM chat with mock mode first, then the real Cerebras/OpenRouter call.
4. Scaffold the frontend against the API contract (it can be built in parallel with step 2).
5. Add the Dockerfile and start/stop scripts, then the Playwright E2E suite.
6. Clean up the docs (§2.3).
