#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RationAI arayüzü.

Üst menüde üç sayfa (st.navigation, position="top"): Günlük rasyon, Karma yem, Besi ekonomisi.
Her sayfanın başlığında "Asistan" düğmesi sohbet penceresini (st.dialog) açar.
Hesaplar deterministik uç noktalara gider; LLM yalnız asistanda devrededir.
"""

import os
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import altair as alt
import pandas as pd
import requests
import streamlit as st

# Docker compose'ta servis adıyla konuşulur (http://backend:8000/api/v1).
# Varsayılan localhost, konteynersiz `quickstart.py` akışı bozulmasın diye korunur.
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000/api/v1")

# Bu kadar günden eski fiyatlar için kullanıcı uyarılır (enflasyon ayda ~%2-4).
STALE_PRICE_DAYS = 45

# Hedefler (rasyon notları: ME + HP) ve grubun rasyon kuralları backend'den gelir: /requirements.
GROUP_LABELS = {
    "lactating": "Sağmal",
    "dry_faroff": "Kuru (erken)",
    "dry_closeup": "Kuru (doğuma yakın)",
    "beef": "Besi sığırı",
    "lamb": "Küçükbaş besisi",
}

# Yem sınıfı -> seçim grubu
FORAGE_TYPES = ("silage", "hay", "straw")
CONCENTRATE_TYPES = ("grain", "protein", "byproduct", "fat", "npn")
MINERAL_TYPES = ("mineral", "salt")

# Mineraller varsayılan olarak seçili gelir; miktarlarını solver Ca/P açığına göre belirler.
# Kuzuda sığır premiksi yok (bakır), DCP yok (Ca:P ≥ 2), amonyum klorür var (idrar taşı).
DEFAULT_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz", "Vitamin-Mineral Premiks")
DEFAULT_LAMB_MINERALS = ("Mermer Tozu", "Tuz", "Koyun Premiksi (Bakırsız)", "Amonyum Klorür")

st.set_page_config(
    page_title="RationAI",
    page_icon=":material/eco:",
    layout="centered",
    initial_sidebar_state="expanded",
)
_ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
st.logo(os.path.join(_ASSETS, "logo.svg"), size="large", icon_image=os.path.join(_ASSETS, "icon.svg"))


# ============================
# BACKEND ÇAĞRILARI
# ============================

@st.cache_data(ttl=300)
def fetch_feeds() -> List[Dict]:
    """Veritabanındaki yem listesini çek."""
    try:
        response = requests.get(f"{BACKEND_URL}/feeds", timeout=10)
        if response.status_code == 200:
            return response.json()
        return []
    except Exception as exc:
        st.error(f"Yem verileri çekilemedi: {exc}", icon=":material/error:")
        return []


@st.cache_data(ttl=3600, show_spinner=False)
def _post_requirements(profile_items: Tuple) -> Dict:
    # Hata istisna olarak çıkar; st.cache_data istisnaları önbelleğe almaz.
    response = requests.post(f"{BACKEND_URL}/requirements", json=dict(profile_items), timeout=10)
    response.raise_for_status()
    return response.json()


def fetch_requirements(profile: Dict) -> Optional[Dict]:
    """Hayvan profilinin ME + HP ihtiyaçları + grubun rasyon kuralları."""
    try:
        return _post_requirements(tuple(sorted(profile.items())))
    except Exception as exc:
        st.error(f"İhtiyaç hesaplanamadı: {exc}", icon=":material/error:")
        return None


def post_prices(path: str, spinner: str, body=None, timeout: int = 120) -> None:
    """Fiyat uç noktasını çağırır; başarıda önbelleği ve fiyat tablosunu sıfırlayıp yeniler.

    Mesaj yenilemeden sonra gösterilsin diye session_state'e yazılır.
    """
    with st.spinner(spinner):
        try:
            resp = requests.post(f"{BACKEND_URL}/{path}", json=body, timeout=timeout)
        except requests.exceptions.Timeout:
            st.error(f"İstek zaman aşımına uğradı ({timeout} sn).", icon=":material/error:")
            return
        except Exception as exc:
            st.error(f"Bağlantı hatası: {exc}", icon=":material/error:")
            return
    if resp.status_code != 200:
        st.error(f"Fiyat işlemi başarısız: {resp.text}", icon=":material/error:")
        return
    st.cache_data.clear()
    for key in [k for k in st.session_state if k.startswith("price_editor")]:
        st.session_state.pop(key, None)
    st.session_state["price_sync_message"] = resp.json()["message"]
    st.rerun()


def price_age_days(feed: Dict) -> Optional[int]:
    """Fiyatın kaç günlük olduğu (`price_updated_at`'ten; taban fiyatta toplandığı tarih)."""
    stamp = feed.get("price_updated_at")
    if not stamp:
        return None
    return max((datetime.now() - datetime.fromisoformat(stamp)).days, 0)


def call_optimization_api(payload: Dict) -> Optional[Dict]:
    """Deterministik optimizasyon uç noktasını çağır.

    LLM devrede DEĞİLDİR: seçilen yemler ve kısıtlar birebir solver'a gider.
    """
    try:
        response = requests.post(f"{BACKEND_URL}/optimize", json=payload, timeout=60)
        if response.status_code == 200:
            return response.json()
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        st.error(f"Optimizasyon hatası: {detail}", icon=":material/error:")
        return None
    except Exception as exc:
        st.error(f"API bağlantı hatası: {exc}", icon=":material/error:")
        return None


# ============================
# İSTEK GÖVDESİ
# ============================

def build_optimization_payload(
    selected_feeds: List[str],
    animal: Dict,
    targets: Dict,
    use_all_feeds: bool,
    min_inclusion_pct_dm: float,
    price_overrides: Optional[Dict[str, float]] = None,
    dm_range: Optional[Tuple[float, float]] = None,
) -> Dict:
    """POST /api/v1/optimize için istek gövdesi üretir.

    Hayvan profili her zaman gider: backend hedefleri (ME + HP) ve grubun rasyon
    kurallarını ondan türetir. Hedefler yalnızca elle ayarlandıysa açıkça gönderilir
    ve profilden gelenlerin önüne geçer. Arayüz enerjiyi kcal/kg KM tutar, backend
    Mcal/kg KM bekler (÷ 1000).

    ⚠️ BİRİM DÖNÜŞÜMÜ: Arayüz Ca ve P'yi rasyon YÜZDESİ olarak toplar (nutrisyonistlerin
    alıştığı gösterim), backend ise g/kg KM bekler. %1 KM = 10 g/kg KM olduğundan
    burada 10 ile çarpılır. Bu dönüşüm atlanırsa hedefler 10 kat düşük gider ve
    Ca:P oranı hiçbir zaman tutmaz.
    """
    payload = {
        "animal": animal,
        "body_weight_kg": animal["body_weight_kg"],
        "selected_feed_names": selected_feeds,
        "use_all_selected_feeds": use_all_feeds,
        "min_inclusion_pct_dm": min_inclusion_pct_dm,
    }
    if targets["manual"]:
        payload.update({
            "crude_protein_min": targets["cp_min"],
            "crude_protein_max": targets["cp_max"],
            "metabolic_energy_min": round(targets["me_min"] / 1000.0, 3),
            "metabolic_energy_max": round(targets["me_max"] / 1000.0, 3),
            # % KM -> g/kg KM
            "calcium_min": round(targets["ca_min"] * 10.0, 2),
            "calcium_max": round(targets["ca_max"] * 10.0, 2),
            "phosphorus_min": round(targets["p_min"] * 10.0, 2),
            "phosphorus_max": round(targets["p_max"] * 10.0, 2),
            "target_dm_intake_kg": targets["dm_intake_kg"],
        })

    # Kuru madde aralığı isteğe bağlı. Varsayılan olarak GÖNDERİLMEZ: silajlı bir
    # rasyon asla %85 KM olamaz, sabit bir aralık modeli çözümsüz bırakır.
    if dm_range:
        payload["dry_matter_min"], payload["dry_matter_max"] = dm_range

    if price_overrides:
        payload["feed_price_overrides"] = {
            name: float(price) for name, price in price_overrides.items() if price and price > 0
        }

    return payload


# ============================
# ÖNERİLER — her modun sonucunun altında (backend: advisor.py)
# ============================

_ADVICE_STYLE = {
    "saglik": (":material/health_and_safety:", "Sağlık"),
    "ekonomi": (":material/payments:", "Ekonomi"),
    "eksik": (":material/arrow_upward:", "Eksik"),
    "fazla": (":material/arrow_downward:", "Fazla"),
    "sinir": (":material/block:", "Sınır"),
    "ucuz": (":material/savings:", "Ucuzlatma"),
}


def _feed_box(feed_type: Optional[str]) -> str:
    if feed_type in FORAGE_TYPES:
        return "forage"
    return "mineral" if feed_type in MINERAL_TYPES else "concentrate"


def _apply_advice(keys: Dict[str, str], add: List[str], remove: List[str],
                  feed_types: Dict[str, str], flag: str) -> None:
    """"Öneriyi uygula": seçim kutularını değiştirir, sonraki çalıştırmada yeniden hesaplatır.

    Geri çağırma (on_click) içinde çalışır: widget'ın değeri ancak orada değiştirilebilir.
    """
    for key in keys.values():
        if key in st.session_state:
            st.session_state[key] = [n for n in st.session_state[key] if n not in remove]
    for name in add:
        key = keys.get(_feed_box(feed_types.get(name)))
        if key is None:
            continue
        current = list(st.session_state.get(key) or [])
        if name not in current:
            st.session_state[key] = current + [name]
    st.session_state[flag] = True


def render_advice(advice: List[Dict], keys: Optional[Dict[str, str]], flag: str) -> None:
    """Öneri listesi; yem ekleyen/çıkaran önerinin yanında "Öneriyi uygula" düğmesi.

    `keys` None ise (sohbetteki sonuç) düğme gösterilmez — seçim kutuları o ekranda değil.
    """
    if not advice:
        return
    feed_types = {f["name"]: f.get("feed_type") for f in fetch_feeds()}
    st.markdown("**Öneriler**")
    for i, item in enumerate(advice):
        icon, label = _ADVICE_STYLE.get(item["kind"], (":material/lightbulb:", "Öneri"))
        with st.container(border=True, horizontal=True, vertical_alignment="center"):
            st.markdown(f"{icon} **{label}** — {item['text']}", width="stretch")
            if keys and (item.get("add") or item.get("remove")):
                st.button("Uygula", key=f"{flag}_apply_{i}", icon=":material/done:",
                          on_click=_apply_advice,
                          args=(keys, item.get("add") or [], item.get("remove") or [], feed_types, flag),
                          help="Önerilen yemi seçime ekler/çıkarır ve yeniden hesaplar.")


# ============================
# SONUÇ EKRANI
# ============================

def render_results(result: Dict, targets: Dict, advice_keys: Optional[Dict[str, str]] = None) -> None:
    """Optimizasyon sonucunu sade bir özet + kapalı detay bölümü olarak göster."""

    solver_details = result.get("solver_details") or {}
    nutrition_mode = solver_details.get("nutrition_mode")
    status = result.get("status", "Bilinmiyor")

    st.subheader("Rasyon sonucu")

    if nutrition_mode == "hard":
        st.success(
            "Besin ihtiyaçları ve rasyon kuralları sağlandı"
            + (" (enerji ihtiyacın üstünde, aşağıya bak)"
               if solver_details.get("energy_surplus") and "expected_gain_kg" not in solver_details
               else "")
            + " — bu, hedefleri karşılayan en ucuz rasyon.",
            icon=":material/check_circle:",
        )
    elif nutrition_mode == "relaxed":
        st.warning(
            "Besin hedeflerinin tamamı aynı anda tutturulamadı. Hangi hedefin neden "
            "tutmadığını Detaylar bölümünde bulabilirsin.",
            icon=":material/warning:",
        )
    elif result.get("success"):
        st.info(f"Durum: {status}", icon=":material/info:")
    else:
        st.error(
            f"Çözüm bulunamadı ({status}). Hedefleri gevşetmeyi veya daha fazla yem "
            "seçmeyi dene.",
            icon=":material/error:",
        )

    dropped = solver_details.get("dropped_feeds")
    if dropped:
        st.warning(
            f"Şu yem(ler) besin hedefleriyle çeliştiği için rasyona giremedi: "
            f"**{', '.join(dropped)}**.",
            icon=":material/remove_circle:",
        )
    excluded = solver_details.get("excluded_feeds")
    if excluded:
        st.info(
            f"Bu grupta verilmediği için rasyondan çıkarıldı: **{', '.join(excluded)}** "
            f"({solver_details.get('excluded_reason', '')}).",
            icon=":material/block:",
        )
    # Enerji fazlası gevşetme değildir: seçilen yemlerle rasyon daha düşük enerjili olamıyor.
    # Besi sığırı ve kuzuda aynı bilgi "beklenen artış" olarak gelir.
    for warning in result.get("warnings") or []:
        if warning.startswith("Enerji fazlası"):
            st.warning(warning, icon=":material/scale:")
        elif warning.startswith("Beklenen artış"):
            st.info(warning, icon=":material/trending_up:")

    recommendations = [
        rec for rec in result.get("feed_recommendations", [])
        if rec.get("amount_kg_per_day", 0) > 0.001
    ]
    daily_intake = result.get("daily_intake") or {}
    nutrient_content = result.get("nutrient_content") or {}

    # Kuzu rasyonu gram mertebesinde: kg ile tuz ve premiks "0.00" görünürdü.
    small = (daily_intake.get("dry_matter_kg") or 0) < 5
    amount_col = "g/gün" if small else "kg/gün"

    # --- Maliyet özeti ---
    cost_per_day = result.get("total_cost_per_day", 0) or 0
    with st.container(horizontal=True):
        st.metric("Günlük maliyet", f"{cost_per_day:,.2f} TL", border=True)
        st.metric("Aylık maliyet", f"{cost_per_day * 30:,.0f} TL", border=True)
        st.metric(
            "Günlük kuru madde",
            f"{daily_intake.get('dry_matter_kg', 0):.{2 if small else 1}f} kg",
            border=True,
        )

    if not recommendations:
        st.caption("Rasyona giren yem yok.")
        return

    # --- Günlük rasyon ---
    recipe_df = pd.DataFrame([
        {
            "Yem": rec["name"],
            "Oran (%)": round(rec["proportion"] * 100, 1),
            amount_col: round(rec["amount_kg_per_day"] * (1000 if small else 1), 0 if small else 2),
            "TL/gün": round(rec["cost_per_day"], 2),
        }
        for rec in recommendations
    ]).sort_values("Oran (%)", ascending=False)

    st.markdown("**Günlük rasyon**")
    st.dataframe(
        recipe_df,
        column_config={
            "Oran (%)": st.column_config.ProgressColumn(
                "Oran", min_value=0, max_value=100, format="%.1f%%"
            ),
            amount_col: st.column_config.NumberColumn(format="%.0f" if small else "%.2f"),
            "TL/gün": st.column_config.NumberColumn(format="%.2f"),
        },
        hide_index=True,
        width="stretch",
    )

    donut_df = recipe_df[["Yem", "Oran (%)"]].rename(columns={"Oran (%)": "Oran"})
    donut = (
        alt.Chart(donut_df)
        .mark_arc(innerRadius=60)
        .encode(
            theta=alt.Theta("Oran:Q", stack=True),
            color=alt.Color("Yem:N", legend=alt.Legend(title=None)),
            tooltip=["Yem:N", "Oran:Q"],
        )
        .properties(height=280)
    )
    st.altair_chart(donut, width="stretch")

    # --- Besin kontrolü ---
    # ⚠️ Arayüz Ca/P'yi % KM tutar, sonuç g/kg KM gelir -> karşılaştırmada x10.
    # Sınırı olmayan tarafta None: MP'nin üst, nişastanın alt sınırı yok.
    checks = [
        ("Ham protein", nutrient_content.get("crude_protein_percent_dm", 0),
         targets["cp_min"], targets["cp_max"], "% KM", 1),
        # Enerji kcal/kg KM gösterilir (rasyon notlarındaki birim); sonuç Mcal gelir.
        ("ME (kcal)", (nutrient_content.get("metabolic_energy_mcal_per_kg_dm") or 0) * 1000,
         targets["me_min"], targets["me_max"], "kcal/kg KM", 0),
        ("Kalsiyum", nutrient_content.get("calcium_g_per_kg_dm", 0),
         targets["ca_min"] * 10, targets["ca_max"] * 10, "g/kg KM", 1),
        ("Fosfor", nutrient_content.get("phosphorus_g_per_kg_dm", 0),
         targets["p_min"] * 10, targets["p_max"] * 10, "g/kg KM", 1),
        # Rasyonun yenebilirliğini gösteren iki asıl kontrol: lif ve kaba yem payı.
        # Pencereler gruba göre değişir (kuru inekte NDF %40-55), backend'den gelir.
        ("NDF", nutrient_content.get("ndf_percent_dm", 0),
         targets["ndf_min"], targets["ndf_max"], "% KM", 1),
        ("Kaba yem", nutrient_content.get("forage_ratio_percent", 0),
         targets["forage_min"], targets["forage_max"], "% KM", 1),
        # NDF lifi söyler ama asidozu yakalamaz; nişasta tavanı onu tutar.
        ("Nişasta", nutrient_content.get("starch_percent_dm") or 0,
         None, targets["starch_max"], "% KM", 1),
    ]

    # Besi gruplarında NDF ve nişasta penceresi yok (backend 0-100 gönderir): kart gösterme.
    # Yalnız alt sınırı olmayan kartlar elenir — ME'nin tavanı kcal cinsinden de 100'ü aşar.
    checks = [c for c in checks if not (c[3] is not None and c[3] >= 100 and not c[2])]
    st.markdown("**Besin kontrolü**")
    with st.container(horizontal=True):
        for label, value, low, high, unit, digits in checks:
            if high is None:
                target_text = f"en az {low:.{digits}f} {unit}"
            elif low is None:
                target_text = f"en fazla {high:.{digits}f} {unit}"
            else:
                target_text = f"{low:.{digits}f}–{high:.{digits}f} {unit}"
            # Gösterilen değer yuvarlandığı için kıyas da 0.5 hane toleranslı.
            tol = 0.5 * 10 ** -digits
            in_range = (low is None or value >= low - tol) and (high is None or value <= high + tol)
            with st.container(border=True):
                st.metric(label, f"{value:.{digits}f}", help=f"Hedef: {target_text}")
                if in_range:
                    st.badge("Hedefte", icon=":material/check:", color="green")
                else:
                    st.badge("Aralık dışı", icon=":material/priority_high:", color="orange")

    render_advice(result.get("advice") or [], advice_keys, "auto_calc_ration")
    if advice_keys:
        render_ask_box("ration", result)

    # --- Detaylar ---
    with st.expander("Detaylar", icon=":material/expand_more:"):
        explanations = result.get("turkish_explanations") or []
        warnings = result.get("warnings") or []
        if explanations:
            st.markdown("**Gevşetilen kısıtlamalar**")
            for item in explanations:
                st.markdown(f"- {item}")
        if warnings:
            st.markdown("**Uyarılar**")
            for item in warnings:
                st.markdown(f"- {item}")

        ca_p_ratio = nutrient_content.get("ca_p_ratio") or 0
        st.markdown(
            f"**Ca:P oranı:** {ca_p_ratio:.2f} "
            f"(hedef {targets['ca_p_min']}–{targets['ca_p_max']}) &nbsp;|&nbsp; "
            f"**Rasyon kuru maddesi:** {nutrient_content.get('dry_matter_percent', 0):.1f}% "
            "(doğal hali) &nbsp;|&nbsp; "
            f"**Kaba yem NDF'si:** {nutrient_content.get('forage_ndf_percent_dm', 0):.1f}% KM"
        )
        st.markdown(
            f"**Ham yağ:** %{nutrient_content.get('ether_extract_percent_dm') or 0:.1f} KM "
            f"(en fazla %{targets['fat_max']:.0f}) &nbsp;|&nbsp; "
            f"**Su ihtiyacı:** ~{targets['water_l_day']:.0f} L/gün"
        )

        constraints = result.get("constraints_status") or []
        if constraints:
            st.markdown("**Kısıtlama durumu**")
            st.dataframe(
                pd.DataFrame([
                    {
                        "Kısıtlama": cs.get("constraint", "").replace("_", " ").capitalize(),
                        "Gerçekleşen": round(cs.get("actual", 0), 2),
                        "Birim": cs.get("unit", ""),
                        "Durum": cs.get("status", ""),
                        "Açıklama": cs.get("turkish_explanation") or "-",
                    }
                    for cs in constraints
                ]),
                hide_index=True,
                width="stretch",
            )

        if daily_intake:
            # Rasyon notlarındaki gibi günlük toplamlar: ihtiyaç ve rasyonun verdiği.
            st.markdown("**Günlük ihtiyaç ve alım**")
            st.dataframe(
                pd.DataFrame([
                    {"Besin maddesi": "Metabolik enerji (kcal)",
                     "İhtiyaç": f"{targets['me_day'] * 1000:,.0f}",
                     "Rasyon": f"{daily_intake.get('metabolic_energy_mcal', 0) * 1000:,.0f}"},
                    {"Besin maddesi": "Ham protein (g)",
                     "İhtiyaç": f"{targets['cp_day']:,.0f}",
                     "Rasyon": f"{daily_intake.get('crude_protein_kg', 0) * 1000:,.0f}"},
                    {"Besin maddesi": "Kalsiyum (g)",
                     "İhtiyaç": f"{targets['ca_day']:.0f}",
                     "Rasyon": f"{daily_intake.get('calcium_g', 0):.0f}"},
                    {"Besin maddesi": "Fosfor (g)",
                     "İhtiyaç": f"{targets['p_day']:.0f}",
                     "Rasyon": f"{daily_intake.get('phosphorus_g', 0):.0f}"},
                    {"Besin maddesi": "Kuru madde (kg)",
                     "İhtiyaç": f"{targets['dm_intake_kg']:.1f}",
                     "Rasyon": f"{daily_intake.get('dry_matter_kg', 0):.1f}"},
                ]),
                hide_index=True,
                width="stretch",
            )

        if solver_details:
            st.markdown("**Solver detayları**")
            st.json(solver_details, expanded=False)


def render_price_editor(selected_feeds: List[str], feeds_data: List[Dict],
                        body_weight: Optional[float] = None,
                        cap_exempt: tuple = ()) -> Dict[str, float]:
    """Seçilen yemlerin fiyat tablosu (düzenlenebilir) + kaydet/sil düğmeleri.

    Döndürdüğü sözlük, kullanıcının tabloda yazdığı fiyatlardır (ad -> TL/kg). Canlı
    ağırlık verilmezse (karma yem) sağlık sınırı sütunu gösterilmez.
    """
    feeds_by_name = {feed["name"]: feed for feed in feeds_data}

    small = bool(body_weight) and body_weight < 100   # kuzu: sınırlar gram mertebesinde

    def _health_cap(feed: Dict) -> Optional[float]:
        """Sağlık sınırını canlı ağırlığa ölçekler (tablo değeri 600 kg için)."""
        cap = feed.get("max_kg_per_day")
        if not cap or feed["name"] in cap_exempt:
            return None
        return round(cap * (body_weight / 600.0), 3 if small else 1)

    rows = []
    for name in selected_feeds:
        feed = feeds_by_name[name]
        row = {
            "Yem": name,
            "Fiyat (TL/kg)": round(feed.get("price_per_kg", 0) or 0, 2),
            "Kaynak": feed.get("price_source") or "taban fiyat",
            "Kaç günlük": price_age_days(feed),
        }
        if body_weight:
            row["Max kg/gün"] = _health_cap(feed)
        # Yem tablosundaki gibi DOĞAL halde: API KM bazında verir, KM oranıyla çarpılır.
        row["HP (%)"] = round((feed.get("crude_protein") or 0) * (feed.get("dry_matter") or 0) / 100, 1)
        row["ME (kcal/kg)"] = round((feed.get("metabolic_energy") or 0) * (feed.get("dry_matter") or 0) * 10)
        rows.append(row)
    price_df = pd.DataFrame(rows)
    # None'lu sütun "object" tipinde kalır ve hücrede "None" yazar; sayıya çevrilince boş görünür.
    for column in ("Max kg/gün", "Kaç günlük"):
        if column in price_df:
            price_df[column] = pd.to_numeric(price_df[column])

    edited_prices = st.data_editor(
        price_df,
        column_config={
            "Yem": st.column_config.TextColumn(width="medium"),
            "Fiyat (TL/kg)": st.column_config.NumberColumn(
                min_value=0.0, max_value=200.0, step=0.10, format="%.2f",
                help="Kendi alım fiyatını girebilirsin.",
            ),
            "Kaynak": st.column_config.TextColumn(
                width="medium",
                help="Canlı fiyatlarda hesap açıkça yazar: 'piyasa 3.50 + teslim 1.20' = "
                     "piyasa ortalaması + çiftliğe nakliye/yükleme. Silaj ve mineraller "
                     "piyasada izlenmediği için taban fiyatta kalır.",
            ),
            "Kaç günlük": st.column_config.NumberColumn(
                format="%d gün",
                help=f"Fiyatın güncellendiği tarihten bu yana geçen süre; "
                     f"{STALE_PRICE_DAYS} günden eskisi uyarı verir.",
            ),
            "Max kg/gün": st.column_config.NumberColumn(
                format="%.3f" if small else "%.1f",
                help="Hayvan sağlığı sınırı — canlı ağırlığa göre ölçeklenmiş günlük azami "
                     "miktar. Boş ise as-fed sınırı yok. Bu sınır hiçbir koşulda aşılmaz.",
            ),
            "HP (%)": st.column_config.NumberColumn(
                format="%.1f", help="Ham protein, yemin doğal (yaş) hali için."),
            "ME (kcal/kg)": st.column_config.NumberColumn(
                format="%d", help="Metabolik enerji, yemin doğal (yaş) hali için."),
        },
        disabled=[c for c in price_df.columns if c != "Fiyat (TL/kg)"],
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        # Anahtar seçime bağlı: seçim değişince tablo satırları kayar ve eski
        # düzenlemeler yanlış yeme uygulanabilirdi.
        key="price_editor_" + "|".join(selected_feeds),
    )
    price_overrides = dict(zip(edited_prices["Yem"], edited_prices["Fiyat (TL/kg)"]))

    # Mineraller rasyon maliyetinin küçük bir kısmı ve fiyatları yavaş değişir: uyarıyı
    # kalabalıklaştırmasınlar.
    stale = [name for name in selected_feeds
             if feeds_by_name[name].get("feed_type") not in MINERAL_TYPES
             and (price_age_days(feeds_by_name[name]) or 0) > STALE_PRICE_DAYS]
    if stale:
        st.warning(
            f"Şu yemlerin fiyatı {STALE_PRICE_DAYS} günden eski: **{', '.join(stale)}**. "
            "Kendi alış fiyatını tabloya yazıp kaydet ya da güncel piyasa fiyatlarını çek.",
            icon=":material/schedule:",
        )

    changed = [
        {"name": name, "price_per_kg": float(price)}
        for name, price in price_overrides.items()
        if price and abs(price - (feeds_by_name[name].get("price_per_kg") or 0)) > 0.005
    ]
    has_saved = any(feed.get("price_source") == "elle girildi" for feed in feeds_data)
    with st.container(horizontal=True):
        if st.button(
            f"Fiyatlarımı kaydet ({len(changed)})", icon=":material/save:",
            disabled=not changed,
            help="Tabloda değiştirdiğin fiyatlar kaydedilir; sonraki hesaplarda da kullanılır "
                 "ve canlı senkron bunları ezmez.",
        ):
            post_prices("feeds/sync-prices", "Fiyatlar kaydediliyor...", body=changed, timeout=30)
        if has_saved and st.button(
            "Kaydettiğim fiyatları sil", icon=":material/restart_alt:",
            help="Kaydettiğin bütün fiyatlar taban fiyata döner; sonra güncel fiyatları "
                 "çekebilirsin.",
        ):
            post_prices("feeds/reset-prices", "Kayıtlı fiyatlar siliniyor...", timeout=30)
    return price_overrides


def render_price_sync() -> None:
    """Kenar çubuğundaki canlı fiyat düğmesi ve son senkronun mesajı (iki modda da)."""
    if st.button("Güncel piyasa fiyatlarını çek", icon=":material/refresh:", width="stretch",
                 help="Orta-büyük bir işletmenin ortalama TESLİM alış fiyatı: borsa ve piyasa "
                      "ortalaması (TOBB, Samsun TB, hayvanci.com) + nakliye/yükleme farkı; "
                      "kesim (karkas) fiyatları UKON'dan. Backend bunu "
                      "günde bir kez kendisi de yapar. Kendi kaydettiğin fiyatlara dokunulmaz."):
        post_prices("feeds/sync-prices", "Borsa fiyatları çekiliyor (~1 dk)...", timeout=180)
    # Senkron sonucu rerun'dan sonra gösterilir; canlı/taban ayrımını net söyler.
    sync_message = st.session_state.pop("price_sync_message", None)
    if sync_message:
        st.toast(sync_message, icon=":material/sell:")
        st.caption(sync_message)


# ============================
# KARMA YEM (konsantre) — 1 ton için en ucuz karışım
# ============================

# Karma yeme kaba yem girmez; mineraller ve premiks varsayılan seçili gelir.
COMPOUND_INGREDIENT_TYPES = ("grain", "protein", "byproduct", "fat", "npn")
DEFAULT_COMPOUND_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz",
                             "Vitamin-Mineral Premiks")


