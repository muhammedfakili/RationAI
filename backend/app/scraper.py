"""RationAI - ticaret borsalarından canlı yem fiyatı çekme.

İki kaynak:
1. TOBB birleşik portalı — tahıl danesi (13 borsa, medyan).
2. Samsun Ticaret Borsası ürün sayfaları — TOBB'da OLMAYAN kepek, saman ve küspe.

Kaynak 1: https://borsa.tobb.org.tr/fiyat_borsa.php?borsakod=<KOD>
TOBB'un birleşik portalı; her borsa aynı HTML tablo yapısını kullanır:

    Ürün Adı | Birim | Son İşlem Tarihi | En Az (TL) | En Çok (TL) |
    Ortalama (TL) | İşlem Miktarı | İşlem Adeti | İşlem Tutarı (TL)

KAPSAM SINIRI (önemli): borsalar yalnızca TAHIL DANESİ işlem görüyor — mısır, arpa,
buğday, yulaf, tritikale, nohut. TOBB'da KÜSPELER grubu boş, ÇEŞİTLİ MADDELER'de sadece
kimyon/süpürge teli var; silaj, yonca ve saman borsada hiç işlem görmüyor (yerel pazar).
(Ekim 2026'da TOBB'un tüm ürün listesi tarandı: kepek/saman/yonca/küspe hiçbir grupta yok.)

Kaynak 2: https://www.samsuntb.org.tr/urunler/ — her ürünün kendi "son işlemler" tablosu
(TARİH | EN AZ | EN ÇOK | ORTALAMA | MAHSÜL YILI | SATIŞ ŞEKLİ), yıllara uzanan geçmişle.
Buğday kepeği, buğday samanı, ayçiçeği ve soya küspesi burada işlem görüyor.

Yine de 37 yemin ~8'i canlı çekilebilir, kalanı `database._CURATED_FEEDS` taban fiyatında
kalır. Hangi fiyatın canlı hangisinin taban olduğu `Feed.price_source` alanında tutulur ve
arayüzde gösterilir — sessizce "canlı" numarası yapılmaz.
"""

import logging
import re
import ssl
import statistics
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import certifi
import requests
from bs4 import BeautifulSoup
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import AuthorityInformationAccessOID, ExtensionOID
from sqlalchemy.orm import Session

from .models import Feed

logger = logging.getLogger(__name__)

TOBB_BASE_URL = "https://borsa.tobb.org.tr/fiyat_borsa.php"

# Tahıl işlemi yoğun borsalar. Liste `fiyat_borsa0.php`'den genişletilebilir;
# bayat veri tazelik filtresiyle zaten elenir.
TOBB_EXCHANGES = {
    "5BA20": "Bandırma TB",
    "5ES10": "Eskişehir TB",
    "5SU10": "Sungurlu TB",
    "5ED10": "Edirne TB",
    "5IL10": "Ilgın TB",
    "5CO20": "Çorum TB",
    "5AL05": "Alaca TB",
    "5AK30": "Akşehir TB",
    "5KA40": "Karapınar TB",
    "5UZ10": "Uzunköprü TB",
    "5BI10": "Biga TB",
    "5NA10": "Nazilli TB",
    "5KO10": "Konya TB",
}

# TOBB ürün adının İLK KELİMESİ -> veritabanındaki yem adı.
# İlk kelimeyle eşleme kasıtlı: "MISIR SARI" dane mısırdır ama "MISIR KEPEĞİ" değildir,
# "FİĞ" dane fiğdir ama bizim "Fiğ Kuru Otu"muz kaba yemdir. Karışmasın diye
# `_EXCLUDED_TOKENS` ile ikinci kelimeler elenir.
TOBB_PRODUCT_PREFIX = {
    "ARPA": "Arpa",
    "MISIR": "Mısır",
    "BUĞDAY": "Buğday",
    "YULAF": "Yulaf",
    "TRİTİKALE": "Tritikale",
    "NOHUT": "Nohut",
}

# Samsun TB ürün listesindeki ad -> veritabanı yem adı. Sayfa adresleri (slug) ürün
# kimliği taşıdığı için sabit kodlanmaz; /urunler/ sayfasındaki bağlantılardan bulunur.
SAMSUN_BASE_URL = "https://www.samsuntb.org.tr"
SAMSUN_PRODUCTS = {
    "BUĞDAY KEPEĞİ DÖKME": "Buğday Kepeği",
    "SAMAN (BUĞDAY)": "Buğday Samanı",
    "AYÇİÇEK KÜSPESİ": "Ayçiçeği Küspesi",
    "SOYA KÜSPESİ": "Soya Küspesi",
}

