import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import nodriver as uc
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from config import CHROME_PROFILE_DIR
from database import Listing

logger = logging.getLogger(__name__)

_MAX_PAGES = 20  # sonsuz döngü güvencesi


def _build_page_url(base_url: str, offset: int) -> str:
    parsed = urlparse(base_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["pagingOffset"] = [str(offset)]
    new_query = urlencode(params, doseq=True)
    return parsed._replace(query=new_query).geturl()


def _paging_size(base_url: str) -> int:
    params = parse_qs(urlparse(base_url).query)
    val = params.get("pagingSize", ["50"])[0]
    return int(val) if val.isdigit() else 50


async def fetch_all_listings(base_url: str) -> list[Listing]:
    """Tüm sayfalardaki ilanları tek browser oturumunda çeker."""
    page_size = _paging_size(base_url)
    all_listings: list[Listing] = []

    browser = await uc.start(headless=False, user_data_dir=CHROME_PROFILE_DIR)
    try:
        for page_num in range(_MAX_PAGES):
            offset = page_num * page_size
            url = _build_page_url(base_url, offset)
            logger.info(f"sayfa {page_num + 1} — offset={offset} url={url}")

            tab = await browser.get(url)

            logger.info("listing tablosu bekleniyor (CF geçişi dahil, max 90s)…")
            try:
                await tab.find("tr.searchResultsItem", timeout=90)
            except Exception:
                logger.warning(f"sayfa {page_num + 1}: listing tablosu bulunamadı — duruyorum")
                break

            await asyncio.sleep(random.uniform(2, 4))
            html = await tab.get_content()
            page_listings, raw_row_count = _parse_page(html)
            all_listings.extend(page_listings)

            logger.info(
                f"sayfa {page_num + 1}: {len(page_listings)} ilan "
                f"({raw_row_count} ham satır), toplam={len(all_listings)}"
            )

            if raw_row_count < page_size:
                logger.info("son sayfa — tarama tamamlandı")
                break

            await asyncio.sleep(random.uniform(1, 3))
    finally:
        browser.stop()

    return all_listings


def _parse_page(html: str) -> tuple[list[Listing], int]:
    soup = BeautifulSoup(html, "lxml")
    all_rows = soup.select("tr.searchResultsItem")
    real_rows = [r for r in all_rows if r.get("data-id")]
    logger.info(f"found {len(real_rows)} listing rows ({len(all_rows)} total incl. promos)")

    listings: list[Listing] = []
    for row in real_rows:
        listing = _parse_row(row)
        if listing:
            listings.append(listing)
    return listings, len(all_rows)


def _parse_price(text: str) -> int:
    """'140.000 TL' → 140000  (tüm rakam karakterlerini birleştir, nokta/virgül ayırıcı)"""
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _parse_area(text: str) -> int | None:
    """'85 m²' veya '120 m2' → 85 / 120  (ilk ardışık rakam bloğu)"""
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _parse_row(row) -> Listing | None:
    try:
        # listing_id doğrudan <tr data-id="..."> attribute'undan
        listing_id = row.get("data-id")
        if not listing_id:
            return None

        title_el = row.select_one("a.classifiedTitle")
        if not title_el:
            return None

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = f"https://www.sahibinden.com{href}" if href.startswith("/") else href

        # fiyat <span> içinde: " 140.000 TL"
        price_el = row.select_one("td.searchResultsPriceValue span")
        price = price_el.get_text(strip=True) if price_el else ""
        price_value = _parse_price(price)

        location_el = row.select_one("td.searchResultsLocationValue")
        location = location_el.get_text(separator=" / ", strip=True) if location_el else ""

        # attr_cells[0] = alan m², attr_cells[1] = oda tipi (3+1 vb.)
        attr_cells = row.select("td.searchResultsAttributeValue")
        area_m2 = _parse_area(attr_cells[0].get_text(strip=True)) if len(attr_cells) > 0 else None
        room_count = attr_cells[1].get_text(strip=True) if len(attr_cells) > 1 else None

        now = datetime.now(tz=timezone.utc)
        return Listing(
            listing_id=listing_id,
            title=title,
            price=price,
            price_value=price_value,
            location=location,
            room_count=room_count or None,
            area_m2=area_m2,
            building_age=None,  # arama sayfasında yok
            url=url,
            created_at=now,
            updated_at=now,
        )
    except Exception as exc:
        logger.warning(f"failed to parse row: {exc}")
        return None


async def _setup_session() -> None:
    browser = await uc.start(
        headless=False,
        user_data_dir=CHROME_PROFILE_DIR,
    )
    await browser.get("https://www.sahibinden.com")
    print("\nTarayıcı açıldı. Login ol, CF challenge'ı geç.")
    print("Hazır olunca buraya dön ve Enter'a bas — session kaydedilir.\n")
    await asyncio.get_event_loop().run_in_executor(None, input)
    browser.stop()
    print("Session kaydedildi. Artık python scraper.py ile çalıştırabilirsin.")


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if "--setup" in sys.argv:
        asyncio.run(_setup_session())
    else:
        from config import SEARCH_URL
        target = sys.argv[1] if len(sys.argv) > 1 else SEARCH_URL
        results = asyncio.run(fetch_all_listings(target))
        for r in results:
            print(r.model_dump_json(indent=2))
        print(f"\nTotal: {len(results)}")
