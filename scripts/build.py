"""Fon Dedektifi veri yayınını üretir (_site/).

Uygulama, güncel fon notlarını bu yayından okur.
  1. Kaynak yayındaki data/*.json dosyalarını ve fon başına geçmiş dosyalarını indirir.
  2. Alım-satım dilini ("GÜÇLÜ AL", "AL" ...) tarafsız kalite notu etiketlerine çevirir.
  3. _site/ klasörüne data/, kısa bir tanıtım sayfası ve data/build.json yazar.

Kaynak adresi kodda yazmaz; KAYNAK_URL ortam değişkeninden (GitHub'da gizli sır) okunur.
İsteğe bağlı KAYNAK_MARKA verilirse, veri metinlerinde geçen bu ad "Fon Dedektifi" ile değiştirilir.
Hata mesajlarında adres yazdırılmaz, yalnızca dosya adı yazdırılır.

Kaynak bozuk ya da eksikse (fon listesi boş, geçmiş dosyalarının çoğu inmedi) betik hata verir ve
_site/ üretilmez; böylece yayındaki veri bozuk veriyle ezilmez.

Kullanım:  KAYNAK_URL=... python scripts/build.py [--out _site]
Yalnızca Python standart kitaplığı kullanılır.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PAGES = ROOT / "sayfalar"          # tanıtım ve (ileride) yasal sayfalar; olduğu gibi kopyalanır
SCHEMA = 1                         # veri biçimi değişirse artırılır; uygulama bunu kontrol eder

# Tek seferlik dosyalar. İlk üçü zorunlu; diğerleri yoksa uygulamadaki ilgili sayfa boş kalır.
REQUIRED = ["meta.json", "funds.json", "categories.json"]
OPTIONAL = ["benchmarks.json", "backtest.json", "simulation.json", "model.json", "model_report.json",
            "bes.json", "bes_categories.json", "bes_model.json"]

# Kaynaktaki sinyal adı -> Fon Dedektifi'ndeki kalite notu etiketi (uygulamadaki util.js ile aynı olmalı)
LABELS = {
    "GÜÇLÜ AL": "Çok yüksek",
    "AL": "Yüksek",
    "İZLE": "Orta",
    "ZAYIF": "Düşük",
    "UZAK DUR": "Çok düşük",
}
UA = "FonDedektifi-veri/1.0"
BRAND = os.environ.get("KAYNAK_MARKA", "").strip()
MAX_MISSING_SHARE = 0.02   # listelerdeki fonların geçmiş dosyalarının en fazla %2'si inmeyebilir


def fetch(url: str, label: str, tries: int = 3) -> bytes | None:
    """URL'yi indirir; 404'te None döner, geçici hatada birkaç kez dener.
    Hata mesajında adres değil yalnızca `label` (dosya adı) yazdırılır."""
    err: Exception | None = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            err = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{label} indirilemedi ({type(err).__name__})")


def relabel(value):
    """Sinyal adlarını kalite notu etiketlerine çevirir (değerlerde ve sözlük anahtarlarında)."""
    if isinstance(value, dict):
        return {LABELS.get(k, k): relabel(v) for k, v in value.items()}
    if isinstance(value, list):
        return [relabel(v) for v in value]
    if isinstance(value, str):
        if value in LABELS:
            return LABELS[value]
        return value.replace(BRAND, "Fon Dedektifi") if BRAND else value
    return value


def codes_in(value, out: set[str]) -> set[str]:
    """JSON içindeki bütün fon kodlarını ("c" alanları) toplar."""
    if isinstance(value, dict):
        c = value.get("c")
        if isinstance(c, str) and 2 <= len(c) <= 4 and c.isalnum():
            out.add(c)
        for v in value.values():
            codes_in(v, out)
    elif isinstance(value, list):
        for v in value:
            codes_in(v, out)
    return out


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "_site"))
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    source = os.environ.get("KAYNAK_URL", "").strip()
    if not source:
        print("HATA: KAYNAK_URL tanımlı değil (GitHub'da Settings → Secrets → Actions)", file=sys.stderr)
        return 1
    base = source.rstrip("/") + "/data/"
    out = Path(args.out)
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)

    # 1) Tek dosyalar
    docs: dict[str, object] = {}
    for name in REQUIRED + OPTIONAL:
        raw = fetch(base + name, name)
        if raw is None:
            if name in REQUIRED:
                print(f"HATA: zorunlu dosya yok: {name}", file=sys.stderr)
                return 1
            print(f"uyarı: {name} yok, ilgili sayfa boş kalacak")
            continue
        docs[name] = json.loads(raw)

    funds = docs["funds.json"]
    if not isinstance(funds, list) or len(funds) < 100:
        print(f"HATA: fon listesi şüpheli ({len(funds) if isinstance(funds, list) else '?'} kayıt)", file=sys.stderr)
        return 1
    meta = docs["meta.json"]
    print(f"kaynak: {meta.get('fundCount')} fon, son fiyat günü {meta.get('lastDataDate')}")

    # 2) Fon başına geçmiş dosyaları
    fund_codes = {f["c"] for f in funds}
    bes_codes = {f["c"] for f in (docs.get("bes.json") or {}).get("funds", [])}
    extra: set[str] = set()
    for name in ("model.json", "model_report.json", "simulation.json", "bes_model.json", "backtest.json"):
        if name in docs:
            codes_in(docs[name], extra)
    jobs = [("history", c) for c in sorted(fund_codes)] + [("bes_history", c) for c in sorted(bes_codes)]
    # Model dosyalarında geçip listelerde olmayan kodlar (eski sepetler): hangi klasördeyse oradan
    for c in sorted(extra - fund_codes - bes_codes):
        jobs += [("history", c), ("bes_history", c)]

    def get(job):
        folder, code = job
        return job, fetch(f"{base}{folder}/{code}.json", f"{folder}/{code}.json")

    histories: dict[tuple[str, str], bytes] = {}
    missing = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for (folder, code), raw in pool.map(get, jobs):
            if raw is None:
                missing.append((folder, code))
            else:
                histories[(folder, code)] = raw
    required_missing = [m for m in missing if (m[0] == "history" and m[1] in fund_codes)
                        or (m[0] == "bes_history" and m[1] in bes_codes)]
    expected = len(fund_codes) + len(bes_codes)
    print(f"geçmiş: {len(histories)} dosya indi, listelerdeki fonlardan {len(required_missing)} eksik")
    if expected and len(required_missing) / expected > MAX_MISSING_SHARE:
        print("HATA: geçmiş dosyalarının çoğu inmedi, yayın durduruldu", file=sys.stderr)
        return 1

    # 3) Yayını geçici klasörde kur, sonra yerine taşı
    if PAGES.exists():
        shutil.copytree(PAGES, tmp)
    else:
        tmp.mkdir(parents=True)
    data = tmp / "data"
    for name, value in docs.items():
        write_json(data / name, relabel(value))
    for (folder, code), raw in histories.items():
        (data / folder).mkdir(parents=True, exist_ok=True)
        (data / folder / f"{code}.json").write_bytes(raw)
    write_json(data / "build.json", {
        "schema": SCHEMA,
        "sourceGenerated": meta.get("generated"),
        "lastDataDate": meta.get("lastDataDate"),
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "funds": len(fund_codes),
        "besFunds": len(bes_codes),
    })
    (tmp / ".nojekyll").write_text("", encoding="utf-8")
    if out.exists():
        shutil.rmtree(out)
    tmp.rename(out)
    files = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"tamam: {out} ({files} dosya)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
