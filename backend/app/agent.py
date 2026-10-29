"""Sohbet ajanı: OpenAI uyumlu bir sohbet API'si (NVIDIA NIM ya da Groq) + yerel araç çağırma.

Akış: sistem mesajı + sohbet geçmişi + (varsa) ekrandaki son sonucun özeti + kullanıcı
mesajı → model ya araç çağırır ya cevap verir. Araç sonuçları modele geri verilir, model
en fazla MAX_TOOL_ROUNDS tur sonra kendi cümleleriyle cevaplar.

⚠️ Hesap araçları ekrandaki düğmelerle AYNI uç nokta fonksiyonlarını çağırır (main.optimize,
main.compound_feed, main.fattening_economics; öneriler dahil). Rakamlar her zaman çözücüden
gelir — model rasyon miktarı uyduramaz; uydurursa bir kez uyarılıp yeniden sorulur, yine
olmazsa cevap reddedilir.

Sağlayıcı `.env`'den seçilir (LLM_PROVIDER; yoksa anahtarı olan ilk sağlayıcı) ve model
LLM_MODEL ile değiştirilebilir. Anahtar yoksa backend'in geri kalanı çalışmaya devam eder,
yalnız /api/v1/agent/query 503 döner.
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from .database import SessionLocal
from .models import Feed
from .optimization import NutrientConstraints, RULES_BY_GROUP
from .rag import query_with_sources
from .schemas import AgentResponse

logger = logging.getLogger(__name__)

# Minerals the UI pre-selects; added when the user names no mineral, otherwise Ca/P
# can only come from forages and the ration lands in relaxed mode.
DEFAULT_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz", "Vitamin-Mineral Premiks")
# Kuzu: sığır premiksi yok (bakır), DCP yok (Ca:P ≥ 2), amonyum klorür var (idrar taşı).
DEFAULT_LAMB_MINERALS = ("Mermer Tozu", "Tuz", "Koyun Premiksi (Bakırsız)", "Amonyum Klorür")
DEFAULT_COMPOUND_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz", "Vitamin-Mineral Premiks")

# Short or English names the LLM sometimes uses -> database name. The short Turkish
# names matter: substring matching sent "yonca" to whichever alfalfa feed came first
# (often the silage) although farmers mean the hay.
_FEED_ALIASES = {
    "yonca": "Yonca Kuru Otu",
    "kuru yonca": "Yonca Kuru Otu",
    "soya": "Soya Küspesi 46",
    "soya küspesi": "Soya Küspesi 46",
    "sfk": "Soya Küspesi 46",
    "saman": "Buğday Samanı",
    "silaj": "Mısır Silajı",
    "ayçiçeği": "Ayçiçeği Küspesi 36",
    "ayçiçeği küspesi": "Ayçiçeği Küspesi 36",
    "atk": "Ayçiçeği Küspesi 36",
    "pamuk küspesi": "Pamuk Tohumu Küspesi 24",
    "ptk": "Pamuk Tohumu Küspesi 24",
    "kolza": "Kanola Küspesi",
    "kanola": "Kanola Küspesi",
    "çayır otu": "Çayır Kuru Otu",
    "bira posası": "Malt Posası (Yaş)",
    "kepek": "Buğday Kepeği",
    "pancar posası": "Şeker Pancarı Posası",
    "ddgs": "Mısır DDGS",
    "bypass yağ": "Korunmuş Yağ (Bypass)",
    "yağ": "Bitkisel Yağ",
    "mermer tozu": "Mermer Tozu",
    "dcp": "DCP (Dikalsiyum Fosfat)",
    "premiks": "Vitamin-Mineral Premiks",
    "corn": "Mısır",
    "wheat": "Buğday",
    "barley": "Arpa",
    "oats": "Yulaf",
    "soybean meal": "Soya Küspesi 46",
    "full-fat soybean": "Soya Dane (Fullfat)",
    "corn gluten feed": "Mısır Gluten Yemi",
    "corn bran": "Mısır Kepeği",
    "wheat bran": "Buğday Kepeği",
    "alfalfa hay": "Yonca Kuru Otu",
    "corn silage": "Mısır Silajı",
    "meadow hay": "Çayır Kuru Otu",
    "straw": "Buğday Samanı",
}

# Physically sensible ranges for LLM-supplied constraints (DM basis).
NUTRIENT_BOUNDS = {
    "crude_protein": (6.0, 30.0),           # % DM
    "metabolizable_energy": (1.5, 4.0),     # Mcal/kg DM
    "net_energy_lactation": (0.9, 2.0),     # Mcal/kg DM
    "calcium": (1.0, 15.0),                 # g/kg DM
    "phosphorus": (1.0, 10.0),              # g/kg DM
    "dry_matter": (20.0, 100.0),            # % as-fed
}
# Constraint key the LLM uses -> NutrientConstraints field prefix.
_CONSTRAINT_FIELDS = {
    "crude_protein": "crude_protein",
    "metabolizable_energy": "metabolic_energy",
    "net_energy_lactation": "net_energy_lactation",
    "calcium": "calcium",
    "phosphorus": "phosphorus",
    "dry_matter": "dry_matter",
}

ToolResult = Tuple[str, Optional[Dict[str, Any]]]


def _to_float(value: Any) -> Optional[float]:
    """Coerce to float; None when missing, unparsable or NaN."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return None if result != result else result


