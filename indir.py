#!/usr/bin/env python3
"""
Yargıtay kararlarını (varsayılan: 9. Hukuk Dairesi, 2020 sonrası) Adalet Bakanlığı
Bedesten servisinden (mevzuat.adalet.gov.tr'nin kullandığı kaynak) indirip
Google Drive'a her karar ayrı bir Google Docs belgesi olacak şekilde kaydeder.

- Kaldığı yerden devam eder: indirilen kararlar durum/indirilenler.tsv'ye yazılır.
- Kararlar yıl alt klasörlerine (2020, 2021, ...) konur.
- Sunucuya nazik davranır: istekler arasında bekler, 429 gelirse yavaşlar.

Ortam değişkenleri (zorunlu):
  GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, DRIVE_KLASOR_ID
İsteğe bağlı:
  DAIRE            (varsayılan "9. Hukuk Dairesi")
  BASLANGIC        (varsayılan "2020-01-01")
  BITIS            (varsayılan bugün)
  ARAMA_IFADESI    (varsayılan "karar" — tüm kararları kapsayan genel ifade)
  SURE_LIMITI_DK   (0 = sınırsız; GitHub Actions için 330)
  ISTEK_ARALIGI_SN (Bedesten istekleri arası başlangıç bekleme, varsayılan 2.0)
  DURUM_KLASORU    (varsayılan "durum")
  SIRALAMA         ("yeni" = en yeni kararlardan başla [varsayılan], "eski" = 2020'den başla)
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

TR = ZoneInfo("Europe/Istanbul")

# Windows konsolunda Türkçe karakter/ok işareti hata vermesin
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:  # noqa: BLE001
    pass

BEDESTEN = "https://bedesten.adalet.gov.tr"
ARAMA_YOLU = "/emsal-karar/searchDocuments"
BELGE_YOLU = "/emsal-karar/getDocumentContent"
BASLIKLAR = {
    "Accept": "*/*",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "AdaletApplicationName": "UyapMevzuat",
    "Content-Type": "application/json; charset=utf-8",
    "Origin": "https://mevzuat.adalet.gov.tr",
    "Referer": "https://mevzuat.adalet.gov.tr/",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"
    ),
}
SAYFA_BOYU = 10            # Bedesten sayfa başına en fazla 10 sonuç veriyor
PENCERE_UST_SINIRI = 1000  # bir tarih aralığında bundan fazla sonuç varsa aralık bölünür
SON_GUNLER_ACIK = 45       # son 45 günlük pencereler "bitti" sayılmaz (yeni kararlar eklenebilir)

DAIRE_KISA = {"9. Hukuk Dairesi": "9. HD"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ----------------------------------------------------------------------------
# Bedesten istemcisi (tek hat, hız sınırlı, 429'da geri çekilir)
# ----------------------------------------------------------------------------
class Bedesten:
    def __init__(self, aralik: float):
        self.s = requests.Session()
        self.s.headers.update(BASLIKLAR)
        self.aralik = aralik
        self.asgari = max(1.0, aralik * 0.75)
        self.son = 0.0
        self.basari_serisi = 0
        self.kilit = threading.Lock()

    def _bekle(self):
        with self.kilit:
            fark = time.monotonic() - self.son
            if fark < self.aralik:
                time.sleep(self.aralik - fark)
            self.son = time.monotonic()

    def _post(self, yol: str, govde: dict) -> dict:
        for deneme in range(8):
            self._bekle()
            try:
                r = self.s.post(BEDESTEN + yol, json=govde, timeout=60)
            except requests.RequestException as e:
                log(f"  ağ hatası ({e.__class__.__name__}), tekrar deneniyor")
                time.sleep(min(60, 5 * 2 ** deneme))
                continue
            if r.status_code == 429:
                try:
                    ra = float(r.headers.get("Retry-After", "30"))
                except ValueError:
                    ra = 30.0
                self.aralik = min(15.0, self.aralik * 1.5)
                self.basari_serisi = 0
                log(f"  429: {ra:.0f} sn bekleniyor, istek aralığı {self.aralik:.1f} sn")
                time.sleep(max(1.0, min(ra, 120.0)))
                continue
            if r.status_code >= 500:
                log(f"  sunucu hatası {r.status_code}, tekrar deneniyor")
                time.sleep(min(120, 5 * 2 ** deneme))
                continue
            r.raise_for_status()
            self.basari_serisi += 1
            if self.basari_serisi >= 200 and self.aralik > self.asgari:
                self.aralik = max(self.asgari, self.aralik * 0.9)
                self.basari_serisi = 0
            return r.json()
        raise RuntimeError(f"Bedesten {yol} 8 denemede yanıt vermedi")

    def ara(self, ifade, daire, bas: dt.date, bit: dt.date, sayfa: int, yon: str = "desc") -> tuple[int, list]:
        govde = {
            "data": {
                "pageSize": SAYFA_BOYU,
                "pageNumber": sayfa,
                "itemTypeList": ["YARGITAYKARARI"],
                "phrase": ifade,
                "birimAdi": daire,
                "kararTarihiStart": f"{bas.isoformat()}T00:00:00.000Z",
                "kararTarihiEnd": f"{bit.isoformat()}T23:59:59.999Z",
                "sortFields": ["KARAR_TARIHI"],
                "sortDirection": yon,
            },
            "applicationName": "UyapMevzuat",
            "paging": True,
        }
        j = self._post(ARAMA_YOLU, govde)
        d = j.get("data") or {}
        return int(d.get("total") or 0), d.get("emsalKararList") or []

    def belge(self, belge_id: str) -> tuple[bytes, str]:
        j = self._post(BELGE_YOLU, {"data": {"documentId": belge_id}, "applicationName": "UyapMevzuat"})
        d = j.get("data") or {}
        if not d.get("content"):
            raise ValueError("belge içeriği boş")
        return base64.b64decode(d["content"]), d.get("mimeType") or "text/html"


# ----------------------------------------------------------------------------
# Künye
# ----------------------------------------------------------------------------
def karar_tarihi(k: dict) -> dt.date | None:
    s = (k.get("kararTarihiStr") or "").strip()
    m = re.fullmatch(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return dt.date(int(m[3]), int(m[2]), int(m[1]))
    s = (k.get("kararTarihi") or "").strip()
    if s:
        try:
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo:
                t = t.astimezone(TR)
            return t.date()
        except ValueError:
            m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
            if m:
                return dt.date(int(m[1]), int(m[2]), int(m[3]))
    return None


def no(k: dict, tur: str) -> str:
    v = k.get(f"{tur}No")
    if v:
        return str(v)
    y, s = k.get(f"{tur}NoYil"), k.get(f"{tur}NoSira")
    return f"{y}/{s}" if y and s else "?"


def kunye(k: dict, daire: str) -> tuple[str, dt.date | None]:
    t = karar_tarihi(k)
    kisa = DAIRE_KISA.get(daire, daire)
    ts = t.strftime("%d.%m.%Y") if t else "?"
    return f"Yargıtay {kisa}, E. {no(k, 'esas')}, K. {no(k, 'karar')}, T. {ts}", t


# ----------------------------------------------------------------------------
# Google Drive
# ----------------------------------------------------------------------------
class Drive:
    KLASOR = "application/vnd.google-apps.folder"

    def __init__(self, kok_id: str):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        cred = Credentials(
            token=None,
            refresh_token=os.environ["GOOGLE_REFRESH_TOKEN"],
            client_id=os.environ["GOOGLE_CLIENT_ID"],
            client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        self.cred = cred
        self.build = build
        self.yerel = threading.local()
        self.kok = kok_id
        self.klasorler: dict[str, str] = {}
        self.kilit = threading.Lock()

    @property
    def svc(self):
        # googleapiclient iş parçacığı güvenli değil: her iş parçacığına ayrı servis
        if not hasattr(self.yerel, "svc"):
            self.yerel.svc = self.build("drive", "v3", credentials=self.cred, cache_discovery=False)
        return self.yerel.svc

    def _tekrarla(self, f):
        from googleapiclient.errors import HttpError

        for deneme in range(8):
            try:
                return f()
            except HttpError as e:
                kod = e.resp.status
                if kod in (403, 429, 500, 502, 503, 504) and deneme < 7:
                    time.sleep(min(64, 2 ** deneme) + 0.5)
                    continue
                raise
            except (OSError, TimeoutError):
                if deneme < 7:
                    time.sleep(min(64, 2 ** deneme) + 0.5)
                    continue
                raise

    def yil_klasoru(self, yil: str) -> str:
        with self.kilit:
            if yil in self.klasorler:
                return self.klasorler[yil]
            q = (f"name = '{yil}' and '{self.kok}' in parents and "
                 f"mimeType = '{self.KLASOR}' and trashed = false")
            r = self._tekrarla(lambda: self.svc.files().list(q=q, fields="files(id)", pageSize=1).execute())
            if r.get("files"):
                fid = r["files"][0]["id"]
            else:
                fid = self._tekrarla(lambda: self.svc.files().create(
                    body={"name": yil, "mimeType": self.KLASOR, "parents": [self.kok]}, fields="id").execute())["id"]
                log(f"  Drive'da '{yil}' klasörü oluşturuldu")
            self.klasorler[yil] = fid
            return fid

    def yukle(self, ad: str, icerik: bytes, mime: str, yil: str, belge_id: str, kaynak: str) -> str:
        from googleapiclient.http import MediaInMemoryUpload

        ebeveyn = self.yil_klasoru(yil)
        govde = {
            "name": ad,
            "parents": [ebeveyn],
            "description": kaynak,
            "appProperties": {"bedestenId": belge_id},
        }
        if mime == "text/html":
            govde["mimeType"] = "application/vnd.google-apps.document"  # Google Docs'a dönüştür
        media = MediaInMemoryUpload(icerik, mimetype=mime, resumable=False)
        r = self._tekrarla(lambda: self.svc.files().create(body=govde, media_body=media, fields="id").execute())
        return r["id"]

    def mevcutlar(self) -> dict[str, str]:
        """Durum dosyası kaybolursa: kök altındaki tüm yıl klasörlerinden bedestenId -> dosya id."""
        sonuc: dict[str, str] = {}
        q = f"'{self.kok}' in parents and mimeType = '{self.KLASOR}' and trashed = false"
        klasorler = self._tekrarla(lambda: self.svc.files().list(q=q, fields="files(id,name)", pageSize=100).execute())
        for kl in klasorler.get("files", []):
            self.klasorler[kl["name"]] = kl["id"]
            token = None
            while True:
                r = self._tekrarla(lambda: self.svc.files().list(
                    q=f"'{kl['id']}' in parents and trashed = false",
                    fields="nextPageToken, files(id, appProperties)", pageSize=1000, pageToken=token).execute())
                for f in r.get("files", []):
                    bid = (f.get("appProperties") or {}).get("bedestenId")
                    if bid:
                        sonuc[bid] = f["id"]
                token = r.get("nextPageToken")
                if not token:
                    break
        return sonuc


# ----------------------------------------------------------------------------
# Durum (kaldığı yerden devam)
# ----------------------------------------------------------------------------
class Durum:
    def __init__(self, klasor: str):
        os.makedirs(klasor, exist_ok=True)
        self.indirilen_yol = os.path.join(klasor, "indirilenler.tsv")
        self.pencere_yol = os.path.join(klasor, "tamamlanan_pencereler.txt")
        self.hata_yol = os.path.join(klasor, "hatalar.tsv")
        self.kilit = threading.Lock()
        self.indirilen: set[str] = set()
        self.pencereler: set[str] = set()
        self.dosya_vardi = os.path.exists(self.indirilen_yol)
        if self.dosya_vardi:
            with open(self.indirilen_yol, encoding="utf-8") as f:
                for satir in f:
                    p = satir.rstrip("\n").split("\t")
                    if p and p[0] and p[0] != "bedesten_id":
                        self.indirilen.add(p[0])
        else:
            with open(self.indirilen_yol, "w", encoding="utf-8") as f:
                f.write("bedesten_id\tdrive_id\tkunye\n")
        if os.path.exists(self.pencere_yol):
            dun = (dt.datetime.now(TR) - dt.timedelta(days=1)).isoformat()
            with open(self.pencere_yol, encoding="utf-8") as f:
                for satir in f:
                    p = satir.strip().split("\t")
                    # "anahtar" = kalıcı bitti; "anahtar<TAB>zaman" = son günlerden, 1 gün geçerli
                    if p[0] and (len(p) == 1 or p[1] > dun):
                        self.pencereler.add(p[0])

    def ekle(self, bid: str, drive_id: str, kunye_: str):
        with self.kilit:
            self.indirilen.add(bid)
            with open(self.indirilen_yol, "a", encoding="utf-8") as f:
                f.write(f"{bid}\t{drive_id}\t{kunye_}\n")

    def hata(self, bid: str, kunye_: str, mesaj: str):
        with self.kilit:
            with open(self.hata_yol, "a", encoding="utf-8") as f:
                f.write(f"{dt.datetime.now(TR):%Y-%m-%d %H:%M}\t{bid}\t{kunye_}\t{mesaj[:300]}\n")

    def pencere_bitti(self, anahtar: str, gecici: bool = False):
        with self.kilit:
            self.pencereler.add(anahtar)
            with open(self.pencere_yol, "a", encoding="utf-8") as f:
                f.write(anahtar + (f"\t{dt.datetime.now(TR).isoformat()}" if gecici else "") + "\n")


# ----------------------------------------------------------------------------
# Ana akış
# ----------------------------------------------------------------------------
def aylar(bas: dt.date, bit: dt.date):
    a = bas.replace(day=1)
    while a <= bit:
        sonraki = (a.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        yield max(a, bas), min(sonraki - dt.timedelta(days=1), bit)
        a = sonraki


def belge_html(kunye_: str, kaynak: str, ham: bytes) -> bytes:
    metin = ham.decode("utf-8", errors="replace")
    ust = (f"<p><b>{html.escape(kunye_)}</b><br>"
           f"Kaynak: <a href=\"{kaynak}\">{kaynak}</a></p><hr>")
    if re.search(r"<body[^>]*>", metin, re.I):
        metin = re.sub(r"(<body[^>]*>)", lambda m: m.group(1) + ust, metin, count=1, flags=re.I)
    else:
        metin = f"<html><head><meta charset=\"utf-8\"></head><body>{ust}{metin}</body></html>"
    return metin.encode("utf-8")


class SureDoldu(Exception):
    pass


def main():
    for d in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN", "DRIVE_KLASOR_ID"):
        if not os.environ.get(d):
            sys.exit(f"Eksik ortam değişkeni: {d}")

    daire = os.environ.get("DAIRE", "9. Hukuk Dairesi")
    bas = dt.date.fromisoformat(os.environ.get("BASLANGIC", "2020-01-01"))
    bit = dt.date.fromisoformat(os.environ.get("BITIS") or dt.datetime.now(TR).date().isoformat())
    ifade = os.environ.get("ARAMA_IFADESI", "karar")
    yeni_once = os.environ.get("SIRALAMA", "yeni").lower() != "eski"
    yon = "desc" if yeni_once else "asc"
    limit_dk = float(os.environ.get("SURE_LIMITI_DK", "0") or 0)
    bitis_ani = time.monotonic() + limit_dk * 60 if limit_dk > 0 else None
    bugun = dt.datetime.now(TR).date()

    api = Bedesten(float(os.environ.get("ISTEK_ARALIGI_SN", "2.0")))
    drive = Drive(os.environ["DRIVE_KLASOR_ID"])
    durum = Durum(os.environ.get("DURUM_KLASORU", "durum"))

    if not durum.dosya_vardi:
        log("Durum dosyası yok; Drive'daki mevcut kararlar taranıyor (çift kayıt olmasın diye)...")
        for bid, fid in drive.mevcutlar().items():
            durum.ekle(bid, fid, "(Drive'dan geri yüklendi)")
        log(f"  {len(durum.indirilen)} karar zaten Drive'da")

    log(f"{daire} | {bas} → {bit} | {'en yeniden başlayarak' if yeni_once else 'en eskiden başlayarak'} "
        f"| daha önce indirilen: {len(durum.indirilen)}")
    sayac = {"yeni": 0, "hata": 0}
    havuz = ThreadPoolExecutor(max_workers=3)  # Drive yüklemeleri Bedesten beklerken arka planda

    def sure_kontrol():
        if bitis_ani and time.monotonic() > bitis_ani:
            raise SureDoldu()

    def isle(k: dict) -> bool:
        bid = str(k["documentId"])
        if bid in durum.indirilen:
            return True
        kun, tarih = kunye(k, daire)
        kaynak = f"https://mevzuat.adalet.gov.tr/ictihat/{bid}"
        try:
            ham, mime = api.belge(bid)
        except Exception as e:  # noqa: BLE001
            durum.hata(bid, kun, f"indirme: {e}")
            sayac["hata"] += 1
            log(f"  HATA indirme {kun}: {e}")
            return False
        yil = str(tarih.year) if tarih else "tarihsiz"
        if mime == "text/html":
            icerik, ad = belge_html(kun, kaynak, ham), kun
        else:
            icerik, ad = ham, kun + (".pdf" if mime == "application/pdf" else "")

        def yukle():
            try:
                fid = drive.yukle(ad, icerik, mime, yil, bid, kaynak)
                durum.ekle(bid, fid, kun)
                sayac["yeni"] += 1
                if sayac["yeni"] % 50 == 0:
                    log(f"  {sayac['yeni']} yeni karar yüklendi (toplam {len(durum.indirilen)})")
                return True
            except Exception as e:  # noqa: BLE001
                durum.hata(bid, kun, f"drive: {e}")
                sayac["hata"] += 1
                log(f"  HATA Drive {kun}: {e}")
                return False

        return havuz.submit(yukle)

    def pencere(b: dt.date, s: dt.date):
        anahtar = f"{b}_{s}"
        if anahtar in durum.pencereler:
            return
        sure_kontrol()
        toplam, ilk = api.ara(ifade, daire, b, s, 1, yon)
        if toplam > PENCERE_UST_SINIRI and b < s:
            orta = b + (s - b) // 2
            parcalar = [(b, orta), (orta + dt.timedelta(days=1), s)]
            for pb, ps in (reversed(parcalar) if yeni_once else parcalar):
                pencere(pb, ps)
            if f"{b}_{orta}" in durum.pencereler and f"{orta + dt.timedelta(days=1)}_{s}" in durum.pencereler:
                durum.pencere_bitti(anahtar, gecici=(bugun - s).days <= SON_GUNLER_ACIK)
            return
        log(f"Pencere {b} → {s}: {toplam} karar")
        sayfalar = (toplam + SAYFA_BOYU - 1) // SAYFA_BOYU
        isler = []
        for sayfa in range(1, sayfalar + 1):
            liste = ilk if sayfa == 1 else api.ara(ifade, daire, b, s, sayfa, yon)[1]
            for k in liste:
                sure_kontrol()
                isler.append(isle(k))
        tamam = all(i if isinstance(i, bool) else i.result() for i in isler)
        if tamam:
            durum.pencere_bitti(anahtar, gecici=(bugun - s).days <= SON_GUNLER_ACIK)

    try:
        donemler = list(aylar(bas, bit))
        for b, s in (reversed(donemler) if yeni_once else donemler):
            pencere(b, s)
        log("Tüm tarih aralığı tarandı.")
    except SureDoldu:
        log("Süre sınırı doldu; bir sonraki çalıştırmada kalınan yerden devam edilecek.")
    finally:
        havuz.shutdown(wait=True)
        log(f"Bu çalıştırmada: {sayac['yeni']} yeni karar, {sayac['hata']} hata. "
            f"Toplam indirilen: {len(durum.indirilen)}")


if __name__ == "__main__":
    main()
