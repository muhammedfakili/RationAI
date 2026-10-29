"""Süt ineği besin ihtiyaçları — kullanıcının zootekni notları (ME + HP sistemi).

Hayvan profilinden solver'ın beklediği KM KONSANTRASYONU hedeflerini üretir. UI
(`/api/v1/requirements`, `/api/v1/optimize` + `animal`) ve ajanın `calculate_ration`
aracı bu tek kaynağı kullanır — hedef sayısını ne arayüz ne LLM kendisi hesaplar.

Formüller ve gerekçeleri: data/notlar/rasyon_notlari.md §2. Kısaltmalar: CA canlı
ağırlık (kg), SV süt verimi (kg/gün), SY süt yağı (%), DSV %4 yağa düzeltilmiş süt.

- KMT: SV < 24 kg → CA×0.025 + SV×0.10; SV ≥ 24 → CA×0.022 + SV×0.20. Kuru inek: NASEM
  2021 kuru inek denklemi (~%1.85 CA) — notlardaki %2.5 enerji fazlası yarattı (bkz. _dm_intake).
- ME (Mcal): yaşama 0.133·CA^0.75 (en az 10), süt (0.577 + 0.165·SY)·SV,
  gebelik yaşama payının %30'u — gebeliğin 190. gününden itibaren (NRC 2001).
- HP (g): yaşama 70.7 + 2.77·CA^0.75 (en az 350), süt (43.6 + 11.5·SY)·SV.
- Ca (g): 0.0154·CA/0.38 + 1.22·DSV/0.38; P (g): 0.0143·CA/0.5 + 0.99·DSV/0.5.
- Su (L): 12.3 + 2.15·KMT + 0.73·SV.

Sağmalda NASEM 2021 KMT denklemi yalnızca karşılaştırma notu olarak hesaplanır.
"""

import math
from dataclasses import dataclass, field, replace
from typing import List, Optional

from .optimization import RULES_BY_GROUP, NutrientConstraints

GROUPS = tuple(RULES_BY_GROUP)
GESTATION_LENGTH_D = 280
DEFAULT_DAYS_PREGNANT = {"dry_faroff": 230, "dry_closeup": 265}

HIGH_YIELD_MILK_KG = 24.0          # bu verim ve üstü "yüksek verim" KMT formülü
ME_MAINTENANCE_PER_MBW = 0.133     # Mcal ME / kg CA^0.75
ME_MAINTENANCE_FLOOR = 10.0        # Mcal — yaşama payı enerji 10 000 kcal'ın altına inmez
GESTATION_ME_SHARE = 0.30          # yaşama payının %30'u
GESTATION_START_DAY = 190          # NRC 2001: gebelik ihtiyacı bu günden sonra anlamlı
CP_MAINTENANCE_FLOOR_G = 350.0     # yaşama payı HP 350 g'ın altına inmez

# ⚠️ Taze ve çok yüksek verimli inekte gereken ME/HP yoğunluğu, yenebilir bir rasyonun
# (kaba yem ≥ %45, NDF ≥ %28) ulaşabileceğinin üstüne çıkar. Hedef tavana çekilir ve açık
# vücut rezervinden karşılanır — yoksa bu inekler hep "gevşetilmiş" moda düşerdi.
# Tavanlar ölçülerek seçildi: tipik yem seçkisinin HİÇBİR yemi (tuz, DCP dahil) düşürmeden
# katı modda ulaşabildiği en yüksek yoğunluk.
# Ölçüm (Ekim 2026, 650 kg): sağmal ME 2.90 / HP 23, close-up 2.60 / 17, far-off 2.55 / 16.
# Besi (400 kg, %30 kaba): silajlı seçimde ME 3.0-3.1, kuru otlu 2.8; HP 6:1'siz ATK 36 ile 16.
# Kuzu (30 ve 55 kg, %30 kaba): yonca + saman 2.85, silaj + yonca 2.90, çayır otu + saman 2.75.
ME_DENSITY_CAP = {"lactating": 2.90, "dry_faroff": 2.55, "dry_closeup": 2.60, "beef": 2.95,
                  "lamb": 2.85}
CP_DENSITY_CAP = {"lactating": 21.0, "dry_faroff": 15.0, "dry_closeup": 16.0, "beef": 16.0,
                  "lamb": 18.0}

