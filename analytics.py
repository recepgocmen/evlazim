import logging
import statistics
from dataclasses import dataclass

from config import MIN_COMPARABLE_LISTINGS, OPPORTUNITY_THRESHOLD
from database import Listing, get_db

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
