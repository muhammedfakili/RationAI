"""Karma yem (konsantre) formülasyonu — kullanıcının HP → ME tablosuna göre, 1 ton için.

Rasyon çözücüsünden (optimization.py) ayrıdır çünkü birimi farklıdır: hedefler ve sonuçlar
karma yemin DOĞAL hali içindir (1 kg yemde % HP, kcal ME, % Ca/P) — defterdeki hesap da
böyle yapılıyor (185 kg arpa → 2.1 HP puanı, 527 kcal). Veritabanı KM bazında tuttuğu için
yem değerleri burada doğal hale geri çevrilir.

Kurallar (data/notlar/rasyon_notlari.md §3-4), yüzdeler karma yemin içinde:
- HP hedef ile hedef + 0.5 arası, ME tablodaki aralık.
- Arpa ≤ %40, buğday ≤ %10, her ayçiçeği/soya küspesi ≤ %25, eklenen yağ ≤ %6,
  protein olmayan azot (üre eşdeğeri) ≤ %1, melas ≤ %3 (30 kg/ton).
- Diğer hammadde tavanları kaynaklı (`FEED_CAP_PCT`, Penn State / Feedipedia / NDDB / Canola
  Council); kaynağı olmayanlar çeşitlilik için en fazla %30, mısır ve yulafta tavan yok.
  Buzağı yemlerinde pamuk küspesi, tam yağlı çiğit ve yer fıstığı küspesi yok.
- Vitamin-mineral premiks sabit 1 kg/ton; tuz %0.5-1; mermer tozu ve DCP'yi Ca/P hedefi belirler.
- Besi yemi: soya küspesi kullanılmaz, dane : küspe en az 6 : 1.
- Ca/P aralıkları taslaktır: resmi tebliğ metnine ulaşılamadı, üretici etiketlerinden
  (besi yemi Ca %1-2, P ≥ %0.5, NaCl ≤ %1) ve buzağı başlangıç önerisinden (Ca ≥ %1) alındı.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

import pulp

from .optimization import FORAGE_TYPES, NPN_UREA_EQ_MAX, UREA_CP_PCT, FeedIngredient
from .schemas import CompoundFeedLine, CompoundFeedResponse, CompoundFeedType

logger = logging.getLogger(__name__)

CP_BAND = 0.5                      # HP hedefi: en az hedef, en fazla hedef + 0.5
SALT_MIN_PCT, SALT_MAX_PCT = 0.5, 1.0
PREMIX_KG_PER_TON = 1.0
MOLASSES_MAX_PCT = 3.0
FAT_ADDED_MAX_PCT = 6.0
# Hammadde tavanları, karma yemin %'si. Kaynaklar:
#   K  = kullanıcının notları (rasyon_notlari.md §3)
#   PS = Penn State Extension, "Concentrates for Dairy Cattle" (2023), konsantre %'si
#   FP = Feedipedia buğday kepeği (sağmal %20, buzağı %10)
#   AÜ = Ankara Üniv. ders notu (DDGS ruminant karma yemde %20-25, buzağıya bira mayası %3-5)
#   CC = Canola Council (kanola küspesi rasyonda %20'ye kadar)
#   ND = NDDB Animal Nutrition (2012): yer fıstığı küspesi %25 üstü ishal yapar
FEED_CAP_PCT = {
    "Arpa": 40.0, "Buğday": 10.0,                                     # K
    "Buğday Kepeği": 20.0,                                            # FP
    "Razmol": 15.0, "Bonkalite": 15.0,                                # PS (wheat midds)
    "Mısır DDGS": 25.0, "Buğday DDGS": 25.0,                          # PS, AÜ
    "Pamuk Tohumu Küspesi 24": 20.0, "Pamuk Tohumu Küspesi 42": 20.0,  # PS
    "Tam Yağlı Çiğit": 20.0,                                          # PS
    "Şeker Pancarı Posası": 25.0,                                     # PS
    "Mısır Gluten Yemi": 20.0,                                        # PS
    "Kanola Küspesi": 20.0,                                           # CC
    "Yer Fıstığı Küspesi": 20.0,                                      # ND
    "Bira Mayası (Kuru)": 5.0,                                        # AÜ
}
MEAL_CAP_PCT = 25.0                # her ayçiçeği / soya küspesi (K)
# PS'de mısır ve yulaf (öğütülmüş) için tavan yok; listede olmayan diğer her hammadde,
# karışım çeşitli olsun diye en fazla %30 (kaynak değil, kullanıcıyla kararlaştırıldı).
UNCAPPED_FEEDS = frozenset({"Mısır", "Nemli Mısır", "Yulaf"})
GENERAL_CAP_PCT = 30.0
# Tritikale tahılların en fazla yarısı (NDDB 2012).
TRITICALE_SHARE_OF_GRAIN = 0.5
# Buzağı yemleri: pamuk küspesi (gossipol, kullanıcı kararı), tam yağlı çiğit (PS: 4-6 aylıktan
# küçük buzağıya verilmez), yer fıstığı küspesi (ND: 6 aylıktan küçükte aflatoksin) yok;
# buğday kepeği %10 (FP).
CALF_CAP_PCT = {
    "Pamuk Tohumu Küspesi 24": 0.0, "Pamuk Tohumu Küspesi 42": 0.0,
    "Tam Yağlı Çiğit": 0.0, "Yer Fıstığı Küspesi": 0.0, "Buğday Kepeği": 10.0,
}
FATTENING_GRAIN_TO_MEAL = 6.0
PREMIX_NAME = "Vitamin-Mineral Premiks"
# Gevşetilmiş modda hedef kaçırmanın bedeli (TL / birim): maliyetin çok önünde.
_SLACK_PENALTY = 1.0e6

_DAIRY = dict(ca_min=0.8, ca_max=1.5, p_min=0.45)
_CALF = dict(ca_min=1.0, ca_max=1.5, p_min=0.5)
_OTHER = dict(ca_min=0.8, ca_max=1.5, p_min=0.5)


def _type(key, label, group, cp, me_min, me_max, minerals, fattening=False) -> CompoundFeedType:
    return CompoundFeedType(key=key, label=label, group=group, cp_min=cp, cp_max=cp + CP_BAND,
                            me_min=me_min, me_max=me_max, fattening=fattening, **minerals)


# Kullanıcının HP → ME tablosu (§4). Buzağı büyütme: tablodaki 2500-2600 (defterdeki 2650
# yerine, kullanıcı kararı).
COMPOUND_FEED_TYPES: Dict[str, CompoundFeedType] = {t.key: t for t in (
    _type("sut_23", "Süt yemi %23", "Süt yemi", 23, 2900, 3000, _DAIRY),
    _type("sut_22", "Süt yemi %22", "Süt yemi", 22, 2750, 2800, _DAIRY),
    _type("sut_21", "Süt yemi %21", "Süt yemi", 21, 2700, 2750, _DAIRY),
    _type("buzagi_19", "Buzağı ve süt yemi %19", "Buzağı yemi", 19, 2650, 2700, _CALF),
    _type("buzagi_baslangic", "Buzağı başlangıç yemi %18", "Buzağı yemi", 18, 2650, 2700, _CALF),
    _type("buzagi_buyutme", "Buzağı büyütme yemi %17", "Buzağı yemi", 17, 2500, 2600, _CALF),
    _type("duve_16", "Düve / kuru dönem / besi başlangıç %16", "Düve-kuru-besi başlangıç",
          16, 2500, 2600, _OTHER),
    _type("duve_15", "Düve / kuru dönem / besi başlangıç %15", "Düve-kuru-besi başlangıç",
          15, 2500, 2600, _OTHER),
    _type("duve_14", "Düve / kuru dönem / besi başlangıç %14", "Düve-kuru-besi başlangıç",
          14, 2500, 2700, _OTHER),
    _type("besi_13", "Besi yemi %13", "Besi yemi", 13, 2500, 2700, _OTHER, fattening=True),
    _type("besi_12", "Besi yemi %12", "Besi yemi", 12, 2500, 2700, _OTHER, fattening=True),
)}


@dataclass
class _AsFed:
    """Bir yemin doğal hal değerleri (1 kg yem için)."""
    feed: FeedIngredient
    cp: float      # %
    me: float      # kcal/kg
    ca: float      # %
    p: float       # %


def _as_fed(feed: FeedIngredient) -> _AsFed:
    dm = feed.dry_matter / 100.0
    return _AsFed(feed, cp=feed.crude_protein * dm, me=feed.metabolic_energy * dm * 1000.0,
                  ca=feed.calcium * dm / 10.0, p=feed.phosphorus * dm / 10.0)


def _is_meal(feed: FeedIngredient) -> bool:
    return "Küspesi" in feed.name


def _is_soybean_meal(feed: FeedIngredient) -> bool:
    return feed.name.startswith("Soya Küspesi")


def _is_calf_feed(target: CompoundFeedType) -> bool:
    return target.group == "Buzağı yemi"


def _cap_pct(feed: FeedIngredient, target: CompoundFeedType) -> Optional[float]:
    """Hammaddenin karma yemdeki tavanı (%); None = burada tavan yok (başka kural tutar)."""
    if _is_calf_feed(target) and feed.name in CALF_CAP_PCT:
        return CALF_CAP_PCT[feed.name]
    if feed.name in FEED_CAP_PCT:
        return FEED_CAP_PCT[feed.name]
    if feed.name.startswith(("Ayçiçeği Küspesi", "Soya Küspesi")):
        return MEAL_CAP_PCT
    if (feed.feed_type in ("mineral", "salt", "fat", "npn") or feed.name in UNCAPPED_FEEDS
            or feed.name in ("Melas", PREMIX_NAME)):
        return None
    return GENERAL_CAP_PCT


def resolve_target(type_key: str, cp_min: Optional[float] = None, cp_max: Optional[float] = None,
                   me_min: Optional[float] = None, me_max: Optional[float] = None) -> CompoundFeedType:
    """Hazır tipi döndürür; verilen HP/ME değerleri ("özel hedef") onun üzerine yazılır."""
    if type_key not in COMPOUND_FEED_TYPES:
        raise ValueError(f"Bilinmeyen karma yem tipi: {type_key}")
    target = COMPOUND_FEED_TYPES[type_key].model_copy()
    custom = {k: v for k, v in dict(cp_min=cp_min, cp_max=cp_max, me_min=me_min, me_max=me_max).items()
              if v is not None}
    if custom:
        target = target.model_copy(update=custom)
        if cp_min is not None and cp_max is None:
            target.cp_max = cp_min + CP_BAND
        target.label = f"Özel hedef (%{target.cp_min:g} HP, {target.me_min:.0f}-{target.me_max:.0f} kcal)"
    target.cp_max = max(target.cp_max, target.cp_min)
    target.me_max = max(target.me_max, target.me_min)
    return target


def optimize_compound_feed(
    feeds: List[FeedIngredient], target: CompoundFeedType, batch_kg: float = 1000.0
) -> CompoundFeedResponse:
    """En ucuz karma yemi bulur: önce hedefler katı, olmazsa hedef kaçırma cezalı."""
    notes: List[str] = []
    excluded = [f.name for f in feeds if f.feed_type in FORAGE_TYPES]
    if excluded:
        notes.append(f"🚫 Kaba yemler karma yeme girmez, çıkarıldı: {', '.join(excluded)}.")
    if target.fattening:
        soy = [f.name for f in feeds if _is_soybean_meal(f)]
        if soy:
            notes.append(f"🚫 Besi yeminde soya küspesi kullanılmaz, çıkarıldı: {', '.join(soy)}.")
            excluded += soy
    if _is_calf_feed(target):
        banned = [f.name for f in feeds if CALF_CAP_PCT.get(f.name) == 0.0]
        if banned:
            notes.append(f"🚫 Buzağı yeminde kullanılmaz, çıkarıldı: {', '.join(banned)} "
                         "(gossipol / aflatoksin riski).")
            excluded += banned
    usable = [_as_fed(f) for f in feeds if f.name not in excluded]
    if not usable:
        return _response("Yem yok", "infeasible", batch_kg, {}, [], target,
                         notes + ["Seçilen yemlerden karma yeme girebilen yok."], excluded)

    status, amounts, missed = _solve(usable, target, batch_kg, relaxed=False)
    mode = "hard"
    if status != "Optimal":
        status, amounts, missed = _solve(usable, target, batch_kg, relaxed=True)
        mode = "relaxed" if status == "Optimal" else "infeasible"
    if mode == "hard":
        notes.insert(0, "✅ Tüm hedefler sağlandı — bu, hedefi tutturan en ucuz karışım.")
    elif mode == "relaxed":
        notes.insert(0, "⚠️ Seçilen hammaddelerle hedeflerin hepsi tutmadı; en yakın karışım:")
        notes[1:1] = missed + _diagnose(usable, target)
    else:
        notes.insert(0, "⛔ Bu hammaddelerle karışım kurulamıyor.")
        notes[1:1] = _diagnose(usable, target)
        amounts = {}
    if not any(a.feed.feed_type == "salt" for a in usable):
        notes.append("🧂 Tuz seçilmedi: karma yemlerde tuz genelde %0.5-1 olur.")
    if not any(a.feed.name == PREMIX_NAME for a in usable):
        notes.append("💊 Vitamin-mineral premiks seçilmedi (tonda 1 kg önerilir).")
    return _response(status, mode, batch_kg, amounts, usable, target, notes, excluded)


def _solve(items: List[_AsFed], t: CompoundFeedType, batch: float, relaxed: bool):
    prob = pulp.LpProblem("Compound_Feed", pulp.LpMinimize)
    x = {a.feed.name: pulp.LpVariable(f"kg_{i}", lowBound=0) for i, a in enumerate(items)}
    penalties = []

    def total(value):
        return pulp.lpSum(value(a) * x[a.feed.name] for a in items)

    def share(subset):
        return pulp.lpSum(x[a.feed.name] for a in subset)

    def bound(name, expr, low=None, high=None):
        """Besin hedefi: katı ya da (gevşetilmiş modda) cezalı."""
        if low is not None:
            s = pulp.LpVariable(f"miss_{name}_min", lowBound=0) if relaxed else 0
            prob.addConstraint(expr >= low * batch - s, f"{name}_min")
            if relaxed:
                penalties.append(s)
        if high is not None:
            s = pulp.LpVariable(f"miss_{name}_max", lowBound=0) if relaxed else 0
            prob.addConstraint(expr <= high * batch + s, f"{name}_max")
            if relaxed:
                penalties.append(s)

    prob += share(items) == batch, "Batch"
    bound("cp", total(lambda a: a.cp), t.cp_min, t.cp_max)
    # ME kcal/kg; ceza birimini HP ile dengelemek için 100 kcal = 1 birim.
    bound("me", total(lambda a: a.me / 100.0), t.me_min / 100.0, t.me_max / 100.0)
    bound("ca", total(lambda a: a.ca), t.ca_min, t.ca_max)
    bound("p", total(lambda a: a.p), t.p_min, None)

    # Kullanım sınırları ve sabit miktarlar her iki modda katıdır.
    for a in items:
        cap = _cap_pct(a.feed, t)
        if cap is not None:
            prob += x[a.feed.name] <= cap / 100.0 * batch, f"Cap_{a.feed.name}"
        if a.feed.name == "Melas":
            prob += x[a.feed.name] <= MOLASSES_MAX_PCT / 100.0 * batch, "Molasses_Max"
        if a.feed.name == PREMIX_NAME:
            prob += x[a.feed.name] == PREMIX_KG_PER_TON * batch / 1000.0, "Premix_Fixed"
    fats = [a for a in items if a.feed.feed_type == "fat"]
    if fats:
        prob += share(fats) <= FAT_ADDED_MAX_PCT / 100.0 * batch, "Fat_Max"
    npn = [a for a in items if a.feed.feed_type == "npn"]
    if npn:
        prob += (pulp.lpSum(a.cp / UREA_CP_PCT * x[a.feed.name] for a in npn)
                 <= NPN_UREA_EQ_MAX * batch), "NPN_Max"
    triticale = [a for a in items if a.feed.name == "Tritikale"]
    if triticale:
        grains = [a for a in items if a.feed.feed_type == "grain"]
        prob += share(triticale) <= TRITICALE_SHARE_OF_GRAIN * share(grains), "Triticale_Of_Grain"
    salt = [a for a in items if a.feed.feed_type == "salt"]
    if salt:
        prob += share(salt) >= SALT_MIN_PCT / 100.0 * batch, "Salt_Min"
        prob += share(salt) <= SALT_MAX_PCT / 100.0 * batch, "Salt_Max"
    if t.fattening:
        meals = [a for a in items if _is_meal(a.feed)]
        grains = [a for a in items if a.feed.feed_type == "grain"]
        if meals:
            prob += share(grains) >= FATTENING_GRAIN_TO_MEAL * share(meals), "Grain_To_Meal"

    # Amaç en sonda (PuLP kopya tuzağı, bkz. CLAUDE.md).
    prob += (pulp.lpSum(a.feed.price_per_kg * x[a.feed.name] for a in items)
             + _SLACK_PENALTY * pulp.lpSum(penalties))
    prob.solve(pulp.PULP_CBC_CMD(msg=False, timeLimit=20))
    status = pulp.LpStatus[prob.status]
    if status != "Optimal":
        return status, {}, []
    amounts = {name: var.varValue or 0.0 for name, var in x.items()}

    missed = []
    labels = {"cp": ("HP", "%", 1, 1.0), "me": ("ME", "kcal/kg", 0, 100.0),
              "ca": ("Ca", "%", 2, 1.0), "p": ("P", "%", 2, 1.0)}
    for var in prob.variables():
        if var.name.startswith("miss_") and (var.varValue or 0) > 1e-6:
            key, side = var.name[5:].rsplit("_", 1)
            label, unit, digits, scale = labels[key]
            miss = var.varValue * scale / batch
            missed.append(f"- {label} hedefin {'altında' if side == 'min' else 'üstünde'} "
                          f"kaldı: {miss:.{digits}f} {unit}")
    return status, amounts, missed


def _diagnose(items: List[_AsFed], t: CompoundFeedType) -> List[str]:
    """Hangi hedefin seçimle neden ulaşılamaz olduğunu söyler."""
    notes = []
    if max(a.cp for a in items) < t.cp_min:
        notes.append(f"❌ HP'si %{t.cp_min:g}'nin üstünde hammadde yok — küspe ekleyin.")
    elif not any(_is_meal(a.feed) or a.feed.feed_type == "protein" for a in items):
        notes.append("⚠️ Protein kaynağı (küspe) yok; HP hedefi zor tutar.")
    if max(a.me for a in items) < t.me_min:
        notes.append(f"❌ ME'si {t.me_min:.0f} kcal'in üstünde hammadde yok — mısır gibi "
                     "enerjili bir tane ekleyin.")
    if not any(a.ca > 10 for a in items):
        notes.append("⚠️ Kalsiyum kaynağı (mermer tozu / DCP) seçilmedi; Ca hedefi tutmayabilir.")
    if not any(a.p > 5 for a in items):
        notes.append("⚠️ Fosfor kaynağı (DCP) seçilmedi; P hedefi tutmayabilir.")
    return notes


def _response(status, mode, batch, amounts, items, target, notes, excluded) -> CompoundFeedResponse:
    by_name = {a.feed.name: a for a in items}
    lines = []
    for name, kg in sorted(amounts.items(), key=lambda kv: -kv[1]):
        if kg <= 1e-3:
            continue
        a = by_name[name]
        lines.append(CompoundFeedLine(
            name=name, amount_kg=round(kg, 1), share_pct=round(kg / batch * 100, 2),
            cp_points=round(kg * a.cp / batch, 2), me_kcal=round(kg * a.me / batch, 1),
            price_per_kg=round(a.feed.price_per_kg, 2), cost=round(kg * a.feed.price_per_kg, 2),
        ))

    def mix(value):
        return sum(value(by_name[n]) * kg for n, kg in amounts.items()) / batch if amounts else 0.0

    cost = sum(line.cost for line in lines)
    return CompoundFeedResponse(
        status=status, mode=mode, batch_kg=batch, lines=lines,
        cp_pct=round(mix(lambda a: a.cp), 2), me_kcal=round(mix(lambda a: a.me)),
        ca_pct=round(mix(lambda a: a.ca), 2), p_pct=round(mix(lambda a: a.p), 2),
        salt_pct=round(sum(kg for n, kg in amounts.items()
                           if by_name[n].feed.feed_type == "salt") / batch * 100, 2),
        cost_total=round(cost, 2), cost_per_kg=round(cost / batch, 2) if lines else 0.0,
        target=target, notes=notes, excluded_feeds=excluded,
    )
