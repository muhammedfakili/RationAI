"""RationAI - canlı yem fiyatı: orta-büyük bir süt işletmesinin ortalama TESLİM ALIŞ fiyatı.

Bakış açısı: fiyat, işletmenin yemi çiftliğe teslim alırken ödediği ortalama tutardır —
tek bir satıcının fiyatı değil. Bu yüzden:
    teslim fiyatı = piyasa ortalaması (kaynakların medyanı) + teslim farkı (nakliye/yükleme)

Üç kaynak:
1. TOBB birleşik portalı — tahıl danesi (13 borsa).
2. Samsun Ticaret Borsası ürün sayfaları — TOBB'da OLMAYAN kepek ve küspe.
3. hayvanci.com piyasa fiyatları — saman, yonca, ayçiçeği küspesi ve tahıllar.

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

Besi ekonomisi yalnız KESİM (karkas) fiyatı kullanır: besici eti kesimhaneye satar, hayvan da
(besilik dahil) kesim fiyatından değerlenir — canlı hayvan fiyatı kullanılmaz (kullanıcı).
Yedek kaynak TOBB (`scrape_livestock`): bazı borsalar karkas ("DANA ETİ KARKAS", "KUZU ETİ
KARKAS") işliyor — Ekim 2026'da Ankara, Gaziantep, Nazilli. Borsa listesi her seferinde
`fiyat_borsa0.php`'den okunur.

Karkas (kesim) fiyatının asıl kaynağı UKON'dur (`scrape_ukon`): Ulusal Kırmızı Et Konseyi her
hafta kombina ve kesimhanelerden yağsız dana ve kuzu kesim fiyatlarını 7 bölge + Türkiye
ortalaması olarak yayımlar. Türkiye'de hayvan bu kesim fiyatı üzerinden değerlenir (alış da);
TOBB'daki karkas satırları yalnız UKON okunamazsa kullanılır.

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

from .models import Feed, MarketPrice

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
    "ÇAVDAR": "Çavdar",
    "SORGUM": "Sorgum",   # Ekim 2026'da borsada güncel işlem yoktu; çıkarsa yakalanır
}

# Karkas (UKON yedeği): TOBB ürün adı (TAM eşleşme) -> MarketPrice anahtarı. Tam eşleşme
# kasıtlı: "DANA ETİ BUT" ve "KUZU ETİ PİRZOLA" parça et.
TOBB_INDEX_URL = "https://borsa.tobb.org.tr/fiyat_borsa0.php"
LIVESTOCK_PRODUCTS = {
    "DANA ETİ KARKAS": "dana_karkas",
    "KUZU ETİ KARKAS": "kuzu_karkas",
}
# UKON haftalık kesim fiyatları: sütun -> karkas anahtarı. Bölge satırları "<anahtar>@<bölge>"
# olarak saklanır; "Ortalama" satırı anahtarın kendisidir (Türkiye ortalaması).
UKON_URL = "https://www.ukon.org.tr/fiyatlar"
UKON_COLUMNS = {"DANA": "dana_karkas", "KUZU": "kuzu_karkas"}
# UKON haftalık yayımlar; bundan eski tablo kullanılmaz.
UKON_MAX_AGE_DAYS = 21

# İndeks okunamazsa: Ekim 2026'da karkas işleyen borsalar.
LIVESTOCK_EXCHANGES = {"5AN10": "Ankara TB", "5GA10": "Gaziantep TB", "5NA10": "Nazilli TB"}

# Samsun TB ürün listesindeki ad -> veritabanı yem adı. Sayfa adresleri (slug) ürün
# kimliği taşıdığı için sabit kodlanmaz; /urunler/ sayfasındaki bağlantılardan bulunur.
SAMSUN_BASE_URL = "https://www.samsuntb.org.tr"
# hayvanci.com/fiyatlar/<slug>: tarihli "Tarih | Fiyat | Değişim" tablosu (robots.txt
# /fiyatlar'a izin veriyor). Genel "saman" iki saman yemine de uygulanır.
# ⚠️ Oradaki "soya" soya FASULYESİDİR (28 TL/kg, Adana üretici bildirimleri) — küspe değil,
# bu yüzden eşlenmez.
HAYVANCI_BASE_URL = "https://hayvanci.com/fiyatlar"
HAYVANCI_PRODUCTS = {
    "saman": ("Buğday Samanı", "Arpa Samanı"),
    "yonca": ("Yonca Kuru Otu",),
    # Derecesiz "ayçiçeği küspesi" fiyatı sağmalların kullandığı %36'lık ürüne yazılır.
    "aycicegi-kuspesi": ("Ayçiçeği Küspesi 36",),
    "arpa": ("Arpa",),
    "bugday": ("Buğday",),
    "misir": ("Mısır",),
    "nohut": ("Nohut",),
}

# Piyasa fiyatının üstüne eklenen TESLİM FARKI (TL/kg): yükleme + çiftliğe nakliye.
# Kaynak: 2026 piyasa derlemeleri — şehirlerarası tır nakliyesi kaba yemde ~1.200-1.800
# TL/ton, kısa mesafede balya başına 15-25 TL. Saman/kuru ot hacimli olduğu için bir tıra
# tahılın yaklaşık yarısı kadar yüklenir; ton başına nakliyesi dökme konsantrenin ~2 katı.
DELIVERY_PREMIUM_TL_KG = {
    "straw": 1.20, "hay": 1.20,
    "grain": 0.50, "protein": 0.50, "byproduct": 0.50,
}

SAMSUN_PRODUCTS = {
    "BUĞDAY KEPEĞİ DÖKME": "Buğday Kepeği",
    "SAMAN (BUĞDAY)": "Buğday Samanı",
    "AYÇİÇEK KÜSPESİ": "Ayçiçeği Küspesi 36",
    "SOYA KÜSPESİ": "Soya Küspesi 46",
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

    def _exchange_rows(self, code: str) -> List[List[str]]:
        """Bir TOBB borsasının fiyat tablosu satırları (hücre metinleri)."""
        resp = self._get(TOBB_BASE_URL, params={"borsakod": code})
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        table = BeautifulSoup(resp.text, "html.parser").find("table")
        if table is None:
            return []
        return [[c.get_text(" ", strip=True) for c in row.find_all("td")]
                for row in table.find_all("tr")[1:]]

    def exchange_codes(self) -> Dict[str, str]:
        """Portaldaki borsalar (kod -> ad); okunamazsa hayvan işleyen bilinen borsalar."""
        try:
            resp = self._get(TOBB_INDEX_URL)
            resp.raise_for_status()
            resp.encoding = resp.apparent_encoding or "utf-8"
            codes = {}
            for link in BeautifulSoup(resp.text, "html.parser").find_all("a", href=True):
                match = re.search(r"borsakod=([^&\s]+)", link["href"])
                if match:
                    # "EDIRNE TICARET BORSASI" -> "Edirne TB"
                    city = re.split(r"\s+T[Iİ]CARET\b", link.get_text(" ", strip=True))[0]
                    codes[match.group(1)] = f"{city.capitalize()} TB"
            if codes:
                return codes
        except Exception as exc:
            logger.warning("TOBB borsa listesi çekilemedi: %s", exc)
        return dict(LIVESTOCK_EXCHANGES)

    def scrape_exchange(self, code: str, label: str) -> List[FeedPrice]:
        """Tek bir borsanın fiyat tablosunu çeker."""
        prices: List[FeedPrice] = []
        cutoff = datetime.now() - timedelta(days=MAX_PRICE_AGE_DAYS)

        try:
            for cells in self._exchange_rows(code):
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

    def scrape_livestock(self) -> Dict[str, FeedPrice]:
        """TOBB karkas fiyatları (UKON yedeği): borsaların ORTALAMA sütununun medyanı (TL/kg).

        `base_prices` burada MarketPrice anahtarına göre taban fiyattır; akıl sağlığı bandı
        aynı (0.3×–3×). Teslim farkı eklenmez — fiyat borsadaki işlem fiyatıdır.
        """
        cutoff = datetime.now() - timedelta(days=MAX_PRICE_AGE_DAYS)
        collected: Dict[str, List[FeedPrice]] = {}
        for code, label in self.exchange_codes().items():
            try:
                rows = self._exchange_rows(code)
            except Exception as exc:
                logger.warning("TOBB %s (%s) çekilemedi: %s", label, code, exc)
                continue
            for cells in rows:
                if len(cells) < 6 or cells[1].strip().upper() not in ("KG", "KİLOGRAM"):
                    continue
                key = LIVESTOCK_PRODUCTS.get(re.sub(r"\s+", " ", _tr_upper(cells[0])).strip())
                traded_at = _parse_date(cells[2])
                if key is None or traded_at is None or traded_at < cutoff:
                    continue
                price = self._plausible_price(key, _parse_turkish_number(cells[5]))
                if price is not None:
                    collected.setdefault(key, []).append(FeedPrice(
                        name=key, price_per_kg=round(price, 2), source=label,
                        date=traded_at.date().isoformat()))

        results: Dict[str, FeedPrice] = {}
        for key, samples in collected.items():
            newest = max(samples, key=lambda p: p.date)
            sources = sorted({p.source for p in samples}, key=lambda src: src != newest.source)
            results[key] = FeedPrice(
                name=key,
                price_per_kg=round(statistics.median(p.price_per_kg for p in samples), 2),
                source=sources[0] + (f" +{len(sources) - 1} borsa" if len(sources) > 1 else ""),
                date=newest.date,
                sample_count=len(samples),
            )
        logger.info("Canlı kaynaklar: %d hayvan/karkas fiyatı bulundu", len(results))
        return results

    def scrape_ukon(self) -> Dict[str, FeedPrice]:
        """UKON haftalık kesim fiyatları: Türkiye ortalaması ve bölgeler (TL/kg karkas).

        Tablo: Bölge | Dana Bıçak TL/KG | Kuzu Bıçak TL/KG; "Ortalama" satırı Türkiye
        ortalaması, "Değişim (%)" satırları atlanır. Tarih başlıkta: "(TL/KG) - 22.10.2026".
        """
        results: Dict[str, FeedPrice] = {}
        try:
            resp = self._get(UKON_URL)
            resp.raise_for_status()
            # Sayfa utf-8 diye bildiriyor ama Türkçe karakterler bozuk geliyor.
            resp.encoding = resp.apparent_encoding or "utf-8"
            soup = BeautifulSoup(resp.text, "html.parser")
            match = re.search(r"\(TL\s*/\s*KG\)\s*-\s*(\d{2}\.\d{2}\.\d{4})", soup.get_text(" "))
            stamp = _parse_date(match.group(1)) if match else None
            table = soup.find("table")
            if stamp is None or table is None:
                logger.warning("UKON: fiyat tablosu ya da tarihi bulunamadı")
                return results
            if stamp < datetime.now() - timedelta(days=UKON_MAX_AGE_DAYS):
                logger.warning("UKON tablosu eski (%s), kullanılmadı", stamp.date())
                return results
            rows = [[c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
                    for tr in table.find_all("tr")]
            header = [_tr_upper(cell) for cell in rows[0]]
            columns = {i: key for i, cell in enumerate(header)
                       for word, key in UKON_COLUMNS.items() if cell.startswith(word)}
            for cells in rows[1:]:
                region = cells[0].strip()
                if not region or "DEĞİŞİM" in _tr_upper(region):
                    continue
                for i, key in columns.items():
                    if i >= len(cells):
                        continue
                    full_key = key if _tr_upper(region).startswith("ORTALAMA") else f"{key}@{region}"
                    price = self._plausible_price(key, _parse_turkish_number(cells[i]))
                    if price is not None:
                        results[full_key] = FeedPrice(
                            name=full_key, price_per_kg=round(price, 2),
                            source="UKON" if full_key == key else f"UKON · {region}",
                            date=stamp.date().isoformat())
        except Exception as exc:
            logger.warning("UKON fiyatları çekilemedi: %s", exc)
        logger.info("UKON: %d kesim fiyatı bulundu", len(results))
        return results

    def scrape_samsun(self) -> List[FeedPrice]:
        """Samsun TB ürün sayfalarından kepek, saman ve küspe fiyatlarını çeker.

        Her ürün için tablodaki EN YENİ "Satım" satırı alınır; `MAX_PRICE_AGE_DAYS`'ten
        eskiyse ürün atlanır (soya küspesinde son işlem bir yıl önce olabiliyor).
        "Müstahsil" satırları atlanır: üreticinin tarlada (çoğu zaman balyasız) sattığı
        fiyattır, işletmenin tüccardan alış fiyatını yansıtmaz — samanda 2.21 TL/kg'a
        karşı piyasa 3.50 TL/kg.
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
                dated = [(d, r) for r in rows if len(r) >= 4 and (d := _parse_date(r[0]))
                         and not (len(r) >= 6 and r[5].strip().lower().startswith("müstahsil"))]
                if not dated:
                    continue
                traded_at, row = max(dated, key=lambda item: item[0])
                if traded_at < cutoff:
                    continue
                price = self._plausible_price(feed_name, _parse_turkish_number(row[3]))
                if price is None:
                    continue
                prices.append(FeedPrice(
                    name=feed_name,
                    price_per_kg=round(price, 2),
                    source="Samsun TB",
                    date=traded_at.date().isoformat(),
                ))
            except Exception as exc:
                logger.warning("Samsun TB %s çekilemedi: %s", feed_name, exc)
        return prices

    def scrape_hayvanci(self) -> List[FeedPrice]:
        """hayvanci.com piyasa fiyatı sayfalarından en yeni tarihli fiyatı çeker."""
        prices: List[FeedPrice] = []
        cutoff = datetime.now() - timedelta(days=MAX_PRICE_AGE_DAYS)
        for slug, feed_names in HAYVANCI_PRODUCTS.items():
            try:
                resp = self._get(f"{HAYVANCI_BASE_URL}/{slug}")
                resp.raise_for_status()
                resp.encoding = resp.apparent_encoding or "utf-8"
                table = BeautifulSoup(resp.text, "html.parser").find("table")
                rows = [[c.get_text(" ", strip=True) for c in tr.find_all("td")]
                        for tr in (table.find_all("tr") if table else [])]
                dated = [(d, r) for r in rows if len(r) >= 2 and (d := _parse_date(r[0]))]
                if not dated:
                    continue
                traded_at, row = max(dated, key=lambda item: item[0])
                # "3,50 TL/kg" — birim TL/kg değilse tahmin yürütme.
                if traded_at < cutoff or "TL/KG" not in _tr_upper(row[1]).replace(" ", ""):
                    continue
                value = _tr_upper(row[1]).replace("TL/KG", "")
                for feed_name in feed_names:
                    price = self._plausible_price(feed_name, _parse_turkish_number(value))
                    if price is not None:
                        prices.append(FeedPrice(
                            name=feed_name, price_per_kg=round(price, 2),
                            source="hayvanci.com", date=traded_at.date().isoformat(),
                        ))
            except Exception as exc:
                logger.warning("hayvanci.com %s çekilemedi: %s", slug, exc)
        return prices

    def scrape_all(self, feed_types: Optional[Dict[str, str]] = None) -> Dict[str, FeedPrice]:
        """Tüm kaynakları gezip yem başına İŞLETME TESLİM fiyatını döndürür.

        Piyasa fiyatı = kaynak örneklerinin medyanı; üstüne yem tipine göre
        `DELIVERY_PREMIUM_TL_KG` eklenir. Kaynak etiketi hesabı açıkça taşır:
        "piyasa 3.50 + teslim 1.20 · hayvanci.com".
        """
        feed_types = feed_types or {}
        collected: Dict[str, List[FeedPrice]] = {}

        for code, label in TOBB_EXCHANGES.items():
            for price in self.scrape_exchange(code, label):
                collected.setdefault(price.name, []).append(price)
        for price in self.scrape_samsun() + self.scrape_hayvanci():
            collected.setdefault(price.name, []).append(price)

        results: Dict[str, FeedPrice] = {}
        for feed_name, samples in collected.items():
            market = statistics.median(p.price_per_kg for p in samples)
            premium = DELIVERY_PREMIUM_TL_KG.get(feed_types.get(feed_name, ""), 0.0)
            newest = max(samples, key=lambda p: p.date)
            sources = sorted({p.source for p in samples}, key=lambda src: src != newest.source)
            label = sources[0] + (f" +{len(sources) - 1} kaynak" if len(sources) > 1 else "")
            if premium:
                label = f"piyasa {market:.2f} + teslim {premium:.2f} · {label}"

            results[feed_name] = FeedPrice(
                name=feed_name,
                price_per_kg=round(market + premium, 2),
                source=label,
                date=newest.date,
                sample_count=len(samples),
            )

        logger.info("Canlı kaynaklar: %d yem için teslim fiyatı bulundu", len(results))
        return results


