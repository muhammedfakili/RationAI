import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from .advisor import advise_compound, advise_economics, advise_ration
from .agent import llm_available, run_agent
from .compound_feed import COMPOUND_FEED_TYPES, optimize_compound_feed, resolve_target
from .database import Base, SessionLocal, engine, get_db, seed_initial_feeds, seed_market_prices
from .economics import DEFAULT_DRESSING_PCT, DEFAULT_MORTALITY_PCT, EconomicsInput, compute_economics
from .models import Feed, MarketPrice
from .optimization import (
    LACTATING_RULES,
    NutrientConstraints,
    feeds_from_db,
    optimization_result_to_schema,
    optimize_ration,
)
from .rag import get_chroma_client, get_or_create_collection, ingest_documents, query_documents
from .requirements import (
    AnimalProfile,
    GROWTH_GROUPS,
    annotate_growth_result,
    compute_requirements,
    requirements_to_constraints,
    rules_for,
)
from .schemas import (
    Advice,
    AgentQuery,
    AgentResponse,
    AnimalProfileIn,
    CompoundFeedRequest,
    CompoundFeedResponse,
    CompoundFeedType,
    EconomicsRequest,
    EconomicsResponse,
    FeedPriceUpdate,
    FeedResponse,
    HealthCheckResponse,
    MarketPriceResponse,
    MarketPriceUpdate,
    OptimizationRequest,
    OptimizationResponse,
    PriceSyncResponse,
    RequirementsResponse,
)
from .scraper import (
    last_live_sync,
    reset_manual_prices,
    reset_market_prices,
    update_feed_prices,
    update_market_prices,
)

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
                seed_market_prices(db)
            logger.info("Veritabanı hazır (deneme %d)", attempt)
            return
        except Exception as exc:  # noqa: BLE001 - any driver error means "not ready yet"
            last_error = exc
            logger.warning(
                "Veritabanına bağlanılamadı (deneme %d/%d): %s", attempt, DB_STARTUP_RETRIES, exc
            )
            time.sleep(DB_STARTUP_DELAY_SECONDS)
    raise RuntimeError(f"Veritabanı {DB_STARTUP_RETRIES} denemede hazır olmadı: {last_error}")


# Günlük otomatik fiyat senkronu (yem + canlı hayvan + karkas). PRICE_AUTO_SYNC=0 kapatır
# (testler ve çevrimdışı çalışma). Son canlı senkron 24 saatten eskiyse çeker; kaynak
# ulaşılamazsa saatte bir yeniden dener. Arka plan iş parçacığı: başlangıcı bekletmez.
PRICE_SYNC_INTERVAL = timedelta(hours=24)
PRICE_SYNC_CHECK_SECONDS = 3600
PRICE_SYNC_START_DELAY_SECONDS = 30


def _price_autosync_loop(stop: threading.Event) -> None:
    if stop.wait(PRICE_SYNC_START_DELAY_SECONDS):
        return
    while True:
        try:
            with SessionLocal() as db:
                last = last_live_sync(db)
                if last is None or datetime.now() - last >= PRICE_SYNC_INTERVAL:
                    feeds = update_feed_prices(db)
                    market = update_market_prices(db)
                    logger.info("Otomatik fiyat senkronu: %d yem, %d hayvan/karkas fiyatı",
                                feeds["live_count"], len(market["updated"]))
        except Exception:
            logger.exception("Otomatik fiyat senkronu başarısız")
        if stop.wait(PRICE_SYNC_CHECK_SECONDS):
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    _prepare_database()
    stop = threading.Event()
    if os.getenv("PRICE_AUTO_SYNC", "1") != "0":
        threading.Thread(target=_price_autosync_loop, args=(stop,), daemon=True,
                         name="price-autosync").start()
    yield
    stop.set()


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
    """All feeds, in the curated display order (most common first in each section)."""
    return db.query(Feed).order_by(Feed.display_order, Feed.name).all()


@app.post("/api/v1/rag/ingest")
def rag_ingest():
    """Rebuild the knowledge base from data/docs/."""
    return {"status": "Documents ingested successfully", **ingest_documents()}


@app.get("/api/v1/rag/search")
def rag_search(query: str):
    return {"results": query_documents(query_text=query)}


