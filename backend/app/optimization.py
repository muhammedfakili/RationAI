"""
RationAI - Lineer Programlama ile Rasyon Optimizasyonu
PuLP tabanlı en düşük maliyetli yem rasyonu hesaplayıcı.

Birim Tutarlılığı (TÜM KISITLAR KURU MADDE - KM BAZINDA):
- dry_matter: % (örn: 86.7)
- crude_protein: % KM bazında (örn: 10.5)
- metabolic_energy: Mcal/kg KM (örn: 3.05 Mcal/kg KM)
- net_energy_lactation: Mcal/kg KM (NEL; hayvan profilinden gelen hedefler bunu kullanır)
- calcium: g/kg KM (örn: 7 g/kg KM = 0.7%)
- phosphorus: g/kg KM (örn: 3.8 g/kg KM = 0.38%)
- price: TL/kg (doğal hali/as-fed bazında fiyat)
"""

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pulp

# schemas.py bu modülü import ETMEZ - tek yönlü bağımlılık, döngü yok.
from .schemas import (
    ConstraintStatus,
    DailyNutrientIntake,
    FeedRecommendation,
    NutrientContent,
    OptimizationResponse,
)

logger = logging.getLogger(__name__)


@dataclass
class FeedIngredient:
    """Yem bileşeni veri sınıfı - tüm besin değerleri KM bazında (fiyat hariç)."""
    name: str
    price_per_kg: float          # TL/kg (doğal hali/as-fed bazında fiyat)
    dry_matter: float            # % (örn: 86.7)
    crude_protein: float         # % KM
    metabolic_energy: float      # Mcal/kg KM
    calcium: float               # g/kg KM
    phosphorus: float            # g/kg KM
    # NDF rasyonun yenebilirliğini belirler: lif fazlaysa inek rasyonu fiziksel olarak
    # tüketemez. Sıfır bırakılırsa NDF kısıtları o yem için etkisiz kalır.
    ndf: float = 0.0                     # % KM (Nötr Deterjan Lif)
    net_energy_lactation: float = 0.0    # Mcal/kg KM (NEL)
    feed_type: str = "unknown"   # silage/hay/straw/grain/protein/byproduct/fat/mineral/salt
    # Hayvan sağlığı sınırı: 600 kg canlı ağırlık için günlük azami doğal hali miktar.
    # None = as-fed sınırı yok (KM yüzdesi tavanı yeterli).
    max_kg_per_day: Optional[float] = None

    def is_forage(self) -> bool:
        return self.feed_type in FORAGE_TYPES

    def health_cap_kg(self, body_weight_kg: Optional[float] = None) -> Optional[float]:
        """Canlı ağırlığa ölçeklenmiş günlük azami doğal hali miktar (kg/gün).

        Tablodaki değer 600 kg'lık inek içindir: 700 kg'da 25 kg silaj -> 29.2 kg.
        """
        if not self.max_kg_per_day:
            return None
        if not body_weight_kg or body_weight_kg <= 0:
            return self.max_kg_per_day
        return self.max_kg_per_day * (body_weight_kg / HEALTH_CAP_REFERENCE_BW_KG)

    def dm_fraction(self) -> float:
        """Kuru madde kesiri (0-1 arası)."""
        return self.dry_matter / 100.0


@dataclass
class NutrientConstraints:
    """Besin maddesi kısıtlamaları - HEPSİ KM BAZINDA (kuru madde hariç)."""
    crude_protein_min: Optional[float] = None      # % KM
    crude_protein_max: Optional[float] = None
    metabolic_energy_min: Optional[float] = None   # Mcal/kg KM
    metabolic_energy_max: Optional[float] = None
    net_energy_lactation_min: Optional[float] = None   # Mcal/kg KM
    net_energy_lactation_max: Optional[float] = None
    calcium_min: Optional[float] = None            # g/kg KM
    calcium_max: Optional[float] = None
    phosphorus_min: Optional[float] = None         # g/kg KM
    phosphorus_max: Optional[float] = None
    dry_matter_min: Optional[float] = None         # % doğal hali
    dry_matter_max: Optional[float] = None
    daily_intake_kg: Optional[float] = None        # kg KM/gün (sabitlenir)


@dataclass
class OptimizationResult:
    """Optimizasyon sonucu."""
    status: str
    feed_amounts: Dict[str, float]           # kg/gün (doğal hali)
    feed_proportions: Dict[str, float]       # oran (toplam = 1.0)
    nutrient_content: Dict[str, float]       # rasyon besin değeri (KM bazında)
    daily_nutrient_intake: Dict[str, float]  # kg/gün veya Mcal/gün cinsinden alım
    total_cost_per_day: float                # TL/gün (sadece yem maliyeti)
    total_cost_per_kg: float                 # TL/kg rasyon (doğal hali)
    total_daily_intake_kg: float             # kg/gün doğal hali
    constraints_status: Dict[str, str]
    violations: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    adjustments: List[str] = field(default_factory=list)  # Türkçe açıklamalar
    solver_details: Dict[str, Any] = field(default_factory=dict)


# ============================================================
# RASYON KURALLARI (süt ineği, laktasyon dönemi)
#
# Yem sınıfı, rasyon kurallarının tamamının dayanağıdır. Kaynak `Feed.feed_type`
# sütunudur; boşsa "unknown" sayılır — o yemi `database._CURATED_FEEDS`'e ekleyin.
# ============================================================
# `Feed.max_kg_per_day` değerlerinin referans aldığı canlı ağırlık.
HEALTH_CAP_REFERENCE_BW_KG = 600.0

FORAGE_TYPES = frozenset({"silage", "hay", "straw"})

# Tek bir yemin rasyon kuru maddesindeki azami payı.
# ⚠️ Bu tavanlar rasyon kalitesinin belkemiğidir: saman KM başına en ucuz yem olduğu
# için tavan konmazsa çözücü onu %90'a kadar yükler ve inek rasyonu yiyemez.
MAX_INCLUSION_BY_TYPE = {
    "silage": 0.55,
    "hay": 0.30,
    "straw": 0.08,
    "grain": 0.30,       # asidoz koruması
    "protein": 0.25,
    "byproduct": 0.20,
    "fat": 0.04,         # korunmuş yağ; rasyon toplam yağı %6-7'yi aşmamalı
    "mineral": 0.015,
    "salt": 0.005,
    "unknown": 0.20,
}

# Silaj seçilmediyse kaba yemin tabanı kuru ottur; pencereler genişler.
# Bu uyarlama olmadan silajsız (kuru) rasyon %45 kaba yem oranına ulaşamayıp
# çözümsüz kalıyor.
MAX_INCLUSION_NO_SILAGE = {"hay": 0.60, "straw": 0.12}


