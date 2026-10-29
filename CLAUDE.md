# RationAI Project

## Overview
RationAI is an AI-powered animal nutrition assistant that helps formulate optimal feed rations. It combines a FastAPI backend with a tool-calling chat agent (NVIDIA NIM or Groq, OpenAI-compatible API), a ChromaDB knowledge base for document retrieval, and linear programming (PuLP) for ration optimization. The system can ingest nutritional documents, answer queries about animal nutrition, retrieve feed ingredient data from a PostgreSQL/SQLite database, and compute cost‑optimal rations that satisfy user‑specified nutrient constraints.

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
│       ├── agent.py           # Chat agent: OpenAI-compatible API (NVIDIA NIM / Groq), native tool calls
│       ├── optimization.py    # PuLP‑based ration optimization logic + infeasibility diagnosis
│       ├── compound_feed.py   # Karma yem (concentrate) formulation: 1 t batch, AS‑FED targets
│       ├── economics.py       # Besi ekonomisi: cost / revenue / profit of one fattening period
│       ├── advisor.py         # "Öneriler" under every result: each suggestion verified by a re-solve
│       ├── requirements.py    # ME + CP requirements (user's notes): animal profile -> DM-concentration targets
│       ├── database.py        # SQLAlchemy setup + the single curated feed table (upsert seeding)
│       ├── models.py          # SQLAlchemy Feed model
│       ├── schemas.py         # Pydantic models for API request/response
│       ├── scraper.py         # Web scraping and live feed price update service
│       └── rag.py             # ChromaDB wrapper for ingestion/query
├── data/
│   ├── chroma_db/             # Persistent ChromaDB storage
│   ├── notlar/                # User's zootekni notebook: rasyon_notlari.md = transcript + every
│   │                          #   decision taken from it (photos are .gitignored, never commit them)
│   └── docs/                  # Source documents for RAG (Markdown tables & text)
│       ├── hbrasyonbilgisi.md
│       ├── Rasyon Hazırlama.md
│       └── Yem Tablosu.md
├── frontend/
│   ├── Dockerfile             # python:3.14-slim; streamlit only, no chromadb/langchain
│   ├── app.py                 # Streamlit UI — 3 pages in a top menu + "Asistan" chat dialog
│   └── assets/                # logo.svg / icon.svg for st.logo; screenshots/ = README images (docker-ignored)
├── .streamlit/
│   └── config.toml            # Theme: green light + dark modes, Inter font, radii; no CSS anywhere
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
| `NVIDIA_API_KEY` | `nvapi-...` | NVIDIA NIM (build.nvidia.com) key for the chat — optional; preferred when set |
| `GROQ_API_KEY` | `gsk_...` | Groq key for the chat — optional fallback |
| `LLM_PROVIDER` | `nvidia` / `groq` | Forces a provider; empty = first one with a key (nvidia, then groq) |
| `LLM_MODEL` | `nvidia/nemotron-3.5-super-vl-120b-a12b` (nvidia default) | Model override; providers retire models (404), so it is not hard-coded. `GROQ_MODEL` is still read for Groq |
| `PRICE_AUTO_SYNC` | `1` (default) / `0` | Daily background price sync (feeds + carcass/slaughter prices). Set `0` in test harnesses and offline — TestClient runs the lifespan too |

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

Start everything (DB seed + RAG index if empty + backend + Streamlit UI). A missing `.env`
is copied from `.env.example`:
`.venv/Scripts/python.exe quickstart.py`

Start backend API server only:
`.venv/Scripts/python.exe -m uvicorn backend.app.main:app --reload`

Start Streamlit frontend only (expects backend on `http://localhost:8000`):
`.venv/Scripts/python.exe -m streamlit run frontend/app.py`

Create tables, run the `feed_type` migration and upsert the curated feed list:
`.venv/Scripts/python.exe -m backend.app.database`

Rebuild the ChromaDB index from `data/docs/` (drops the collection first, ~10 s; `--if-empty`
only builds when there is no index — quickstart uses that):
`.venv/Scripts/python.exe -m backend.app.rag`

Free port 8000 if a stale uvicorn is holding it (Windows):
`powershell -File kill_ports.ps1`

