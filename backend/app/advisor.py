"""Öneriler: rasyon, karma yem ve besi ekonomisi sonuçları için "ne yapmalısın" listesi.

Her "şunu ekle / şunun yerine bunu koy" önerisi DENENİR: aday yem seçime eklenip (ya da
değiştirilip) aynı çözücüyle yeniden çözülür; öneri yalnız sonuç gerçekten tutuyorsa ve
rakamı (kaç kg, kaç TL) o çözümden gelir (kullanıcı kararı, Ekim 2026). Bir çözüm ~25-50 ms,
bütün denemeler 1-2 sn.

Türler ve sıra: sağlık (her zaman gösterilir) → eksik → fazla → sınır → ucuzlatma; sağlık
dışındakiler toplam MAX_ADVICE'ı doldurana kadar. Besi ekonomisinde "ekonomi" maddeleri de
her zaman gösterilir. Adaylar yalnız yaygın yemlerdir (COMMON_FEEDS) — elinde olmayan
"malt çimi ekle" gibi öneriler çıkmasın.
"""

from typing import Callable, Dict, List, Optional

from .compound_feed import _cap_pct as compound_cap_pct
from .optimization import (
    RationRules,
    _feed_cap,
    _max_inclusion_windows,
    feeds_from_db,
)

MAX_ADVICE = 5
ORDER = ("saglik", "ekonomi", "eksik", "fazla", "sinir", "ucuz")
ALWAYS_SHOWN = frozenset({"saglik", "ekonomi"})

# Öneri adayları: Türkiye'de her işletmenin bulabileceği yemler (display_order başı).
COMMON_FEEDS = (
    "Mısır Silajı", "Yonca Kuru Otu", "Buğday Samanı", "Çayır Kuru Otu", "Fiğ Kuru Otu",
    "Arpa", "Mısır", "Buğday", "Buğday Kepeği", "Ayçiçeği Küspesi 36", "Soya Küspesi 46",
    "Pamuk Tohumu Küspesi 24", "Mısır DDGS", "Şeker Pancarı Posası", "Kanola Küspesi",
    "Tritikale", "Bitkisel Yağ", "Melas",
    "Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz",
)
# Tutmayan hedef -> onu yükseltecek adaylar (önce yaygın ve ucuz olanlar).
_FIXERS = {
    "metabolic_energy_min": ("Mısır", "Arpa", "Buğday", "Bitkisel Yağ", "Mısır DDGS", "Melas"),
    "crude_protein_min": ("Ayçiçeği Küspesi 36", "Soya Küspesi 46", "Mısır DDGS",
                          "Pamuk Tohumu Küspesi 24", "Kanola Küspesi", "Yonca Kuru Otu"),
    "calcium_min": ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Yonca Kuru Otu"),
    "phosphorus_min": ("DCP (Dikalsiyum Fosfat)", "Buğday Kepeği"),
}
_NUTRIENT_LABEL = {
    "metabolic_energy_min": "Enerji (ME)", "crude_protein_min": "Ham protein",
    "calcium_min": "Kalsiyum", "phosphorus_min": "Fosfor",
}
_NUTRIENT_SHORT = {"metabolic_energy_min": "enerji", "crude_protein_min": "protein",
                   "calcium_min": "kalsiyum", "phosphorus_min": "fosfor"}
# Tek yem yetmezse kaç adaydan ikili denensin (≤ 15 ek çözüm).
_PAIR_POOL = 6
# Enerji fazlasını seyreltecek düşük enerjili kaba yemler.
_DILUTERS = ("Buğday Samanı", "Arpa Samanı", "Çayır Kuru Otu")
# Ucuzlatmada değiş-tokuş yalnız aynı sınıf içinde (küspe yerine küspe, tahıl yerine tahıl).
# Kaba yemde aynı tür: "silaj yerine kuru ot" kâğıtta ucuz ama işletmenin kaba yem düzenini
# değiştirir — öyle bir öneri yapılmaz.
_SWAP_CLASS = {"silage": "silage", "hay": "hay", "straw": "straw",
               "grain": "grain", "protein": "protein", "byproduct": "byproduct"}
