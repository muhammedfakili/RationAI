from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from datetime import datetime
from enum import Enum


class FeedBase(BaseModel):
    """Base schema for feed ingredient."""
    name: str = Field(..., description="Feed name")
    feed_type: str = Field(
        "unknown",
        description="silage/hay/straw/grain/protein/byproduct/fat/mineral/salt — "
                    "drives the solver's inclusion windows and the UI grouping",
    )
    price_per_kg: float = Field(..., ge=0, description="Price per kg (TL/kg, as-fed basis)")
    dry_matter: float = Field(..., ge=0, le=100, description="Dry matter %")
    crude_protein: float = Field(..., ge=0, le=100, description="Crude protein % DM")
    metabolic_energy: float = Field(..., ge=0, description="Metabolic energy Mcal/kg DM")
    net_energy_lactation: float = Field(..., ge=0, description="Net energy for lactation Mcal/kg DM (NRC 2001)")
    ndf: float = Field(..., ge=0, le=100, description="Neutral detergent fiber % DM (NRC 2001)")
    calcium: float = Field(..., ge=0, description="Calcium g/kg DM")
    phosphorus: float = Field(..., ge=0, description="Phosphorus g/kg DM")
    max_kg_per_day: Optional[float] = Field(
        None, ge=0,
        description="Animal-health ceiling: max kg as-fed per day for a 600 kg cow "
                    "(scaled by body weight in the solver). None = no as-fed limit.",
    )
    price_source: Optional[str] = Field(
        None, description="Where the price came from: exchange name + date, or 'taban fiyat'"
    )
    price_updated_at: Optional[datetime] = Field(None, description="When the price was last set")


class FeedCreate(FeedBase):
    """Schema for creating a new feed ingredient."""
    pass


class FeedUpdate(BaseModel):
    """Schema for updating a feed ingredient."""
    price_per_kg: Optional[float] = Field(None, ge=0)
    dry_matter: Optional[float] = Field(None, ge=0, le=100)
    crude_protein: Optional[float] = Field(None, ge=0, le=100)
    metabolic_energy: Optional[float] = Field(None, ge=0)
    net_energy_lactation: Optional[float] = Field(None, ge=0)
    ndf: Optional[float] = Field(None, ge=0, le=100)
    calcium: Optional[float] = Field(None, ge=0)
    phosphorus: Optional[float] = Field(None, ge=0)


class FeedResponse(FeedBase):
    """Schema for feed ingredient response."""
    id: int

    class Config:
        from_attributes = True


class FeedListResponse(BaseModel):
    """Response schema for paginated feed list."""
    items: List[FeedResponse]
    total: int


class RAGIngestRequest(BaseModel):
    """Request schema for RAG document ingestion."""
    force_reindex: bool = False


class RAGIngestResponse(BaseModel):
    """Response schema for RAG ingestion."""
    message: str
    documents_processed: int
    chunks_created: int


class RAGSearchRequest(BaseModel):
    """Request schema for RAG search."""
    query: str = Field(..., min_length=1, max_length=1000)
    k: int = Field(5, ge=1, le=20)


class RAGSearchResult(BaseModel):
    """Single RAG search result."""
    content: str
    metadata: Dict[str, Any]
    score: float


class RAGSearchResponse(BaseModel):
    """Response schema for RAG search."""
    query: str
    results: List[RAGSearchResult]


class AnimalType(str, Enum):
    """Animal type for optimization."""
    DAIRY_COW = "dairy_cow"
    DRY_COW = "dry_cow"
    HEIFER = "heifer"