@dataclass(frozen=True)
class RationRules:
    """Bir hayvan grubunun rasyon kuralları (oranlar KM payı, NDF % KM)."""
    label: str
    max_inclusion: Dict[str, float]
    max_inclusion_no_silage: Dict[str, float]
    ndf_min: float
    ndf_max: float
    forage_ndf_min: float           # peNDF vekili
    forage_ratio_min: float
    forage_ratio_max: float
    silage_base_min: float          # silaj seçiliyse kaba yemin tabanı; 0 = kural yok
    hay_agg_max_with_silage: Optional[float]


# ⚠️ Kuru inek sağmal kurallarına zorlanırsa (NDF ≤ %35, saman ≤ %8) enerji yoğun,
# yağlandıran bir rasyon çıkar. Kuru dönemde saman bilerek kaba yemin taşıyıcısıdır.
RULES_BY_GROUP = {
    "lactating": RationRules(
        label="Sağmal",
        max_inclusion=MAX_INCLUSION_BY_TYPE,
        max_inclusion_no_silage=MAX_INCLUSION_NO_SILAGE,
        ndf_min=28.0, ndf_max=35.0, forage_ndf_min=19.0,
        forage_ratio_min=0.45, forage_ratio_max=0.65,
        silage_base_min=0.25, hay_agg_max_with_silage=0.40,
    ),
    "dry_faroff": RationRules(
        label="Kuru (erken)",
        max_inclusion={**MAX_INCLUSION_BY_TYPE, "silage": 0.40, "hay": 0.60, "straw": 0.35,
                       "grain": 0.10, "protein": 0.15, "fat": 0.0},
        max_inclusion_no_silage={"hay": 0.80, "straw": 0.45},
        ndf_min=40.0, ndf_max=55.0, forage_ndf_min=30.0,
        forage_ratio_min=0.70, forage_ratio_max=0.90,
        silage_base_min=0.0, hay_agg_max_with_silage=None,
    ),
    "dry_closeup": RationRules(
        label="Kuru (doğuma yakın)",
        max_inclusion={**MAX_INCLUSION_BY_TYPE, "silage": 0.45, "hay": 0.50, "straw": 0.25,
                       "grain": 0.15, "protein": 0.20, "fat": 0.02},
        max_inclusion_no_silage={"hay": 0.75, "straw": 0.35},
        ndf_min=35.0, ndf_max=45.0, forage_ndf_min=25.0,
        forage_ratio_min=0.60, forage_ratio_max=0.80,
        silage_base_min=0.0, hay_agg_max_with_silage=None,
    ),
}
LACTATING_RULES = RULES_BY_GROUP["lactating"]

# Minimum dahil oranları (yumuşak): karışımın homojenliği ve iz element gereksinimi.
MIN_INCLUSION_BY_TYPE = {
    "silage": 0.05,
    "hay": 0.03,
    "straw": 0.0,
    "grain": 0.03,
    "protein": 0.03,
    "byproduct": 0.02,
    "fat": 0.0,
    "mineral": 0.001,
    "salt": 0.001,
    "unknown": 0.01,
}

NDF_INTAKE_PCT_BW = 1.2                        # kg NDF/gün <= %1.2 x canlı ağırlık
CA_P_RATIO_MIN, CA_P_RATIO_MAX = 1.5, 2.5
MIN_DAILY_DM_INTAKE_KG = 12.0                  # hedef verilmezse önemsiz çözümleri engeller
SALT_MIN_PCT_DM = 0.25                         # seçiliyse tuz en az bu kadar (% KM)

# Protein korumalı koridor: bu yemlerden biri seçiliyse HP < %12 KM ağır cezalandırılır.
PROTEIN_GUARDRAIL_PCT = 12.0
PROTEIN_RICH_FEEDS = frozenset({
    "Soya Küspesi", "Soya-Fullfat", "Ayçiçeği Küspesi", "Yonca Kuru Otu", "Yonca", "Yonca Silajı",
})

# Ceza çarpanları (soft_constraint_penalty'nin katları).
PENALTY_MAX_INCLUSION = 5.0
PENALTY_FORCED_INCLUSION = 30.0   # "seçtiğim yemi kullan" ekonomik tercihlerin çok önünde
PENALTY_CA_P_RATIO = 2.0
PENALTY_RATION_RULE = 4.0
PENALTY_PROTEIN_GUARDRAIL = 10.0

# Besin sınırları: kısıt öneki -> (FeedIngredient alanı, KM başına katsayı bölücüsü,
# nutrient_content anahtarı, birim, Türkçe ad, gösterim hanesi).
# HP % KM olduğu için 100'e bölünür: slack birimi kg HP/gün kalır.
NUTRIENTS = {
    "crude_protein": ("crude_protein", 100.0, "crude_protein_percent_dm", "% KM", "Ham protein", 1),
    "metabolic_energy": ("metabolic_energy", 1.0, "metabolic_energy_mcal_per_kg_dm", "Mcal/kg KM", "Metabolik enerji", 2),
    "net_energy_lactation": ("net_energy_lactation", 1.0, "net_energy_lactation_mcal_per_kg_dm", "Mcal/kg KM", "Net enerji (NEL)", 2),
    "calcium": ("calcium", 1.0, "calcium_g_per_kg_dm", "g/kg KM", "Kalsiyum", 1),
    "phosphorus": ("phosphorus", 1.0, "phosphorus_g_per_kg_dm", "g/kg KM", "Fosfor", 1),
}
DRY_MATTER_BOUND = ("dry_matter_percent", "%", "Kuru madde", 1)

_EMPTY_CONTENT_KEYS = (
    "crude_protein_percent_dm", "metabolic_energy_mcal_per_kg_dm", "calcium_g_per_kg_dm",
    "phosphorus_g_per_kg_dm", "dry_matter_percent", "net_energy_lactation_mcal_per_kg_dm",
    "ndf_percent_dm", "forage_ndf_percent_dm", "forage_ratio_percent",
    "crude_protein_percent_af", "metabolic_energy_mcal_per_kg_af", "calcium_g_per_kg_af",
    "phosphorus_g_per_kg_af",
)
_INTAKE_KEYS = (
    "crude_protein_kg", "metabolic_energy_mcal", "calcium_g", "phosphorus_g",
    "dry_matter_kg", "ndf_kg", "net_energy_lactation_mcal",
)


def _max_inclusion_windows(feeds: List[FeedIngredient], rules: RationRules) -> Dict[str, float]:
    """Seçime uyarlanmış azami dahil tavanlarını (yem tipi -> oran) döndürür."""
    windows = dict(rules.max_inclusion)
    if not any(f.feed_type == "silage" for f in feeds):
        windows.update(rules.max_inclusion_no_silage)
    return windows


def feeds_from_db(db_feeds) -> List[FeedIngredient]:
    """Veritabanı Feed satırlarını FeedIngredient listesine çevirir (değerler zaten KM bazında)."""
    return [
        FeedIngredient(
            name=feed.name,
            # 0 fiyatlı değişkeni çözücü sınırsız artırabilir -> çok küçük bir fiyat ver.
            price_per_kg=feed.price_per_kg if feed.price_per_kg > 0 else 0.001,
            dry_matter=feed.dry_matter,
            crude_protein=feed.crude_protein,
            metabolic_energy=feed.metabolic_energy,
            net_energy_lactation=feed.net_energy_lactation or 0.0,
            ndf=feed.ndf or 0.0,
            calcium=feed.calcium,
            phosphorus=feed.phosphorus,
            feed_type=feed.feed_type or "unknown",
            max_kg_per_day=feed.max_kg_per_day,
        )
        for feed in db_feeds
    ]


