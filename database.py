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


@dataclass
class UpsertResult:
    listing: Listing
    is_new: bool
    price_changed: bool
    old_price: str | None
    old_price_value: int | None
    direction: str | None  # "drop" | "rise" | None


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


async def upsert_listing(listing: Listing) -> UpsertResult:
    db = get_db()
    now = datetime.now(tz=timezone.utc)

    existing = await db.listings.find_one({"listing_id": listing.listing_id})

    if existing is None:
        doc = listing.model_dump(exclude={"created_at", "updated_at", "price_history"})
        doc["price_history"] = []
        doc["created_at"] = now
        doc["updated_at"] = now
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
        await db.listings.update_one(
            {"listing_id": listing.listing_id},
            {
                "$set": {
                    "price": listing.price,
                    "price_value": listing.price_value,
                    "area_m2": listing.area_m2,
                    "updated_at": now,
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
        )

    # fiyat aynı — updated_at + sayısal alanları backfill
    await db.listings.update_one(
        {"listing_id": listing.listing_id},
        {
            "$set": {
                "price_value": listing.price_value,
                "area_m2": listing.area_m2,
                "updated_at": now,
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


async def close() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None