class OptimizationRequest(BaseModel):
    """Request schema for ration optimization."""
    animal_type: AnimalType = AnimalType.DAIRY_COW
    body_weight_kg: float = Field(600, ge=300, le=1000, description="Body weight in kg")
    milk_yield_kg: float = Field(30, ge=0, le=80, description="Milk yield in kg/day")
    milk_fat_percent: float = Field(3.5, ge=2.5, le=6.0, description="Milk fat %")
    milk_protein_percent: float = Field(3.2, ge=2.5, le=5.0, description="Milk protein %")
    days_in_milk: int = Field(150, ge=0, le=305, description="Days in milk")
    lactation_number: int = Field(2, ge=1, le=6, description="Lactation number")

    # Feed selection - user chosen feed IDs or names
    selected_feed_ids: Optional[List[int]] = Field(None, description="List of feed IDs to include")
    selected_feed_names: Optional[List[str]] = Field(None, description="List of feed names to include")

    # Optional nutrient constraints (DM basis)
    crude_protein_min: Optional[float] = Field(None, ge=0, le=30, description="Min CP % DM")
    crude_protein_max: Optional[float] = Field(None, ge=0, le=30, description="Max CP % DM")

    # The solver works in ME (Mcal/kg DM) - these are the constraints it actually applies.
    metabolic_energy_min: Optional[float] = Field(None, ge=0, le=5, description="Min ME Mcal/kg DM")
    metabolic_energy_max: Optional[float] = Field(None, ge=0, le=5, description="Max ME Mcal/kg DM")

    net_energy_lactation_min: Optional[float] = Field(None, ge=0, le=2.5, description="Min NEL Mcal/kg DM (informational)")
    net_energy_lactation_max: Optional[float] = Field(None, ge=0, le=2.5, description="Max NEL Mcal/kg DM (informational)")
    ndf_min: Optional[float] = Field(None, ge=15, le=70, description="Min NDF % DM (informational)")
    ndf_max: Optional[float] = Field(None, ge=15, le=70, description="Max NDF % DM (informational)")

    # ⚠️ BIRIM: Ca ve P g/kg KM cinsindendir - yüzdenin 10 KATI.
    # Örn. rasyonda %0.6 Ca hedefliyorsan buraya 6.0 gönder.
    calcium_min: Optional[float] = Field(None, ge=0, description="Min Ca g/kg DM (= % x 10)")
    calcium_max: Optional[float] = Field(None, ge=0, description="Max Ca g/kg DM (= % x 10)")
    phosphorus_min: Optional[float] = Field(None, ge=0, description="Min P g/kg DM (= % x 10)")
    phosphorus_max: Optional[float] = Field(None, ge=0, description="Max P g/kg DM (= % x 10)")

    # ⚠️ Silajlı bir TMR asla %85 KM olamaz (mısır silajı ~%30 KM).
    # Silaj kullanıyorsan ya boş bırak ya da %40-60 aralığı ver.
    dry_matter_min: Optional[float] = Field(None, ge=20, le=90, description="Min DM % as-fed")
    dry_matter_max: Optional[float] = Field(None, ge=20, le=90, description="Max DM % as-fed")

    # Intake constraints
    target_dm_intake_kg: Optional[float] = Field(None, ge=10, le=35, description="Target DM intake kg/day")

    # Advanced options
    min_forage_ratio: float = Field(0.40, ge=0.2, le=0.8, description="Minimum forage ratio of DM")
    max_concentrate_ratio: float = Field(0.60, ge=0.2, le=0.8, description="Maximum concentrate ratio of DM")

    # Seçilen yemlerin tamamının rasyona katılması
    use_all_selected_feeds: bool = Field(
        True, description="Force every selected feed into the ration (type-aware minimum)"
    )
    min_inclusion_pct_dm: float = Field(
        3.0, ge=0.1, le=15.0, description="Target minimum inclusion per feed, % DM"
    )

    # Kullanıcının arayüzde elle düzelttiği fiyatlar (TL/kg, doğal hali)
    feed_price_overrides: Optional[Dict[str, float]] = Field(
        None, description="Feed name -> price TL/kg, overrides the database price"
    )


class OptimizationSettings(BaseModel):
    """Settings for optimization algorithm."""
    solver_time_limit: int = Field(30, ge=5, le=300)
    soft_constraint_penalty: float = Field(100000, ge=1000, le=1000000)
    max_daily_intake_kg: float = Field(50, ge=20, le=100)