# Hedef bantları: alt sınır hesaplanan ihtiyaç, üst sınır = alt + bant (HP % KM, ME Mcal/kg KM).
_BANDS = {"lactating": (4.0, 0.25), "dry_faroff": (3.0, 0.15), "dry_closeup": (3.0, 0.15)}
# Sağmalda HP üst sınırı en az bu kadar: düşük verimde ihtiyaç ~%9, +4 bantla tavan %13 olur;
# ama tablodaki mısır silajı (%11.7 HP KM) ve nişasta/NDF kurallarıyla yenebilir rasyon
# %13'ün altına inemiyor (700 kg, 15 L inek çözümsüz kalıyordu). Alt sınır formülde kalır.
_CP_MAX_FLOOR = {"lactating": 16.0}
# Kuru dönemde HP tabanı: notlardaki formül kuru inekte yalnızca yaşama payını verir
# (~%3 KM) — fetüs ve kolostrum için NASEM 2021'in far-off ~%12 / close-up ~%13'ü korunur.
_DRY_CP_FLOOR = {"dry_faroff": 12.0, "dry_closeup": 13.0}
# Ca / P üst sınırları, g/kg KM (alt sınır ihtiyaçtan gelir). Besi: Merck'in Ca %0.4-1.0,
# P %0.2-0.4 KM aralığı İHTİYAÇ aralığıdır, tavan değil — P'yi 4 g/kg'da kesmek ATK'lı genç
# dana rasyonunu çözümsüz bırakıyordu (ATK 11 g P/kg KM). Sağmal tavanları kullanılır.
_MINERAL_MAX = {"lactating": (12.0, 7.0), "dry_faroff": (7.0, 4.0), "dry_closeup": (7.0, 4.0),
                "beef": (12.0, 7.0), "lamb": (12.0, 6.0)}

