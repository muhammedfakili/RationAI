from .database import Base
from sqlalchemy import Column, Integer, String, Float, DateTime

class Feed(Base):
    __tablename__ = "feeds"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True)
    # Rasyon kurallarının dayandığı sınıf: silage/hay/straw/grain/protein/byproduct/
    # fat/mineral/salt. database._CURATED_FEEDS'ten tohumlanır.
    feed_type = Column(String, index=True, default="unknown")
    price_per_kg = Column(Float)
    # Fiyatın nereden geldiği: "Bandırma TB (+3 borsa)", "taban fiyat" vb.
    price_source = Column(String, default="taban fiyat")
    price_updated_at = Column(DateTime, nullable=True)
    # Hayvan sağlığı sınırı: 600 kg canlı ağırlık için günlük azami doğal hali miktar
    # (kg/gün). Solver canlı ağırlıkla oranlar. None = as-fed sınırı yok.
    max_kg_per_day = Column(Float, nullable=True)
    dry_matter = Column(Float)
    crude_protein = Column(Float)
    metabolic_energy = Column(Float)
    net_energy_lactation = Column(Float)  # NEL Mcal/kg DM (NRC 2001)
    ndf = Column(Float)  # Neutral Detergent Fiber % DM (NRC 2001)
    calcium = Column(Float)
    phosphorus = Column(Float)
    starch = Column(Float, nullable=True)         # % KM — asidoz sınırı
    ether_extract = Column(Float, nullable=True)  # ham yağ, % KM
    rup = Column(Float, nullable=True)            # by-pass protein, HAM PROTEİNİN %'si