def parse_constraints(raw: Any, expected_dm_intake_kg: Optional[float] = None) -> NutrientConstraints:
    """Turn the LLM's nested constraint JSON into clamped NutrientConstraints.

    Accepts ``{"crude_protein": {"min": 16, "max": 18}, ..., "daily_intake_kg": 22}``
    as a dict or JSON string. Unparsable values are dropped, bounds are clamped to
    `NUTRIENT_BOUNDS`, and inverted min/max are swapped instead of discarded.
    Without an intake in the JSON, `expected_dm_intake_kg` is used (None stays None).
    """
    data = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or {})
    values: Dict[str, Optional[float]] = {}

    for key, (lo, hi) in NUTRIENT_BOUNDS.items():
        bounds = data.get(key)
        if isinstance(bounds, dict):
            low, high = _to_float(bounds.get("min")), _to_float(bounds.get("max"))
        else:
            low, high = _to_float(bounds), None  # a bare scalar is read as a minimum
        low = None if low is None else max(lo, min(hi, low))
        high = None if high is None else max(lo, min(hi, high))
        if low is not None and high is not None and low > high:
            low, high = high, low
        field = _CONSTRAINT_FIELDS[key]
        values[f"{field}_min"], values[f"{field}_max"] = low, high

    intake = _to_float(data.get("daily_intake_kg"))
    if intake is None:
        intake = expected_dm_intake_kg
    values["daily_intake_kg"] = None if intake is None else max(0.5, min(60.0, intake))
    return NutrientConstraints(**values)


def _words(text: str) -> List[str]:
    return re.findall(r"[a-zçğıöşü0-9]+", text.replace("I", "ı").replace("İ", "i").lower())


def _match_feed_names(requested: List[str], all_names: List[str]) -> List[str]:
    """Map LLM feed names onto database names: exact, alias, substring, then word prefixes.

    The last step catches spelling variants: "ayçiçek küspesi" -> "Ayçiçeği Küspesi 36"
    (every word of the request, minus 1-2 letters, starts a word of the feed name).
    """
    by_lower = {name.lower(): name for name in all_names}
    by_lower.update({alias: name for alias, name in _FEED_ALIASES.items() if name in all_names})
    matched: List[str] = []
    for name in requested:
        key = name.lower().strip()
        # Birden çok ad içeriyorsa en uzunu: "arpa hasılı" -> "Arpa Hasılı Silajı", "Arpa" değil.
        hit = by_lower.get(key) or max(
            ((lower, canonical) for lower, canonical in by_lower.items() if key in lower or lower in key),
            key=lambda item: len(item[0]), default=(None, None))[1]
        if hit is None:
            stems = [w[:max(3, len(w) - 2)] for w in _words(key)]
            # Takma adlar önce: derecesiz "ayçiçek" ATK 36'ya gider (ATK 24'e değil).
            aliases_first = [(a, c) for a, c in _FEED_ALIASES.items() if c in all_names] + list(by_lower.items())
            hit = next((canonical for lower, canonical in aliases_first
                        if stems and all(any(word.startswith(st) for word in _words(lower)) for st in stems)),
                       None)
        if hit and hit not in matched:
            matched.append(hit)
    return matched


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [n.strip() for n in value.split(",") if n.strip()]
    return [str(n).strip() for n in (value or []) if str(n).strip()]


# ============================================================
# Sonuç özetleri — hem araç çıktısı (modele) hem "bu sonuç hakkında sor" bağlamı
# ============================================================

