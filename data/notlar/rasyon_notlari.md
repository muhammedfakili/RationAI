# Rasyon notları (defter dökümü)

Kaynak: kullanıcının zootekni defteri (17 fotoğraf, repoda yok). Bu dosya onun düz metin
dökümü ve rasyon motoruna işlenirken verilen kararlardır. **[karar]** ile işaretli satırlar
defterden farklıdır ya da defterde yoktu; gerekçesi yanında yazar.

Kısaltmalar: CA canlı ağırlık (kg) · SV süt verimi (kg/gün) · SY süt yağı (%) ·
KM kuru madde · KMT kuru madde tüketimi · DSV düzeltilmiş (%4 yağlı) süt verimi ·
HP ham protein · ME metabolik enerji (kcal) · GCAA günlük canlı ağırlık artışı ·
BBCA / BSCA besi başı / besi sonu canlı ağırlık · ATK ayçiçeği tohumu küspesi ·
SFK soya fasulyesi küspesi · PTK pamuk tohumu küspesi · DCP dikalsiyum fosfat

---

## 1. Yem tablosu (HP % ve ME kcal/kg — **doğal / yaş hal**)

Aralıklı değerlerde orta değer alınır; ATK, SFK ve PTK piyasada ayrı ürün olduğu için
ayrı yem olur. Tabloda olmayan besinler (KM, NDF, Ca, P, nişasta, RUP) NRC 2001'den gelir.

### Kaba yemler

| Yem | HP % | kcal/kg | Not |
|---|---|---|---|
| Yonca kuru otu | 16 | 2200 | |
| Fiğ kuru otu | 10–19 | 2000 | |
| Korunga kuru otu | 12–15 | 1800 | |
| Çayır kuru otu | 7–10 | 1500–1850 | |
| Mısır silajı | 3.5 | 900 | %30 KM |
| Korunga silajı | 3.5 | 500 | |
| Ot silajı (yaş) | 4 | 600 | el yazısı ek |
| Nemli mısır | 6 | 2260 | el yazısı ek |
| Arpa samanı | 4.5 | 1550 | |
| Buğday samanı | 3–4 | 1400 | |
| Yulaf samanı | 4–6 | 2000 | |
| Mercimek samanı | 10–14 | 1000 | |
| Nohut samanı | 5–6 | 1600 | |
| Üçgül samanı | 9 | 1350 | |
| Burçak samanı | 4.5 | 1500 | |

### Kesif yemler

| Yem | HP % | kcal/kg | Not |
|---|---|---|---|
| ATK | 24–36 | 2200 | ayrı ürün: ATK 24 ve ATK 36, ikisi de 2200 |
| SFK | 46–48–52 | 2500 | ayrı ürün: SFK 46 / 48 / 52 |
| PTK | 24–42 | 2000–2500 | ayrı ürün: PTK 24 → 2000, PTK 42 → 2500 |
| Kanola küspesi | 38–40 | 2000–2400 | |
| Yer fıstığı küspesi | 40–44 | 2400 | |
| Keten tohumu küspesi | 32–36 | 2300–2700 | |
| Fındık küspesi | 34–40 | 2000–2200 | |
| Buğday kepeği | 15 | 2500 | |
| Razmol | 14–15 | 2500 | |
| Bonkalite | 15–19 | 3000 | |
| Pirinç kepeği | 10 | 2200–2800 | |
| Çavdar kepeği | 13–15 | 2000 | |
| Mısır dane | 6.5–9 | 3000–3200 | |
| Sorgum | 11 | 2900 | |
| Buğday | 12–13 | 2900 | |
| Arpa | 11.5 | 2850 | |
| Tritikale | 13.5–14 | 2750 | |
| Çavdar | 14 | 2650 | |
| Yulaf | 16 | 2500–2600 | |
| Fiğ dane | 32 | 2700 | |
| Bakla dane | 20–26 | 2600 | |
| Soya dane | 34 | 3300 | |
| Malt çimi yaş / kuru | 6 / 28 | 400 / 2200 | |
| Malt posası yaş / kuru | 6 / 25 | 400 / 1500 | |
| Bira mayası yaş / kuru | 11 / 50–55 | 600 / 2800 | |
| DDGS gold | 26–30 | 3000 | |
| Şeker pancarı posası yaş / kuru | 2 / 9 | 400 / 2100 | |
| Melas | 10 | 2200 | |
| Üre (saf) | 292 | — | protein olmayan azot (POA) — sınır aşağıda |
| Ürik asit | 210 | — | POA |
| Amonyum sülfat | 132 | — | POA |
| Baypass yağ | — | 9800 | |
| Bitkisel yağ | — | 8800 | **[karar]** tabloda yok, defterdeki buzağı yeminde var |
| ~~Tüy unu, kan unu, balık unu~~ | 84 / 80 / 60–66 | | **[karar]** eklenmez — geviş getirenlerde hayvansal un yasak |
| ~~Ecomass, vinaz~~ | 55 / 40 | 3050 / — | **[karar]** eklenmez — içerikleri belirsiz |