def _empty_result(status: str, feeds: List[FeedIngredient], **kwargs) -> OptimizationResult:
    return OptimizationResult(
        status=status,
        feed_amounts={f.name: 0.0 for f in feeds},
        feed_proportions={f.name: 0.0 for f in feeds},
        nutrient_content={},
        daily_nutrient_intake={},
        total_cost_per_day=0.0,
        total_cost_per_kg=0.0,
        total_daily_intake_kg=0.0,
        constraints_status={},
        **kwargs,
    )


def solve_ration_problem(
    feeds: List[FeedIngredient],
    constraints: NutrientConstraints,
    soft_constraint_penalty: float = 100000.0,
    max_daily_intake_kg: Optional[float] = 50.0,
    expected_daily_intake_kg: float = 20.0,
    force_use_all_feeds: bool = False,
    min_inclusion_pct_dm: float = 1.0,
    hard_nutrients: bool = False,
    body_weight_kg: Optional[float] = None,
    hard_ration_rules: bool = True,
    rules: RationRules = LACTATING_RULES,
) -> OptimizationResult:
    """En düşük maliyetli rasyonu çözer. TÜM KISITLAR KM BAZINDADIR.

    Args:
        soft_constraint_penalty: Birim ihlal başına temel ceza (TL); beklenen günlük
            tüketime (`expected_daily_intake_kg`) bölünerek normalize edilir.
        max_daily_intake_kg: Azami günlük doğal hali tüketim (KATI güvenlik sınırı).
        force_use_all_feeds: Seçilen tüm yemleri rasyona sokmaya zorlar (pahalı-yumuşak).
        min_inclusion_pct_dm: Zorunlu dahil hedefi (% KM); yem tipi tavanına göre kısılır.
        hard_nutrients: True ise HP/ME/Ca/P KATI kurulur (model infeasible dönebilir).
            İki aşamalı akış için `optimize_ration()` kullan.
        body_weight_kg: Verilirse NDF alım kapasitesi (kg NDF/gün <= %1.2 x CA) uygulanır.
        hard_ration_rules: True ise yem sınıfı tavanları, NDF penceresi, kaba yem oranı
            ve silaj tabanı KATI; False iken cezalı-yumuşak (ikinci faz).
        rules: Hayvan grubunun rasyon kuralları (`RULES_BY_GROUP`); varsayılan sağmal.
    """
    if not feeds:
        return _empty_result("Yem bileşeni yok", feeds, adjustments=["Hiç yem bileşeni sağlanmadı."])

    adjustments: List[str] = []
    prob = pulp.LpProblem("Ration_Optimization", pulp.LpMinimize)

    # ⚠️ PuLP TUZAĞI: `prob += ifade` o anki ifadenin KOPYASINI alır. Aşağıdaki bloklar
    # cezaları `penalties` listesine ekler; amaç fonksiyonu prob.solve()'dan hemen önce
    # TEK SEFERDE atanır. Yeni bir ceza terimi o satırın ÜSTÜNDE eklenmeli.
    penalties = []

    def soft(name: str, weight: float):
        """Cezalı yeni bir slack değişkeni döndürür (ceza tüketime göre normalize)."""
        var = pulp.LpVariable(name, lowBound=0)
        penalties.append(weight * soft_constraint_penalty * var / expected_daily_intake_kg)
        return var

    # Karar değişkenleri: her yem için günlük miktar (kg doğal hali).
    # ⚠️ HAYVAN SAĞLIĞI SINIRI değişkenin ÜST SINIRI olarak kurulur, kısıt satırı olarak
    # değil: slack takılamaz ve her iki fazda da geçerlidir. 25 kg'lık silaj tavanı
    # hiçbir koşulda aşılamaz — bedeli, ikinci fazın da infeasible dönebilmesi.
    x = {}
    health_caps: Dict[str, float] = {}
    for feed in feeds:
        cap = feed.health_cap_kg(body_weight_kg)
        x[feed.name] = pulp.LpVariable(f"feed_{feed.name}", lowBound=0, upBound=cap)
        if cap is not None:
            health_caps[feed.name] = cap
    if health_caps:
        detail = ", ".join(f"{name} ≤ {cap:.1f} kg" for name, cap in sorted(health_caps.items()))
        adjustments.append(f"🩺 Hayvan sağlığı sınırları (günlük doğal hali): {detail}")

    def dm_sum(subset, value=lambda f: 1.0):
        """Σ (yem KM kg/gün x değer) — değer KM başına besin içeriği."""
        return pulp.lpSum(f.dm_fraction() * value(f) * x[f.name] for f in subset)

    total_intake = pulp.lpSum(x.values())
    total_dm = dm_sum(feeds)
    feed_cost = pulp.lpSum(f.price_per_kg * x[f.name] for f in feeds)

    # ---- Protein korumalı koridor (HP >= %12 KM, çok pahalı yumuşak) ----
    total_cp = dm_sum(feeds, lambda f: f.crude_protein / 100.0)
    if any(f.name in PROTEIN_RICH_FEEDS for f in feeds):
        slack = soft("protein_guardrail_slack", PENALTY_PROTEIN_GUARDRAIL)
        prob += total_cp >= PROTEIN_GUARDRAIL_PCT / 100.0 * total_dm - slack, "Protein_Guardrail_Min_12pct"
        adjustments.append(
            f"🛡️ Protein korumalı koridor aktif: Soya/Ayçiçeği/Yonca yemleri mevcut -> "
            f"CP <%12 KM cezalandırılır (ceza katsayısı: {PENALTY_PROTEIN_GUARDRAIL:.0f}x)."
        )

    # ---- Tüketim sınırları (KATI) ----
    if max_daily_intake_kg:
        prob += total_intake <= max_daily_intake_kg, "Max_Intake"
        prob += total_intake >= 0.1, "Min_Intake"
    # Taban yalnızca hedef yokken: doğuma yakın düvenin KMT'si 10 kg'ın altındadır.
    prob += total_dm >= (constraints.daily_intake_kg or MIN_DAILY_DM_INTAKE_KG), "Min_DM_Intake"
    if constraints.daily_intake_kg is not None:
        prob += total_dm == constraints.daily_intake_kg, "Fixed_DM_Intake"
        # Rasyon KM'si en az %35 varsayımıyla doğal hali güvenlik tavanı.
        prob += total_intake <= constraints.daily_intake_kg / 0.35, "Max_AsFed_Intake_Safety"

    # ============================================================
    # YEM SINIFI BAZLI DAHİL PENCERELERİ
    # ⚠️ Tavan HER yeme uygulanır — eskiden kaba yemler tavansızdı ve saman
    # rasyonun %90'ına kadar çıkabiliyordu.
    # ============================================================
    windows = _max_inclusion_windows(feeds, rules)

    if force_use_all_feeds:
        # İstenen minimum yem tipine göre kısılır: tuzu %3 koymaya zorlamak modeli
        # çözümsüz yapar (tavanın yarısını geçme), n yem toplamda %80 KM'yi aşmasın.
        # Kısıt pahalı-yumuşaktır: besin hedefiyle gerçekten çakışan yem düşürülür.
        requested = min_inclusion_pct_dm / 100.0
        applied = {}
        for feed in feeds:
            frac = min(requested, windows.get(feed.feed_type, 0.20) * 0.5, 0.80 / len(feeds))
            if frac <= 0:
                continue
            applied[feed.name] = frac
            slack = soft(f"forced_incl_slack_{feed.name}", PENALTY_FORCED_INCLUSION)
            prob += (feed.dm_fraction() * x[feed.name] >= frac * total_dm - slack,
                     f"Force_Min_Inclusion_{feed.name}")
        if applied:
            detail = ", ".join(f"{name} %{frac * 100:.2f}" for name, frac in sorted(applied.items()))
            adjustments.append(
                f"🔒 ZORUNLU DAHİL: Seçilen {len(applied)} yemin tamamı rasyona "
                f"katılıyor (hedef %{min_inclusion_pct_dm:.1f} KM, yem tipine göre "
                f"uygulanan: {detail})"
            )

    windowed: Dict[str, List[str]] = {}
    # KM için büyük-M: doğal hali tüketim tavanı KM'yi de sınırlar.
    big_m = max_daily_intake_kg or 50.0
    for feed in feeds:
        min_frac = MIN_INCLUSION_BY_TYPE.get(feed.feed_type, 0.01)
        # Zorunlu dahil kapalıyken minimum "ya hiç ya en az" kuralıdır (yarı sürekli).
        # ⚠️ Eskiden yumuşak bir alt sınırdı: her yemi rasyona sokmanın bir cezası vardı,
        # "en ucuz kombinasyon" isteyene 28-33 yemlik rasyon çıkıyordu.
        # Zorunlu dahil açıkken kurulmaz (o zaman her yem zaten zorunlu).
        if min_frac > 0 and not force_use_all_feeds:
            used = pulp.LpVariable(f"used_{feed.name}", cat="Binary")
            dm_i = feed.dm_fraction() * x[feed.name]
            prob += dm_i >= min_frac * total_dm - big_m * (1 - used), f"Min_Inclusion_{feed.name}"
            prob += dm_i <= big_m * used, f"Used_{feed.name}"
            windowed.setdefault(feed.feed_type, []).append(feed.name)

        max_slack = 0 if hard_ration_rules else soft(f"max_incl_slack_{feed.name}", PENALTY_MAX_INCLUSION)
        prob += (feed.dm_fraction() * x[feed.name] <= windows.get(feed.feed_type, 0.20) * total_dm + max_slack,
                 f"Max_Inclusion_{feed.name}")

    if windowed:
        parts = [
            f"{ftype} (%{MIN_INCLUSION_BY_TYPE.get(ftype, 0.01) * 100:.1f}-"
            f"%{windows.get(ftype, 0.20) * 100:.1f} KM): {', '.join(names)}"
            for ftype, names in windowed.items()
        ]
        mode = "KATI" if hard_ration_rules else "yumuşak"
        adjustments.append(
            f"🔒 Yem sınıfı dahil pencereleri ({mode}; kullanılan yem en az alt sınır kadar "
            f"girer): {'; '.join(parts)}"
        )

    # ---- Besin hedefleri (HP, ME, Ca, P) ----
    # hard_nutrients=True iken slack yok -> hedefler GARANTİ edilir (model infeasible olabilir).
    if hard_nutrients:
        adjustments.append(
            "🎯 Besin hedefleri KATI kısıt olarak uygulanıyor: maliyet ancak HP/ME/Ca/P "
            "hedeflerinin tamamı sağlandığı sürece minimize edilir."
        )
    nutrient_totals = {}
    for key, (attr, divisor, *_rest) in NUTRIENTS.items():
        total = dm_sum(feeds, lambda f, a=attr, d=divisor: getattr(f, a) / d)
        nutrient_totals[key] = total
        low = getattr(constraints, f"{key}_min")
        high = getattr(constraints, f"{key}_max")
        if low is not None:
            slack = 0 if hard_nutrients else soft(f"slack_{key}_min", 1.0)
            prob += total >= low / divisor * total_dm - slack, f"{key}_min"
        if high is not None:
            slack = 0 if hard_nutrients else soft(f"slack_{key}_max", 1.0)
            prob += total <= high / divisor * total_dm + slack, f"{key}_max"

    # Ca:P ORANI — Ca ve P tek tek hedefte olsa bile oran bozuk olabilir. Her zaman
    # yumuşak: sınırlı yem seçkisinde oranı katı tutmak modeli gereksiz yere çözümsüz bırakır.
    total_ca, total_p = nutrient_totals["calcium"], nutrient_totals["phosphorus"]
    prob += total_ca >= CA_P_RATIO_MIN * total_p - soft("ca_p_ratio_low_slack", PENALTY_CA_P_RATIO), "CaP_Ratio_Min"
    prob += total_ca <= CA_P_RATIO_MAX * total_p + soft("ca_p_ratio_high_slack", PENALTY_CA_P_RATIO), "CaP_Ratio_Max"
    adjustments.append(f"⚖️ Ca:P oranı {CA_P_RATIO_MIN}-{CA_P_RATIO_MAX} aralığında tutulmaya çalışılıyor.")

    # Kuru madde % (doğal hali) — her zaman yumuşak.
    if constraints.dry_matter_min is not None:
        prob += (total_dm >= constraints.dry_matter_min / 100.0 * total_intake
                 - soft("slack_dry_matter_min", 1.0)), "DM_Min"
    if constraints.dry_matter_max is not None:
        prob += (total_dm <= constraints.dry_matter_max / 100.0 * total_intake
                 + soft("slack_dry_matter_max", 1.0)), "DM_Max"

    # ============================================================
    # RASYONUN YENEBİLİRLİĞİ: NDF ve kaba yem kuralları
    # NDF penceresi olmadan çözücü en ucuz lif kaynağını (saman) yükleyip NDF'yi
    # %43'e çıkarıyor; o rasyon kâğıt üzerinde doğru, ahırda yenmez.
    # ============================================================
    def rule_slack(name: str):
        return 0 if hard_ration_rules else soft(f"rule_slack_{name}", PENALTY_RATION_RULE)

    total_ndf = dm_sum(feeds, lambda f: f.ndf)
    has_ndf_data = any(f.ndf > 0 for f in feeds)
    if has_ndf_data:
        prob += total_ndf >= rules.ndf_min * total_dm - rule_slack("ndf_min"), "NDF_Min"
        prob += total_ndf <= rules.ndf_max * total_dm + rule_slack("ndf_max"), "NDF_Max"
        adjustments.append(
            f"🌱 Rasyon NDF'si %{rules.ndf_min:.0f}-%{rules.ndf_max:.0f} KM aralığında tutuluyor "
            f"({rules.label})."
        )
        if body_weight_kg:
            ndf_capacity_kg = NDF_INTAKE_PCT_BW / 100.0 * body_weight_kg
            # total_ndf birimi kg KM x % KM -> 100'e bölünce kg NDF/gün
            prob += total_ndf / 100.0 <= ndf_capacity_kg + rule_slack("ndf_capacity"), "NDF_Intake_Capacity"
            adjustments.append(
                f"📏 NDF alım kapasitesi: en fazla {ndf_capacity_kg:.1f} kg NDF/gün "
                f"(canlı ağırlığın %{NDF_INTAKE_PCT_BW})."
            )

    forage_feeds = [f for f in feeds if f.is_forage()]
    silage_feeds = [f for f in feeds if f.feed_type == "silage"]
    hay_feeds = [f for f in feeds if f.feed_type == "hay"]

    if forage_feeds:
        forage_dm = dm_sum(forage_feeds)
        prob += forage_dm >= rules.forage_ratio_min * total_dm - rule_slack("forage_min"), "Min_Forage_Ratio"
        prob += forage_dm <= rules.forage_ratio_max * total_dm + rule_slack("forage_max"), "Max_Forage_Ratio"
        adjustments.append(
            f"🌾 Kaba yem oranı %{rules.forage_ratio_min * 100:.0f}-%{rules.forage_ratio_max * 100:.0f} KM: "
            f"{', '.join(f.name for f in forage_feeds)}"
        )
        if has_ndf_data:
            forage_ndf = dm_sum(forage_feeds, lambda f: f.ndf)
            prob += forage_ndf >= rules.forage_ndf_min * total_dm - rule_slack("forage_ndf"), "Forage_NDF_Min"

        # Silaj seçildiyse kaba yemin TABANI silajdır. Bu kural olmadan çözücü silajı
        # bırakıp kuru otu tavana dayıyor (yonca proteini soya küspesinden tasarruf ettiriyor).
        # Kuru dönemde kural yoktur: orada silaj enerjisi sınırlanması gereken şeydir.
        if silage_feeds and rules.silage_base_min > 0:
            prob += dm_sum(silage_feeds) >= rules.silage_base_min * total_dm - rule_slack("silage_base"), "Silage_Base_Min"
            adjustments.append(
                f"🌽 Silaj kaba yemin tabanı: en az %{rules.silage_base_min * 100:.0f} KM "
                f"({', '.join(f.name for f in silage_feeds)})."
            )
        if silage_feeds and hay_feeds and rules.hay_agg_max_with_silage is not None:
            prob += (dm_sum(hay_feeds) <= rules.hay_agg_max_with_silage * total_dm + rule_slack("hay_agg"),
                     "Hay_Aggregate_Max")
    else:
        adjustments.append("⚠️ UYARI: Rasyonda hiç kaba yem yok! Rumen sağlığı için silaj/kuru ot ekleyin.")

    # Tuzun modelde besin karşılığı yok (Na izlenmiyor): kural olmasa en ucuz rasyon
    # tuzu her zaman düşürürdü.
    salt_feeds = [f for f in feeds if f.feed_type == "salt"]
    if salt_feeds:
        prob += (dm_sum(salt_feeds) >= SALT_MIN_PCT_DM / 100.0 * total_dm - rule_slack("salt_min"),
                 "Salt_Min")
        adjustments.append(f"🧂 Tuz en az %{SALT_MIN_PCT_DM} KM.")

    # ==================== AMAÇ FONKSİYONU (EN SONDA, TEK SEFERDE) ====================
    prob += feed_cost + pulp.lpSum(penalties), "Total_Cost"

    try:
        prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=30, threads=1, gapRel=0.0001))
    except Exception as exc:
        logger.error("Solver error: %s", exc)
        return _empty_result(
            f"Solver Hatasi: {exc}", feeds,
            violations=[f"Solver istisnasi: {exc}"],
            adjustments=["Solver hatası oluştu, varsayılan değerler döndürülüyor."],
        )

    status = pulp.LpStatus[prob.status]
    # ⚠️ Infeasible bir çözümden sonra CBC'nin değişkenlerde bıraktığı değerler ANLAMSIZDIR.
    # Eskiden okunup "Feasible" diye etiketleniyordu ve 25 kg tavanlı silaj 73 kg
    # raporlandı. Infeasible sonuç sıfırlanır ve dürüstçe bildirilir.
    infeasible = status == "Infeasible"
    amounts = {f.name: 0.0 if infeasible or x[f.name].varValue is None else x[f.name].varValue for f in feeds}

    total_kg = sum(amounts.values())
    proportions = {name: (amt / total_kg if total_kg > 0 else 0.0) for name, amt in amounts.items()}

    def actual(value=lambda f: 1.0, subset=feeds) -> float:
        return sum(f.dm_fraction() * value(f) * amounts[f.name] for f in subset)

    nutrient_content: Dict[str, Any] = {key: 0 for key in _EMPTY_CONTENT_KEYS}
    daily_intake: Dict[str, float] = {key: 0.0 for key in _INTAKE_KEYS}
    actual_dm = actual()

    if total_kg > 0 and actual_dm > 0:
        cp = actual(lambda f: f.crude_protein / 100.0)
        me = actual(lambda f: f.metabolic_energy)
        ca = actual(lambda f: f.calcium)
        p = actual(lambda f: f.phosphorus)
        nel = actual(lambda f: f.net_energy_lactation)
        ndf = actual(lambda f: f.ndf) / 100.0           # kg NDF/gün
        forage_dm = actual(subset=forage_feeds)
        forage_ndf = actual(lambda f: f.ndf, forage_feeds) / 100.0

        nutrient_content.update({
            "crude_protein_percent_dm": cp / actual_dm * 100,
            "metabolic_energy_mcal_per_kg_dm": me / actual_dm,
            "calcium_g_per_kg_dm": ca / actual_dm,
            "phosphorus_g_per_kg_dm": p / actual_dm,
            "dry_matter_percent": actual_dm / total_kg * 100,
            "net_energy_lactation_mcal_per_kg_dm": nel / actual_dm,
            "ndf_percent_dm": ndf / actual_dm * 100,
            "forage_ndf_percent_dm": forage_ndf / actual_dm * 100,
            "forage_ratio_percent": forage_dm / actual_dm * 100,
            "crude_protein_percent_af": cp / total_kg * 100,
            "metabolic_energy_mcal_per_kg_af": me / total_kg,
            "calcium_g_per_kg_af": ca / total_kg,
            "phosphorus_g_per_kg_af": p / total_kg,
        })
        if p > 0:
            nutrient_content["ca_p_ratio"] = ca / p
        daily_intake.update({
            "crude_protein_kg": cp, "metabolic_energy_mcal": me, "calcium_g": ca,
            "phosphorus_g": p, "dry_matter_kg": actual_dm, "ndf_kg": ndf,
            "net_energy_lactation_mcal": nel,
        })

    actual_feed_cost = sum(f.price_per_kg * amounts[f.name] for f in feeds)

    # Yumuşak besin kısıtlarının ihlal miktarları (günlük toplam birimde).
    slack_values = {
        var.name: var.varValue for var in prob.variables()
        if var.name.startswith("slack_") and var.varValue and var.varValue > 1e-6
    }

    # ---- Kısıt durumu ve gevşetme açıklamaları ----
    constraints_status: Dict[str, str] = {}
    violations: List[str] = []
    warnings: List[str] = []

    if total_kg > 0 and actual_dm > 0:
        bounds = {key: spec[2:] for key, spec in NUTRIENTS.items()}
        bounds["dry_matter"] = DRY_MATTER_BOUND
        for key, (content_key, unit, label, digits) in bounds.items():
            value = nutrient_content[content_key]
            for side in ("min", "max"):
                target = getattr(constraints, f"{key}_{side}")
                if target is None:
                    continue
                name = f"{key}_{side}"
                below = side == "min" and value < target - 1e-6
                above = side == "max" and value > target + 1e-6
                if not (below or above):
                    constraints_status[name] = "OK"
                    continue
                diff = abs(value - target)
                constraints_status[name] = (
                    f"{'ALTINDA' if below else 'USTUNDE'} ({value:.4f} {'<' if below else '>'} "
                    f"{target:.4f} {unit}, fark: {diff:.4f})"
                )
                violations.append(f"{name}: {constraints_status[name]}")
                adjustments.append(
                    f"⚠️ {label} {'minimumu' if below else 'maksimumu'} ({target:.{digits}f} {unit}) "
                    f"{'sağlanamadı' if below else 'aşıldı'}; gerçekleşen: {value:.{digits}f} {unit} "
                    f"(fark: {diff:.{digits}f} {unit}). Hedef gevşetildi."
                )

        ca_p = nutrient_content.get("ca_p_ratio", 0)
        if ca_p > 0 and (ca_p < 1.0 or ca_p > 3.0):
            warnings.append(f"Ca:P oranı = {ca_p:.2f} (önerilen 1.0-3.0 aralığında)")

    if infeasible:
        # Hayvan sağlığı sınırları gevşetilemediği için yumuşak modda bile mümkündür.
        result_status = "Infeasible (saglik sinirlari icinde cozum yok)"
    elif hard_nutrients and status != "Optimal":
        result_status = "Infeasible (kati besin hedefleri saglanamadi)"
    elif violations:
        result_status = "Feasible (hedefler kısmen gevsetildi)"
    elif status == "Optimal":
        result_status = "Optimal (tum hedefler saglandi)"
    else:
        result_status = status

    return OptimizationResult(
        status=result_status,
        feed_amounts=amounts,
        feed_proportions=proportions,
        nutrient_content=nutrient_content,
        daily_nutrient_intake=daily_intake,
        total_cost_per_day=actual_feed_cost,
        total_cost_per_kg=actual_feed_cost / total_kg if total_kg > 0 else 0.0,
        total_daily_intake_kg=total_kg,
        constraints_status=constraints_status,
        violations=violations,
        warnings=warnings,
        adjustments=adjustments,
        solver_details={
            "status": status,
            "objective_value": pulp.value(prob.objective) if prob.objective else 0.0,
            "feed_cost": actual_feed_cost,
            "num_variables": len(prob.variables()),
            "num_constraints": len(prob.constraints()),
            "slack_values": slack_values,
            "force_use_all_feeds": force_use_all_feeds,
            "min_inclusion_pct_dm": min_inclusion_pct_dm if force_use_all_feeds else None,
            "hard_nutrients": hard_nutrients,
        },
    )


