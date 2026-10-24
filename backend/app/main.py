import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .agent import llm_available, run_agent
from .database import Base, SessionLocal, engine, get_db, seed_initial_feeds
from .models import Feed
from .optimization import (
    LACTATING_RULES,
    RULES_BY_GROUP,
    NutrientConstraints,
    feeds_from_db,
    optimization_result_to_schema,
    optimize_ration,
)
from .rag import get_chroma_client, get_or_create_collection, ingest_documents, query_documents
from .requirements import AnimalProfile, compute_requirements, requirements_to_constraints
from .schemas import (
    AgentQuery,
    AgentResponse,
    AnimalProfileIn,
    FeedPriceUpdate,
    FeedResponse,
    HealthCheckResponse,
    OptimizationRequest,
    OptimizationResponse,
    PriceSyncResponse,
    RequirementsResponse,
)
from .scraper import reset_manual_prices, update_feed_prices

# Uvicorn only configures its own loggers, so without this the backend's module
# loggers fall back to the WARNING-only "last resort" handler. The level is raised on
# the `backend` package only — putting it on the root logger would also switch on
# httpx/urllib3 INFO chatter. LOG_LEVEL=DEBUG brings back the per-request agent tracing.
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
            with SessionLocal() as db:
                seed_initial_feeds(db)
            logger.info("Veritabanı hazır (deneme %d)", attempt)
            return
        except Exception as exc:  # noqa: BLE001 - any driver error means "not ready yet"
            last_error = exc
            logger.warning(
                "Veritabanına bağlanılamadı (deneme %d/%d): %s", attempt, DB_STARTUP_RETRIES, exc
            )
            time.sleep(DB_STARTUP_DELAY_SECONDS)
    raise RuntimeError(f"Veritabanı {DB_STARTUP_RETRIES} denemede hazır olmadı: {last_error}")


@asynccontextmanager
async def lifespan(app: FastAPI):
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
    return db.query(Feed).all()


@app.post("/api/v1/rag/ingest")
def rag_ingest():
    """Rebuild the knowledge base from data/docs/."""
    return {"status": "Documents ingested successfully", **ingest_documents()}


@app.get("/api/v1/rag/search")
def rag_search(query: str):
    return {"results": query_documents(query_text=query)}


@app.post("/api/v1/requirements", response_model=RequirementsResponse)
def requirements(animal: AnimalProfileIn):
    """NASEM 2021 requirements for an animal profile, plus its group's ration rules."""
    req = compute_requirements(AnimalProfile(**animal.model_dump()))
    rules = RULES_BY_GROUP[req.group]
    return RequirementsResponse(
        group=req.group,
        group_label=rules.label,
        dm_intake_kg=req.dm_intake_kg,
        nel_mcal_day=req.nel_mcal_day,
        nel_min=req.nel_min,
        nel_max=req.nel_max,
        mp_g_day=req.mp_g_day,
        mp_min=req.mp_min,
        cp_kg_day=req.cp_kg_day,
        cp_min=req.cp_min,
        cp_max=req.cp_max,
        calcium_min=req.ca_min,
        calcium_max=req.ca_max,
        phosphorus_min=req.p_min,
        phosphorus_max=req.p_max,
        energy_deficit_mcal=req.energy_deficit_mcal,
        notes=req.notes,
        ndf_min=rules.ndf_min,
        ndf_max=rules.ndf_max,
        forage_ratio_min=rules.forage_ratio_min * 100,
        forage_ratio_max=rules.forage_ratio_max * 100,
        starch_max=rules.starch_max,
        fat_max=rules.fat_max,
    )