### API Endpoints Overview
| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Liveness ping |
| `GET` | `/api/v1/health` | DB / ChromaDB / LLM availability (`HealthCheckResponse`); the compose healthcheck reads this. Always 200 — a missing LLM is a supported state, not a failure |
| `GET` | `/api/v1/feeds` | Returns all feed ingredients and nutritional values |
| `POST` | `/api/v1/rag/ingest` | Rebuilds the ChromaDB index from `data/docs/` |
| `GET` | `/api/v1/rag/search?query=<text>` | Query the knowledge base for nutrient guidelines |
| `POST` | `/api/v1/requirements` | ME + CP targets for an `AnimalProfileIn` (daily totals, DM concentrations, water) + the group's NDF/forage/Ca:P windows (the sidebar reads this) |
| `POST` | `/api/v1/optimize` | **Deterministic ration optimization** — the Streamlit "Calculate" button uses this; no LLM involved. With `animal`, empty targets are filled from the requirements and the group's rules apply |
| `GET` | `/api/v1/compound-feed/types` | Karma yem presets from the user's CP → ME table (as‑fed targets, Ca/P draft ranges) |
| `POST` | `/api/v1/compound-feed` | Cheapest compound‑feed batch (default 1000 kg) for a preset or custom CP/ME target |
| `POST` | `/api/v1/agent/query` | Chat: `query` + `history` + `context` (the result on screen). Answers with `answer`, `sources`, `tools_used`, `result` ({kind, data} of a calculation). **503 without an LLM key** — the only endpoint that needs one |
| `POST` | `/api/v1/feeds/sync-prices` | Live delivered-price sync (TOBB + Samsun TB + hayvanci.com, + delivery premium); a JSON list body instead saves the user's own prices as `"elle girildi"` |
| `POST` | `/api/v1/feeds/reset-prices` | Returns saved (`"elle girildi"`) prices — feeds and slaughter prices — to the curated baseline |
| `GET` | `/api/v1/market-prices` | Slaughter (carcass) prices, TL/kg: Türkiye average + UKON regions (`"<key>@<Bölge>"`), with source and date |
| `POST` | `/api/v1/market-prices` | Saves the user's own slaughter price (`"elle girildi"`; the daily sync keeps it) |
| `POST` | `/api/v1/fattening-economics` | Besi ekonomisi: per-head and herd cost, revenue (sale to the abattoir) and profit for one fattening period |

## Architecture Notes
- **Two request paths.** The Streamlit "Rasyonu Hesapla" button posts a typed
  `OptimizationRequest` to `/api/v1/optimize`, which calls the solver directly — the feed
  selection and constraints reach it verbatim. The agent path (`/api/v1/agent/query`) exists
  only for free‑text conversation, where the LLM may re‑word or drop things. Never route the
  deterministic calculation back through the LLM.
- **`optimize_ration()` is the single entry point** (`optimization.py`). Both paths call it.
  It runs a **nutrition‑first two‑phase solve**:
  1. `solve_ration_problem(hard_nutrients=True)` — CP/NEL(/ME)/Ca/P as HARD constraints. If this
     succeeds the targets are guaranteed and the answer is the cheapest ration that meets them.
  2. Only if that is infeasible, re‑solve with soft constraints and prepend
     `diagnose_infeasibility()` output as Turkish explanations.
  `solver_details["nutrition_mode"]` is `"hard"`, `"relaxed"` or `"infeasible"` — the UI badges off this.
- **Frontend talks to the backend only over HTTP**, via `BACKEND_URL` (env, defaults to
  `http://localhost:8000/api/v1` so the non‑Docker `quickstart.py` flow keeps working; compose
  sets `http://backend:8000/api/v1`). It imports nothing from `backend/` — that is why its image
  needs neither chromadb nor langchain.