def optimize_ration(
    feeds: List[FeedIngredient],
    constraints: NutrientConstraints,
    *,
    use_all_selected: bool = True,
    min_inclusion_pct_dm: float = 3.0,
    expected_daily_intake_kg: float = 22.0,
    max_daily_intake_kg: Optional[float] = None,
    body_weight_kg: Optional[float] = None,
    soft_constraint_penalty: float = 100000.0,
    rules: RationRules = LACTATING_RULES,
) -> OptimizationResult:
    """İki aşamalı, BESİN ÖNCELİKLİ rasyon optimizasyonu. Tek giriş noktasıdır.

    AŞAMA 1: Besin hedefleri ve rasyon kuralları KATI kurulur, maliyet minimize edilir.
             Çözüm bulunursa besin değerleri ve rasyonun yenebilirliği GARANTİLİDİR.
    AŞAMA 2: Aşama 1 infeasible ise her şey yumuşak kısıtla tekrar çözülür ve
             `diagnose_infeasibility()` çıktısı Türkçe açıklama olarak eklenir.

    `solver_details["nutrition_mode"]`: "hard", "relaxed" veya "infeasible".
    """
    if max_daily_intake_kg is None:
        max_daily_intake_kg = expected_daily_intake_kg / 0.35  # en az %35 KM varsayımı

    common = dict(
        feeds=feeds,
        constraints=constraints,
        soft_constraint_penalty=soft_constraint_penalty,
        max_daily_intake_kg=max_daily_intake_kg,
        expected_daily_intake_kg=expected_daily_intake_kg,
        force_use_all_feeds=use_all_selected,
        min_inclusion_pct_dm=min_inclusion_pct_dm,
        body_weight_kg=body_weight_kg,
        rules=rules,
    )

    # ---------- AŞAMA 1: Katı besin kısıtları + katı rasyon kuralları ----------
    result = solve_ration_problem(**common, hard_nutrients=True, hard_ration_rules=True)
    if result.status == "Optimal (tum hedefler saglandi)":
        result.solver_details["nutrition_mode"] = "hard"
        result.solver_details["ration_rules_mode"] = "hard"
        result.adjustments.insert(
            0,
            "✅ Tüm besin hedefleri ve rasyon kuralları tam olarak sağlandı. Bu, "
            "hedefleri karşılayan rasyonlar arasındaki EN UCUZ olanıdır."
        )
        _annotate_dropped_feeds(result, feeds, use_all_selected)
        return result

    # ---------- AŞAMA 2: Yumuşak kısıtlar + teşhis ----------
    # Sağlık sınırları burada da geçerlidir, bu yüzden bu faz da infeasible dönebilir.
    relaxed = solve_ration_problem(**common, hard_nutrients=False, hard_ration_rules=False)
    relaxed.solver_details["nutrition_mode"] = "relaxed"
    relaxed.solver_details["ration_rules_mode"] = "relaxed"
    relaxed.solver_details["hard_phase_status"] = result.status

    diagnosis = diagnose_infeasibility(feeds, constraints, body_weight_kg=body_weight_kg, rules=rules)

    if "Infeasible" in relaxed.status:
        relaxed.solver_details["nutrition_mode"] = "infeasible"
        relaxed.adjustments = [
            "⛔ Seçilen yemlerle, hayvan sağlığı sınırlarını aşmadan rasyon kurulamıyor. "
            "Sağlık sınırları hiçbir koşulda gevşetilmez — yem eklemek gerekiyor:"
        ] + diagnosis + relaxed.adjustments
        return relaxed

    notes = [
        "⚠️ Besin hedeflerinin tamamı seçilen yemlerle SAĞLANAMADI; hedefler "
        "gevşetilerek en yakın rasyon üretildi. Aşağıdaki teşhisi dikkate alın:"
    ] + (diagnosis or [
        "Tek tek her besin hedefi karşılanabilir görünüyor; sorun hedeflerin "
        "BİRLİKTE sağlanamamasından kaynaklanıyor. Daha çeşitli yem ekleyin "
        "veya hedef aralıklarını genişletin."
    ])
    relaxed.adjustments = notes + relaxed.adjustments
    _annotate_dropped_feeds(relaxed, feeds, use_all_selected)
    return relaxed


