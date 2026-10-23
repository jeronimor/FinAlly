# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background tasks: market data sim/poller,      │
│                    portfolio snapshots          │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

### One Service Layer

All business rules live in backend service functions — `execute_trade()`, `add_to_watchlist()`, `remove_from_watchlist()`, `get_portfolio()`. Both the REST routes and the LLM chat flow call these same functions, so validation, persistence and tracked-ticker bookkeeping exist in exactly one place.

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── db/                   # Schema definitions, seed data, init logic
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   └── stop_mac.sh           # Stop Docker container (macOS/Linux)
├── test/                     # Playwright E2E tests (run from the host)
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; db/*.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored)
├── .env.example              # Committed template for .env
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/db/`** contains schema SQL definitions and seed logic. The backend initializes the database on startup — creating tables and seeding default data if the SQLite file doesn't exist or is empty.
- **`db/`** at the top level is the runtime volume mount point. The SQLite file (`db/finally.db`) is created here by the backend and persists across container restarts via Docker volume.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests, run from the host against the running container. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required for real LLM chat: OpenRouter API key
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Massive poll interval in seconds (default 15 = free tier; paid tiers can use 2-15)
MASSIVE_POLL_INTERVAL=15

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false
```

`.env.example` with these keys (and empty secrets) is committed; `.env` is gitignored.

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data, polling every `MASSIVE_POLL_INTERVAL` seconds
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (see §9)
- If `LLM_MOCK` is not true and `OPENROUTER_API_KEY` is missing → the app runs normally; chat replies with a clear "LLM not configured" assistant message instead of erroring
- **Loading**: in Docker, variables come from `docker run --env-file .env`. For local development the backend calls `load_dotenv()` (python-dotenv) on the project-root `.env` if present; real environment variables take precedence.

---

## 6. Market Data

*Implemented — see `planning/MARKET_DATA_SUMMARY.md`. Follow-up changes are listed in §13.*

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.); unknown tickers get a random $50–300 seed price
- A newly added ticker has a price in the cache immediately
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for all tracked tickers every `MASSIVE_POLL_INTERVAL` seconds (default 15s for the free tier's 5 calls/min)
- A newly added ticker has no price until the next poll completes
- Parses REST response into the same format as the simulator

### Tracked Tickers

The set of tickers the data source prices is **watchlist ∪ tickers with an open position**. This guarantees every held position always has a live price for valuation and selling.

- On startup, the backend starts the data source with that union, read from the database
- Adding to the watchlist → `source.add_ticker()`
- Removing from the watchlist → `source.remove_ticker()` **only if** no position is held in that ticker
- Selling a position to zero → `source.remove_ticker()` **only if** the ticker is not on the watchlist

This bookkeeping lives in the service layer (§3), not in the routes.

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds, per ticker: latest price, previous price, timestamp, and **session open** — the first price seen for that ticker since the server started
- Session open is the reference for the watchlist's change % ("since session start"). It is source-agnostic: the simulator's seed price, or Massive's first polled price
- SSE streams, portfolio valuation and trade execution all read from this cache

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- The first message is `retry: 1000` (browser reconnects after 1s on disconnect)
- The server checks the cache every ~500ms and, **only if anything changed**, sends one event containing **all** tracked tickers, keyed by ticker:

```
data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.41,
                "session_open": 190.00, "timestamp": 1760600000.123,
                "change": 0.11, "change_percent": 0.0578,
                "session_change_percent": 0.2737, "direction": "up"}, ...}
```

- `timestamp` is Unix seconds (float), not ISO
- `change` / `change_percent` are tick-to-tick (drive flash animations); `session_change_percent` is the watchlist's change % column
- `direction` is `"up"`, `"down"` or `"flat"`
- Under Massive, events arrive roughly once per poll interval rather than every 500ms

---

## 7. Database

### SQLite with Lazy Initialization

The backend initializes the database at startup. If the file doesn't exist or tables are missing, it creates the schema (`CREATE TABLE IF NOT EXISTS`) and seeds default data. This means:

- No separate migration step
- No manual database setup
- Fresh Docker volumes start with a clean, seeded database automatically

### Schema

Every table except `users_profile` has a `user_id` column defaulting to `"default"`; in `users_profile`, the `id` column *is* the user id. This is hardcoded for now (single-user) but enables future multi-user support without schema migration.

Tables that hold one row per user per ticker use the natural composite key `(user_id, ticker)` instead of a UUID, which makes upserts (`INSERT ... ON CONFLICT DO UPDATE`) trivial. Append-only tables use UUID ids.

**users_profile** — User state (cash balance)
- `id` TEXT PRIMARY KEY (default: `"default"`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT (ISO timestamp)

**watchlist** — Tickers the user is watching
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `added_at` TEXT (ISO timestamp)
- PRIMARY KEY `(user_id, ticker)`

**positions** — Current holdings (one row per ticker per user)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT (ISO timestamp)
- PRIMARY KEY `(user_id, ticker)`

**trades** — Trade history (append-only log)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL (fractional shares supported)
- `price` REAL
- `executed_at` TEXT (ISO timestamp)

**portfolio_snapshots** — Portfolio value over time (for P&L chart)
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `total_value` REAL
- `recorded_at` TEXT (ISO timestamp)

**chat_messages** — Conversation history with LLM
- `id` TEXT PRIMARY KEY (UUID)
- `user_id` TEXT (default: `"default"`)
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — executed/failed trades and watchlist changes, in the same shape as the `POST /api/chat` response; null for user messages)
- `created_at` TEXT (ISO timestamp)

### Default Seed Data

- One user profile: `id="default"`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

### Trade Rules

- `quantity` must be `> 0`; fractional shares allowed, rounded to 4 decimal places
- Tickers are normalized to uppercase and must match `^[A-Z.]{1,10}$`
- A trade requires a cached price for the ticker; otherwise it fails with "No price available for X — add it to the watchlist first"
- **Buy**: requires `cash ≥ quantity × price`. New `avg_cost` = weighted average of the existing position and the new shares
- **Sell**: requires `quantity ≤ held quantity`. `avg_cost` is unchanged on partial sells. When the remaining quantity is below `1e-9`, the position row is deleted
- Realized P&L is not tracked (the `trades` log contains enough to derive it later if wanted)
- Each trade (cash update + position upsert/delete + trade log row + snapshot) runs in **one SQLite transaction**

### Portfolio Snapshots

- One snapshot at startup, one every 30 seconds (background task), and one immediately after each trade
- Total value = cash + Σ(quantity × current cached price); snapshots use the same `get_portfolio()` function as the API
- No retention pruning (~2,880 rows/day is fine for SQLite). `GET /api/portfolio/history` returns at most the latest 1,000 snapshots

---

## 8. API Endpoints

All request and response bodies are JSON. Errors use FastAPI's standard shape: `{"detail": "Human-readable message"}`. Timestamps in REST responses are ISO 8601 strings.

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates (format in §6) |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade |
| GET | `/api/portfolio/history` | Portfolio value snapshots over time (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |
| GET | `/api/chat/history` | Recent conversation for the chat panel on page load |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### API Contract

**`GET /api/portfolio`** → `200`
```json
{
  "cash": 8095.00,
  "total_value": 10012.40,
  "positions": [
    {"ticker": "AAPL", "quantity": 10, "avg_cost": 190.50, "current_price": 191.74,
     "market_value": 1917.40, "unrealized_pnl": 12.40, "unrealized_pnl_pct": 0.65}
  ]
}
```
If a held ticker has no cached price yet, `current_price` falls back to `avg_cost`.

**`POST /api/portfolio/trade`** — body `{"ticker": "AAPL", "side": "buy", "quantity": 10}`
- `200` → `{"trade": {"id", "ticker", "side", "quantity", "price", "executed_at"}, "portfolio": <same shape as GET /api/portfolio>}`
- `400` → validation failure: `{"detail": "Insufficient cash"}`, `"Insufficient shares"`, `"Quantity must be positive"`, `"Invalid ticker"`, `"No price available for X — add it to the watchlist first"`

**`GET /api/portfolio/history`** → `200` `[{"total_value": 10000.0, "recorded_at": "2026-10-16T12:00:00Z"}, ...]` (oldest first, max 1,000)

**`GET /api/watchlist`** → `200`
```json
[{"ticker": "AAPL", "price": 191.74, "previous_price": 191.70, "session_change_percent": 0.92, "direction": "up"}]
```
Price fields are `null` until the ticker has a cached price. Ordered by `added_at`.

**`POST /api/watchlist`** — body `{"ticker": "PYPL"}`
- `201` → `{"ticker": "PYPL"}` when added
- `200` → `{"ticker": "PYPL"}` when already present (idempotent)
- `400` → `{"detail": "Invalid ticker"}`

Any ticker matching the format rule is accepted. The simulator prices it immediately; under Massive an invalid symbol simply shows `null` prices. This is intended behavior.

**`DELETE /api/watchlist/{ticker}`** → `204` (also `204` if the ticker wasn't on the watchlist — idempotent)

**`POST /api/chat`** — body `{"message": "Buy 5 NVDA"}` → `200`
```json
{
  "message": "Done — bought 5 NVDA at $880.10.",
  "trades": [
    {"ticker": "NVDA", "side": "buy", "quantity": 5, "status": "executed", "price": 880.10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add", "status": "failed", "error": "Invalid ticker"}
  ]
}
```
`status` is `"executed"` or `"failed"`; `price` is present only on executed trades, `error` only on failures. The endpoint always returns `200` with an assistant message, including on LLM errors (§9).

**`GET /api/chat/history?limit=50`** → `200` `[{"id", "role", "content", "actions", "created_at"}, ...]` (oldest first; `actions` is the parsed JSON object or `null`)

**`GET /api/health`** → `200` `{"status": "ok"}`

---

## 9. LLM Integration

When writing code to make calls to LLMs, use the cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Stores the user message in `chat_messages`
2. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
3. Loads the last 20 messages from `chat_messages`, including each assistant message's stored `actions` (so the LLM knows what it already executed and what failed)
4. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
5. Calls the LLM via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill
6. Parses the complete structured JSON response
7. Auto-executes actions through the shared service functions (§3): **watchlist changes first, then trades in order**. Each trade is validated against the state left by the previous one; failures don't stop later actions (partial success is allowed and reported per action)
8. Stores the assistant message and the action results in `chat_messages`
9. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. `quantity` must be a positive number — the LLM computes it from the portfolio context (e.g., "sell all" → the held quantity). Each trade goes through the same validation as manual trades
- `watchlist_changes` (optional): Array of `{ticker, action: "add" | "remove"}`

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

If an action fails validation (e.g., insufficient cash), it is reported with `status: "failed"` and an `error` in the response. The LLM's `message` was written before execution, so the frontend shows action results inline beneath the message as confirmations or errors; the stored results let the LLM acknowledge failures on the next turn.

### Error Handling

The chat endpoint never returns a 5xx for LLM problems. In every case below it returns `200` with an assistant `message`, empty action arrays, logs the error, and stores both messages:

- `OPENROUTER_API_KEY` missing (and not mock mode) → "The AI assistant isn't configured. Add OPENROUTER_API_KEY to .env to enable chat."
- Network/API error or timeout → "Sorry, I couldn't reach the AI service. Please try again."
- Response doesn't parse against the schema → "Sorry, I had trouble with that request. Please try rephrasing."

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Manage the watchlist proactively
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter, then runs them through the normal execution path (step 7 onwards). This enables fast, free, reproducible E2E tests, development without an API key, and CI.

Mock rules (case-insensitive, first match wins; tickers are uppercased):

| User message matches | Mock response |
|---|---|
| `\b(buy\|sell)\s+(\d+(?:\.\d+)?)\s+([a-z.]+)` | `message: "Executing your {side} of {qty} {TICKER}."`, `trades: [{ticker, side, quantity}]` |
| `\b(add\|remove)\s+([a-z.]+)` | `message: "Updating your watchlist: {action} {TICKER}."`, `watchlist_changes: [{ticker, action}]` |
| anything else | `message: "Hello! I'm FinAlly, your AI trading assistant (mock mode)."`, no actions |

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), change % since session start (`session_change_percent`), and a sparkline mini-chart (accumulated from SSE since page load). Tickers without a price yet show "—"
- **Main chart area** — larger chart for the currently selected ticker, with at minimum price over time since page load. Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using `GET /api/portfolio/history`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill. Validation errors (`detail`) shown inline.
- **AI chat panel** — docked/collapsible sidebar. Loads prior conversation from `GET /api/chat/history` on page load. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Executed and failed actions shown inline beneath the assistant message.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### State & Data Flow

- **Prices**: one `EventSource` feeds a single price store. The store keeps a per-ticker ring buffer (last ~500 points) that powers both the sparklines and the main chart
- **Portfolio**: one store/hook (e.g., `usePortfolio`) holds the last `GET /api/portfolio` response. The header, positions table and heatmap all derive from it plus live SSE prices: market value = quantity × live price, total value = cash + Σ market values. No polling
- **Refetch** portfolio (and history) after every manual trade and every chat response; refetch the watchlist after any chat response that includes watchlist changes

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`; map `EventSource` states to the connection dot (open = green, connecting = yellow, closed = red)
- **Charting: Recharts** for everything — sparklines, main price chart, P&L line chart, and the treemap (`<Treemap>`). One library, one API; SVG performance is fine at this data volume. Disable chart animations for streaming data
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls use relative paths (`/api/*`) — same origin in production, no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

### Local Development

During development the frontend runs on `next dev` (port 3000) and the backend on uvicorn (port 8000). `next.config` defines `rewrites()` that proxy `/api/*` to `http://localhost:8000` **only in development** (rewrites are not supported by `output: 'export'`, so they must be conditional). If the dev proxy buffers SSE, the price hook may point `EventSource` directly at `http://localhost:8000` in development, with the backend enabling permissive CORS only when a dev flag is set.

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 20 slim
  - Copy frontend/
  - npm ci && npm run build (produces static export in out/)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync --frozen (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app
```

FastAPI serves all API routes on port 8000, and mounts the static frontend last (`StaticFiles(directory="static", html=True)` at `/`) so `/api/*` routes always take precedence.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The backend writes `finally.db` to `/app/db` in the container.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Builds the Docker image if not already built (or if `--build` flag passed)
- Runs the container (fixed name `finally`) with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

All scripts should be idempotent — safe to run multiple times.

### Optional Cloud Deployment

The container is designed to deploy to AWS App Runner, Render, or any container platform. A Terraform configuration for App Runner may be provided in a `deploy/` directory as a stretch goal, but is not part of the core build.

**Caveat:** there is no authentication. A public deployment lets anyone with the URL trade the shared portfolio and spend the owner's OpenRouter credits. Deploy with `LLM_MOCK=true` or behind platform-level access control.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface *(done)*
- Portfolio: trade execution logic, weighted avg cost, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss, selling to zero, fractional quantities, no cached price)
- Tracked tickers: removing a held ticker from the watchlist keeps it priced; selling an unwatched position to zero stops tracking it
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses and API errors, partial-success execution, mock-mode rules
- API routes: status codes and response shapes per the §8 contract

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations (live total value from SSE prices)
- Chat message rendering, inline action results, and loading state

### E2E Tests (in `test/`)

**Infrastructure**: Playwright runs on the host (`npx playwright test` from `test/`) against the app container started with `LLM_MOCK=true`. This keeps browser dependencies out of the production image without extra compose files.

**Environment**: Tests run with `LLM_MOCK=true` and a fresh volume (or fresh `db/`) for determinism.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send "buy 1 AAPL", receive a response, trade execution appears inline and the position appears
- SSE resilience: disconnect and verify reconnection

---

## 13. Follow-ups for Already-Built Code

The market data component was built before these decisions were made. The backend agent should make these changes first:

1. **Session open price** — `PriceCache` records `session_open` (the first price seen per ticker), and `PriceUpdate.to_dict()` adds `session_open` and `session_change_percent` (§6). Add tests.
2. **SSE router factory** — `app/market/stream.py` decorates a module-level `router` inside `create_stream_router()`, so calling it twice registers the route twice. Create the `APIRouter` inside the factory.
3. **Massive poll interval** — `create_market_data_source()` reads `MASSIVE_POLL_INTERVAL` (default 15) and passes it to `MassiveDataSource`.
4. **Repo housekeeping** — create `db/.gitkeep`, add `db/*.db` to `.gitignore`, and commit `.env.example` (§5).
