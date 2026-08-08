import asyncio
import io
import json
import logging
import statistics
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import MIN_COMPARABLE_LISTINGS, OPPORTUNITY_THRESHOLD
from database import (
    Listing,
    get_db,
    get_snapshot_before,
    get_snapshots,
    save_metrics_snapshot,
)

logger = logging.getLogger(__name__)


@dataclass
class Baseline:
    median: float   # medyan fiyat/m²
    average: float  # ortalama fiyat/m²
    sample_size: int


@dataclass
class Opportunity:
    discount_pct: float  # 0.0–1.0 arası; örn 0.20 → %20 indirim
    baseline: Baseline


async def compute_baseline(
    location: str, room_count: str | None, exclude_id: str
) -> Baseline | None:
    db = get_db()
    query = {
        "listing_id": {"$ne": exclude_id},
        "location": location,
        "room_count": room_count,
        "price_value": {"$gt": 0},
        "area_m2": {"$gt": 0},
    }
    cursor = db.listings.find(query, {"price_value": 1, "area_m2": 1, "_id": 0})
    docs = await cursor.to_list(length=None)

    if len(docs) < MIN_COMPARABLE_LISTINGS:
        logger.debug(
            f"baseline skipped: only {len(docs)} comparables for "
            f"location={location!r} room_count={room_count!r}"
        )
        return None

    price_per_m2 = [d["price_value"] / d["area_m2"] for d in docs]
    return Baseline(
        median=statistics.median(price_per_m2),
        average=statistics.mean(price_per_m2),
        sample_size=len(price_per_m2),
    )


def evaluate_opportunity(
    listing: Listing,
    baseline: Baseline,
    threshold: float = OPPORTUNITY_THRESHOLD,
) -> Opportunity | None:
    if not listing.area_m2 or not listing.price_value:
        return None

    listing_ppm2 = listing.price_value / listing.area_m2
    discount = (baseline.median - listing_ppm2) / baseline.median

    if discount >= threshold:
        return Opportunity(discount_pct=discount, baseline=baseline)
    return None


# ---------------------------------------------------------------------------
# Haftalık analitik
# ---------------------------------------------------------------------------

@dataclass
class WeeklyReport:
    avg_price_per_m2: float
    sample_size: int                  # aktif ilan = stok
    price_wow_pct: float | None       # fiyat haftalık % değişim
    inventory_wow_pct: float | None   # stok haftalık % değişim
    removed_count: int
    avg_days_on_market: float | None
    drop_ratio: float                 # 0.0–1.0; fiyat düşüren aktif ilan oranı
    avg_discount_amount: float | None  # ortalama indirim tutarı (TL)
    chart_png: bytes
    new_count: int                    # son 7 günde eklenen ilan sayısı
    net_inventory_change: int         # new_count - removed_count
    dropped_count: int                # son 7 günde fiyat düşüren ilan adedi
    median_price_per_m2: float
    min_price_per_m2: float
    max_price_per_m2: float
    usd_try: float | None            # rapor anındaki USD/TRY kuru
    age_buckets: dict[str, int]      # aktif ilanların yayında kalma süresi dağılımı


async def compute_weekly_metrics() -> tuple[float, int, float, float, float]:
    """Aktif ilanlardan bölge genelinde m² fiyat istatistiklerini hesaplar.
    Döner: (ortalama, adet, medyan, min, max)."""
    db = get_db()
    cursor = db.listings.find(
        {"is_active": {"$ne": False}, "price_value": {"$gt": 0}, "area_m2": {"$gt": 0}},
        {"price_value": 1, "area_m2": 1, "_id": 0},
    )
    docs = await cursor.to_list(length=None)
    if not docs:
        return 0.0, 0, 0.0, 0.0, 0.0
    prices = [d["price_value"] / d["area_m2"] for d in docs]
    return (
        statistics.mean(prices),
        len(prices),
        statistics.median(prices),
        min(prices),
        max(prices),
    )