# ---- Besi sığırı (data/notlar/rasyon_notlari.md §5) --------------------------------
# KMT: NASEM (2016) büyüyen-besi sığırı denklemi (NRC 1996 yıllık dana katsayıları):
#   KMT = SBW^0.75 · (0.2435·NEm − 0.0466·NEm² − 0.0869) / NEm
# NEm rasyonun enerji yoğunluğundan (NRC: NEm = 1.37·ME − 0.138·ME² + 0.0105·ME³ − 1.12).
# Notlardaki sabit %2.5 CA 400 kg'da denklemle aynı çıkıyor, ama 200 kg'da az (%3.0),
# 600 kg'da fazla (%2.25) — ağır danaya gereksiz enerji fazlası gösteriyordu.
# Tüketim rasyonun yoğunluğuna bağlı olduğu için yoğunluk, "KMT × yoğunluk = ihtiyaç"
# olacak şekilde çözülür (_beef_dmi).
BEEF_DMI_COEF = (0.2435, 0.0466, 0.0869)
BEEF_ME_SEARCH_MIN = 1.8           # Mcal/kg KM; bunun altı yenebilir besi rasyonu değil
# Artış enerjisi: kullanıcının "7000 kcal ≈ 1 kg" kuralı referans noktaya (sığır 300 kg ve
# 1 kg/gün, kuzu 30 kg ve 0.3 kg/gün) sabitlenir ve NRC'nin tutulan enerji denkleminin
# (RE = 0.0635 · EQEBW^0.75 · EBG^1.097; NRC 1996/2000, NASEM 2016) üsleriyle ölçeklenir:
# ağır hayvanın kilosu daha çok yağ, daha çok enerji ister. NRC'nin kendisi 300 kg'da
# ~7.9 Mcal ME/kg verir (kg ≈ 0.43) — kural %10 içinde; 600 kg'da ~12 Mcal/kg'a çıkar.
# Sabit 7000 kcal ağır danaya "günde 2 kg fazla" diyordu. Kuzuda NRC 30 kg / 0.3 kg'da
# 7.6 (1985) - 7.9 (2007) Mcal/kg veriyor — aynı %10 fark.
ME_PER_KG_GAIN = 7.0               # Mcal, referans noktada
RE_WEIGHT_EXP, RE_GAIN_EXP = 0.75, 1.097
# Tür → (yaşama payı ME Mcal/CA^0.75, referans CA kg, referans artış kg/gün).
# Koyun yaşama payı: NRC NEm 56 kcal/CA^0.75 ÷ km 0.60 (sığırdaki 0.133 de aynı mantık).
_GROWTH = {"beef": (ME_MAINTENANCE_PER_MBW, 300.0, 1.0), "lamb": (0.093, 30.0, 0.3)}
GROWTH_GROUPS = frozenset(_GROWTH)
# Dane:küspe 6:1 yalnız besi sonunda: hafif danada protein ihtiyacı (%14-18 HP) 6:1'le
# karşılanamıyor (kesif ~%13-14 HP'ye sıkışıyor). Kullanıcı kararı, 350 kg.
GRAIN_TO_MEAL_FROM_BW = 350.0
BEEF_CP_BAND, BEEF_ME_BAND = 3.0, 0.25
# Besi başında kaba:kesif 50:50, sonra 30:70 (kullanıcı notu). Geçiş: en az 14 gün, pratikte
# ~3 hafta, kesif 4-5 günde bir artırılır (Oklahoma State 2010; T&F 2023 derleme; Türk
# kaynakları). (son gün, kaba yem tabanı) — 20 günde 5 adım.
BEEF_FORAGE_STEPS = ((5, 0.50), (10, 0.45), (15, 0.40), (20, 0.35))
BEEF_FORAGE_FINAL = 0.30
# NASEM (2016) büyüyen-besi sığırı ham protein ihtiyacı, kg/gün (Merck Veterinary Manual
# tablosu; ergin ağırlık 635 kg, MP = 0.64 × HP). Satır: canlı ağırlık (kg), sütun: GCAA.
_NRC_CP_WEIGHTS = (91, 181, 272, 363, 454, 544, 635)
_NRC_CP_GAINS = (0.0, 0.45, 0.91, 1.36, 1.81)
_NRC_CP_KG = (
    (0.17, 0.38, 0.58, 0.78, 0.98),
    (0.29, 0.49, 0.69, 0.89, 1.08),
    (0.38, 0.59, 0.79, 0.99, 1.17),
    (0.48, 0.69, 0.89, 1.08, 1.27),
    (0.57, 0.79, 0.99, 1.18, 1.37),
    (0.65, 0.89, 1.10, 1.29, 1.47),
    (0.73, 1.00, 1.22, 1.41, 1.60),
)
# NRC Ca/P (1996-2016 aynı): bakım Ca 15.4, P 16 mg/kg boş canlı ağırlık; artış Ca 7.1,
# P 3.9 g / 100 g net protein; gerçek emilim Ca %50, P %68. Net protein ≈ 150 g/kg artış.
# Arkansas MP391 (NASEM 2016) tablosuyla iki noktada %0-5 farkla doğrulandı.
_SHRINK = 0.96
_NET_PROTEIN_G_PER_KG_GAIN = 150.0

# ---- Kuzu / toklu besisi (data/notlar/rasyon_notlari.md §6) --------------------------
# NRC (1985) koyun Tablo 1-2, "erken sütten kesilmiş kuzu" (20 kg) ve "besi kuzusu, 4-7
# ay" (30-50 kg) satırları; 60 kg "hızlı büyüyen" satırı. Notlardaki sabit %3.5 yerine KM
# ağırlıkla iner (20 kg %5 → 60 kg %2.8): hafif kuzu ağırlığına göre çok daha fazla yer.
# NRC 2007 kontrol noktası: 30 kg / 300 g → %4.15 KM, %12.4 HP, 8 g Ca, 4 g P (Beef+Lamb NZ).
_LAMB_WEIGHTS = (20.0, 30.0, 40.0, 50.0, 60.0)
_LAMB_DMI_PCT = (5.0, 4.3, 4.0, 3.2, 2.8)
_LAMB_CP_PCT = (16.9, 14.7, 11.6, 10.0, 10.0)   # % KM
_LAMB_CA = (5.4, 5.1, 4.2, 3.5, 3.5)            # g/kg KM
_LAMB_P = (2.4, 2.4, 2.1, 1.9, 1.9)
LAMB_CP_BAND, LAMB_ME_BAND = 3.0, 0.25
# HP üst sınırı en az bu: kesif yem ≥ %16 HP (doğal) kuralıyla yonca kaba yemli rasyon
# %18'e çıkıyor; 50 kg'ın %10 + 3 tavanı bu kuralla çelişirdi.
LAMB_CP_MAX_FLOOR = 20.0
LAMB_WATER_PER_KG_DM = 2.5         # NRC: koyunda su KM tüketiminin 2-3 katı


