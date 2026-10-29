# RationAI'ın rasyon kuralları ve gerekçeleri

Bu belge, RationAI'ın rasyon hesaplarken uyguladığı kuralları ve **neden** uyguladığını
anlatır. Sohbet asistanı "neden?" sorularını bu belgeye ve rasyon notlarına
(data/notlar/rasyon_notlari.md) dayanarak cevaplar.

## Hesap nasıl yapılır?

RationAI önce hayvanın ihtiyacını hesaplar (kuru madde tüketimi, metabolik enerji, ham
protein, kalsiyum, fosfor, su), sonra seçilen yemlerle bu ihtiyacı karşılayan **en ucuz**
karışımı doğrusal programlamayla bulur. Önce besin hedefleri ve rasyon kuralları kesin
(katı) uygulanır; tutmazsa hedefler esnetilir ve hangisinin neden tutmadığı yazılır.
Enerji birimi kcal ME, protein ham protein (HP) — zootekni derslerindeki sistem.

## Süt ineği (sağmal)

- Kuru madde tüketimi: günde 24 kg altı sütte CA × 0,025 + süt × 0,10; 24 kg ve üstünde
  CA × 0,022 + süt × 0,20.
- Yaşama payı enerji 0,133 × CA^0,75 Mcal (en az 10.000 kcal); süt için
  (0,577 + 0,165 × süt yağı) Mcal/kg süt; gebeliğin 190. gününden sonra yaşama payının %30'u.
- Yaşama payı protein 70,7 + 2,77 × CA^0,75 g (en az 350 g); süt için (43,6 + 11,5 × süt yağı) g/kg.
- Kaba yem rasyon kuru maddesinin %45–65'i olmalı: geviş getirme ve rumen sağlığı için lif gerekir.
- NDF %28–35 KM: NDF fazlaysa inek rasyonu fiziksel olarak tüketemez, azsa asidoz riski artar.
- Nişasta en fazla %28 KM: NDF lifi gösterir ama asidozu yakalamaz; fazla nişasta rumeni asitlendirir.
- Silaj seçilmişse kaba yemin tabanıdır (en az %25 KM); yoksa çözücü silajı bırakıp yoncayı doldururdu.
- Ca:P oranı 1,5–2,5 arasında tutulur.

## Kuru dönem

- Kuru inekte yonca (kuru ot ve silaj) ve mermer tozu **verilmez**: yonca potasyum ve
  kalsiyum bakımından zengindir; doğumdan önce yüksek kalsiyum ve potasyum, doğumda süt
  hummasına (hipokalsemi) yol açar. Kuru dönemde kalsiyum eklenmez.
- Ca:P en az 1,0: kalsiyum eklenmez ama fosfor kalsiyumu geçmemeli; günde 80 g'ı aşan fosfor
  D vitamini üretimini baskılayıp süt hummasını tetikler.
- Doğuma yakın (son 3 hafta): kalsiyum günde en fazla 100 g, fosfor en fazla 45 g.
- Kuru inekte kaba yem payı yüksektir (erken kuru %70–90, doğuma yakın %60–80) ve saman daha
  fazla kullanılabilir; enerji fazlası yağlanmaya, doğum güçlüğüne ve ketozise yol açar.
- Kuru madde tüketimi NASEM kuru inek denklemiyle (~%1,85 CA) hesaplanır; %2,5 ile hesap
  günde ~12.700 kcal enerji fazlası çıkarıyordu.

## Yem kullanım sınırları (rasyon kuru maddesinde)

- Arpa en fazla %40, buğday en fazla %10 (asidoz; buğday hızlı fermente olur).
- Tahıl tek başına en fazla %30–40, küspe (ATK, SFK) en fazla %25 (rasyon notu).
- Saman sağmalda en fazla %8: saman kuru madde başına en ucuz yemdir, sınır olmasa çözücü
  rasyonu samanla doldururdu ve inek yiyemezdi.