async def compute_new_listings_count() -> int:
    """Son 7 günde eklenen (created_at) ilan sayısını döndürür."""
    db = get_db()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    return await db.listings.count_documents({"created_at": {"$gte": cutoff}})


async def compute_days_on_market() -> tuple[int, float | None]:
    """Son 7 günde siteden kalkan ilanların ortalama yayında kalma süresini hesaplar."""
    db = get_db()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    cursor = db.listings.find(
        {"removed_at": {"$gte": cutoff}},
        {"created_at": 1, "removed_at": 1, "_id": 0},
    )
    docs = await cursor.to_list(length=None)
    if not docs:
        return 0, None
    days_list = [(d["removed_at"] - d["created_at"]).days for d in docs]
    return len(days_list), statistics.mean(days_list)


async def compute_days_on_market_series(days: int = 60) -> list[tuple[object, float]]:
    """Tarihe göre ortalama ilanda kalma süresi: her gün için, o günle biten 7 günlük
    pencerede siteden kalkan ilanların ortalaması. Haftalık pencere, günlük 1-2 kaldırmanın
    yarattığı gürültüyü söndürür."""
    db = get_db()
    today = datetime.now(tz=timezone.utc).date()
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days + 7)
    cursor = db.listings.find(
        {"removed_at": {"$gte": cutoff}},
        {"created_at": 1, "removed_at": 1, "_id": 0},
    )
    docs = await cursor.to_list(length=None)

    by_day: dict[object, list[int]] = {}
    for d in docs:
        by_day.setdefault(d["removed_at"].date(), []).append(
            (d["removed_at"] - d["created_at"]).days
        )

    series = []
    for offset in range(days, -1, -1):
        day = today - timedelta(days=offset)
        window = [
            v for back in range(7) for v in by_day.get(day - timedelta(days=back), [])
        ]
        if window:
            series.append((day, statistics.mean(window)))
    return series


_USD_TRY_URL = "https://open.er-api.com/v6/latest/USD"  # anahtarsız, günlük güncellenen ücretsiz API
_AGE_BUCKETS = ("0-7 gün", "8-30 gün", "31-60 gün", "61-90 gün", "90+ gün")


_usd_cache: tuple[float, datetime] | None = None