_HEALTH_CAP_REASON = {
    "silage": "asidoz ve ishal", "straw": "tüketimi ve sindirimi düşürür",
    "hay": "yonca: şişme (timpani) riski", "fat": "rumen fermantasyonunu bozar",
    "npn": "amonyak zehirlenmesi", "mineral": "mineral dengesi",
    "salt": "tuz zehirlenmesi", "byproduct": "ishal", "protein": "ishal",
}
_INCLUSION_REASON = {
    "grain": "asidoz", "protein": "rasyon notu",
    "straw": "tüketimi düşürür", "byproduct": "rasyon dengesi", "silage": "kaba yem dengesi",
    "hay": "kaba yem dengesi", "fat": "rumen fermantasyonu", "mineral": "mineral dengesi",
}


def _advice(kind: str, text: str, add=(), remove=(), saving: Optional[float] = None) -> dict:
    return {"kind": kind, "text": text, "add": list(add), "remove": list(remove),
            "saving_tl_day": None if saving is None else round(saving, 2)}


def _rank(items: List[dict]) -> List[dict]:
    """Sağlık/ekonomi hep; diğerleri sırayla, toplam MAX_ADVICE'ı doldurana kadar."""
    items = sorted(items, key=lambda a: ORDER.index(a["kind"]))
    shown = [a for a in items if a["kind"] in ALWAYS_SHOWN]
    for a in items:
        if a["kind"] not in ALWAYS_SHOWN and len(shown) < MAX_ADVICE:
            shown.append(a)
    return shown


def _amount_text(kg: float, small: bool) -> str:
    return f"~{kg * 1000:.0f} g/gün" if small else f"~{kg:.2f} kg/gün"


def _threshold(cost: float) -> float:
    """Gösterilecek en küçük tasarruf: maliyetin %3'ü ya da 5 TL/gün, hangisi küçükse."""
    return min(0.03 * cost, 5.0)


def _status_ok(resp, name: str) -> bool:
    return all(c.status == "OK" for c in resp.constraints_status if c.constraint == name)


def _amount(resp, name: str) -> float:
    return next((r.amount_kg_per_day for r in resp.feed_recommendations if r.name == name), 0.0)


# =====================================================================
# Günlük rasyon
# =====================================================================

