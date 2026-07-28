import os
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

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


# Eski isimden yeni isme geçişler. Upsert öncesi uygulanır ki aynı yem iki kez
# görünmesin (ör. genel "Saman" -> tür belirten "Buğday Samanı").
_RENAMED_FEEDS = {
    "Saman": "Buğday Samanı",
}


# Sonradan eklenen sütunlar: ad -> SQL tipi. SQLite ve PostgreSQL'de aynı DDL çalışır.
_ADDED_COLUMNS = {
    "feed_type": "VARCHAR",
    "price_source": "VARCHAR",
    "price_updated_at": "TIMESTAMP",
    "max_kg_per_day": "FLOAT",
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

    # Yeniden adlandırılan yemleri taşı (yeni ad zaten varsa eskisini sil).
    for old_name, new_name in _RENAMED_FEEDS.items():
        stale = existing.get(old_name)
        if stale is None:
            continue
        if new_name in existing:
            db.delete(stale)
        else:
            stale.name = new_name
            existing[new_name] = stale
        existing.pop(old_name)

    for data in _get_curated_feeds():
        feed = existing.get(data["name"])
        if feed is None:
            db.add(Feed(price_source="taban fiyat", **data))
            continue
        for field, value in data.items():
            if field == "name":
                continue
            if field == "price_per_kg":
                # Canlı borsadan gelen veya kullanıcının girdiği fiyatı ezme;
                # yalnızca taban fiyatta duran kayıtlar güncellenir.
                if feed.price_source in (None, "", "taban fiyat"):
                    feed.price_per_kg = value
                    feed.price_source = "taban fiyat"
                continue
            setattr(feed, field, value)

    db.commit()


# Küratörlü yem tablosu — projedeki TEK tohumlama kaynağı.
#
# Birimler (fiyat hariç hepsi KURU MADDE bazında):
#   price : TL/kg (doğal hali)      DM  : %
#   CP    : % KM                    ME  : Mcal/kg KM
#   NEL   : Mcal/kg KM              NDF : % KM
#   Ca, P : g/kg KM
#
# `feed_type` rasyon kurallarını belirler (bkz. optimization.MAX_INCLUSION_BY_TYPE):
#   silage · hay · straw · grain · protein · byproduct · fat · mineral · salt
#
# `max_kg` = HAYVAN SAĞLIĞI SINIRI: 600 kg canlı ağırlıklı bir inek için günlük azami
# doğal hali miktar (kg/gün). Solver bunu canlı ağırlıkla oranlar ve değişken üst sınırı
# olarak kurar — yani gevşetilmiş modda bile aşılamaz. None = as-fed sınırı yok
# (tahıl ve küspeleri zaten KM yüzdesi tavanları tutuyor).
#
# Besin değerleri NRC 2001 / INRA 2004 tablolarındandır.
# Fiyatlar Temmuz 2026 Türkiye piyasası (USD/TRY ~47.4); tahıllar TOBB borsa
# ortalamalarından, kaba yem ve küspeler piyasa araştırmasından. Tahıl fiyatları
# `/api/v1/feeds/sync-prices` ile canlı borsadan güncellenir.
_FEED_FIELDS = (
    "name", "feed_type", "price_per_kg", "dry_matter", "crude_protein",
    "metabolic_energy", "net_energy_lactation", "ndf", "calcium", "phosphorus",
    "max_kg_per_day",
)

_CURATED_FEEDS = [
    # ---- KABA YEM: Silajlar ------------------------------------------------
    # Silaj sağlık sınırı 25 kg/gün: fazlası asidoz ve ishal yapar.
    ("Mısır Silajı",             "silage",    5.0, 30.0,  8.0, 2.30, 1.55, 45.0,   3.0,   2.0, 25.0),
    ("Yonca Silajı",             "silage",    6.0, 35.0, 18.0, 2.25, 1.35, 45.0,  13.0,   3.0, 25.0),
    ("Arpa Hasılı Silajı",       "silage",    4.5, 30.0, 10.0, 2.20, 1.35, 55.0,   4.0,   2.5, 25.0),
    ("İtalyan Çimi Silajı",      "silage",    5.0, 30.0, 13.0, 2.30, 1.40, 50.0,   6.0,   3.0, 25.0),

    # ---- KABA YEM: Kuru otlar ----------------------------------------------
    ("Yonca Kuru Otu",           "hay",      18.0, 88.0, 17.5, 2.35, 1.42, 40.0,  12.0,   3.0, None),
    ("Çayır Otu",                "hay",      12.0, 85.0, 10.0, 2.05, 1.10, 55.0,   6.0,   2.5, None),
    ("Fiğ Kuru Otu",             "hay",      16.0, 88.0, 15.0, 2.25, 1.35, 45.0,  11.0,   2.5, None),
    ("Korunga Kuru Otu",         "hay",      15.0, 88.0, 14.0, 2.20, 1.30, 46.0,  12.0,   2.4, None),

    # ---- KABA YEM: Samanlar ------------------------------------------------
    ("Buğday Samanı",            "straw",     5.5, 90.0,  3.5, 1.55, 0.90, 78.0,   3.0,   0.8,  4.0),
    ("Arpa Samanı",              "straw",     5.5, 90.0,  4.5, 1.65, 0.95, 74.0,   3.5,   1.0,  4.0),

    # ---- KESİF YEM: Tahıl daneleri (TOBB borsasından canlı güncellenir) -----
    ("Mısır",                    "grain",    14.0, 88.0,  8.5, 3.35, 2.05, 10.0,   0.4,   2.8, None),
    ("Buğday",                   "grain",    15.5, 87.0, 12.0, 3.25, 2.00, 12.0,   0.5,   3.5, None),
    ("Arpa",                     "grain",    14.8, 88.0, 11.0, 3.00, 1.85, 18.0,   0.6,   3.2, None),
    ("Yulaf",                    "grain",    13.5, 88.1, 11.5, 2.95, 1.80, 25.0,   0.7,   3.0, None),
    ("Tritikale",                "grain",    14.0, 87.3, 10.5, 3.10, 1.90, 14.0,   0.6,   3.0, None),

    # ---- KESİF YEM: Protein kaynakları -------------------------------------
    ("Soya Küspesi",             "protein",  19.5, 89.0, 45.0, 3.10, 1.95, 14.0,   2.8,   6.5, None),
    ("Soya-Fullfat",             "protein",  23.5, 88.1, 35.0, 3.80, 2.35, 12.0,   2.5,   5.8, None),
    ("Ayçiçeği Küspesi",         "protein",  12.5, 90.0, 32.0, 2.70, 1.45, 40.0,   3.0,  10.0, None),
    ("Kolza Küspesi",            "protein",  14.0, 92.2, 36.0, 2.60, 1.35, 30.0,   4.0,   9.0, None),
    ("Pamuk Tohumu Küspesi",     "protein",  13.5, 90.0, 40.0, 2.60, 1.50, 30.0,   2.0,  11.0, None),
    ("Tam Yağlı Çiğit",          "protein",  14.5, 92.0, 23.0, 3.40, 2.00, 50.0,   1.5,   6.0, None),
    ("Mısır Gluten Yemi",        "protein",  15.5, 88.0, 20.0, 3.05, 1.90, 36.0,   0.6,   4.0, None),
    ("Mısır DDGS",               "protein",  13.5, 88.2, 25.0, 3.20, 2.00, 32.0,   0.5,   7.5, None),
    ("Buğday DDGS",              "protein",  13.0, 90.0, 34.0, 3.30, 2.05, 34.0,   0.6,   8.0, None),
    ("Kuru Bira Posası",         "protein",   9.5, 92.0, 25.0, 2.60, 1.50, 47.0,   3.0,   6.0, None),
    ("Nohut",                    "protein",  30.0, 88.0, 24.7, 3.00, 1.85, 18.0,   1.0,   4.5, None),

    # ---- KESİF YEM: Yan ürünler --------------------------------------------
    ("Buğday Kepeği",            "byproduct", 9.6, 87.1, 15.0, 2.60, 1.40, 42.0,   1.0,   9.0, None),
    ("Mısır Kepeği",             "byproduct",10.3, 87.8, 10.0, 2.70, 1.50, 55.0,   0.5,   5.5, None),
    ("Şeker Pancarı Posası",     "byproduct",11.0, 89.1,  9.0, 2.80, 1.65, 42.0,   6.0,   1.0, None),
    ("Yaş Şeker Pancarı Posası", "byproduct", 2.5, 22.0, 10.0, 2.70, 1.60, 45.0,   8.0,   1.0, 20.0),
    ("Melas",                    "byproduct", 9.0, 75.0,  6.0, 3.00, 1.80,  0.0,   1.5,   0.3,  1.5),

    # ---- KESİF YEM: Korunmuş yağ -------------------------------------------
    ("Korunmuş Yağ (Bypass)",    "fat",      75.0, 99.0,  0.0, 6.50, 5.50,  0.0,   0.0,   0.0,  0.5),

    # ---- MİNERAL ve KATKILAR -----------------------------------------------
    ("Mermer Tozu",              "mineral",   3.5, 96.0,  0.0, 0.00, 0.00,  0.0, 380.0,   0.0,  0.4),
    ("DCP (Dikalsiyum Fosfat)",  "mineral",  14.0, 96.0,  0.0, 0.00, 0.00,  0.0, 220.0, 180.0,  0.2),
    ("Sodyum Bikarbonat",        "mineral",  22.0, 99.0,  0.0, 0.00, 0.00,  0.0,   0.0,   0.0, 0.25),
    ("Vitamin-Mineral Premiks",  "mineral",  60.0, 98.0,  0.0, 0.00, 0.00,  0.0, 150.0,  60.0, 0.25),
    ("Tuz",                      "salt",      5.0, 99.0,  0.0, 0.00, 0.00,  0.0,   0.0,   0.0, 0.15),
]


def _get_curated_feeds():
    """Küratörlü yem listesini `Feed(**data)` ile kullanılabilir sözlükler olarak döndürür."""
    return [dict(zip(_FEED_FIELDS, row)) for row in _CURATED_FEEDS]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


if __name__ == "__main__":
    # Create tables first
    from backend.app.models import Feed, Base
    Base.metadata.create_all(bind=engine)

    # Then seed
    db = SessionLocal()
    try:
        seed_initial_feeds(db)
        print(f"Seeded {db.query(Feed).count()} feeds")
    finally:
        db.close()