---

## 2. Süt ineği ihtiyaçları (sistem ME + HP — backend/app/requirements.py)

| İhtiyaç | Formül |
|---|---|
| KM, SV < 24 L | CA × 0.025 + SV × 0.10 |
| KM, SV ≥ 24 L | CA × 0.022 + SV × 0.20 |
| KM, kurudaki inek | ~~CA × 0.025~~ **[karar]** NASEM 2021 kuru inek denklemi (~CA × 0.0185). %2.5 ile tablodaki enerji değerleri günde ~12 700 kcal fazla (~1.8 kg/gün yağlanma) veriyordu |
| Su (L/gün) | 12.3 + 2.15 × KMT + 0.73 × SV |
| Yaşam payı HP (g) | 70.7 + CA^0.75 × 2.77 |
| Verim payı HP (g) | (43.6 + 11.5 × SY) × SV — parantez 1 litre sütün HP'si |
| Yaşam payı ME (kcal) | 0.133 × CA^0.75 × 1000 |
| Verim payı ME (kcal) | (0.577 + 0.165 × SY) × SV × 1000 |
| Gebelik ME (kcal) | yaşam payı × 0.3 — **[karar]** gebeliğin 190. gününden itibaren (defter: 3. aydan; NRC 2001 190. gün, erken başlarsa günde ~0.7 kg fazla yağlanma) |
| DSV | (SY × 0.15 + 0.4) × SV |
| Yaşam payı Ca (g) | 0.0154 × CA / 0.38 |
| Verim payı Ca (g) | 1.22 × DSV / 0.38 |
| Yaşam payı P (g) | 0.0143 × CA / 0.5 |
| Verim payı P (g) | 0.99 × DSV / 0.5 |

Alt sınırlar: yaşam payı enerji 10 000 kcal'ın, yaşam payı HP 350 g'ın altına inemez.
Fazladan alınan her 7000 kcal ≈ 1 kg canlı ağırlık.

### Uygulamada eklenen kararlar

- **[karar]** Kuru dönemde HP en az %12 (erken) / %13 (doğuma yakın) KM: formül kuru inekte
  yalnızca yaşama payını verir (~%3 KM); fetüs ve kolostrum için NASEM tabanı korunur.
- **[karar]** Sağmalda HP üst sınırı en az %16 KM: tablodaki mısır silajı %11.7 HP (KM) olduğu
  için düşük verimli inekte "ihtiyaç + 4" tavanının (%13) altına inen yenebilir rasyon yok.
- **[karar]** Enerji fazlası: tablodaki ME değerleri NRC'den ~%10–15 yüksek; düşük verimli
  (≲ 25 L) ve kuru inekte rasyonun enerjisi ihtiyacın altına inemiyor. Program fazlayı en aza
  indirir ve "günde X kcal fazla → ~Y kg/gün" diye uyarır (7000 kcal ≈ 1 kg). Mısır silajının
  %30 KM'si kullanıcı tarafından doğrulandı (Ekim 2026); fazla bu yüzden uyarı olarak kalır.

---

## 3. Rasyon kuralları

Yüzdeler **tüm rasyonun KM'si** içindedir.

