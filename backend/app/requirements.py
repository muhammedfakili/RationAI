"""Süt ineği besin ihtiyaçları — NASEM 2021 (Nutrient Requirements of Dairy Cattle, 8. baskı).

Hayvan profilinden solver'ın beklediği KM KONSANTRASYONU hedeflerini üretir. UI
(`/api/v1/requirements`, `/api/v1/optimize` + `animal`) ve ajanın `calculate_ration`
aracı bu tek kaynağı kullanır — hedef sayısını ne arayüz ne LLM kendisi hesaplar.

Kaynaklar:
- Sağmal KMT: NASEM 2021 Eq. 2-1 (de Souza vd., 2019).
- Kuru inek KMT: NASEM 2021, %CA = 1.47 − (0.365 − 0.0028·NDF)·hafta − 0.035·hafta²
  (hafta doğuma kalan süre, negatif; 3 haftadan uzaksa −3). Düve (ilk doğum) × 0.88.
- Süt enerjisi: 9.29·yağ + 5.85·gerçek protein + 3.95·laktoz (Mcal/kg bileşen).
- NEL idame: 0.10·CA^0.75.
- Gebelik NEL/MP: NASEM 2021 tablosu (gün 50–275), CA ile oranlanır.

Protein METABOLİZE PROTEİN (MP) olarak hedeflenir: solver yemlerin RDP/RUP'undan
mikrobiyal protein + by-pass protein arzını hesaplar (optimization.py, NRC 2001).
Ham protein yalnızca üst sınır (azot israfı) ve kuru dönem tabanı olarak kalır.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional

from .optimization import RULES_BY_GROUP, NutrientConstraints

GROUPS = tuple(RULES_BY_GROUP)
GESTATION_LENGTH_D = 280
DEFAULT_DAYS_PREGNANT = {"dry_faroff": 230, "dry_closeup": 265}

NEL_MAINTENANCE_PER_MBW = 0.10     # Mcal NEL / kg CA^0.75
MP_TO_CP = 0.64                    # MP:HP oranı — yalnızca HP üst sınırını kurmak için
MP_MILK_EFFICIENCY = 0.69          # NASEM hedef verimi (süt gerçek proteini)
MFP_G_PER_KG_DMI = 30.0            # metabolik dışkı proteini (NRC 2001 üst yaklaşımı)
HEIFER_DRY_DMI_FACTOR = 0.88

# Gebelik ihtiyacı, NASEM 2021 (gün -> NEL Mcal/gün, MP g/gün), ~700 kg ergin inek.
_GESTATION_TABLE = (
    (0, 0.0, 0.0), (50, 0.04, 3.0), (100, 0.1, 13.0), (150, 0.5, 43.0), (200, 1.4, 125.0),
    (220, 2.0, 185.0), (250, 3.5, 320.0), (275, 5.4, 489.0), (GESTATION_LENGTH_D, 5.8, 523.0),
)
_GESTATION_REFERENCE_BW = 700.0

# ⚠️ Taze inekte gereken NEL yoğunluğu, yenebilir bir rasyonun (kaba yem ≥ %45,
# NDF ≥ %28) ulaşabileceğinin üstüne çıkar. Hedef tavana çekilir ve açık vücut
# rezervinden karşılanır (negatif enerji dengesi) — yoksa her taze inek rasyonu
# "gevşetilmiş" moda düşerdi.
# Tavanlar ölçülerek seçildi: tipik yem seçkisinin HİÇBİR yemi (tuz, DCP dahil) düşürmeden
# katı modda ulaşabildiği en yüksek NEL (sağmal 1.71, close-up 1.56, far-off 1.46).
# Tavana çok yakın hedefte enerjisiz yemler enerjiyi seyrelttiği için tuz rasyondan düşüyordu.
NEL_DENSITY_CAP = {"lactating": 1.70, "dry_faroff": 1.45, "dry_closeup": 1.55}
# Aynı sebeple MP (g MP / kg KM): ölçülen tavan sağmal 114, close-up 98, far-off 88.
# Taze inekte KMT düşükken MP yoğunluğu 130'u aşar.
MP_DENSITY_CAP = {"lactating": 112.0, "dry_faroff": 86.0, "dry_closeup": 96.0}
# HP üst sınırının hesaplandığı tavan (MP ihtiyacının HP karşılığı bunu aşmasın).
CP_DENSITY_CAP = 19.0

# Hedef bantları: alt sınır hesaplanan ihtiyaç, üst sınır = alt + bant.
# HP bandı geniş: kaba yem ve NDF kuralları yonca/soyayı sokunca protein doğal olarak artar.
_BANDS = {"lactating": (4.0, 0.25), "dry_faroff": (3.0, 0.15), "dry_closeup": (3.0, 0.15)}
# Kuru dönemde HP tabanı (NASEM 2021: far-off ~%12, close-up ~%13 HP önerilir).
_DRY_CP_FLOOR = {"dry_faroff": 12.0, "dry_closeup": 13.0}
# Ca / P, g/kg KM. Kuru dönemde düşük Ca: doğuma yakın süt hummasını önlemeye yardım eder.
_MINERALS = {
    "lactating": (6.0, 12.0, 3.5, 7.0),
    "dry_faroff": (4.0, 7.0, 2.5, 4.0),
    "dry_closeup": (4.0, 7.0, 2.5, 4.0),
}


@dataclass
class AnimalProfile:
    group: str = "lactating"
    body_weight_kg: float = 650.0
    milk_kg: float = 30.0
    fat_pct: float = 3.8
    true_protein_pct: float = 3.2
    lactose_pct: float = 4.85
    days_in_milk: int = 120
    multiparous: bool = True
    bcs: float = 3.0
    days_pregnant: int = 0


@dataclass
class Requirements:
    group: str
    dm_intake_kg: float
    nel_mcal_day: float            # toplam ihtiyaç (tavandan önce)
    nel_min: float                 # Mcal/kg KM
    nel_max: float
    mp_g_day: float                # toplam ihtiyaç (tavandan önce)
    mp_min: float                  # g MP / kg KM
    cp_kg_day: float
    cp_min: Optional[float]        # % KM — yalnızca kuru dönem tabanı; sağmalda MP yeter
    cp_max: float
    ca_min: float                  # g/kg KM
    ca_max: float
    p_min: float
    p_max: float
    energy_deficit_mcal: float = 0.0
    notes: List[str] = field(default_factory=list)


def _gestation(days_pregnant: int, body_weight_kg: float) -> tuple:
    """Gebelik için (NEL Mcal/gün, MP g/gün); tablo doğrusal ara değerlenir."""
    day = min(max(days_pregnant, 0), GESTATION_LENGTH_D)
    for (d0, e0, m0), (d1, e1, m1) in zip(_GESTATION_TABLE, _GESTATION_TABLE[1:]):
        if day <= d1:
            t = (day - d0) / (d1 - d0)
            scale = body_weight_kg / _GESTATION_REFERENCE_BW
            return (e0 + t * (e1 - e0)) * scale, (m0 + t * (m1 - m0)) * scale
    return 0.0, 0.0


def milk_energy_mcal_per_kg(profile: AnimalProfile) -> float:
    return (0.0929 * profile.fat_pct + 0.0585 * profile.true_protein_pct
            + 0.0395 * profile.lactose_pct)


def _lactating_dmi(profile: AnimalProfile, milk_ne: float) -> float:
    parity = 1.0 if profile.multiparous else 0.0
    base = ((3.7 + parity * 5.7) + 0.305 * milk_ne + 0.022 * profile.body_weight_kg
            + (-0.689 - 1.87 * parity) * profile.bcs)
    return base * (1 - (0.212 + parity * 0.136) * math.exp(-0.053 * profile.days_in_milk))


def _dry_dmi(profile: AnimalProfile, days_pregnant: int) -> float:
    rules = RULES_BY_GROUP[profile.group]
    ndf = min(max((rules.ndf_min + rules.ndf_max) / 2, 30.0), 55.0)
    week = max(-(GESTATION_LENGTH_D - days_pregnant) / 7.0, -3.0)
    pct_bw = 1.47 - (0.365 - 0.0028 * ndf) * week - 0.035 * week ** 2
    if not profile.multiparous:
        pct_bw *= HEIFER_DRY_DMI_FACTOR
    return pct_bw / 100.0 * profile.body_weight_kg


def compute_requirements(profile: AnimalProfile) -> Requirements:
    """Hayvan profilinden günlük ihtiyacı ve KM konsantrasyonu hedeflerini hesaplar."""
    if profile.group not in RULES_BY_GROUP:
        raise ValueError(f"Bilinmeyen hayvan grubu: {profile.group} ({', '.join(GROUPS)})")
    lactating = profile.group == "lactating"
    bw = profile.body_weight_kg
    notes: List[str] = []

    days_pregnant = profile.days_pregnant or DEFAULT_DAYS_PREGNANT.get(profile.group, 0)
    gest_nel, gest_mp = _gestation(days_pregnant, bw)

    if lactating:
        milk_ne = profile.milk_kg * milk_energy_mcal_per_kg(profile)
        dmi = _lactating_dmi(profile, milk_ne)
        milk_mp = profile.milk_kg * profile.true_protein_pct * 10.0 / MP_MILK_EFFICIENCY
    else:
        milk_ne, milk_mp = 0.0, 0.0
        dmi = _dry_dmi(profile, days_pregnant)
        notes.append(
            f"Gebeliğin {days_pregnant}. günü, doğuma ~{GESTATION_LENGTH_D - days_pregnant} gün."
        )

    nel_total = NEL_MAINTENANCE_PER_MBW * bw ** 0.75 + milk_ne + gest_nel
    mp_total = ((4.1 * bw ** 0.5 + 0.3 * bw ** 0.6) / 0.67 + MFP_G_PER_KG_DMI * dmi
                + milk_mp + gest_mp)
    cp_kg = mp_total / MP_TO_CP / 1000.0

    nel_density = nel_total / dmi
    nel_cap = NEL_DENSITY_CAP[profile.group]
    deficit = 0.0
    if nel_density > nel_cap:
        deficit = nel_total - nel_cap * dmi
        nel_density = nel_cap
        notes.append(
            f"Negatif enerji dengesi: ihtiyacın günde ~{deficit:.1f} Mcal NEL'i vücut "
            "rezervinden karşılanıyor ("
            + ("laktasyon başında normaldir" if lactating else "kuru dönem rasyonu daha yoğun olamaz")
            + "; BCS'yi izleyin)."
        )

    mp_density = mp_total / dmi
    mp_cap = MP_DENSITY_CAP[profile.group]
    if mp_density > mp_cap:
        notes.append(
            f"Protein açığı: günde ~{mp_total - mp_cap * dmi:.0f} g MP vücut proteininden "
            "karşılanıyor (düşük tüketim dönemi)."
        )
        mp_density = mp_cap

    # HP üst sınırı MP ihtiyacının HP karşılığından kurulur; alt sınır yalnızca kuru dönemde.
    cp_ref = min(cp_kg / dmi * 100.0, CP_DENSITY_CAP)
    floor = _DRY_CP_FLOOR.get(profile.group)
    if floor:
        cp_ref = max(cp_ref, floor)
    cp_band, nel_band = _BANDS[profile.group]
    ca_min, ca_max, p_min, p_max = _MINERALS[profile.group]

    return Requirements(
        group=profile.group,
        dm_intake_kg=round(dmi, 2),
        nel_mcal_day=round(nel_total, 2),
        nel_min=round(nel_density, 3),
        nel_max=round(nel_density + nel_band, 3),
        mp_g_day=round(mp_total),
        mp_min=round(mp_density, 1),
        cp_kg_day=round(cp_kg, 3),
        cp_min=floor,
        cp_max=round(cp_ref + cp_band, 2),
        ca_min=ca_min, ca_max=ca_max, p_min=p_min, p_max=p_max,
        energy_deficit_mcal=round(deficit, 2),
        notes=notes,
    )


def requirements_to_constraints(req: Requirements) -> NutrientConstraints:
    """İhtiyacı solver kısıtlarına çevirir (enerji NEL, protein MP üzerinden)."""
    return NutrientConstraints(
        metabolizable_protein_min=req.mp_min,
        crude_protein_min=req.cp_min,
        crude_protein_max=req.cp_max,
        net_energy_lactation_min=req.nel_min,
        net_energy_lactation_max=req.nel_max,
        calcium_min=req.ca_min,
        calcium_max=req.ca_max,
        phosphorus_min=req.p_min,
        phosphorus_max=req.p_max,
        daily_intake_kg=req.dm_intake_kg,
    )