_CONSTRAINT_LABEL = {
    "crude_protein_min": "ham protein alt sınırı", "crude_protein_max": "ham protein üst sınırı",
    "metabolic_energy_min": "enerji (ME) alt sınırı", "metabolic_energy_max": "enerji (ME) üst sınırı",
    "calcium_min": "kalsiyum alt sınırı", "calcium_max": "kalsiyum üst sınırı",
    "phosphorus_min": "fosfor alt sınırı", "phosphorus_max": "fosfor üst sınırı",
}
_MODE_TEXT = {
    "hard": "BÜTÜN besin hedefleri ve rasyon kuralları SAĞLANDI.",
    "relaxed": "Besin hedeflerinin HEPSİ TUTMADI (seçilen yemlerle en yakın rasyon). Bunu kullanıcıya açıkça söyle.",
    "infeasible": "Bu yemlerle rasyon KURULAMADI.",
}


def summarize_ration(r: Dict[str, Any]) -> str:
    sd, nc = r.get("solver_details") or {}, r.get("nutrient_content") or {}
    small = (r.get("daily_intake") or {}).get("dry_matter_kg", 0) < 5
    mode = sd.get("nutrition_mode")
    lines = [f"SONUÇ: {_MODE_TEXT.get(mode, mode)}",
             f"Günlük maliyet {r.get('total_cost_per_day', 0):.2f} TL, "
             f"kuru madde {(r.get('daily_intake') or {}).get('dry_matter_kg', 0):.2f} kg/gün."]
    if sd.get("energy_surplus") and mode == "hard":
        lines.append("Enerji ihtiyacın üstünde (uyarıya bak); bu bir hedef ihlali sayılmaz.")
    for f in r.get("feed_recommendations") or []:
        amount = f"{f['amount_kg_per_day'] * 1000:.0f} g" if small else f"{f['amount_kg_per_day']:.2f} kg"
        lines.append(f"- {f['name']}: {amount}/gün, {f['cost_per_day']:.2f} TL")
    lines.append(
        f"Besin değeri (KM): HP %{nc.get('crude_protein_percent_dm', 0):.1f}, ME "
        f"{(nc.get('metabolic_energy_mcal_per_kg_dm') or 0) * 1000:.0f} kcal/kg, Ca "
        f"{nc.get('calcium_g_per_kg_dm', 0):.1f} g/kg, P {nc.get('phosphorus_g_per_kg_dm', 0):.1f} g/kg, "
        f"NDF %{nc.get('ndf_percent_dm') or 0:.1f}, kaba yem %{nc.get('forage_ratio_percent') or 0:.0f}, "
        f"nişasta %{nc.get('starch_percent_dm') or 0:.1f}.")
    bad = [f"{_CONSTRAINT_LABEL.get(c['constraint'], c['constraint'])} ({c['status']})"
           for c in r.get("constraints_status") or [] if c.get("status") != "OK"
           and not (mode == "hard" and c["constraint"] == "metabolic_energy_max")]
    if bad:
        lines.append("TUTMAYAN HEDEFLER: " + "; ".join(bad))
    for w in (r.get("warnings") or [])[:3]:
        lines.append(f"Uyarı: {w}")
    for key in ("dropped_feeds", "excluded_feeds"):
        if sd.get(key):
            lines.append(f"{key}: {', '.join(sd[key])}")
    for a in r.get("advice") or []:
        lines.append(f"Öneri ({a['kind']}): {a['text']}")
    return "\n".join(lines)


def summarize_compound(r: Dict[str, Any]) -> str:
    t = r.get("target") or {}
    lines = [f"SONUÇ: {_MODE_TEXT.get(r.get('mode'), r.get('mode'))}",
             f"Karma yem ({t.get('label')}): {r.get('batch_kg', 0):,.0f} kg, "
             f"maliyet {r.get('cost_total', 0):,.0f} TL ({r.get('cost_per_kg', 0):.2f} TL/kg). "
             f"HP %{r.get('cp_pct', 0):.2f} (hedef {t.get('cp_min')}-{t.get('cp_max')}), ME "
             f"{r.get('me_kcal', 0):.0f} kcal/kg (hedef {t.get('me_min')}-{t.get('me_max')}), "
             f"Ca %{r.get('ca_pct', 0):.2f}, P %{r.get('p_pct', 0):.2f}."]
    for line in r.get("lines") or []:
        lines.append(f"- {line['name']}: {line['amount_kg']:.1f} kg (%{line['share_pct']:.1f})")
    for note in (r.get("notes") or [])[:4]:
        lines.append(f"Not: {note}")
    for a in r.get("advice") or []:
        lines.append(f"Öneri ({a['kind']}): {a['text']}")
    return "\n".join(lines)


