import os
from datetime import datetime

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

# Load environment variables from .env file in project root
# __file__ is .../backend/app/database.py
# Go up three levels to reach project root: backend/app -> backend -> project_root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
dotenv_path = os.path.join(BASE_DIR, '.env')
load_dotenv(dotenv_path)

# Get the database URL from environment variable
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is not set. Please check your .env file.")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# Küratörlü taban fiyatların toplandığı tarih. `price_updated_at` olarak yazılır ki arayüz
# taban fiyatın kaç günlük olduğunu canlı fiyatlarla aynı alandan hesaplayabilsin.
BASELINE_PRICE_DATE = datetime(2026, 7, 15)
BASELINE_SOURCE = "taban fiyat"
MANUAL_SOURCE = "elle girildi"

# Sonradan eklenen sütunlar: ad -> SQL tipi. SQLite ve PostgreSQL'de aynı DDL çalışır.
_ADDED_COLUMNS = {
    "feed_type": "VARCHAR",
    "price_source": "VARCHAR",
    "price_updated_at": "TIMESTAMP",
    "max_kg_per_day": "FLOAT",
    "starch": "FLOAT",
    "ether_extract": "FLOAT",
    "rup": "FLOAT",
    "display_order": "INTEGER",
}

# Yeniden adlandırılan yemler: eski ad -> yeni ad. Satır yerinde yeniden adlandırılır ki
# canlı çekilmiş ya da kullanıcının kaydettiği fiyat kaybolmasın.
_RENAMED_FEEDS = {
    "Ayçiçeği Küspesi": "Ayçiçeği Küspesi 36",
    "Soya Küspesi": "Soya Küspesi 46",
    "Pamuk Tohumu Küspesi": "Pamuk Tohumu Küspesi 24",
    "Kolza Küspesi": "Kanola Küspesi",
    "Soya-Fullfat": "Soya Dane (Fullfat)",
    "Kuru Bira Posası": "Malt Posası (Kuru)",
    "Çayır Otu": "Çayır Kuru Otu",
}


