from dataclasses import dataclass
from datetime import datetime, timezone

import motor.motor_asyncio
from pydantic import BaseModel

from config import settings


class PriceHistoryEntry(BaseModel):
    price: str
    price_value: int
    recorded_at: datetime


class Listing(BaseModel):
    listing_id: str
    title: str
    price: str
    price_value: int
    location: str
    room_count: str | None = None
    area_m2: int | None = None
    building_age: str | None = None
    url: str
    price_history: list[PriceHistoryEntry] = []
    created_at: datetime
    updated_at: datetime
    is_active: bool = True
    removed_at: datetime | None = None
    missed_scans: int = 0


@dataclass
class UpsertResult:
    listing: Listing
    is_new: bool
    price_changed: bool
    old_price: str | None
    old_price_value: int | None
    direction: str | None  # "drop" | "rise" | None
    price_drop_count: int = 0  # bu düşüş dahil toplam düşüş sayısı
    price_chain: list[str] | None = None  # ilk fiyattan güncel fiyata tüm zincir


_client: motor.motor_asyncio.AsyncIOMotorClient | None = None


def get_db() -> motor.motor_asyncio.AsyncIOMotorDatabase:
    global _client
    if _client is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(settings.mongo_uri)
    return _client[settings.mongo_db_name]


async def init_indexes() -> None:
    db = get_db()
    await db.listings.create_index("listing_id", unique=True)
    await db.listings.create_index([("location", 1), ("room_count", 1)])
    await db.listings.create_index("is_active")
    await db.listings.create_index("removed_at")
    await db.weekly_metrics.create_index("recorded_at")


async def upsert_listing(listing: Listing) -> UpsertResult:
    db = get_db()
    now = datetime.now(tz=timezone.utc)

    existing = await db.listings.find_one({"listing_id": listing.listing_id})

    if existing is None:
        doc = listing.model_dump(
            exclude={"created_at", "updated_at", "price_history", "removed_at", "missed_scans"}
        )
        doc["price_history"] = []
        doc["created_at"] = now
        doc["updated_at"] = now
        doc["is_active"] = True
        doc["missed_scans"] = 0
        doc["removed_at"] = None
        await db.listings.insert_one(doc)
        return UpsertResult(
            listing=listing,
            is_new=True,
            price_changed=False,
            old_price=None,
            old_price_value=None,
            direction=None,
        )

    # Faz 1'den kalan eski dökümanlar price_value taşımaz; backfill yap, fiyat değişimi sayma
    if "price_value" not in existing:
        await db.listings.update_one(
            {"listing_id": listing.listing_id},
            {
                "$set": {
                    "price_value": listing.price_value,
                    "area_m2": listing.area_m2,
                    "updated_at": now,
                    "is_active": True,
                    "missed_scans": 0,
                    "removed_at": None,
                }
            },
        )
        return UpsertResult(
            listing=listing,
            is_new=False,
            price_changed=False,
            old_price=None,
            old_price_value=None,
            direction=None,
        )

    existing_value = existing["price_value"]

    if existing_value != listing.price_value:
        history_entry = {
            "price": existing["price"],
            "price_value": existing_value,
            "recorded_at": now,
        }
        direction = "drop" if listing.price_value < existing_value else "rise"
        prior_history = existing.get("price_history") or []
        prior_drops = sum(
            1 for h in prior_history if h.get("price_value", 0) > listing.price_value
            # ponytail: sayıyoruz "bu anki fiyattan pahalı olan geçmiş kayıtlar" = önceki düşüşler
        )
        drop_count = (prior_drops + 1) if direction == "drop" else 0
        price_chain = [h["price"] for h in prior_history] + [existing["price"], listing.price]
        await db.listings.update_one(
            {"listing_id": listing.listing_id},
            {
                "$set": {
                    "price": listing.price,
                    "price_value": listing.price_value,
                    "area_m2": listing.area_m2,
                    "updated_at": now,
                    "is_active": True,
                    "missed_scans": 0,
                    "removed_at": None,
                },
                "$push": {"price_history": history_entry},
            },
        )
        return UpsertResult(
            listing=listing,
            is_new=False,
            price_changed=True,
            old_price=existing["price"],
            old_price_value=existing_value,
            direction=direction,
            price_drop_count=drop_count,
            price_chain=price_chain,
        )

    # fiyat aynı — updated_at + sayısal alanları backfill; ilan görüldü, canlı işaretle
    await db.listings.update_one(
        {"listing_id": listing.listing_id},
        {
            "$set": {
                "price_value": listing.price_value,
                "area_m2": listing.area_m2,
                "updated_at": now,
                "is_active": True,
                "missed_scans": 0,
                "removed_at": None,
            }
        },
    )
    return UpsertResult(
        listing=listing,
        is_new=False,
        price_changed=False,
        old_price=None,
        old_price_value=None,
        direction=None,
    )


async def mark_removed(listing_id: str) -> None:
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    await db.listings.update_one(
        {"listing_id": listing_id},
        {"$set": {"is_active": False, "removed_at": now}},
    )


async def mark_missing_listings(seen_ids: set[str]) -> list[dict]:
    """Taramada görülmeyen canlı ilanların missed_scans sayacını artırır;
    eşiğe (>=2) ulaşanları is_active=False + removed_at ile işaretler.
    Kaldırılan ilanların listesini döndürür."""
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    seen = list(seen_ids)

    await db.listings.update_many(
        {"is_active": {"$ne": False}, "listing_id": {"$nin": seen}},
        {"$inc": {"missed_scans": 1}},
    )

    to_remove = await db.listings.find(
        {
            "is_active": {"$ne": False},
            "missed_scans": {"$gte": 2},
            "listing_id": {"$nin": seen},
        },
        {"listing_id": 1, "created_at": 1, "_id": 0},
    ).to_list(length=None)

    if to_remove:
        await db.listings.update_many(
            {"listing_id": {"$in": [d["listing_id"] for d in to_remove]}},
            {"$set": {"is_active": False, "removed_at": now}},
        )

    return to_remove


async def save_weekly_snapshot(avg_price_per_m2: float, sample_size: int) -> None:
    """Bu haftanın ortalama m² fiyat özetini weekly_metrics koleksiyonuna yazar."""
    db = get_db()
    now = datetime.now(tz=timezone.utc)
    await db.weekly_metrics.insert_one(
        {
            "recorded_at": now,
            "avg_price_per_m2": avg_price_per_m2,
            "sample_size": sample_size,
        }
    )


async def get_weekly_snapshots(limit: int = 12) -> list[dict]:
    """Grafik için son `limit` adet haftalık snapshot'ı artan sırada döndürür."""
    db = get_db()
    cursor = (
        db.weekly_metrics.find({}, {"recorded_at": 1, "avg_price_per_m2": 1, "sample_size": 1, "_id": 0})
        .sort("recorded_at", -1)
        .limit(limit)
    )
    docs = await cursor.to_list(length=None)
    return list(reversed(docs))


async def get_last_snapshot() -> dict | None:
    """WoW hesabı için en son kaydedilmiş snapshot'ı döndürür."""
    db = get_db()
    return await db.weekly_metrics.find_one(
        {},
        {"avg_price_per_m2": 1, "sample_size": 1, "_id": 0},
        sort=[("recorded_at", -1)],
    )


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