def summarize_economics(r: Dict[str, Any]) -> str:
    ph, herd = r.get("per_head") or {}, r.get("herd") or {}
    prices = r.get("prices") or {}
    lines = [
        f"Besi ekonomisi: {r.get('head_count')} baş, {r.get('start_weight_kg')} → {r.get('end_weight_kg', 0):.1f} kg, "
        f"{r.get('days')} gün, karkas {r.get('carcass_weight_kg', 0):.1f} kg (randıman %{r.get('dressing_pct')}).",
        f"Kesim fiyatı {(prices.get('carcass') or {}).get('price_per_kg', 0):.2f} TL/kg "
        f"({(prices.get('carcass') or {}).get('source')}); alış {(prices.get('purchase') or {}).get('price_per_kg', 0):.1f} "
        f"TL/kg canlı ({(prices.get('purchase') or {}).get('source')}).",
        f"Baş başına: alış {ph.get('purchase', 0):,.0f}, yem {ph.get('feed', 0):,.0f} "
        f"({r.get('feed_cost_per_day', 0):.2f} TL/gün), diğer {ph.get('other_total', 0):,.0f}, toplam maliyet "
        f"{ph.get('total_cost', 0):,.0f}, gelir {ph.get('revenue', 0):,.0f}, KÂR {ph.get('profit', 0):,.0f} TL.",
        f"Sürü kârı {herd.get('profit', 0):,.0f} TL, günlük kâr/baş {r.get('profit_per_day', 0):.1f} TL, "
        f"başabaş kesim fiyatı {r.get('breakeven_carcass_price', 0):.1f} TL/kg, yemden yararlanma "
        f"{r.get('feed_conversion', 0):.2f}.",
    ]
    lines += [f"Uyarı: {w}" for w in r.get("warnings") or []]
    lines += [f"Öneri ({a['kind']}): {a['text']}" for a in r.get("advice") or []]
    return "\n".join(lines)


SUMMARIZERS = {"ration": summarize_ration, "compound": summarize_compound, "economics": summarize_economics}


# ============================================================
# Araçlar — her biri (modele verilecek metin, ekranda gösterilecek yapı | None) döndürür
# ============================================================

def _all_feed_names() -> List[str]:
    with SessionLocal() as db:
        return [name for (name,) in db.query(Feed.name).all()]


def search_knowledge_base(query: str, _sources: Optional[List[str]] = None) -> ToolResult:
    hits = query_with_sources(query, n_results=4)
    if not hits:
        return "Bilgi tabanında ilgili bilgi bulunamadı.", None
    if _sources is not None:
        _sources.extend(src for _, src in hits if src not in _sources)
    return "\n\n".join(f"[Kaynak: {src}]\n{text}" for text, src in hits), None


def get_feed_data(feed_names: Any = None) -> ToolResult:
    wanted = _as_list(feed_names)
    with SessionLocal() as db:
        feeds = db.query(Feed).order_by(Feed.display_order).all()
    if wanted:
        names = set(_match_feed_names(wanted, [f.name for f in feeds]))
        feeds = [f for f in feeds if f.name in names]
    if not feeds:
        return "Bu adla yem bulunamadı.", None
    lines = ["Yem değerleri (HP ve ME doğal halde, Ca/P/NDF KM'de):"] + [
        f"- {f.name} [{f.feed_type}]: {f.price_per_kg:.2f} TL/kg ({f.price_source}), KM %{f.dry_matter:g}, "
        f"HP %{f.crude_protein * f.dry_matter / 100:.1f}, ME {f.metabolic_energy * f.dry_matter * 10:.0f} kcal/kg, "
        f"NDF %{f.ndf:g}, Ca {f.calcium:g} g/kg, P {f.phosphorus:g} g/kg"
        for f in feeds
    ]
    return "\n".join(lines), None