def advise_ration(
    base,                                   # OptimizationResponse
    names: List[str],
    feeds_by_name: Dict[str, object],       # Feed satırları (tüm yemler)
    rules: RationRules,
    group: Optional[str],
    body_weight: float,
    solve: Callable[[List[str]], object],
    me_max: Optional[float] = None,         # Mcal/kg KM
    gain_target: Optional[float] = None,    # besi: hedef kg/gün
) -> List[dict]:
    selected = [n for n in names if n not in rules.excluded_feeds]
    feed_type = {n: (feeds_by_name[n].feed_type if n in feeds_by_name else "unknown") for n in feeds_by_name}
    mode = base.solver_details.get("nutrition_mode")
    cost = base.total_cost_per_day or 0.0
    small = (base.daily_intake.dry_matter_kg or 0.0) < 5
    nc = base.nutrient_content
    items: List[dict] = []
    tried: Dict[tuple, object] = {}

    def candidates(pool) -> List[str]:
        return [c for c in pool if c in feeds_by_name and c not in selected
                and c not in rules.excluded_feeds]

    def try_names(new_names: List[str]):
        key = tuple(sorted(new_names))
        if key not in tried:
            tried[key] = solve(new_names)
        return tried[key]

    # ---- Sağlık ------------------------------------------------------------
    types_selected = {feed_type.get(n) for n in selected}
    if "salt" not in types_selected and "Tuz" in feeds_by_name:
        items.append(_advice("saglik", "Tuz seçilmedi: sodyum ihtiyacı karşılanmaz. Rasyona tuz ekleyin "
                             "(KM'nin %0.25-0.5'i).", add=["Tuz"]))
    if group == "lamb":
        for name, why in (("Amonyum Klorür", "erkek kuzuda idrar taşına karşı %0.5 KM önerilir"),
                          ("Koyun Premiksi (Bakırsız)", "vitamin ve iz element için; sığır premiksi "
                                                        "bakır nedeniyle koyunda kullanılmaz")):
            if name not in selected and name in feeds_by_name:
                items.append(_advice("saglik", f"{name} seçilmedi: {why}.", add=[name]))
    elif "Vitamin-Mineral Premiks" not in selected and "Vitamin-Mineral Premiks" in feeds_by_name:
        items.append(_advice("saglik", "Vitamin-mineral premiks seçilmedi: A-D-E vitaminleri ve iz "
                             "elementler (Zn, Cu, Mn, Se) yemlerden yeterli gelmez.",
                             add=["Vitamin-Mineral Premiks"]))
    starch = nc.starch_percent_dm or 0.0
    if rules.starch_max < 100 and starch >= 0.9 * rules.starch_max:
        add = [] if "Sodyum Bikarbonat" in selected else ["Sodyum Bikarbonat"]
        items.append(_advice("saglik", f"Nişasta %{starch:.1f}, sınıra yakın (en fazla %{rules.starch_max:g}): "
                             "asidoz riski. Tahılı azaltıp lifli yem artırın"
                             + ("; tampon olarak sodyum bikarbonat ekleyebilirsiniz." if add else "."),
                             add=add))
    ratio = nc.ca_p_ratio or 0.0
    if ratio and ratio < rules.ca_p_ratio_min - 0.05:
        why = {"lamb": "idrar taşı", "beef": "idrar taşı"}.get(group, "kemik ve süt humması")
        add = [] if "Mermer Tozu" in selected or "Mermer Tozu" in rules.excluded_feeds else ["Mermer Tozu"]
        items.append(_advice("saglik", f"Ca:P oranı {ratio:.2f}, en az {rules.ca_p_ratio_min:g} olmalı ({why})."
                             + (" Mermer tozu ekleyin." if add else ""), add=add))

    # ---- Eksik: tutmayan besin hedefleri -------------------------------------
    if mode in ("relaxed", "infeasible"):
        failing = [c for c in base.constraints_status if c.constraint in _FIXERS and c.status != "OK"]
        missing_text = ", ".join(_NUTRIENT_SHORT[c.constraint] for c in failing)
        pool = list(dict.fromkeys(c for f in failing for c in candidates(_FIXERS[f.constraint])))
        whole = None
        for cand in pool:
            resp = try_names(selected + [cand])
            if resp.solver_details.get("nutrition_mode") == "hard" and (
                    whole is None or resp.total_cost_per_day < whole[1].total_cost_per_day):
                whole = ([cand], resp)
        if whole is None and len(failing) > 1:
            short = pool[:_PAIR_POOL]
            for i, a in enumerate(short):
                for b in short[i + 1:]:
                    resp = try_names(selected + [a, b])
                    if resp.solver_details.get("nutrition_mode") == "hard" and (
                            whole is None or resp.total_cost_per_day < whole[1].total_cost_per_day):
                        whole = ([a, b], resp)
        if whole is not None:
            add, resp = whole
            amounts = " ve ".join(f"{n} ({_amount_text(_amount(resp, n), small)})" for n in add)
            items.append(_advice("eksik", f"Tutmayan hedef: {missing_text}. {amounts} eklersen bütün "
                                 f"hedefler tutuyor; maliyet {resp.total_cost_per_day - cost:+.2f} TL/gün.",
                                 add=add))
            failing = []
        for c in failing:
            best = None
            for cand in candidates(_FIXERS[c.constraint]):
                resp = try_names(selected + [cand])
                if not _status_ok(resp, c.constraint):
                    continue
                score = (resp.solver_details.get("nutrition_mode") != "hard", resp.total_cost_per_day)
                if best is None or score < best[0]:
                    best = (score, cand, resp)
            label = _NUTRIENT_LABEL[c.constraint]
            if best:
                _, cand, resp = best
                all_ok = resp.solver_details.get("nutrition_mode") == "hard"
                items.append(_advice(
                    "eksik",
                    f"{label} hedefin altında kaldı. {cand} eklersen ({_amount_text(_amount(resp, cand), small)}) "
                    + ("bütün hedefler tutuyor" if all_ok else f"{_NUTRIENT_SHORT[c.constraint]} tutuyor")
                    + f"; maliyet {resp.total_cost_per_day - cost:+.2f} TL/gün.", add=[cand]))
            else:
                items.append(_advice("eksik", f"{label} hedefin altında kaldı ve yaygın yemlerden biri "
                                     "eklenince de tutmuyor: daha yoğun bir kaynak ya da kaba yem "
                                     "seçimini değiştirmek gerekiyor."))

    # Besi: rasyon hedef artışı vermiyorsa enerji kaynağı dene.
    expected = base.solver_details.get("expected_gain_kg")
    if gain_target and expected is not None and expected < 0.97 * gain_target:
        best = None
        for cand in candidates(_FIXERS["metabolic_energy_min"]):
            resp = try_names(selected + [cand])
            gain = resp.solver_details.get("expected_gain_kg") or 0.0
            if resp.solver_details.get("nutrition_mode") == "hard" and gain > expected + 0.03 * gain_target:
                if best is None or gain > best[0]:
                    best = (gain, cand, resp)
        unit = (lambda g: f"{g * 1000:.0f} g") if group == "lamb" else (lambda g: f"{g:.2f} kg")
        if best:
            gain, cand, resp = best
            items.append(_advice("eksik", f"Bu rasyon hedef artışı vermiyor (~{unit(expected)}/gün, hedef "
                                 f"{unit(gain_target)}). {cand} eklersen ~{unit(gain)}/gün olur; maliyet "
                                 f"{resp.total_cost_per_day - cost:+.2f} TL/gün.", add=[cand]))
        else:
            items.append(_advice("eksik", f"Bu rasyon hedef artışı vermiyor (~{unit(expected)}/gün, hedef "
                                 f"{unit(gain_target)}): bu ağırlıkta yenebilir en yoğun rasyon da yetmiyor, "
                                 "hedef artışı düşürmek gerçekçi olur."))

    # ---- Fazla: enerji fazlası (süt ineği / kuru dönem; besi fazlası kilo olur) --
    if base.solver_details.get("energy_surplus") and gain_target is None and me_max:
        dmi = base.daily_intake.dry_matter_kg or 0.0
        excess = (nc.metabolic_energy_mcal_per_kg_dm - me_max) * dmi * 1000.0
        best = None
        for cand in candidates(_DILUTERS):
            resp = try_names(selected + [cand])
            if resp.solver_details.get("nutrition_mode") != "hard":
                continue
            new_excess = (resp.nutrient_content.metabolic_energy_mcal_per_kg_dm - me_max) * dmi * 1000.0
            if new_excess < excess * 0.9 and (best is None or new_excess < best[0]):
                best = (new_excess, cand, resp)
        text = (f"Enerji ihtiyacın günde ~{excess:,.0f} kcal üstünde (≈{excess / 7000:.1f} kg/gün "
                "yağlanma, 7000 kcal ≈ 1 kg).")
        if best:
            new_excess, cand, resp = best
            items.append(_advice("fazla", text + f" {cand} eklersen fazla ~{max(new_excess, 0):,.0f} kcal'e "
                                 f"iner; maliyet {resp.total_cost_per_day - cost:+.2f} TL/gün.", add=[cand]))
        else:
            items.append(_advice("fazla", text + " Seçili yemlerle daha düşük enerjili rasyon kurulamıyor: "
                                 "tahıl yerine saman/kuru ot gibi lifli kaba yem ekleyin."))

    # ---- Sınır: tavana dayanan yemler, düşen yemler ----------------------------
    feed_inputs = feeds_from_db([feeds_by_name[n] for n in selected if n in feeds_by_name])
    windows = _max_inclusion_windows(feed_inputs, rules)
    by_input = {f.name: f for f in feed_inputs}
    dm_total = base.daily_intake.dry_matter_kg or 0.0
    limits: List[dict] = []
    for rec in base.feed_recommendations:
        f = by_input.get(rec.name)
        if f is None or rec.amount_kg_per_day <= 0:
            continue
        cap_kg = None if f.name in rules.health_cap_exempt else f.health_cap_kg(body_weight)
        if cap_kg and rec.amount_kg_per_day >= 0.98 * cap_kg:
            reason = _HEALTH_CAP_REASON.get(f.feed_type, "hayvan sağlığı")
            shown = f"{cap_kg * 1000:.0f} g" if cap_kg < 1 else f"{cap_kg:.1f} kg"
            limits.append(_advice("sinir", f"{f.name} günlük sağlık sınırında ({shown}): bundan fazlası "
                                  f"konmaz ({reason})."))
            continue
        share = rec.amount_kg_per_day * f.dm_fraction() / dm_total if dm_total else 0.0
        cap = _feed_cap(f, windows, rules)
        if f.feed_type not in ("mineral", "salt") and cap < 1 and share >= cap - 0.005:
            reason = "asidoz" if f.name in ("Arpa", "Buğday") else _INCLUSION_REASON.get(f.feed_type, "rasyon dengesi")
            limits.append(_advice("sinir", f"{f.name} tavanda: rasyon kuru maddesinde en fazla "
                                  f"%{cap * 100:g} olabilir ({reason})."))
    items += limits[:2]
    for name in base.solver_details.get("dropped_feeds") or []:
        items.append(_advice("sinir", f"{name} hedeflerle çeliştiği için rasyona girmedi; seçimden "
                             "çıkarabilirsin.", remove=[name]))

    # ---- Ucuzlatma: ekle / değiştir --------------------------------------------
    if mode == "hard" and cost > 0:
        threshold = _threshold(cost)
        savings: List[tuple] = []
        for cand in candidates(COMMON_FEEDS):
            if feed_type.get(cand) not in _SWAP_CLASS:
                continue
            resp = try_names(selected + [cand])
            if resp.solver_details.get("nutrition_mode") == "hard":
                savings.append((cost - resp.total_cost_per_day, [cand], [], resp))
        expensive = [r for r in base.feed_recommendations
                     if r.name in selected and feed_type.get(r.name) in _SWAP_CLASS
                     and r.cost_per_day >= 0.05 * cost]
        for rec in expensive:
            klass = _SWAP_CLASS[feed_type[rec.name]]
            for cand in candidates(COMMON_FEEDS):
                if _SWAP_CLASS.get(feed_type.get(cand)) != klass:
                    continue
                new = [n for n in selected if n != rec.name] + [cand]
                resp = try_names(new)
                if resp.solver_details.get("nutrition_mode") == "hard":
                    savings.append((cost - resp.total_cost_per_day, [cand], [rec.name], resp))
        savings = sorted((s for s in savings if s[0] >= threshold), key=lambda s: -s[0])
        used = set()
        for saving, add, remove, resp in savings:
            if used & set(add + remove):
                continue
            used |= set(add + remove)
            month = saving * 30
            if remove:
                text = f"{remove[0]} yerine {add[0]} koyarsan rasyon günde {saving:.2f} TL ucuzlar"
            else:
                text = f"{add[0]} eklersen ({_amount_text(_amount(resp, add[0]), small)}) rasyon günde {saving:.2f} TL ucuzlar"
            items.append(_advice("ucuz", text + f" (ayda ~{month:,.0f} TL); hedefler yine tutuyor.",
                                 add=add, remove=remove, saving=saving))
            if len([i for i in items if i["kind"] == "ucuz"]) >= 2:
                break

    return _rank(items)