# Ürün adının devamında bunlardan biri geçiyorsa eşleme İPTAL edilir: dane değil,
# işlenmiş/kaba yem ürünüdür.
_EXCLUDED_TOKENS = (
    "KEPEK", "KEPEĞİ", "KÜSPE", "KÜSPESİ", "SİLAJ", "SİLAJI", "SAMAN", "SAMANI",
    "UNU", "NİŞASTA", "GLUTEN", "HASIL", "YAĞ", "TOHUM", "KALBURALTI",
)

# Çekilen fiyat taban fiyatın bu katsayı aralığı dışındaysa reddedilir.
# Gerekli: aynı portalda dana eti 587 TL/kg, zeytinyağı 220 TL/kg satırları var ve
# TOBB bazı satırları "12.007,000" gibi tutarsız biçimlendiriyor.
PRICE_SANITY_MIN_FACTOR = 0.3
PRICE_SANITY_MAX_FACTOR = 3.0

# Bu kadar günden eski işlem verisi kullanılmaz. Konya borsası Temmuz'da hâlâ
# Mayıs verisi gösteriyordu - filtre olmadan bayat fiyat "canlı" diye kaydedilirdi.
MAX_PRICE_AGE_DAYS = 30


def _aia_ca_bundle(host: str, timeout: int) -> str:
    """Sunucunun göndermediği ara sertifikayı AIA adresinden indirip CA paketine ekler.

    ⚠️ borsa.tobb.org.tr Ağustos 2026'daki sertifika yenilemesinden beri zinciri EKSİK
    gönderiyor (yalnızca kendi sertifikası, Sectigo ara sertifikası yok). Tarayıcılar ve
    Windows eksiği AIA ile kendisi tamamlar, Python tamamlamaz: her istek SSLError verdi ve
    canlı fiyat sessizce sıfıra düştü. Sertifika bir daha yenilendiğinde de kendiliğinden
    çalışsın diye ara sertifika sabit kodlanmaz, her seferinde AIA'dan bulunur.

    AIA adresi düz HTTP'dir; indirilen sertifikaya bu yüzden güvenilmez: (1) sunucu
    sertifikasını gerçekten imzalamış olmalı, (2) certifi'deki bir kök tarafından
    imzalanmış olmalı, (3) süresi geçerli olmalı. Biri tutmazsa ValueError.
    """
    leaf_pem = ssl.get_server_certificate((host, 443), timeout=timeout)  # doğrulamasız okuma
    leaf = x509.load_pem_x509_certificate(leaf_pem.encode())
    aia = leaf.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
    urls = [d.access_location.value for d in aia
            if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
    if not urls:
        raise ValueError(f"{host}: sertifikada AIA ara sertifika adresi yok")

    raw = requests.get(urls[0], timeout=timeout).content
    intermediate = (x509.load_pem_x509_certificate(raw) if raw.lstrip().startswith(b"-----")
                    else x509.load_der_x509_certificate(raw))
    leaf.verify_directly_issued_by(intermediate)

    roots_pem = Path(certifi.where()).read_bytes()
    with warnings.catch_warnings():
        # certifi'de seri numarası RFC 5280'e uymayan eski bir kök var; zararsız.
        warnings.simplefilter("ignore")
        roots = x509.load_pem_x509_certificates(roots_pem)

    def _issued_by(root) -> bool:
        try:
            intermediate.verify_directly_issued_by(root)
            return True
        except Exception:  # noqa: BLE001 - imza, ad veya anahtar tipi uyuşmazlığı
            return False

    if not any(_issued_by(root) for root in roots):
        raise ValueError(f"{host}: AIA ara sertifikası güvenilir bir köke bağlanmıyor")
    now = datetime.now(timezone.utc)
    if not intermediate.not_valid_before_utc <= now <= intermediate.not_valid_after_utc:
        raise ValueError(f"{host}: AIA ara sertifikasının süresi geçerli değil")

    bundle = Path(tempfile.gettempdir()) / f"rationai_ca_{host}.pem"
    bundle.write_bytes(roots_pem + b"\n" + intermediate.public_bytes(Encoding.PEM))
    logger.info("%s: eksik ara sertifika AIA'dan tamamlandı (%s)", host, intermediate.subject.rfc4514_string())
    return str(bundle)


@dataclass
class FeedPrice:
    """Bir yem için çekilmiş fiyat."""
    name: str
    price_per_kg: float          # TL/kg (doğal hali)
    source: str                  # "Bandırma TB (+3 borsa)"
    date: str                    # ISO tarih
    sample_count: int = 1


def _parse_turkish_number(text: str) -> List[float]:
    """Türkçe biçimli sayının olası okumalarını döndürür.

    TOBB tutarlı değil: aynı sütunda hem `13,570` (= 13.570) hem `12.007,000`
    (aslında 12.007) geçiyor. Tek bir doğru yorum olmadığı için olası okumaların
    hepsi döndürülür, hangisinin gerçek olduğuna akıl sağlığı bandı karar verir.
    """
    cleaned = text.strip().replace(" ", "").replace("\xa0", "")
    if not cleaned or not re.search(r"\d", cleaned):
        return []

    candidates = []
    # Standart tr-TR: nokta binlik, virgül ondalık
    try:
        candidates.append(float(cleaned.replace(".", "").replace(",", ".")))
    except ValueError:
        pass
    # Alternatif: nokta ondalık, sondaki ",000" biçim artığı
    alt = cleaned.split(",")[0]
    try:
        candidates.append(float(alt))
    except ValueError:
        pass

    return [c for c in candidates if c > 0]


def _tr_upper(text: str) -> str:
    """Türkçe büyük harf: str.upper() "Kepeği"yi "KEPEĞI" (noktasız) yapar, eşleşme kaçar."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def _parse_date(text: str) -> Optional[datetime]:
    """'28.07.2026 11:17' -> datetime."""
    match = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day)
    except ValueError:
        return None


def _match_product(product_name: str) -> Optional[str]:
    """TOBB ürün adını veritabanı yem adına eşler; dane olmayanları eler."""
    upper = product_name.upper().strip()
    if not upper:
        return None

    first_token = re.split(r"[\s(/,.]+", upper, maxsplit=1)[0]
    feed_name = TOBB_PRODUCT_PREFIX.get(first_token)
    if feed_name is None:
        return None

    remainder = upper[len(first_token):]
    if any(token in remainder for token in _EXCLUDED_TOKENS):
        return None

    return feed_name


class TurkishFeedPriceScraper:
    """TOBB borsa portalından tahıl fiyatlarını çeker."""

    def __init__(self, timeout: int = 15, base_prices: Optional[Dict[str, float]] = None):
        self.timeout = timeout
        self.base_prices = base_prices or {}
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "tr-TR,tr;q=0.9,en;q=0.8",
        })

    def _plausible_price(self, feed_name: str, candidates: List[float]) -> Optional[float]:
        """Olası okumalar içinden taban fiyata göre makul olanı seçer."""
        base = self.base_prices.get(feed_name)
        if base is None or base <= 0:
            # Taban yoksa yem fiyatı için genel bir bant uygula
            return next((c for c in candidates if 0.5 <= c <= 200.0), None)
        low, high = base * PRICE_SANITY_MIN_FACTOR, base * PRICE_SANITY_MAX_FACTOR
        return next((c for c in candidates if low <= c <= high), None)

    def _get(self, url: str, **kwargs) -> requests.Response:
        """GET; sertifika zinciri eksikse ara sertifikayı AIA'dan tamamlayıp bir kez daha dener."""
        try:
            return self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.exceptions.SSLError:
            if self.session.verify is not True:
                raise  # paket zaten tamamlanmış, sorun başka
            self.session.verify = _aia_ca_bundle(urlparse(url).hostname, self.timeout)
            return self.session.get(url, timeout=self.timeout, **kwargs)

    def scrape_exchange(self, code: str, label: str) -> List[FeedPrice]:
        """Tek bir borsanın fiyat tablosunu çeker."""
        prices: List[FeedPrice] = []
        cutoff = datetime.now() - timedelta(days=MAX_PRICE_AGE_DAYS)

        try:
            resp = self._get(TOBB_BASE_URL, params={"borsakod": code})
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            table = BeautifulSoup(resp.text, "html.parser").find("table")
            if table is None:
                return prices

            for row in table.find_all("tr")[1:]:
                cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
                if len(cells) < 6:
                    continue

                feed_name = _match_product(cells[0])
                if feed_name is None:
                    continue

                # Birim sütunu KG değilse tahmin yürütme, atla.
                if cells[1].strip().upper() not in ("KG", "KİLOGRAM"):
                    continue

                traded_at = _parse_date(cells[2])
                if traded_at is None or traded_at < cutoff:
                    continue

                price = self._plausible_price(feed_name, _parse_turkish_number(cells[5]))
                if price is None:
                    continue

                prices.append(FeedPrice(
                    name=feed_name,
                    price_per_kg=round(price, 2),
                    source=label,
                    date=traded_at.date().isoformat(),
                ))
        except Exception as exc:
            logger.warning("TOBB %s (%s) çekilemedi: %s", label, code, exc)

        return prices

    def scrape_samsun(self) -> List[FeedPrice]:
        """Samsun TB ürün sayfalarından kepek, saman ve küspe fiyatlarını çeker.

        Her ürün için tablodaki EN YENİ satır alınır; `MAX_PRICE_AGE_DAYS`'ten eskiyse
        ürün atlanır (soya küspesinde son işlem bir yıl önce olabiliyor). Satış şekli
        "Müstahsil" ise kaynağa yazılır: üreticinin tarlada sattığı fiyattır, nakliye ve
        balya dahil değildir.
        """
        prices: List[FeedPrice] = []
        cutoff = datetime.now() - timedelta(days=MAX_PRICE_AGE_DAYS)
        try:
            index = self._get(f"{SAMSUN_BASE_URL}/urunler/")
            index.raise_for_status()
            index.encoding = index.apparent_encoding or "utf-8"
            pages: Dict[str, str] = {}
            for link in BeautifulSoup(index.text, "html.parser").find_all("a", href=True):
                name = _tr_upper(re.sub(r"\s+", " ", link.get_text(" ", strip=True)))
                name = name.replace("( ", "(").replace(" )", ")")
                # Bağlantı metni adın ardından kod ve fiyat da taşır:
                # "Buğday Kepeği Dökme BUGKEPDOK (SATIM) 13,64 En Az: ..."
                feed_name = next((feed for key, feed in SAMSUN_PRODUCTS.items()
                                  if name.startswith(key)), None)
                if feed_name and "/piyasa-verileri/" in link["href"]:
                    pages.setdefault(feed_name, link["href"])
        except Exception as exc:
            logger.warning("Samsun TB ürün listesi çekilemedi: %s", exc)
            return prices

        for feed_name, url in pages.items():
            try:
                resp = self._get(url if url.startswith("http") else SAMSUN_BASE_URL + url)
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                table = BeautifulSoup(resp.text, "html.parser").find("table")
                rows = [[c.get_text(" ", strip=True) for c in tr.find_all("td")]
                        for tr in (table.find_all("tr") if table else [])]
                dated = [(d, r) for r in rows if len(r) >= 4 and (d := _parse_date(r[0]))]
                if not dated:
                    continue
                traded_at, row = max(dated, key=lambda item: item[0])
                if traded_at < cutoff:
                    continue
                price = self._plausible_price(feed_name, _parse_turkish_number(row[3]))
                if price is None:
                    continue
                producer = len(row) >= 6 and row[5].strip().lower().startswith("müstahsil")
                prices.append(FeedPrice(
                    name=feed_name,
                    price_per_kg=round(price, 2),
                    source="Samsun TB" + (" (müstahsil)" if producer else ""),
                    date=traded_at.date().isoformat(),
                ))
            except Exception as exc:
                logger.warning("Samsun TB %s çekilemedi: %s", feed_name, exc)
        return prices

    def scrape_all(self) -> Dict[str, FeedPrice]:
        """Tüm kaynakları gezip yem başına medyan fiyatı döndürür."""
        collected: Dict[str, List[FeedPrice]] = {}

        for code, label in TOBB_EXCHANGES.items():
            for price in self.scrape_exchange(code, label):
                collected.setdefault(price.name, []).append(price)
        for price in self.scrape_samsun():
            collected.setdefault(price.name, []).append(price)

        results: Dict[str, FeedPrice] = {}
        for feed_name, samples in collected.items():
            median_price = statistics.median(p.price_per_kg for p in samples)
            newest = max(samples, key=lambda p: p.date)
            sources = {p.source for p in samples}
            label = newest.source
            if len(sources) > 1:
                label = f"{newest.source} (+{len(sources) - 1} borsa)"

            results[feed_name] = FeedPrice(
                name=feed_name,
                price_per_kg=round(median_price, 2),
                source=label,
                date=newest.date,
                sample_count=len(samples),
            )

        logger.info("Borsalar: %d yem için canlı fiyat bulundu", len(results))
        return results


# =====================================================================
# PUBLIC API
# =====================================================================

def _curated_base_prices() -> Dict[str, float]:
    """Taban fiyatlar tek kaynaktan: küratörlü yem tablosu."""
    from .database import _get_curated_feeds
    return {feed["name"]: feed["price_per_kg"] for feed in _get_curated_feeds()}


def update_feed_prices(
    db: Session,
    price_updates: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Yem fiyatlarını canlı borsadan (veya verilen sözlükten) günceller.

    Args:
        db: Veritabanı oturumu
        price_updates: Verilirse canlı çekim yapılmaz, bu fiyatlar elle uygulanır.

    Returns:
        {"updated": [Feed], "live_count": int, "baseline_count": int, "source": str}
        Canlı kaynak bulunamazsa fiyatlara DOKUNULMAZ ve live_count 0 döner —
        eskiden burada sessizce rastgele bir simülasyon çalışıyordu.
    """
    from .database import MANUAL_SOURCE

    now = datetime.now()
    updated: List[Feed] = []
    kept_manual: List[str] = []

    if price_updates is not None:
        source_label = MANUAL_SOURCE
        resolved = {name: (price, source_label, now) for name, price in price_updates.items()}
    else:
        scraper = TurkishFeedPriceScraper(base_prices=_curated_base_prices())
        scraped = scraper.scrape_all()
        source_label = "TOBB + Samsun TB"
        resolved = {
            name: (fp.price_per_kg, f"{fp.source} · {fp.date}", now)
            for name, fp in scraped.items()
        }

    for feed_name, (new_price, source, stamp) in resolved.items():
        feed = db.query(Feed).filter(Feed.name == feed_name).first()
        if feed is None:
            logger.warning("Veritabanında bulunmayan yem atlandı: %s", feed_name)
            continue
        # Çiftçinin kaydettiği alış fiyatı piyasa ortalamasından daha doğrudur: canlı
        # senkron onu ezmez. Piyasaya dönmek için `reset_manual_prices()`.
        if price_updates is None and feed.price_source == MANUAL_SOURCE:
            kept_manual.append(feed_name)
            continue
        logger.debug("%s: %.2f -> %.2f (%s)", feed_name, feed.price_per_kg or 0, new_price, source)
        feed.price_per_kg = new_price
        feed.price_source = source
        feed.price_updated_at = stamp
        updated.append(feed)

    if updated:
        db.commit()

    total_feeds = db.query(Feed).count()
    return {
        "updated": updated,
        "live_count": len(updated),
        "baseline_count": max(total_feeds - len(updated) - len(kept_manual), 0),
        "source": source_label,
        "kept_manual": kept_manual,
    }


def reset_manual_prices(db: Session) -> List[str]:
    """Elle girilen fiyatları küratörlü taban fiyata döndürür (sonraki senkron günceller)."""
    from .database import BASELINE_PRICE_DATE, BASELINE_SOURCE, MANUAL_SOURCE

    base = _curated_base_prices()
    reset = []
    for feed in db.query(Feed).filter(Feed.price_source == MANUAL_SOURCE).all():
        if feed.name in base:
            feed.price_per_kg = base[feed.name]
            feed.price_source = BASELINE_SOURCE
            feed.price_updated_at = BASELINE_PRICE_DATE
            reset.append(feed.name)
    if reset:
        db.commit()
    return reset


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for name, fp in sorted(TurkishFeedPriceScraper(base_prices=_curated_base_prices()).scrape_all().items()):
        print(f"{name:<12} {fp.price_per_kg:>7.2f} TL/kg  {fp.source} · {fp.date} "
              f"({fp.sample_count} kayıt)")