@dataclass
class AnimalProfile:
    group: str = "lactating"
    body_weight_kg: float = 650.0
    milk_kg: float = 30.0
    fat_pct: float = 3.8
    # Aşağıdakiler yalnızca NASEM karşılaştırma notunda kullanılır.
    true_protein_pct: float = 3.2
    lactose_pct: float = 4.85
    days_in_milk: int = 120
    multiparous: bool = True       # kuru inek KMT'sini de etkiler (düve × 0.88)
    bcs: float = 3.0
    days_pregnant: int = 0
    # Besi sığırı
    daily_gain_kg: float = 1.4
    days_on_feed: int = 60


@dataclass
class Requirements:
    group: str
    dm_intake_kg: float
    me_mcal_day: float             # toplam ihtiyaç (tavandan önce)
    me_min: float                  # Mcal/kg KM
    me_max: float
    cp_g_day: float                # toplam ihtiyaç (tavandan önce)
    cp_min: float                  # % KM
    cp_max: float
    ca_g_day: float
    p_g_day: float
    ca_min: float                  # g/kg KM
    ca_max: float
    p_min: float
    p_max: float
    water_l_day: float
    energy_deficit_mcal: float = 0.0
    nasem_dmi_kg: Optional[float] = None
    achievable_gain_kg: Optional[float] = None    # besi: KM'nin izin verdiği artış
    notes: List[str] = field(default_factory=list)


def rules_for(profile: AnimalProfile):
    """Grubun rasyon kuralları; besi gruplarında kaba yem tabanı besi gününe göre kademeli."""
    rules = RULES_BY_GROUP[profile.group]
    if profile.group not in GROWTH_GROUPS:
        return rules
    floor = next((share for last_day, share in BEEF_FORAGE_STEPS if profile.days_on_feed <= last_day),
                 BEEF_FORAGE_FINAL)
    grain_to_meal = None
    if rules.grain_to_meal_min and profile.body_weight_kg >= GRAIN_TO_MEAL_FROM_BW:
        grain_to_meal = rules.grain_to_meal_min
    return replace(rules, forage_ratio_min=floor, grain_to_meal_min=grain_to_meal)


def gain_me_mcal(group: str, body_weight_kg: float, gain_kg: float) -> float:
    """Günlük artış için gereken ME (Mcal/gün); referans noktada 7 Mcal/kg."""
    if gain_kg <= 0:
        return 0.0
    _, ref_bw, ref_gain = _GROWTH[group]
    return (ME_PER_KG_GAIN * ref_gain * (body_weight_kg / ref_bw) ** RE_WEIGHT_EXP
            * (gain_kg / ref_gain) ** RE_GAIN_EXP)


def gain_from_me(group: str, body_weight_kg: float, me_for_gain_mcal: float) -> float:
    """gain_me_mcal'ın tersi: artışa kalan ME'nin sağladığı günlük artış (kg)."""
    if me_for_gain_mcal <= 0:
        return 0.0
    _, ref_bw, ref_gain = _GROWTH[group]
    scale = ME_PER_KG_GAIN * ref_gain * (body_weight_kg / ref_bw) ** RE_WEIGHT_EXP
    return ref_gain * (me_for_gain_mcal / scale) ** (1.0 / RE_GAIN_EXP)


def maintenance_me(group: str, body_weight_kg: float) -> float:
    return _GROWTH[group][0] * body_weight_kg ** 0.75


def annotate_growth_result(result, profile: AnimalProfile) -> None:
    """Besi sonucunda "enerji fazlası" uyarısını "beklenen artış" bilgisine çevirir.

    Besi hayvanında fazla enerji kilo olarak geri döner; asıl soru rasyonun kaç kg/gün
    büyüttüğüdür. Sonucun günlük ME alımından yaşama payı düşülür, kalan artışa çevrilir.
    """
    me_supply = result.daily_nutrient_intake.get("metabolic_energy_mcal") or 0.0
    if me_supply <= 0:
        return
    group, bw = profile.group, profile.body_weight_kg
    expected = gain_from_me(group, bw, me_supply - maintenance_me(group, bw))
    if group == "lamb":   # kuzu artışı gram ile konuşulur
        text = f"~{expected * 1000:.0f} g/gün (hedef {profile.daily_gain_kg * 1000:.0f} g/gün)"
    else:
        text = f"~{expected:.2f} kg/gün (hedef {profile.daily_gain_kg:g} kg/gün)"
    result.warnings = [w for w in result.warnings if not w.startswith("Enerji fazlası")]
    result.warnings.insert(0, (
        f"Beklenen artış: bu rasyonla {text}. Enerji, rasyon notlarındaki ME formülüyle hesaplandı."
    ))
    result.solver_details["expected_gain_kg"] = round(expected, 3)