class FeedAmount(BaseModel):
    """Feed amount in optimized ration."""
    feed_name: str
    amount_kg_as_fed: float = Field(..., description="Amount in kg/day as-fed basis")
    amount_kg_dm: float = Field(..., description="Amount in kg/day dry matter basis")
    proportion: float = Field(..., description="Proportion of total DM")
    cost_per_day: float = Field(..., description="Cost contribution TL/day")


class NutrientContent(BaseModel):
    """Ration nutrient content.

    The DM-basis core values are always produced by the solver and therefore required.
    NEL/NDF and the as-fed equivalents are only available for some feed sources, so they
    stay optional. This is the single definition used by both the API-facing
    `OptimizationResult` and the agent-facing `OptimizationResponse`.
    """
    crude_protein_percent_dm: float = Field(..., description="Crude protein % DM")
    metabolic_energy_mcal_per_kg_dm: float = Field(..., description="ME Mcal/kg DM (computed)")
    calcium_g_per_kg_dm: float = Field(..., description="Calcium g/kg DM")
    phosphorus_g_per_kg_dm: float = Field(..., description="Phosphorus g/kg DM")
    dry_matter_percent: float = Field(..., description="Ration DM % as-fed")

    net_energy_lactation_mcal_per_kg_dm: Optional[float] = Field(None, description="NEL Mcal/kg DM")
    ndf_percent_dm: Optional[float] = Field(None, description="NDF % DM")
    forage_ndf_percent_dm: Optional[float] = Field(None, description="NDF from forages, % DM")
    forage_ratio_percent: Optional[float] = Field(None, description="Forage share of ration DM, %")
    ca_p_ratio: Optional[float] = Field(None, description="Ca:P ratio")

    # As-fed (doğal hali) equivalents
    crude_protein_percent_af: Optional[float] = Field(None, description="Crude protein % as-fed")
    metabolic_energy_mcal_per_kg_af: Optional[float] = Field(None, description="ME Mcal/kg as-fed")
    calcium_g_per_kg_af: Optional[float] = Field(None, description="Calcium g/kg as-fed")
    phosphorus_g_per_kg_af: Optional[float] = Field(None, description="Phosphorus g/kg as-fed")


class DailyNutrientIntake(BaseModel):
    """Daily nutrient intake amounts (totals per day, not concentrations).

    The five core values are always produced by the solver and therefore required.
    NEL/NDF depend on the feed table carrying those columns, so they stay optional.
    This is the single definition used by both the API-facing `OptimizationResult`
    and the agent-facing `OptimizationResponse` — see `NutrientContent` above for
    the same pattern on the concentration side.
    """
    dry_matter_kg: float = Field(..., description="DM intake kg/day")
    crude_protein_kg: float = Field(..., description="CP intake kg/day")
    metabolic_energy_mcal: float = Field(..., description="ME intake Mcal/day")
    calcium_g: float = Field(..., description="Ca intake g/day")
    phosphorus_g: float = Field(..., description="P intake g/day")

    net_energy_lactation_mcal: Optional[float] = Field(None, description="NEL intake Mcal/day")
    ndf_kg: Optional[float] = Field(None, description="NDF intake kg/day")


class OptimizationResult(BaseModel):
    """Response schema for ration optimization result."""
    status: str
    feed_amounts: List[FeedAmount]
    nutrient_content: NutrientContent
    daily_nutrient_intake: DailyNutrientIntake
    total_cost_per_day: float = Field(..., description="Total feed cost TL/day")
    total_cost_per_kg_dm: float = Field(..., description="Cost per kg DM")
    total_cost_per_kg_as_fed: float = Field(..., description="Cost per kg as-fed")
    total_daily_intake_kg: float = Field(..., description="Total intake kg/day as-fed")
    total_daily_dm_intake_kg: float = Field(..., description="Total DM intake kg/day")
    constraints_status: Dict[str, str]
    violations: List[str]
    warnings: List[str]
    adjustments: List[str]
    solver_details: Dict[str, Any]

    # Requirement coverage percentages
    crude_protein_coverage_percent: Optional[float] = None
    net_energy_lactation_coverage_percent: Optional[float] = None
    metabolic_energy_coverage_percent: Optional[float] = None
    calcium_coverage_percent: Optional[float] = None
    phosphorus_coverage_percent: Optional[float] = None
    dry_matter_coverage_percent: Optional[float] = None
    ndf_coverage_percent: Optional[float] = None


