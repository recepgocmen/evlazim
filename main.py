import asyncio
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analytics import compute_baseline, evaluate_opportunity
from config import SCAN_INTERVAL_MIN, SEARCH_URL, settings
from database import close as db_close, get_db, init_indexes, upsert_listing
from notifier import TelegramNotifier
from scraper import fetch_all_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

notifier = TelegramNotifier()


async def scrape_job() -> None:
    logger.info(f"scrape job starting — url={SEARCH_URL}")
    db = get_db()

    listings = await fetch_all_listings(SEARCH_URL)
    if not listings:
        logger.warning("scrape returned 0 listings — skipping")
        return

    is_first_run = await db.listings.count_documents({}, limit=1) == 0
    if is_first_run:
        logger.info("first run detected — seeding DB, no notifications will be sent")

    new_count = 0
    changed_count = 0

    for listing in listings:
        result = await upsert_listing(listing)

        if is_first_run:
            continue

        if result.is_new:
            opportunity = None
            baseline = await compute_baseline(
                listing.location, listing.room_count, listing.listing_id
            )
            if baseline:
                opportunity = evaluate_opportunity(listing, baseline)
            try:
                await notifier.send_new_listing(listing, opportunity)
                new_count += 1
            except Exception as exc:
                logger.error(f"failed to notify new listing_id={listing.listing_id}: {exc}")

        elif result.price_changed:
            opportunity = None
            if result.direction == "drop":
                baseline = await compute_baseline(
                    listing.location, listing.room_count, listing.listing_id
                )
                if baseline:
                    opportunity = evaluate_opportunity(listing, baseline)
            try:
                await notifier.send_price_change(
                    listing,
                    old_price=result.old_price,
                    direction=result.direction,
                    opportunity=opportunity,
                )
                changed_count += 1
            except Exception as exc:
                logger.error(f"failed to notify price change listing_id={listing.listing_id}: {exc}")

    logger.info(
        f"scrape done: total={len(listings)} new={new_count} "
        f"price_changed={changed_count} bootstrap={is_first_run}"
    )


async def main(once: bool = False) -> None:
    await init_indexes()
    logger.info(f"DB indexes ready — interval={SCAN_INTERVAL_MIN}min url={SEARCH_URL}")

    if once:
        await scrape_job()
        await db_close()
        return

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scrape_job,
        "interval",
        minutes=SCAN_INTERVAL_MIN,
        next_run_time=datetime.now(),
    )
    scheduler.start()

    stop_event = asyncio.Event()

    def _shutdown(sig):
        logger.info(f"received {sig.name}, shutting down…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown, sig)

    await stop_event.wait()
    scheduler.shutdown(wait=False)
    await db_close()
    logger.info("shutdown complete")


if __name__ == "__main__":
    once_mode = "--once" in sys.argv
    asyncio.run(main(once=once_mode))