def _interpolate(xs, x):
    """Sıralı xs içinde x'in (alt indeks, ağırlık) çifti; uçlarda sabitlenir."""
    x = min(max(x, xs[0]), xs[-1])
    for i in range(len(xs) - 1):
        if x <= xs[i + 1]:
            return i, (x - xs[i]) / (xs[i + 1] - xs[i])
    return len(xs) - 2, 1.0


def nrc_beef_cp_kg(body_weight_kg: float, gain_kg: float) -> float:
    """NASEM 2016 tablosundan çift doğrusal ara değerle günlük HP ihtiyacı (kg)."""
    i, wi = _interpolate(_NRC_CP_WEIGHTS, body_weight_kg)
    j, wj = _interpolate(_NRC_CP_GAINS, gain_kg)
    t = _NRC_CP_KG
    low = t[i][j] + wj * (t[i][j + 1] - t[i][j])
    high = t[i + 1][j] + wj * (t[i + 1][j + 1] - t[i + 1][j])
    return low + wi * (high - low)


def _nem_from_me(me: float) -> float:
    """NRC 1996: rasyonun ME'sinden (Mcal/kg KM) NEm'i."""
    return 1.37 * me - 0.138 * me ** 2 + 0.0105 * me ** 3 - 1.12


def nasem_beef_dmi(body_weight_kg: float, me_density: float) -> float:
    """NASEM 2016 büyüyen-besi sığırı KMT'si (kg/gün), verilen rasyon ME yoğunluğunda."""
    a, b, c = BEEF_DMI_COEF
    nem = _nem_from_me(me_density)
    return (_SHRINK * body_weight_kg) ** 0.75 * (a * nem - b * nem ** 2 - c) / nem


def _beef_dmi(body_weight_kg: float, me_total: float, me_cap: float):
    """KMT ve ME yoğunluğu: KMT(yoğunluk) × yoğunluk = ihtiyaç olacak yoğunluk (ikiye bölme).

    Bu aralıkta KMT × yoğunluk yoğunlukla artar. İhtiyaç tavanın üstündeyse yoğunluk
    tavanda kalır (artış hedefi o tüketimle karşılanamaz); en seyrek rasyon bile fazla
    veriyorsa KMT o yoğunlukta alınır ve hedef ihtiyaç/KMT olur.
    """
    def supply(d):
        return nasem_beef_dmi(body_weight_kg, d) * d

    if supply(me_cap) < me_total:
        return nasem_beef_dmi(body_weight_kg, me_cap), me_cap
    if supply(BEEF_ME_SEARCH_MIN) >= me_total:
        dmi = nasem_beef_dmi(body_weight_kg, BEEF_ME_SEARCH_MIN)
        return dmi, me_total / dmi
    low, high = BEEF_ME_SEARCH_MIN, me_cap
    for _ in range(40):
        mid = (low + high) / 2
        low, high = (mid, high) if supply(mid) < me_total else (low, mid)
    return nasem_beef_dmi(body_weight_kg, high), high


