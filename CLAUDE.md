# RationAI Project

## Overview
RationAI is an AI-powered animal nutrition assistant that helps formulate optimal feed rations. It combines a FastAPI backend with a LangChain-based agent using Groq's LLM, a ChromaDB knowledge base for document retrieval, and linear programming (PuLP) for ration optimization. The system can ingest nutritional documents, answer queries about animal nutrition, retrieve feed ingredient data from a PostgreSQL/SQLite database, and compute cost‑optimal rations that satisfy user‑specified nutrient constraints.

---

## 🚨 IMPORTANT ASSISTANT GUIDELINES (CLAUDE CODE EXECUTION RULES)

To maintain maximum autonomy and seamless development flow, the AI Assistant must strictly follow these operational rules:

1. **Full Execution Autonomy (No Manual User Steps)**:
   - NEVER provide raw code blocks and ask the user to manually copy, paste, create, or execute files.
   - ALWAYS create, edit, update, or refactor files directly using your file-editing tools.
   - ALWAYS execute terminal commands, test scripts, and migrations yourself.

2. **Python Environment Standard**:
   - On Windows environments, ALWAYS execute Python scripts using the dedicated virtual environment executable:
     `.venv/Scripts/python.exe <script_name>.py`
   - Do NOT rely on generic `python` or `python3` terminal invocations as they hit WindowsApps path errors.

3. **Workspace & Temporary File Hygiene**:
   - You are fully authorized to create permanent modules, feature files, or temporary utility scripts as needed.
   - ALWAYS automatically remove temporary debug/test scripts (`debug_*.py`, `check_*.py`, `tmp_*.py`, `test_*.py`) immediately after verification/testing is complete.
   - Keep the project root clean. Only keep core folders (`backend`, `data`, `frontend`, `.venv`, `.claude`), the launcher scripts (`quickstart.py`, `kill_ports.ps1`), and standard root configuration files (`.env`, `.env.example`, `.gitignore`, `.dockerignore`, `docker-compose.yml`, `CLAUDE.md`, `README.md`, the three `requirements*.txt`).
   - Never leave `*.bak` files or one‑off JSON dumps behind. The three requirements files are
     deliberate, not duplication: root is a thin `-r` include, the other two are what each Docker
     image installs. A new dependency goes in the file of the service that imports it.

---

## Directory Structure
rationai/
├── .claude/                   # Claude Code settings (permissions)
│   └── settings.local.json
├── .env                       # Environment variables (NEVER committed — see .env.example)
├── .env.example               # Committed template; GROQ_API_KEY is optional
├── .dockerignore              # Keeps .venv (700 MB) out of the build context
├── .gitignore
├── docker-compose.yml         # db (postgres) + backend + frontend
├── .venv/                     # Python virtual environment
├── backend/                   # FastAPI application
│   ├── __init__.py
│   ├── Dockerfile             # python:3.14-slim; bakes the ChromaDB index at build
│   └── app/
│       ├── __init__.py
│       ├── main.py            # FastAPI entry point and API routes
│       ├── agent.py           # LangGraph agent: state graph, tools, manual tool parsing
│       ├── optimization.py    # PuLP‑based ration optimization logic + infeasibility diagnosis
│       ├── database.py        # SQLAlchemy setup + the single curated feed table (upsert seeding)
│       ├── models.py          # SQLAlchemy Feed model
│       ├── schemas.py         # Pydantic models for API request/response
│       ├── scraper.py         # Web scraping and live feed price update service
│       └── rag.py             # ChromaDB wrapper for ingestion/query
├── data/
│   ├── chroma_db/             # Persistent ChromaDB storage
│   └── docs/                  # Source documents for RAG (Markdown tables & text)
│       ├── hbrasyonbilgisi.md
│       ├── Rasyon Hazırlama.md
│       └── Yem Tablosu.md
├── frontend/
│   ├── Dockerfile             # python:3.14-slim; streamlit only, no chromadb/langchain
│   └── app.py                 # Streamlit UI — dairy‑cow only, flat script (no `main()`)
├── .streamlit/
│   └── config.toml            # Streamlit theme (green primary); replaces hand‑written CSS
├── rationai.db                # Local SQLite database (default `DATABASE_URL`)
├── quickstart.py              # Cross‑platform launcher (seed → ingest → backend + frontend)
├── kill_ports.ps1             # Windows helper: kill whatever is listening on port 8000
├── requirements.txt           # Thin `-r` include of both files below (dev .venv)
├── requirements-backend.txt   # What the backend image installs
├── requirements-frontend.txt  # What the frontend image installs (no chromadb/langchain)
├── README.md                  # Public-facing project readme
└── CLAUDE.md                  # Development guidelines & project instructions