def calculate_ration(
    feed_names: Any = None,
    group: str = "lactating",
    animal_weight: float = 650.0,
    milk_yield: float = 30.0,
    daily_gain: Optional[float] = None,
    days_on_feed: int = 60,
    days_in_milk: int = 120,
    fat_pct: float = 3.8,
    days_pregnant: int = 0,
    first_lactation: bool = False,
    constraints: Any = None,
    cheapest_subset: bool = False,
) -> ToolResult:
    """Ekrandaki "Rasyonu hesapla" ile aynı: main.optimize (öneriler dahil)."""
    from .main import optimize          # döngüsel import: main bu modülü içe aktarıyor
    from .schemas import AnimalProfileIn, OptimizationRequest

    group = group if group in RULES_BY_GROUP else "lactating"
    if daily_gain is None:
        daily_gain = 0.3 if group == "lamb" else 1.4
    all_names = _all_feed_names()
    names = _match_feed_names(_as_list(feed_names), all_names)
    use_all = bool(names) and not cheapest_subset
    if not names:
        names = all_names               # yem söylenmediyse hepsi aday, hiçbiri zorunlu değil
    with SessionLocal() as db:
        types = {f.name: f.feed_type for f in db.query(Feed).all()}
        if not any(types.get(n) in ("mineral", "salt") for n in names):
            names += [n for n in (DEFAULT_LAMB_MINERALS if group == "lamb" else DEFAULT_MINERALS)
                      if n in types and n not in names]
        explicit = parse_constraints(constraints)
        # Yalnız kullanıcının açıkça söylediği hedefler profilden gelenlerin üstüne yazılır.
        overrides = {k: v for k, v in vars(explicit).items()
                     if v is not None and k != "daily_intake_kg" and not k.startswith("net_energy")}
        if explicit.daily_intake_kg:
            overrides["target_dm_intake_kg"] = explicit.daily_intake_kg
        request = OptimizationRequest(
            animal=AnimalProfileIn(
                group=group, body_weight_kg=float(animal_weight),
                milk_kg=float(milk_yield or 0) if group == "lactating" else 0.0,
                fat_pct=float(fat_pct), days_in_milk=max(1, int(days_in_milk)),
                multiparous=not bool(first_lactation), days_pregnant=max(0, int(days_pregnant)),
                daily_gain_kg=min(max(float(daily_gain), 0.0), 2.5), days_on_feed=max(1, int(days_on_feed)),
            ),
            selected_feed_names=names, use_all_selected_feeds=use_all, with_advice=True, **overrides,
        )
        result = optimize(request, db).model_dump()
    result["request"] = {"animal": request.animal.model_dump(), "selected_feed_names": names}
    return summarize_ration(result), {"kind": "ration", "data": result}


def design_compound_feed(type_key: str = "sut_21", feed_names: Any = None, batch_kg: float = 1000.0,
                         cp_target: Optional[float] = None) -> ToolResult:
    """Ekrandaki "Karma yemi hesapla" ile aynı: main.compound_feed (öneriler dahil)."""
    from .compound_feed import COMPOUND_FEED_TYPES
    from .main import compound_feed
    from .schemas import CompoundFeedRequest

    if type_key not in COMPOUND_FEED_TYPES:
        return f"Bilinmeyen karma yem tipi '{type_key}'. Geçerli tipler: {', '.join(COMPOUND_FEED_TYPES)}", None
    all_names = _all_feed_names()
    names = _match_feed_names(_as_list(feed_names), all_names)
    if not names:
        return ("Karma yem için hammadde adı gerekli (ör. arpa, mısır, ayçiçeği küspesi, kepek). "
                "Kullanıcıya elindeki hammaddeleri sor."), None
    names += [n for n in DEFAULT_COMPOUND_MINERALS if n in all_names and n not in names]
    request = CompoundFeedRequest(type_key=type_key, selected_feed_names=names, batch_kg=float(batch_kg),
                                  cp_min=_to_float(cp_target), with_advice=True)
    with SessionLocal() as db:
        result = compound_feed(request, db).model_dump()
    result["request"] = {"type_key": type_key, "selected_feed_names": names, "batch_kg": float(batch_kg)}
    return summarize_compound(result), {"kind": "compound", "data": result}


def fattening_economics(species: str = "beef", head_count: int = 1, start_weight_kg: float = 300.0,
                        daily_gain_kg: Optional[float] = None, days: int = 180, feed_names: Any = None,
                        region: Optional[str] = None, dressing_pct: Optional[float] = None) -> ToolResult:
    """Ekrandaki "Besi ekonomisini hesapla" ile aynı: main.fattening_economics."""
    from fastapi import HTTPException

    from .main import fattening_economics as economics_endpoint
    from .schemas import EconomicsRequest

    species = "lamb" if species in ("lamb", "kuzu", "sheep", "küçükbaş") else "beef"
    if daily_gain_kg is None:
        daily_gain_kg = 0.3 if species == "lamb" else 1.4
    all_names = _all_feed_names()
    names = _match_feed_names(_as_list(feed_names), all_names)
    if not names:
        names = (["Yonca Kuru Otu", "Buğday Samanı", "Arpa", "Mısır", "Ayçiçeği Küspesi 36", "Buğday Kepeği"]
                 if species == "lamb" else
                 ["Mısır Silajı", "Buğday Samanı", "Arpa", "Mısır", "Ayçiçeği Küspesi 36", "Buğday Kepeği"])
    defaults = DEFAULT_LAMB_MINERALS if species == "lamb" else DEFAULT_MINERALS
    names += [n for n in defaults if n in all_names and n not in names]
    request = EconomicsRequest(
        species=species, head_count=max(1, int(head_count)), start_weight_kg=float(start_weight_kg),
        daily_gain_kg=float(daily_gain_kg), days=int(days), selected_feed_names=names,
        region=region or None, dressing_pct=_to_float(dressing_pct), with_advice=True)
    with SessionLocal() as db:
        try:
            result = economics_endpoint(request, db).model_dump(mode="json")
        except HTTPException as exc:
            return f"Hesaplanamadı: {exc.detail}", None
    result["request"] = request.model_dump(mode="json")
    return summarize_economics(result), {"kind": "economics", "data": result}