def _annotate_dropped_feeds(
    result: OptimizationResult,
    feeds: List[FeedIngredient],
    use_all_selected: bool
) -> None:
    """Zorunlu dahil edilmesi istenen ama rasyona giremeyen yemleri raporlar."""
    if not use_all_selected:
        return
    dropped = [f.name for f in feeds if result.feed_amounts.get(f.name, 0.0) <= 0.001]
    if dropped:
        result.adjustments.append(
            f"ℹ️ Şu yem(ler) besin hedefleriyle çeliştiği için rasyona giremedi: "
            f"{', '.join(dropped)}. Zorunlu kullanılmasını istiyorsanız hedefleri "
            f"gevşetin veya minimum dahil oranını düşürün."
        )
        result.solver_details["dropped_feeds"] = dropped


def optimization_result_to_schema(
    result: OptimizationResult, feeds: List[FeedIngredient]
) -> OptimizationResponse:
    """OptimizationResult'ı API'nin döndürdüğü OptimizationResponse'a çevirir."""
    prices = {f.name: f.price_per_kg for f in feeds}
    content = result.nutrient_content
    intake = result.daily_nutrient_intake

    def r(source: Dict[str, float], key: str, digits: int) -> float:
        return round(source.get(key, 0), digits)

    def r_opt(source: Dict[str, float], key: str, digits: int) -> Optional[float]:
        value = source.get(key)
        return None if value is None else round(value, digits)

    unit_by_key = {key: spec[3] for key, spec in NUTRIENTS.items()}
    content_by_key = {key: spec[2] for key, spec in NUTRIENTS.items()}
    unit_by_key["dry_matter"], content_by_key["dry_matter"] = DRY_MATTER_BOUND[1], DRY_MATTER_BOUND[0]

    constraints_status = []
    for name, status in result.constraints_status.items():
        key = name.rsplit("_", 1)[0]
        # Teşhis mesajları kısıt adını ('crude_protein_min') içerir.
        explanation = next((adj for adj in result.adjustments if name in adj), None)
        constraints_status.append(ConstraintStatus(
            constraint=name,
            actual=round(content.get(content_by_key[key], 0), 2),
            status=status,
            unit=unit_by_key[key],
            turkish_explanation=explanation,
        ))

    return OptimizationResponse(
        status=result.status,
        success=result.status.startswith(("Optimal", "Feasible")),
        total_cost_per_day=round(result.total_cost_per_day, 2),
        total_cost_per_kg=round(result.total_cost_per_kg, 2),
        total_daily_intake_kg=round(result.total_daily_intake_kg, 2),
        feed_recommendations=[
            FeedRecommendation(
                name=name,
                amount_kg_per_day=round(amount, 3),
                proportion=round(result.feed_proportions.get(name, 0), 4),
                price_per_kg=round(prices.get(name, 0.0), 2),
                cost_per_day=round(amount * prices.get(name, 0.0), 2),
            )
            for name, amount in result.feed_amounts.items() if amount > 0.001
        ],
        nutrient_content=NutrientContent(
            crude_protein_percent_dm=r(content, "crude_protein_percent_dm", 2),
            metabolic_energy_mcal_per_kg_dm=r(content, "metabolic_energy_mcal_per_kg_dm", 3),
            calcium_g_per_kg_dm=r(content, "calcium_g_per_kg_dm", 2),
            phosphorus_g_per_kg_dm=r(content, "phosphorus_g_per_kg_dm", 2),
            dry_matter_percent=r(content, "dry_matter_percent", 1),
            net_energy_lactation_mcal_per_kg_dm=r(content, "net_energy_lactation_mcal_per_kg_dm", 3),
            ndf_percent_dm=r(content, "ndf_percent_dm", 1),
            forage_ndf_percent_dm=r(content, "forage_ndf_percent_dm", 1),
            forage_ratio_percent=r(content, "forage_ratio_percent", 1),
            ca_p_ratio=round(content["ca_p_ratio"], 2) if content.get("ca_p_ratio") else None,
            crude_protein_percent_af=r(content, "crude_protein_percent_af", 2),
            metabolic_energy_mcal_per_kg_af=r(content, "metabolic_energy_mcal_per_kg_af", 3),
            calcium_g_per_kg_af=r(content, "calcium_g_per_kg_af", 2),
            phosphorus_g_per_kg_af=r(content, "phosphorus_g_per_kg_af", 2),
        ),
        daily_intake=DailyNutrientIntake(
            crude_protein_kg=r(intake, "crude_protein_kg", 3),
            metabolic_energy_mcal=r(intake, "metabolic_energy_mcal", 2),
            calcium_g=r(intake, "calcium_g", 1),
            phosphorus_g=r(intake, "phosphorus_g", 1),
            dry_matter_kg=r(intake, "dry_matter_kg", 2),
            net_energy_lactation_mcal=r_opt(intake, "net_energy_lactation_mcal", 2),
            ndf_kg=r_opt(intake, "ndf_kg", 3),
        ),
        constraints_status=constraints_status,
        turkish_explanations=result.adjustments,
        warnings=result.warnings,
        solver_details=result.solver_details,
    )


