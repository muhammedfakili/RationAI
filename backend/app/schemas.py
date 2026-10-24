from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

AnimalGroup = Literal["lactating", "dry_faroff", "dry_closeup"]


class AnimalProfileIn(BaseModel):
    """Animal profile; NASEM 2021 targets are derived from it (requirements.py)."""
    group: AnimalGroup = Field("lactating", description="lactating / dry_faroff / dry_closeup")
    body_weight_kg: float = Field(650, ge=300, le=1000)
    milk_kg: float = Field(30, ge=0, le=70, description="Daily milk yield (ignored for dry cows)")
    fat_pct: float = Field(3.8, ge=2.0, le=6.5)
    true_protein_pct: float = Field(3.2, ge=2.4, le=4.5)
    lactose_pct: float = Field(4.85, ge=4.0, le=5.5)
    days_in_milk: int = Field(120, ge=1, le=600)
    multiparous: bool = Field(True, description="False = first lactation / heifer")
    bcs: float = Field(3.0, ge=1.0, le=5.0)
    days_pregnant: int = Field(0, ge=0, le=285, description="0 = not pregnant (dry groups use a default)")


class RequirementsResponse(BaseModel):
    """Daily requirements and the DM-concentration targets the solver gets."""
    group: AnimalGroup
    group_label: str
    dm_intake_kg: float
    nel_mcal_day: float
    nel_min: float = Field(..., description="Mcal NEL/kg DM")
    nel_max: float
    mp_g_day: float
    mp_min: float = Field(..., description="g metabolizable protein / kg DM")
    cp_kg_day: float
    cp_min: Optional[float] = Field(None, description="% DM — dry-cow floor only")
    cp_max: float
    calcium_min: float = Field(..., description="g/kg DM (= % x 10)")
    calcium_max: float
    phosphorus_min: float = Field(..., description="g/kg DM (= % x 10)")
    phosphorus_max: float
    energy_deficit_mcal: float
    notes: List[str]
    # Grubun rasyon kuralları — UI'nin kontrol kartları bunlara göre hedefte/dışı der.
    ndf_min: float
    ndf_max: float
    forage_ratio_min: float = Field(..., description="% of DM")
    forage_ratio_max: float
    starch_max: float = Field(..., description="% DM")
    fat_max: float = Field(..., description="Crude fat (EE) % DM")