def _beef_requirements(profile: AnimalProfile) -> Requirements:
    """Besi sığırı: KMT NASEM 2016, ME = yaşama + artış (7 Mcal/kg ölçekli), HP ve Ca/P NRC."""
    bw, gain = profile.body_weight_kg, profile.daily_gain_kg
    notes: List[str] = []
    me_maint = maintenance_me("beef", bw)
    me_total = me_maint + gain_me_mcal("beef", bw, gain)
    me_cap = ME_DENSITY_CAP["beef"]
    dmi, me_density = _beef_dmi(bw, me_total, me_cap)
    cp_g = nrc_beef_cp_kg(bw, gain) * 1000.0
    net_protein = _NET_PROTEIN_G_PER_KG_GAIN * gain
    sbw = _SHRINK * bw
    ca_g = (0.0154 * sbw + 0.071 * net_protein) / 0.50
    p_g = (0.016 * sbw + 0.039 * net_protein) / 0.68

    notes.append(f"Kuru madde NASEM 2016 besi denklemiyle: CA'nın %{dmi / bw * 100:.2f}'i "
                 "(rasyonun enerji yoğunluğuna göre).")
    deficit, achievable = 0.0, None
    if me_density * dmi < me_total - 1e-6:
        deficit = me_total - me_density * dmi
        achievable = gain_from_me("beef", bw, me_density * dmi - me_maint)
        notes.append(
            f"Hedef {gain:.2f} kg/gün artış için gereken enerji yenebilir bir rasyona sığmıyor: "
            f"en yoğun rasyonla ulaşılabilir artış ~{achievable:.2f} kg/gün."
        )
    cp_density = cp_g / 10.0 / dmi
    if cp_density > CP_DENSITY_CAP["beef"]:
        cp_density = CP_DENSITY_CAP["beef"]
        notes.append(
            f"Protein ihtiyacı (%{cp_g / 10.0 / dmi:.1f} KM) yenebilir rasyonun üstünde; hedef "
            f"%{cp_density:.0f}'ya çekildi — genç danada yüksek proteinli küspe (ATK 36) kullanın."
        )
    floor = rules_for(profile).forage_ratio_min
    notes.append(f"Besinin {profile.days_on_feed}. günü: kaba yem en az %{floor * 100:.0f} "
                 "(besi başı %50'den 20 günde %30'a iner).")
    ca_cap, p_cap = _MINERAL_MAX["beef"]
    ca_min, p_min = ca_g / dmi, p_g / dmi
    return Requirements(
        group="beef",
        dm_intake_kg=round(dmi, 2),
        me_mcal_day=round(me_total, 2),
        me_min=round(me_density, 3),
        me_max=round(me_density + BEEF_ME_BAND, 3),
        cp_g_day=round(cp_g),
        cp_min=round(cp_density, 2),
        cp_max=round(cp_density + BEEF_CP_BAND, 2),
        ca_g_day=round(ca_g, 1),
        p_g_day=round(p_g, 1),
        ca_min=round(ca_min, 2),
        ca_max=max(ca_cap, round(ca_min + 2.0, 2)),
        p_min=round(p_min, 2),
        p_max=max(p_cap, round(p_min + 1.0, 2)),
        water_l_day=round(12.3 + 2.15 * dmi, 1),
        energy_deficit_mcal=round(deficit, 2),
        achievable_gain_kg=None if achievable is None else round(achievable, 2),
        notes=notes,
    )


def _lamb_table(values, body_weight_kg: float) -> float:
    i, w = _interpolate(_LAMB_WEIGHTS, body_weight_kg)
    return values[i] + w * (values[i + 1] - values[i])


def _lamb_requirements(profile: AnimalProfile) -> Requirements:
    """Kuzu/toklu besisi: KMT, HP, Ca, P NRC 1985 tablosundan; ME = yaşama + artış."""
    bw, gain = profile.body_weight_kg, profile.daily_gain_kg
    notes: List[str] = []
    dmi = _lamb_table(_LAMB_DMI_PCT, bw) / 100.0 * bw
    me_maint = maintenance_me("lamb", bw)
    me_total = me_maint + gain_me_mcal("lamb", bw, gain)
    notes.append(f"Kuru madde NRC tablosundan: CA'nın %{dmi / bw * 100:.1f}'i "
                 "(kuzu ağırlaştıkça oran düşer).")

    me_density = me_total / dmi
    me_cap = ME_DENSITY_CAP["lamb"]
    deficit, achievable = 0.0, None
    if me_density > me_cap:
        deficit = me_total - me_cap * dmi
        achievable = gain_from_me("lamb", bw, me_cap * dmi - me_maint)
        me_density = me_cap
        notes.append(
            f"Hedef {gain * 1000:.0f} g/gün artış bu kuru maddeyle mümkün değil: yenebilir en "
            f"yoğun rasyonla ulaşılabilir artış ~{achievable * 1000:.0f} g/gün."
        )
    cp_density = min(_lamb_table(_LAMB_CP_PCT, bw), CP_DENSITY_CAP["lamb"])
    ca_min, p_min = _lamb_table(_LAMB_CA, bw), _lamb_table(_LAMB_P, bw)
    floor = rules_for(profile).forage_ratio_min
    notes.append(f"Besinin {profile.days_on_feed}. günü: kaba yem en az %{floor * 100:.0f} "
                 "(besi başı %50'den 20 günde %30'a iner). Kesif yem en az %16 HP (doğal).")
    ca_cap, p_cap = _MINERAL_MAX["lamb"]
    return Requirements(
        group="lamb",
        dm_intake_kg=round(dmi, 3),
        me_mcal_day=round(me_total, 3),
        me_min=round(me_density, 3),
        me_max=round(me_density + LAMB_ME_BAND, 3),
        cp_g_day=round(cp_density * dmi * 10.0),
        cp_min=round(cp_density, 2),
        cp_max=round(max(cp_density + LAMB_CP_BAND, LAMB_CP_MAX_FLOOR), 2),
        ca_g_day=round(ca_min * dmi, 1),
        p_g_day=round(p_min * dmi, 1),
        ca_min=round(ca_min, 2),
        ca_max=ca_cap,
        p_min=round(p_min, 2),
        p_max=p_cap,
        water_l_day=round(LAMB_WATER_PER_KG_DM * dmi, 1),
        energy_deficit_mcal=round(deficit, 3),
        achievable_gain_kg=None if achievable is None else round(achievable, 3),
        notes=notes,
    )


