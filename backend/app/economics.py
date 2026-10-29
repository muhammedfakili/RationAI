"""Besi ekonomisi: bir besi döneminin baş başına maliyeti, geliri ve kârı.

Hesap kullanıcının hocasının/notlarının yöntemiyle yapılır (data/notlar/rasyon_notlari.md
§5-6): yem tüketimi BESİ ORTASINDAKİ ağırlıktan hesaplanır — besi ortası ağırlığı için
çıkan günlük rasyonun maliyeti × besi günü. Kullanıcı ortalama bir rakam bilmeli; dönem
dönem rasyon daha kesin olurdu ama defterdeki hesapla karşılaştırılamazdı.

Türkiye'de hayvan o günkü kilo başı KESİM fiyatından değerlenir (kullanıcı, Ekim 2026):
- Alış = başlangıç ağırlığı × randıman × kesim fiyatı (ağırlığa göre ayrı "besilik" fiyatı
  yoktur); kullanıcı kendi alış fiyatını girebilir.
- Satış = bitiş ağırlığı × randıman × kesim fiyatı. Besici eti kesimhaneye satar; ayrı bir
  "canlı satış" hesabı yoktur (borsadaki canlı fiyatla karşılaştırma kaldırıldı).
- Diğer giderler: defterdeki kural "yem maliyetinin 1/3'ü" ya da kalem kalem
  (ölüm/fire, veteriner, işçilik, faiz).
"""

from dataclasses import dataclass
from typing import Dict

OTHER_COST_SHARE_OF_FEED = 1.0 / 3.0   # notlar: "diğer giderler yem maliyetinin 1/3'ü"
# Karkas randımanı varsayılanı: dana %55 (notlardaki örnek), kuzu %45 (kullanıcı kararı; örnekteki
# %42 düşük kaldı). Kullanıcı değiştirebilir.
DEFAULT_DRESSING_PCT = {"beef": 55.0, "lamb": 45.0}
# Kalem kalem modda ölüm/fire varsayılanı: besi sığırında %1-1.5, kuzu besisinde %2-4
# (ABD feedlot ve kuzu besisi derlemeleri); kullanıcı değiştirir.
DEFAULT_MORTALITY_PCT = {"beef": 1.5, "lamb": 3.0}


@dataclass
class EconomicsInput:
    species: str                    # "beef" | "lamb"
    head_count: int
    start_weight_kg: float
    daily_gain_kg: float
    days: int
    purchase_price: float           # TL/kg canlı (alış)
    carcass_price: float            # TL/kg karkas (kesim fiyatı)
    dressing_pct: float             # karkas randımanı, %
    itemized_costs: bool = False
    mortality_pct: float = 0.0      # baş başına beklenen kayıp, %
    vet_per_head: float = 0.0       # TL/baş, dönem boyunca
    labor_per_head_day: float = 0.0  # TL/baş/gün (işçilik, elektrik, su ...)
    interest_pct_year: float = 0.0  # yıllık faiz, %


def mid_weight(inp: EconomicsInput) -> float:
    return inp.start_weight_kg + inp.daily_gain_kg * inp.days / 2.0


def compute_economics(inp: EconomicsInput, feed_cost_per_day: float,
                      feed_asfed_per_day: float, feed_dm_per_day: float) -> Dict:
    """Baş başına ve sürü için maliyet, gelir, kâr; başabaş kesim fiyatı."""
    total_gain = inp.daily_gain_kg * inp.days
    end_weight = inp.start_weight_kg + total_gain
    purchase = inp.start_weight_kg * inp.purchase_price
    feed = feed_cost_per_day * inp.days

    if inp.itemized_costs:
        # Ölen hayvan alış bedelini ve (ortalama) yediği yemin yarısını götürür. Faiz aynı
        # bağlanan sermaye üzerinden: alış parası baştan, yem parası dönem boyunca yayılır.
        tied_capital = purchase + feed / 2.0
        other = {
            "Ölüm / fire": inp.mortality_pct / 100.0 * tied_capital,
            "Veteriner / aşı": inp.vet_per_head,
            "İşçilik ve diğer": inp.labor_per_head_day * inp.days,
            "Faiz": tied_capital * inp.interest_pct_year / 100.0 * inp.days / 365.0,
        }
    else:
        other = {"Diğer giderler (yemin 1/3'ü)": feed * OTHER_COST_SHARE_OF_FEED}
    other_total = sum(other.values())
    total_cost = purchase + feed + other_total

    carcass_weight = end_weight * inp.dressing_pct / 100.0
    revenue = carcass_weight * inp.carcass_price
    profit = revenue - total_cost

    per_head = {
        "purchase": purchase,
        "feed": feed,
        "other": other,
        "other_total": other_total,
        "total_cost": total_cost,
        "revenue": revenue,
        "profit": profit,
    }
    return {
        "end_weight_kg": end_weight,
        "mid_weight_kg": mid_weight(inp),
        "total_gain_kg": total_gain,
        "carcass_weight_kg": carcass_weight,
        "feed_cost_per_day": feed_cost_per_day,
        "feed_asfed_kg_total": feed_asfed_per_day * inp.days,
        "feed_dm_kg_total": feed_dm_per_day * inp.days,
        "per_head": per_head,
        "herd": {key: (value * inp.head_count if not isinstance(value, dict)
                       else {k: v * inp.head_count for k, v in value.items()})
                 for key, value in per_head.items()},
        "profit_per_day": profit / inp.days,
        # Kilo artışının maliyeti alışı içermez: yem + diğer giderler / kazanılan kilo.
        "cost_per_kg_gain": (feed + other_total) / total_gain if total_gain > 0 else 0.0,
        "feed_conversion": feed_dm_per_day * inp.days / total_gain if total_gain > 0 else 0.0,
        "breakeven_carcass_price": total_cost / carcass_weight if carcass_weight > 0 else 0.0,
    }