class FeedResponse(BaseModel):
    """A feed ingredient as stored in the database."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
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
    starch: Optional[float] = Field(None, ge=0, le=100, description="Starch % DM")
    ether_extract: Optional[float] = Field(None, ge=0, le=100, description="Crude fat (EE) % DM")
    rup: Optional[float] = Field(None, ge=0, le=100, description="Rumen-undegradable protein, % of CP")
    max_kg_per_day: Optional[float] = Field(
        None, ge=0,
        description="Animal-health ceiling: max kg as-fed per day for a 600 kg cow "
                    "(scaled by body weight in the solver). None = no as-fed limit.",
    )
    price_source: Optional[str] = Field(
        None, description="Where the price came from: exchange name + date, or 'taban fiyat'"
    )
    price_updated_at: Optional[datetime] = Field(None, description="When the price was last set")


class OptimizationRequest(BaseModel):
    """Request body for POST /api/v1/optimize. All nutrient bounds are DM basis.

    With `animal`, every target left as None is filled from the NASEM 2021 requirements
    and the group's ration rules apply; explicitly sent targets always win. Without it
    the targets are exactly what is sent and the lactating rules apply.
    """
    body_weight_kg: float = Field(600, ge=300, le=1000, description="Body weight in kg")
    selected_feed_names: List[str] = Field(default_factory=list, description="Feed names to include")
    animal: Optional[AnimalProfileIn] = Field(None, description="Derive targets + rules from this profile")

    crude_protein_min: Optional[float] = Field(None, ge=0, le=30, description="Min CP % DM")
    crude_protein_max: Optional[float] = Field(None, ge=0, le=30, description="Max CP % DM")
    metabolic_energy_min: Optional[float] = Field(None, ge=0, le=5, description="Min ME Mcal/kg DM")
    metabolic_energy_max: Optional[float] = Field(None, ge=0, le=5, description="Max ME Mcal/kg DM")
    net_energy_lactation_min: Optional[float] = Field(None, ge=0, le=3, description="Min NEL Mcal/kg DM")
    net_energy_lactation_max: Optional[float] = Field(None, ge=0, le=3, description="Max NEL Mcal/kg DM")
    metabolizable_protein_min: Optional[float] = Field(
        None, ge=0, le=200, description="Min metabolizable protein, g/kg DM (NRC 2001 MP supply)"
    )

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

    target_dm_intake_kg: Optional[float] = Field(None, ge=5, le=35, description="Target DM intake kg/day")

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


class NutrientContent(BaseModel):
    """Ration nutrient concentrations (DM basis unless suffixed `_af`)."""
    crude_protein_percent_dm: float
    metabolic_energy_mcal_per_kg_dm: float
    calcium_g_per_kg_dm: float
    phosphorus_g_per_kg_dm: float
    dry_matter_percent: float = Field(..., description="Ration DM % as-fed")
    net_energy_lactation_mcal_per_kg_dm: Optional[float] = None
    ndf_percent_dm: Optional[float] = None
    forage_ndf_percent_dm: Optional[float] = Field(None, description="NDF from forages, % DM")
    forage_ratio_percent: Optional[float] = Field(None, description="Forage share of ration DM, %")
    ca_p_ratio: Optional[float] = None
    crude_protein_percent_af: Optional[float] = None
    metabolic_energy_mcal_per_kg_af: Optional[float] = None
    calcium_g_per_kg_af: Optional[float] = None
    phosphorus_g_per_kg_af: Optional[float] = None
    starch_percent_dm: Optional[float] = None
    ether_extract_percent_dm: Optional[float] = None
    rdp_percent_dm: Optional[float] = None
    rup_percent_dm: Optional[float] = None
    mp_g_per_kg_dm: Optional[float] = Field(None, description="Metabolizable protein supply, g/kg DM")
    rdp_balance_g: Optional[float] = Field(None, description="RDP minus what rumen microbes need, g/day")


class DailyNutrientIntake(BaseModel):
    """Daily nutrient intake amounts (totals per day, not concentrations)."""
    dry_matter_kg: float
    crude_protein_kg: float
    metabolic_energy_mcal: float
    calcium_g: float
    phosphorus_g: float
    net_energy_lactation_mcal: Optional[float] = None
    ndf_kg: Optional[float] = None
    mp_g: Optional[float] = Field(None, description="Metabolizable protein supply, g/day")


class FeedRecommendation(BaseModel):
    """One feed in the optimized ration."""
    name: str
    amount_kg_per_day: float
    proportion: float = Field(..., description="Share of the as-fed ration, 0-1")
    price_per_kg: float
    cost_per_day: float


class ConstraintStatus(BaseModel):
    """Whether one nutrient bound was met."""
    constraint: str
    actual: float
    status: str
    unit: str
    turkish_explanation: Optional[str] = None


class OptimizationResponse(BaseModel):
    """Optimization result shared by /optimize and the agent tool."""
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


class AgentQuery(BaseModel):
    query: str


class AgentResponse(BaseModel):
    """Agent query response."""
    success: bool = True
    answer: Optional[str] = None
    response: Optional[str] = None
    optimization_result: Optional[Dict[str, Any]] = None


class HealthCheckResponse(BaseModel):
    status: str
    timestamp: datetime
    database_connected: bool
    chromadb_connected: bool
    llm_available: bool


class FeedPriceUpdate(BaseModel):
    """A manually entered feed price."""
    name: str
    price_per_kg: float


class PriceSyncResponse(BaseModel):
    """Result of a feed price synchronization."""
    updated_at: str
    updated_feeds_count: int
    updated_feeds: List[str]
    baseline_feeds_count: int
    source: str
    message: str
    kept_manual_feeds: List[str] = Field(
        default_factory=list, description="Feeds left on the user's own saved price"
    )