- **Frontend layout:** `st.navigation(position="top")` with three `st.Page` callables —
  `render_ration_page`, `render_compound_feed_page`, `render_economics_page` (the old sidebar
  "Ne hesaplayalım?" control is gone; the sidebar holds each page's inputs only). `st.logo`
  shows `frontend/assets/logo.svg`. Every page starts with `page_header()`: title + an
  **"Asistan"** button (hidden without an LLM key) that opens `assistant_dialog()` — a large
  `st.dialog` with the chat. Look and feel come only from `.streamlit/config.toml` (light +
  dark themes, Inter, radii); keep it that way. Programmatic page changes go through the
  `goto` session key → `st.switch_page()` before `page.run()`; the dialog is opened by the
  `open_assistant` flag **after** `page.run()` (only one dialog per run, and a button inside a
  page can't open it directly before the page finishes).
- **Frontend is deliberately minimal** (dairy, beef and lamb groups). `frontend/app.py` uses
  native Streamlit components — no hand‑written CSS, no
  `unsafe_allow_html`, no Plotly (the one chart is an Altair donut). `use_container_width`
  is deprecated in Streamlit 1.60; use `width="stretch"`. The sidebar collects an animal
  profile (group segmented control; milk details behind "Hayvan detayları") and shows the
  targets from `/requirements` — **the frontend computes no requirement itself**. Targets are
  only exposed as editable inputs behind the "Gelişmiş ayarlar" → "Besin hedeflerini elle
  ayarla" toggle; in auto mode the payload carries only `animal`. Keep the results
  view at one screen: summary + donut + 7 nutrient checks (HP/ME/Ca/P **plus NDF, forage
  ratio and starch** — the three that say whether the cow can actually eat it; their windows
  come from the group's rules), everything else in the collapsed "Detaylar" expander. Energy is
  shown in **kcal** (the user's unit; the API speaks Mcal), feed HP/ME in the price table
  **as‑fed** like the user's sheet. Feeds are picked from three boxes (kaba yem / kesif yem /
  mineral) grouped by `feed_type`, in `display_order`; minerals come pre‑selected. Widget
  labels/help must not depend on the animal group — a changing help text rebuilt the forage
  multiselect and wiped the selection on every group switch. Where a widget *must* differ
  (weight range, default minerals) it gets a per‑species `key` (`bw_cow`/`bw_beef`/`bw_lamb`,
  `minerals_cattle`/`minerals_lamb`) so each species keeps its own state. Lamb rations are
  grams: under 5 kg DM the recipe column switches to g/gün. Cards without a lower bound and a
  ≥ 100 ceiling (NDF/starch in beef and lamb) are hidden — the filter must not catch ME, whose
  kcal ceiling is also > 100 (it once hid the ME card for every group).
- **Requirements (`requirements.py`) are the single source of targets** for the UI, `/optimize`
  and the agent. They are **the user's zootekni formulas** (ME + crude protein system), not
  NASEM — see `data/notlar/rasyon_notlari.md` §2, which also records why each deviation exists:
  KMT `BW·0.025 + milk·0.10` (< 24 kg milk) / `BW·0.022 + milk·0.20`; ME maintenance
  `0.133·BW^0.75` Mcal (≥ 10), milk `(0.577 + 0.165·fat)·milk`, gestation +30 % of maintenance
  **from day 190** (NRC 2001; the notes said month 3); CP maintenance `70.7 + 2.77·BW^0.75` g
  (≥ 350), milk `(43.6 + 11.5·fat)·milk`; Ca/P from FCM with absorption 0.38/0.5; water
  `12.3 + 2.15·KMT + 0.73·milk`. **Dry‑cow KMT is the NASEM dry equation (~1.85 % BW)**, not the
  notes' 2.5 %: with the table's energy values 2.5 % meant ~12 700 kcal/day surplus. NASEM
  lactating DMI survives only as a comparison note. Dry CP floor 12/13 % (the formula gives
  maintenance only, ~3 %); lactating CP ceiling never below 16 % (`_CP_MAX_FLOOR` — the table's
  11.7 % CP corn silage made 13 % ceilings infeasible). Bands: CP +4 / ME +0.25 lactating,
  +3 / +0.15 dry. ⚠️ `ME_DENSITY_CAP` (2.90 / 2.60 / 2.55) and `CP_DENSITY_CAP` (21 / 16 / 15)
  were **measured** like the old NEL/MP caps: the highest density a typical selection reaches in
  hard mode without dropping a feed. MP/RDP code still exists but only runs when a request sets
  `metabolizable_protein_min` explicitly.
- **Besi sığırı (`group="beef"`)** has its own branch, `_beef_requirements()`: KMT from the
  **NASEM 2016 growing‑finishing equation** `SBW^0.75·(0.2435·NEm − 0.0466·NEm² − 0.0869)/NEm`
  (NEm from ration ME, NRC 1996 cubic). Intake depends on ration density, so `_beef_dmi()` bisects
  for the density where KMT × density = requirement (capped at `ME_DENSITY_CAP`). The notes' flat
  2.5 % BW matched only at 400 kg (200 kg → 3.0 %, 600 kg → 2.25 %). ME = maintenance
  `0.133·BW^0.75` + gain energy, where the user's "7000 kcal ≈ 1 kg" is anchored at
  300 kg / 1 kg/day and scaled with NRC's retained‑energy exponents (`BW^0.75 · gain^1.097`) —
  a flat 7000 told a 600 kg steer it would gain 3.4 kg/day; CP from the NASEM 2016 table
  (Merck reproduction, bilinear interpolation); Ca/P from the NRC equations (absorption 0.50/0.68,
  checked against Arkansas MP391). `rules_for(profile)` adapts the `beef` rule set: forage floor
  steps 50 → 45 → 40 → 35 → 30 % over days 1–20 (≥ 14‑day step‑up in the literature), and
  grain:meal ≥ 6 only from 350 kg (lighter calves need 14–18 % CP, which 6:1 can't reach).
  No NDF/starch window (none in the sources; the 30 % forage floor is far above US feedlots'
  8–15 %), total fat ≤ 6.5 % DM, Ca:P 1.5–2 (urinary calculi), soybean meal excluded, no CP
  guardrail. Merck's P 0.2–0.4 % is a requirement range, not a ceiling — capping P at 4 g/kg made
  ATK rations for young calves infeasible. `annotate_growth_result()` replaces the energy‑surplus
  warning with "Beklenen artış: ~X kg/gün"; when the target gain needs a denser ration than
  `ME_DENSITY_CAP["beef"]`, the requirements report `achievable_gain_kg`.
- **Kuzu besisi (`group="lamb"`)**, `_lamb_requirements()`: KMT, CP, Ca, P interpolated by weight
  from the NRC 1985 sheep table (early‑weaned 20 kg, finishing 30–50 kg, 60 kg) — KMT falls from
  5 % BW at 20 kg to 2.8 % at 60 kg (the notes' flat 3.5 % was asked to be replaced). ME =
  maintenance `0.093·BW^0.75` (NEm 56 kcal ÷ km 0.60, same logic as cattle's 0.133) + gain
  energy with the same 7000 kcal rule anchored at 30 kg / 0.3 kg/day (`_GROWTH` holds both
  species' anchors; NRC gives 7.6–7.9 Mcal/kg there — the same ~10 % gap as in beef). Checked
  against NRC 2007 (30 kg / 300 g: 4.15 % BW, 12.4 % CP). The `lamb` rule set: forage steps like
  beef, **concentrate part ≥ 16 % CP as‑fed** (`concentrate_cp_min_asfed`, user's note), Ca:P 2–2.5
  (urinary calculi), **ammonium chloride fixed at 0.5 % DM when selected**
  (`min_inclusion_by_feed` + `max_inclusion_by_feed`), cattle premix excluded (copper is toxic
  to sheep — "Koyun Premiksi (Bakırsız)" replaces it), all NPN excluded, soybean meal excluded
  (besi), no grain:meal rule (conflicts with the 16 % concentrate), alfalfa hay exempt from the
  cattle‑based as‑fed health cap (`health_cap_exempt`; `/requirements` returns the list so the
  price table hides it). Health caps otherwise scale by BW/600, which is why NH4Cl's `max_kg`
  is 0.30: at 0.15 a 20 kg lamb could not take its 0.5 % dose and fell to relaxed mode.
  `ME_DENSITY_CAP["lamb"]` 2.85 was measured (yonca + saman; silage 2.90, çayır otu 2.75).
  Excluded‑feed messages use `excluded_reason_by_feed`, falling back to `excluded_reason`.
- **Energy surplus is not relaxation.** The table's ME values are ~10–15 % above NRC, so for
  low‑yield and dry cows no edible ration gets down to the requirement. The ME ceiling is
  therefore soft **even in phase 1** (`ENERGY_KEYS`): the solver minimises the overshoot, the
  result stays `"hard"` with `solver_details["energy_surplus"]`, and a warning states the
  surplus in kcal and kg/day via the user's 7000 kcal ≈ 1 kg rule.
- **Karma yem mode (`compound_feed.py`) is a separate, simpler LP** — not `optimize_ration()`.
  Everything there is **AS‑FED per kg of mix** (the notebook computes it that way), so feed values
  are converted back from DM. Presets = the user's CP → ME table (CP target..target+0.5, ME the
  table range; buzağı büyütme 2500–2600 by user decision). Hard first; if infeasible, nutrient
  targets get penalised slacks and the misses are listed. Ingredient caps (`FEED_CAP_PCT`) each
  cite a source (Penn State concentrate table, Feedipedia, NDDB, Canola Council, user's notes);
  anything uncited is capped at 30 % for variety, corn/oats uncapped, triticale ≤ ½ of grains.
  Without caps the LP filled 70–78 % of the batch with one cheap ingredient (DDGS, triticale,
  bran). Premix fixed 1 kg/t, salt 0.5–1 %, molasses ≤ 3 %. Calf presets drop cottonseed meal,
  whole cottonseed and groundnut meal; fattening presets drop soybean meal and need grain:meal ≥ 6.
  Ca/P ranges are **drafts from manufacturer labels** (the official tebliğ text wasn't reachable).
  In the UI the sidebar's first control ("Ne hesaplayalım?") picks the mode; the karma yem page
  is `render_compound_feed_page()` followed by `st.stop()`, and all modes share
  `render_price_editor()` / `render_price_sync()`.
- **Besi ekonomisi (`economics.py`, `/fattening-economics`, `render_economics_page()`)** — one
  fattening period, per head and for the herd. Feed cost uses the **mid‑period weight** method
  the user's teacher uses (the user wants an average figure, not period‑by‑period rations): the
  endpoint calls `optimize()` for `start + gain·days/2` with `days_on_feed = days/2`, then
  `feed = ration TL/day × days`. Other costs are the notebook's "1/3 of feed" or itemized
  (mortality × (purchase + feed/2), vet per head, labour per head‑day, interest on
  purchase + feed/2). **Sale is one figure: `end × dressing × slaughter price`** — the fattener
  sells the meat to the abattoir; there is no live sale (user). A live‑sale column priced from
  TOBB's live‑animal rows was removed: that price sat above slaughter × dressing, so "live sale"
  always looked more profitable and contradicted how the market works. Dressing defaults 55 %
  beef (notebook) / 45 % lamb (user; the notebook example said 42), editable. Empty prices come from `MarketPrice`.
  **Purchase defaults to carcass price × dressing** (TL/kg live): in Turkey an animal — a
  feeder too — is valued at the current slaughter price per kg; there is no weight‑class
  "besilik" price (user). Defaulting to the exchange's live price overstated profit and
  forced the user to type a price. The carcass price can be a UKON region (`region`;
  `"<key>@<Bölge>"` rows), default the Türkiye average. Warns when the ration's expected gain is under the target
  (then end weight and profit are optimistic). Checked against the notebook: 220 kg Simental,
  270 days, 1.5 kg/day → 2 770 kg DM per head (notebook 2 851).
- **Öneriler (`advisor.py`)** — `with_advice: true` on `/optimize`, `/compound-feed` and
  `/fattening-economics` (the UI always sends it; the agent does not) adds `advice: [Advice]`
  (`kind`, `text`, `add`, `remove`, `saving_tl_day`). Every "add X / swap X for Y" suggestion is
  **verified by re-solving** the same request with the changed feed list (user's choice: no
  untested rule‑of‑thumb amounts); one solve is 25–50 ms, a full advice pass 0.5–1.2 s.
  Candidates come only from `COMMON_FEEDS` (~20 feeds a farm can actually buy). Kinds and
  order: `saglik` (always shown: missing salt/premix, lamb NH4Cl/koyun premiksi, starch ≥ 90 %
  of the cap, low Ca:P) → `eksik` (failing targets: first a single feed that fixes everything,
  then pairs from the first `_PAIR_POOL` candidates, then per nutrient; growth groups also when
  the ration's expected gain is < 97 % of target) → `fazla` (dairy/dry energy surplus: dilute
  with straw/hay) → `sinir` (health caps and inclusion ceilings that bind, max 2; dropped feeds
  with a "remove" action) → `ucuz` (add a common feed or swap within the same class, shown
  when the saving ≥ min(3 % of cost, 5 TL/day); compound ≥ 1 % of the batch). Forage swaps
  stay within the same type — "corn silage → meadow hay" was cheaper on paper but changes the
  farm's forage system. `MAX_ADVICE` 5; `saglik`/`ekonomi` don't count against it. Economics
  adds `ekonomi` items: break‑even slaughter price, cost split + most expensive feed, and a
  feeding‑period check (days × 0.6/0.8/1.2/1.4, each with its own mid‑weight ration; only
  periods whose ration still gives the target gain). In the UI `render_advice()` shows them
  under each result; "Uygula" is an `on_click` callback that edits the multiselect keys
  (`ration_forages`/`ration_concentrates`/`minerals_*`, `compound_*`, `econ_*`) and sets an
  `auto_calc_*` flag so the next run recalculates. Write texts without Turkish suffixes after
  numbers ("%8'ında" was wrong) — phrase around them.
- **Agent flow (`agent.py`)** — no LangChain/LangGraph: `requests` against an **OpenAI‑compatible
  `/chat/completions`** with **native tool calling** (`TOOL_SPECS`). Provider from `PROVIDERS`
  (NVIDIA NIM `integrate.api.nvidia.com/v1`, Groq `api.groq.com/openai/v1`), chosen by
  `LLM_PROVIDER` or the first key present; model by `LLM_MODEL`. `run_agent(query, history,
  context)`: system prompt + the summary of the result on screen (`context`, via
  `SUMMARIZERS`) + the last `HISTORY_TURNS` turns + the question; up to `MAX_TOOL_ROUNDS` tool
  rounds, then the model answers **in its own words** (tool text goes back to the model, not
  to the user). Tools: `search_knowledge_base` (chunks tagged with their source file; the
  file names come back as `sources`), `get_feed_data`, `calculate_ration`,
  `design_compound_feed`, `fattening_economics`. The three calculation tools call the
  **same endpoint functions as the UI buttons** (`main.optimize`, `main.compound_feed`,
  `main.fattening_economics`, imported lazily — main imports agent) with `with_advice=True`,
  feed names matched by `_match_feed_names` (exact → alias → longest substring → word
  prefixes, aliases first, so "ayçiçek küspesi" → ATK 36 and "arpa hasılı" → the silage),
  and return `{"kind", "data"}` (data also carries `request` so the UI can reopen it) — the
  UI renders it with the normal result components. Summaries state the outcome in words
  (`_MODE_TEXT`: "BÜTÜN hedefler SAĞLANDI" / "HEPSİ TUTMADI", failing targets by name): with
  "mode=relaxed (hard = …)" the model once told the user all targets were met when the
  protein ceiling was not. If a calculation request (`_RATION_REQUEST`) is answered without a
  tool but with kg/TL figures (`_SUSPECT_ANSWER`), it is retried once with `_TOOL_REMINDER`,
  then refused — never shown. `<think>` blocks are stripped. `max_tokens` is 4000: reasoning
  models spend tokens before answering and 1500 cut answers mid‑sentence.
  **Model choice (Oct 2026, NIM free tier):** `nemotron-3.5-super-vl-120b-a12b` — Turkish OK,
  tool calls in ~3 s, knowledge answers 8–15 s. `nemotron-3-ultra-550b` had the best Turkish
  but 30–50 s; `nemotron-3.5-lightning-30b` picked wrong tool arguments; `gpt-oss-20b` answered
  factually wrong; Nemotron 3 Super, Kimi K2.6, Nano, Mistral Large returned 404 for the
  account; Gemma 4 / DeepSeek V4.1 / GLM timed out. NIM's free tier is for development —
  production use needs a licence, so keep the provider switchable.
- **Knowledge base (`rag.py`)** indexes `data/docs/*.md` plus `EXTRA_FILES`
  (`data/notlar/rasyon_notlari.md`). ⚠️ ChromaDB's default embedding (all‑MiniLM‑L6‑v2) is
  English: for "kuruda yonca neden verilmez?" it returned the table of contents. So
  `query_with_sources()` ranks **Turkish keyword matches first** (lower‑cased words cut to
  5‑letter stems, stop words dropped, all chunks cached in memory) and fills up with the
  vector hits. `data/docs/RationAI Kuralları.md` explains the program's
  own rules **with their reasons** in Turkish ("kuruda yonca neden yok?") — keep it in sync
  when a rule changes. The Dockerfile copies the notes file; `.dockerignore` excludes only the
  photos.
- **Chat UI** — the "Asistan" dialog (`assistant_dialog()`, a fragment: sending a message reruns
  only the dialog): example questions as `st.pills`, `st.chat_message` history in a fixed‑height
  container (`st.session_state["chat"]`), the last calculation result becomes `chat_context`, results are
  drawn with `render_results` / `render_compound_result(actionable=False)` /
  `render_economics_result(actionable=False)` (advice without "Uygula" buttons), and
  "… ekranında aç" (`_open_in_screen`, a plain button followed by `st.rerun()` — a callback
  would only rerun the dialog) writes the inputs into the page's widget keys
  (`ration_group`, `bw_*`, `milk_kg`, `gain_*`, `days_on_feed`, `compound_type`,
  `compound_batch`, `econ_*`, feed boxes), sets `goto` and the `auto_calc_*` flag. Every result
  has "Bu sonuç hakkında sor" (`render_ask_box`, hidden without an LLM key): it stores the
  question and the result (with its `request`) and opens the dialog.
- **Structured results:** the optimizer's `OptimizationResult` is converted to the API‑facing
  `OptimizationResponse` by `optimization_result_to_schema()` in `optimization.py`, shared by
  both the endpoint and the agent tool. `format_result_for_agent()` is the text form.

## ⚠️ PuLP trap: build the objective LAST
`prob += expr` **copies** the expression at that moment. Appending to the Python variable
afterwards (`penalty_expr += ...`) does **not** update the problem — those terms silently get a
coefficient of zero. This once made every min/max inclusion penalty free, so the solver dropped
7 of 9 selected feeds at no cost.

In `solve_ration_problem` the objective is assigned in exactly one place, immediately before
`prob.solve()`. **Any new penalty term must go through `soft()` (which appends to `penalties`)
above that line.** To
verify, print `prob.objective.items()` and confirm your slack variable is present with a
non‑zero coefficient.

## Units — the two that bite
| Quantity | UI collects | Solver/DB expects | Conversion |
|---|---|---|---|
| Calcium, Phosphorus | % of DM (0.6) | **g/kg DM** (6.0) | `× 10` in `build_optimization_payload()` |
| ME / CP requirement | animal profile | **concentration** (Mcal/kg DM, % DM) | daily total ÷ daily DM intake, in `requirements.compute_requirements()` |
| Energy in the UI | kcal/kg DM | **Mcal/kg DM** | `÷ 1000` in `build_optimization_payload()` |
| Feed HP / ME | user's table: **as‑fed** % and kcal/kg | **DM basis** | `÷ DM` in `database._get_curated_feeds()` (the table rows stay as‑fed so they match the sheet) |
| RUP | — | **% of CP** (not % DM) | `FeedIngredient.rup_pct_dm()` = CP × RUP / 100; RDP is the rest |

Never feed a daily total (kg CP/day, Mcal/day) straight into a concentration constraint, and
never send a `dry_matter` as‑fed range by default — a silage TMR sits near 35 % DM, so a fixed
85‑95 % range makes every target unreachable. Leave it unset unless the user asks.
- **One seed source:** `database.py::_CURATED_FEEDS` (71 feeds, compact tuple table, ordered
  most‑common‑first → `display_order`). HP and ME come from the **user's feed table, as‑fed**
  (range midpoints; ATK 24/36, SFK 46/48/52, PTK 24/42 are separate products); feeds missing
  from it keep NRC values converted to as‑fed (comment `NRC`). NEL is derived from ME
  (`0.703·ME − 0.19`) for the MP report only. Starch, ether extract, NDF, Ca, P and RUP are
  typical NRC 2001 values — lab analyses beat them. Prices marked `tahmini` are estimates.
  Animal‑by‑product meals (feather/blood/fish) are deliberately absent (illegal for ruminants),
  as are Ecomass and vinasse. Renamed feeds go through `_RENAMED_FEEDS` so the row (and its live
  or saved price) is renamed in place.
  `seed_initial_feeds()` **upserts** on every startup — it adds missing feeds and refreshes
  nutrient values, `feed_type` and `max_kg_per_day`. `price_per_kg` is only refreshed while
  `price_source` is still `"taban fiyat"`, so live‑scraped and user‑entered prices survive.
  New columns go in `_ADDED_COLUMNS` (`_ensure_columns()` issues the `ALTER TABLE`). (The old `seed_feeds.py`, 63 concentrates with no NDF/NEL/prices,
  was deleted — running it wiped the forages.)

## Prices: what is live and what is not
**Perspective: the average DELIVERED purchase price of a medium‑to‑large dairy operation**, not
one seller's quote. `scrape_all()` takes the median of every source sample per feed (market
price) and adds `DELIVERY_PREMIUM_TL_KG` by feed type — straw/hay +1.20, grain/protein/
byproduct +0.50 TL/kg (2026 haulage: ~1,200–1,800 TL/t intercity for bulky forage; a truck
carries about half as much straw as grain). The label shows the arithmetic:
`"piyasa 3.50 + teslim 1.20 · hayvanci.com"`. Farm‑gate (müstahsil) rows are skipped — an
operation buys from traders, and Samsun's farm‑gate straw (2.21) sat far below market (3.50).

`scraper.py` has three sources:
1. **`borsa.tobb.org.tr`** — TOBB's unified exchange portal, one HTML table shape across 13
   exchanges, TL/kg. **Grains only**: in Oct 2026 the whole product tree was scanned — the
   KÜSPELER group is empty and there is no bran, straw, hay or silage anywhere.
2. **`samsuntb.org.tr/urunler/`** — Samsun TB product pages ("son işlemler" tables with years
   of history): wheat bran, wheat straw, sunflower meal, soybean meal. Product URLs carry ids,
   so they are found by link text (`SAMSUN_PRODUCTS`, prefix match — the link text also holds
   a code and the price).
3. **`hayvanci.com/fiyatlar/<slug>`** — dated market price tables (robots.txt allows
   `/fiyatlar`): straw (applied to both straws), alfalfa hay, sunflower meal, grains.
   ⚠️ Its "soya" is soybean **seed** (28 TL/kg, Adana growers), not meal — never map it.

So ~11 of 71 feeds update live (rye via TOBB since Oct 2026; sorghum is mapped but had no
recent trades); silages, most hays, most meals and minerals stay on the curated baseline.
A grade‑less market price ("ayçiçeği küspesi", "soya küspesi") is written to the grade dairies
use: ATK 36 and SFK 46.
**Carcass (slaughter) prices come from UKON** (`scrape_ukon`, ukon.org.tr/fiyatlar): the
Ulusal Kırmızı Et Konseyi's weekly table of lean beef and lamb slaughter prices collected
from abattoirs, 7 regions + "Ortalama". The average row is `dana_karkas` / `kuzu_karkas`, each
region `dana_karkas@Ege Bölgesi` etc. (rows created on first sync). The page declares utf‑8
but isn't — `apparent_encoding` fixes the Turkish letters. Tables older than 21 days are
ignored. TOBB carcass rows are only the fallback.

**No live‑animal prices.** `MarketPrice` holds only slaughter prices; `seed_market_prices()`
deletes rows whose base key is no longer in `_MARKET_PRICES` (the old `dana_canli`/`kuzu_canli`).
The TOBB fallback (`scrape_livestock()`) reads only the exchanges' carcass rows ("DANA ETİ
KARKAS", "KUZU ETİ KARKAS" — Ankara, Gaziantep, Nazilli), exchange list from
`fiyat_borsa0.php` every time, product names matched **exactly** ("DANA ETİ BUT" is a cut),
median of the average column. ESK publishes carcass prices only around Kurban — not usable.
The "Güncel piyasa fiyatlarını çek" button (`feeds/sync-prices`) syncs feeds and slaughter prices.

**Daily auto‑sync:** `main._price_autosync_loop` (daemon thread started in the lifespan,
30 s after startup, re‑checked hourly) syncs feeds + slaughter prices when the newest live price is
older than 24 h (`scraper.last_live_sync`). `PRICE_AUTO_SYNC=0` disables it — set it in any
TestClient harness, otherwise the test run scrapes the web.

Do not present baseline prices as live: `Feed.price_source` and `price_updated_at` record the
origin. Baseline rows carry `BASELINE_PRICE_DATE` (2026‑07‑15) so the UI computes every
feed's age the same way and warns above `STALE_PRICE_DAYS` (45, minerals excluded).

**User prices win.** The UI's "Fiyatlarımı kaydet" posts changed prices as a list body to
`sync-prices`; they are stored as `"elle girildi"` (`MANUAL_SOURCE`) and a live sync never
overwrites them (`kept_manual` in the result). `reset-prices` returns them to baseline.

⚠️ **TLS:** since TOBB's certificate renewal (26 Aug 2026) the server sends no intermediate
certificate. Browsers and Windows schannel complete the chain via AIA; Python does not, so
every request failed with `CERTIFICATE_VERIFY_FAILED` and the sync silently returned 0 prices.
`_aia_ca_bundle()` downloads the intermediate from the leaf's AIA URL and **verifies it** (it
signed the leaf, a certifi root signed it, it is in date) before adding it to a CA bundle —
the AIA URL is plain HTTP, so never trust it unverified. Do not "fix" this with `verify=False`.
Turkish text: `str.upper()` turns "Kepeği" into "KEPEĞI" (dotless) — use `_tr_upper()`.

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

## Agent constraint parsing
`agent.parse_constraints()` turns the LLM's **nested** JSON (dict or string) into
`NutrientConstraints`:

```python
{
  "crude_protein":        {"min": 16.0, "max": 18.0},   # % DM
  "metabolizable_energy": {"min": 2.6,  "max": 2.9},    # Mcal ME/kg DM (the target system)
  "net_energy_lactation": {"min": 1.6,  "max": 1.8},    # Mcal NEL/kg DM (legacy, still accepted)
  "calcium":              {"min": 6.0,  "max": 10.0},   # g/kg DM
  "phosphorus":           {"min": 3.0,  "max": 5.0},    # g/kg DM
  "dry_matter":           {"min": 40.0, "max": 60.0},   # % as-fed — omit by default
  "daily_intake_kg": 22.2,                              # default: KMT of the animal
}
```
These are **overrides only** — `calculate_ration` starts from `compute_requirements()` and
replaces just the keys present here. Values are clamped to `NUTRIENT_BOUNDS`, unparsable
ones dropped, inverted min/max swapped.
Solver constraints are all DM concentrations — never pass a daily total (kg CP/day) as a bound.

## Feed classification drives everything
`Feed.feed_type` (DB column, seeded explicitly) is the source of truth:
`silage · hay · straw · grain · protein · byproduct · fat · npn · mineral · salt`
(`npn` = urea, ammonium sulfate, uric acid — CP equivalent up to 292 %, so `FeedResponse`
allows CP ≤ 300).
An empty column is treated as `"unknown"` (20 % ceiling) — add the feed to `_CURATED_FEEDS`
instead of guessing from its name (the old keyword classifier was removed).
The API exposes `feed_type` on `FeedResponse`, and the UI's three selection boxes group by it.

## Ration rules the solver always applies
`optimize_ration()` runs these as **hard** constraints in phase 1 alongside the nutrient targets;
only if that is infeasible does phase 2 re‑solve with all of them penalised‑soft
(`solver_details["ration_rules_mode"]` = `"hard"`/`"relaxed"`).

The rules are a `RationRules` per animal group (`RULES_BY_GROUP`, passed as `rules=`; default
lactating). The values below are the **lactating** set; change them only on purpose and re‑run
the regression scenarios. Dry groups differ on purpose: far‑off NDF 40–55 %, forage 70–90 %,
straw ≤ 35 %, grain ≤ 10 %; close‑up NDF 35–45 %, forage 60–80 %, straw ≤ 25 %, grain ≤ 15 %;
neither has the silage‑base rule (in a dry cow silage energy is what must be limited). Dry
groups also: **yonca (hay and silage) and Mermer Tozu are excluded** (`excluded_feeds` —
`optimize_ration()` removes them before solving and reports them in
`solver_details["excluded_feeds"]`; left in, forced inclusion would push the ration to relaxed
mode), **Ca:P ≥ 1.0** instead of 1.5 (no added Ca, but P must not exceed Ca — > 80 g P/day
triggers milk fever), and close‑up caps **Ca ≤ 100 g/day, P ≤ 45 g/day**.

- **Per‑feed inclusion ceilings** (`MAX_INCLUSION_BY_TYPE`, share of ration DM) — silage 55 %,
  hay 30 %, **straw 8 %**, grain 30 % (acidosis), protein 25 % (ATK/SFK, user's note), byproduct
  20 %, fat 6 %, npn 1 %, mineral 1.5 %, salt 0.5 %. The ceiling applies to **every** feed.
  Named feeds override their type (`max_inclusion_by_feed`): **Arpa 40 %, Buğday 10 %**
  (lactating; dry keeps only Buğday). This is the rule that keeps
  the ration edible: straw is the cheapest feed per kg DM (1.89 TL vs corn silage 7.40), so
  without a ceiling the solver loads 11–12 kg of it.
- **Added fat ≤ 6 % DM** in total (`fat_added_max`; dry 0 / 2 %) and **NPN ≤ 1 % DM as urea
  equivalent** (`NPN_UREA_EQ_MAX`; CP / 292 per feed) on top of the per‑feed ceilings.
- **No forage selected is a rule violation**, not just a warning: the forage‑ratio floor is
  added with zero forage, so phase 1 is infeasible. A beet‑pulp/bran ration once reported
  "all targets met" with 0 % forage.
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
- **Ca:P ratio 1.5–2.5** (dry 1.0–2.5). Hitting Ca and P individually does not guarantee a sane
  ratio, so it is constrained explicitly.
- **Protein guardrail** — CP below 12 % DM is heavily penalised when protein‑rich feeds are available.
- **Starch ≤ 28 % DM** (far‑off 18, close‑up 22) and **ether extract ≤ 7 %** (dry 5) — NDF says
  the fibre is there but does not catch acidosis: a 31 % starch ration fit the NDF window easily.
- **RDP balance ≥ 0** — only when an MP target is requested explicitly (it belongs to the MP
  system; profile targets are CP‑based): RDP ≥ 0.13/0.85 × TDN.
- **Salt ≥ 0.25 % DM** when salt is selected — Na is not modelled, so without this rule the
  cheapest ration always dropped the salt.
- **Minimum inclusion is semi‑continuous** when `use_all_selected_feeds` is off: a binary per
  feed, "unused or at least `MIN_INCLUSION_BY_TYPE`". It used to be a soft minimum, which
  penalised leaving any feed out — "cheapest combination" with all 37 feeds returned 31 of them.

### Animal‑health caps (as‑fed kg/day) — never relaxed
`Feed.max_kg_per_day` is the daily as‑fed ceiling **for a 600 kg cow**;
`FeedIngredient.health_cap_kg()` scales it by body weight (700 kg → 25 kg silage becomes 29.2).
Seeded values: silages 25, wet beet pulp 20, wet malt pulp / malt sprouts 15, wet brewer's
yeast 10, **alfalfa hay 8** (user's note), straws 4, molasses 1.5, bypass fat / vegetable oil
0.5, bicarb/premix 0.25, DCP 0.2, limestone 0.4, salt 0.15, urea 0.2 (other NPN at the same
nitrogen: ammonium sulfate 0.45, uric acid 0.28). Grains and meals have none — their
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

## The LLM key is optional — keep it that way
`agent.py` touches no network at import; `llm_available()` only checks the env vars. (A
`ChatGroq` built at import once took the entire backend down without a key — including the
deterministic `/optimize` path, which never touches an LLM.) Anyone cloning the repo must get a
working app without signing up for anything; only `/api/v1/agent/query` answers 503, and the
UI hides the chat box / shows a warning.

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
  README/plan). pytest around `optimize_ration()`, `agent.parse_constraints()` and the
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