@app.post("/api/v1/requirements", response_model=RequirementsResponse)
def requirements(animal: AnimalProfileIn):
    """ME + CP requirements for an animal profile, plus its group's ration rules."""
    profile = AnimalProfile(**animal.model_dump())
    req = compute_requirements(profile)
    rules = rules_for(profile)
    return RequirementsResponse(
        group=req.group,
        group_label=rules.label,
        dm_intake_kg=req.dm_intake_kg,
        me_mcal_day=req.me_mcal_day,
        me_min=req.me_min,
        me_max=req.me_max,
        cp_g_day=req.cp_g_day,
        cp_min=req.cp_min,
        cp_max=req.cp_max,
        calcium_g_day=req.ca_g_day,
        phosphorus_g_day=req.p_g_day,
        calcium_min=req.ca_min,
        calcium_max=req.ca_max,
        phosphorus_min=req.p_min,
        phosphorus_max=req.p_max,
        water_l_day=req.water_l_day,
        energy_deficit_mcal=req.energy_deficit_mcal,
        nasem_dmi_kg=req.nasem_dmi_kg,
        achievable_gain_kg=req.achievable_gain_kg,
        notes=req.notes,
        ca_p_ratio_min=rules.ca_p_ratio_min,
        ca_p_ratio_max=rules.ca_p_ratio_max,
        ndf_min=rules.ndf_min,
        ndf_max=rules.ndf_max,
        forage_ratio_min=rules.forage_ratio_min * 100,
        forage_ratio_max=rules.forage_ratio_max * 100,
        starch_max=rules.starch_max,
        fat_max=rules.fat_max,
        health_cap_exempt=sorted(rules.health_cap_exempt),
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
        profile = AnimalProfile(**request.animal.model_dump())
        req = compute_requirements(profile)
        derived = requirements_to_constraints(req)
        for name, value in vars(derived).items():
            if getattr(constraints, name) is None:
                setattr(constraints, name, value)
        rules = rules_for(profile)
        icon = "🐑" if profile.group == "lamb" else "🐄"
        notes = [f"{icon} {rules.label} grubu için ME + HP hedefleri (rasyon notları) kullanıldı."] + [
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
    if request.animal and request.animal.group in GROWTH_GROUPS:
        annotate_growth_result(result, profile)
    response = optimization_result_to_schema(result, feed_inputs)
    if request.with_advice:
        response.advice = _ration_advice(request, response, rules, body_weight,
                                         constraints.metabolic_energy_max, db)
    return response


def _ration_advice(request: OptimizationRequest, response, rules, body_weight: float,
                   me_max: Optional[float], db: Session) -> List[Advice]:
    """Öneriler: her aday aynı istekle (yalnız yem listesi değişik) yeniden çözülür."""
    feeds_by_name = {f.name: f for f in db.query(Feed).all()}

    def solve(names: List[str]):
        return optimize(request.model_copy(update={"selected_feed_names": names, "with_advice": False}), db)

    group = request.animal.group if request.animal else None
    gain = request.animal.daily_gain_kg if request.animal and group in GROWTH_GROUPS else None
    items = advise_ration(response, request.selected_feed_names, feeds_by_name, rules, group,
                          body_weight, solve, me_max=me_max, gain_target=gain)
    return [Advice(**item) for item in items]


@app.get("/api/v1/compound-feed/types", response_model=list[CompoundFeedType])
def compound_feed_types():
    """Compound-feed presets from the user's CP -> ME table (as-fed targets)."""
    return list(COMPOUND_FEED_TYPES.values())


@app.post("/api/v1/compound-feed", response_model=CompoundFeedResponse)
def compound_feed(request: CompoundFeedRequest, db: Session = Depends(get_db)):
    """Cheapest compound-feed batch (default 1 t) hitting an as-fed CP/ME/Ca/P target."""
    names = request.selected_feed_names
    if not names:
        raise HTTPException(status_code=400, detail="En az bir hammadde seçmelisiniz.")
    feeds_db = db.query(Feed).filter(Feed.name.in_(names)).all()
    missing = sorted(set(names) - {f.name for f in feeds_db})
    if missing:
        raise HTTPException(status_code=400, detail=f"Şu yemler veritabanında yok: {', '.join(missing)}")
    try:
        target = resolve_target(request.type_key, request.cp_min, request.cp_max,
                                request.me_min, request.me_max)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    feed_inputs = feeds_from_db(feeds_db)
    for feed in feed_inputs:
        override = (request.feed_price_overrides or {}).get(feed.name)
        if override is not None and override > 0:
            feed.price_per_kg = override
    response = optimize_compound_feed(feed_inputs, target, request.batch_kg)
    if request.with_advice:
        def solve(new_names: List[str]):
            return compound_feed(request.model_copy(
                update={"selected_feed_names": new_names, "with_advice": False}), db)

        feeds_by_name = {f.name: f for f in db.query(Feed).all()}
        response.advice = [Advice(**item) for item in
                           advise_compound(response, names, feeds_by_name, target, solve)]
    return response


@app.post("/api/v1/agent/query", response_model=AgentResponse)
def agent_query(query: AgentQuery):
    """Free-text chat through the LLM agent (history + the result on screen as context).

    Without an LLM key (NVIDIA_API_KEY or GROQ_API_KEY) this is the only endpoint that
    stops working: it answers 503 while the rest of the API stays up. A crash inside the
    agent is reported as HTTP 500 — not folded into a 200 — so callers can tell
    "answered" from "crashed".
    """
    if not llm_available():
        raise HTTPException(
            status_code=503,
            detail="Sohbet için LLM anahtarı tanımlı değil (NVIDIA_API_KEY ya da GROQ_API_KEY). "
                   "Rasyon hesaplama (/api/v1/optimize) anahtarsız da çalışır.",
        )
    try:
        return run_agent(query.query, [t.model_dump() for t in query.history], query.context)
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
    # Canlı senkron besi ekonomisinin hayvan ve karkas fiyatlarını da çeker (tek düğme).
    market = update_market_prices(db) if overrides is None else None
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
            f"{live_count} yemin işletme teslim fiyatı güncellendi (piyasa ortalaması + "
            f"nakliye): {', '.join(updated_names)}. Kalan {result['baseline_count']} yem "
            "taban fiyatta (silaj, bazı kuru otlar ve mineraller piyasada izlenmiyor)."
        )
    if kept:
        message += f" Kendi kaydettiğin fiyatlara dokunulmadı: {', '.join(kept)}."
    if market is not None:
        if market["updated"]:
            main_rows = [label for label in market["updated"] if " — " not in label]
            regional = len(market["updated"]) - len(main_rows)
            message += (f" Kesim fiyatı: {', '.join(main_rows)} güncellendi (UKON"
                        + (f"; +{regional} bölge" if regional else "") + ").")
        else:
            message += " Kesim fiyatı bulunamadı, önceki fiyatlar korundu."

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
    market = reset_market_prices(db)
    return {"reset_feeds": reset, "reset_market": market,
            "message": f"{len(reset) + len(market)} kayıtlı fiyat silindi."}


@app.get("/api/v1/market-prices", response_model=list[MarketPriceResponse])
def market_prices(db: Session = Depends(get_db)):
    """Live-animal and carcass prices (TL/kg) with their source and date."""
    return db.query(MarketPrice).order_by(MarketPrice.id).all()


@app.post("/api/v1/market-prices")
def save_market_prices(updates: List[MarketPriceUpdate], db: Session = Depends(get_db)):
    """Kullanıcının kendi hayvan/karkas fiyatlarını kaydeder ("elle girildi")."""
    result = update_market_prices(db, {u.key: u.price_per_kg for u in updates})
    return {"updated": result["updated"],
            "message": f"{len(result['updated'])} fiyat kaydedildi: {', '.join(result['updated'])}."}


_CARCASS_KEY = {"beef": "dana_karkas", "lamb": "kuzu_karkas"}
STALE_MARKET_PRICE_DAYS = 45


@app.post("/api/v1/fattening-economics", response_model=EconomicsResponse)
def fattening_economics(request: EconomicsRequest, db: Session = Depends(get_db)):
    """Bir besi döneminin baş başına ve sürü için maliyet/gelir/kâr hesabı.

    Yem maliyeti besi ORTASINDAKİ ağırlığın rasyonundan (aynı /optimize çözücüsü) gelir.
    Boş bırakılan fiyatlar `market_prices`'tan alınır.
    """
    species = request.species

    def mid_ration(days: int, with_advice: bool = False):
        """Besi ortası ağırlığının rasyonu (aynı /optimize çözücüsü)."""
        animal = AnimalProfileIn(
            group=species, body_weight_kg=request.start_weight_kg + request.daily_gain_kg * days / 2.0,
            daily_gain_kg=request.daily_gain_kg, days_on_feed=max(1, days // 2),
        )
        return optimize(OptimizationRequest(
            animal=animal,
            selected_feed_names=request.selected_feed_names,
            feed_price_overrides=request.feed_price_overrides,
            use_all_selected_feeds=request.use_all_selected_feeds,
            min_inclusion_pct_dm=request.min_inclusion_pct_dm,
            with_advice=with_advice,
        ), db)

    ration = mid_ration(request.days, with_advice=request.with_advice)
    if not ration.success or not ration.feed_recommendations:
        raise HTTPException(status_code=422, detail=(
            "Besi ortası ağırlığı için rasyon kurulamadı: " + " ".join(ration.turkish_explanations[:3])))

    carcass_key = _CARCASS_KEY[species]
    stored = {row.key: row for row in db.query(MarketPrice).all()}
    if request.region and f"{carcass_key}@{request.region}" in stored:
        carcass_key = f"{carcass_key}@{request.region}"

    def price(value: Optional[float], key: str) -> dict:
        if value is not None:
            return {"price_per_kg": value, "source": "bu hesap için girildi", "updated_at": None}
        row = stored[key]
        return {"price_per_kg": row.price_per_kg, "source": row.price_source,
                "updated_at": row.price_updated_at}

    dressing = request.dressing_pct or DEFAULT_DRESSING_PCT[species]
    prices = {"carcass": price(request.carcass_price, carcass_key)}
    # Türkiye'de besilik hayvan da o günkü kesim fiyatı üzerinden değerlenir (kullanıcı):
    # alış = canlı ağırlık × randıman × karkas fiyatı. Ağırlığa göre ayrı fiyat yok.
    prices["purchase"] = (price(request.purchase_price, carcass_key) if request.purchase_price
                          else {**prices["carcass"],
                                "price_per_kg": prices["carcass"]["price_per_kg"] * dressing / 100.0,
                                "source": f"karkas fiyatı × randıman %{dressing:g}"})
    inp = EconomicsInput(
        species=species, head_count=request.head_count,
        start_weight_kg=request.start_weight_kg, daily_gain_kg=request.daily_gain_kg,
        days=request.days,
        purchase_price=prices["purchase"]["price_per_kg"],
        carcass_price=prices["carcass"]["price_per_kg"],
        dressing_pct=dressing,
        itemized_costs=request.itemized_costs,
        mortality_pct=(request.mortality_pct if request.mortality_pct is not None
                       else DEFAULT_MORTALITY_PCT[species]),
        vet_per_head=request.vet_per_head,
        labor_per_head_day=request.labor_per_head_day,
        interest_pct_year=request.interest_pct_year,
    )
    econ = compute_economics(inp, ration.total_cost_per_day, ration.total_daily_intake_kg,
                             ration.daily_intake.dry_matter_kg or 0.0)

    warnings = []
    if ration.solver_details.get("nutrition_mode") != "hard":
        warnings.append("Besi ortası rasyonu hedeflerin hepsini tutturamadı; yem maliyeti "
                        "yaklaşıktır (rasyon ayrıntılarına bak).")
    expected = ration.solver_details.get("expected_gain_kg")
    if expected is not None and expected < request.daily_gain_kg * 0.95:
        warnings.append(
            f"Bu rasyonla beklenen artış ~{expected:g} kg/gün, hedefin ({request.daily_gain_kg:g}) "
            "altında: bitiş ağırlığı ve kâr iyimser olabilir.")
    stamp = prices["carcass"]["updated_at"]
    if stamp is not None and (datetime.now() - stamp).days > STALE_MARKET_PRICE_DAYS:
        warnings.append(f"Kesim fiyatı {STALE_MARKET_PRICE_DAYS} günden eski — güncel fiyatları çekin.")

    advice: List[Advice] = []
    if request.with_advice:
        # Besi süresi: aynı artış hedefiyle daha kısa/uzun dönemin günlük kârı. Hedef artışı
        # vermeyen (hayvan ağırlaştıkça) ya da hedefleri tutmayan süre aday sayılmaz.
        options = []
        for factor in (0.6, 0.8, 1.2, 1.4):
            days = int(round(request.days * factor / 10.0) * 10)
            if days < 30 or days > 600 or days == request.days:
                continue
            alt = mid_ration(days)
            alt_gain = alt.solver_details.get("expected_gain_kg") or 0.0
            if (alt.solver_details.get("nutrition_mode") != "hard"
                    or alt_gain < request.daily_gain_kg * 0.95):
                continue
            alt_econ = compute_economics(replace(inp, days=days), alt.total_cost_per_day,
                                         alt.total_daily_intake_kg, alt.daily_intake.dry_matter_kg or 0.0)
            options.append({"days": days, "end_weight": alt_econ["end_weight_kg"],
                            "profit_per_day": alt_econ["profit_per_day"]})
        advice = [Advice(**item) for item in advise_economics(
            econ, inp, [a.model_dump() for a in ration.advice], prices["carcass"]["price_per_kg"],
            ration.feed_recommendations, options)]

    return EconomicsResponse(
        species=species, head_count=request.head_count, days=request.days,
        start_weight_kg=request.start_weight_kg, dressing_pct=dressing,
        prices=prices, warnings=warnings, ration=ration, advice=advice, **econ,
    )