## Environment Variables
A `.env` file in the project root must contain the following variables:

| Variable | Example Value | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///rationai.db` (current) or `postgresql://postgres:123456@localhost:5432/rationai` | Database connection string |
| `GROQ_API_KEY` | `gsk_...` | API key for Groq (`langchain_groq.ChatGroq`) |

## Getting Started & Execution Commands

### Docker (the path a stranger cloning the repo uses)
`cp .env.example .env` then `docker compose up`. Three services: `db` (postgres:17-alpine,
named volume `pgdata`), `backend` (:8000), `frontend` (:8501). Startup is ordered by
healthchecks — `backend` waits for `service_healthy` on `db`, `frontend` on `backend`.

Rules that keep this working — do not break them casually:
- **Base image is `python:3.14-slim`, never Alpine.** PuLP's bundled CBC binary and chromadb's
  `onnxruntime` are glibc-linked; musl breaks both.
- **The ChromaDB index is baked at build** (`RUN python -m backend.app.rag`), and that step also
  pulls the ~166 MB ONNX embedding model into `$HOME/.cache/chroma`. The Dockerfile sets `HOME`
  and runs the step as the **same non-root user** that runs the app — otherwise the model is
  invisible at runtime and gets re-downloaded. `data/docs/` is therefore NOT in `.dockerignore`.
- **`.venv/` must stay in `.dockerignore`** — 700 MB of build context otherwise.
- Compose reads `.env` only for `${...}` interpolation; the container's `DATABASE_URL` is built
  in `docker-compose.yml` and overrides the SQLite value in `.env`.

### Execution & Development
Always run commands via the project's virtual environment:

Start everything (DB seed + RAG ingest + backend + Streamlit UI):
`.venv/Scripts/python.exe quickstart.py`

Start backend API server only:
`.venv/Scripts/python.exe -m uvicorn backend.app.main:app --reload`

Start Streamlit frontend only (expects backend on `http://localhost:8000`):
`.venv/Scripts/python.exe -m streamlit run frontend/app.py`

Create tables, run the `feed_type` migration and upsert the curated feed list:
`.venv/Scripts/python.exe -m backend.app.database`

Rebuild the ChromaDB index from `data/docs/`:
`.venv/Scripts/python.exe -m backend.app.rag`

Free port 8000 if a stale uvicorn is holding it (Windows):
`powershell -File kill_ports.ps1`

### API Endpoints Overview
| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Liveness ping |
| `GET` | `/api/v1/health` | DB / ChromaDB / LLM availability (`HealthCheckResponse`); the compose healthcheck reads this. Always 200 — a missing LLM is a supported state, not a failure |
| `GET` | `/api/v1/feeds` | Returns all feed ingredients and nutritional values |
| `POST` | `/api/v1/rag/ingest` | (Re)ingests all documents from `data/docs/` into ChromaDB |
| `GET` | `/api/v1/rag/search?query=<text>` | Query the knowledge base for nutrient guidelines |
| `POST` | `/api/v1/optimize` | **Deterministic ration optimization** — the Streamlit "Calculate" button uses this; no LLM involved |
| `POST` | `/api/v1/agent/query` | Free-text ration questions through the LangGraph agent. **503 when `GROQ_API_KEY` is unset** — the only endpoint that needs it |
| `POST` | `/api/v1/agent/initialize` | Automated database seeding and document ingestion |
| `POST` | `/api/v1/feeds/sync-prices` | Live market price sync, optional manual overrides |
| `POST` | `/api/v1/feeds/update-prices` | Legacy simulated price update (prefer `sync-prices`) |

## Architecture Notes
- **Two request paths.** The Streamlit "Rasyonu Hesapla" button posts a typed
  `OptimizationRequest` to `/api/v1/optimize`, which calls the solver directly — the feed
  selection and constraints reach it verbatim. The agent path (`/api/v1/agent/query`) exists
  only for free‑text conversation, where the LLM may re‑word or drop things. Never route the
  deterministic calculation back through the LLM.
- **`optimize_ration()` is the single entry point** (`optimization.py`). Both paths call it.
  It runs a **nutrition‑first two‑phase solve**:
  1. `solve_ration_problem(hard_nutrients=True)` — CP/ME/Ca/P as HARD constraints. If this
     succeeds the targets are guaranteed and the answer is the cheapest ration that meets them.
  2. Only if that is infeasible, re‑solve with soft constraints and prepend
     `diagnose_infeasibility()` output as Turkish explanations.
  `solver_details["nutrition_mode"]` is `"hard"` or `"relaxed"` — the UI badges off this.