class FeedsUpdatePricesRequest(BaseModel):
    """Request schema for updating feed prices."""
    feed_prices: Dict[str, float] = Field(..., description="Feed name to price mapping (TL/kg)")


class HealthCheckResponse(BaseModel):
    """Health check response."""
    status: str
    timestamp: datetime
    database_connected: bool
    chromadb_connected: bool
    llm_available: bool


# ============================================================
# Agent Response Schemas (for LLM agent output)
# ============================================================

class FeedRecommendation(BaseModel):
    """Single feed recommendation in optimized ration."""
    name: str
    amount_kg_per_day: float
    proportion: float
    price_per_kg: float
    cost_per_day: float
    nutrients: Dict[str, float] = {}


class ConstraintStatus(BaseModel):
    """Status of a nutrient constraint."""
    constraint: str
    actual: float
    status: str
    unit: str
    turkish_explanation: Optional[str] = None


class OptimizationResponse(BaseModel):
    """Full optimization response for agent/frontend."""
    status: str
    success: bool
    total_cost_per_day: float
    total_cost_per_kg: float
    total_daily_intake_kg: float
    feed_recommendations: List[FeedRecommendation]
    nutrient_content: NutrientContent
    daily_intake: DailyNutrientIntake
    constraints_status: List[ConstraintStatus]
    turkish_explanations: List[str]
    warnings: List[str]
    solver_details: Dict[str, Any]


class AgentResponse(BaseModel):
    """Agent query response."""
    success: bool = True
    answer: Optional[str] = None
    response: Optional[str] = None
    optimization_result: Optional[Dict[str, Any]] = None

    model_config = ConfigDict(extra="ignore")


# ============================================================
# Constraint sanitation / feasibility helpers
#
# Both helpers speak the *nested* constraint format used by the agent tools:
#   {"crude_protein": {"min": 14.0, "max": 18.0}, ..., "daily_intake_kg": 22.0}
# Nutrient keys: crude_protein (% DM), metabolizable_energy (Mcal/kg DM),
# calcium & phosphorus (g/kg DM), dry_matter (% as-fed).
# ============================================================

# Physically sensible ranges for dairy cattle rations (DM basis).
NUTRIENT_BOUNDS: Dict[str, tuple] = {
    "crude_protein": (6.0, 30.0),           # % DM
    "metabolizable_energy": (1.5, 3.5),     # Mcal/kg DM
    "calcium": (1.0, 15.0),                 # g/kg DM
    "phosphorus": (1.0, 10.0),              # g/kg DM
    "dry_matter": (20.0, 100.0),            # % as-fed
}

# Feed dict key that carries each nutrient (see FeedIngredient.to_dict()).
_FEED_NUTRIENT_KEYS: Dict[str, str] = {
    "crude_protein": "crude_protein",
    "metabolizable_energy": "metabolic_energy",
    "calcium": "calcium",
    "phosphorus": "phosphorus",
    "dry_matter": "dry_matter",
}

_NUTRIENT_LABELS_TR: Dict[str, str] = {
    "crude_protein": "Ham protein",
    "metabolizable_energy": "Metabolik enerji",
    "calcium": "Kalsiyum",
    "phosphorus": "Fosfor",
    "dry_matter": "Kuru madde",
}


def _to_float(value: Any) -> Optional[float]:
    """Coerce a value to float, returning None when it is missing or unparsable."""
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return None if result != result else result  # drop NaN