_FEED_ARRAY = {"type": "array", "items": {"type": "string"},
               "description": "Kullanıcının söylediği yemler, Türkçe adlarıyla (ör. 'mısır silajı', 'arpa', 'soya')."}
TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "search_knowledge_base",
        "description": "Rasyon notları, program kuralları ve yem tablolarında arama yapar. Beslemeyle ilgili "
                       "bilgi/'neden' sorularında önce bunu kullan.",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_feed_data",
        "description": "Yemlerin fiyatı, HP, ME, NDF, Ca, P değerleri. Ad verilmezse tüm yemler.",
        "parameters": {"type": "object", "properties": {"feed_names": _FEED_ARRAY}}}},
    {"type": "function", "function": {
        "name": "calculate_ration",
        "description": "Bir hayvan için ihtiyacı karşılayan en ucuz günlük rasyonu hesaplar (öneriler dahil). "
                       "Rasyon istenince MUTLAKA bunu çağır.",
        "parameters": {"type": "object", "properties": {
            "group": {"type": "string", "enum": ["lactating", "dry_faroff", "dry_closeup", "beef", "lamb"],
                      "description": "lactating=sağmal, dry_faroff=kuru (erken), dry_closeup=doğuma son 3 hafta, "
                                     "beef=besi sığırı, lamb=küçükbaş/kuzu/toklu besisi"},
            "animal_weight": {"type": "number", "description": "Canlı ağırlık, kg"},
            "milk_yield": {"type": "number", "description": "Sağmalda günlük süt, kg (1 litre ≈ 1 kg)"},
            "daily_gain": {"type": "number", "description": "Besi/küçükbaşta hedef günlük artış, kg (300 g = 0.3)"},
            "days_on_feed": {"type": "integer", "description": "Besinin kaçıncı günü (besi başında kaba yem fazla)"},
            "days_pregnant": {"type": "integer"}, "fat_pct": {"type": "number"},
            "feed_names": _FEED_ARRAY,
            "cheapest_subset": {"type": "boolean", "description": "true: seçilenlerin hepsini kullanmak zorunda değil"}},
            "required": ["group", "animal_weight"]}}},
    {"type": "function", "function": {
        "name": "design_compound_feed",
        "description": "Karma yem (konsantre) için en ucuz karışımı hesaplar.",
        "parameters": {"type": "object", "properties": {
            "type_key": {"type": "string", "enum": ["sut_23", "sut_22", "sut_21", "buzagi_19", "buzagi_baslangic",
                                                    "buzagi_buyutme", "duve_16", "duve_15", "duve_14", "besi_13", "besi_12"],
                         "description": "sut_21 = %21 HP süt yemi, besi_13 = %13 besi yemi, buzagi_* buzağı yemleri"},
            "feed_names": _FEED_ARRAY, "batch_kg": {"type": "number", "description": "Karışım miktarı, kg (varsayılan 1000)"}},
            "required": ["type_key", "feed_names"]}}},
    {"type": "function", "function": {
        "name": "fattening_economics",
        "description": "Bir besi döneminin maliyet, gelir ve kârını hesaplar (kesim fiyatı UKON'dan).",
        "parameters": {"type": "object", "properties": {
            "species": {"type": "string", "enum": ["beef", "lamb"]},
            "head_count": {"type": "integer"}, "start_weight_kg": {"type": "number"},
            "daily_gain_kg": {"type": "number"}, "days": {"type": "integer"}, "feed_names": _FEED_ARRAY,
            "region": {"type": "string", "description": "UKON bölgesi, ör. 'Marmara Bölgesi' (boş = Türkiye ortalaması)"},
            "dressing_pct": {"type": "number", "description": "Karkas randımanı %"}},
            "required": ["species", "head_count", "start_weight_kg", "days"]}}},
]
TOOLS = {
    "search_knowledge_base": search_knowledge_base,
    "get_feed_data": get_feed_data,
    "calculate_ration": calculate_ration,
    "design_compound_feed": design_compound_feed,
    "fattening_economics": fattening_economics,
}