- **Frontend talks to the backend only over HTTP**, via `BACKEND_URL` (env, defaults to
  `http://localhost:8000/api/v1` so the non‑Docker `quickstart.py` flow keeps working; compose
  sets `http://backend:8000/api/v1`). It imports nothing from `backend/` — that is why its image
  needs neither chromadb nor langchain.
- **Frontend is dairy‑only and deliberately minimal.** `frontend/app.py` is a flat script
  (no `main()` wrapper) using native Streamlit components — no hand‑written CSS, no
  `unsafe_allow_html`, no Plotly (the one chart is an Altair donut). `use_container_width`
  is deprecated in Streamlit 1.60; use `width="stretch"`. Nutrient targets are derived from
  body weight + milk yield by `estimate_requirements()` and only exposed as editable inputs
  behind the "Gelişmiş ayarlar" → "Besin hedeflerini elle ayarla" toggle. Keep the results
  view at one screen: summary + donut + 6 nutrient checks (CP/ME/Ca/P **plus NDF and forage
  ratio** — the two that say whether the cow can actually eat it), everything else in the
  collapsed "Detaylar" expander. Feeds are picked from three boxes (kaba yem / kesif yem /
  mineral) grouped by `feed_type`; minerals come pre‑selected.
  The auto CP band is `cp_req … cp_req + 4.0` (`CP_BAND`) — a narrower band pushes silage‑free
  and high‑yield rations into relaxed mode for no good reason, because the forage and NDF rules
  naturally pull protein up.
- **Agent flow:** `run_agent()` → LangGraph `agent → tool_executor → END`. The LLM does *not* use
  native function calling; `agent_node` prompts for a JSON tool call and `tool_executor_node`
  parses it manually. Tools: `search_knowledge_base`, `get_feed_data`,
  `calculate_optimal_ration_enhanced`, `calculate_simple_ration`.
- **Structured results:** the optimizer's `OptimizationResult` is converted to the API‑facing
  `OptimizationResponse` by `optimization_result_to_schema()` in `optimization.py`, shared by
  both the endpoint and the agent tools.

## ⚠️ PuLP trap: build the objective LAST
`prob += expr` **copies** the expression at that moment. Appending to the Python variable
afterwards (`penalty_expr += ...`) does **not** update the problem — those terms silently get a
coefficient of zero. This once made every min/max inclusion penalty free, so the solver dropped
7 of 9 selected feeds at no cost.

In `solve_ration_problem` the objective is assigned in exactly one place, immediately before
`prob.solve()`. **Any new penalty term must be added to `penalty_expr` above that line.** To
verify, print `prob.objective.items()` and confirm your slack variable is present with a
non‑zero coefficient.

## Units — the two that bite
| Quantity | UI collects | Solver/DB expects | Conversion |
|---|---|---|---|
| Calcium, Phosphorus | % of DM (0.6) | **g/kg DM** (6.0) | `× 10` in `build_optimization_payload()` |
| CP / ME requirement | animal inputs | **concentration** (% DM, Mcal/kg DM) | daily total ÷ daily DM intake, in `estimate_requirements()` |

Never feed a daily total (kg CP/day, Mcal/day) straight into a concentration constraint, and
never send a `dry_matter` as‑fed range by default — a silage TMR sits near 35 % DM, so a fixed
85‑95 % range makes every target unreachable. Leave it unset unless the user asks.
- **One seed source:** `database.py::_CURATED_FEEDS` (37 feeds, compact tuple table).
  `seed_initial_feeds()` **upserts** on every startup — it adds missing feeds and refreshes
  nutrient values, `feed_type` and `max_kg_per_day`. `price_per_kg` is only refreshed while
  `price_source` is still `"taban fiyat"`, so live‑scraped and user‑entered prices survive.
  Renames go in `_RENAMED_FEEDS`; new columns go in `_ADDED_COLUMNS` (`_ensure_columns()`
  issues the `ALTER TABLE`). (The old `seed_feeds.py`, 63 concentrates with no NDF/NEL/prices,
  was deleted — running it wiped the forages.)

## Prices: what is live and what is not
`scraper.py` scrapes **`borsa.tobb.org.tr`** — TOBB's unified exchange portal, one HTML table
shape across every exchange, prices already TL/kg. It is the only structured Turkish source
that actually works.