# =====================================================================
# Karma yem
# =====================================================================

_COMPOUND_FIXERS = {
    "cp": ("Ayçiçeği Küspesi 36", "Soya Küspesi 46", "Mısır DDGS", "Pamuk Tohumu Küspesi 24",
           "Kanola Küspesi"),
    "me_low": ("Mısır", "Buğday", "Arpa", "Bitkisel Yağ", "Melas"),
    "me_high": ("Buğday Kepeği", "Şeker Pancarı Posası", "Ayçiçeği Küspesi 36"),
    "ca": ("Mermer Tozu",),
    "p": ("DCP (Dikalsiyum Fosfat)", "Buğday Kepeği"),
}
_COMPOUND_TYPES = ("grain", "protein", "byproduct")


def advise_compound(base, names: List[str], feeds_by_name: Dict[str, object], target,
                    solve: Callable[[List[str]], object]) -> List[dict]:
    selected = [n for n in names if n not in base.excluded_feeds]
    items: List[dict] = []
    cost = base.cost_total or 0.0

    def candidates(pool) -> List[str]:
        return [c for c in pool if c in feeds_by_name and c not in names]

    def ok(resp) -> bool:
        return resp.mode == "hard"

    # ---- Sağlık ----
    if not any(feeds_by_name[n].feed_type == "salt" for n in selected if n in feeds_by_name):
        items.append(_advice("saglik", "Tuz seçilmedi: karma yemde tuz genelde %0.5-1 olur.", add=["Tuz"]))
    if "Vitamin-Mineral Premiks" not in selected:
        items.append(_advice("saglik", "Vitamin-mineral premiks seçilmedi (tonda 1 kg önerilir).",
                             add=["Vitamin-Mineral Premiks"]))

    # ---- Eksik / fazla: tutmayan hedefler ----
    if base.mode != "hard":
        misses = []
        if base.cp_pct < target.cp_min - 0.05:
            misses.append(("cp", "ham protein hedefin altında"))
        if base.me_kcal < target.me_min - 5:
            misses.append(("me_low", "enerji hedefin altında"))
        if base.me_kcal > target.me_max + 5:
            misses.append(("me_high", "enerji hedefin üstünde"))
        if base.ca_pct < target.ca_min - 0.01:
            misses.append(("ca", "kalsiyum hedefin altında"))
        if base.p_pct < target.p_min - 0.01:
            misses.append(("p", "fosfor hedefin altında"))
        pool = list(dict.fromkeys(c for key, _ in misses for c in candidates(_COMPOUND_FIXERS[key])))
        combos = [[c] for c in pool]
        if len(misses) > 1:
            short = pool[:_PAIR_POOL]
            combos += [[a, b] for i, a in enumerate(short) for b in short[i + 1:]]
        best = None
        for add in combos:
            if best is not None and len(add) > len(best[0]):
                break                                  # tek yem yetiyorsa ikiliye geçme
            resp = solve(selected + add)
            if ok(resp) and (best is None or resp.cost_total < best[1].cost_total):
                best = (add, resp)
        labels = "; ".join(label for _, label in misses)
        labels = labels[:1].upper() + labels[1:]
        kind = "fazla" if all(key == "me_high" for key, _ in misses) else "eksik"
        if misses and best:
            add, resp = best
            kgs = " ve ".join(f"{n} ({next((l.amount_kg for l in resp.lines if l.name == n), 0):.0f} kg)"
                              for n in add)
            items.append(_advice(kind, f"{labels}. {kgs} eklersen bütün hedefler tutuyor; karışımın "
                                 f"maliyeti {resp.cost_total - cost:+,.0f} TL.", add=add))
        elif misses:
            items.append(_advice(kind, f"{labels}; yaygın hammaddelerden biri ya da ikisi eklenince de "
                                 "tutmuyor — hedefi ya da hammadde seçimini değiştirmek gerekiyor."))

    # ---- Sınır: tavana dayanan hammaddeler ----
    inputs = {f.name: f for f in feeds_from_db([feeds_by_name[n] for n in selected if n in feeds_by_name])}
    limits = []
    for line in base.lines:
        f = inputs.get(line.name)
        cap = compound_cap_pct(f, target) if f else None
        if cap and line.share_pct >= cap - 0.1:
            limits.append(_advice("sinir", f"{line.name} tavanda: karışımda en fazla %{cap:g} olabilir "
                                  "(kaynaklı hammadde sınırı)."))
    items += limits[:2]

    # ---- Ucuzlatma ----
    if base.mode == "hard" and cost > 0:
        threshold = 0.01 * cost          # tonda en az %1 (~150 TL)
        savings = []
        pool = [c for c in candidates(COMMON_FEEDS) if feeds_by_name[c].feed_type in _COMPOUND_TYPES]
        for cand in pool:
            resp = solve(selected + [cand])
            if ok(resp):
                savings.append((cost - resp.cost_total, [cand], []))
        for line in base.lines:
            f = feeds_by_name.get(line.name)
            if f is None or f.feed_type not in _COMPOUND_TYPES or line.cost < 0.05 * cost:
                continue
            for cand in pool:
                if feeds_by_name[cand].feed_type != f.feed_type:
                    continue
                resp = solve([n for n in selected if n != line.name] + [cand])
                if ok(resp):
                    savings.append((cost - resp.cost_total, [cand], [line.name]))
        used = set()
        for saving, add, remove in sorted((s for s in savings if s[0] >= threshold), key=lambda s: -s[0]):
            if used & set(add + remove):
                continue
            used |= set(add + remove)
            text = (f"{remove[0]} yerine {add[0]} koyarsan" if remove else f"{add[0]} eklersen")
            items.append(_advice("ucuz", f"{text} karışım {base.batch_kg:,.0f} kg'da {saving:,.0f} TL ucuzlar "
                                 "(hedefler yine tutuyor).", add=add, remove=remove, saving=saving))
            if len([i for i in items if i["kind"] == "ucuz"]) >= 2:
                break
    return _rank(items)