@st.cache_data(ttl=3600)
def fetch_compound_types() -> List[Dict]:
    """Kullanıcının HP → ME tablosundaki karma yem tipleri (hedefler doğal halde)."""
    try:
        response = requests.get(f"{BACKEND_URL}/compound-feed/types", timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        st.error(f"Karma yem tipleri çekilemedi: {exc}", icon=":material/error:")
        return []


def render_compound_result(result: Dict, actionable: bool = True) -> None:
    """Defterdeki gibi: hammadde, kg, HP ve ME katkısı; altta toplamlar ve hedef kontrolü."""
    target = result["target"]
    mode = result["mode"]
    st.subheader("Karma yem sonucu")
    if mode == "hard":
        st.success("Tüm hedefler sağlandı — bu, hedefi tutturan en ucuz karışım.",
                   icon=":material/check_circle:")
    elif mode == "relaxed":
        st.warning("Seçilen hammaddelerle hedeflerin hepsi tutmadı; en yakın karışım gösteriliyor.",
                   icon=":material/warning:")
    else:
        st.error("Bu hammaddelerle karışım kurulamıyor.", icon=":material/error:")
    for note in result["notes"][1:]:
        st.caption(note)
    if not result["lines"]:
        return

    batch = result["batch_kg"]
    with st.container(horizontal=True):
        st.metric(f"{batch:,.0f} kg maliyeti", f"{result['cost_total']:,.0f} TL", border=True)
        st.metric("Kilo maliyeti", f"{result['cost_per_kg']:.2f} TL/kg", border=True)

    rows = [{"Hammadde": line["name"], "Miktar (kg)": line["amount_kg"],
             "Oran (%)": line["share_pct"], "HP": line["cp_points"],
             "ME (kcal)": line["me_kcal"], "TL": line["cost"]} for line in result["lines"]]
    rows.append({"Hammadde": "TOPLAM", "Miktar (kg)": batch, "Oran (%)": 100.0,
                 "HP": result["cp_pct"], "ME (kcal)": result["me_kcal"],
                 "TL": result["cost_total"]})
    st.dataframe(
        pd.DataFrame(rows),
        column_config={
            "Miktar (kg)": st.column_config.NumberColumn(format="%.1f"),
            "Oran (%)": st.column_config.NumberColumn(format="%.1f"),
            "HP": st.column_config.NumberColumn(
                format="%.2f", help="Karışımın HP'sine katkısı (puan, 1 kg karma yemde %)."),
            "ME (kcal)": st.column_config.NumberColumn(
                format="%.0f", help="Karışımın 1 kg'ındaki ME'ye katkısı."),
            "TL": st.column_config.NumberColumn(format="%.2f"),
        },
        hide_index=True,
        width="stretch",
    )

    checks = [
        ("Ham protein (%)", result["cp_pct"], target["cp_min"], target["cp_max"], 1),
        ("ME (kcal/kg)", result["me_kcal"], target["me_min"], target["me_max"], 0),
        ("Kalsiyum (%)", result["ca_pct"], target["ca_min"], target["ca_max"], 2),
        ("Fosfor (%)", result["p_pct"], target["p_min"], None, 2),
        ("Tuz (%)", result["salt_pct"], 0.5, 1.0, 2),
    ]
    st.markdown("**Hedef kontrolü** (1 kg karma yemde, doğal hal)")
    with st.container(horizontal=True):
        for label, value, low, high, digits in checks:
            target_text = (f"en az {low:.{digits}f}" if high is None
                           else f"{low:.{digits}f}–{high:.{digits}f}")
            tol = 0.5 * 10 ** -digits
            ok = value >= low - tol and (high is None or value <= high + tol)
            with st.container(border=True):
                st.metric(label, f"{value:.{digits}f}", help=f"Hedef: {target_text}")
                if ok:
                    st.badge("Hedefte", icon=":material/check:", color="green")
                else:
                    st.badge("Aralık dışı", icon=":material/priority_high:", color="orange")
    st.caption("Ca ve P aralıkları taslaktır: üretici etiketlerindeki değerlerden alındı.")
    render_advice(result.get("advice") or [],
                  {"concentrate": "compound_ingredients", "mineral": "compound_minerals"} if actionable else None,
                  "auto_calc_compound")
    if actionable:
        render_ask_box("compound", result)


def render_compound_feed_page() -> None:
    """Karma yem modu: tip + hammaddeler → 1 ton (veya seçilen miktar) için en ucuz karışım."""
    types = fetch_compound_types()
    if not types:
        st.stop()
    types_by_key = {t["key"]: t for t in types}

    with st.sidebar:
        st.subheader("Karma yem")
        type_key = st.selectbox(
            "Yem tipi", options=list(types_by_key),
            format_func=lambda key: types_by_key[key]["label"],
            help="Hedefler rasyon notlarındaki HP → ME tablosundan gelir (doğal hal).",
            key="compound_type",
        )
        target = types_by_key[type_key]
        custom = st.toggle("Özel hedef", value=False,
                           help="HP ve ME hedefini kendin gir; Ca/P seçili tipten gelir.")
        if custom:
            cp_min = st.number_input("Ham protein (%)", 8.0, 40.0, float(target["cp_min"]), 0.5)
            col_a, col_b = st.columns(2)
            with col_a:
                me_min = st.number_input("Min ME (kcal/kg)", 1800, 3400, int(target["me_min"]), 50)
            with col_b:
                me_max = st.number_input("Max ME (kcal/kg)", 1800, 3600, int(target["me_max"]), 50)
            overrides = {"cp_min": cp_min, "me_min": me_min, "me_max": max(me_max, me_min)}
            cp_text, me_text = f"%{cp_min:g}–{cp_min + 0.5:g}", f"{me_min}–{max(me_max, me_min)}"
        else:
            overrides = {}
            cp_text = f"%{target['cp_min']:g}–{target['cp_max']:g}"
            me_text = f"{target['me_min']:.0f}–{target['me_max']:.0f}"
        with st.container(horizontal=True):
            st.metric("HP", cp_text, border=True)
            st.metric("ME (kcal/kg)", me_text, border=True)
        st.caption(f"Ca %{target['ca_min']}–{target['ca_max']} · P en az %{target['p_min']}"
                   + (" · besi yemi: soya küspesi yok, dane:küspe ≥ 6:1"
                      if target["fattening"] else ""))
        batch_kg = st.number_input("Karışım miktarı (kg)", 100.0, 50000.0, 1000.0, 100.0,
                                   key="compound_batch")
        render_price_sync()

    page_header("Karma yem", "Hedef protein ve enerjiyi tutturan en ucuz karışım (1 ton ya da istediğin miktar).")

    feeds_data = fetch_feeds()
    if not feeds_data:
        st.error("Yem verileri yüklenemedi. Backend çalışıyor mu?", icon=":material/error:")
        st.stop()
    ingredient_options = [f["name"] for f in feeds_data if f.get("feed_type") in COMPOUND_INGREDIENT_TYPES]
    mineral_options = [f["name"] for f in feeds_data if f.get("feed_type") in MINERAL_TYPES]

    st.subheader("Hammadde seçimi")
    selected_ingredients = st.multiselect(
        "Tahıl, küspe, kepek", options=ingredient_options, placeholder="Mısır, arpa, küspe…",
        key="compound_ingredients",
    )
    selected_minerals = st.multiselect(
        "Mineral ve katkılar", options=mineral_options,
        default=[n for n in DEFAULT_COMPOUND_MINERALS if n in mineral_options],
        help="Premiks tonda 1 kg sabit; tuz %0.5–1; mermer tozu ve DCP'yi Ca/P hedefi belirler.",
        key="compound_minerals",
    )
    selected = selected_ingredients + selected_minerals

    price_overrides: Dict[str, float] = {}
    if selected:
        price_overrides = render_price_editor(selected, feeds_data)
    else:
        st.info("Başlamak için hammadde seç.", icon=":material/info:")

    if st.button("Karma yemi hesapla", type="primary", icon=":material/calculate:",
                 width="stretch", disabled=not selected_ingredients) \
            or (st.session_state.pop("auto_calc_compound", False) and selected_ingredients):
        payload = {"type_key": type_key, "selected_feed_names": selected, "batch_kg": batch_kg,
                   "with_advice": True,
                   "feed_price_overrides": {n: float(p) for n, p in price_overrides.items() if p and p > 0},
                   **overrides}
        with st.spinner("Karışım hesaplanıyor..."):
            try:
                response = requests.post(f"{BACKEND_URL}/compound-feed", json=payload, timeout=60)
            except Exception as exc:
                st.error(f"API bağlantı hatası: {exc}", icon=":material/error:")
                response = None
        if response is not None and response.status_code == 200:
            st.session_state["compound_result"] = {
                **response.json(),
                "request": {"type_key": type_key, "selected_feed_names": selected, "batch_kg": batch_kg}}
        elif response is not None:
            st.error(f"Hesaplama hatası: {response.text}", icon=":material/error:")
            st.session_state.pop("compound_result", None)

    if "compound_result" in st.session_state:
        render_compound_result(st.session_state["compound_result"])


# ============================
# BESİ EKONOMİSİ — bir besi döneminin maliyeti, geliri ve kârı
# ============================

ECON_SPECIES = {"beef": "Besi sığırı", "lamb": "Küçükbaş"}
# Yem maliyeti besi ortası ağırlığının rasyonundan; randıman ve ölüm varsayılanları
# backend'deki economics.py ile aynı. Yalnız kesim fiyatı var: besici eti kesimhaneye satar.
ECON_DEFAULTS = {
    "beef": {"weight": 220.0, "gain": 1.5, "days": 270, "dressing": 55.0, "mortality": 1.5,
             "carcass_key": "dana_karkas"},
    "lamb": {"weight": 30.0, "gain": 0.315, "days": 120, "dressing": 45.0, "mortality": 3.0,
             "carcass_key": "kuzu_karkas"},
}


@st.cache_data(ttl=300)
def fetch_market_prices() -> Dict[str, Dict]:
    """Kesim (karkas) fiyatları, bölgeler dahil (anahtar -> satır)."""
    try:
        response = requests.get(f"{BACKEND_URL}/market-prices", timeout=10)
        response.raise_for_status()
        return {row["key"]: row for row in response.json()}
    except Exception as exc:
        st.error(f"Kesim fiyatları çekilemedi: {exc}", icon=":material/error:")
        return {}


def _price_caption(row: Dict) -> str:
    age = price_age_days(row)
    return f"{row['price_source']}" + (f" · {age} günlük" if age is not None else "")


def render_economics_result(result: Dict, actionable: bool = True) -> None:
    """Kâr özeti, kalem kalem maliyet tablosu ve besi ortası rasyonu."""
    per_head, herd = result["per_head"], result["herd"]
    lamb = result["species"] == "lamb"
    st.subheader("Besi ekonomisi sonucu")
    for warning in result["warnings"]:
        st.warning(warning, icon=":material/warning:")

    if per_head["profit"] >= 0:
        st.success(f"Bu fiyatlarla besi kâr ediyor: baş başına **{per_head['profit']:,.0f} TL**.",
                   icon=":material/trending_up:")
    else:
        st.error(f"Bu fiyatlarla besi zarar ediyor: baş başına **{per_head['profit']:,.0f} TL**.",
                 icon=":material/trending_down:")

    with st.container(horizontal=True):
        st.metric("Kâr / baş", f"{per_head['profit']:,.0f} TL", border=True)
        st.metric(f"Sürü kârı ({result['head_count']} baş)", f"{herd['profit']:,.0f} TL", border=True)
        st.metric("Başabaş kesim fiyatı", f"{result['breakeven_carcass_price']:,.1f} TL/kg", border=True,
                  help="Kesim fiyatı bunun altına düşerse besi zarar eder.")
    with st.container(horizontal=True):
        st.metric("Günlük kâr / baş", f"{result['profit_per_day']:,.1f} TL", border=True)
        st.metric("1 kg canlı artışın maliyeti", f"{result['cost_per_kg_gain']:,.1f} TL",
                  border=True, help="Yem + diğer giderler ÷ kazanılan kilo (alış bedeli hariç).")
        st.metric("Yemden yararlanma", f"{result['feed_conversion']:.2f}", border=True,
                  help="1 kg canlı ağırlık artışı için tüketilen kuru madde (kg).")

    rows = [("Hayvan alışı", per_head["purchase"], herd["purchase"]),
            ("Yem", per_head["feed"], herd["feed"])]
    rows += [(name, value, herd["other"][name]) for name, value in per_head["other"].items()]
    rows += [("TOPLAM MALİYET", per_head["total_cost"], herd["total_cost"]),
             ("Gelir (kesimhaneye satış)", per_head["revenue"], herd["revenue"]),
             ("KÂR", per_head["profit"], herd["profit"])]
    rows = [(name, round(head), round(total)) for name, head, total in rows]
    st.dataframe(
        pd.DataFrame(rows, columns=["Kalem", "Baş başına (TL)", "Sürü (TL)"]),
        column_config={"Baş başına (TL)": st.column_config.NumberColumn(format="localized"),
                       "Sürü (TL)": st.column_config.NumberColumn(format="localized")},
        hide_index=True, width="stretch",
    )
    prices = result["prices"]
    st.caption(
        f"{result['start_weight_kg']:g} kg → **{result['end_weight_kg']:.1f} kg** "
        f"({result['days']} gün, {result['total_gain_kg']:.1f} kg artış) · karkas "
        f"{result['carcass_weight_kg']:.1f} kg (randıman %{result['dressing_pct']:g})."
    )
    st.caption(
        f"Fiyatlar: alış {prices['purchase']['price_per_kg']:,.1f} "
        f"({prices['purchase']['source']}), kesim {prices['carcass']['price_per_kg']:,.1f} "
        f"TL/kg ({prices['carcass']['source']}) · baş başına toplam yem {result['feed_asfed_kg_total']:,.0f} kg doğal "
        f"({result['feed_dm_kg_total']:,.0f} kg KM)."
    )

    species = result["species"]
    render_advice(result.get("advice") or [],
                  {"forage": f"econ_forage_{species}", "concentrate": f"econ_conc_{species}",
                   "mineral": f"econ_minerals_{species}"} if actionable else None, "auto_calc_econ")
    if actionable:
        render_ask_box("economics", result)

    ration = result["ration"]
    with st.expander(f"Besi ortası rasyonu ({result['mid_weight_kg']:.1f} kg) — "
                     f"{result['feed_cost_per_day']:.2f} TL/gün", icon=":material/restaurant:"):
        factor, unit = (1000, "g/gün") if lamb else (1, "kg/gün")
        recipe = [{"Yem": rec["name"], unit: round(rec["amount_kg_per_day"] * factor, 0 if lamb else 2),
                   "TL/gün": round(rec["cost_per_day"], 2)}
                  for rec in ration["feed_recommendations"] if rec["amount_kg_per_day"] > 0.0005]
        st.dataframe(pd.DataFrame(recipe), hide_index=True, width="stretch")
        st.caption("Yem maliyeti = bu rasyonun günlük maliyeti × besi günü (besi ortası ağırlığı "
                   "yöntemi). Rasyon, Günlük rasyon ekranındaki çözücüyle aynı kurallarla hesaplandı.")
        for item in ration["turkish_explanations"][:4]:
            st.caption(item)


def render_economics_page() -> None:
    """Besi ekonomisi modu: hayvan + besi süresi + yemler + fiyatlar → kâr hesabı."""
    with st.sidebar:
        st.subheader("Besi ekonomisi")
        species = st.segmented_control("Tür", options=list(ECON_SPECIES), format_func=ECON_SPECIES.get,
                                       default="beef", key="econ_species") or "beef"
        lamb = species == "lamb"
        d = ECON_DEFAULTS[species]
        head_count = st.number_input("Hayvan sayısı (baş)", 1, 100000, 800 if lamb else 114, 1,
                                     key=f"econ_heads_{species}")
        start_weight = st.number_input("Besi başı canlı ağırlık (kg)", 10.0 if lamb else 100.0,
                                       80.0 if lamb else 700.0, d["weight"], 1.0 if lamb else 10.0,
                                       key=f"econ_bw_{species}")
        if lamb:
            daily_gain = st.number_input("Günlük canlı ağırlık artışı (g)", 50, 500, 315, 5,
                                         key="econ_gain_lamb") / 1000
        else:
            daily_gain = st.number_input("Günlük canlı ağırlık artışı (kg)", 0.3, 2.5, d["gain"], 0.05,
                                         key="econ_gain_beef")
        days = st.number_input("Besi süresi (gün)", 10, 600, d["days"], 5, key=f"econ_days_{species}")
        mid = start_weight + daily_gain * days / 2
        end = start_weight + daily_gain * days
        with st.container(horizontal=True):
            st.metric("Besi sonu", f"{end:.1f} kg", border=True)
            st.metric("Besi ortası", f"{mid:.1f} kg", border=True,
                      help="Yem tüketimi ve maliyeti bu ağırlığın rasyonundan hesaplanır.")
        render_price_sync()

    page_header("Besi ekonomisi", "Bir besi döneminin maliyeti, geliri ve kârı — kesim fiyatı UKON'dan.")

    feeds_data = fetch_feeds()
    market = fetch_market_prices()
    if not feeds_data or not market:
        st.stop()
    names_of = lambda types: [f["name"] for f in feeds_data if f.get("feed_type") in types]  # noqa: E731

    st.subheader("Yemler")
    col_forage, col_concentrate = st.columns(2)
    with col_forage:
        forages = st.multiselect("Kaba yemler", names_of(FORAGE_TYPES), placeholder="Silaj, kuru ot, saman…",
                                 key=f"econ_forage_{species}")
    with col_concentrate:
        concentrates = st.multiselect("Kesif yemler", names_of(CONCENTRATE_TYPES),
                                      placeholder="Tahıl, küspe, kepek…", key=f"econ_conc_{species}")
    mineral_options = names_of(MINERAL_TYPES)
    minerals = st.multiselect(
        "Mineraller", mineral_options,
        default=[n for n in (DEFAULT_LAMB_MINERALS if lamb else DEFAULT_MINERALS) if n in mineral_options],
        key=f"econ_minerals_{species}",
    )
    selected = forages + concentrates + minerals
    price_overrides: Dict[str, float] = {}
    if selected:
        req = fetch_requirements({"group": species, "body_weight_kg": mid,
                                  "daily_gain_kg": float(daily_gain), "days_on_feed": max(1, days // 2)})
        price_overrides = render_price_editor(selected, feeds_data, mid,
                                              tuple((req or {}).get("health_cap_exempt") or ()))

    st.subheader("Kesim fiyatı")
    # UKON bölgeleri "<karkas anahtarı>@<bölge>" satırlarıdır; varsayılan Türkiye ortalaması.
    regions = sorted(k.split("@", 1)[1] for k in market if k.startswith(d["carcass_key"] + "@"))
    region = st.selectbox("Bölge (kesim fiyatı)", ["Türkiye ortalaması"] + regions, key="econ_region",
                          help="UKON'un kombina ve kesimhanelerden topladığı haftalık kesim fiyatları.")
    carcass_row = market.get(f"{d['carcass_key']}@{region}", market[d["carcass_key"]])
    # Anahtar fiyatı da taşır: senkron ya da bölge değişince kutular yeni fiyatla açılsın.
    col_a, col_b = st.columns(2)
    with col_a:
        carcass = st.number_input("Kesim / karkas (TL/kg)", 1.0, 5000.0, float(carcass_row["price_per_kg"]),
                                  5.0, key=f"econ_carcass_{carcass_row['key']}_{carcass_row['price_per_kg']}")
    with col_b:
        dressing = st.number_input("Karkas randımanı (%)", 30.0, 75.0, d["dressing"], 0.5,
                                   key=f"econ_dressing_{species}",
                                   help="Varsayılan: dana %55, kuzu %45.")
    # Türkiye'de besilik hayvan da o günkü kesim fiyatından değerlenir: alış = karkas × randıman.
    implied_buy = carcass * dressing / 100
    purchase = None
    if st.toggle("Alış fiyatını kendim gireceğim", value=False, key=f"econ_custom_buy_{species}",
                 help="Kapalıyken hayvan o günkü kesim fiyatından alınmış sayılır: "
                      "alış = karkas fiyatı × randıman."):
        purchase = st.number_input("Alış (TL/kg canlı)", 1.0, 5000.0, round(implied_buy, 1), 5.0,
                                   key=f"econ_buy_{species}")
    else:
        st.caption(f"Alış: **{implied_buy:,.1f} TL/kg canlı** (kesim {carcass:,.2f} × randıman "
                   f"%{dressing:g}) → baş başına {start_weight * implied_buy:,.0f} TL.")
    st.caption(f"Kesim fiyatı: {_price_caption(carcass_row)} · satış = bitiş ağırlığı × randıman × "
               "kesim fiyatı (besici eti kesimhaneye satar).")
    changed = ([{"key": carcass_row["key"], "price_per_kg": float(carcass)}]
               if abs(carcass - carcass_row["price_per_kg"]) > 0.005 else [])
    has_saved = any(row["price_source"] == "elle girildi" for row in market.values())
    with st.container(horizontal=True):
        if st.button("Kesim fiyatımı kaydet", icon=":material/save:", disabled=not changed,
                     help="Kendi kesim fiyatın kaydedilir; günlük senkron bunu ezmez."):
            post_prices("market-prices", "Fiyatlar kaydediliyor...", body=changed, timeout=30)
        if has_saved and st.button("Kaydettiğim fiyatları sil", icon=":material/restart_alt:",
                                   key="econ_reset_prices"):
            post_prices("feeds/reset-prices", "Kayıtlı fiyatlar siliniyor...", timeout=30)

    st.subheader("Diğer giderler")
    itemized = st.segmented_control(
        "Hesap şekli", options=["Yemin 1/3'ü", "Kalem kalem"], default="Yemin 1/3'ü",
        key="econ_other_mode",
        help="Notlardaki kural: diğer giderler yem maliyetinin 1/3'ü kadardır.",
    ) == "Kalem kalem"
    costs = {}
    if itemized:
        col_a, col_b, col_c, col_d = st.columns(4)
        with col_a:
            costs["mortality_pct"] = st.number_input("Ölüm / fire (%)", 0.0, 50.0, d["mortality"], 0.5,
                                                     key=f"econ_mort_{species}")
        with col_b:
            costs["vet_per_head"] = st.number_input("Veteriner / aşı (TL/baş)", 0.0, 100000.0, 0.0, 50.0,
                                                    key=f"econ_vet_{species}")
        with col_c:
            costs["labor_per_head_day"] = st.number_input(
                "İşçilik ve diğer (TL/baş/gün)", 0.0, 10000.0, 0.0, 1.0, key=f"econ_labor_{species}",
                help="İşçilik, elektrik, su, barınak payı …")
        with col_d:
            costs["interest_pct_year"] = st.number_input(
                "Faiz (yıllık %)", 0.0, 200.0, 0.0, 1.0, key="econ_interest",
                help="Alış parası ve yem parasının besi boyunca bağlı kalmasının maliyeti.")

    if st.button("Besi ekonomisini hesapla", type="primary", icon=":material/calculate:",
                 width="stretch", disabled=not selected) \
            or (st.session_state.pop("auto_calc_econ", False) and selected):
        payload = {
            "species": species, "head_count": int(head_count), "start_weight_kg": float(start_weight),
            "daily_gain_kg": float(daily_gain), "days": int(days), "selected_feed_names": selected,
            "feed_price_overrides": {n: float(p) for n, p in price_overrides.items() if p and p > 0},
            "purchase_price": float(purchase) if purchase else None,
            # Değişmediyse gönderme: sonuç fiyatın kaynağını (UKON, bölge) gösterebilsin.
            "carcass_price": float(carcass) if changed else None,
            "dressing_pct": float(dressing),
            "region": None if region == "Türkiye ortalaması" else region,
            "itemized_costs": itemized, "with_advice": True, **costs,
        }
        with st.spinner("Besi ortası rasyonu ve ekonomi hesaplanıyor..."):
            try:
                response = requests.post(f"{BACKEND_URL}/fattening-economics", json=payload, timeout=60)
            except Exception as exc:
                st.error(f"API bağlantı hatası: {exc}", icon=":material/error:")
                response = None
        if response is not None and response.status_code == 200:
            st.session_state["econ_result"] = {**response.json(), "request": payload}
        elif response is not None:
            st.error(f"Hesaplama hatası: {response.json().get('detail', response.text)}",
                     icon=":material/error:")
            st.session_state.pop("econ_result", None)

    result = st.session_state.get("econ_result")
    if result and result["species"] == species:
        render_economics_result(result)
    elif not selected:
        st.info("Başlamak için yem seç.", icon=":material/info:")


# ============================
# SOHBET — LLM ajanı (/agent/query): geçmiş + ekrandaki son sonuç bağlam olarak gider
# ============================

CHAT_EXAMPLES = (
    "650 kilo, günde 30 litre süt veren ineğim var; elimde mısır silajı, saman, arpa ve soya var. Rasyon hazırla.",
    "Kuru dönemdeki ineğe neden yonca verilmez?",
    "Arpa, mısır, kepek ve ayçiçeği küspesiyle 1 ton %21 süt yemi yap.",
    "220 kiloluk 114 danayı günde 1,5 kilo artışla 270 gün beslesem ne kazanırım?",
)
_CONTEXT_LABEL = {"ration": "günlük rasyon", "compound": "karma yem", "economics": "besi ekonomisi"}
_OPEN_LABEL = {"ration": "Günlük rasyon ekranında aç", "compound": "Karma yem ekranında aç",
               "economics": "Besi ekonomisi ekranında aç"}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_health() -> Dict:
    try:
        return requests.get(f"{BACKEND_URL}/health", timeout=10).json()
    except Exception:
        return {}


def targets_from_requirements(req: Dict) -> Dict:
    """Otomatik moddaki hedefler (sonuç kartları bunlara göre "hedefte" der)."""
    return {
        "manual": False,
        "dm_intake_kg": req["dm_intake_kg"],
        "cp_min": req["cp_min"], "cp_max": req["cp_max"],
        "starch_max": req["starch_max"], "fat_max": req["fat_max"],
        "me_min": req["me_min"] * 1000, "me_max": req["me_max"] * 1000,     # kcal/kg KM
        "ca_min": req["calcium_min"] / 10, "ca_max": req["calcium_max"] / 10,
        "p_min": req["phosphorus_min"] / 10, "p_max": req["phosphorus_max"] / 10,
        "ca_p_min": req["ca_p_ratio_min"], "ca_p_max": req["ca_p_ratio_max"],
        # Günlük ihtiyaçlar (Detaylar tablosu): ME Mcal, HP/Ca/P g.
        "me_day": req["me_mcal_day"], "cp_day": req["cp_min"] * req["dm_intake_kg"] * 10,
        "ca_day": req["calcium_g_day"], "p_day": req["phosphorus_g_day"],
        "water_l_day": req["water_l_day"],
        # Grubun rasyon kuralları — sonuç kartları bunlara göre "hedefte" der.
        "ndf_min": req["ndf_min"], "ndf_max": req["ndf_max"],
        "forage_min": req["forage_ratio_min"], "forage_max": req["forage_ratio_max"],
    }


def _ask_about(kind: str, data: Dict) -> None:
    """"Bu sonuç hakkında sor": soruyu ve sonucu sohbete taşır (on_click geri çağırması)."""
    question = (st.session_state.get(f"ask_{kind}") or "").strip()
    if not question:
        return
    st.session_state[f"ask_{kind}"] = ""
    st.session_state["chat_context"] = {"kind": kind, "data": data}
    st.session_state["chat_pending"] = question
    st.session_state["open_assistant"] = True


def render_ask_box(kind: str, data: Dict) -> None:
    """Sonucun altında küçük soru kutusu; LLM anahtarı yoksa gösterilmez."""
    if not fetch_health().get("llm_available"):
        return
    with st.container(border=True):
        st.markdown(":material/forum: **Bu sonuç hakkında sor**")
        with st.container(horizontal=True, vertical_alignment="bottom"):
            st.text_input("Soru", key=f"ask_{kind}", label_visibility="collapsed",
                          placeholder="Ör. neden saman bu kadar az? Arpa yerine buğday koysam?")
            st.button("Sor", key=f"ask_btn_{kind}", icon=":material/send:",
                      on_click=_ask_about, args=(kind, data))


def _split_by_box(names: List[str]) -> Dict[str, List[str]]:
    types = {f["name"]: f.get("feed_type") for f in fetch_feeds()}
    boxes: Dict[str, List[str]] = {"forage": [], "concentrate": [], "mineral": []}
    for name in names:
        if name in types:
            boxes[_feed_box(types[name])].append(name)
    return boxes


def _open_in_screen(result: Dict) -> None:
    """Sohbetteki sonucu ilgili sayfaya taşır: girdileri doldurur ve yeniden hesaplatır.

    Pencerenin içinden düz düğmeyle çağrılır (geri çağırma değil) ve ardından tüm uygulama
    yeniden çalıştırılır: pencere kapanır, `goto` ilgili sayfaya geçirir.
    """
    kind, data = result["kind"], result["data"]
    request = data.get("request") or {}
    boxes = _split_by_box(request.get("selected_feed_names") or [])
    if kind == "ration":
        animal = request.get("animal") or {}
        group = animal.get("group", "lactating")
        st.session_state["ration_group"] = group
        weight = float(animal.get("body_weight_kg", 650))
        if group == "lamb":
            st.session_state["bw_lamb"] = min(max(weight, 10.0), 80.0)
            st.session_state["gain_lamb"] = int(min(max(round(animal.get("daily_gain_kg", 0.3) * 1000), 50), 500))
        elif group == "beef":
            st.session_state["bw_beef"] = min(max(weight, 100.0), 1000.0)
            st.session_state["gain_beef"] = float(min(max(animal.get("daily_gain_kg", 1.4), 0.3), 2.5))
        else:
            st.session_state["bw_cow"] = min(max(weight, 300.0), 1000.0)
        if group == "lactating":
            st.session_state["milk_kg"] = float(min(max(animal.get("milk_kg", 30), 0.0), 70.0))
        if group in ("beef", "lamb"):
            st.session_state["days_on_feed"] = int(min(max(animal.get("days_on_feed", 60), 1), 500))
        st.session_state["ration_forages"] = boxes["forage"]
        st.session_state["ration_concentrates"] = boxes["concentrate"]
        st.session_state["minerals_lamb" if group == "lamb" else "minerals_cattle"] = boxes["mineral"]
        st.session_state["auto_calc_ration"] = True
        st.session_state["goto"] = "ration"
    elif kind == "compound":
        st.session_state["compound_type"] = request.get("type_key", "sut_21")
        st.session_state["compound_batch"] = float(request.get("batch_kg", 1000.0))
        st.session_state["compound_ingredients"] = boxes["concentrate"]
        st.session_state["compound_minerals"] = boxes["mineral"]
        st.session_state["auto_calc_compound"] = True
        st.session_state["goto"] = "compound"
    elif kind == "economics":
        species = request.get("species", "beef")
        st.session_state["econ_species"] = species
        st.session_state[f"econ_heads_{species}"] = int(request.get("head_count", 1))
        weight = float(request.get("start_weight_kg", 300))
        st.session_state[f"econ_bw_{species}"] = (min(max(weight, 10.0), 80.0) if species == "lamb"
                                                  else min(max(weight, 100.0), 700.0))
        gain = float(request.get("daily_gain_kg", 1.4))
        if species == "lamb":
            st.session_state["econ_gain_lamb"] = int(min(max(round(gain * 1000), 50), 500))
        else:
            st.session_state["econ_gain_beef"] = min(max(gain, 0.3), 2.5)
        st.session_state[f"econ_days_{species}"] = int(min(max(request.get("days", 180), 10), 600))
        st.session_state[f"econ_forage_{species}"] = boxes["forage"]
        st.session_state[f"econ_conc_{species}"] = boxes["concentrate"]
        st.session_state[f"econ_minerals_{species}"] = boxes["mineral"]
        st.session_state["auto_calc_econ"] = True
        st.session_state["goto"] = "economics"


def _render_chat_result(result: Dict) -> None:
    """Sohbetteki hesap sonucu, ekranlardaki aynı bileşenlerle (öneri düğmeleri olmadan)."""
    kind, data = result.get("kind"), result.get("data") or {}
    if kind == "ration":
        animal = (data.get("request") or {}).get("animal")
        req = fetch_requirements(animal) if animal else None
        if req:
            render_results(data, targets_from_requirements(req))
    elif kind == "compound":
        render_compound_result(data, actionable=False)
    elif kind == "economics":
        render_economics_result(data, actionable=False)


def page_header(title: str, caption: str) -> None:
    """Sayfa başlığı; sağında asistan düğmesi (LLM anahtarı yoksa düğme yok)."""
    with st.container(horizontal=True, vertical_alignment="center"):
        st.title(title, width="stretch")
        if fetch_health().get("llm_available") and st.button(
                "Asistan", icon=":material/support_agent:", key="assistant_button",
                help="Soru sor ya da rasyonu sohbetle hesaplat."):
            st.session_state["open_assistant"] = True
    st.caption(caption)


def _pick_suggestion() -> None:
    st.session_state["chat_pending"] = st.session_state.get("chat_suggestion")
    st.session_state["chat_suggestion"] = None


@st.dialog("RationAI asistan", width="large", icon=":material/support_agent:")
def assistant_dialog() -> None:
    """Sohbet penceresi. İçindeki etkileşimler yalnız pencereyi yeniden çalıştırır (fragment)."""
    chat = st.session_state.setdefault("chat", [])
    context = st.session_state.get("chat_context")

    with st.container(horizontal=True, vertical_alignment="center"):
        model = next((m.get("model") for m in reversed(chat) if m.get("model")), None)
        note = (f":material/push_pin: Bağlam: son {_CONTEXT_LABEL.get(context['kind'], 'sonuç')}"
                if context else "Rasyon, karma yem, besi kârı ya da besleme sorusu sor.")
        st.caption(note + (f" · {model}" if model else ""), width="stretch")
        if context and st.button("Bağlamı kaldır", icon=":material/link_off:", type="tertiary"):
            st.session_state.pop("chat_context", None)
            st.rerun(scope="fragment")
        if chat and st.button("Temizle", icon=":material/delete_sweep:", type="tertiary"):
            st.session_state["chat"] = []
            st.session_state.pop("chat_context", None)
            st.rerun(scope="fragment")

    history = st.container(height=520, border=False)
    with history:
        if not chat:
            st.pills("Örnek sorular", CHAT_EXAMPLES, key="chat_suggestion", label_visibility="collapsed",
                     on_change=_pick_suggestion)
        for i, message in enumerate(chat):
            avatar = ":material/support_agent:" if message["role"] == "assistant" else ":material/person:"
            with st.chat_message(message["role"], avatar=avatar):
                st.markdown(message["content"])
                if message.get("sources"):
                    st.caption(":material/menu_book: Kaynak: " + ", ".join(message["sources"]))
                result = message.get("result")
                if result:
                    with st.container(border=True):
                        _render_chat_result(result)
                        if st.button(_OPEN_LABEL.get(result["kind"], "Sayfada aç"), key=f"chat_open_{i}",
                                     icon=":material/open_in_new:"):
                            _open_in_screen(result)
                            st.rerun()          # tüm uygulama: pencere kapanır, sayfa değişir

    prompt = st.chat_input("Sorunu yaz…", submit_mode="disable") or st.session_state.pop("chat_pending", None)
    if not prompt:
        return
    past = [{"role": m["role"], "content": m["content"]} for m in chat if not m.get("error")]
    chat.append({"role": "user", "content": prompt})
    with history:
        with st.chat_message("user", avatar=":material/person:"):
            st.markdown(prompt)
        with st.chat_message("assistant", avatar=":material/support_agent:"):
            with st.spinner("Düşünüyor… (hesap gerekiyorsa çözücü çalışıyor)"):
                try:
                    response = requests.post(
                        f"{BACKEND_URL}/agent/query", timeout=300,
                        json={"query": prompt, "history": past, "context": st.session_state.get("chat_context")})
                    error = None
                except Exception as exc:
                    response, error = None, str(exc)
    if response is not None and response.status_code == 200:
        data = response.json()
        chat.append({"role": "assistant", "content": data.get("answer") or "", "sources": data.get("sources") or [],
                     "result": data.get("result"), "model": data.get("model")})
        if data.get("result"):
            st.session_state["chat_context"] = data["result"]
    else:
        detail = (response.json().get("detail", response.text) if response is not None else error)
        chat.append({"role": "assistant", "content": f"Şu an cevap veremedim: {detail}", "error": True})
    st.rerun(scope="fragment")


def render_ration_page() -> None:
    """Günlük rasyon sayfası: hayvan profili (kenar) → yem seçimi → sonuç."""
    # ============================
    # KENAR ÇUBUĞU - HAYVAN PROFİLİ
    # ============================

    with st.sidebar:
        st.subheader("Hayvan profili")

        group = st.segmented_control(
            "Hayvan grubu",
            options=list(GROUP_LABELS),
            format_func=GROUP_LABELS.get,
            default="lactating",
            key="ration_group",
            help="Kuru dönemin rasyon kuralları farklıdır: daha fazla lif ve saman, daha az enerji. "
                 "Besi sığırı ve kuzuda kaba yem besi başında %50, 20 günde %30'a iner.",
        ) or "lactating"
        lactating = group == "lactating"
        beef = group == "beef"
        lamb = group == "lamb"

        # Her tür için ayrı anahtar: kuzu 30 kg'da, dana 400 kg'da, inek 650 kg'da kalır.
        if lamb:
            body_weight = st.number_input("Canlı ağırlık (kg)", 10.0, 80.0, 30.0, 1.0, key="bw_lamb")
        else:
            body_weight = st.number_input(
                "Canlı ağırlık (kg)", min_value=100.0 if beef else 300.0, max_value=1000.0,
                value=400.0 if beef else 650.0, step=25.0, key="bw_beef" if beef else "bw_cow",
            )
        milk_yield = st.number_input(
            "Günlük süt verimi (kg)", min_value=0.0, max_value=70.0, value=30.0, step=1.0, key="milk_kg"
        ) if lactating else 0.0
        daily_gain, days_on_feed = 1.4, 60
        if beef or lamb:
            if lamb:
                daily_gain = st.number_input("Hedef günlük canlı ağırlık artışı (g)", 50, 500, 300, 10,
                                             key="gain_lamb") / 1000
            else:
                daily_gain = st.number_input("Hedef günlük canlı ağırlık artışı (kg)", 0.3, 2.5, 1.4, 0.1,
                                             key="gain_beef")
            days_on_feed = st.number_input(
                "Besinin kaçıncı günü", 1, 500, 60, 1, key="days_on_feed",
                help="İlk 20 günde kaba yem payı %50 → %45 → %40 → %35 → %30 iner (5 günde bir).",
            )

        fat_pct, protein_pct, days_in_milk, days_pregnant = 3.8, 3.2, 120, 0
        first_lactation, bcs = False, 3.0
        if not (beef or lamb):
            with st.expander("Hayvan detayları", icon=":material/pets:"):
                nasem_only = "Yalnızca NASEM 2021 karşılaştırma notunda kullanılır."
                if lactating:
                    col_a, col_b = st.columns(2)
                    with col_a:
                        fat_pct = st.number_input("Süt yağı (%)", 2.0, 6.5, 3.8, 0.1)
                        days_in_milk = st.number_input("Laktasyon günü", 1, 600, 120, 5, help=nasem_only)
                    with col_b:
                        protein_pct = st.number_input("Süt proteini (%)", 2.4, 4.5, 3.2, 0.1,
                                                      help=nasem_only)
                        days_pregnant = st.number_input(
                            "Gebelik günü", 0, 285, 0, 10,
                            help="0 = gebe değil. 190. günden itibaren yaşama payı enerjisine %30 eklenir.",
                        )
                else:
                    fat_pct, protein_pct, days_in_milk = 3.8, 3.2, 120
                    days_pregnant = st.number_input(
                        "Gebelik günü", 150, 285, 230 if group == "dry_faroff" else 265, 5,
                        help="Doğuma ~3 haftadan az kaldıysa 'Kuru (doğuma yakın)' grubunu seç.",
                    )
                first_lactation = st.toggle(
                    "İlk laktasyon / düve" if lactating else "Düve (ilk doğum)", value=False
                )
                bcs = st.slider("Vücut kondisyon skoru (BCS)", 1.0, 5.0, 3.0, 0.25, help=nasem_only)

        animal = {
            "group": group,
            "body_weight_kg": body_weight,
            "milk_kg": milk_yield,
            "fat_pct": fat_pct,
            "true_protein_pct": protein_pct,
            "days_in_milk": int(days_in_milk),
            "multiparous": not first_lactation,
            "bcs": bcs,
            "days_pregnant": int(days_pregnant),
            "daily_gain_kg": float(daily_gain),
            "days_on_feed": int(days_on_feed),
        }
        req = fetch_requirements(animal)
        if req is None:
            st.stop()

        with st.container(horizontal=True):
            st.metric("Kuru madde", f"{req['dm_intake_kg']:.2f} kg" if lamb else f"{req['dm_intake_kg']:.1f} kg",
                      border=True)
            st.metric("ME", f"{req['me_mcal_day'] * 1000:,.0f}", border=True,
                      help=f"Günlük metabolik enerji ihtiyacı, kcal · hedef "
                           f"{req['me_min'] * 1000:,.0f} kcal/kg KM")
        # Solver'ın uyguladığı HP hedefi: kuru dönemde formül yalnızca yaşama payını verir,
        # %12-13 KM tabanı devreye girer; çok yüksek verimde tavan uygulanır.
        cp_target_g = req["cp_min"] * req["dm_intake_kg"] * 10
        with st.container(horizontal=True):
            st.metric("Ham protein", f"{cp_target_g:,.0f} g", border=True,
                      help=f"Günlük HP hedefi · %{req['cp_min']:.1f} KM · formüle göre ihtiyaç "
                           f"{req['cp_g_day']:,.0f} g")
            st.metric("Su", f"{req['water_l_day']:.1f} L" if lamb else f"{req['water_l_day']:.0f} L",
                      border=True,
                      help="Günlük içme suyu ihtiyacı · koyunda KM tüketiminin ~2.5 katı" if lamb else
                           "Günlük içme suyu ihtiyacı: 12.3 + 2.15 × KMT + 0.73 × süt")
        st.caption("Hedefler rasyon notlarındaki formüllerle (ME + HP) hesaplanır.")
        for note in req["notes"]:
            st.caption(f":material/info: {note}")

        with st.expander("Gelişmiş ayarlar", icon=":material/tune:"):
            manual_targets = st.toggle(
                "Besin hedeflerini elle ayarla",
                value=False,
                help="Kapalıyken hedefler hayvan profilinden otomatik hesaplanır.",
            )

            if manual_targets:
                dm_intake = st.number_input(
                    "Günlük kuru madde tüketimi (kg)",
                    min_value=5.0, max_value=35.0, value=float(req["dm_intake_kg"]), step=0.5,
                )
                col_a, col_b = st.columns(2)
                with col_a:
                    cp_min = st.number_input("Min ham protein (% KM)", 5.0, 30.0,
                                             float(round(req["cp_min"], 1)), 0.5)
                    me_min = st.number_input("Min ME (kcal/kg KM)", 1500, 3500,
                                             int(round(req["me_min"] * 1000, -1)), 10)
                    ca_min = st.number_input("Min kalsiyum (% KM)", 0.1, 2.0,
                                             req["calcium_min"] / 10, 0.05)
                    p_min = st.number_input("Min fosfor (% KM)", 0.1, 1.5,
                                            req["phosphorus_min"] / 10, 0.05)
                with col_b:
                    cp_max = st.number_input("Max ham protein (% KM)", 8.0, 35.0,
                                             float(round(req["cp_max"], 1)), 0.5)
                    me_max = st.number_input("Max ME (kcal/kg KM)", 1600, 4000,
                                             int(round(req["me_max"] * 1000, -1)), 10)
                    ca_max = st.number_input("Max kalsiyum (% KM)", 0.3, 3.0,
                                             req["calcium_max"] / 10, 0.05)
                    p_max = st.number_input("Max fosfor (% KM)", 0.2, 2.0,
                                            req["phosphorus_max"] / 10, 0.05)
            else:
                dm_intake = req["dm_intake_kg"]
                cp_min, cp_max = req["cp_min"], req["cp_max"]
                me_min, me_max = req["me_min"] * 1000, req["me_max"] * 1000
                ca_min, ca_max = req["calcium_min"] / 10, req["calcium_max"] / 10
                p_min, p_max = req["phosphorus_min"] / 10, req["phosphorus_max"] / 10

            ratio = ca_min / p_min if p_min > 0 else 0
            st.caption(f"Hedeflenen Ca:P oranı {ratio:.2f} "
                       f"(bu grupta {req['ca_p_ratio_min']}–{req['ca_p_ratio_max']})")

            use_all_feeds = st.toggle(
                "Seçili yemlerin tümünü kullan",
                value=True,
                help="Kapatırsan çözücü sadece en ucuz kombinasyonu seçer ve bazı yemleri "
                     "hiç kullanmayabilir.",
            )
            min_inclusion_pct = st.slider(
                "Yem başına minimum oran (% KM)",
                min_value=1.0, max_value=8.0, value=3.0, step=0.5,
                disabled=not use_all_feeds,
                help="Mineraller için güvenlik tavanı otomatik uygulanır (tuz %3 olamaz).",
            )

            limit_dm = st.toggle(
                "Rasyon kuru madde aralığını sınırla",
                value=False,
                help="Genelde gerekmez. Silaj kullanıyorsan rasyon KM'si %30-50 civarındadır; "
                     "%85 gibi bir alt sınır modeli çözümsüz bırakır.",
            )
            dm_range = st.slider(
                "Rasyon KM aralığı (% doğal hali)",
                min_value=25.0, max_value=95.0, value=(40.0, 60.0), step=1.0,
            ) if limit_dm else None

        render_price_sync()

    # Otomatik hedefler + "Gelişmiş ayarlar"da elle girilenler (kapalıyken aynı değerler).
    targets = {
        **targets_from_requirements(req),
        "manual": manual_targets,
        "dm_intake_kg": dm_intake,
        "cp_min": cp_min, "cp_max": cp_max,
        "me_min": me_min, "me_max": me_max,       # kcal/kg KM
        "ca_min": ca_min, "ca_max": ca_max,
        "p_min": p_min, "p_max": p_max,
        "cp_day": cp_target_g,
    }


    # ============================
    # ANA EKRAN
    # ============================

    page_header("Günlük rasyon", "İhtiyacı tam karşılayan en ucuz rasyon — süt ineği, kuru dönem, "
                "besi sığırı ve küçükbaş.")

    feeds_data = fetch_feeds()
    if not feeds_data:
        st.error("Yem verileri yüklenemedi. Backend çalışıyor mu?", icon=":material/error:")
        st.stop()

    st.subheader("Yem seçimi")


    def _names_of(types) -> List[str]:
        """Verilen yem sınıflarındaki yem adları (API'deki `feed_type` alanına göre)."""
        return [feed["name"] for feed in feeds_data if feed.get("feed_type") in types]


    forage_options = _names_of(FORAGE_TYPES)
    concentrate_options = _names_of(CONCENTRATE_TYPES)
    mineral_options = _names_of(MINERAL_TYPES)

    col_forage, col_concentrate = st.columns(2)
    with col_forage:
        selected_forages = st.multiselect(
            "Kaba yemler",
            options=forage_options,
            placeholder="Silaj, kuru ot, saman…",
            # Yardım metni sabit kalmalı: gruba göre değişirse widget yeniden kurulur ve
            # hayvan grubu değişince seçili kaba yemler siliniyordu.
            help="Rasyonun kaba yem payı gruba göre: sağmal %45–65, kuru dönem %60–90, "
                 "besi ve kuzu %30–60 (besi başında %50).",
            key="ration_forages",   # "Öneriyi uygula" bu anahtarla seçimi değiştirir
        )
    with col_concentrate:
        selected_concentrates = st.multiselect(
            "Kesif yemler",
            options=concentrate_options,
            placeholder="Tahıl, küspe, kepek…",
            key="ration_concentrates",
        )

    # Koyun ve sığır için ayrı anahtar: varsayılanlar farklı ve tür değişince seçim korunur.
    selected_minerals = st.multiselect(
        "Mineraller",
        options=mineral_options,
        default=[name for name in (DEFAULT_LAMB_MINERALS if lamb else DEFAULT_MINERALS)
                 if name in mineral_options],
        help="Otomatik seçili gelir; miktarlarını çözücü Ca/P açığına göre belirler.",
        key="minerals_lamb" if lamb else "minerals_cattle",
    )

    selected_feeds = selected_forages + selected_concentrates + selected_minerals

    if selected_feeds and not selected_forages:
        st.warning(
            f"Hiç kaba yem seçmedin. Rumen sağlığı için rasyonun en az "
            f"%{targets['forage_min']:.0f}'i kaba yem olmalı.",
            icon=":material/warning:",
        )

    price_overrides: Dict[str, float] = {}

    if selected_feeds:
        price_overrides = render_price_editor(selected_feeds, feeds_data, body_weight,
                                              tuple(req.get("health_cap_exempt") or ()))
    else:
        st.info("Başlamak için en az bir yem seç.", icon=":material/info:")

    calculate = st.button(
        "Rasyonu hesapla",
        type="primary",
        icon=":material/calculate:",
        width="stretch",
        disabled=not selected_feeds,
    )
    # "Öneriyi uygula" seçimi değiştirip bu bayrağı koyar: rasyon kendiliğinden yeniden hesaplanır.
    calculate = calculate or (st.session_state.pop("auto_calc_ration", False) and bool(selected_feeds))

    if calculate:
        with st.spinner("Rasyon optimize ediliyor..."):
            payload = build_optimization_payload(
                selected_feeds=selected_feeds,
                animal=animal,
                targets=targets,
                use_all_feeds=use_all_feeds,
                min_inclusion_pct_dm=min_inclusion_pct,
                price_overrides=price_overrides,
                dm_range=dm_range,
            )
            payload["with_advice"] = True
            result = call_optimization_api(payload)

        if result:
            # Sohbet bağlamı ve "Ekranda aç" için girdiler de saklanır.
            result["request"] = {"animal": animal, "selected_feed_names": selected_feeds}
            st.session_state["last_result"] = result
            st.session_state["last_targets"] = targets
        else:
            st.session_state.pop("last_result", None)

    if "last_result" in st.session_state:
        render_results(st.session_state["last_result"], st.session_state["last_targets"],
                       {"forage": "ration_forages", "concentrate": "ration_concentrates",
                        "mineral": "minerals_lamb" if lamb else "minerals_cattle"})

    st.caption("RationAI · FastAPI + PuLP · İhtiyaçlar ve HP/ME değerleri: rasyon notları · "
               "Diğer besin değerleri: NRC 2001")


# ============================
# SAYFALAR — üst menü; asistan penceresi sayfadan sonra açılır
# ============================

PAGES = {
    "ration": st.Page(render_ration_page, title="Günlük rasyon", icon=":material/grass:",
                      url_path="rasyon", default=True),
    "compound": st.Page(render_compound_feed_page, title="Karma yem", icon=":material/blender:",
                        url_path="karma-yem"),
    "economics": st.Page(render_economics_page, title="Besi ekonomisi", icon=":material/payments:",
                         url_path="besi-ekonomisi"),
}
current_page = st.navigation(list(PAGES.values()), position="top")
# Asistandaki "… sayfasında aç": girdiler yazıldı, şimdi o sayfaya geç.
target = st.session_state.pop("goto", None)
if target in PAGES:
    st.switch_page(PAGES[target])
current_page.run()
if st.session_state.pop("open_assistant", False):
    assistant_dialog()