- Eklenen yağ toplamı en fazla %6: fazla yağ rumen fermantasyonunu bozar.
- Üre ve diğer protein olmayan azot (amonyum sülfat, ürik asit) toplamı üre eşdeğeri olarak
  en fazla %1: fazlası amonyak zehirlenmesi yapar.
- Günlük sağlık sınırları (600 kg inek için, ağırlıkla oranlanır): silaj 25 kg (asidoz,
  ishal), yonca kuru otu 8 kg, saman 4 kg, melas 1,5 kg, yaş pancar posası 20 kg, üre 200 g.
  Bu sınırlar hiçbir durumda aşılmaz.
- Tuz seçilmişse en az %0,25 KM; sodyum ihtiyacı başka yerden karşılanmaz.

## Besi sığırı

- Kuru madde tüketimi NASEM 2016 besi denklemiyle: 200 kg danada canlı ağırlığın ~%3'ü,
  400 kg'da ~%2,5'i, 600 kg'da ~%2,25'i.
- Artış enerjisi: "7000 kcal ≈ 1 kg canlı ağırlık" kuralı 300 kg danada geçerlidir; hayvan
  ağırlaştıkça kilonun çoğu yağ olur, 1 kg artış daha çok enerji ister (450 kg'da ~9.800,
  600 kg'da ~12.200 kcal).
- Kaba yem besi başında %50, 20 günde 5 adımda %30'a iner: rumenin kesif yeme alışması
  gerekir, ani geçiş asidoz yapar.
- 350 kg'dan sonra dane:küspe en az 6:1; besi rasyonunda soya küspesi kullanılmaz.
- Ca:P 1,5–2: idrar taşına karşı.

## Küçükbaş (kuzu, toklu) besisi

- Kuru madde ağırlığa göre azalır: 20 kg kuzu canlı ağırlığının ~%5'ini, 50 kg toklu ~%3'ünü yer.
- Kesif yem kısmı en az %16 ham protein (doğal halde).
- Sığır premiksi koyuna **verilmez**: koyun bakıra çok hassastır, bakır karaciğerde birikir ve
  hemolitik krizle ölüm yapar. Bakırsız koyun premiksi kullanılır.
- İdrar taşı (ürolitiyazis) erkek kuzuda yaygındır: Ca:P en az 2 olmalı ve rasyona %0,5
  amonyum klorür eklenir (idrarı asitlendirir).
- Kuzu besisinde üre ve diğer protein olmayan azot verilmez.

## Karma yem

- Hedefler doğal halde, 1 kg karma yem için: süt yemi %21–23 HP (2700–3000 kcal), buzağı
  yemi %17–19, düve/kuru/besi başlangıç %14–16, besi yemi %12–13.
- Hammadde sınırları kaynaklıdır: DDGS %25, buğday kepeği %20, pamuk küspesi %20, arpa %40,
  buğday %10; kaynağı olmayan hammadde en fazla %30 (çeşitlilik için).
- Buzağı yemine pamuk küspesi (gossipol), tam yağlı çiğit ve yer fıstığı küspesi (aflatoksin)
  girmez. Premiks tonda 1 kg, tuz %0,5–1.

## Besi ekonomisi

- Yem maliyeti besi ortasındaki ağırlığın rasyonundan hesaplanır (rasyon günlük maliyeti ×
  besi günü).
- Türkiye'de hayvan o günkü kilo başı kesim fiyatından değerlenir: alış = canlı ağırlık ×
  karkas randımanı × kesim fiyatı; satış da besicinin eti kesimhaneye sattığı gibi.
- Kesim fiyatı UKON'un (Ulusal Kırmızı Et Konseyi) haftalık kombina fiyatlarıdır.
- Diğer giderler yem maliyetinin 1/3'ü kabul edilir ya da kalem kalem girilir.
- Randıman varsayılanı dana %55, kuzu %45.
