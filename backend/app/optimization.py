#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RationAI - Lineer Programlama ile Rasyon Optimizasyonu
PuLP tabanlı en düşük maliyetli yem rasyonu hesaplayıcı.

Birim Tutarlılığı (TÜM KISITLAR KURU MADDE - KM BAZINDA):
- dry_matter: % (örn: 86.7)
- crude_protein: % KM bazında (örn: 10.5)
- metabolic_energy: Mcal/kg KM (örn: 3.05 Mcal/kg KM)
- calcium: g/kg KM (örn: 7 g/kg KM = 0.7%)
- phosphorus: g/kg KM (örn: 3.8 g/kg KM = 0.38%)
- price: TL/kg (doğal hali/as-fed bazında fiyat)
"""

import pulp
import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

# schemas.py bu modülü import ETMEZ - tek yönlü bağımlılık, döngü yok.
from .schemas import (
    FeedRecommendation,
    NutrientContent,
    DailyNutrientIntake,
    ConstraintStatus,
    OptimizationResponse,
)

logger = logging.getLogger(__name__)


class SolverStatus(Enum):
    OPTIMAL = "Optimal"
    FEASIBLE = "Feasible"
    INFEASIBLE = "Infeasible"
    UNBOUNDED = "Unbounded"
    ERROR = "Error"


@dataclass
class FeedIngredient:
    """Yem bileşeni veri sınıfı - tüm besin değerleri KM bazında (fiyat hariç)."""
    name: str
    price_per_kg: float          # TL/kg (doğal hali/as-fed bazında fiyat)
    dry_matter: float            # % (örn: 86.7)
    crude_protein: float         # % KM (Ham Protein - KM bazında)
    metabolic_energy: float      # Mcal/kg KM (Metabolez Edilebilir Enerji - KM bazında)
    calcium: float               # g/kg KM (Kalsiyum - KM bazında)
    phosphorus: float            # g/kg KM (Fosfor - KM bazında)
    # NDF rasyonun yenebilirliğini belirler: lif fazlaysa inek rasyonu fiziksel olarak
    # tüketemez. Sıfır bırakılırsa NDF kısıtları o yem için etkisiz kalır.
    ndf: float = 0.0                     # % KM (Nötr Deterjan Lif)
    net_energy_lactation: float = 0.0    # Mcal/kg KM (NEL)
    feed_type: str = "unknown"   # silage/hay/straw/grain/protein/byproduct/fat/mineral/salt
    # Hayvan sağlığı sınırı: 600 kg canlı ağırlık için günlük azami doğal hali miktar.
    # None = as-fed sınırı yok (KM yüzdesi tavanı yeterli).
    max_kg_per_day: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "price_per_kg": self.price_per_kg,
            "dry_matter": self.dry_matter,
            "crude_protein": self.crude_protein,
            "metabolic_energy": self.metabolic_energy,
            "net_energy_lactation": self.net_energy_lactation,
            "ndf": self.ndf,
            "calcium": self.calcium,
            "phosphorus": self.phosphorus,
            "feed_type": self.feed_type,
            "max_kg_per_day": self.max_kg_per_day,
        }

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

    def price_per_kg_dm(self) -> float:
        """KM bazında kg başına fiyat (TL/kg KM)."""
        dm_frac = self.dm_fraction()
        return self.price_per_kg / dm_frac if dm_frac > 0 else 0.0


# Backward compatibility alias for old agent code
FeedInput = FeedIngredient


@dataclass
class NutrientConstraints:
    """Besin maddesi kısıtlamaları - HEPSİ KM BAZINDA (Dry Matter Basis)."""
    # Protein: % KM
    crude_protein_min: Optional[float] = None
    crude_protein_max: Optional[float] = None

    # Enerji: Mcal/kg KM
    metabolic_energy_min: Optional[float] = None
    metabolic_energy_max: Optional[float] = None

    # Mineraller: g/kg KM
    calcium_min: Optional[float] = None
    calcium_max: Optional[float] = None
    phosphorus_min: Optional[float] = None
    phosphorus_max: Optional[float] = None

    # Kuru madde: %
    dry_matter_min: Optional[float] = None
    dry_matter_max: Optional[float] = None

    # Günlük tüketim miktarı (kg doğal hali) - bu eklersen mutlak kısıt olur
    daily_intake_kg: Optional[float] = None


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
    # Requirement coverage percentages (actual / target * 100)
    crude_protein_coverage_percent: Optional[float] = None
    metabolic_energy_coverage_percent: Optional[float] = None
    calcium_coverage_percent: Optional[float] = None
    phosphorus_coverage_percent: Optional[float] = None
    dry_matter_coverage_percent: Optional[float] = None
    # Forced feeds (when force_use_all_feeds=True)
    forced_feeds: List[str] = field(default_factory=list)


# ============================================================
# RASYON KURALLARI (süt ineği, laktasyon dönemi)
#
# Yem sınıfı, rasyon kurallarının tamamının dayanağıdır. Kaynak `Feed.feed_type`
# sütunudur; `_classify_feed_type()` yalnızca sütun boşsa devreye giren yedektir.
# ============================================================
# `Feed.max_kg_per_day` değerlerinin referans aldığı canlı ağırlık.
HEALTH_CAP_REFERENCE_BW_KG = 600.0

FORAGE_TYPES = frozenset({"silage", "hay", "straw"})
CONCENTRATE_TYPES = frozenset({"grain", "protein", "byproduct", "fat"})
MINERAL_TYPES = frozenset({"mineral", "salt"})

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

RATION_NDF_MIN, RATION_NDF_MAX = 28.0, 35.0   # % KM
FORAGE_NDF_MIN = 19.0                          # % KM (peNDF vekili)
NDF_INTAKE_PCT_BW = 1.2                        # kg NDF/gün <= %1.2 x canlı ağırlık
FORAGE_RATIO_MIN, FORAGE_RATIO_MAX = 0.45, 0.65
SILAGE_BASE_MIN = 0.25                         # silaj seçiliyse kaba yemin tabanı
HAY_AGG_MAX_WITH_SILAGE = 0.40                 # silaj varken kuru ot toplamı


def _max_inclusion_windows(feeds: List["FeedIngredient"]) -> Dict[str, float]:
    """Seçime uyarlanmış azami dahil tavanlarını (yem tipi -> oran) döndürür."""
    windows = dict(MAX_INCLUSION_BY_TYPE)
    if not any(f.feed_type == "silage" for f in feeds):
        windows.update(MAX_INCLUSION_NO_SILAGE)
    return windows


def _classify_feed_type(name: str) -> str:
    """İsimden yem sınıfı tahmini — `Feed.feed_type` boş olduğunda kullanılan YEDEK yol.

    Sınıflar: silage, hay, straw, grain, protein, byproduct, fat, mineral, salt, unknown.
    Asıl kaynak veritabanındaki `feed_type` sütunudur; buraya düşen bir yem varsa
    küratörlü listeye (`database._CURATED_FEEDS`) eklenmesi gerekir.
    """
    name_lower = name.lower()

    straw_keywords = ["saman", "samani", "straw", "kavuz", "sap"]
    silage_keywords = ["silaj", "silajı", "silaji", "silage", "hasıl", "hasil"]
    hay_keywords = [
        "kuru ot", "otu", "ot ", "çayır", "çayırı", "yonca", "korunga", "fiğ",
        "hay", "alfalfa", "grass", "mera", "sudan", "sorgum",
    ]

    fat_keywords = ["korunmuş yağ", "bypass", "by-pass", "yağ asidi", "kalsiyum sabunu"]
    salt_keywords = ["tuz", "tuzu", "salt", "nacl"]
    mineral_keywords = [
        "kalsiyum", "kalsit", "dcp", "mcp", "mermer", "fosfat", "magnezyum",
        "sodyum", "bikarbonat", "liz", "mineral", "premiks", "premix", "pre-mix",
        "vitamin", "katkı", "tampon",
    ]

    grain_keywords = [
        "mısır", "corn", "arpa", "barley", "buğday", "wheat", "tritikale", "triticale",
        "yulaf", "oats", "çavdar", "sorgum", "pirinç", "tahıl", "grain", "dane",
    ]
    protein_keywords = [
        "küspe", "küspesi", "soya", "soybean", "fullfat", "fulfat", "ddgs", "gluten",
        "ayçiçeği", "sunflower", "kolza", "canola", "susam", "sesame", "çiğit",
        "pamuk tohumu", "bakla", "mercimek", "nohut", "fasulye", "bira posası",
        "maya", "ptk", "sfk", "spk",
    ]
    byproduct_keywords = [
        "kepek", "kepeği", "kepe", "bran", "razmol", "posa", "posası", "melas",
        "vinas", "malt", "nişasta", "middlings",
    ]

    # Helper: check if keyword appears as a "word" in text (bounded by non-letter chars or string boundaries)
    # Turkish-safe: treat all Unicode letters as word chars, so boundaries are non-letters
    def kw_match(keyword: str, text: str) -> bool:
        """Check if keyword appears as a whole word (bounded by non-letters or string boundaries)."""
        kw_len = len(keyword)
        start = 0
        while True:
            idx = text.find(keyword, start)
            if idx == -1:
                return False
            # Check left boundary: start of string OR previous char is not a letter
            left_ok = (idx == 0) or (not text[idx - 1].isalpha())
            # Check right boundary: end of string OR next char after keyword is not a letter
            right_idx = idx + kw_len
            right_ok = (right_idx >= len(text)) or (not text[right_idx].isalpha())
            if left_ok and right_ok:
                return True
            start = idx + 1

    # Sıra önemli: kaba yem nitelemeleri tahıl adlarından ÖNCE gelmeli, aksi halde
    # "Buğday Samanı" tahıl, "Mısır Silajı" dane mısır sayılır.
    keyword_groups = [
        ("salt", salt_keywords),
        ("fat", fat_keywords),
        ("mineral", mineral_keywords),
        ("straw", straw_keywords),
        ("silage", silage_keywords),
        ("hay", hay_keywords),
        ("protein", protein_keywords),
        ("byproduct", byproduct_keywords),
        ("grain", grain_keywords),
    ]

    for feed_type, keywords in keyword_groups:
        for kw in keywords:
            if kw_match(kw, name_lower):
                return feed_type

    return "unknown"


def feeds_from_db(db_feeds) -> List[FeedIngredient]:
    """Veritabanı Feed modellerini FeedIngredient listesine çevirir.

    Not: Veritabanındaki değerler şu formatta saklanır (HEPSİ KM BAZINDA):
    - dry_matter: %
    - crude_protein: % KM
    - metabolic_energy: Mcal/kg KM
    - calcium: g/kg KM
    - phosphorus: g/kg KM
    """
    ingredients = []
    for feed in db_feeds:
        # Fiyat 0 ise çok küçük bir değer ver (solver 0 fiyatlı değişkeni sonsuza kadar artırabilir)
        price = feed.price_per_kg if feed.price_per_kg > 0 else 0.001

        # Sınıf veritabanından gelir; boşsa isimden tahmin edilir.
        feed_type = getattr(feed, "feed_type", None) or _classify_feed_type(feed.name)

        ingredients.append(FeedIngredient(
            name=feed.name,
            price_per_kg=price,
            dry_matter=feed.dry_matter,
            crude_protein=feed.crude_protein,  # Zaten % KM
            metabolic_energy=feed.metabolic_energy,  # Zaten Mcal/kg KM
            net_energy_lactation=getattr(feed, "net_energy_lactation", 0.0) or 0.0,
            ndf=getattr(feed, "ndf", 0.0) or 0.0,
            calcium=feed.calcium,  # Zaten g/kg KM
            phosphorus=feed.phosphorus,  # Zaten g/kg KM
            feed_type=feed_type,
            max_kg_per_day=getattr(feed, "max_kg_per_day", None),
        ))
    return ingredients


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
    hard_ration_rules: bool = True
) -> OptimizationResult:
    """
    En düşük maliyetli rasyon optimizasyonu çözer.
    TÜM KISITLAR KM BAZINDA DEĞERLENDİRİLİR.

    Args:
        feeds: Kullanılabilir yem bileşenleri listesi
        constraints: Besin maddesi kısıtlamaları (KM bazında)
        soft_constraint_penalty: Ceza maliyeti (TL) per birim ihlal.
                                 Ihmal birimleri: HP->kg/gün, ME->Mcal/gün, Ca->g/gün, P->g/gün, KM->kg/gün.
                                 Bu ceza, beklenen günlük tüketim (expected_daily_intake_kg) bazında
                                 normalize edilerek uygulanır.
        max_daily_intake_kg: Maksimum günlük tüketim (kg doğal hali) - güvenlik sınırı (HARD constraint)
        expected_daily_intake_kg: Beklenen günlük tüketim (ceza normalizasyonu için)
        force_use_all_feeds: True ise seçilen TÜM yemler rasyona dahil edilmeye zorlanır.
                            Her yem için minimum dahil oranı yem tipine göre otomatik
                            kısılır (mineral tavanı ve yem sayısı gözetilir).
        min_inclusion_pct_dm: force_use_all_feeds=True iken hedeflenen minimum KM dahil oranı (%).
                             Yem tipi tavanını aşarsa otomatik düşürülür.
        hard_nutrients: True ise HP/ME/Ca/P kısıtları KATI (slack'siz) kurulur. Besin
                        hedeflerini garanti eder ama model 'Infeasible' dönebilir.
                        False (varsayılan) ise tüm besin kısıtları yumuşaktır ve model
                        ASLA 'Infeasible' döndürmez.
                        İki aşamalı akış için `optimize_ration()` kullan.
        body_weight_kg: Canlı ağırlık (kg). Verilirse NDF alım kapasitesi kısıtı
                        (kg NDF/gün <= %1.2 x CA) uygulanır.
        hard_ration_rules: True ise yem sınıfı tavanları, NDF penceresi, kaba yem oranı
                        ve silaj-tabanı kuralları KATI kurulur. False iken hepsi
                        cezalı-yumuşak olur (ikinci faz).

    Returns:
        OptimizationResult nesnesi. hard_nutrients=False iken asla 'Infeasible' olmaz.
    """
    if not feeds:
        return OptimizationResult(
            status="Yem bileşeni yok",
            feed_amounts={},
            feed_proportions={},
            nutrient_content={},
            daily_nutrient_intake={},
            total_cost_per_day=0.0,
            total_cost_per_kg=0.0,
            total_daily_intake_kg=0.0,
            constraints_status={},
            adjustments=["Hiç yem bileşeni sağlanmadı."]
        )

    # Initialize adjustments list at top level to avoid UnboundLocalError
    adjustments = []  # Türkçe açıklama mesajları

    # Problem tanımı: Minimize maliyet
    prob = pulp.LpProblem("Ration_Optimization", pulp.LpMinimize)

    # Karar değişkenleri: her yem için günlük miktar (kg doğal hali)
    #
    # ⚠️ HAYVAN SAĞLIĞI SINIRI değişkenin ÜST SINIRI olarak kurulur, kısıt satırı olarak
    # değil. Böylece slack takılamaz ve sınır KATI/YUMUŞAK ayrımından bağımsız olarak
    # her iki fazda da geçerli olur: 25 kg'lık silaj tavanı hiçbir koşulda aşılamaz.
    # Bedeli: model artık ikinci fazda da infeasible dönebilir (bkz. optimize_ration).
    feed_vars = {}
    health_caps: Dict[str, float] = {}
    for feed in feeds:
        cap = feed.health_cap_kg(body_weight_kg)
        feed_vars[feed.name] = pulp.LpVariable(f"feed_{feed.name}", lowBound=0, upBound=cap)
        if cap is not None:
            health_caps[feed.name] = cap

    if health_caps:
        detail = ", ".join(f"{name} ≤ {cap:.1f} kg" for name, cap in sorted(health_caps.items()))
        adjustments.append(f"🩺 Hayvan sağlığı sınırları (günlük doğal hali): {detail}")

    # Slack variables for soft constraints (YUMUŞAK KISITLAR - ASLA INFEASIBLE YAPMAZ)
    # Ihmal birimleri: toplam günlük miktar (kg/gün, Mcal/gün, g/gün)
    slack_vars = {}
    constraint_names = [
        "cp_min", "cp_max",
        "me_min", "me_max",
        "ca_min", "ca_max",
        "p_min", "p_max",
        "dm_min", "dm_max",
    ]
    for cname in constraint_names:
        slack_vars[f"{cname}_slack"] = pulp.LpVariable(f"slack_{cname}", lowBound=0)

    # Yardımcı ifadeler
    # Toplam doğal hali tüketim (kg/gün)
    total_intake = pulp.lpSum(feed_vars[feed.name] for feed in feeds)

    # Toplam KM tüketim (kg KM/gün)
    total_dm = pulp.lpSum(feed.dm_fraction() * feed_vars[feed.name] for feed in feeds)

    # Toplam Ham Protein (kg HP/gün) = Σ(yem_kg * DM% * HP%/100)
    total_cp = pulp.lpSum(
        feed.dm_fraction() * (feed.crude_protein / 100.0) * feed_vars[feed.name]
        for feed in feeds
    )

    # Toplam ME (Mcal/gün) = Σ(yem_kg * KM% * ME_Mcal_per_kg_KM)
    total_me = pulp.lpSum(
        feed.dm_fraction() * feed.metabolic_energy * feed_vars[feed.name]
        for feed in feeds
    )

    # Toplam Ca (g/gün) = Σ(yem_kg * KM% * Ca_g_per_kg_KM)
    total_ca = pulp.lpSum(
        feed.dm_fraction() * feed.calcium * feed_vars[feed.name]
        for feed in feeds
    )

    # Toplam P (g/gün) = Σ(yem_kg * KM% * P_g_per_kg_KM)
    total_p = pulp.lpSum(
        feed.dm_fraction() * feed.phosphorus * feed_vars[feed.name]
        for feed in feeds
    )

    # Objektif fonksiyon: Toplam günlük yem maliyeti + soft constraint cezaları
    # Ceza normalize edilir: soft_constraint_penalty * (slack / expected_daily_intake_kg)
    # Bu sayede ceza birimi: TL per (birim ihlal per kg tüketim)
    feed_cost = pulp.lpSum(feed.price_per_kg * feed_vars[feed.name] for feed in feeds)

    # Soft constraint penalty - always enabled to NEVER return Infeasible
    penalty_expr = soft_constraint_penalty * pulp.lpSum(
        slack_vars[name] / expected_daily_intake_kg for name in slack_vars
    )

    # ============================================================
    # PROTEIN & RUMEN BALANCE GUARDRAIL
    # If CP < 12% DM and protein-rich feeds (Soya Küspesi, Ayçiçeği Küspesi, Yonca) are
    # present in the selected feed set, heavily penalize the solution to force
    # inclusion of protein feeds for realistic rumen function.
    # ============================================================
    PROTEIN_GUARDRAIL_THRESHOLD = 12.0  # % DM - minimum CP for rumen health
    PROTEIN_GUARDRAIL_PENALTY = soft_constraint_penalty * 10.0  # 10x heavier penalty

    # NOT: burada `adjustments = []` YAPMA. Liste fonksiyonun başında kuruluyor;
    # yeniden atamak o noktaya kadar eklenmiş açıklamaları (sağlık sınırları gibi) siler.

    # Check if protein-rich feeds are available in the selection
    protein_rich_feeds_available = any(
        feed.name in ["Soya Küspesi", "Soya-Fullfat", "Ayçiçeği Küspesi", "Yonca Kuru Otu", "Yonca", "Yonca Silajı"]
        for feed in feeds
    )

    if protein_rich_feeds_available:
        # Add slack variable for protein guardrail (allows violation but at high cost)
        guardrail_slack = pulp.LpVariable("protein_guardrail_slack", lowBound=0)

        # Guardrail: total_cp / total_dm >= 0.12  (i.e., CP >= 12% DM)
        # Linearized: total_cp >= 0.12 * total_dm - guardrail_slack
        prob += total_cp >= 0.12 * total_dm - guardrail_slack, "Protein_Guardrail_Min_12pct"

        # Add penalty to objective
        penalty_expr += PROTEIN_GUARDRAIL_PENALTY * (guardrail_slack / expected_daily_intake_kg)

        adjustments.append(
            f"🛡️ Protein korumalı koridor aktif: Soya/Ayçiçeği/Yonca yemleri mevcut -> "
            f"CP <%12 KM cezalandırılır (ceza katsayısı: {PROTEIN_GUARDRAIL_PENALTY/soft_constraint_penalty:.0f}x)."
        )

    # ⚠️ DİKKAT: Amaç fonksiyonu BURADA atanmaz!
    # PuLP'ta `prob += ifade` o anki ifadenin bir kopyasını alır; sonradan Python
    # tarafında `penalty_expr += ...` yapmak problemi ETKİLEMEZ. Aşağıdaki bloklar
    # (min/max dahil cezaları, zorunlu dahil cezaları) penalty_expr'e ekleme yaptığı
    # için amaç fonksiyonu prob.solve() çağrısından hemen önce, TEK SEFERDE atanır.
    # Bu satırın altına ceza ekleyen yeni kod yazarsan objective'in hâlâ en sonda
    # atandığından emin ol.

    # ==================== KISITLAMALAR (TÜMÜ KM BAZINDA) ====================

    # 1. Toplam günlük tüketim sınırı (güvenlik - doğal hali bazında, HARD constraint)
    if max_daily_intake_kg:
        prob += total_intake <= max_daily_intake_kg, "Max_Intake"
        prob += total_intake >= 0.1, "Min_Intake"

    # 1b. Minimum günlük KM tüketimi (HARD constraint - trivial çözümleri engeller)
    # Süt inekleri için minimum ~12 kg KM/gün (DAILY_DMI_MIN_KG from schemas)
    MIN_DAILY_DM_INTAKE_KG = 12.0
    # Eğer kullanıcı daily_intake_kg verdiyse, onu minimum olarak kullan
    if constraints.daily_intake_kg is not None:
        MIN_DAILY_DM_INTAKE_KG = max(MIN_DAILY_DM_INTAKE_KG, constraints.daily_intake_kg)
    prob += total_dm >= MIN_DAILY_DM_INTAKE_KG, "Min_DM_Intake"

    # ============================================================
    # YEM SINIFI BAZLI DAHİL PENCERELERİ
    # Sınırlar modül düzeyindeki MIN/MAX_INCLUSION_BY_TYPE tablolarından gelir
    # (NRC 2001 / INRA pratiği). Tavanlar `hard_ration_rules=True` iken KATI,
    # ikinci fazda cezalı-yumuşaktır.
    #
    # ⚠️ Tavan HER yeme uygulanır — eskiden kaba yemler tavansızdı ve saman
    # rasyonun %90'ına kadar çıkabiliyordu.
    # ============================================================

    # Sınıf veritabanından gelir (feeds_from_db); yalnızca boşsa isimden tahmin edilir.
    feed_classifications = {
        feed.name: (feed.feed_type or _classify_feed_type(feed.name)) for feed in feeds
    }
    max_inclusion_windows = _max_inclusion_windows(feeds)

    # Track which feeds have inclusion constraints (for adjustment messages)
    inclusion_constrained_feeds = []

    # Add slack variables for minimum inclusion (only for feeds with min > 0)
    min_inclusion_slack_vars = {}
    for feed in feeds:
        ftype = feed_classifications.get(feed.name, "unknown")
        min_frac = MIN_INCLUSION_BY_TYPE.get(ftype, 0.01)
        if min_frac > 0:
            min_inclusion_slack_vars[feed.name] = pulp.LpVariable(f"min_incl_slack_{feed.name}", lowBound=0)
            inclusion_constrained_feeds.append(feed.name)

    # Azami dahil ihlali için slack — yalnızca yumuşak modda anlamlı.
    max_inclusion_slack_vars = {}
    if not hard_ration_rules:
        for feed in feeds:
            max_inclusion_slack_vars[feed.name] = pulp.LpVariable(
                f"max_incl_slack_{feed.name}", lowBound=0
            )

    # NOTE: The inclusion constraints and their adjustment messages are added AFTER
    # the force_use_all_feeds block below, because that block may reset
    # `min_inclusion_slack_vars` to disable the soft variants. Registering them here
    # too would create duplicate PuLP constraint names and double-count the penalties.

    # ============================================================
    # FORCE USE ALL FEEDS: her seçili yem rasyona girsin
    #
    # İstenen minimum oran yem tipine göre kısılır:
    #   - mineral tavanı %1.5 KM olduğu için tuzu %3 koymaya zorlamak modeli
    #     infeasible yapar -> tavanın yarısını geçme
    #   - n yem x min_oran toplamda %100'ü aşamaz -> %80/n ile sınırla
    #
    # Kısıt HARD değil, ÇOK PAHALI YUMUŞAK kurulur: besin hedefleriyle gerçekten
    # çakışırsa model çökmek yerine o yemi düşürür ve durumu açıklamalarda bildirir.
    # ============================================================
    forced_inclusion_slack_vars = {}
    if force_use_all_feeds:
        requested_frac = min_inclusion_pct_dm / 100.0
        headroom_frac = 0.80 / len(feeds)  # n yem toplamda %80 KM'yi aşmasın

        applied_fracs = {}
        for feed in feeds:
            ftype = feed_classifications.get(feed.name, "unknown")
            type_cap = max_inclusion_windows.get(ftype, 0.20) * 0.5
            effective_frac = min(requested_frac, type_cap, headroom_frac)
            if effective_frac <= 0:
                continue

            applied_fracs[feed.name] = effective_frac
            slack = pulp.LpVariable(f"forced_incl_slack_{feed.name}", lowBound=0)
            forced_inclusion_slack_vars[feed.name] = slack
            prob += (
                feed.dm_fraction() * feed_vars[feed.name] >= effective_frac * total_dm - slack,
                f"Force_Min_Inclusion_{feed.name}"
            )

        if applied_fracs:
            detail = ", ".join(
                f"{name} %{frac * 100:.2f}" for name, frac in sorted(applied_fracs.items())
            )
            adjustments.append(
                f"🔒 ZORUNLU DAHİL: Seçilen {len(applied_fracs)} yemin tamamı rasyona "
                f"katılıyor (hedef %{min_inclusion_pct_dm:.1f} KM, yem tipine göre "
                f"uygulanan: {detail})"
            )

        # Aynı yemler için ayrıca yumuşak minimum dahil kısıtı kurulmasın
        # (çift kısıt + çift ceza olurdu)
        min_inclusion_slack_vars = {}
        inclusion_constrained_feeds = []

    # Penalty for violating minimum inclusion (moderate - 3x base penalty)
    # This encourages but doesn't force inclusion
    MIN_INCLUSION_PENALTY = soft_constraint_penalty * 3.0

    # Penalty for violating maximum inclusion (high - 5x base penalty)
    # Prevents acidosis from excess concentrates or mineral toxicity
    MAX_INCLUSION_PENALTY = soft_constraint_penalty * 5.0

    for feed in feeds:
        ftype = feed_classifications.get(feed.name, "unknown")
        min_frac = MIN_INCLUSION_BY_TYPE.get(ftype, 0.01)
        max_frac = max_inclusion_windows.get(ftype, 0.20)

        # Minimum inclusion constraint: feed_DM >= min_frac * total_DM - slack
        if feed.name in min_inclusion_slack_vars:
            prob += feed.dm_fraction() * feed_vars[feed.name] >= min_frac * total_dm - min_inclusion_slack_vars[feed.name], f"Min_Inclusion_{feed.name}"

        # Maximum inclusion constraint - katı modda slack yok.
        max_slack = max_inclusion_slack_vars.get(feed.name, 0)
        prob += (
            feed.dm_fraction() * feed_vars[feed.name] <= max_frac * total_dm + max_slack,
            f"Max_Inclusion_{feed.name}"
        )

    # Zorunlu dahil ihlali cezası - normal minimum dahil cezasının 10 katı.
    # "Seçtiğim yemi kullan" talebi, ekonomik tercihlerin çok önünde gelir.
    FORCED_INCLUSION_PENALTY = MIN_INCLUSION_PENALTY * 10.0
    if forced_inclusion_slack_vars:
        penalty_expr += FORCED_INCLUSION_PENALTY * pulp.lpSum(
            forced_inclusion_slack_vars[name] / expected_daily_intake_kg
            for name in forced_inclusion_slack_vars
        )

    # Add minimum inclusion slacks to penalty expression
    if min_inclusion_slack_vars:
        penalty_expr += MIN_INCLUSION_PENALTY * pulp.lpSum(
            min_inclusion_slack_vars[name] / expected_daily_intake_kg for name in min_inclusion_slack_vars
        )

    # Add maximum inclusion slacks to penalty expression
    if max_inclusion_slack_vars:
        penalty_expr += MAX_INCLUSION_PENALTY * pulp.lpSum(
            max_inclusion_slack_vars[name] / expected_daily_intake_kg for name in max_inclusion_slack_vars
        )

    # Add message about which feeds have minimum inclusion
    if inclusion_constrained_feeds:
        type_summary = {}
        for fname in inclusion_constrained_feeds:
            ftype = feed_classifications.get(fname, "unknown")
            type_summary.setdefault(ftype, []).append(fname)
        msg_parts = []
        for ftype, fnames in type_summary.items():
            min_pct = MIN_INCLUSION_BY_TYPE.get(ftype, 0.01) * 100
            max_pct = max_inclusion_windows.get(ftype, 0.20) * 100
            msg_parts.append(
                f"{ftype} (%{min_pct:.1f}-%{max_pct:.1f} KM): {', '.join(fnames)}"
            )
        mode = "KATI" if hard_ration_rules else "yumuşak"
        adjustments.append(f"🔒 Yem sınıfı dahil pencereleri ({mode}): {'; '.join(msg_parts)}")

    # 2-5. Besin maddesi kısıtları (HP, ME, Ca, P)
    # hard_nutrients=True iken slack terimi eklenmez -> hedefler GARANTİ edilir
    # (model infeasible olabilir). False iken mevcut yumuşak davranış korunur.
    def nutrient_slack(name: str):
        """Katı modda 0, yumuşak modda ilgili slack değişkeni döner."""
        return 0 if hard_nutrients else slack_vars[name]

    if hard_nutrients:
        adjustments.append(
            "🎯 Besin hedefleri KATI kısıt olarak uygulanıyor: maliyet ancak HP/ME/Ca/P "
            "hedeflerinin tamamı sağlandığı sürece minimize edilir."
        )

    # 2. Ham Protein kısıtı (% KM bazında)
    # Lineer form: Toplam HP (kg/gün) >= Toplam KM (kg/gün) * HP_min / 100
    if constraints.crude_protein_min is not None:
        cp_min_frac = constraints.crude_protein_min / 100.0
        prob += total_cp >= cp_min_frac * total_dm - nutrient_slack("cp_min_slack"), "CP_Min"

    if constraints.crude_protein_max is not None:
        cp_max_frac = constraints.crude_protein_max / 100.0
        prob += total_cp <= cp_max_frac * total_dm + nutrient_slack("cp_max_slack"), "CP_Max"

    # 3. Metabolez Edilebilir Enerji kısıtı (Mcal/kg KM)
    if constraints.metabolic_energy_min is not None:
        prob += total_me >= constraints.metabolic_energy_min * total_dm - nutrient_slack("me_min_slack"), "ME_Min"

    if constraints.metabolic_energy_max is not None:
        prob += total_me <= constraints.metabolic_energy_max * total_dm + nutrient_slack("me_max_slack"), "ME_Max"

    # 4. Kalsiyum kısıtı (g/kg KM)
    if constraints.calcium_min is not None:
        prob += total_ca >= constraints.calcium_min * total_dm - nutrient_slack("ca_min_slack"), "Ca_Min"

    if constraints.calcium_max is not None:
        prob += total_ca <= constraints.calcium_max * total_dm + nutrient_slack("ca_max_slack"), "Ca_Max"

    # 5. Fosfor kısıtı (g/kg KM)
    if constraints.phosphorus_min is not None:
        prob += total_p >= constraints.phosphorus_min * total_dm - nutrient_slack("p_min_slack"), "P_Min"

    if constraints.phosphorus_max is not None:
        prob += total_p <= constraints.phosphorus_max * total_dm + nutrient_slack("p_max_slack"), "P_Max"

    # 5b. Ca:P ORANI (1.5 - 2.5)
    # Ca ve P tek tek hedefte olsa bile oran bozuk olabilir; oran kemik gelişimi ve
    # idrar taşı riski açısından mutlak değerler kadar önemlidir. Lineer form:
    #   Ca >= 1.5 * P   ve   Ca <= 2.5 * P
    # HER ZAMAN yumuşaktır: sınırlı yem seçkisinde oranı katı tutmak modeli
    # gereksiz yere çözümsüz bırakır, bunun yerine pahalı hale getirilir.
    CA_P_RATIO_MIN, CA_P_RATIO_MAX = 1.5, 2.5
    CA_P_PENALTY = soft_constraint_penalty * 2.0
    ca_p_low_slack = pulp.LpVariable("ca_p_ratio_low_slack", lowBound=0)
    ca_p_high_slack = pulp.LpVariable("ca_p_ratio_high_slack", lowBound=0)
    prob += total_ca >= CA_P_RATIO_MIN * total_p - ca_p_low_slack, "CaP_Ratio_Min"
    prob += total_ca <= CA_P_RATIO_MAX * total_p + ca_p_high_slack, "CaP_Ratio_Max"
    penalty_expr += CA_P_PENALTY * (
        (ca_p_low_slack + ca_p_high_slack) / expected_daily_intake_kg
    )
    adjustments.append(
        f"⚖️ Ca:P oranı {CA_P_RATIO_MIN}-{CA_P_RATIO_MAX} aralığında tutulmaya çalışılıyor."
    )

    # 6. Kuru Madde kısıtı (% doğal hali bazında)
    # Rasyon KM% = Toplam KM / Toplam Doğal Hali * 100
    if constraints.dry_matter_min is not None:
        dm_min_frac = constraints.dry_matter_min / 100.0
        slack = slack_vars["dm_min_slack"]
        prob += total_dm >= dm_min_frac * total_intake - slack, "DM_Min"

    if constraints.dry_matter_max is not None:
        dm_max_frac = constraints.dry_matter_max / 100.0
        slack = slack_vars["dm_max_slack"]
        prob += total_dm <= dm_max_frac * total_intake + slack, "DM_Max"

    # 7. If daily intake target provided (HARD constraint) - interpret as DM BASIS (standard dairy nutrition)
    # total_dm = sum of feed DM intake in kg DM/day
    if constraints.daily_intake_kg is not None:
        target_dm = constraints.daily_intake_kg
        prob += total_dm == target_dm, "Fixed_DM_Intake"
        # Also set reasonable max as-fed intake as safety (assuming min 35% DM in ration)
        prob += total_intake <= target_dm / 0.35, "Max_AsFed_Intake_Safety"

    # ============================================================
    # 8. RASYONUN YENEBİLİRLİĞİ: NDF ve kaba yem kuralları
    #
    # Besin hedeflerini tutturmak yeterli değil — inek rasyonu fiziksel olarak
    # tüketebilmeli. NDF penceresi olmadan çözücü en ucuz lif kaynağını (saman)
    # yükleyip NDF'yi %43'e çıkarıyor; o rasyon kâğıt üzerinde doğru, ahırda yenmez.
    #
    # Katı modda slack yok; yumuşak modda her kural cezalı slack alır ve cezalar
    # penalty_expr'e EKLENİR (amaç fonksiyonu aşağıda tek seferde atanır).
    # ============================================================
    ration_rule_slacks = []

    def rule_slack(name: str):
        """Katı modda 0, yumuşak modda cezalandırılan yeni bir slack döndürür."""
        if hard_ration_rules:
            return 0
        var = pulp.LpVariable(f"rule_slack_{name}", lowBound=0)
        ration_rule_slacks.append(var)
        return var

    total_ndf = pulp.lpSum(
        feed.dm_fraction() * feed.ndf * feed_vars[feed.name] for feed in feeds
    )
    has_ndf_data = any(feed.ndf > 0 for feed in feeds)

    if has_ndf_data:
        # Rasyon NDF'si (% KM)
        prob += total_ndf >= RATION_NDF_MIN * total_dm - rule_slack("ndf_min"), "NDF_Min"
        prob += total_ndf <= RATION_NDF_MAX * total_dm + rule_slack("ndf_max"), "NDF_Max"
        adjustments.append(
            f"🌱 Rasyon NDF'si %{RATION_NDF_MIN:.0f}-%{RATION_NDF_MAX:.0f} KM aralığında tutuluyor."
        )

        # NDF alım kapasitesi: inek günde canlı ağırlığının ~%1.2'si kadar NDF yiyebilir.
        if body_weight_kg:
            ndf_capacity_kg = NDF_INTAKE_PCT_BW / 100.0 * body_weight_kg
            # total_ndf birimi: kg KM x (% KM) -> 100'e bölünce kg NDF/gün
            prob += total_ndf / 100.0 <= ndf_capacity_kg + rule_slack("ndf_capacity"), "NDF_Intake_Capacity"
            adjustments.append(
                f"📏 NDF alım kapasitesi: en fazla {ndf_capacity_kg:.1f} kg NDF/gün "
                f"(canlı ağırlığın %{NDF_INTAKE_PCT_BW})."
            )

    forage_feeds = [f for f in feeds if feed_classifications.get(f.name) in FORAGE_TYPES]
    silage_feeds = [f for f in feeds if feed_classifications.get(f.name) == "silage"]
    hay_feeds = [f for f in feeds if feed_classifications.get(f.name) == "hay"]

    if forage_feeds:
        forage_dm = pulp.lpSum(f.dm_fraction() * feed_vars[f.name] for f in forage_feeds)
        prob += forage_dm >= FORAGE_RATIO_MIN * total_dm - rule_slack("forage_min"), "Min_Forage_Ratio"
        prob += forage_dm <= FORAGE_RATIO_MAX * total_dm + rule_slack("forage_max"), "Max_Forage_Ratio"
        adjustments.append(
            f"🌾 Kaba yem oranı %{FORAGE_RATIO_MIN * 100:.0f}-%{FORAGE_RATIO_MAX * 100:.0f} KM: "
            f"{', '.join(f.name for f in forage_feeds)}"
        )

        # Kaba yemden gelen NDF (peNDF vekili) - rumen çiğneme faaliyeti için.
        if has_ndf_data:
            forage_ndf = pulp.lpSum(
                f.dm_fraction() * f.ndf * feed_vars[f.name] for f in forage_feeds
            )
            prob += forage_ndf >= FORAGE_NDF_MIN * total_dm - rule_slack("forage_ndf"), "Forage_NDF_Min"

        # Silaj seçildiyse kaba yemin TABANI silajdır. Bu kural olmadan çözücü
        # silajı bırakıp kuru otu tavana dayıyor (yonca daha yüksek proteinli
        # olduğu için soya küspesinden tasarruf ettiriyor).
        if silage_feeds:
            silage_dm = pulp.lpSum(f.dm_fraction() * feed_vars[f.name] for f in silage_feeds)
            prob += silage_dm >= SILAGE_BASE_MIN * total_dm - rule_slack("silage_base"), "Silage_Base_Min"
            adjustments.append(
                f"🌽 Silaj kaba yemin tabanı: en az %{SILAGE_BASE_MIN * 100:.0f} KM "
                f"({', '.join(f.name for f in silage_feeds)})."
            )
            if hay_feeds:
                hay_dm = pulp.lpSum(f.dm_fraction() * feed_vars[f.name] for f in hay_feeds)
                prob += hay_dm <= HAY_AGG_MAX_WITH_SILAGE * total_dm + rule_slack("hay_agg"), "Hay_Aggregate_Max"
    else:
        adjustments.append(
            "⚠️ UYARI: Rasyonda hiç kaba yem yok! Rumen sağlığı için silaj/kuru ot ekleyin."
        )

    # Rasyon kuralı ihlallerinin cezası - besin hedefi ihlalleriyle aynı ağırlıkta.
    if ration_rule_slacks:
        RATION_RULE_PENALTY = soft_constraint_penalty * 4.0
        penalty_expr += RATION_RULE_PENALTY * pulp.lpSum(
            slack / expected_daily_intake_kg for slack in ration_rule_slacks
        )

    # ==================== AMAÇ FONKSİYONU (EN SONDA ATANIR) ====================
    # Tüm ceza terimleri toplandıktan sonra tek seferde atanır. Bkz. yukarıdaki
    # uyarı: PuLP sonradan yapılan `penalty_expr += ...` eklemelerini görmez.
    prob += feed_cost + penalty_expr, "Total_Cost"

    # Solver ayarları
    solver = pulp.PULP_CBC_CMD(
        msg=False,
        timeLimit=30,
        threads=1,
        gapRel=0.0001
    )

    try:
        prob.solve(solver)
    except Exception as e:
        logger.error(f"Solver error: {e}")
        return OptimizationResult(
            status=f"Solver Hatasi: {str(e)}",
            feed_amounts={f.name: 0.0 for f in feeds},
            feed_proportions={f.name: 0.0 for f in feeds},
            nutrient_content={},
            daily_nutrient_intake={},
            total_cost_per_day=0.0,
            total_cost_per_kg=0.0,
            total_daily_intake_kg=0.0,
            constraints_status={},
            violations=[f"Solver istisnasi: {e}"],
            adjustments=["Solver hatası oluştu, varsayılan değerler döndürülüyor."]
        )

    # Sonuçları topla
    status = pulp.LpStatus[prob.status]

    # ⚠️ Infeasible bir çözümden sonra CBC'nin değişkenlerde bıraktığı değerler
    # ANLAMSIZDIR - kısıtları sağlamazlar. Eskiden bunlar olduğu gibi okunup durum
    # "Feasible (soft constraint cozumu)" diye yeniden etiketleniyordu; sağlık
    # sınırları eklendiğinde bu, 25 kg tavanlı silajın 73 kg olarak raporlanmasına
    # yol açtı. Artık infeasible sonuç sıfırlanır ve dürüstçe bildirilir.
    infeasible = status == "Infeasible"

    feed_amounts = {}
    for feed in feeds:
        val = feed_vars[feed.name].varValue
        feed_amounts[feed.name] = 0.0 if infeasible or val is None else val

    # Oranlar
    total_kg = sum(feed_amounts.values())
    feed_proportions = {}
    if total_kg > 0:
        for name, amt in feed_amounts.items():
            feed_proportions[name] = amt / total_kg
    else:
        for name in feed_amounts:
            feed_proportions[name] = 0.0

    # Besin maddesi içeriği (KM bazında)
    nutrient_content = {}
    daily_intake = {}

    if total_kg > 0:
        # Gerçekleşen toplam KM
        actual_dm = sum(f.dm_fraction() * feed_amounts[f.name] for f in feeds)
        actual_cp = sum(f.dm_fraction() * (f.crude_protein / 100.0) * feed_amounts[f.name] for f in feeds)
        actual_me = sum(f.dm_fraction() * f.metabolic_energy * feed_amounts[f.name] for f in feeds)
        actual_ca = sum(f.dm_fraction() * f.calcium * feed_amounts[f.name] for f in feeds)
        actual_p = sum(f.dm_fraction() * f.phosphorus * feed_amounts[f.name] for f in feeds)
        actual_nel = sum(f.dm_fraction() * f.net_energy_lactation * feed_amounts[f.name] for f in feeds)
        # NDF birimi: (kg KM) x (% KM) -> 100'e bölünce kg NDF/gün
        actual_ndf = sum(f.dm_fraction() * f.ndf * feed_amounts[f.name] for f in feeds) / 100.0
        actual_forage_dm = sum(
            f.dm_fraction() * feed_amounts[f.name] for f in feeds if f.is_forage()
        )
        actual_forage_ndf = sum(
            f.dm_fraction() * f.ndf * feed_amounts[f.name] for f in feeds if f.is_forage()
        ) / 100.0

        if actual_dm > 0:
            nutrient_content["crude_protein_percent_dm"] = (actual_cp / actual_dm) * 100
            nutrient_content["metabolic_energy_mcal_per_kg_dm"] = actual_me / actual_dm
            nutrient_content["calcium_g_per_kg_dm"] = actual_ca / actual_dm
            nutrient_content["phosphorus_g_per_kg_dm"] = actual_p / actual_dm
            nutrient_content["dry_matter_percent"] = (actual_dm / total_kg) * 100
            nutrient_content["net_energy_lactation_mcal_per_kg_dm"] = actual_nel / actual_dm
            nutrient_content["ndf_percent_dm"] = (actual_ndf / actual_dm) * 100
            nutrient_content["forage_ndf_percent_dm"] = (actual_forage_ndf / actual_dm) * 100
            nutrient_content["forage_ratio_percent"] = (actual_forage_dm / actual_dm) * 100

            if actual_p > 0:
                nutrient_content["ca_p_ratio"] = actual_ca / actual_p
        else:
            nutrient_content = {
                "crude_protein_percent_dm": 0,
                "metabolic_energy_mcal_per_kg_dm": 0,
                "calcium_g_per_kg_dm": 0,
                "phosphorus_g_per_kg_dm": 0,
                "dry_matter_percent": 0,
                "ca_p_ratio": 0
            }

        # Doğal hali bazında değerler de ekle (bilgi için)
        nutrient_content["crude_protein_percent_af"] = (actual_cp / total_kg) * 100
        nutrient_content["metabolic_energy_mcal_per_kg_af"] = actual_me / total_kg
        nutrient_content["calcium_g_per_kg_af"] = actual_ca / total_kg
        nutrient_content["phosphorus_g_per_kg_af"] = actual_p / total_kg

        # Placeholders for coverage percentages (computed later after constraints checked)
        nutrient_content["crude_protein_coverage_percent"] = None
        nutrient_content["metabolic_energy_coverage_percent"] = None
        nutrient_content["calcium_coverage_percent"] = None
        nutrient_content["phosphorus_coverage_percent"] = None
        nutrient_content["dry_matter_coverage_percent"] = None

        # Günlük alım
        daily_intake["crude_protein_kg"] = actual_cp
        daily_intake["metabolic_energy_mcal"] = actual_me
        daily_intake["calcium_g"] = actual_ca
        daily_intake["phosphorus_g"] = actual_p
        daily_intake["dry_matter_kg"] = actual_dm
        daily_intake["ndf_kg"] = actual_ndf
        daily_intake["net_energy_lactation_mcal"] = actual_nel
    else:
        nutrient_content = {
            "crude_protein_percent_dm": 0,
            "metabolic_energy_mcal_per_kg_dm": 0,
            "calcium_g_per_kg_dm": 0,
            "phosphorus_g_per_kg_dm": 0,
            "dry_matter_percent": 0,
            "ca_p_ratio": 0,
            "ndf_percent_dm": 0,
            "forage_ndf_percent_dm": 0,
            "forage_ratio_percent": 0,
            "net_energy_lactation_mcal_per_kg_dm": 0,
            "crude_protein_percent_af": 0,
            "metabolic_energy_mcal_per_kg_af": 0,
            "calcium_g_per_kg_af": 0,
            "phosphorus_g_per_kg_af": 0
        }
        daily_intake = {
            k: 0.0 for k in [
                "crude_protein_kg", "metabolic_energy_mcal", "calcium_g", "phosphorus_g",
                "dry_matter_kg", "ndf_kg", "net_energy_lactation_mcal",
            ]
        }

    # Sadece yem maliyeti (ceza hariç) - asıl maliyet
    actual_feed_cost = sum(f.price_per_kg * feed_amounts[f.name] for f in feeds)
    cost_per_kg = actual_feed_cost / total_kg if total_kg > 0 else 0.0

    # Slack değişken değerleri (soft constraint analizi)
    slack_values = {}
    for name, var in slack_vars.items():
        val = var.varValue
        if val is not None and val > 1e-6:
            slack_values[name] = val

    # Kısıtlama durumu kontrolü ve düzeltme mesajları oluştur
    constraints_status = {}
    violations = []
    warnings = []

    # Ensure actual_dm is available (compute if total_kg was 0)
    if 'actual_dm' not in locals():
        actual_dm = sum(f.dm_fraction() * feed_amounts[f.name] for f in feeds)

    def check_constraint(name: str, actual: float, target: float, constraint_type: str, unit: str = "") -> str:
        """Kısıt kontrolü ve durum mesajı."""
        if constraint_type == "min":
            if actual >= target - 1e-6:
                return "OK"
            else:
                diff = target - actual
                return f"ALTINDA ({actual:.4f} < {target:.4f} {unit}, fark: {diff:.4f})"
        else:
            if actual <= target + 1e-6:
                return "OK"
            else:
                diff = actual - target
                return f"USTUNDE ({actual:.4f} > {target:.4f} {unit}, fark: {diff:.4f})"

    # Kısıtlama durumu kontrolü - use actual_dm (evaluated float) instead of total_dm (PuLP expression)
    if total_kg > 0 and actual_dm > 0:
        # CP
        actual_cp_pct = nutrient_content.get("crude_protein_percent_dm", 0)
        if constraints.crude_protein_min is not None:
            s = check_constraint("CP_min", actual_cp_pct, constraints.crude_protein_min, "min", "%KM")
            constraints_status["crude_protein_min"] = s
            if "ALTINDA" in s:
                violations.append(f"HP Min: {s}")
                slack_val = slack_values.get("cp_min_slack", 0)
                adjustments.append(
                    f"⚠️ Ham Protein minimumu ({constraints.crude_protein_min:.1f}% KM) sağlanamadı; "
                    f"gerçekleşen: {actual_cp_pct:.1f}% KM (fark: {constraints.crude_protein_min - actual_cp_pct:.1f}% KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} kg/gün HP ihlali)."
                )
        if constraints.crude_protein_max is not None:
            s = check_constraint("CP_max", actual_cp_pct, constraints.crude_protein_max, "max", "%KM")
            constraints_status["crude_protein_max"] = s
            if "USTUNDE" in s:
                violations.append(f"HP Max: {s}")
                slack_val = slack_values.get("cp_max_slack", 0)
                adjustments.append(
                    f"⚠️ Ham Protein maximumu ({constraints.crude_protein_max:.1f}% KM) aşıldı; "
                    f"gerçekleşen: {actual_cp_pct:.1f}% KM (fark: {actual_cp_pct - constraints.crude_protein_max:.1f}% KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} kg/gün HP ihlali)."
                )

        # ME
        actual_me = nutrient_content.get("metabolic_energy_mcal_per_kg_dm", 0)
        if constraints.metabolic_energy_min is not None:
            s = check_constraint("ME_min", actual_me, constraints.metabolic_energy_min, "min", "Mcal/kg KM")
            constraints_status["metabolic_energy_min"] = s
            if "ALTINDA" in s:
                violations.append(f"ME Min: {s}")
                slack_val = slack_values.get("me_min_slack", 0)
                adjustments.append(
                    f"⚠️ Metabolez Edilebilir Enerji minimumu ({constraints.metabolic_energy_min:.2f} Mcal/kg KM) sağlanamadı; "
                    f"gerçekleşen: {actual_me:.2f} Mcal/kg KM (fark: {constraints.metabolic_energy_min - actual_me:.2f} Mcal/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} Mcal/gün ME ihlali)."
                )
        if constraints.metabolic_energy_max is not None:
            s = check_constraint("ME_max", actual_me, constraints.metabolic_energy_max, "max", "Mcal/kg KM")
            constraints_status["metabolic_energy_max"] = s
            if "USTUNDE" in s:
                violations.append(f"ME Max: {s}")
                slack_val = slack_values.get("me_max_slack", 0)
                adjustments.append(
                    f"⚠️ Metabolez Edilebilir Enerji maximumu ({constraints.metabolic_energy_max:.2f} Mcal/kg KM) aşıldı; "
                    f"gerçekleşen: {actual_me:.2f} Mcal/kg KM (fark: {actual_me - constraints.metabolic_energy_max:.2f} Mcal/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} Mcal/gün ME ihlali)."
                )

        # Ca
        actual_ca_val = nutrient_content.get("calcium_g_per_kg_dm", 0)
        if constraints.calcium_min is not None:
            s = check_constraint("Ca_min", actual_ca_val, constraints.calcium_min, "min", "g/kg KM")
            constraints_status["calcium_min"] = s
            if "ALTINDA" in s:
                violations.append(f"Ca Min: {s}")
                slack_val = slack_values.get("ca_min_slack", 0)
                adjustments.append(
                    f"⚠️ Kalsiyum minimumu ({constraints.calcium_min:.1f} g/kg KM) sağlanamadı; "
                    f"gerçekleşen: {actual_ca_val:.1f} g/kg KM (fark: {constraints.calcium_min - actual_ca_val:.1f} g/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} g/gün Ca ihlali)."
                )
        if constraints.calcium_max is not None:
            s = check_constraint("Ca_max", actual_ca_val, constraints.calcium_max, "max", "g/kg KM")
            constraints_status["calcium_max"] = s
            if "USTUNDE" in s:
                violations.append(f"Ca Max: {s}")
                slack_val = slack_values.get("ca_max_slack", 0)
                adjustments.append(
                    f"⚠️ Kalsiyum maximumu ({constraints.calcium_max:.1f} g/kg KM) aşıldı; "
                    f"gerçekleşen: {actual_ca_val:.1f} g/kg KM (fark: {actual_ca_val - constraints.calcium_max:.1f} g/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} g/gün Ca ihlali)."
                )

        # P
        actual_p_val = nutrient_content.get("phosphorus_g_per_kg_dm", 0)
        if constraints.phosphorus_min is not None:
            s = check_constraint("P_min", actual_p_val, constraints.phosphorus_min, "min", "g/kg KM")
            constraints_status["phosphorus_min"] = s
            if "ALTINDA" in s:
                violations.append(f"P Min: {s}")
                slack_val = slack_values.get("p_min_slack", 0)
                adjustments.append(
                    f"⚠️ Fosfor minimumu ({constraints.phosphorus_min:.1f} g/kg KM) sağlanamadı; "
                    f"gerçekleşen: {actual_p_val:.1f} g/kg KM (fark: {constraints.phosphorus_min - actual_p_val:.1f} g/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} g/gün P ihlali)."
                )
        if constraints.phosphorus_max is not None:
            s = check_constraint("P_max", actual_p_val, constraints.phosphorus_max, "max", "g/kg KM")
            constraints_status["phosphorus_max"] = s
            if "USTUNDE" in s:
                violations.append(f"P Max: {s}")
                slack_val = slack_values.get("p_max_slack", 0)
                adjustments.append(
                    f"⚠️ Fosfor maximumu ({constraints.phosphorus_max:.1f} g/kg KM) aşıldı; "
                    f"gerçekleşen: {actual_p_val:.1f} g/kg KM (fark: {actual_p_val - constraints.phosphorus_max:.1f} g/kg KM). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} g/gün P ihlali)."
                )

        # DM
        actual_dm_pct = nutrient_content.get("dry_matter_percent", 0)
        if constraints.dry_matter_min is not None:
            s = check_constraint("DM_min", actual_dm_pct, constraints.dry_matter_min, "min", "%")
            constraints_status["dry_matter_min"] = s
            if "ALTINDA" in s:
                violations.append(f"KM Min: {s}")
                slack_val = slack_values.get("dm_min_slack", 0)
                adjustments.append(
                    f"⚠️ Kuru Madde minimumu (%{constraints.dry_matter_min:.1f}) sağlanamadı; "
                    f"gerçekleşen: %{actual_dm_pct:.1f} (fark: {constraints.dry_matter_min - actual_dm_pct:.1f}%). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} kg/gün KM ihlali)."
                )
        if constraints.dry_matter_max is not None:
            s = check_constraint("DM_max", actual_dm_pct, constraints.dry_matter_max, "max", "%")
            constraints_status["dry_matter_max"] = s
            if "USTUNDE" in s:
                violations.append(f"KM Max: {s}")
                slack_val = slack_values.get("dm_max_slack", 0)
                adjustments.append(
                    f"⚠️ Kuru Madde maximumu (%{constraints.dry_matter_max:.1f}) aşıldı; "
                    f"gerçekleşen: %{actual_dm_pct:.1f} (fark: {actual_dm_pct - constraints.dry_matter_max:.1f}%). "
                    f"Hedef biraz gevşetildi (slack: {slack_val:.4f} kg/gün KM ihlali)."
                )

        # Ca:P oranı uyarısı
        ca_p = nutrient_content.get("ca_p_ratio", 0)
        if ca_p > 0 and (ca_p < 1.0 or ca_p > 3.0):
            warnings.append(f"Ca:P oranı = {ca_p:.2f} (önerilen 1.0-3.0 aralığında)")

    # ============================================================
    # REQUIREMENT COVERAGE PERCENTAGES
    # Compute actual / target * 100 for each nutrient constraint
    # ============================================================
    def calc_coverage(actual_val: float, target: Optional[float], is_min: bool) -> Optional[float]:
        """Calculate coverage % = actual / target * 100 (clamped to reasonable range)."""
        if target is None:
            return None
        if target == 0:
            return 100.0
        pct = (actual_val / target) * 100
        # Clamp to reasonable bounds (0-500%)
        return max(0.0, min(pct, 500.0))

    coverage_cp = None
    coverage_me = None
    coverage_ca = None
    coverage_p = None
    coverage_dm = None

    if total_kg > 0 and actual_dm > 0:
        actual_cp_pct = nutrient_content.get("crude_protein_percent_dm", 0)
        actual_me = nutrient_content.get("metabolic_energy_mcal_per_kg_dm", 0)
        actual_ca_val = nutrient_content.get("calcium_g_per_kg_dm", 0)
        actual_p_val = nutrient_content.get("phosphorus_g_per_kg_dm", 0)
        actual_dm_pct = nutrient_content.get("dry_matter_percent", 0)

        if constraints.crude_protein_min is not None:
            coverage_cp = calc_coverage(actual_cp_pct, constraints.crude_protein_min, True)
        elif constraints.crude_protein_max is not None:
            # For max constraint, coverage = target / actual (as inverse)
            coverage_cp = calc_coverage(constraints.crude_protein_max, actual_cp_pct, False)

        if constraints.metabolic_energy_min is not None:
            coverage_me = calc_coverage(actual_me, constraints.metabolic_energy_min, True)
        elif constraints.metabolic_energy_max is not None:
            coverage_me = calc_coverage(constraints.metabolic_energy_max, actual_me, False)

        if constraints.calcium_min is not None:
            coverage_ca = calc_coverage(actual_ca_val, constraints.calcium_min, True)
        elif constraints.calcium_max is not None:
            coverage_ca = calc_coverage(constraints.calcium_max, actual_ca_val, False)

        if constraints.phosphorus_min is not None:
            coverage_p = calc_coverage(actual_p_val, constraints.phosphorus_min, True)
        elif constraints.phosphorus_max is not None:
            coverage_p = calc_coverage(constraints.phosphorus_max, actual_p_val, False)

        if constraints.dry_matter_min is not None:
            coverage_dm = calc_coverage(actual_dm_pct, constraints.dry_matter_min, True)
        elif constraints.dry_matter_max is not None:
            coverage_dm = calc_coverage(constraints.dry_matter_max, actual_dm_pct, False)

    # Update nutrient_content with coverage percentages for schema compatibility
    nutrient_content["crude_protein_coverage_percent"] = coverage_cp
    nutrient_content["metabolic_energy_coverage_percent"] = coverage_me
    nutrient_content["calcium_coverage_percent"] = coverage_ca
    nutrient_content["phosphorus_coverage_percent"] = coverage_p
    nutrient_content["dry_matter_coverage_percent"] = coverage_dm

    # Yumuşak modda ASLA "Infeasible" döndürmez - en kötü durumda "Feasible (with violations)".
    # Katı modda (hard_nutrients=True) infeasible GERÇEK bir sonuçtur ve olduğu gibi
    # bildirilir; optimize_ration() bunu görüp yumuşak aşamaya düşer.
    if infeasible:
        # Hayvan sağlığı sınırları (değişken üst sınırları) gevşetilemediği için
        # yumuşak modda bile infeasible mümkündür. Uydurma bir "feasible" etiketi
        # yapıştırmak yerine olduğu gibi bildirilir.
        result_status = "Infeasible (saglik sinirlari icinde cozum yok)"
    elif hard_nutrients and status != "Optimal":
        result_status = "Infeasible (kati besin hedefleri saglanamadi)"
    elif status == "Optimal":
        if violations:
            result_status = "Feasible (hedefler kısmen gevsetildi)"
        else:
            result_status = "Optimal (tum hedefler saglandi)"
    else:
        # Solver Optimal bulamasa bile feasible bir çözüm bulmuş olabilir
        result_status = "Feasible (hedefler kısmen gevsetildi)" if violations else status

    solver_details = {
        "status": status,
        "objective_value": pulp.value(prob.objective) if prob.objective else 0.0,
        "feed_cost": actual_feed_cost,
        "num_variables": len(prob.variables()),
        "num_constraints": len(prob.constraints()),
        "slack_values": slack_values,
        "force_use_all_feeds": force_use_all_feeds,
        "min_inclusion_pct_dm": min_inclusion_pct_dm if force_use_all_feeds else None,
        "hard_nutrients": hard_nutrients
    }

    forced_feeds_list = []
    if force_use_all_feeds:
        forced_feeds_list = [f.name for f in feeds]

    return OptimizationResult(
        status=result_status,
        feed_amounts=feed_amounts,
        feed_proportions=feed_proportions,
        nutrient_content=nutrient_content,
        daily_nutrient_intake=daily_intake,
        total_cost_per_day=actual_feed_cost,
        total_cost_per_kg=cost_per_kg,
        total_daily_intake_kg=total_kg,
        constraints_status=constraints_status,
        violations=violations,
        warnings=warnings,
        adjustments=adjustments,
        solver_details=solver_details,
        crude_protein_coverage_percent=coverage_cp,
        metabolic_energy_coverage_percent=coverage_me,
        calcium_coverage_percent=coverage_ca,
        phosphorus_coverage_percent=coverage_p,
        dry_matter_coverage_percent=coverage_dm
    )


def optimization_result_to_schema(result: OptimizationResult, feed_inputs: List[FeedInput]) -> OptimizationResponse:
    """
    Convert OptimizationResult to structured OptimizationResponse schema.
    This provides the frontend with clean, typed data including Turkish explanations.
    """
    # Build feed recommendations with actual price data from feed_inputs
    feed_price_map = {f.name: f.price_per_kg for f in feed_inputs}

    feed_recs = []
    for name, amount in result.feed_amounts.items():
        if amount > 0.001:
            proportion = result.feed_proportions.get(name, 0)
            price = feed_price_map.get(name, 0.0)
            feed_recs.append(FeedRecommendation(
                name=name,
                amount_kg_per_day=round(amount, 3),
                proportion=round(proportion, 4),  # 0-1
                price_per_kg=round(price, 2),
                cost_per_day=round(amount * price, 2),
                nutrients={}
            ))

    # Build nutrient content
    nutrient_content = NutrientContent(
        crude_protein_percent_dm=round(result.nutrient_content.get("crude_protein_percent_dm", 0), 2),
        metabolic_energy_mcal_per_kg_dm=round(result.nutrient_content.get("metabolic_energy_mcal_per_kg_dm", 0), 3),
        calcium_g_per_kg_dm=round(result.nutrient_content.get("calcium_g_per_kg_dm", 0), 2),
        phosphorus_g_per_kg_dm=round(result.nutrient_content.get("phosphorus_g_per_kg_dm", 0), 2),
        dry_matter_percent=round(result.nutrient_content.get("dry_matter_percent", 0), 1),
        net_energy_lactation_mcal_per_kg_dm=round(
            result.nutrient_content.get("net_energy_lactation_mcal_per_kg_dm", 0), 3),
        ndf_percent_dm=round(result.nutrient_content.get("ndf_percent_dm", 0), 1),
        forage_ndf_percent_dm=round(result.nutrient_content.get("forage_ndf_percent_dm", 0), 1),
        forage_ratio_percent=round(result.nutrient_content.get("forage_ratio_percent", 0), 1),
        ca_p_ratio=round(result.nutrient_content.get("ca_p_ratio", 0), 2) if result.nutrient_content.get("ca_p_ratio") else None,
        crude_protein_percent_af=round(result.nutrient_content.get("crude_protein_percent_af", 0), 2),
        metabolic_energy_mcal_per_kg_af=round(result.nutrient_content.get("metabolic_energy_mcal_per_kg_af", 0), 3),
        calcium_g_per_kg_af=round(result.nutrient_content.get("calcium_g_per_kg_af", 0), 2),
        phosphorus_g_per_kg_af=round(result.nutrient_content.get("phosphorus_g_per_kg_af", 0), 2),
    )

    # Build daily intake. NEL/NDF are optional on the schema: the solver only fills them
    # when the feed rows carry those columns, so pass None through instead of a fake 0.
    intake = result.daily_nutrient_intake

    def _opt_round(key: str, digits: int) -> Optional[float]:
        value = intake.get(key)
        return None if value is None else round(value, digits)

    daily_intake = DailyNutrientIntake(
        crude_protein_kg=round(intake.get("crude_protein_kg", 0), 3),
        metabolic_energy_mcal=round(intake.get("metabolic_energy_mcal", 0), 2),
        calcium_g=round(intake.get("calcium_g", 0), 1),
        phosphorus_g=round(intake.get("phosphorus_g", 0), 1),
        dry_matter_kg=round(intake.get("dry_matter_kg", 0), 2),
        net_energy_lactation_mcal=_opt_round("net_energy_lactation_mcal", 2),
        ndf_kg=_opt_round("ndf_kg", 3),
    )

    # Build constraint status with Turkish explanations
    constraints_status = []
    # Map constraint names to nutrient keys and units
    constraint_info = {
        "crude_protein_min": ("crude_protein_percent_dm", "% KM", "Ham Protein Minimum"),
        "crude_protein_max": ("crude_protein_percent_dm", "% KM", "Ham Protein Maksimum"),
        "metabolic_energy_min": ("metabolic_energy_mcal_per_kg_dm", "Mcal/kg KM", "Met. Enerji Minimum"),
        "metabolic_energy_max": ("metabolic_energy_mcal_per_kg_dm", "Mcal/kg KM", "Met. Enerji Maksimum"),
        "calcium_min": ("calcium_g_per_kg_dm", "g/kg KM", "Kalsiyum Minimum"),
        "calcium_max": ("calcium_g_per_kg_dm", "g/kg KM", "Kalsiyum Maksimum"),
        "phosphorus_min": ("phosphorus_g_per_kg_dm", "g/kg KM", "Fosfor Minimum"),
        "phosphorus_max": ("phosphorus_g_per_kg_dm", "g/kg KM", "Fosfor Maksimum"),
        "dry_matter_min": ("dry_matter_percent", "%", "Kuru Madde Minimum"),
        "dry_matter_max": ("dry_matter_percent", "%", "Kuru Madde Maksimum"),
    }

    for constraint, status in result.constraints_status.items():
        turkish_exp = None
        for adj in result.adjustments:
            # Match constraint name to adjustment
            constraint_readable = constraint.replace("_", " ")
            if constraint_readable.lower() in adj.lower() or constraint.lower() in adj.lower():
                turkish_exp = adj
                break

        info = constraint_info.get(constraint)
        if info:
            actual_key, unit, name = info
            actual_val = result.nutrient_content.get(actual_key, 0)
        else:
            actual_val = 0
            unit = ""
            name = constraint

        constraints_status.append(ConstraintStatus(
            constraint=constraint,
            actual=round(actual_val, 2),
            status=status,
            unit=unit,
            turkish_explanation=turkish_exp
        ))

    # Determine success based on status
    success = result.status.startswith("Optimal") or result.status.startswith("Feasible")

    return OptimizationResponse(
        status=result.status,
        success=success,
        total_cost_per_day=round(result.total_cost_per_day, 2),
        total_cost_per_kg=round(result.total_cost_per_kg, 2),
        total_daily_intake_kg=round(result.total_daily_intake_kg, 2),
        feed_recommendations=feed_recs,
        nutrient_content=nutrient_content,
        daily_intake=daily_intake,
        constraints_status=constraints_status,
        turkish_explanations=result.adjustments,
        warnings=result.warnings,
        solver_details=result.solver_details
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
    soft_constraint_penalty: float = 100000.0
) -> OptimizationResult:
    """İki aşamalı, BESİN ÖNCELİKLİ rasyon optimizasyonu. Tek giriş noktasıdır.

    AŞAMA 1: Besin hedefleri (HP/ME/Ca/P) ve rasyon kuralları (yem sınıfı pencereleri,
             NDF, kaba yem oranı, silaj tabanı) KATI kısıt olarak kurulur ve maliyet
             minimize edilir. Çözüm bulunursa hem besin değerleri hem de rasyonun
             yenebilirliği GARANTİLİDİR.
    AŞAMA 2: Aşama 1 infeasible ise her şey yumuşak kısıtla tekrar çözülür ve
             `diagnose_infeasibility()` çıktısı Türkçe açıklama olarak eklenir.
             Böylece hedef sessizce gevşetilmek yerine nedeni bildirilir.

    Args:
        feeds: Kullanılacak yem bileşenleri
        constraints: Besin kısıtları (KM bazında; Ca/P g/kg KM)
        use_all_selected: True ise seçilen tüm yemler rasyona katılmaya zorlanır
        min_inclusion_pct_dm: Zorunlu dahil hedefi (% KM); yem tipine göre kısılır
        expected_daily_intake_kg: Beklenen günlük KM tüketimi (ceza normalizasyonu)
        max_daily_intake_kg: Maksimum günlük doğal hali tüketim (güvenlik sınırı)
        body_weight_kg: Canlı ağırlık; NDF alım kapasitesi kısıtı için
        soft_constraint_penalty: Temel ceza katsayısı

    Returns:
        OptimizationResult - `solver_details["nutrition_mode"]` alanı hangi aşamanın
        sonucu döndürdüğünü belirtir ("hard" veya "relaxed").
    """
    if max_daily_intake_kg is None:
        # KM tüketimini doğal haline çevirirken en az %35 KM varsayımı
        max_daily_intake_kg = expected_daily_intake_kg / 0.35

    common = dict(
        feeds=feeds,
        constraints=constraints,
        soft_constraint_penalty=soft_constraint_penalty,
        max_daily_intake_kg=max_daily_intake_kg,
        expected_daily_intake_kg=expected_daily_intake_kg,
        force_use_all_feeds=use_all_selected,
        min_inclusion_pct_dm=min_inclusion_pct_dm,
        body_weight_kg=body_weight_kg,
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
    # Hayvan sağlığı sınırları burada da geçerlidir (değişken üst sınırı), bu yüzden
    # ikinci faz da infeasible dönebilir - seçilen yemler sağlık sınırları içinde
    # hedeflenen kuru maddeyi karşılayamıyorsa.
    relaxed = solve_ration_problem(**common, hard_nutrients=False, hard_ration_rules=False)
    relaxed.solver_details["nutrition_mode"] = "relaxed"
    relaxed.solver_details["ration_rules_mode"] = "relaxed"
    relaxed.solver_details["hard_phase_status"] = result.status

    diagnosis = diagnose_infeasibility(feeds, constraints, body_weight_kg=body_weight_kg)

    if "Infeasible" in relaxed.status:
        # Sağlık sınırları aşılamadığı için hiçbir rasyon kurulamadı.
        relaxed.solver_details["nutrition_mode"] = "infeasible"
        relaxed.adjustments = [
            "⛔ Seçilen yemlerle, hayvan sağlığı sınırlarını aşmadan rasyon kurulamıyor. "
            "Sağlık sınırları hiçbir koşulda gevşetilmez — yem eklemek gerekiyor:"
        ] + diagnosis.get("recommendations", []) + relaxed.adjustments
        return relaxed

    notes = [
        "⚠️ Besin hedeflerinin tamamı seçilen yemlerle SAĞLANAMADI; hedefler "
        "gevşetilerek en yakın rasyon üretildi. Aşağıdaki teşhisi dikkate alın:"
    ]
    notes.extend(diagnosis.get("recommendations", []))
    if len(notes) == 1:
        notes.append(
            "Tek tek her besin hedefi karşılanabilir görünüyor; sorun hedeflerin "
            "BİRLİKTE sağlanamamasından kaynaklanıyor. Daha çeşitli yem ekleyin "
            "veya hedef aralıklarını genişletin."
        )
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


def format_result_for_agent(result: OptimizationResult) -> str:
    """Agent için metin formatında sonuç üretir."""
    lines = [
        "[RASYON OPTIMIZASYON SONUCU]",
        f"Durum: {result.status}",
        f"Toplam Gunluk Maliyet: {result.total_cost_per_day:.2f} TL/gun",
        f"Rasyon Maliyeti: {result.total_cost_per_kg:.2f} TL/kg",
        f"Gunluk Tuketim: {result.total_daily_intake_kg:.2f} kg/gun (dogal hali)",
        "",
        "[YEM KOMPOZISYONU (kg/gun)]:"
    ]

    for name, amt in sorted(result.feed_amounts.items(), key=lambda x: -x[1]):
        if amt > 0.001:
            pct = result.feed_proportions.get(name, 0) * 100
            lines.append(f"  - {name}: {amt:.3f} kg/gun ({pct:.1f}%)")

    lines.extend([
        "",
        "[RASYON BESIN DEGERI (KM Bazinda)]:",
        f"  - Ham Protein: {result.nutrient_content.get('crude_protein_percent_dm', 0):.2f}% KM",
        f"  - Met. Enerji: {result.nutrient_content.get('metabolic_energy_mcal_per_kg_dm', 0):.3f} Mcal/kg KM",
        f"  - Kalsiyum: {result.nutrient_content.get('calcium_g_per_kg_dm', 0):.2f} g/kg KM",
        f"  - Fosfor: {result.nutrient_content.get('phosphorus_g_per_kg_dm', 0):.2f} g/kg KM",
        f"  - Kuru Madde: {result.nutrient_content.get('dry_matter_percent', 0):.1f}% (dogal hali)",
        f"  - Ca:P Orani: {result.nutrient_content.get('ca_p_ratio', 0):.2f}",
        "",
        "[GUNLUK BESIN MADDESI ALIMI]:",
        f"  - Ham Protein: {result.daily_nutrient_intake.get('crude_protein_kg', 0):.3f} kg/gun",
        f"  - Met. Enerji: {result.daily_nutrient_intake.get('metabolic_energy_mcal', 0):.2f} Mcal/gun",
        f"  - Kalsiyum: {result.daily_nutrient_intake.get('calcium_g', 0):.1f} g/gun",
        f"  - Fosfor: {result.daily_nutrient_intake.get('phosphorus_g', 0):.1f} g/gun",
        f"  - Kuru Madde: {result.daily_nutrient_intake.get('dry_matter_kg', 0):.2f} kg/gun",
    ])

    if result.constraints_status:
        lines.append("")
        lines.append("[KISITLAMA DURUMU]:")
        for constr, stat in result.constraints_status.items():
            icon = "[OK]" if stat == "OK" else "[GEVSETILDI]"
            lines.append(f"  {icon} {constr}: {stat}")

    if result.violations:
        lines.append("")
        lines.append("[IHLALER (GEVSETILEN KISITLAR)]:")
        for v in result.violations:
            lines.append(f"  - {v}")

    if result.adjustments:
        lines.append("")
        lines.append("[DUZELTMELER (Turkce Aciklamalar)]:")
        for a in result.adjustments:
            lines.append(f"  - {a}")

    if result.warnings:
        lines.append("")
        lines.append("[UYARILAR]:")
        for w in result.warnings:
            lines.append(f"  - {w}")

    if result.solver_details.get("slack_values"):
        lines.append("")
        lines.append("[YUMUSAK KISIT SLACK DEGERLERI (GUNLUK TOPLAM IHLAL MIKTARLARI)]:")
        for k, v in result.solver_details["slack_values"].items():
            lines.append(f"  - {k}: {v:.6f}")

    return "\n".join(lines)


def diagnose_infeasibility(
    feeds: List[FeedIngredient],
    constraints: NutrientConstraints,
    body_weight_kg: Optional[float] = None
) -> Dict[str, Any]:
    """
    İhmaliyetli problem için teşhis yapar.
    Her kısıtı tek tek test ederek hangisinin sorun yarattığını bulur.
    Not: Soft constraints ile artık 'Infeasible' gelmez, bu fonksiyon teşhis amaçlıdır.
    """
    diagnosis = {
        "individual_constraint_tests": {},
        "feed_nutrient_ranges": {},
        "recommendations": []
    }

    # Her yemin besin aralıklarını hesapla
    for nutrient in ["crude_protein", "metabolic_energy", "calcium", "phosphorus", "dry_matter"]:
        values = []
        for f in feeds:
            if nutrient == "crude_protein":
                val = f.crude_protein
            elif nutrient == "metabolic_energy":
                val = f.metabolic_energy
            elif nutrient == "calcium":
                val = f.calcium
            elif nutrient == "phosphorus":
                val = f.phosphorus
            elif nutrient == "dry_matter":
                val = f.dry_matter
            else:
                val = 0
            values.append(val)

        if values:
            diagnosis["feed_nutrient_ranges"][nutrient] = {
                "min": min(values),
                "max": max(values),
                "feeds": {f.name: (f.crude_protein if nutrient=="crude_protein" else
                                     f.metabolic_energy if nutrient=="metabolic_energy" else
                                     f.calcium if nutrient=="calcium" else
                                     f.phosphorus if nutrient=="phosphorus" else
                                     f.dry_matter) for f in feeds}
            }

    # Her kısıtı tek başına test et
    test_constraints = [
        ("crude_protein_min", "crude_protein"),
        ("crude_protein_max", "crude_protein"),
        ("metabolic_energy_min", "metabolic_energy"),
        ("metabolic_energy_max", "metabolic_energy"),
        ("calcium_min", "calcium"),
        ("calcium_max", "calcium"),
        ("phosphorus_min", "phosphorus"),
        ("phosphorus_max", "phosphorus"),
        ("dry_matter_min", "dry_matter"),
        ("dry_matter_max", "dry_matter"),
    ]

    for constr_name, nutrient in test_constraints:
        target = getattr(constraints, constr_name)
        if target is None:
            continue

        feasible_feeds = []
        for f in feeds:
            if nutrient == "crude_protein":
                val = f.crude_protein
            elif nutrient == "metabolic_energy":
                val = f.metabolic_energy
            elif nutrient == "calcium":
                val = f.calcium
            elif nutrient == "phosphorus":
                val = f.phosphorus
            elif nutrient == "dry_matter":
                val = f.dry_matter
            else:
                val = 0

            if constr_name.endswith("_min") and val >= target:
                feasible_feeds.append(f.name)
            elif constr_name.endswith("_max") and val <= target:
                feasible_feeds.append(f.name)

        diagnosis["individual_constraint_tests"][constr_name] = {
            "target": target,
            "feasible_feeds": feasible_feeds,
            "num_feasible": len(feasible_feeds)
        }

        if len(feasible_feeds) == 0:
            diagnosis["recommendations"].append(
                f"❌ '{constr_name}' için hedef {target} - HIÇBİR YEM BU KRİTERİ KARŞILAMIYOR. "
                f"Hedefi gevşetin veya farklı yemler ekleyin."
            )
        elif len(feasible_feeds) < 2:
            diagnosis["recommendations"].append(
                f"⚠️ '{constr_name}' için sadece {len(feasible_feeds)} yem uygun: {', '.join(feasible_feeds)}. "
                f"Rasyon çeşitliliği sınırlı kalabilir."
            )

    # ---- Rasyon kuralları (yem sınıfı pencereleri, NDF, kaba yem oranı) ----
    diagnosis["recommendations"].extend(
        _diagnose_ration_rules(feeds, constraints, body_weight_kg)
    )

    return diagnosis


def _diagnose_ration_rules(
    feeds: List[FeedIngredient],
    constraints: Optional[NutrientConstraints] = None,
    body_weight_kg: Optional[float] = None,
) -> List[str]:
    """Yem seçiminin rasyon kurallarını karşılayıp karşılayamayacağını teşhis eder.

    Besin hedefleri tek tek tutuyor olsa bile seçim yem sınıfı pencerelerini
    doldurmaya yetmiyor olabilir (ör. sadece saman + kuru ot ile %45 kaba yem
    oranına ulaşılamaz). Bu teşhisler kullanıcıya ne ekleyeceğini söyler.
    """
    notes: List[str] = []
    windows = _max_inclusion_windows(feeds)

    by_type: Dict[str, List[FeedIngredient]] = {}
    for feed in feeds:
        by_type.setdefault(feed.feed_type or "unknown", []).append(feed)

    # ---- Hayvan sağlığı sınırları hedeflenen kuru maddeyi karşılıyor mu? ----
    # Sağlık sınırları asla gevşetilmediği için, sınırlı yemlerin toplam KM katkısı
    # hedefin altında kalıyorsa model hiçbir fazda çözüm bulamaz. Kullanıcıya ne kadar
    # açık olduğunu söylemek, kuru bir "Infeasible"dan çok daha kullanışlı.
    target_dm = getattr(constraints, "daily_intake_kg", None) if constraints else None
    if target_dm:
        capped = [f for f in feeds if f.health_cap_kg(body_weight_kg)]
        if capped and len(capped) == len(feeds):
            max_dm = sum(
                f.health_cap_kg(body_weight_kg) * f.dm_fraction() for f in feeds
            )
            if max_dm < target_dm - 1e-6:
                detail = ", ".join(
                    f"{f.name} ≤ {f.health_cap_kg(body_weight_kg):.1f} kg" for f in capped
                )
                notes.append(
                    f"⛔ Seçilen yemlerle sağlık sınırları içinde en fazla {max_dm:.1f} kg "
                    f"kuru madde verilebiliyor, hedef {target_dm:.1f} kg ({detail}). "
                    "Sınırsız yem (kuru ot, tahıl, küspe) ekleyin."
                )

    forage_feeds = [f for f in feeds if f.is_forage()]
    if not forage_feeds:
        notes.append(
            "❌ Hiç kaba yem seçilmemiş. Rasyonun en az "
            f"%{FORAGE_RATIO_MIN * 100:.0f}'i kaba yemden gelmeli — silaj veya kuru ot ekleyin."
        )
        return notes

    # Seçilen kaba yemlerin tavanları toplamı kaba yem oranı alt sınırına yetiyor mu?
    forage_ceiling = sum(windows.get(f.feed_type, 0.20) for f in forage_feeds)
    if forage_ceiling < FORAGE_RATIO_MIN:
        detail = ", ".join(
            f"{f.name} max %{windows.get(f.feed_type, 0.20) * 100:.0f}" for f in forage_feeds
        )
        notes.append(
            f"❌ Seçilen kaba yemlerle %{FORAGE_RATIO_MIN * 100:.0f} kaba yem oranına "
            f"ulaşılamıyor ({detail}). Silaj veya kuru ot ekleyin."
        )

    # NDF penceresi doldurulabiliyor mu?
    if any(f.ndf > 0 for f in feeds):
        max_possible_ndf = sum(windows.get(f.feed_type, 0.20) * f.ndf for f in feeds)
        min_possible_ndf = min(f.ndf for f in feeds)
        if max_possible_ndf < RATION_NDF_MIN:
            notes.append(
                f"❌ Seçilen yemlerle rasyon NDF'si %{RATION_NDF_MIN:.0f}'e ulaşamıyor. "
                "Daha lifli kaba yem (saman, çayır otu, silaj) ekleyin."
            )
        if min_possible_ndf > RATION_NDF_MAX:
            notes.append(
                f"❌ En düşük NDF'li yem bile %{RATION_NDF_MAX:.0f} sınırının üstünde. "
                "Tahıl danesi veya küspe gibi düşük lifli yem ekleyin."
            )

    # Saman ağırlıklı seçim uyarısı
    straw_only = [f for f in forage_feeds if f.feed_type == "straw"]
    if straw_only and len(straw_only) == len(forage_feeds):
        notes.append(
            f"⚠️ Tek kaba yem kaynağı saman. Saman rasyonun en fazla "
            f"%{windows.get('straw', 0.08) * 100:.0f}'ini oluşturabilir (düşük enerji, "
            "yüksek lif). Silaj veya kuru ot ekleyin."
        )

    if not by_type.get("protein") and not by_type.get("hay") and not by_type.get("silage"):
        notes.append(
            "⚠️ Protein kaynağı yok (küspe, DDGS veya yonca). Protein hedefi tutmayabilir."
        )

    return notes


def calculate_simple_ration(
    db,
    animal_weight: float,
    target_protein_kg: Optional[float] = None,
    target_energy_mcal: Optional[float] = None
) -> Dict[str, Any]:
    """
    Eski uyumluluk için basit rasyon hesaplayıcı.
    Hayvan ağırlığına göre NRC benzeri ihtiyaçları tahmin eder.
    """
    from .models import Feed

    feeds = db.query(Feed).all()
    if not feeds:
        return {"status": "No feeds available", "feed_amounts": {}, "total_cost": 0.0}

    feed_ingredients = feeds_from_db(feeds)

    # Basit ihtiyaç formülleri (NRC 2001 yaklaşımı)
    # Süt inekleri için
    if target_protein_kg is None:
        # Yaklaşık: Yaşam payı + Süt verimi
        # 600 kg inek için: ~1.8 kg HP/gün yaşama + süt kg * 0.07
        target_protein_kg = animal_weight * 0.003 + 0.3  # basit katsayı

    if target_energy_mcal is None:
        # NEL Mcal/kg KM ~ 1.6-1.7, tüketim ~ 22 kg KM
        target_energy_mcal = animal_weight * 0.035 + 5.0  # basit katsayı

    constraints = NutrientConstraints(
        crude_protein_min=target_protein_kg,
        metabolic_energy_min=target_energy_mcal,
        dry_matter_min=85.0,
        daily_intake_kg=animal_weight * 0.037  # ~3.7% LW
    )

    # Maksimum tüketim güvenliği
    max_intake = animal_weight * 0.05

    result = solve_ration_problem(
        feeds=feed_ingredients,
        constraints=constraints,
        soft_constraint_penalty=100000.0,
        max_daily_intake_kg=max_intake,
        expected_daily_intake_kg=animal_weight * 0.037
    )

    # Eski format geri döndür
    return {
        "status": result.status,
        "feed_amounts": result.feed_amounts,
        "total_cost": result.total_cost_per_day,
        "nutrient_content": result.nutrient_content,
        "constraints_status": result.constraints_status
    }


def calculate_optimal_ration(
    db,
    animal_weight: float,
    target_protein_kg: Optional[float] = None,
    target_energy_mcal: Optional[float] = None
) -> Dict[str, Any]:
    """
    Backward compatibility wrapper for the old calculate_optimal_ration function.
    Delegates to calculate_simple_ration.
    """
    return calculate_simple_ration(db, animal_weight, target_protein_kg, target_energy_mcal)