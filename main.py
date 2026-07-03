import asyncio
import logging
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analytics import compute_baseline, evaluate_opportunity, generate_weekly_report
from config import SCAN_INTERVAL_MIN, SEARCH_URL, settings
from database import close as db_close, get_db, init_indexes, mark_missing_listings, mark_removed, upsert_listing
from notifier import TelegramNotifier
from scraper import SessionExpiredError, fetch_all_listings, is_suspicious_title, verify_listings_live

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

notifier = TelegramNotifier()


async def scrape_job() -> None:
    logger.info(f"scrape job starting — url={SEARCH_URL}")
    db = get_db()

    try:
        listings = await fetch_all_listings(SEARCH_URL)
    except SessionExpiredError as exc:
        logger.error(f"oturum doldu: {exc}")
        try:
            await notifier.send_alert(
                "Sahibinden oturumu doldu. python scraper.py --setup ile tekrar giris yap."
            )
        except Exception as notify_exc:
            logger.error(f"alert gonderilemedi: {notify_exc}")
        return

    if not listings:
        logger.warning("scrape returned 0 listings — skipping")
        return

    is_first_run = await db.listings.count_documents({}, limit=1) == 0
    if is_first_run:
        logger.info("first run detected — seeding DB, no notifications will be sent")

    new_count = 0
    changed_count = 0
    candidates: list[tuple] = []  # (UpsertResult, Listing, opportunity | None)

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
            candidates.append((result, listing, opportunity))

        elif result.price_changed:
            opportunity = None
            if result.direction == "drop":
                baseline = await compute_baseline(
                    listing.location, listing.room_count, listing.listing_id
                )
                if baseline:
                    opportunity = evaluate_opportunity(listing, baseline)
            candidates.append((result, listing, opportunity))

    # Sadece supheligi tespit edilen ilanlarin detay sayfasini dogrula
    suspicious_urls = [l.url for _, l, _ in candidates if is_suspicious_title(l.title)]
    if suspicious_urls:
        logger.info(f"{len(suspicious_urls)} supheligi tespit edilen ilan dogrulanacak")
    live_map = await verify_listings_live(suspicious_urls) if suspicious_urls else {}

    for result, listing, opportunity in candidates:
        if is_suspicious_title(listing.title) and not live_map.get(listing.url, True):
            logger.info(f"olu ilan atildi, DB'de kaldirildi: {listing.listing_id}")
            await mark_removed(listing.listing_id)
            continue

        try:
            if result.is_new:
                await notifier.send_new_listing(listing, opportunity)
                new_count += 1
            elif result.price_changed:
                await notifier.send_price_change(
                    listing,
                    old_price=result.old_price,
                    direction=result.direction,
                    opportunity=opportunity,
                )
                changed_count += 1
        except Exception as exc:
            logger.error(f"failed to notify listing_id={listing.listing_id}: {exc}")

    seen_ids = {l.listing_id for l in listings}
    if not is_first_run:
        removed = await mark_missing_listings(seen_ids)
        if removed:
            logger.info(f"marked {len(removed)} listings as removed/sold")

    logger.info(
        f"scrape done: total={len(listings)} new={new_count} "
        f"price_changed={changed_count} bootstrap={is_first_run}"
    )


def _wow(x: float | None) -> str:
    if x is None:
        return "—"
    arrow = "🔺" if x >= 0 else "📉"
    sign = "+" if x >= 0 else ""
    return f"{arrow} {sign}%{x:.1f}"


def _format_weekly_report(report) -> str:
    now_str = datetime.now().strftime("%d.%m.%Y")
    days = f"{report.avg_days_on_market:.0f}" if report.avg_days_on_market is not None else "—"
    discount = (
        f"{report.avg_discount_amount:,.0f} TL"
        if report.avg_discount_amount is not None
        else "—"
    )
    return (
        f"🏠 <b>EV LAZIM — HAFTALIK EMLAK PİYASASI ANALİZİ</b>\n"
        f"📅 <i>Rapor Tarihi: {now_str}</i>\n"
        f"📍 <i>Bölge: İstanbul / Maltepe &amp; İdealtepe</i>\n"
        f"\n"
        f"📈 <b>1. BÖLGESEL BİRİM FİYAT TRENDİ</b>\n"
        f"• <b>Ortalama m² Fiyatı:</b> {report.avg_price_per_m2:,.0f} TL / m²\n"
        f"• <b>Geçen Haftaya Göre Değişim:</b> {_wow(report.price_wow_pct)}\n"
        f"💡 <i>Aktif ilanların ağırlıklı ortalama birim fiyatı.</i>\n"
        f"\n"
        f"⏱️ <b>2. PİYASA LİKİDİTESİ &amp; SATIŞ HIZI</b>\n"
        f"• <b>Ortalama İlanda Kalma Süresi:</b> {days} Gün\n"
        f"• <b>Bu Hafta İlandan Kalkan İlan Sayısı:</b> {report.removed_count} Adet\n"
        f"💡 <i>30 günün altındaki süreler piyasanın canlı olduğunu gösterir.</i>\n"
        f"\n"
        f"📦 <b>3. ARZ &amp; STOK DURUMU</b>\n"
        f"• <b>Aktif İlan Sayısı:</b> {report.sample_size} İlan\n"
        f"• <b>Geçen Haftaya Göre Değişim:</b> {_wow(report.inventory_wow_pct)}\n"
        f"💡 <i>Stok azalması talebin yüksek olduğunu gösterir.</i>\n"
        f"\n"
        f"🤝 <b>4. SATICI DAVRANIŞI &amp; PAZARLIK ESNEKLİĞİ</b>\n"
        f"• <b>Fiyat Düşüren Satıcı Oranı:</b> %{report.drop_ratio * 100:.1f}\n"
        f"• <b>Ortalama Yapılan İndirim Tutarı:</b> {discount}\n"
        f"💡 <i>Bu oranın yükselmesi satıcıların pazarlığa daha açık olduğunu gösterir.</i>\n"
        f"\n"
        f"📊 <i>Haftalık TL/m² ve Stok değişim grafiği ektedir.</i>"
    )


async def weekly_report_job() -> None:
    report = await generate_weekly_report()
    text = _format_weekly_report(report)
    try:
        await notifier.send_weekly_report(text, report.chart_png)
        logger.info("weekly report sent")
    except Exception as exc:
        logger.error(f"failed to send weekly report: {exc}")


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
    scheduler.add_job(weekly_report_job, "cron", day_of_week="mon", hour=9, minute=0)
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


async def _run_weekly() -> None:
    await init_indexes()
    await weekly_report_job()
    await db_close()


if __name__ == "__main__":
    if "--weekly" in sys.argv:
        asyncio.run(_run_weekly())
    else:
        asyncio.run(main(once="--once" in sys.argv))
