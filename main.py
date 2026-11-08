import asyncio
import html
import logging
import re
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from analytics import (
    compute_baseline,
    evaluate_opportunity,
    fetch_usd_try,
    find_relist,
    generate_weekly_report,
    record_snapshot,
)
from config import NEW_LISTING_FLOOD_LIMIT, PRICE_MAX_USD, SCAN_INTERVAL_MIN, SEARCH_URL, settings
from database import close as db_close, get_db, init_indexes, mark_missing_listings, upsert_listing
from notifier import TelegramNotifier
from scraper import SessionExpiredError, fetch_all_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# httpx INFO satırı istek URL'sini, yani Telegram bot token'ını loga yazıyor
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

notifier = TelegramNotifier()


async def _search_url() -> str:
    """Fiyat tavanı dolar bazlı sabit: TL karşılığı her taramada güncel kurla hesaplanır."""
    rate = await fetch_usd_try()
    if not rate:  # API düştüyse son bilinen kur; tavan geri 15M'ye düşüp ilanları "kalktı" saymasın
        last = await get_db().weekly_metrics.find_one(
            {"usd_try": {"$gt": 0}}, sort=[("recorded_at", -1)]
        )
        if not last:
            return SEARCH_URL
        rate = last["usd_try"]
    price_max = int(round(PRICE_MAX_USD * rate, -5))
    logger.info(f"fiyat tavani: ${PRICE_MAX_USD:,} x {rate:.2f} = {price_max:,} TL")
    return re.sub(r"price_max=\d+", f"price_max={price_max}", SEARCH_URL)