def corrected_milk_kg(profile: AnimalProfile) -> float:
    """%4 yağa düzeltilmiş süt (DSV)."""
    return (profile.fat_pct * 0.15 + 0.4) * profile.milk_kg


def _dm_intake(profile: AnimalProfile, days_pregnant: int) -> float:
    bw, milk = profile.body_weight_kg, profile.milk_kg
    if profile.group != "lactating":
        # ⚠️ Notlardaki %2.5 CA kuru inekte 650 kg için 16.25 kg eder: tablodaki enerji
        # değerleriyle en düşük enerjili yenebilir rasyon bile günde ~12 700 kcal fazla
        # veriyordu (~1.8 kg/gün yağlanma). Kullanıcı kararıyla NASEM denklemi (~%1.85 CA).
        return _nasem_dmi(profile, days_pregnant)
    if milk >= HIGH_YIELD_MILK_KG:
        return bw * 0.022 + milk * 0.20
    return bw * 0.025 + milk * 0.10


def _nasem_dmi(profile: AnimalProfile, days_pregnant: int) -> float:
    """NASEM 2021 KMT tahmini — yalnızca karşılaştırma için."""
    bw = profile.body_weight_kg
    if profile.group == "lactating":
        milk_ne = profile.milk_kg * (0.0929 * profile.fat_pct + 0.0585 * profile.true_protein_pct
                                     + 0.0395 * profile.lactose_pct)
        parity = 1.0 if profile.multiparous else 0.0
        base = ((3.7 + parity * 5.7) + 0.305 * milk_ne + 0.022 * bw
                + (-0.689 - 1.87 * parity) * profile.bcs)
        return base * (1 - (0.212 + parity * 0.136) * math.exp(-0.053 * profile.days_in_milk))
    rules = RULES_BY_GROUP[profile.group]
    ndf = min(max((rules.ndf_min + rules.ndf_max) / 2, 30.0), 55.0)
    week = max(-(GESTATION_LENGTH_D - days_pregnant) / 7.0, -3.0)
    pct_bw = 1.47 - (0.365 - 0.0028 * ndf) * week - 0.035 * week ** 2
    if not profile.multiparous:
        pct_bw *= 0.88
    return pct_bw / 100.0 * bw


