import io
import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from config import MIN_COMPARABLE_LISTINGS, OPPORTUNITY_THRESHOLD
from database import Listing, get_db, get_last_snapshot, get_weekly_snapshots, save_weekly_snapshot

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


async def compute_weekly_metrics() -> tuple[float, int]:
    """Aktif ilanlardan bölge genelinde ortalama m² fiyatını hesaplar."""
    db = get_db()
    cursor = db.listings.find(
        {"is_active": {"$ne": False}, "price_value": {"$gt": 0}, "area_m2": {"$gt": 0}},
        {"price_value": 1, "area_m2": 1, "_id": 0},
    )
    docs = await cursor.to_list(length=None)
    if not docs:
        return 0.0, 0
    prices = [d["price_value"] / d["area_m2"] for d in docs]
    return statistics.mean(prices), len(prices)


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


async def compute_seller_behavior() -> tuple[float, float | None]:
    """Son 7 günde fiyat düşüren aktif ilanların oranını ve ort. indirim tutarını hesaplar."""
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=7)

    active_total = await db.listings.count_documents({"is_active": {"$ne": False}})
    if not active_total:
        return 0.0, None

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
    return ratio, avg_discount


def _render_trend_chart(snapshots: list[dict]) -> bytes:
    """weekly_metrics snapshot listesinden çift eksenli trend grafiği üretir.
    Sol eksen: TL/m² (mavi), sağ eksen: aktif ilan sayısı/stok (turuncu)."""
    dates = [d["recorded_at"] for d in snapshots]
    prices = [d["avg_price_per_m2"] for d in snapshots]
    stocks = [d.get("sample_size", 0) for d in snapshots]

    fig, ax1 = plt.subplots(figsize=(9, 4))

    color_price = "#2563eb"
    ax1.plot(dates, prices, marker="o", linewidth=2, color=color_price, label="TL/m²")
    ax1.set_ylabel("TL / m²", color=color_price)
    ax1.tick_params(axis="y", labelcolor=color_price)
    ax1.grid(True, linestyle="--", alpha=0.4)

    ax2 = ax1.twinx()
    color_stock = "#f97316"
    ax2.plot(dates, stocks, marker="s", linewidth=2, linestyle="--",
             color=color_stock, label="Aktif İlan")
    ax2.set_ylabel("Aktif İlan Sayısı", color=color_stock)
    ax2.tick_params(axis="y", labelcolor=color_stock)

    ax1.set_title("Haftalık TL/m² ve Aktif İlan (Stok)", fontsize=13)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left", fontsize=9)

    fig.autofmt_xdate()
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()


async def generate_weekly_report() -> WeeklyReport:
    """Haftalık analitik verileri toplar, snapshot kaydeder ve rapor döndürür."""
    avg_ppm2, sample = await compute_weekly_metrics()
    prev = await get_last_snapshot()           # insert'ten ÖNCE oku (WoW için)
    await save_weekly_snapshot(avg_ppm2, sample)
    snapshots = await get_weekly_snapshots()
    removed_count, avg_days = await compute_days_on_market()
    drop_ratio, avg_discount = await compute_seller_behavior()

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

    chart = _render_trend_chart(snapshots)
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
    )