**It only covers grains.** TOBB's KÜSPELER group is empty and silage/hay/straw are never
exchange‑traded, so ~5 feeds (corn, barley, wheat, oats, chickpea) update live and the other
~32 stay on the curated baseline. Do not present baseline prices as live: `Feed.price_source`
and `price_updated_at` record the origin and the UI shows it per feed.

Parsing traps handled — keep them:
- TOBB mixes number formats in one column (`13,570` = 13.57 but `12.007,000` = 12.007).
  `_parse_turkish_number()` returns *both* readings and the sanity band (0.3×–3× the baseline)
  picks the plausible one.
- The same portal serves beef at 587 TL/kg and olive oil at 220 — the sanity band plus
  first‑token product matching (`_match_product`, with `_EXCLUDED_TOKENS`) keeps them out.
- Exchange data goes stale: Konya still showed May figures in late July. Rows older than
  `MAX_PRICE_AGE_DAYS` (30) are dropped.
- If nothing is scraped, prices are **left untouched** and reported as such. The old code
  silently substituted a random ±5 % walk around a duplicate 2024 price table
  (`REALISTIC_BASE_PRICES_TL_KG`, now deleted) — that is why the sync button could never
  reach current price levels.

## Constraint Contract (important)
`sanitize_nutrient_constraints()` and `validate_ration_feasibility()` in `schemas.py` both speak the
**nested** constraint format the agent tools build — keep any new caller on this shape:

```python
{
  "crude_protein":        {"min": 16.0, "max": 18.0},   # % DM
  "metabolizable_energy": {"min": 2.5,  "max": 3.0},    # Mcal/kg DM
  "calcium":              {"min": 6.0,  "max": 10.0},   # g/kg DM
  "phosphorus":           {"min": 3.0,  "max": 5.0},    # g/kg DM
  "dry_matter":           {"min": 40.0, "max": 60.0},   # % as-fed
  "daily_intake_kg": 22.2,
}
```
- `sanitize_nutrient_constraints(data, expected_daily_intake_kg=None)` returns the same nested shape
  with every nutrient key present, values clamped to `NUTRIENT_BOUNDS`, and inverted min/max swapped.
- `validate_ration_feasibility(feeds, constraints)` returns **a dict** `{"feasible": bool,
  "warnings": [str]}` — not a tuple. Feeds are `FeedIngredient.to_dict()` dicts.
- Solver constraints are all in DM concentration units. A tool that accepts *daily totals*
  (like `calculate_simple_ration`) must divide by the expected DM intake before building them.

## Feed classification drives everything
`Feed.feed_type` (DB column, seeded explicitly) is the source of truth:
`silage · hay · straw · grain · protein · byproduct · fat · mineral · salt`.
`_classify_feed_type()` guesses from the name and is only a **fallback** for rows with an empty
column — if a feed lands there, add it to `_CURATED_FEEDS` instead of extending the keyword lists.
The API exposes `feed_type` on `FeedResponse`, and the UI's three selection boxes group by it.

## Ration rules the solver always applies
`optimize_ration()` runs these as **hard** constraints in phase 1 alongside the nutrient targets;
only if that is infeasible does phase 2 re‑solve with all of them penalised‑soft
(`solver_details["ration_rules_mode"]` = `"hard"`/`"relaxed"`).

- **Per‑feed inclusion ceilings** (`MAX_INCLUSION_BY_TYPE`, share of ration DM) — silage 55 %,
  hay 30 %, **straw 8 %**, grain 30 % (acidosis), protein 25 %, byproduct 20 %, fat 4 %,
  mineral 1.5 %, salt 0.5 %. The ceiling applies to **every** feed. This is the rule that keeps
  the ration edible: straw is the cheapest feed per kg DM (1.89 TL vs corn silage 7.40), so
  without a ceiling the solver loads 11–12 kg of it.
- **No‑silage adaptation** (`MAX_INCLUSION_NO_SILAGE`) — with no silage selected, hay rises to
  60 % and straw to 12 %, because hay is then the forage base. Without this a legitimate dry
  ration cannot reach the 45 % forage floor and falls to relaxed mode.
- **NDF window 28–35 % DM**, plus **forage NDF ≥ 19 %** (peNDF proxy) and an intake cap of
  **1.2 % of body weight** in kg NDF/day (needs `body_weight_kg`). NDF is what makes a ration
  physically edible; the complained‑about ration sat at 43 %.
- **Forage ratio 45–65 %** of DM when any forage is selected.
- **Silage is the forage base** — when a silage is selected it must supply ≥ 25 % of DM and
  total hay is capped at 40 %. Without this the solver drops silage and maxes out alfalfa hay
  (higher CP → saves on soybean meal).