def compute_requirements(profile: AnimalProfile) -> Requirements:
    """Hayvan profilinden günlük ihtiyacı ve KM konsantrasyonu hedeflerini hesaplar."""
    if profile.group not in RULES_BY_GROUP:
        raise ValueError(f"Bilinmeyen hayvan grubu: {profile.group} ({', '.join(GROUPS)})")
    if profile.group == "beef":
        return _beef_requirements(profile)
    if profile.group == "lamb":
        return _lamb_requirements(profile)
    lactating = profile.group == "lactating"
    bw = profile.body_weight_kg
    milk = profile.milk_kg if lactating else 0.0
    mbw = bw ** 0.75
    notes: List[str] = []

    days_pregnant = profile.days_pregnant or DEFAULT_DAYS_PREGNANT.get(profile.group, 0)
    if not lactating:
        notes.append(
            f"Gebeliğin {days_pregnant}. günü, doğuma ~{GESTATION_LENGTH_D - days_pregnant} gün."
        )

    dmi = _dm_intake(profile, days_pregnant)

    me_maint = max(ME_MAINTENANCE_PER_MBW * mbw, ME_MAINTENANCE_FLOOR)
    me_gest = GESTATION_ME_SHARE * me_maint if days_pregnant >= GESTATION_START_DAY else 0.0
    me_milk = (0.577 + 0.165 * profile.fat_pct) * milk
    me_total = me_maint + me_gest + me_milk
    if me_gest:
        notes.append(f"Gebelik payı eklendi: +{me_gest * 1000:,.0f} kcal/gün (yaşama payının %30'u).")

    cp_total = max(70.7 + 2.77 * mbw, CP_MAINTENANCE_FLOOR_G) + (43.6 + 11.5 * profile.fat_pct) * milk

    dsv = corrected_milk_kg(profile) if lactating else 0.0
    ca_total = 0.0154 * bw / 0.38 + 1.22 * dsv / 0.38
    p_total = 0.0143 * bw / 0.5 + 0.99 * dsv / 0.5

    me_density = me_total / dmi
    me_cap = ME_DENSITY_CAP[profile.group]
    deficit = 0.0
    if me_density > me_cap:
        deficit = me_total - me_cap * dmi
        me_density = me_cap
        notes.append(
            f"Negatif enerji dengesi: ihtiyacın günde ~{deficit * 1000:,.0f} kcal'i vücut "
            "rezervinden karşılanıyor (yenebilir bir rasyon daha yoğun olamaz; BCS'yi izleyin)."
        )

    cp_density = cp_total / 1000.0 / dmi * 100.0
    cp_cap = CP_DENSITY_CAP[profile.group]
    if cp_density > cp_cap:
        notes.append(
            f"Protein açığı: günde ~{cp_total - cp_cap * dmi * 10:,.0f} g HP vücut "
            "proteininden karşılanıyor."
        )
        cp_density = cp_cap
    floor = _DRY_CP_FLOOR.get(profile.group)
    if floor and cp_density < floor:
        cp_density = floor

    cp_band, me_band = _BANDS[profile.group]
    ca_cap, p_cap = _MINERAL_MAX[profile.group]
    ca_min, p_min = ca_total / dmi, p_total / dmi

    nasem = _nasem_dmi(profile, days_pregnant)
    if lactating:
        notes.append(f"Karşılaştırma: NASEM 2021 bu hayvan için ~{nasem:.1f} kg KM tahmin ediyor.")
    else:
        notes.append(f"Kuru madde NASEM 2021 kuru inek denklemiyle: CA'nın %{dmi / bw * 100:.2f}'i.")

    return Requirements(
        group=profile.group,
        dm_intake_kg=round(dmi, 2),
        me_mcal_day=round(me_total, 2),
        me_min=round(me_density, 3),
        me_max=round(me_density + me_band, 3),
        cp_g_day=round(cp_total),
        cp_min=round(cp_density, 2),
        cp_max=round(max(cp_density + cp_band, _CP_MAX_FLOOR.get(profile.group, 0.0)), 2),
        ca_g_day=round(ca_total, 1),
        p_g_day=round(p_total, 1),
        ca_min=round(ca_min, 2),
        ca_max=max(ca_cap, round(ca_min + 2.0, 2)),
        p_min=round(p_min, 2),
        p_max=max(p_cap, round(p_min + 1.0, 2)),
        water_l_day=round(12.3 + 2.15 * dmi + 0.73 * milk, 1),
        energy_deficit_mcal=round(deficit, 2),
        nasem_dmi_kg=round(nasem, 1),
        notes=notes,
    )


def requirements_to_constraints(req: Requirements) -> NutrientConstraints:
    """İhtiyacı solver kısıtlarına çevirir (enerji ME, protein HP üzerinden)."""
    return NutrientConstraints(
        crude_protein_min=req.cp_min,
        crude_protein_max=req.cp_max,
        metabolic_energy_min=req.me_min,
        metabolic_energy_max=req.me_max,
        calcium_min=req.ca_min,
        calcium_max=req.ca_max,
        phosphorus_min=req.p_min,
        phosphorus_max=req.p_max,
        daily_intake_kg=req.dm_intake_kg,
    )