def format_result_for_agent(result: OptimizationResult) -> str:
    """Optimizasyon sonucunu LLM'e verilecek düz metne çevirir."""
    content = result.nutrient_content
    lines = [
        "=== RASYON OPTİMİZASYON SONUCU ===",
        f"Durum: {result.status}",
        f"Toplam günlük maliyet: {result.total_cost_per_day:.2f} TL/gün",
        f"Rasyon maliyeti: {result.total_cost_per_kg:.2f} TL/kg",
        f"Günlük tüketim: {result.total_daily_intake_kg:.2f} kg/gün (doğal hali)",
        "",
        "YEM ÖNERİLERİ (kg/gün):",
    ]
    for name, amount in sorted(result.feed_amounts.items(), key=lambda item: -item[1]):
        if amount > 0.001:
            lines.append(f"  - {name}: {amount:.3f} kg/gün ({result.feed_proportions.get(name, 0) * 100:.1f}%)")

    lines += [
        "",
        "RASYON BESİN DEĞERİ (KM bazında):",
        f"  - Ham protein: {content.get('crude_protein_percent_dm', 0):.2f}% KM",
        f"  - Net enerji (NEL): {content.get('net_energy_lactation_mcal_per_kg_dm', 0):.3f} Mcal/kg KM",
        f"  - Met. enerji: {content.get('metabolic_energy_mcal_per_kg_dm', 0):.3f} Mcal/kg KM",
        f"  - Kalsiyum: {content.get('calcium_g_per_kg_dm', 0):.2f} g/kg KM",
        f"  - Fosfor: {content.get('phosphorus_g_per_kg_dm', 0):.2f} g/kg KM",
        f"  - Kuru madde: {content.get('dry_matter_percent', 0):.1f}% (doğal hali)",
    ]
    if content.get("ca_p_ratio"):
        lines.append(f"  - Ca:P oranı: {content['ca_p_ratio']:.2f}")

    sections = (
        ("KISITLAMA DURUMU:", [f"{'✓' if s == 'OK' else '⚠'} {c}: {s}" for c, s in result.constraints_status.items()]),
        ("TÜRKÇE AÇIKLAMALAR:", result.adjustments),
        ("UYARILAR:", result.warnings),
    )
    for title, items in sections:
        if items:
            lines += ["", title] + [f"  - {item}" for item in items]
    return "\n".join(lines)


