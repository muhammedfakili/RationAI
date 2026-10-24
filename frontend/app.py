#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
RationAI - Süt ineği rasyon optimizasyonu arayüzü.

Tek sayfa: hayvan profili (kenar çubuğu) -> yem seçimi -> rasyon sonucu.
Hesaplama deterministik `/api/v1/optimize` uç noktasına gider, LLM devrede değildir.
"""

import os
from typing import Dict, List, Optional, Tuple

import altair as alt
import pandas as pd
import requests
import streamlit as st

# Docker compose'ta servis adıyla konuşulur (http://backend:8000/api/v1).
# Varsayılan localhost, konteynersiz `quickstart.py` akışı bozulmasın diye korunur.
BACKEND_URL = os.getenv("BACKEND_URL", "http://localhost:8000/api/v1")

# Hedefler (NASEM 2021) ve grubun rasyon kuralları backend'den gelir: /requirements.
GROUP_LABELS = {
    "lactating": "Sağmal",
    "dry_faroff": "Kuru (erken)",
    "dry_closeup": "Kuru (doğuma yakın)",
}

# Yem sınıfı -> seçim grubu
FORAGE_TYPES = ("silage", "hay", "straw")
CONCENTRATE_TYPES = ("grain", "protein", "byproduct", "fat")
MINERAL_TYPES = ("mineral", "salt")

# Mineraller varsayılan olarak seçili gelir; miktarlarını solver Ca/P açığına göre belirler.
DEFAULT_MINERALS = ("Mermer Tozu", "DCP (Dikalsiyum Fosfat)", "Tuz")

st.set_page_config(
    page_title="RationAI",
    page_icon=":material/agriculture:",
    layout="centered",
    initial_sidebar_state="expanded",
)


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
    """Hayvan profilinin NASEM 2021 ihtiyaçları + grubun rasyon kuralları."""
    try:
        return _post_requirements(tuple(sorted(profile.items())))
    except Exception as exc:
        st.error(f"İhtiyaç hesaplanamadı: {exc}", icon=":material/error:")
        return None


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

    Hayvan profili her zaman gider: backend hedefleri (NASEM 2021) ve grubun rasyon
    kurallarını ondan türetir. Hedefler yalnızca elle ayarlandıysa açıkça gönderilir
    ve profilden gelenlerin önüne geçer.

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
            # HP alt sınırı gönderilmez: kuru dönem tabanını backend profilden doldurur.
            "metabolizable_protein_min": targets["mp_min"],
            "crude_protein_max": targets["cp_max"],
            "net_energy_lactation_min": targets["nel_min"],
            "net_energy_lactation_max": targets["nel_max"],
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
# SONUÇ EKRANI
# ============================

def render_results(result: Dict, targets: Dict) -> None:
    """Optimizasyon sonucunu sade bir özet + kapalı detay bölümü olarak göster."""

    solver_details = result.get("solver_details") or {}
    nutrition_mode = solver_details.get("nutrition_mode")
    status = result.get("status", "Bilinmiyor")

    st.subheader("Rasyon sonucu")

    if nutrition_mode == "hard":
        st.success(
            "Tüm besin hedefleri sağlandı — bu, hedefleri karşılayan en ucuz rasyon.",
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

    recommendations = [
        rec for rec in result.get("feed_recommendations", [])
        if rec.get("amount_kg_per_day", 0) > 0.001
    ]
    daily_intake = result.get("daily_intake") or {}
    nutrient_content = result.get("nutrient_content") or {}

    # --- Maliyet özeti ---
    cost_per_day = result.get("total_cost_per_day", 0) or 0
    with st.container(horizontal=True):
        st.metric("Günlük maliyet", f"{cost_per_day:,.2f} TL", border=True)
        st.metric("Aylık maliyet", f"{cost_per_day * 30:,.0f} TL", border=True)
        st.metric(
            "Günlük kuru madde",
            f"{daily_intake.get('dry_matter_kg', 0):.1f} kg",
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
            "kg/gün": round(rec["amount_kg_per_day"], 2),
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
            "kg/gün": st.column_config.NumberColumn(format="%.2f"),
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
        # Protein MP olarak hedeflenir (mikrobiyal + by-pass protein), g/gün gösterilir.
        ("MP (g/gün)", daily_intake.get("mp_g") or 0,
         targets["mp_min"] * targets["dm_intake_kg"], None, "g/gün", 0),
        ("NEL", nutrient_content.get("net_energy_lactation_mcal_per_kg_dm", 0),
         targets["nel_min"], targets["nel_max"], "Mcal/kg KM", 2),
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
            f"**Ca:P oranı:** {ca_p_ratio:.2f} (hedef 1.5–2.5) &nbsp;|&nbsp; "
            f"**Rasyon kuru maddesi:** {nutrient_content.get('dry_matter_percent', 0):.1f}% "
            "(doğal hali) &nbsp;|&nbsp; "
            f"**Kaba yem NDF'si:** {nutrient_content.get('forage_ndf_percent_dm', 0):.1f}% KM"
        )
        st.markdown(
            f"**Ham protein:** %{nutrient_content.get('crude_protein_percent_dm', 0):.1f} KM "
            f"(RDP %{nutrient_content.get('rdp_percent_dm') or 0:.1f}, "
            f"RUP %{nutrient_content.get('rup_percent_dm') or 0:.1f}) &nbsp;|&nbsp; "
            f"**RDP dengesi:** {nutrient_content.get('rdp_balance_g') or 0:+.0f} g/gün &nbsp;|&nbsp; "
            f"**Ham yağ:** %{nutrient_content.get('ether_extract_percent_dm') or 0:.1f} KM "
            f"(en fazla %{targets['fat_max']:.0f})"
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
            st.markdown("**Günlük besin maddesi alımı**")
            st.dataframe(
                pd.DataFrame([
                    {"Besin maddesi": "Metabolize protein (MP)",
                     "Alım": f"{daily_intake.get('mp_g') or 0:.0f} g/gün"},
                    {"Besin maddesi": "Ham protein",
                     "Alım": f"{daily_intake.get('crude_protein_kg', 0):.2f} kg/gün"},
                    {"Besin maddesi": "Net enerji (NEL)",
                     "Alım": f"{daily_intake.get('net_energy_lactation_mcal') or 0:.1f} Mcal/gün"},
                    {"Besin maddesi": "Metabolik enerji",
                     "Alım": f"{daily_intake.get('metabolic_energy_mcal', 0):.1f} Mcal/gün"},
                    {"Besin maddesi": "Kalsiyum",
                     "Alım": f"{daily_intake.get('calcium_g', 0):.0f} g/gün"},
                    {"Besin maddesi": "Fosfor",
                     "Alım": f"{daily_intake.get('phosphorus_g', 0):.0f} g/gün"},
                    {"Besin maddesi": "Kuru madde",
                     "Alım": f"{daily_intake.get('dry_matter_kg', 0):.1f} kg/gün"},
                ]),
                hide_index=True,
                width="stretch",
            )

        if solver_details:
            st.markdown("**Solver detayları**")
            st.json(solver_details, expanded=False)


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
        help="Kuru dönemin rasyon kuralları farklıdır: daha fazla lif ve saman, daha az enerji.",
    ) or "lactating"
    lactating = group == "lactating"

    body_weight = st.number_input(
        "Canlı ağırlık (kg)", min_value=300.0, max_value=1000.0, value=650.0, step=25.0
    )
    milk_yield = st.number_input(
        "Günlük süt verimi (kg)", min_value=0.0, max_value=70.0, value=30.0, step=1.0
    ) if lactating else 0.0

    with st.expander("Hayvan detayları", icon=":material/pets:"):
        if lactating:
            col_a, col_b = st.columns(2)
            with col_a:
                fat_pct = st.number_input("Süt yağı (%)", 2.0, 6.5, 3.8, 0.1)
                days_in_milk = st.number_input("Laktasyon günü", 1, 600, 120, 5)
            with col_b:
                protein_pct = st.number_input("Süt proteini (%)", 2.4, 4.5, 3.2, 0.1)
                days_pregnant = st.number_input(
                    "Gebelik günü", 0, 285, 0, 10, help="0 = gebe değil"
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
        bcs = st.slider("Vücut kondisyon skoru (BCS)", 1.0, 5.0, 3.0, 0.25)

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
    }
    req = fetch_requirements(animal)
    if req is None:
        st.stop()

    with st.container(horizontal=True):
        st.metric("Kuru madde", f"{req['dm_intake_kg']:.1f} kg", border=True)
        st.metric("NEL", f"{req['nel_min']:.2f}", border=True,
                  help=f"Mcal/kg KM · günlük ihtiyaç {req['nel_mcal_day']:.1f} Mcal")
        st.metric("MP", f"{req['mp_g_day']:.0f} g", border=True,
                  help=f"Metabolize protein ihtiyacı, günlük · hedef {req['mp_min']:.0f} g/kg KM")
    st.caption("Hedefler NASEM 2021 denklemleriyle hesaplanır.")
    for note in req["notes"]:
        st.caption(f":material/info: {note}")

    with st.expander("Gelişmiş ayarlar", icon=":material/tune:"):
        manual_targets = st.toggle(
            "Besin hedeflerini elle ayarla",
            value=False,
            help="Kapalıyken hedefler hayvan profilinden NASEM 2021 ile otomatik hesaplanır.",
        )

        if manual_targets:
            dm_intake = st.number_input(
                "Günlük kuru madde tüketimi (kg)",
                min_value=5.0, max_value=35.0, value=float(req["dm_intake_kg"]), step=0.5,
            )
            col_a, col_b = st.columns(2)
            with col_a:
                mp_min = st.number_input("Min MP (g/kg KM)", 40.0, 160.0,
                                         float(round(req["mp_min"])), 1.0)
                nel_min = st.number_input("Min NEL (Mcal/kg KM)", 0.9, 2.0,
                                          float(round(req["nel_min"], 2)), 0.02)
                ca_min = st.number_input("Min kalsiyum (% KM)", 0.2, 2.0,
                                         req["calcium_min"] / 10, 0.05)
                p_min = st.number_input("Min fosfor (% KM)", 0.1, 1.5,
                                        req["phosphorus_min"] / 10, 0.05)
            with col_b:
                cp_max = st.number_input("Max ham protein (% KM)", 10.0, 35.0,
                                         float(round(req["cp_max"], 1)), 0.5)
                nel_max = st.number_input("Max NEL (Mcal/kg KM)", 1.0, 2.5,
                                          float(round(req["nel_max"], 2)), 0.02)
                ca_max = st.number_input("Max kalsiyum (% KM)", 0.3, 3.0,
                                         req["calcium_max"] / 10, 0.05)
                p_max = st.number_input("Max fosfor (% KM)", 0.2, 2.0,
                                        req["phosphorus_max"] / 10, 0.05)
        else:
            dm_intake = req["dm_intake_kg"]
            mp_min, cp_max = req["mp_min"], req["cp_max"]
            nel_min, nel_max = req["nel_min"], req["nel_max"]
            ca_min, ca_max = req["calcium_min"] / 10, req["calcium_max"] / 10
            p_min, p_max = req["phosphorus_min"] / 10, req["phosphorus_max"] / 10

        ratio = ca_min / p_min if p_min > 0 else 0
        st.caption(f"Hedeflenen Ca:P oranı {ratio:.2f} (önerilen 1.5–2.5)")

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

    if st.button("Güncel piyasa fiyatlarını çek", icon=":material/refresh:", width="stretch"):
        with st.spinner("Borsa ve piyasa fiyatları çekiliyor..."):
            try:
                resp = requests.post(f"{BACKEND_URL}/feeds/sync-prices", timeout=60)
                if resp.status_code == 200:
                    info = resp.json()
                    st.cache_data.clear()
                    for key in [k for k in st.session_state if k.startswith("price_editor")]:
                        st.session_state.pop(key, None)
                    st.session_state["price_sync_message"] = info["message"]
                    st.rerun()
                else:
                    st.error(f"Senkronizasyon hatası: {resp.text}", icon=":material/error:")
            except requests.exceptions.Timeout:
                st.error("İstek zaman aşımına uğradı (60 sn).", icon=":material/error:")
            except Exception as exc:
                st.error(f"Bağlantı hatası: {exc}", icon=":material/error:")

    # Senkron sonucu rerun'dan sonra gösterilir; canlı/taban ayrımını net söyler.
    sync_message = st.session_state.pop("price_sync_message", None)
    if sync_message:
        st.caption(sync_message)

targets = {
    "manual": manual_targets,
    "dm_intake_kg": dm_intake,
    "mp_min": mp_min, "cp_max": cp_max,
    "starch_max": req["starch_max"], "fat_max": req["fat_max"],
    "nel_min": nel_min, "nel_max": nel_max,
    "ca_min": ca_min, "ca_max": ca_max,
    "p_min": p_min, "p_max": p_max,
    # Grubun rasyon kuralları — sonuç kartları bunlara göre "hedefte" der.
    "ndf_min": req["ndf_min"], "ndf_max": req["ndf_max"],
    "forage_min": req["forage_ratio_min"], "forage_max": req["forage_ratio_max"],
}


# ============================
# ANA EKRAN
# ============================

st.title("RationAI")
st.caption("Süt ineği için en düşük maliyetli rasyon hesaplayıcı")

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
        help=f"Bu grupta rasyonun %{targets['forage_min']:.0f}-{targets['forage_max']:.0f}'i "
             "kaba yemden gelir.",
    )
with col_concentrate:
    selected_concentrates = st.multiselect(
        "Kesif yemler",
        options=concentrate_options,
        placeholder="Tahıl, küspe, kepek…",
    )

selected_minerals = st.multiselect(
    "Mineraller",
    options=mineral_options,
    default=[name for name in DEFAULT_MINERALS if name in mineral_options],
    help="Otomatik seçili gelir; miktarlarını çözücü Ca/P açığına göre belirler.",
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
    feeds_by_name = {feed["name"]: feed for feed in feeds_data}

    def _health_cap(feed: Dict) -> Optional[float]:
        """Sağlık sınırını canlı ağırlığa ölçekler (tablo değeri 600 kg için)."""
        cap = feed.get("max_kg_per_day")
        return round(cap * (body_weight / 600.0), 1) if cap else None

    price_df = pd.DataFrame([
        {
            "Yem": name,
            "Fiyat (TL/kg)": round(feeds_by_name[name].get("price_per_kg", 0) or 0, 2),
            "Kaynak": feeds_by_name[name].get("price_source") or "taban fiyat",
            "Max kg/gün": _health_cap(feeds_by_name[name]),
            "HP (%)": round(feeds_by_name[name].get("crude_protein", 0) or 0, 1),
            "NEL (Mcal/kg)": round(feeds_by_name[name].get("net_energy_lactation", 0) or 0, 2),
        }
        for name in selected_feeds
    ])

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
                help="Fiyat canlı borsadan mı geldi yoksa taban fiyat mı? Silaj, kuru ot, "
                     "küspe ve mineraller borsada işlem görmediği için taban fiyatta kalır.",
            ),
            "Max kg/gün": st.column_config.NumberColumn(
                format="%.1f",
                help="Hayvan sağlığı sınırı — canlı ağırlığa göre ölçeklenmiş günlük azami "
                     "miktar. Boş ise as-fed sınırı yok. Bu sınır hiçbir koşulda aşılmaz.",
            ),
            "HP (%)": st.column_config.NumberColumn(format="%.1f"),
            "NEL (Mcal/kg)": st.column_config.NumberColumn(format="%.2f"),
        },
        disabled=["Yem", "Kaynak", "Max kg/gün", "HP (%)", "NEL (Mcal/kg)"],
        hide_index=True,
        num_rows="fixed",
        width="stretch",
        # Anahtar seçime bağlı: seçim değişince tablo satırları kayar ve eski
        # düzenlemeler yanlış yeme uygulanabilirdi.
        key="price_editor_" + "|".join(selected_feeds),
    )
    price_overrides = dict(zip(edited_prices["Yem"], edited_prices["Fiyat (TL/kg)"]))
else:
    st.info("Başlamak için en az bir yem seç.", icon=":material/info:")

calculate = st.button(
    "Rasyonu hesapla",
    type="primary",
    icon=":material/calculate:",
    width="stretch",
    disabled=not selected_feeds,
)

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
        result = call_optimization_api(payload)

    if result:
        st.session_state["last_result"] = result
        st.session_state["last_targets"] = targets
    else:
        st.session_state.pop("last_result", None)

if "last_result" in st.session_state:
    render_results(st.session_state["last_result"], st.session_state["last_targets"])

st.caption("RationAI · FastAPI + PuLP · İhtiyaçlar: NASEM 2021 · Yem verileri: INRA 2004 tabloları")