# =====================================================================
# PUBLIC API
# =====================================================================

def _curated_base_prices() -> Dict[str, float]:
    """Taban fiyatlar tek kaynaktan: küratörlü yem tablosu."""
    from .database import _get_curated_feeds
    return {feed["name"]: feed["price_per_kg"] for feed in _get_curated_feeds()}


def _curated_feed_types() -> Dict[str, str]:
    from .database import _get_curated_feeds
    return {feed["name"]: feed["feed_type"] for feed in _get_curated_feeds()}


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
        scraped = scraper.scrape_all(_curated_feed_types())
        source_label = "TOBB + Samsun TB + hayvanci.com"
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


def update_market_prices(
    db: Session,
    price_updates: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    """Kesim (karkas) fiyatlarını (veya verilen sözlüğü) yazar.

    UKON'dan (bölgeler dahil); UKON'un vermediği anahtar için TOBB karkas satırı.
    Yem fiyatlarıyla aynı kurallar: canlı senkron elle girilen fiyatı ezmez, kaynak
    bulunamazsa fiyatlara dokunulmaz. Bölge satırları ilk senkronda oluşturulur.
    """
    from .database import MANUAL_SOURCE

    now = datetime.now()
    rows = {row.key: row for row in db.query(MarketPrice).all()}
    if price_updates is not None:
        resolved = {key: (price, MANUAL_SOURCE) for key, price in price_updates.items()}
    else:
        scraper = TurkishFeedPriceScraper(
            base_prices={key: row.price_per_kg for key, row in rows.items()})
        scraped = {**scraper.scrape_livestock(), **scraper.scrape_ukon()}
        resolved = {key: (fp.price_per_kg, f"{fp.source} · {fp.date}")
                    for key, fp in scraped.items()}
        for key in resolved:
            if key not in rows and "@" in key:
                base, region = key.split("@", 1)
                if base in rows:
                    rows[key] = MarketPrice(key=key, label=f"{rows[base].label} — {region}")
                    db.add(rows[key])

    updated, kept_manual = [], []
    for key, (price, source) in resolved.items():
        row = rows.get(key)
        if row is None:
            continue
        if price_updates is None and row.price_source == MANUAL_SOURCE:
            kept_manual.append(row.label)
            continue
        row.price_per_kg, row.price_source, row.price_updated_at = price, source, now
        updated.append(row.label)
    if updated:
        db.commit()
    return {"updated": updated, "kept_manual": kept_manual}


def reset_market_prices(db: Session) -> List[str]:
    """Elle girilen kesim fiyatlarını taban fiyata döndürür."""
    from .database import BASELINE_SOURCE, MANUAL_SOURCE, seed_market_prices

    reset = []
    for row in db.query(MarketPrice).filter(MarketPrice.price_source == MANUAL_SOURCE).all():
        row.price_source = BASELINE_SOURCE
        reset.append(row.label)
    if reset:
        db.commit()
        seed_market_prices(db)   # taban fiyatı ve tarihi geri yazar
    return reset


def last_live_sync(db: Session) -> Optional[datetime]:
    """En son canlı senkronun zamanı (taban ve elle girilen fiyatlar sayılmaz)."""
    from .database import BASELINE_SOURCE, MANUAL_SOURCE

    stamps = [row.price_updated_at
              for model in (Feed, MarketPrice)
              for row in db.query(model.price_updated_at).filter(
                  model.price_source.notin_((BASELINE_SOURCE, MANUAL_SOURCE)),
                  model.price_updated_at.isnot(None)).all()]
    return max(stamps) if stamps else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    scraper = TurkishFeedPriceScraper(base_prices=_curated_base_prices())
    for name, fp in sorted(scraper.scrape_all(_curated_feed_types()).items()):
        print(f"{name:<12} {fp.price_per_kg:>7.2f} TL/kg  {fp.source} · {fp.date} "
              f"({fp.sample_count} kayıt)")