# ============================================================
# LLM sağlayıcısı — OpenAI uyumlu /chat/completions (NVIDIA NIM, Groq)
# ============================================================

# (temel adres, anahtar değişkeni, varsayılan model)
PROVIDERS = {
    "nvidia": ("https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY", "nvidia/nemotron-3.5-super-vl-120b-a12b"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY", "openai/gpt-oss-120b"),
}
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT", "120"))
MAX_TOOL_ROUNDS = 4
HISTORY_TURNS = 8


def _provider() -> Optional[Tuple[str, str, str, str]]:
    """(ad, temel adres, anahtar, model) — LLM_PROVIDER ya da anahtarı olan ilk sağlayıcı."""
    wanted = os.getenv("LLM_PROVIDER", "").strip().lower()
    order = [wanted] if wanted in PROVIDERS else list(PROVIDERS)
    for name in order:
        base, key_var, default_model = PROVIDERS[name]
        key = os.getenv(key_var, "").strip()
        if key:
            # GROQ_MODEL eski ayar adı, geriye uyumluluk için okunur.
            model = (os.getenv("LLM_MODEL", "").strip()
                     or (os.getenv("GROQ_MODEL", "").strip() if name == "groq" else "") or default_model)
            return name, base, key, model
    return None


def llm_available() -> bool:
    """Bir sağlayıcının anahtarı tanımlı mı (ağ çağrısı yok)."""
    return _provider() is not None


def llm_info() -> Optional[str]:
    provider = _provider()
    return f"{provider[0]} · {provider[3]}" if provider else None


def _chat(messages: List[Dict[str, Any]], tools: bool = True) -> Dict[str, Any]:
    provider = _provider()
    if provider is None:
        raise RuntimeError("Sohbet için LLM anahtarı tanımlı değil (NVIDIA_API_KEY ya da GROQ_API_KEY).")
    _, base, key, model = provider
    body: Dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": 4000}
    if tools:
        body["tools"] = TOOL_SPECS
        body["tool_choice"] = "auto"
    for attempt in range(2):
        resp = requests.post(f"{base}/chat/completions", json=body, timeout=LLM_TIMEOUT_S,
                             headers={"Authorization": f"Bearer {key}"})
        if resp.status_code == 429 and attempt == 0:
            time.sleep(5)               # ücretsiz katmanlarda dakikalık sınır
            continue
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]
    resp.raise_for_status()
    return {}


_THINK = re.compile(r"<think>[\s\S]*?</think>", re.IGNORECASE)


def _clean(text: Optional[str]) -> str:
    """Akıl yürütme bloklarını at (bazı modeller cevaba <think> koyuyor)."""
    return _THINK.sub("", text or "").strip()


SYSTEM_PROMPT = """Sen RationAI'sın: Türkiye'deki çiftçilere ve zooteknistlere hayvan beslemede yardım eden bir asistan.

Kurallar:
- Her zaman Türkçe, sade ve kısa cevap ver; çiftçinin anlayacağı dille konuş.
- Rasyon, karma yem ya da besi kâr/zarar hesabı istenirse MUTLAKA ilgili aracı çağır:
  calculate_ration, design_compound_feed, fattening_economics. Kendi başına yem miktarı (kg),
  maliyet ya da kâr rakamı YAZMA — bütün rakamlar araçtan gelir.
- Araç sonucu ekranda tablo olarak ayrıca gösteriliyor: cevabında tabloyu tekrar yazma; sonucu 3-6
  cümleyle özetle (hedefler tuttu mu, maliyet, en önemli 1-2 öneri) ve kullanıcının sorusunu cevapla.
- Beslemeyle ilgili bilgi ve "neden" sorularında önce search_knowledge_base ile ara ve cevabını
  bulduğun kaynağa dayandır. Kaynakta olmayan bir şeyi genel bilginle söylüyorsan cümlenin sonuna
  "(genel bilgi, notlarda yok)" yaz.
- Eksik bilgi varsa (hayvanın ağırlığı, süt verimi, elindeki yemler) makul varsayım yap ve bunu
  söyle; ya da kısa bir soruyla sor.
- Hayvan grupları: sağmal inek, kuru dönem (erken / doğuma yakın), besi sığırı, küçükbaş (kuzu/toklu) besisi.
- Ekrandaki ya da önceki bir sonuç hakkında "neden" sorulursa (ör. "neden saman az?"), önce o sonucun
  "Öneri (sinir)" ve uyarı satırlarına bak: çoğu zaman cevap programın bir kuralıdır (ör. sağmalda saman
  en fazla %8, arpa en fazla %40). Kuralın gerekçesi için search_knowledge_base ile "RationAI Kuralları"
  belgesinde ara.
- Önceki mesajlara atıf yapılırsa ("bir de arpayı çıkarırsam?") önceki hesabın bilgilerini kullanıp aracı yeniden çağır.
"""