async def scrape_job() -> None:
    url = await _search_url()
    logger.info(f"scrape job starting — url={url}")
    db = get_db()

    try:
        listings = await fetch_all_listings(url)
    except SessionExpiredError as exc:
        logger.error(f"oturum doldu: {exc}")
        try:
            await notifier.send_alert(f"Sahibinden taramasi yapilamadi: {exc}")
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
    relists: dict[str, dict] = {}  # listing_id -> kalkmış önceki ilan

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
            relist = await find_relist(listing)
            if relist:
                relists[listing.listing_id] = relist
                await db.listings.update_one(
                    {"listing_id": listing.listing_id},
                    {"$set": {"relist_of": relist["listing_id"]}},
                )

        elif result.price_changed:
            opportunity = None
            if result.direction == "drop":
                baseline = await compute_baseline(
                    listing.location, listing.room_count, listing.listing_id
                )
                if baseline:
                    opportunity = evaluate_opportunity(listing, baseline)
            candidates.append((result, listing, opportunity))

    # Filtre genisletildiginde eski ilanlar toptan "yeni" gorunuyor; 286 mesaj atmak yerine tek ozet
    new_candidates = [c for c in candidates if c[0].is_new]
    if len(new_candidates) > NEW_LISTING_FLOOD_LIMIT:
        logger.warning(f"{len(new_candidates)} yeni ilan — tek tek bildirim atlandi")
        await notifier.send_alert(
            f"{len(new_candidates)} yeni ilan geldi (limit {NEW_LISTING_FLOOD_LIMIT}). "
            "Filtre degismis olabilir; tek tek bildirilmedi, DB'ye yazildi."
        )
        candidates = [c for c in candidates if not c[0].is_new]
        new_count = len(new_candidates)

    for result, listing, opportunity in candidates:
        try:
            if result.is_new:
                await notifier.send_new_listing(
                    listing, opportunity, relists.get(listing.listing_id)
                )
                new_count += 1
            elif result.price_changed:
                await notifier.send_price_change(
                    listing,
                    old_price=result.old_price,
                    direction=result.direction,
                    opportunity=opportunity,
                    price_drop_count=result.price_drop_count,
                    price_chain=result.price_chain,
                )
                changed_count += 1
        except Exception as exc:
            logger.error(f"failed to notify listing_id={listing.listing_id}: {exc}")

    seen_ids = {l.listing_id for l in listings}
    if not is_first_run:
        removed = await mark_missing_listings(seen_ids)
        if removed:
            logger.info(f"marked {len(removed)} listings as removed/sold")

    await record_snapshot()  # her taramada trend grafiği için veri noktası

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
    usd_line = (
        f"• <b>Dolar Bazlı Medyan:</b> "
        f"${report.median_price_per_m2 / report.usd_try:,.0f} / m² "
        f"<i>(kur: {report.usd_try:.2f} TL)</i>\n"
        if report.usd_try
        else "• <b>Dolar Bazlı Medyan:</b> — <i>(kur alınamadı)</i>\n"
    )
    weeks_of_stock = (
        f"{report.sample_size / report.removed_count:.0f} Hafta"
        if report.removed_count
        else "—"
    )
    rate = report.usd_try or 1
    unit = "$" if report.usd_try else "TL "
    segments = "".join(
        f"• <b>{html.escape(room)}:</b> {count} ilan — {unit}{ppm2 / rate:,.0f} / m²\n"
        for room, count, ppm2 in report.segments
    ) or "• —\n"
    targets = "".join(
        f"• <a href=\"{html.escape(t['url'])}\">{html.escape(t['title'][:60])}</a> — "
        f"{html.escape(t['price'])}, {t['days']} gün, {t['drops']} indirim (%{t['cut_pct']:.0f})\n"
        for t in report.negotiation_targets
    ) or "• Şu an kriteri karşılayan ilan yok\n"
    return (
        f"🏠 <b>EV LAZIM — HAFTALIK EMLAK PİYASASI ANALİZİ</b>\n"
        f"📅 <i>Rapor Tarihi: {now_str}</i>\n"
        f"📍 <i>Bölge: İstanbul / Maltepe &amp; İdealtepe</i>\n"
        f"\n"
        f"📈 <b>1. BÖLGESEL BİRİM FİYAT TRENDİ</b>\n"
        f"• <b>Ortalama m² Fiyatı:</b> {report.avg_price_per_m2:,.0f} TL / m²\n"
        f"• <b>Medyan / Min–Max:</b> {report.median_price_per_m2:,.0f} TL / m² "
        f"({report.min_price_per_m2:,.0f} – {report.max_price_per_m2:,.0f})\n"
        f"{usd_line}"
        f"• <b>Geçen Haftaya Göre Değişim:</b> {_wow(report.price_wow_pct)}\n"
        f"💡 <i>Aktif ilanların ağırlıklı ortalama birim fiyatı.</i>\n"
        f"\n"
        f"⏱️ <b>2. PİYASA LİKİDİTESİ &amp; SATIŞ HIZI</b>\n"
        f"• <b>Ortalama İlanda Kalma Süresi:</b> {days} Gün\n"
        f"• <b>Bu Hafta İlandan Kalkan İlan Sayısı:</b> {report.removed_count} Adet\n"
        f"• <b>Stok Erime Süresi:</b> {weeks_of_stock}\n"
        f"💡 <i>30 günün altındaki süreler piyasanın canlı olduğunu gösterir. "
        f"Erime süresi uzadıkça alıcının eli güçlenir.</i>\n"
        f"\n"
        f"📦 <b>3. ARZ &amp; STOK DURUMU</b>\n"
        f"• <b>Aktif İlan Sayısı:</b> {report.sample_size} İlan\n"
        f"• <b>Bu Hafta Eklenen / Kalkan:</b> +{report.new_count} / -{report.removed_count} "
        f"(Net: {report.net_inventory_change:+d})\n"
        f"• <b>Geçen Haftaya Göre Değişim:</b> {_wow(report.inventory_wow_pct)}\n"
        f"💡 <i>Stok azalması talebin yüksek olduğunu gösterir.</i>\n"
        f"\n"
        f"🤝 <b>4. SATICI DAVRANIŞI &amp; PAZARLIK ESNEKLİĞİ</b>\n"
        f"• <b>Fiyat Düşüren Satıcı Oranı:</b> %{report.drop_ratio * 100:.1f} "
        f"({report.dropped_count} ilan)\n"
        f"• <b>Ortalama Yapılan İndirim Tutarı:</b> {discount}\n"
        f"• <b>İlk İndirime Kadar Medyan Süre:</b> {_opt(report.median_days_to_first_cut, '.0f', ' Gün')}\n"
        f"• <b>İndirim Yapanlarda Medyan Toplam İndirim:</b> %{_opt(report.median_total_cut_pct, '.1f')}\n"
        f"• <b>Kalkan İlanlarda İndirim Yapmış Oranı (30g):</b> "
        f"%{_opt(report.removed_with_cut_ratio and report.removed_with_cut_ratio * 100, '.0f')}\n"
        f"• <b>Bu Hafta Tekrar İlana Çıkan:</b> {report.relist_count} Adet\n"
        f"💡 <i>Bu oranın yükselmesi satıcıların pazarlığa daha açık olduğunu gösterir.</i>\n"
        f"\n"
        f"🛏 <b>5. ODA SAYISINA GÖRE MEDYAN</b>\n"
        f"{segments}"
        f"\n"
        f"🎯 <b>6. PAZARLIK HEDEFLERİ</b> <i>(60+ gün, 2+ indirim)</i>\n"
        f"{targets}"
        f"\n"
        f"🩺 <i>Son 7 günde başarılı tarama: {report.scan_count}/2</i>\n"
        f"📊 <i>TL/m² – USD/m² trendi, stok ve ilanda kalma süresi grafikleri ektedir.</i>"
    )


def _opt(x: float | None, spec: str, suffix: str = "") -> str:
    return f"{x:{spec}}{suffix}" if x is not None else "—"


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
