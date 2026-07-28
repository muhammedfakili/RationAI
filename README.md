# RationAI

Süt ineği rasyonlarını **maliyet-optimal** ve **besinsel olarak uygulanabilir** biçimde hesaplayan
tam yığın bir uygulama. Kullanıcı hayvanın canlı ağırlığını ve süt verimini girer, elindeki
yemleri seçer; sistem hayvan sağlığı sınırlarını çiğnemeden en ucuz karışımı çözer.

```
Streamlit arayüz  ──HTTP──▶  FastAPI  ──▶  PuLP/CBC doğrusal programlama çözücüsü
                                    ├──▶  PostgreSQL (yem tablosu, fiyatlar)
                                    ├──▶  ChromaDB (RAG bilgi tabanı)
                                    └──▶  LangGraph ajanı → Groq LLM (opsiyonel)
```

## Hızlı başlangıç

```bash
cp .env.example .env      # Groq anahtarı OPSİYONEL, boş bırakabilirsiniz
docker compose up
```

- Arayüz: <http://localhost:8501>
- API dokümanı: <http://localhost:8000/docs>
- Sağlık kontrolü: <http://localhost:8000/api/v1/health>

İlk açılışta veritabanı otomatik olarak **37 yemle** tohumlanır; RAG indeksi imajın içinde hazır
gelir, indirme beklemesi yoktur.

### Groq anahtarı olmadan ne çalışır?

Anahtarsız **her şey** çalışır: rasyon optimizasyonu, yem listesi, canlı fiyat senkronu ve
bilgi tabanı araması. Yalnızca serbest metin sohbeti (`POST /api/v1/agent/query`) devre dışı
kalır ve açıklayıcı bir `503` döner. Sohbeti denemek isterseniz
[console.groq.com](https://console.groq.com) üzerinden ücretsiz bir anahtar alıp `.env` içindeki
`GROQ_API_KEY` alanına yazın.

## Projenin ilginç yanı: iki ayrı istek yolu

| Yol | Uç nokta | LLM devrede mi? |
|---|---|---|
| **Hesaplama** | `POST /api/v1/optimize` | **Hayır.** Seçilen yemler ve kısıtlar birebir çözücüye gider — sonuç tekrarlanabilir ve denetlenebilir. |
| **Sohbet** | `POST /api/v1/agent/query` | Evet; LangGraph ajanı bilgi tabanını arar, yem verisini çeker, gerekirse aynı çözücüyü çağırır. |

Arayüzdeki "Rasyonu Hesapla" düğmesi **her zaman** deterministik yolu kullanır. Bir rasyon
hesabında LLM'in yemleri yeniden adlandırması ya da sessizce düşürmesi kabul edilemez.

## Çözücü ne yapıyor?

Amaç fonksiyonu günlük yem maliyetini minimize eder; asıl iş kısıtlarda:

- **Besin hedefleri** — HP, ME, Ca, P. Önce **hard** kısıt olarak denenir; yalnızca çözümsüzse
  gevşetilip Türkçe açıklama üretilir (`nutrition_mode: hard | relaxed | infeasible`).
- **Fizyolojik uygulanabilirlik** — NDF penceresi %28–35, kaba yem NDF'i ≥ %19, kaba yem oranı
  %45–65, Ca:P oranı 1.5–2.5.
- **Yem tipi tavanları** — saman %8, tahıl %30 (asidoz), yağ %4 … Saman kuru madde başına en ucuz
  yemdir; tavan olmasa çözücü rasyonu samanla doldurur.
- **Hayvan sağlığı sınırları** — günlük azami doğal hali miktarlar (silaj 25 kg, melas 1.5 kg…),
  canlı ağırlıkla ölçeklenir. Bunlar **hiçbir koşulda** gevşetilmez.

## Fiyatlar

`borsa.tobb.org.tr` üzerinden **canlı** fiyat çekilir; ancak borsalarda yalnızca tahıl danesi işlem
gördüğü için ~5 yem canlı, kalan ~32 yem küratörlü taban fiyattadır. Her yem hangi kaynaktan
geldiğini (`price_source`, ör. `"Bandırma TB · 2026-07-28"`) taşır ve arayüz bunu gösterir —
taban fiyatlar asla canlıymış gibi sunulmaz.

## Docker'sız çalıştırma

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Linux/macOS: .venv/bin/pip
.venv/Scripts/python quickstart.py
```

Bu yol varsayılan olarak SQLite kullanır (`DATABASE_URL=sqlite:///rationai.db`).

## Teknoloji

FastAPI · Streamlit · SQLAlchemy · PostgreSQL · PuLP (CBC) · ChromaDB · LangChain/LangGraph ·
Groq (Llama 3.3 70B) · Docker Compose