- **Ca:P ratio 1.5–2.5.** Hitting Ca and P individually does not guarantee a sane ratio, so it
  is constrained explicitly.
- **Protein guardrail** — CP below 12 % DM is heavily penalised when protein‑rich feeds are available.

### Animal‑health caps (as‑fed kg/day) — never relaxed
`Feed.max_kg_per_day` is the daily as‑fed ceiling **for a 600 kg cow**;
`FeedIngredient.health_cap_kg()` scales it by body weight (700 kg → 25 kg silage becomes 29.2).
Seeded values: silages 25, wet beet pulp 20, straw 4, molasses 1.5, bypass fat 0.5,
bicarb/premix 0.25, DCP 0.2, limestone 0.4, salt 0.15. Grains and meals have none — their
DM‑percentage ceilings already bind.

These are set as the PuLP **variable upper bound**, not a constraint row, so no slack can
attach and they hold in *both* phases — "more than 25 kg of silage causes acidosis" is not
something the solver may trade away. Consequence: **phase 2 can now return Infeasible**
(e.g. silage alone caps DM at 8 kg against a 22.5 kg target). `optimize_ration()` detects this
and returns `nutrition_mode="infeasible"` with a Turkish diagnosis naming the binding caps.

⚠️ When a solve is infeasible, CBC leaves meaningless values in the variables. `solve_ration_problem`
zeroes the amounts in that case. Do not restore the old behaviour of relabelling an infeasible
solve as "Feasible (soft constraint cozumu)" — it once reported 73 kg of silage against a 25 kg cap.
- **Forced inclusion** (`use_all_selected_feeds`) — the requested per‑feed minimum is capped by
  `0.5 × ceiling` and by `80 % / feed_count`, so forcing salt to 3 % DM cannot make
  the model infeasible. It is expensive‑soft, not hard: a feed that truly conflicts with the
  nutrient targets is dropped and reported in `solver_details["dropped_feeds"]`.

## The Groq key is optional — keep it that way
`agent.py` builds the LLM **lazily** (`_get_llm()`, cached; `llm_available()` just checks the env
var). Never construct `ChatGroq` at module import: it raises without `GROQ_API_KEY`, and because
`main.py` imports `agent.py`, that once took the entire backend down — including the deterministic
`/optimize` path, which never touches an LLM. Anyone cloning the repo must get a working app
without signing up for anything; only `/api/v1/agent/query` answers 503.

Same reasoning applies to startup: `create_all()` + seeding live in the **lifespan** handler with a
retry loop (`_prepare_database()`), not at import. Postgres reports healthy slightly before it
accepts connections, and an import-time crash is unrecoverable.

## Logging
Backend modules use `logging.getLogger(__name__)` — no `print`/`traceback.print_exc` on request
paths. `main.py` installs the handler once and raises the level on the **`backend` package only**
(`LOG_LEVEL`, default `INFO`); putting it on the root logger would switch on httpx/urllib3 INFO
chatter. `LOG_LEVEL=DEBUG` restores the per-request agent tracing.

`run_agent()` **re-raises** after logging instead of returning `success=False`; `agent_query`
turns that into an HTTP 500. Do not fold agent crashes back into a 200 response — callers could
no longer tell "the agent answered" from "the agent crashed". (`rag.py`/`database.py` still print
— they are CLI-style scripts, not request paths.)

## Known Issues
- **No test suite.** Every change is verified by hand (see the Docker verification list in the
  README/plan). pytest around `optimize_ration()`, `sanitize_nutrient_constraints()` and the
  endpoints is the obvious next step.

### Fixed — do not reintroduce
`agent_node` must append the **extracted** answer, not the raw `AIMessage`, when no tool is
called. The LLM replies with a JSON envelope (`{"answer": "..."}`) and `run_agent` returns the
last `AIMessage`, so appending the raw message showed users the JSON blob as the agent's answer.
The tool branch still appends the raw message — `tool_executor_node` puts the readable result
after it.

## Code Conventions
- **Style:** PEP 8 compliance; clean type hints (Python 3.9+).
- **Error Handling:** Descriptive error strings in agent tools; `HTTPException` in API endpoints.
- **Data Integrity:** Ensure full feed profiles (Dry Matter, Crude Protein, ME, Ca, P, Price) are parsed cleanly from `data/docs/` into the SQL database.

## Maintenance Tips
- Adding new feed standards or reference docs: place `.md` files in `data/docs/` and call `/api/v1/rag/ingest`.
- Always clean up temporary execution scripts after database updates or RAG index builds.