def sanitize_nutrient_constraints(
    data: Dict[str, Any],
    expected_daily_intake_kg: Optional[float] = None
) -> Dict[str, Any]:
    """Clean nutrient constraints and clamp them to physically achievable ranges.

    Preserves the nested ``{"min": x, "max": y}`` shape the agent tools build, drops
    unparsable/None values, clamps each bound to `NUTRIENT_BOUNDS`, and swaps min/max
    if the LLM produced them inverted.

    Args:
        data: Nested constraint dict (possibly with None values or stray keys).
        expected_daily_intake_kg: Fallback for ``daily_intake_kg`` when absent.

    Returns:
        A sanitized dict in the same nested shape. Nutrient keys are always present
        (as ``{"min": ..., "max": ...}``) so callers can chain ``.get(k, {}).get("min")``.
    """
    cleaned: Dict[str, Any] = {}

    for nutrient, (lo, hi) in NUTRIENT_BOUNDS.items():
        raw = data.get(nutrient)
        if isinstance(raw, dict):
            min_val = _to_float(raw.get("min"))
            max_val = _to_float(raw.get("max"))
        else:
            # Tolerate a bare scalar being passed as a minimum.
            min_val = _to_float(raw)
            max_val = None

        if min_val is not None:
            min_val = max(lo, min(hi, min_val))
        if max_val is not None:
            max_val = max(lo, min(hi, max_val))

        # Inverted bounds: swap rather than silently dropping the constraint.
        if min_val is not None and max_val is not None and min_val > max_val:
            min_val, max_val = max_val, min_val

        cleaned[nutrient] = {"min": min_val, "max": max_val}

    intake = _to_float(data.get("daily_intake_kg"))
    if intake is None:
        intake = _to_float(expected_daily_intake_kg)
    if intake is not None:
        intake = max(0.5, min(60.0, intake))
    cleaned["daily_intake_kg"] = intake

    return cleaned


def validate_ration_feasibility(
    selected_feeds: List[Dict[str, Any]],
    constraints: Dict[str, Any]
) -> Dict[str, Any]:
    """Check whether the selected feeds can possibly satisfy the constraints.

    Compares each nutrient minimum against the richest available feed and each maximum
    against the poorest, since any mixture lands between those two extremes.

    Args:
        selected_feeds: Feed dicts as produced by ``FeedIngredient.to_dict()``.
        constraints: Sanitized nested constraints from `sanitize_nutrient_constraints`.

    Returns:
        ``{"feasible": bool, "warnings": List[str]}`` — Turkish warnings for the user.
    """
    warnings: List[str] = []

    if not selected_feeds:
        return {"feasible": False, "warnings": ["Hiç yem maddesi seçilmedi"]}
    if len(selected_feeds) < 2:
        warnings.append("En az 2 yem maddesi seçilmeli; tek yemle dengeli rasyon kurulamaz")

    for nutrient, feed_key in _FEED_NUTRIENT_KEYS.items():
        bounds = constraints.get(nutrient)
        if not isinstance(bounds, dict):
            continue

        min_val = bounds.get("min")
        max_val = bounds.get("max")
        if min_val is None and max_val is None:
            continue

        values = [
            v for v in (_to_float(f.get(feed_key)) for f in selected_feeds)
            if v is not None
        ]
        if not values:
            continue

        label = _NUTRIENT_LABELS_TR.get(nutrient, nutrient)
        if min_val is not None and max(values) < min_val:
            warnings.append(
                f"{label} hedefi ({min_val:g}) seçilen yemlerin en yükseğinden "
                f"({max(values):g}) fazla - bu hedefe ulaşılamaz"
            )
        if max_val is not None and min(values) > max_val:
            warnings.append(
                f"{label} üst sınırı ({max_val:g}) seçilen yemlerin en düşüğünden "
                f"({min(values):g}) az - bu sınır aşılacak"
            )

    return {"feasible": len(warnings) == 0, "warnings": warnings}