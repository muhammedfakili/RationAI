from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from .database import engine, Base, get_db, sessionmaker, seed_initial_feeds, SessionLocal
from .schemas import (
    FeedResponse,
    OptimizationRequest,
    OptimizationResponse,
    AgentResponse,
    HealthCheckResponse,
)
from .rag import ingest_documents, query_documents, get_chroma_client, get_or_create_collection
from .agent import run_agent, initialize_knowledge_base, llm_available
from .optimization import (
    optimize_ration,
    optimization_result_to_schema,
    feeds_from_db,
    NutrientConstraints,
)
from .scraper import update_feed_prices
from pydantic import BaseModel
from typing import Optional, List
import logging
import os
import time
from datetime import datetime

# Uvicorn only configures its own loggers, so without this the backend's module
# loggers fall back to the WARNING-only "last resort" handler. The level is raised on
# the `backend` package only — putting it on the root logger would also switch on
# httpx/urllib3 INFO chatter. LOG_LEVEL=DEBUG brings back the per-request agent
# tracing that used to be raw prints.
logging.basicConfig(format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("backend").setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

DB_STARTUP_RETRIES = int(os.getenv("DB_STARTUP_RETRIES", "15"))
DB_STARTUP_DELAY_SECONDS = 2.0


def _prepare_database() -> None:
    """Create the tables and seed the curated feeds, waiting for the DB to accept us.

    Under docker compose the Postgres container reports healthy slightly before it is
    ready for real connections, so a single attempt at import time would crash the
    backend. This runs at startup (not import) and retries instead.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, DB_STARTUP_RETRIES + 1):
        try:
            Base.metadata.create_all(bind=engine)
            db = SessionLocal()
            try:
                seed_initial_feeds(db)
            finally:
                db.close()
            logger.info("Veritabanı hazır (deneme %d)", attempt)
            return
        except Exception as exc:  # noqa: BLE001 - any driver error means "not ready yet"
            last_error = exc
            logger.warning(
                "Veritabanına bağlanılamadı (deneme %d/%d): %s",
                attempt, DB_STARTUP_RETRIES, exc,
            )
            time.sleep(DB_STARTUP_DELAY_SECONDS)
    raise RuntimeError(f"Veritabanı {DB_STARTUP_RETRIES} denemede hazır olmadı: {last_error}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepare the database on startup (replaces the deprecated @app.on_event)."""
    _prepare_database()
    yield


app = FastAPI(
    title="RationAI",
    description="Süt ineği rasyon optimizasyonu: deterministik LP çözücü + RAG destekli LLM ajanı.",
    lifespan=lifespan,
)


@app.get("/")
def read_root():
    return {"message": "RationAI Backend is running"}


@app.get("/api/v1/health", response_model=HealthCheckResponse)
def health_check():
    """Report which subsystems are usable — the compose healthcheck reads this.

    Always answers 200: a degraded backend is still worth reporting on. The LLM being
    absent is a normal, supported state (no GROQ_API_KEY), not a failure.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database_connected = True
    except Exception:
        logger.exception("health: veritabanı erişilemez")
        database_connected = False

    try:
        chromadb_connected = get_or_create_collection(get_chroma_client()).count() > 0
    except Exception:
        logger.exception("health: ChromaDB erişilemez")
        chromadb_connected = False

    return HealthCheckResponse(
        status="ok" if database_connected else "degraded",
        timestamp=datetime.now(),
        database_connected=database_connected,
        chromadb_connected=chromadb_connected,
        llm_available=llm_available(),
    )


@app.get("/api/v1/feeds", response_model=list[FeedResponse])
def get_feeds(db: Session = Depends(get_db)):
    seed_initial_feeds(db)
    from .models import Feed
    feeds = db.query(Feed).all()
    return feeds


# RAG endpoints
@app.post("/api/v1/rag/ingest")
def rag_ingest():
    """
    Ingest documents from the data/docs directory into the ChromaDB collection.
    """
    ingest_documents()
    return {"status": "Documents ingested successfully"}


@app.get("/api/v1/rag/search")
def rag_search(query: str):
    """
    Search for relevant document chunks based on the query string.
    """
    results = query_documents(query_text=query)
    return {"results": results}


# ============================================================
# Deterministic optimization endpoint
#
# This is the path the "Calculate ration" button uses. It does NOT involve the LLM:
# the exact feed selection and constraints the user entered reach the solver verbatim,
# so no feed can be silently dropped or re-worded. The agent endpoint below stays for
# free-text conversation.
# ============================================================
@app.post("/api/v1/optimize", response_model=OptimizationResponse)
def optimize(request: OptimizationRequest, db: Session = Depends(get_db)):
    """Compute a nutrition-first, cost-optimal ration from an explicit feed selection.

    Nutrient targets are applied as HARD constraints first; only if that is infeasible
    are they relaxed, and the response then explains which target could not be met.

    Note on units: calcium/phosphorus are g/kg DM (ten times the percentage).
    """
    from .models import Feed

    names = request.selected_feed_names or []
    if not names and not request.selected_feed_ids:
        raise HTTPException(
            status_code=400,
            detail="En az bir yem seçmelisiniz (selected_feed_names veya selected_feed_ids)."
        )

    query = db.query(Feed)
    if names:
        query = query.filter(Feed.name.in_(names))
    else:
        query = query.filter(Feed.id.in_(request.selected_feed_ids))
    feeds_db = query.all()

    if not feeds_db:
        raise HTTPException(
            status_code=400,
            detail=f"Seçilen yemler veritabanında bulunamadı: {', '.join(names) or request.selected_feed_ids}"
        )

    # Report names that did not match instead of silently optimizing a smaller set.
    if names:
        missing = sorted(set(names) - {f.name for f in feeds_db})
        if missing:
            raise HTTPException(
                status_code=400,
                detail=f"Şu yemler veritabanında yok: {', '.join(missing)}"
            )

    feed_inputs = feeds_from_db(feeds_db)

    # Apply the prices the user edited in the UI table.
    if request.feed_price_overrides:
        for feed in feed_inputs:
            override = request.feed_price_overrides.get(feed.name)
            if override is not None and override > 0:
                feed.price_per_kg = override

    expected_dm_intake = request.target_dm_intake_kg or (request.body_weight_kg * 0.037)

    constraints = NutrientConstraints(
        crude_protein_min=request.crude_protein_min,
        crude_protein_max=request.crude_protein_max,
        metabolic_energy_min=request.metabolic_energy_min,
        metabolic_energy_max=request.metabolic_energy_max,
        calcium_min=request.calcium_min,
        calcium_max=request.calcium_max,
        phosphorus_min=request.phosphorus_min,
        phosphorus_max=request.phosphorus_max,
        dry_matter_min=request.dry_matter_min,
        dry_matter_max=request.dry_matter_max,
        daily_intake_kg=expected_dm_intake,
    )

    result = optimize_ration(
        feed_inputs,
        constraints,
        use_all_selected=request.use_all_selected_feeds,
        min_inclusion_pct_dm=request.min_inclusion_pct_dm,
        expected_daily_intake_kg=expected_dm_intake,
        max_daily_intake_kg=(request.body_weight_kg * 0.05) / 0.35,
        body_weight_kg=request.body_weight_kg,
    )

    return optimization_result_to_schema(result, feed_inputs)


# Agent endpoints
class AgentQuery(BaseModel):
    query: str


@app.post("/api/v1/agent/query", response_model=AgentResponse)
def agent_query(query: AgentQuery):
    """
    Process a user query through the RationAI agent.
    The agent can search the knowledge base, retrieve feed data,
    and help formulate optimal rations.
    Returns both a text response and structured optimization data.

    Without a GROQ_API_KEY this is the only endpoint that stops working: it answers
    503 while the rest of the API stays up. A failure inside the agent is a real
    server error and is reported as HTTP 500 — it is not folded into a 200 response,
    so callers can tell "the agent answered" apart from "the agent crashed".
    """
    if not llm_available():
        raise HTTPException(
            status_code=503,
            detail="GROQ_API_KEY tanımlı değil, sohbet uç noktası devre dışı. "
                   "Rasyon hesaplama (/api/v1/optimize) anahtarsız da çalışır.",
        )

    logger.debug("agent_query: query=%r", query.query)
    try:
        agent_response = run_agent(query.query)
    except Exception as exc:
        logger.exception("agent_query failed for query=%r", query.query)
        raise HTTPException(status_code=500, detail=f"Ajan sorguyu yanıtlayamadı: {exc}")

    logger.debug(
        "agent_query: success=%s, optimization_result=%s",
        agent_response.success,
        agent_response.optimization_result is not None,
    )
    return agent_response


@app.post("/api/v1/agent/initialize")
def initialize_agent():
    """
    Initialize the agent's knowledge base by ingesting documents and seed the feed database.
    """
    try:
        # Initialize knowledge base (ingest documents)
        result = initialize_knowledge_base()
        # Seed feed database
        db = SessionLocal()
        try:
            seed_initial_feeds(db)
            seed_status = "Feed database seeded successfully."
        finally:
            db.close()
        return {"status": result, "seed": seed_status}
    except Exception as e:
        logger.exception("initialize_agent başarısız")
        raise HTTPException(status_code=500, detail=str(e))


# NEW: Feed price sync endpoints
class PriceSyncResponse(BaseModel):
    """Response model for feed price synchronization."""
    updated_at: str
    updated_feeds_count: int
    updated_feeds: List[str]
    baseline_feeds_count: int
    source: str
    message: str


class FeedPriceUpdate(BaseModel):
    """Individual feed price update."""
    name: str
    price_per_kg: float


@app.post("/api/v1/feeds/sync-prices", response_model=PriceSyncResponse)
def sync_feed_prices(
    db: Session = Depends(get_db),
    manual_overrides: Optional[List[FeedPriceUpdate]] = None
):
    """Yem fiyatlarını TOBB ticaret borsalarından günceller.

    Borsalarda yalnızca tahıl danesi işlem gördüğü için canlı güncellenen yem sayısı
    sınırlıdır (mısır, arpa, buğday, yulaf, tritikale, nohut). Kalan yemler küratörlü
    taban fiyatta kalır — bu yanıt hangisinin canlı hangisinin taban olduğunu sayar.
    Canlı kaynak bulunamazsa fiyatlara dokunulmaz; sahte "güncellendi" bilgisi verilmez.

    Args:
        manual_overrides: Verilirse canlı çekim yapılmaz, bu fiyatlar uygulanır.
    """
    timestamp = datetime.now().isoformat()

    price_overrides = None
    if manual_overrides:
        price_overrides = {update.name: update.price_per_kg for update in manual_overrides}

    result = update_feed_prices(db, price_overrides)
    updated_names = [f.name for f in result["updated"]]
    live_count = result["live_count"]

    if live_count == 0:
        message = (
            "Canlı borsa verisi bulunamadı (kaynak ulaşılamıyor veya veriler 30 günden "
            "eski). Fiyatlara dokunulmadı, taban fiyatlar geçerli."
        )
    else:
        message = (
            f"{live_count} yemin fiyatı canlı borsadan güncellendi: "
            f"{', '.join(updated_names)}. Kalan {result['baseline_count']} yem "
            "taban fiyatta (silaj, kuru ot, küspe ve mineraller borsada işlem görmüyor)."
        )

    return PriceSyncResponse(
        updated_at=timestamp,
        updated_feeds_count=live_count,
        updated_feeds=updated_names,
        baseline_feeds_count=int(result["baseline_count"]),
        source=result["source"],
        message=message,
    )


@app.post("/api/v1/feeds/update-prices", response_model=dict)
def update_feed_prices_endpoint(db: Session = Depends(get_db)):
    """Legacy alias for /api/v1/feeds/sync-prices. Prefer that endpoint."""
    result = update_feed_prices(db)
    return {
        "updated_feeds": [f.name for f in result["updated"]],
        "live_count": result["live_count"],
    }