def diagnose_infeasibility(
    feeds: List[FeedIngredient],
    constraints: NutrientConstraints,
    body_weight_kg: Optional[float] = None,
    rules: RationRules = LACTATING_RULES,
) -> List[str]:
    """Hedeflerin neden birlikte sağlanamadığını Türkçe önerilerle açıklar.

    Her besin sınırını tek tek seçilen yemlerle karşılaştırır, ardından rasyon
    kurallarını (sağlık sınırları, kaba yem, NDF) kontrol eder.
    """
    notes: List[str] = []
    checks = {key: spec[0] for key, spec in NUTRIENTS.items()}
    checks["dry_matter"] = "dry_matter"

    for key, attr in checks.items():
        for side in ("min", "max"):
            target = getattr(constraints, f"{key}_{side}")
            if target is None:
                continue
            name = f"{key}_{side}"
            ok = [f.name for f in feeds
                  if (getattr(f, attr) >= target if side == "min" else getattr(f, attr) <= target)]
            if not ok:
                notes.append(
                    f"❌ '{name}' için hedef {target} - HIÇBİR YEM BU KRİTERİ KARŞILAMIYOR. "
                    f"Hedefi gevşetin veya farklı yemler ekleyin."
                )
            elif len(ok) < 2:
                notes.append(
                    f"⚠️ '{name}' için sadece {len(ok)} yem uygun: {', '.join(ok)}. "
                    f"Rasyon çeşitliliği sınırlı kalabilir."
                )

    return notes + _diagnose_ration_rules(feeds, constraints, body_weight_kg, rules)