# =====================================================================
# Besi ekonomisi
# =====================================================================

def advise_economics(econ: dict, inp, ration_advice: List[dict], carcass_price: float,
                     ration_lines, period_options: List[dict]) -> List[dict]:
    """Ekonomi maddeleri + besi ortası rasyonunun önerileri (tasarruflar dönem için)."""
    items: List[dict] = []
    ph = econ["per_head"]
    be = econ["breakeven_carcass_price"]
    items.append(_advice("ekonomi", f"Kesim fiyatı {be:,.1f} TL/kg'nin altına düşerse besi zarar eder "
                         f"(şu an {carcass_price:,.1f}; güvenlik payı %{(carcass_price - be) / carcass_price * 100:.0f})."))
    total = ph["total_cost"] or 1.0
    top = max(ration_lines, key=lambda r: r.cost_per_day, default=None)
    text = (f"Maliyet dağılımı: hayvan alışı %{ph['purchase'] / total * 100:.0f}, yem "
            f"%{ph['feed'] / total * 100:.0f}, diğer giderler %{ph['other_total'] / total * 100:.0f}.")
    if top is not None and ph["feed"] > 0:
        share = top.cost_per_day * inp.days / ph["feed"] * 100
        text += (f" En pahalı yem {top.name}: günde {top.cost_per_day:.2f} TL "
                 f"(yem maliyetindeki payı %{share:.0f}).")
    items.append(_advice("ekonomi", text))
    best = max(period_options, key=lambda o: o["profit_per_day"], default=None)
    current = econ["profit_per_day"]
    if best and best["days"] != inp.days and best["profit_per_day"] > current * 1.05 and best["profit_per_day"] > 0:
        items.append(_advice("ekonomi", f"Besiyi {best['days']} gün yaparsan (bitiş ~{best['end_weight']:.0f} kg) "
                             f"günlük kâr {best['profit_per_day']:,.1f} TL/baş olur (şu an {current:,.1f}). "
                             "Hayvan ağırlaştıkça kilo artışı pahalılaşır; yaklaşık bir hesaptır."))
    for a in ration_advice:
        if a["kind"] == "ucuz" and a["saving_tl_day"]:
            a = {**a, "text": a["text"] + f" Besi boyunca baş başına ~{a['saving_tl_day'] * inp.days:,.0f} TL, "
                 f"sürüde ~{a['saving_tl_day'] * inp.days * inp.head_count:,.0f} TL."}
        items.append(a)
    return _rank(items)