- Arpa en fazla %40 (defter %40–45; temkinli taraf).
- Buğday en fazla %10.
- ATK ve SFK en fazla %25 (defter bir yerde %20, bir yerde %20–25).
- Eklenen yağ en fazla %6.
- Dane : küspe oranı 6 : 1 — **[karar]** sadece besi sığırı ve besi yeminde. 6:1 karışımın
  HP'si %12–15 (besi yemi aralığı); süt yemi %21–23 için ~1.6:1 – 2.8:1 gerekir, süt ineğinde
  sert kural olsaydı protein hedefi hiç tutmazdı.
- **[karar]** Protein olmayan azot (üre, ürik asit, amonyum sülfat) toplamı, üre eşdeğeri
  olarak rasyon KM'sinin en fazla %1'i (FAO: süt yemlerinde %1–2 üre, %3'te lezzet düşer).
- SFK besi yeminde kesinlikle kullanılmaz.
- Mısır silajı günde en fazla 25 kg, yonca kuru otu en fazla 8 kg — 600 kg inek için;
  canlı ağırlıkla ölçeklenir.
- Ca ve P ihtiyacının yaklaşık yarısı yemlerden gelir, kalanı DCP ve mermer tozuyla eklenir.
  DCP: %18 P, %22 Ca. Mermer tozu: **[karar]** %40 Ca (defter %45; saf kalsiyum karbonat
  en fazla %40 Ca içerir).
- Normalde Ca : P = 2 : 1.
- **Kuru dönem** **[karar]** (defter: "Ca:P tersine döner, 1:2", "kuru dönem yeminde Ca olmaz"):
  mermer tozu eklenmez, Ca:P alt sınırı 1.0 (P, Ca'yı geçmez). Doğuma yakın grupta
  Ca ≤ 100 g/gün, P ≤ 45 g/gün. Gerekçe: günde 80 g üstü P süt hummasını tetikler (Horst & Goff).
- Yonca: kuru dönem başında (sütten kesildikten hemen sonra) **ve** doğuma yakın grupta verilmez.

### Kaba yem tercihleri
- Yonca: yüksek verimli inek, buzağı, dana, düve.
- Mısır silajı: hemen bütün hayvanlar.
- Buğday samanı: düve, kurudaki inek.

### Kesif yem tercihleri
- Arpa: besideki hayvanlar; nişasta kaynağı olarak tercih edilir.
- Mısır: hepsi.
- ATK (36): sağmallar. ATK (24): besidekiler.
- SFK (44): yüksek verimliler.
- Yulaf: düveler (tüyü parlatır).
- Buğday kepeği çok iyi bir besi yemidir.

---

## 4. Karma yem hedefleri (1 ton; HP % → ME kcal/kg)

| Yem tipi | HP % | ME kcal/kg |
|---|---|---|
| Süt yemi | 23 | 2900–3000 |
| Süt yemi | 22 | 2750–2800 |
| Süt yemi | 21 | 2700–2750 |
| Buzağı ve süt yemi | 19 | 2650–2700 |
| Buzağı ve süt yemi | 18 | 2650–2700 |
| Buzağı ve süt yemi | 17 | 2500–2600 |
| Besi, düve, kuru dönem, besi başlangıç | 16 | 2500–2600 |
| Besi, düve, kuru dönem, besi başlangıç | 15 | 2500–2600 |
| Besi, düve, kuru dönem, besi başlangıç | 14 | 2500–2700 |
| Besi | 13 | 2500–2700 |
| Besi | 12 | 2500–2700 |

Program karışımı **kendisi bulur**: hedef HP ve ME'yi tutturan en ucuz karışımı, yukarıdaki
kurallara uyarak. Pearson karesinin çok yemli hâli. (backend/app/compound_feed.py)

### Karma yem kararları (Ekim 2026)

- Hedefler karma yemin **doğal hali** için. HP: hedef ile hedef + 0.5 arası; ME: tablodaki aralık.
- **[karar]** Buzağı büyütme yemi ME 2500–2600 (tablodaki %17 satırı; defterdeki 2650 değil).
- Kullanım sınırları rasyondakiyle aynı, bu sefer karma yemin içinde: arpa ≤ %40, buğday ≤ %10,
  ATK/SFK ≤ %25, eklenen yağ ≤ %6, üre eşdeğeri ≤ %1. Melas ≤ 30 kg/ton. Premiks tonda 1 kg sabit.
- **[karar]** Tuz %0.5–1 (üretici etiketleri: NaCl en çok %1).
- **[karar, taslak]** Ca/P (resmi tebliğ metnine ulaşılamadı, üretici etiketlerinden):
  süt yemi Ca %0.8–1.5, P ≥ %0.45; buzağı Ca %1.0–1.5, P ≥ %0.5; düve/besi Ca %0.8–1.5, P ≥ %0.5.
- **[karar]** Kaynaklı hammadde tavanları (karma yemin %'si): buğday kepeği 20 (Feedipedia;
  buzağıda 10), razmol/bonkalite 15, DDGS 25, pamuk küspesi 20, tam yağlı çiğit 20, kuru pancar
  posası 25, mısır gluten yemi 20 (Penn State), kanola 20 (Canola Council), yer fıstığı küspesi 20
  (NDDB), kuru bira mayası 5 (Ankara Üniv.), tritikale tahılların en fazla yarısı (NDDB). Kaynağı
  olmayanlar çeşitlilik için en fazla %30; mısır ve yulafta tavan yok (Penn State).
  Tavansız ilk denemede tek bir ucuz hammadde karışımın %70–78'ini dolduruyordu.
- **[karar]** Buzağı yemine pamuk küspesi (gossipol), tam yağlı çiğit ve yer fıstığı küspesi
  (aflatoksin) girmez.

### Defterdeki örnek tarifler (test için)

**Buzağı büyütme yemi** — hedef %17 HP, 2650 kcal:
arpa 185, mısır 225, yulaf 38, tritikale 60, buğday kepeği 215, ATK(36) 160,
kanola küspesi 70, tuz 6, vitamin-mineral 1, melas 30, mermer tozu 10 → 1000 kg.
HP toplamı 16.8 ✓; ME toplamı 2557 (hedef 2650).

**Buzağı başlangıç yemi** — hedef %18 HP, 2700 kcal:
mısır 260, buğday kepeği 196, arpa 160, tritikale 60, ATK(36) 160, kanola küspesi 80,
SFK 30, tuz 5, melas 30, vitamin 1, mermer tozu 10, bitkisel yağ 8 → 1000 kg.
HP toplamı 18.0 ✓; yağın ME katkısı 704 yazılmış, 70.4 olmalı → ME toplamı 2516 (hedef 2700).

**Pearson karesi:** mısır %8 + fındık küspesi %34 → hedef %20: %53.8 mısır, %46.2 fındık küspesi.
Buğday %14 + ATK %32 → hedef %19: %72.2 buğday, %27.8 ATK (defterde 28.8 yazıyor).

---

## 5. Besi sığırı

- Beside KM tüketimi CA'nın %2.5'i (bir örnekte %3).
- Kaba : kesif oranı besi boyunca %30 : %70; besi başında %50 : %50.
- Yem tüketimi besi ortasındaki ağırlığa göre hesaplanır.
- Diğer giderler yem maliyetinin 1/3'ü kadardır.
- 1 kg karkas yağı 3 kg ete tekabül eder.
- Besi başlangıç yemlerinin proteini yüksek olur.

### Uygulamadaki besi kararları (Ekim 2026 — backend/app/requirements.py)

- ~~KM CA'nın %2.5'i~~ → **[karar, Ekim 2026]** KM NASEM 2016 besi sığırı denklemiyle:
  KMT = (0.96·CA)^0.75 · (0.2435·NEm − 0.0466·NEm² − 0.0869) / NEm, NEm rasyonun ME'sinden.
  Tüketim rasyonun yoğunluğuna bağlı; program "KMT × yoğunluk = ihtiyaç" olan yoğunluğu bulur.
  1.4 kg/gün hedefle: 200 kg %3.0, 400 kg %2.5 (notla aynı), 600 kg %2.25. Sabit %2.5 ağır
  danaya gereksiz enerji fazlası, hafif danaya yetersiz tüketim gösteriyordu. Ulaşılabilir en
  yüksek artış ~1.7 kg/gün (NRC besi verileriyle uyumlu).
- **[karar]** Artış enerjisi: 7000 kcal/kg kuralı 300 kg ve 1 kg/gün'de aynen; ağırlık ve artışla
  NRC'nin tutulan enerji denklemindeki üslerle (CA^0.75 · artış^1.097) büyür → 450 kg'da ~9.8,
  600 kg'da ~12.2 Mcal/kg. Sabit 7000, 600 kg danaya günde 3.4 kg artış gösteriyordu.
- **[karar]** HP: NASEM 2016 tablosu (canlı ağırlık × günlük artış); en fazla %16 KM.
  Ca/P: NRC formülü (bakım + artış, emilim Ca %50, P %68).
- **[karar]** Besi başı geçişi: kaba yem 1-5. gün %50, 6-10 %45, 11-15 %40, 16-20 %35,
  21. günden itibaren %30 (literatür: en az 14 gün, pratikte ~3 hafta, 4-5 günde bir adım).
- **[karar]** Dane:küspe 6:1 **350 kg ve üstünde** (hafif danada %14-18 HP ihtiyacı 6:1'le
  karşılanamıyor). SFK besi rasyonunda da yok.
- **[karar]** Nişasta ve NDF için kaynaklarda sayısal sınır yok; %30 kaba yem tabanı asidozu tutar.
  Toplam yağ ≤ %6.5 KM, Ca:P 1.5-2 (idrar taşı) — Merck / NASEM 2016.
- Sonuç ekranı "beklenen artış ~X kg/gün" gösterir; hedef KM'ye sığmazsa ulaşılabilir artışı söyler.

**Örnek 1:** BBCA 410 kg, GCAA 2 kg, 5 ay. KM = 410 × 0.03 = 12.3 kg (6.15 kaba + 6.15 kesif).
Enerji = 0.133 × 410^0.75 × 1000 = 12 118 kcal + 2 kg × 7000 = 26 118 kcal.

### Besi ekonomisi kararları (Ekim 2026 — backend/app/economics.py)

- **[karar]** Yem maliyeti besi ORTASINDAKİ ağırlığın rasyonundan × besi günü (hocanın yöntemi:
  kullanıcı ortalama bir rakam bilmeli). Rasyon, günlük rasyon ekranındaki çözücüyle kurulur.
- ~~Satış hem canlı hem karkas~~ → **[karar]** Canlı satış yok: besici eti kesimhaneye satar.
  Satış = bitiş ağırlığı × randıman × kesim fiyatı (tek hesap). Randıman varsayılanı
  dana %55 (örnekteki), kuzu %45 (kullanıcı kararı; örnekteki %42 düşük), kullanıcı değiştirir.
- **[karar]** Fiyatlar: yalnız kesim fiyatı (UKON; yedek TOBB karkas), kaynak ve tarih gösterilir; günde 1 otomatik + elle güncelle düğmesi; kullanıcının fiyatı
  geçerli (kaydedilirse senkron ezmez). ESK yalnız Kurban'da fiyat açıklıyor.
- ~~Alış fiyatı varsayılanı borsadaki canlı ortalama~~ → **[karar]** Türkiye'de hayvan
  (besilik de) o günkü kilo başı kesim fiyatından değerlenir, ağırlığa göre ayrı fiyat yok:
  alış = canlı ağırlık × randıman × kesim fiyatı. Kullanıcı isterse kendi alışını girer.
- **[karar]** Kesim fiyatı kaynağı UKON (ukon.org.tr/fiyatlar, haftalık, kombina ve
  kesimhanelerden yağsız dana ve kuzu kesim fiyatı; 7 bölge + Türkiye ortalaması). Bölge
  seçilebilir; TOBB karkas satırları yalnız yedek.
- **[karar]** Diğer giderler varsayılan yemin 1/3'ü; istenirse kalem kalem: ölüm/fire (sığır
  %1.5, kuzu %3 varsayılan), veteriner, işçilik, faiz (alış + yemin yarısı üzerinden).
- Kontrol: 220 kg Simental, 270 gün, 1.5 kg/gün → baş başına 2 770 kg KM (defterde 2 851).

**Örnek 2:** 114 Simental, 6 aylıkta besiye, 15 aylıkta kesim. BBCA 220 kg, 270 gün,
GCAA 1500 g → BSCA 625 kg. Besi ortası (135. gün) 422.5 kg × %2.5 = 10.56 kg KM/gün →
2851 kg KM/baş (%30 kaba, %70 kesif; KM %90). Karkas randımanı %55, kesim fiyatı 420 TL/kg,
hayvan 80 000 TL/baş, kaba yem 6000 TL/ton, kesif 12 000 TL/ton.
→ Fiyatlar uygulamada canlı çekilecek / düzenli güncellenecek.

---

## 6. Küçükbaş

- Doğum yapmış küçükbaş koyundur. Doğum ağırlığı genelde 4 kg alınır.
- Kapalı ortamda KM tüketimi CA'nın %3.5'i.
- Koyunlarda kesif yem en az %16–17 HP olmalı.
- Koyunlar büyükbaşa göre daha büyük partiküllü yemle beslenir.
- Keçi beslenme açısından en hassas olandır.

### Uygulamadaki kuzu besisi kararları (Ekim 2026 — backend/app/requirements.py)

- **[karar]** Kapsam: yalnız kuzu/toklu besisi (erkek, besi). Gebe/emziren koyun ve keçi sonra.
- **[karar]** KM %3.5 sabit değil, NRC 1985 koyun tablosundan ağırlıkla iner: 20 kg %5.0,
  30 kg %4.3, 40 kg %4.0, 50 kg %3.2, 60 kg %2.8 (ara değerler doğrusal). HP (% KM) 16.9 /
  14.7 / 11.6 / 10.0 / 10.0, Ca 5.4 / 5.1 / 4.2 / 3.5 g/kg, P 2.4 / 2.4 / 2.1 / 1.9 g/kg.
  NRC 2007 kontrol: 30 kg / 300 g → %4.15 KM, %12.4 HP, Ca 8 g, P 4 g (Beef+Lamb NZ).
- **[karar]** ME = yaşama 0.093 × CA^0.75 Mcal (NRC NEm 56 kcal ÷ km 0.60 — sığırdaki 0.133 ile
  aynı mantık) + artış: 7000 kcal/kg kuralı 30 kg ve 300 g/gün'de aynen, ağırlık ve artışla
  sığırdaki NRC üsleriyle büyür. NRC orada 7.6-7.9 Mcal/kg veriyor (sığırdaki gibi ~%10 fark).
- **[karar]** Kaba yem besi başında %50'den 20 günde %30'a (sığırla aynı adımlar).
- **[karar]** Kesif yem kısmı en az **%16 HP doğal halde** (kullanıcı notu: "kesif en az
  %16-17"). Tüm rasyon HP'si NRC tablosundan; üst sınır en az %20.
- **[karar]** Bakır: sığır premiksi kuzu rasyonundan çıkarılır, "Koyun Premiksi (Bakırsız)" eklendi.
- **[karar]** İdrar taşı: Ca:P 2-2.5 (Merck), "Amonyum Klorür" seçilirse %0.5 KM sabit.
- **[karar]** Üre ve tüm protein olmayan azot kuzuda yok; soya küspesi besi rasyonunda yok;
  dane:küspe 6:1 kuzuda yok (kesif %16 HP kuralıyla çelişir).
- **[karar]** Yonca kuru otunun 8 kg/gün sınırı (inek) koyunda uygulanmaz; diğer sağlık
  sınırları canlı ağırlığa ölçeklenir.
- Yenebilir en yoğun kuzu rasyonu ölçüldü: yonca + saman 2850, silaj + yonca 2900, çayır otu
  + saman 2750 kcal/kg KM. Hedef artış buna sığmazsa ulaşılabilir artış gram olarak söylenir
  (ör. 50 kg toklu ~290 g/gün; NRC 1985 50 kg için 205 g varsayar).
- Su: KM tüketiminin ~2.5 katı (NRC: 2-3 kat).

**Örnek:** 800 erkek toklu, BBCA 30 kg, GCAA 315 g, 120 gün → BSCA 67.8 kg.
Besi ortası 48.9 kg × %3.5 = 1.711 kg KM/gün → 205.4 kg KM → 233.4 kg yaş yem (KM %88),
%70 kesif, %30 kaba. Karkas randımanı %42 → 28.5 kg; kesim fiyatı 550 TL/kg.

---

## 7. Genel bilgiler (chatbot bilgi tabanı için)

- Süt verimi arttıkça süt yağı düşer.
- Nişasta kaynağı olarak arpa tercih edilir.