def _ensure_columns():
    """Eksik sütunları `ALTER TABLE` ile ekler (hafif şema geçişi)."""
    inspector = inspect(engine)
    if "feeds" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("feeds")}
    missing = {name: sql for name, sql in _ADDED_COLUMNS.items() if name not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for name, sql_type in missing.items():
            conn.execute(text(f"ALTER TABLE feeds ADD COLUMN {name} {sql_type}"))


def seed_initial_feeds(db):
    """Yem tablosunu küratörlü listeyle senkronlar (upsert).

    Eksik yemler eklenir, mevcut yemlerin besin değerleri ve `feed_type`'ı güncellenir.
    `price_per_kg`'a DOKUNULMAZ: canlı fiyat senkronu (`scraper.update_feed_prices`) ve
    kullanıcının girdiği fiyatlar korunsun diye. Yeni eklenen yemler küratörlü fiyatla gelir.
    """
    from .models import Feed

    _ensure_columns()

    existing = {feed.name: feed for feed in db.query(Feed).all()}
    for old, new in _RENAMED_FEEDS.items():
        if old in existing and new not in existing:
            existing[new] = existing.pop(old)
            existing[new].name = new

    for data in _get_curated_feeds():
        feed = existing.get(data["name"])
        if feed is None:
            db.add(Feed(price_source=BASELINE_SOURCE, price_updated_at=BASELINE_PRICE_DATE, **data))
            continue
        for field, value in data.items():
            if field == "name":
                continue
            if field == "price_per_kg":
                # Canlı borsadan gelen veya kullanıcının girdiği fiyatı ezme;
                # yalnızca taban fiyatta duran kayıtlar güncellenir.
                if feed.price_source in (None, "", BASELINE_SOURCE):
                    feed.price_per_kg = value
                    feed.price_source = BASELINE_SOURCE
                    feed.price_updated_at = BASELINE_PRICE_DATE
                continue
            setattr(feed, field, value)

    db.commit()


# Besi ekonomisi fiyatları (TL/kg): anahtar, ad, taban fiyat. Yalnız kesim (karkas) fiyatı:
# besici eti kesimhaneye satar, hayvan da (besilik dahil) kesim fiyatından değerlenir —
# canlı hayvan fiyatı kullanılmaz (kullanıcı). Taban = UKON 22 Ekim 2026 Türkiye ortalaması.
# `scraper.update_market_prices` günde bir canlısını yazar; UKON'un bölge satırları
# ("dana_karkas@Ege Bölgesi") ilk senkronda eklenir.
MARKET_BASELINE_DATE = datetime(2026, 10, 27)
_MARKET_PRICES = [
    ("dana_karkas", "Dana karkas", 586.0),
    ("kuzu_karkas", "Kuzu karkas", 617.0),
]


def seed_market_prices(db):
    """Fiyat satırlarını ekler; taban fiyatta duranları günceller (canlı ve elle girilene dokunmaz)."""
    from .models import MarketPrice

    existing = {row.key: row for row in db.query(MarketPrice).all()}
    known = {key for key, _, _ in _MARKET_PRICES}
    for key, row in existing.items():
        if key.split("@", 1)[0] not in known:   # artık kullanılmayan satırlar (eski canlı fiyatlar)
            db.delete(row)
    for key, label, price in _MARKET_PRICES:
        row = existing.get(key)
        if row is None:
            db.add(MarketPrice(key=key, label=label, price_per_kg=price,
                               price_source=BASELINE_SOURCE, price_updated_at=MARKET_BASELINE_DATE))
            continue
        row.label = label
        if row.price_source in (None, "", BASELINE_SOURCE):
            row.price_per_kg = price
            row.price_updated_at = MARKET_BASELINE_DATE
    db.commit()


# Küratörlü yem tablosu — projedeki TEK tohumlama kaynağı.
#
# HP ve enerji KULLANICININ YEM TABLOSUNDAN gelir (data/notlar/rasyon_notlari.md §1) ve
# tabloda yazıldığı gibi DOĞAL (yaş) HALDE girilir: HP % ve ME kcal/kg. Aralıklı değerlerde
# orta değer alındı. `_get_curated_feeds()` bunları solver'ın beklediği KURU MADDE bazına
# çevirir. Tabloda olmayan yemlerin HP/ME'si NRC 2001'den, aynı doğal hal birimine
# çevrilerek yazıldı (yorumda "NRC").
#
# Diğer sütunlar (hepsi KURU MADDE bazında, NRC 2001 / INRA 2004 tipik değerleri):
#   NDF   : % KM                    Ca, P : g/kg KM
#   starch: nişasta % KM            EE    : ham yağ % KM
#   RUP   : by-pass protein, HAM PROTEİNİN %'si — RDP = HP × (1 − RUP)
# Laboratuvar analizi varsa onunla değiştirilmeli.
#
# `feed_type` rasyon kurallarını belirler (bkz. optimization.MAX_INCLUSION_BY_TYPE):
#   silage · hay · straw · grain · protein · byproduct · fat · npn · mineral · salt
#
# `max_kg` = HAYVAN SAĞLIĞI SINIRI: 600 kg canlı ağırlıklı bir inek için günlük azami
# doğal hali miktar (kg/gün). Solver bunu canlı ağırlıkla oranlar ve değişken üst sınırı
# olarak kurar — yani gevşetilmiş modda bile aşılamaz. None = as-fed sınırı yok
# (tahıl ve küspeleri zaten KM yüzdesi tavanları tutuyor).
#
# Sıra arayüzdeki sıradır (`display_order`): her bölümde en yaygın yemler başta.
#
# Fiyatlar Temmuz 2026 Türkiye piyasası (USD/TRY ~47.4); tahıllar TOBB borsa
# ortalamalarından, kaba yem ve küspeler piyasa araştırmasından. Ekim 2026'da eklenen
# yemlerin fiyatı (yorumda "tahmini") benzer yemlere oranlanmış TAHMİNDİR.
_FEED_FIELDS = (
    "name", "feed_type", "price_per_kg", "dry_matter", "crude_protein",
    "metabolic_energy", "ndf", "calcium", "phosphorus",
    "max_kg_per_day", "starch", "ether_extract", "rup",
)

_CURATED_FEEDS = [
    # ad, tip, TL/kg, KM %, HP % (doğal), ME kcal/kg (doğal), NDF, Ca, P, max kg, nişasta, EE, RUP
    # ---- KABA YEM -----------------------------------------------------------
    # Silaj sağlık sınırı 25 kg/gün: fazlası asidoz ve ishal yapar. Yonca kuru otu 8 kg.
    ("Mısır Silajı",             "silage",    5.0, 30.0,   3.5,  900, 45.0,   3.0,   2.0, 25.0,  32.0,   3.2,   27),
    ("Yonca Kuru Otu",           "hay",      18.0, 88.0,  16.0, 2200, 40.0,  12.0,   3.0,  8.0,   2.0,   2.5,   22),
    ("Buğday Samanı",            "straw",     5.5, 90.0,   3.5, 1400, 78.0,   3.0,   0.8,  4.0,   1.0,   1.5,   35),
    ("Arpa Samanı",              "straw",     5.5, 90.0,   4.5, 1550, 74.0,   3.5,   1.0,  4.0,   1.0,   1.8,   35),
    ("Fiğ Kuru Otu",             "hay",      16.0, 88.0,  14.5, 2000, 45.0,  11.0,   2.5, None,   3.0,   2.5,   25),
    ("Çayır Kuru Otu",           "hay",      12.0, 85.0,   8.5, 1675, 55.0,   6.0,   2.5, None,   2.0,   2.5,   25),
    ("Korunga Kuru Otu",         "hay",      15.0, 88.0,  13.5, 1800, 46.0,  12.0,   2.4, None,   3.0,   2.5,   35),
    ("Yonca Silajı",             "silage",    6.0, 35.0,   6.3,  788, 45.0,  13.0,   3.0, 25.0,   2.0,   3.0,   20),  # NRC
    ("Arpa Hasılı Silajı",       "silage",    4.5, 30.0,   3.0,  660, 55.0,   4.0,   2.5, 25.0,  15.0,   3.0,   22),  # NRC
    ("İtalyan Çimi Silajı",      "silage",    5.0, 30.0,   3.9,  690, 50.0,   6.0,   3.0, 25.0,   2.0,   3.5,   20),  # NRC
    ("Ot Silajı",                "silage",    4.0, 30.0,   4.0,  600, 55.0,   6.0,   3.0, 25.0,   2.0,   3.5,   20),  # tahmini
    ("Korunga Silajı",           "silage",    4.5, 30.0,   3.5,  500, 45.0,  12.0,   2.5, 25.0,   2.0,   3.0,   30),  # tahmini
    ("Yulaf Samanı",             "straw",     5.5, 90.0,   5.0, 2000, 70.0,   2.5,   1.0,  4.0,   2.0,   2.0,   35),  # tahmini
    ("Mercimek Samanı",          "straw",     6.0, 90.0,  12.0, 1000, 60.0,  10.0,   1.5,  4.0,   1.0,   1.5,   30),  # tahmini
    ("Nohut Samanı",             "straw",     5.5, 90.0,   5.5, 1600, 65.0,   8.0,   1.2,  4.0,   1.0,   1.5,   30),  # tahmini
    ("Burçak Samanı",            "straw",     6.0, 90.0,   4.5, 1500, 65.0,   8.0,   1.2,  4.0,   1.0,   1.5,   30),  # tahmini
    ("Üçgül Samanı",             "straw",     6.0, 90.0,   9.0, 1350, 60.0,  10.0,   1.5,  4.0,   1.0,   1.5,   30),  # tahmini

    # ---- KESİF YEM ----------------------------------------------------------
    # Tahıllar TOBB borsasından canlı güncellenir.
    ("Arpa",                     "grain",    14.8, 88.0,  11.5, 2850, 18.0,   0.6,   3.2, None,  58.0,   2.2,   30),
    ("Mısır",                    "grain",    14.0, 88.0,  7.75, 3100, 10.0,   0.4,   2.8, None,  72.0,   4.2,   52),
    ("Buğday Kepeği",            "byproduct", 9.6, 88.0,  15.0, 2500, 42.0,   1.0,   9.0, None,  22.0,   4.5,   25),
    ("Ayçiçeği Küspesi 36",      "protein",  18.0, 90.0,  36.0, 2200, 35.0,   4.0,  11.0, None,   1.0,   2.0,   26),
    ("Soya Küspesi 46",          "protein",  19.5, 89.0,  46.0, 2500, 14.0,   3.5,   7.0, None,   3.0,   1.5,   35),
    ("Buğday",                   "grain",    15.5, 88.0,  12.5, 2900, 12.0,   0.5,   3.5, None,  65.0,   2.0,   25),
    ("Ayçiçeği Küspesi 24",      "protein",  13.0, 90.0,  24.0, 2200, 45.0,   3.5,   9.0, None,   1.0,   2.0,   26),  # tahmini
    ("Pamuk Tohumu Küspesi 24",  "protein",  13.5, 90.0,  24.0, 2000, 50.0,   2.0,   9.0, None,   1.0,   2.0,   43),
    ("Mısır DDGS",               "protein",  13.5, 89.0,  28.0, 3000, 32.0,   0.5,   7.5, None,   5.0,  10.0,   52),
    ("Şeker Pancarı Posası",     "byproduct",11.0, 89.0,   9.0, 2100, 42.0,   6.0,   1.0, None,   1.0,   1.0,   45),
    ("Yaş Şeker Pancarı Posası", "byproduct", 2.5, 22.0,   2.0,  400, 45.0,   8.0,   1.0, 20.0,   1.0,   1.0,   40),
    ("Melas",                    "byproduct", 9.0, 75.0,  10.0, 2200,  0.0,   1.5,   0.3,  1.5,   0.0,   0.2,    0),
    ("Tritikale",                "grain",    14.0, 88.0, 13.75, 2750, 14.0,   0.6,   3.0, None,  62.0,   1.8,   25),
    ("Yulaf",                    "grain",    13.5, 88.0,  16.0, 2550, 28.0,   0.8,   3.4, None,  42.0,   5.0,   20),
    ("Razmol",                   "byproduct",13.5, 88.0,  14.5, 2500, 35.0,   1.0,   8.5, None,  28.0,   4.0,   25),  # tahmini
    ("Kanola Küspesi",           "protein",  14.0, 90.0,  39.0, 2200, 30.0,   4.0,   9.0, None,   2.0,   3.5,   35),
    ("Soya Küspesi 48",          "protein",  20.5, 89.0,  48.0, 2500,  9.0,   3.5,   7.0, None,   3.0,   1.5,   35),  # tahmini
    ("Pamuk Tohumu Küspesi 42",  "protein",  17.0, 90.0,  42.0, 2500, 28.0,   2.0,  11.0, None,   1.0,   2.0,   43),  # tahmini
    ("Soya Dane (Fullfat)",      "protein",  23.5, 90.0,  34.0, 3300, 12.0,   2.5,   5.8, None,   4.0,  20.0,   30),
    ("Mısır Gluten Yemi",        "protein",  15.5, 88.0,  17.6, 2684, 36.0,   0.6,   4.0, None,  18.0,   3.5,   25),  # NRC
    ("Tam Yağlı Çiğit",          "protein",  14.5, 92.0, 21.16, 3128, 50.0,   1.5,   6.0, None,   1.0,  19.0,   30),  # NRC
    ("Malt Posası (Yaş)",        "protein",   3.0, 25.0,   6.0,  400, 47.0,   3.0,   6.0, 15.0,   5.0,   9.0,   50),  # tahmini
    ("Malt Posası (Kuru)",       "protein",   9.5, 92.0,  25.0, 1500, 47.0,   3.0,   6.0, None,   5.0,   9.0,   58),
    ("Nemli Mısır",              "grain",    12.0, 72.0,   6.0, 2260, 10.0,   0.4,   2.8, None,  70.0,   4.0,   40),  # tahmini
    ("Bonkalite",                "byproduct",14.5, 88.0,  17.0, 3000, 20.0,   0.8,   7.0, None,  40.0,   4.0,   25),  # tahmini
    ("Mısır Kepeği",             "byproduct",10.3, 87.8,  8.78, 2371, 55.0,   0.5,   5.5, None,  22.0,   6.0,   40),  # NRC
    ("Pirinç Kepeği",            "byproduct",10.0, 90.0,  10.0, 2500, 25.0,   1.0,  15.0, None,  25.0,  14.0,   30),  # tahmini
    ("Çavdar Kepeği",            "byproduct",11.0, 88.0,  14.0, 2000, 40.0,   1.0,   8.0, None,  20.0,   3.5,   25),  # tahmini
    ("Buğday DDGS",              "protein",  13.0, 90.0,  30.6, 2970, 34.0,   0.6,   8.0, None,   4.0,   5.0,   45),  # NRC
    ("Sorgum",                   "grain",    13.5, 88.0,  11.0, 2900, 14.0,   0.4,   3.4, None,  70.0,   3.2,   55),  # tahmini
    ("Çavdar",                   "grain",    15.5, 88.0,  14.0, 2650, 19.0,   0.7,   3.6, None,  58.0,   1.7,   20),  # tahmini
    ("Fiğ Dane",                 "protein",  16.0, 88.0,  32.0, 2700, 18.0,   1.5,   4.0, None,  35.0,   1.5,   25),  # tahmini
    ("Bakla Dane",               "protein",  18.0, 88.0,  23.0, 2600, 16.0,   1.5,   5.0, None,  40.0,   1.5,   25),  # tahmini
    ("Nohut",                    "protein",  30.0, 88.0, 21.74, 2640, 18.0,   1.0,   4.5, None,  45.0,   6.0,   25),  # NRC
    ("Soya Küspesi 52",          "protein",  22.5, 89.0,  52.0, 2500,  8.0,   3.5,   7.0, None,   3.0,   1.5,   35),  # tahmini
    ("Yer Fıstığı Küspesi",      "protein",  17.0, 92.0,  42.0, 2400, 20.0,   2.0,   6.5, None,   5.0,   6.0,   25),  # tahmini
    ("Keten Tohumu Küspesi",     "protein",  15.0, 90.0,  34.0, 2500, 25.0,   4.0,   8.5, None,   3.0,   5.0,   35),  # tahmini
    ("Fındık Küspesi",           "protein",  12.0, 90.0,  37.0, 2100, 28.0,   2.5,   9.0, None,   3.0,   8.0,   30),  # tahmini
    ("Malt Çimi (Kuru)",         "protein",  11.0, 92.0,  28.0, 2200, 45.0,   2.5,   7.0, None,  10.0,   1.5,   25),  # tahmini
    ("Malt Çimi (Yaş)",          "protein",   3.0, 25.0,   6.0,  400, 45.0,   2.5,   7.0, 15.0,  10.0,   1.5,   25),  # tahmini
    ("Bira Mayası (Kuru)",       "protein",  40.0, 93.0,  52.5, 2800,  3.0,   1.5,  14.0, None,   3.0,   1.0,   20),  # tahmini
    ("Bira Mayası (Yaş)",        "protein",   3.5, 22.0,  11.0,  600,  3.0,   1.5,  14.0, 10.0,   3.0,   1.0,   20),  # tahmini

    # ---- Yağlar: eklenen yağ toplamı rasyon KM'sinin en fazla %6'sı ------------
    ("Korunmuş Yağ (Bypass)",    "fat",      75.0, 99.0,   0.0, 9800,  0.0,   0.0,   0.0,  0.5,   0.0,  85.0,    0),
    ("Bitkisel Yağ",             "fat",      60.0, 99.0,   0.0, 8800,  0.0,   0.0,   0.0,  0.5,   0.0,  99.0,    0),  # tahmini

    # ---- Protein olmayan azot: toplamı üre eşdeğeri olarak KM'nin en fazla %1'i --
    # Sağlık sınırı (600 kg inek) zehirlenmeye karşı: üre 200 g, diğerleri aynı azot kadar.
    ("Üre",                      "npn",      22.0, 99.0, 292.0,    0,  0.0,   0.0,   0.0,  0.2,   0.0,   0.0,    0),  # tahmini
    ("Amonyum Sülfat",           "npn",      12.0, 99.0, 132.0,    0,  0.0,   0.0,   0.0, 0.45,   0.0,   0.0,    0),  # tahmini
    ("Ürik Asit",                "npn",      30.0, 99.0, 210.0,    0,  0.0,   0.0,   0.0, 0.28,   0.0,   0.0,    0),  # tahmini

    # ---- MİNERAL ve KATKILAR (Ca/P ürünün kendisi için: KM %100) -------------
    ("Tuz",                      "salt",      5.0, 99.0,   0.0,    0,  0.0,   0.0,   0.0, 0.15,   0.0,   0.0,    0),
    ("Mermer Tozu",              "mineral",   3.5,100.0,   0.0,    0,  0.0, 400.0,   0.0,  0.4,   0.0,   0.0,    0),
    ("DCP (Dikalsiyum Fosfat)",  "mineral",  14.0,100.0,   0.0,    0,  0.0, 220.0, 180.0,  0.2,   0.0,   0.0,    0),
    ("Vitamin-Mineral Premiks",  "mineral",  60.0, 98.0,   0.0,    0,  0.0, 150.0,  60.0, 0.25,   0.0,   0.0,    0),
    ("Sodyum Bikarbonat",        "mineral",  22.0, 99.0,   0.0,    0,  0.0,   0.0,   0.0, 0.25,   0.0,   0.0,    0),
    # Koyuna özgü: sığır premiksindeki bakır koyunda zehirlidir. Amonyum klorür idrar taşına
    # karşı (%0.5 KM); azotu protein sayılmaz (yalnız asitlendirici olarak verilir).
    ("Koyun Premiksi (Bakırsız)", "mineral", 60.0, 98.0,   0.0,    0,  0.0, 150.0,  60.0, 0.25,   0.0,   0.0,    0),  # tahmini
    ("Amonyum Klorür",           "mineral",  25.0, 99.0,   0.0,    0,  0.0,   0.0,   0.0, 0.30,   0.0,   0.0,    0),  # tahmini
]


def _get_curated_feeds():
    """Küratörlü yem listesini `Feed(**data)` ile kullanılabilir sözlükler olarak döndürür.

    Doğal haldeki HP (%) ve ME (kcal/kg) KM bazına çevrilir: HP % KM ve ME Mcal/kg KM.
    NEL tabloda yok; NRC 2001 dönüşümüyle ME'den türetilir (NEL = 0.703·ME − 0.19) ve
    yalnızca bilgi/MP raporu için tutulur — rasyon enerjisi ME ile kurulur.
    """
    feeds = []
    for order, row in enumerate(_CURATED_FEEDS):
        data = dict(zip(_FEED_FIELDS, row))
        dm = data["dry_matter"] / 100.0
        data["crude_protein"] = round(data["crude_protein"] / dm, 2)
        me = data["metabolic_energy"] / 1000.0 / dm
        data["metabolic_energy"] = round(me, 3)
        data["net_energy_lactation"] = round(max(0.703 * me - 0.19, 0.0), 3) if me > 0 else 0.0
        data["display_order"] = order
        feeds.append(data)
    return feeds


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    from backend.app.models import Feed
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as db:
        seed_initial_feeds(db)
        print(f"Seeded {db.query(Feed).count()} feeds")