# ============================================================
# Deterministic optimization endpoint
#
# This is the path the "Calculate ration" button uses. It does NOT involve the LLM:
# the exact feed selection and constraints the user entered reach the solver verbatim,
# so no feed can be silently dropped or re-worded.
# ============================================================
@app.post("/api/v1/optimize", response_model=OptimizationResponse)
def optimize(request: OptimizationRequest, db: Session = Depends(get_db)):
    """Compute a nutrition-first, cost-optimal ration from an explicit feed selection.

    Note on units: calcium/phosphorus are g/kg DM (ten times the percentage).
    """
    names = request.selected_feed_names
    if not names:
        raise HTTPException(status_code=400, detail="En az bir yem seçmelisiniz (selected_feed_names).")

    feeds_db = db.query(Feed).filter(Feed.name.in_(names)).all()
    # Report names that did not match instead of silently optimizing a smaller set.
    missing = sorted(set(names) - {f.name for f in feeds_db})
    if missing:
        raise HTTPException(status_code=400, detail=f"Şu yemler veritabanında yok: {', '.join(missing)}")

    feed_inputs = feeds_from_db(feeds_db)
    for feed in feed_inputs:
        override = (request.feed_price_overrides or {}).get(feed.name)
        if override is not None and override > 0:
            feed.price_per_kg = override

    constraints = NutrientConstraints(
        crude_protein_min=request.crude_protein_min,
        crude_protein_max=request.crude_protein_max,
        metabolic_energy_min=request.metabolic_energy_min,
        metabolic_energy_max=request.metabolic_energy_max,
        net_energy_lactation_min=request.net_energy_lactation_min,
        net_energy_lactation_max=request.net_energy_lactation_max,
        metabolizable_protein_min=request.metabolizable_protein_min,
        calcium_min=request.calcium_min,
        calcium_max=request.calcium_max,
        phosphorus_min=request.phosphorus_min,
        phosphorus_max=request.phosphorus_max,
        dry_matter_min=request.dry_matter_min,
        dry_matter_max=request.dry_matter_max,
        daily_intake_kg=request.target_dm_intake_kg,
    )
    body_weight = request.body_weight_kg
    rules = LACTATING_RULES
    notes: List[str] = []
    if request.animal:
        # Profilden gelen hedefler yalnızca BOŞ alanları doldurur; açık gönderilen kazanır.
        body_weight = request.animal.body_weight_kg
        req = compute_requirements(AnimalProfile(**request.animal.model_dump()))
        derived = requirements_to_constraints(req)
        for name, value in vars(derived).items():
            if getattr(constraints, name) is None:
                setattr(constraints, name, value)
        rules = RULES_BY_GROUP[req.group]
        notes = [f"🐄 {rules.label} ineği için NASEM 2021 hedefleri kullanıldı."] + [
            f"ℹ️ {note}" for note in req.notes
        ]

    expected_dm_intake = constraints.daily_intake_kg or (body_weight * 0.037)
    constraints.daily_intake_kg = expected_dm_intake

    result = optimize_ration(
        feed_inputs,
        constraints,
        use_all_selected=request.use_all_selected_feeds,
        min_inclusion_pct_dm=request.min_inclusion_pct_dm,
        expected_daily_intake_kg=expected_dm_intake,
        max_daily_intake_kg=(body_weight * 0.05) / 0.35,
        body_weight_kg=body_weight,
        rules=rules,
    )
    result.adjustments = notes + result.adjustments
    return optimization_result_to_schema(result, feed_inputs)


@app.post("/api/v1/agent/query", response_model=AgentResponse)
def agent_query(query: AgentQuery):
    """Free-text questions through the LLM agent.

    Without a GROQ_API_KEY this is the only endpoint that stops working: it answers
    503 while the rest of the API stays up. A crash inside the agent is reported as
    HTTP 500 — not folded into a 200 — so callers can tell "answered" from "crashed".
    """
    if not llm_available():
        raise HTTPException(
            status_code=503,
            detail="GROQ_API_KEY tanımlı değil, sohbet uç noktası devre dışı. "
                   "Rasyon hesaplama (/api/v1/optimize) anahtarsız da çalışır.",
        )
    try:
        return run_agent(query.query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Ajan sorguyu yanıtlayamadı: {exc}")


@app.post("/api/v1/feeds/sync-prices", response_model=PriceSyncResponse)
def sync_feed_prices(
    db: Session = Depends(get_db),
    manual_overrides: Optional[List[FeedPriceUpdate]] = None,
):
    """Yem fiyatlarını borsalardan (TOBB + Samsun TB) günceller ya da kullanıcının
    kendi fiyatlarını kaydeder.

    `manual_overrides` verilirse canlı çekim yapılmaz, bu fiyatlar "elle girildi" olarak
    kaydedilir. Canlı senkron elle girilmiş fiyatları ezmez. Canlı kaynak bulunamazsa
    fiyatlara dokunulmaz.
    """
    overrides = {u.name: u.price_per_kg for u in manual_overrides} if manual_overrides else None
    result = update_feed_prices(db, overrides)
    updated_names = [f.name for f in result["updated"]]
    live_count = result["live_count"]
    kept = result["kept_manual"]

    if overrides is not None:
        message = f"{live_count} yemin fiyatı kaydedildi: {', '.join(updated_names)}."
    elif live_count == 0:
        message = (
            "Canlı borsa verisi bulunamadı (kaynak ulaşılamıyor veya veriler 30 günden "
            "eski). Fiyatlara dokunulmadı."
        )
    else:
        message = (
            f"{live_count} yemin fiyatı canlı borsadan güncellendi: "
            f"{', '.join(updated_names)}. Kalan {result['baseline_count']} yem "
            "taban fiyatta (silaj, kuru ot ve mineraller borsada işlem görmüyor)."
        )
    if kept:
        message += f" Kendi kaydettiğin fiyatlara dokunulmadı: {', '.join(kept)}."

    return PriceSyncResponse(
        updated_at=datetime.now().isoformat(),
        updated_feeds_count=live_count,
        updated_feeds=updated_names,
        baseline_feeds_count=result["baseline_count"],
        source=result["source"],
        message=message,
        kept_manual_feeds=kept,
    )


@app.post("/api/v1/feeds/reset-prices")
def reset_prices(db: Session = Depends(get_db)):
    """Elle kaydedilen fiyatları taban fiyata döndürür; sonraki senkron piyasadan günceller."""
    reset = reset_manual_prices(db)
    return {"reset_feeds": reset, "message": f"{len(reset)} yemin kayıtlı fiyatı silindi."}