def _get_json(url: str) -> dict:
    # varsayılan python-urllib UA'sı bazı kur API'lerinde 403 alıyor
    req = urllib.request.Request(url, headers={"User-Agent": "evlazim/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


async def fetch_usd_try() -> float | None:
    """Güncel USD/TRY kurunu döndürür; API erişilemezse None.
    Kaynak günde bir güncellendiği için 6 saat cache'lenir (her taramada çağrılıyor)."""
    global _usd_cache
    now = datetime.now(tz=timezone.utc)
    if _usd_cache and now - _usd_cache[1] < timedelta(hours=6):
        return _usd_cache[0]

    try:
        rate = float((await asyncio.to_thread(_get_json, _USD_TRY_URL))["rates"]["TRY"])
    except Exception as exc:  # ağ/format hatası taramayı bloklamasın
        logger.warning(f"USD/TRY kuru alinamadi: {exc}")
        return None
    _usd_cache = (rate, now)
    logger.info(f"USD/TRY = {rate:.4f}")
    return rate


_USD_HISTORY_URL = "https://api.frankfurter.dev/v1/{start}..{end}?base=USD&symbols=TRY"


async def fetch_usd_try_history(start: date, end: date) -> dict[date, float]:
    """Geçmiş günlerin USD/TRY serisi (ECB, anahtarsız). Kur alanı olmayan eski
    snapshot'ların USD karşılığını hesaplamak için. Hafta sonu/tatil boşlukları bir
    önceki iş gününün kuruyla doldurulur."""
    # start hafta sonuna denk gelirse ilk günler boş kalmasın diye 5 gün geriden iste
    url = _USD_HISTORY_URL.format(start=start - timedelta(days=5), end=end)
    try:
        raw = (await asyncio.to_thread(_get_json, url))["rates"]
    except Exception as exc:
        logger.warning(f"USD/TRY gecmisi alinamadi: {exc}")
        return {}

    published = {date.fromisoformat(k): v["TRY"] for k, v in raw.items()}
    filled: dict[date, float] = {}
    rate = None
    day = start - timedelta(days=5)
    while day <= end:
        rate = published.get(day, rate)
        if rate and day >= start:
            filled[day] = rate
        day += timedelta(days=1)
    return filled


async def record_snapshot() -> None:
    """Her taramanın sonunda anlık piyasa metriklerini + kuru snapshot'lar."""
    avg_ppm2, sample, *_ = await compute_weekly_metrics()
    if not sample:
        return
    await save_metrics_snapshot(avg_ppm2, sample, await fetch_usd_try())


def _age_bucket(days: int) -> str:
    if days <= 7:
        return _AGE_BUCKETS[0]
    if days <= 30:
        return _AGE_BUCKETS[1]
    if days <= 60:
        return _AGE_BUCKETS[2]
    if days <= 90:
        return _AGE_BUCKETS[3]
    return _AGE_BUCKETS[4]


async def compute_age_distribution() -> dict[str, int]:
    """Aktif ilanların kaç gündür yayında olduğunu kovalara böler."""
    db = get_db()
    now = datetime.now(tz=timezone.utc).replace(tzinfo=None)  # Mongo naive UTC döner
    cursor = db.listings.find(
        {"is_active": {"$ne": False}}, {"created_at": 1, "_id": 0}
    )
    docs = await cursor.to_list(length=None)
    buckets = dict.fromkeys(_AGE_BUCKETS, 0)
    for d in docs:
        buckets[_age_bucket((now - d["created_at"]).days)] += 1
    return buckets


async def compute_seller_behavior() -> tuple[float, float | None, int]:
    """Son 7 günde fiyat düşüren aktif ilanların oranını, ort. indirim tutarını ve adedini hesaplar."""
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=7)

    active_total = await db.listings.count_documents({"is_active": {"$ne": False}})
    if not active_total:
        return 0.0, None, 0

    cursor = db.listings.find(
        {"is_active": {"$ne": False}, "price_history.recorded_at": {"$gte": cutoff}},
        {"price_value": 1, "price_history": 1, "_id": 0},
    )
    docs = await cursor.to_list(length=None)

    # MongoDB naive UTC döndürür; Python karşılaştırması için cutoff'u da naive yap
    cutoff_naive = cutoff.replace(tzinfo=None)

    dropped_listings = 0
    discounts = []
    for d in docs:
        hist = d.get("price_history", [])
        listing_dropped = False
        for i, entry in enumerate(hist):
            if entry["recorded_at"] < cutoff_naive:
                continue
            # entry = değişim anındaki ESKİ fiyat; yeni fiyat bir sonraki entry ya da güncel price_value
            new_val = hist[i + 1]["price_value"] if i + 1 < len(hist) else d["price_value"]
            if new_val < entry["price_value"]:
                listing_dropped = True
                discounts.append(entry["price_value"] - new_val)
        if listing_dropped:
            dropped_listings += 1

    ratio = dropped_listings / active_total
    avg_discount = statistics.mean(discounts) if discounts else None
    return ratio, avg_discount, dropped_listings


def _daily_series(
    snapshots: list[dict], usd_history: dict[date, float] | None = None
) -> list[dict]:
    """Her taramada yazılan snapshot'ları günlük ortalamaya indirger — ham hâli
    (günde ~100 nokta) grafikte okunmuyor. Snapshot'ta kur yoksa (eski kayıtlar)
    o günün tarihsel kuru kullanılır."""
    usd_history = usd_history or {}
    by_day: dict[date, list[dict]] = {}
    for d in snapshots:
        by_day.setdefault(d["recorded_at"].date(), []).append(d)

    series = []
    for day in sorted(by_day):
        rows = by_day[day]
        price = statistics.mean(r["avg_price_per_m2"] for r in rows)
        rate = next(
            (r["usd_try"] for r in rows if r.get("usd_try")), usd_history.get(day)
        )
        series.append(
            {
                "date": day,
                "price": price,
                "stock": statistics.mean(r.get("sample_size", 0) for r in rows),
                "usd_price": price / rate if rate else float("nan"),
            }
        )
    return series


def _render_trend_chart(
    snapshots: list[dict],
    age_buckets: dict[str, int],
    dom_series: list[tuple[object, float]],
    usd_history: dict[date, float] | None = None,
) -> bytes:
    """1) TL/m² + USD/m², 2) stok + ort. ilanda kalma süresi, 3) süre dağılımı."""
    series = _daily_series(snapshots, usd_history)
    dates = [d["date"] for d in series]
    prices = [d["price"] for d in series]
    stocks = [d["stock"] for d in series]
    usd_prices = [d["usd_price"] for d in series]
    # marker hep açık: tek günlük/kesik seriler (örn. kuru yeni gelen USD) çizgiyle görünmüyor
    marker_size = 4

    fig, (ax1, ax_stock, ax_age) = plt.subplots(
        3, 1, figsize=(9, 10), gridspec_kw={"height_ratios": [3, 2, 2]}
    )

    color_price = "#2563eb"
    ax1.plot(dates, prices, marker="o", markersize=marker_size, linewidth=2,
             color=color_price, label="TL/m²")
    ax1.set_ylabel("TL / m²", color=color_price)
    ax1.tick_params(axis="y", labelcolor=color_price)
    ax1.grid(True, linestyle="--", alpha=0.4)

    ax2 = ax1.twinx()
    color_usd = "#16a34a"
    ax2.plot(dates, usd_prices, marker="^", markersize=marker_size, linewidth=2, linestyle="-.",
             color=color_usd, label="USD/m²")
    ax2.set_ylabel("USD / m²", color=color_usd)
    ax2.tick_params(axis="y", labelcolor=color_usd)

    ax1.set_title("Birim Fiyat Trendi — TL ve Dolar Bazlı", fontsize=13)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

    ax_stock.plot(dates, stocks, marker="s", markersize=marker_size, linewidth=2, linestyle="--",
                  color="#f97316", label="Aktif İlan")
    ax_stock.set_ylabel("Aktif İlan Sayısı", color="#f97316")
    ax_stock.tick_params(axis="y", labelcolor="#f97316")
    ax_stock.set_title("Stok ve Ortalama İlanda Kalma Süresi", fontsize=12)
    ax_stock.grid(True, linestyle="--", alpha=0.4)

    color_dom = "#9333ea"
    ax_dom = ax_stock.twinx()
    ax_dom.plot(
        [d for d, _ in dom_series], [v for _, v in dom_series],
        marker="d", markersize=marker_size, linewidth=2, color=color_dom,
        label="Ort. İlanda Kalma (7g)",
    )
    ax_dom.set_ylabel("Gün", color=color_dom)
    ax_dom.tick_params(axis="y", labelcolor=color_dom)

    lines3, labels3 = ax_stock.get_legend_handles_labels()
    lines4, labels4 = ax_dom.get_legend_handles_labels()
    ax_stock.legend(lines3 + lines4, labels3 + labels4, loc="upper left", fontsize=9)

    bars = ax_age.bar(list(age_buckets), list(age_buckets.values()), color="#0f766e")
    ax_age.bar_label(bars, fontsize=9)
    ax_age.set_ylabel("İlan Sayısı")
    ax_age.set_title("Aktif İlanların Yayında Kalma Süresi", fontsize=12)
    ax_age.grid(True, axis="y", linestyle="--", alpha=0.4)

    # autofmt_xdate çoklu panelde üst eksenlerin tarih etiketlerini gizliyor; elle döndür
    for ax in (ax1, ax_stock):
        ax.tick_params(axis="x", rotation=30, labelsize=8)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


async def generate_weekly_report() -> WeeklyReport:
    """Haftalık analitik verileri toplar ve raporu döndürür.
    Snapshot'lar her taramada record_snapshot() ile yazılır."""
    avg_ppm2, sample, median_ppm2, min_ppm2, max_ppm2 = await compute_weekly_metrics()
    prev = await get_snapshot_before(days=7)
    snapshots = await get_snapshots()
    removed_count, avg_days = await compute_days_on_market()
    drop_ratio, avg_discount, dropped_count = await compute_seller_behavior()
    new_count = await compute_new_listings_count()
    age_buckets = await compute_age_distribution()
    dom_series = await compute_days_on_market_series()
    usd_try = await fetch_usd_try()
    usd_history = (
        await fetch_usd_try_history(
            snapshots[0]["recorded_at"].date(), datetime.now(tz=timezone.utc).date()
        )
        if snapshots
        else {}
    )

    price_wow = (
        (avg_ppm2 - prev["avg_price_per_m2"]) / prev["avg_price_per_m2"] * 100
        if prev and prev.get("avg_price_per_m2")
        else None
    )
    inv_wow = (
        (sample - prev["sample_size"]) / prev["sample_size"] * 100
        if prev and prev.get("sample_size")
        else None
    )

    chart = _render_trend_chart(snapshots, age_buckets, dom_series, usd_history)
    return WeeklyReport(
        avg_price_per_m2=avg_ppm2,
        sample_size=sample,
        price_wow_pct=price_wow,
        inventory_wow_pct=inv_wow,
        removed_count=removed_count,
        avg_days_on_market=avg_days,
        drop_ratio=drop_ratio,
        avg_discount_amount=avg_discount,
        chart_png=chart,
        new_count=new_count,
        net_inventory_change=new_count - removed_count,
        dropped_count=dropped_count,
        median_price_per_m2=median_ppm2,
        min_price_per_m2=min_ppm2,
        max_price_per_m2=max_ppm2,
        usd_try=usd_try,
        age_buckets=age_buckets,
    )


if __name__ == "__main__":
    # Kur/grafik hattının çalıştığını doğrular: python analytics.py
    logging.basicConfig(level=logging.INFO)

    async def _check():
        rate = await fetch_usd_try()
        print(f"guncel USD/TRY: {rate}")

        today = datetime.now(tz=timezone.utc).date()
        hist = await fetch_usd_try_history(today - timedelta(days=14), today)
        print(f"gecmis kur: {len(hist)} gun, ornek: {sorted(hist.items())[:3]}")
        # hafta sonları ECB kur yayınlamaz; forward-fill her günü doldurmalı
        assert len(hist) == 15, hist  # forward-fill her günü doldurmalı (hafta sonu dahil)

        snaps = [
            {"recorded_at": datetime(2026, 8, 1, 9), "avg_price_per_m2": 60000, "sample_size": 100},
            {"recorded_at": datetime(2026, 8, 1, 15), "avg_price_per_m2": 62000, "sample_size": 102},
            {"recorded_at": datetime(2026, 8, 2, 9), "avg_price_per_m2": 61000,
             "sample_size": 101, "usd_try": 40.0},
        ]
        series = _daily_series(snaps, {date(2026, 8, 1): 50.0})
        assert series[0]["usd_price"] == 61000 / 50.0, series[0]   # kur geçmişten
        assert series[1]["usd_price"] == 61000 / 40.0, series[1]   # snapshot'ın kendi kuru
        print("USD serisi OK:", [(s["date"], round(s["usd_price"])) for s in series])

    asyncio.run(_check())