def _diagnose_ration_rules(
    feeds: List[FeedIngredient],
    constraints: Optional[NutrientConstraints] = None,
    body_weight_kg: Optional[float] = None,
    rules: RationRules = LACTATING_RULES,
) -> List[str]:
    """Yem seçiminin rasyon kurallarını karşılayıp karşılayamayacağını teşhis eder.

    Besin hedefleri tek tek tutuyor olsa bile seçim yem sınıfı pencerelerini
    doldurmaya yetmiyor olabilir. Bu teşhisler kullanıcıya ne ekleyeceğini söyler.
    """
    notes: List[str] = []
    windows = _max_inclusion_windows(feeds, rules)
    types = {f.feed_type for f in feeds}

    # Sağlık sınırları asla gevşetilmediği için, tüm yemler sınırlıysa ve toplam KM
    # katkıları hedefin altında kalıyorsa model hiçbir fazda çözüm bulamaz.
    target_dm = constraints.daily_intake_kg if constraints else None
    if target_dm and feeds and all(f.health_cap_kg(body_weight_kg) for f in feeds):
        max_dm = sum(f.health_cap_kg(body_weight_kg) * f.dm_fraction() for f in feeds)
        if max_dm < target_dm - 1e-6:
            detail = ", ".join(f"{f.name} ≤ {f.health_cap_kg(body_weight_kg):.1f} kg" for f in feeds)
            notes.append(
                f"⛔ Seçilen yemlerle sağlık sınırları içinde en fazla {max_dm:.1f} kg "
                f"kuru madde verilebiliyor, hedef {target_dm:.1f} kg ({detail}). "
                "Sınırsız yem (kuru ot, tahıl, küspe) ekleyin."
            )

    forage_feeds = [f for f in feeds if f.is_forage()]
    if not forage_feeds:
        notes.append(
            "❌ Hiç kaba yem seçilmemiş. Rasyonun en az "
            f"%{rules.forage_ratio_min * 100:.0f}'i kaba yemden gelmeli — silaj veya kuru ot ekleyin."
        )
        return notes

    if sum(windows.get(f.feed_type, 0.20) for f in forage_feeds) < rules.forage_ratio_min:
        detail = ", ".join(f"{f.name} max %{windows.get(f.feed_type, 0.20) * 100:.0f}" for f in forage_feeds)
        notes.append(
            f"❌ Seçilen kaba yemlerle %{rules.forage_ratio_min * 100:.0f} kaba yem oranına "
            f"ulaşılamıyor ({detail}). Silaj veya kuru ot ekleyin."
        )

    if any(f.ndf > 0 for f in feeds):
        if sum(windows.get(f.feed_type, 0.20) * f.ndf for f in feeds) < rules.ndf_min:
            notes.append(
                f"❌ Seçilen yemlerle rasyon NDF'si %{rules.ndf_min:.0f}'e ulaşamıyor. "
                "Daha lifli kaba yem (saman, çayır otu, silaj) ekleyin."
            )
        if min(f.ndf for f in feeds) > rules.ndf_max:
            notes.append(
                f"❌ En düşük NDF'li yem bile %{rules.ndf_max:.0f} sınırının üstünde. "
                "Tahıl danesi veya küspe gibi düşük lifli yem ekleyin."
            )

    if all(f.feed_type == "straw" for f in forage_feeds):
        notes.append(
            f"⚠️ Tek kaba yem kaynağı saman. Saman rasyonun en fazla "
            f"%{windows.get('straw', 0.08) * 100:.0f}'ini oluşturabilir (düşük enerji, "
            "yüksek lif). Silaj veya kuru ot ekleyin."
        )

    if not types & {"protein", "hay", "silage"}:
        notes.append("⚠️ Protein kaynağı yok (küspe, DDGS veya yonca). Protein hedefi tutmayabilir.")

    return notes