_RATION_REQUEST = re.compile(r"rasyon|ration|yemleme|karma yem|kâr|kar eder|kazan", re.IGNORECASE)
_SUSPECT_ANSWER = re.compile(r"\d+(?:[.,]\d+)?\s*(?:kg|TL)\b", re.IGNORECASE)
_TOOL_REMINDER = ("Bu bir hesap isteği. Rakamları kendin yazma: uygun aracı (calculate_ration, "
                  "design_compound_feed ya da fattening_economics) şimdi çağır.")
_NO_ANSWER = "Üzgünüm, bu soruya şu an cevap üretemedim. Lütfen tekrar deneyin."
_NO_CALC = ("Bu hesabı şu an yapamadım. Lütfen tekrar deneyin ya da ekrandaki hesap düğmelerini "
            "kullanın — onlar dil modeli olmadan çalışır.")


def run_agent(query: str, history: Optional[List[Dict[str, str]]] = None,
              context: Optional[Dict[str, Any]] = None) -> AgentResponse:
    """Bir sohbet turu. `context` = ekrandaki son sonuç ({"kind", "data"}); varsa modele özeti verilir.

    Hata olursa loglanıp yeniden fırlatılır — uç nokta 500'e çevirir (çökme 200 olarak dönmez).
    """
    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if context and context.get("kind") in SUMMARIZERS:
        messages.append({"role": "system", "content": "Kullanıcının ekranda baktığı son sonuç:\n"
                         + SUMMARIZERS[context["kind"]](context.get("data") or {})
                         + ("\nBu sonucun girdileri: " + json.dumps(context["data"].get("request"), ensure_ascii=False)
                            if (context.get("data") or {}).get("request") else "")})
    for turn in (history or [])[-HISTORY_TURNS:]:
        if turn.get("role") in ("user", "assistant") and turn.get("content"):
            messages.append({"role": turn["role"], "content": str(turn["content"])})
    messages.append({"role": "user", "content": query})

    sources: List[str] = []
    tools_used: List[str] = []
    structured: Optional[Dict[str, Any]] = None
    reminded = False
    try:
        for _ in range(MAX_TOOL_ROUNDS + 1):
            msg = _chat(messages)
            calls = msg.get("tool_calls") or []
            if not calls:
                answer = _clean(msg.get("content"))
                if (not tools_used and not reminded and _RATION_REQUEST.search(query)
                        and _SUSPECT_ANSWER.search(answer)):
                    # Model hesabı araçsız, kafadan yaptı: bir kez uyar, yine olmazsa gösterme.
                    logger.info("Hesap isteği araçsız cevaplandı, yeniden soruluyor")
                    reminded = True
                    messages += [{"role": "assistant", "content": answer},
                                 {"role": "system", "content": _TOOL_REMINDER}]
                    continue
                if reminded and not tools_used:
                    answer = _NO_CALC
                break
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                tool = TOOLS.get(name)
                if tool is None:
                    text, data = f"Bilinmeyen araç: {name}", None
                else:
                    try:
                        if name == "search_knowledge_base":
                            text, data = tool(_sources=sources, **args)
                        else:
                            text, data = tool(**args)
                    except Exception as exc:   # noqa: BLE001 - modele hata metni verilir
                        logger.exception("Araç %s başarısız (args=%r)", name, args)
                        text, data = f"Araç çalıştırılamadı ({name}): {exc}", None
                tools_used.append(name)
                if data:
                    structured = data
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": text})
        else:
            # Tur sınırı doldu: araçsız son bir cevap iste.
            answer = _clean(_chat(messages + [{"role": "system", "content": "Şimdi kısa bir cevap yaz."}],
                                  tools=False).get("content"))
    except Exception:
        logger.exception("run_agent başarısız (query=%r)", query)
        raise

    answer = answer or _NO_ANSWER
    return AgentResponse(
        success=True, answer=answer, response=answer, sources=sources, tools_used=tools_used,
        result=structured, model=llm_info(),
        optimization_result=structured["data"] if structured and structured["kind"] == "ration" else None,
    )
