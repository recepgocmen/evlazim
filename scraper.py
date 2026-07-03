import asyncio
import logging
import random
import re
import sys
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlparse

import nodriver as uc
from bs4 import BeautifulSoup
from tenacity import retry, stop_after_attempt, wait_exponential

from config import CHROME_PROFILE_DIR, SEARCH_URL
from database import Listing

logger = logging.getLogger(__name__)

_CDP_PORT = 9227  # sabit port; nodriver'a "zaten hazir" diye baglaniriz


class SessionExpiredError(Exception):
    """Sahibinden oturumu dolmus; yeniden giris gerekiyor."""


_MAX_PAGES = 20

# Standalone "var" veya "son X gun" iceren basliklar suphelidir
_SUSPICIOUS_RE = re.compile(r"\bvar\b|son \d+ g[uü]n", re.IGNORECASE)


def is_suspicious_title(title: str) -> bool:
    return bool(_SUSPICIOUS_RE.search(title))


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


def _is_login_page(tab_url: str, html_lower: str) -> bool:
    if "giris" in tab_url.lower() or "/login" in tab_url.lower():
        return True
    return ("e-posta" in html_lower or "e-mail" in html_lower) and (
        "giris" in html_lower or "sifre" in html_lower or "password" in html_lower
    )


async def _get_page_html(tab, timeout: int = 90) -> str:
    """tab.find exception atsa da HTML'i mutlaka dondur.
    Login sayfasi tespit edilirse 90s beklemeden erken cikis yapar."""
    await asyncio.sleep(3)
    quick_html = await tab.get_content()
    if _is_login_page(tab.url, quick_html.lower()):
        return quick_html  # 90s bekleme, hemen don
    try:
        await tab.find("tr.searchResultsItem", timeout=timeout)
    except Exception:
        pass
    await asyncio.sleep(random.uniform(2, 4))
    return await tab.get_content()


def _kill_stale_chrome() -> None:
    import subprocess, time
    result = subprocess.run(
        ["pgrep", "-f", CHROME_PROFILE_DIR], capture_output=True, text=True
    )
    pids = result.stdout.strip().split()
    if pids:
        logger.info(f"stale Chrome process'leri temizleniyor: pids={pids}")
        subprocess.run(["pkill", "-f", CHROME_PROFILE_DIR])
        time.sleep(2)


async def _start_browser():
    """Chrome'u kendimiz baslatip CDP hazir olana kadar poll eder,
    sonra nodriver'i o porta baglar (nodriver Chrome baslatmaz)."""
    import subprocess
    import urllib.request

    _kill_stale_chrome()

    chrome_proc = subprocess.Popen(
        [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            f"--remote-debugging-port={_CDP_PORT}",
            f"--user-data-dir={CHROME_PROFILE_DIR}",
            "--remote-debugging-host=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--no-pings",
            "--disable-dev-shm-usage",
            "--disable-session-crashed-bubble",
            "--disable-infobars",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    logger.info(f"Chrome baslatildi (pid={chrome_proc.pid}), CDP bekleniyor...")

    cdp_url = f"http://127.0.0.1:{_CDP_PORT}/json/version"
    for attempt in range(20):  # max 10s
        await asyncio.sleep(0.5)
        try:
            urllib.request.urlopen(cdp_url, timeout=1)
            logger.info(f"CDP hazir ({(attempt + 1) * 0.5:.1f}s)")
            break
        except Exception:
            pass
    else:
        chrome_proc.kill()
        raise RuntimeError("Chrome 10s icinde CDP'ye cevap vermedi")

    # host+port verilince nodriver Chrome baslatmaz, direkt baglanir
    return await uc.start(host="127.0.0.1", port=_CDP_PORT)


async def fetch_all_listings(base_url: str) -> list[Listing]:
    """Tum sayfalardaki ilanlari tek browser oturumunda ceker."""
    page_size = _paging_size(base_url)
    all_listings: list[Listing] = []

    _kill_stale_chrome()
    browser = await _start_browser()
    try:
        for page_num in range(_MAX_PAGES):
            offset = page_num * page_size
            url = _build_page_url(base_url, offset)
            logger.info(f"sayfa {page_num + 1} — offset={offset} url={url}")

            tab = await browser.get(url)
            logger.info("listing tablosu bekleniyor (CF gecisi dahil, max 90s)...")
            html = await _get_page_html(tab)
            page_listings, raw_row_count = _parse_page(html)

            if raw_row_count == 0:
                if page_num == 0:
                    # Ilk sayfada 0 satirr -> oturum dolmus veya CF engeli
                    if sys.stdin.isatty():
                        print(
                            "\n*** Sahibinden oturumu dolmus veya erisim engeli. ***\n"
                            "Tarayicida giris yap (gerekirse CF challenge'i gec),\n"
                            "bitince buraya don ve Enter'a bas -- sure siniri yok.\n"
                        )
                        await asyncio.get_event_loop().run_in_executor(None, input)
                        tab = await browser.get(url)
                        html = await _get_page_html(tab)
                        page_listings, raw_row_count = _parse_page(html)
                        if raw_row_count == 0:
                            logger.warning("giris sonrasi hala 0 ilan -- duruyorum")
                            break
                    else:
                        raise SessionExpiredError(
                            "oturum suresi doldu -- python scraper.py --setup ile giris yenile"
                        )
                else:
                    logger.info(f"sayfa {page_num + 1}: 0 satir -- son sayfa")
                    break

            all_listings.extend(page_listings)
            logger.info(
                f"sayfa {page_num + 1}: {len(page_listings)} ilan "
                f"({raw_row_count} ham satir), toplam={len(all_listings)}"
            )

            if raw_row_count < page_size:
                logger.info("son sayfa -- tarama tamamlandi")
                break

            await asyncio.sleep(random.uniform(1, 3))
    finally:
        browser.stop()
        await asyncio.sleep(1)  # nodriver aclose() task'inin calismasi icin

    return all_listings


async def verify_listings_live(urls: list[str]) -> dict[str, bool]:
    """Supheligi tespit edilen ilanlarin detay sayfasini kontrol eder.
    Erisim basarisiz olursa fail-open (True) doner."""
    if not urls:
        return {}

    live: dict[str, bool] = {}
    _kill_stale_chrome()
    browser = await _start_browser()
    try:
        for url in urls:
            try:
                tab = await browser.get(url)
                await asyncio.sleep(3)
                html = (await tab.get_content()).lower()
                dead = (
                    "aradığınız sayfa bulunamıyor" in html
                    or "aradiginiz sayfa bulunamiyor" in html
                    or "yayindan kaldirildi" in html
                    or "yayından kaldırıldı" in html
                )
                live[url] = not dead
                logger.info(f"verify {'CANLI' if live[url] else 'OLU'}: {url}")
            except Exception as exc:
                logger.warning(f"verify hatasi, fail-open: {url} -- {exc}")
                live[url] = True
            await asyncio.sleep(random.uniform(2, 4))
    finally:
        browser.stop()
        await asyncio.sleep(1)

    return live


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
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else 0


def _parse_area(text: str) -> int | None:
    m = re.search(r"\d+", text)
    return int(m.group()) if m else None


def _parse_row(row) -> Listing | None:
    try:
        listing_id = row.get("data-id")
        if not listing_id:
            return None

        title_el = row.select_one("a.classifiedTitle")
        if not title_el:
            return None

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = f"https://www.sahibinden.com{href}" if href.startswith("/") else href

        price_el = row.select_one("td.searchResultsPriceValue span")
        price = price_el.get_text(strip=True) if price_el else ""
        price_value = _parse_price(price)

        location_el = row.select_one("td.searchResultsLocationValue")
        location = location_el.get_text(separator=" / ", strip=True) if location_el else ""

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
            building_age=None,
            url=url,
            created_at=now,
            updated_at=now,
        )
    except Exception as exc:
        logger.warning(f"failed to parse row: {exc}")
        return None


async def _setup_session() -> None:
    browser = await uc.start(headless=False, user_data_dir=CHROME_PROFILE_DIR)
    await browser.get("https://www.sahibinden.com")
    print("\nTarayici acildi. Login ol, CF challenge'i gec.")
    print("Hazir olunca buraya don ve Enter'a bas -- sure siniri yok.\n")
    await asyncio.get_event_loop().run_in_executor(None, input)

    print("Giris dogrulanıyor...")
    tab = await browser.get(SEARCH_URL)
    html = await _get_page_html(tab, timeout=30)
    _, raw_row_count = _parse_page(html)
    if raw_row_count > 0:
        print("Giris basarili. Session kaydedildi.")
    else:
        print("UYARI: Listing tablosu bulunamadi -- oturumun acik olduğunu kontrol et.")

    browser.stop()
    await asyncio.sleep(1)
    print("Artik python main.py --once ile calistirabilisin.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    if "--setup" in sys.argv:
        asyncio.run(_setup_session())
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else SEARCH_URL
        results = asyncio.run(fetch_all_listings(target))
        for r in results:
            print(r.model_dump_json(indent=2))
        print(f"\nTotal: {len(results)}")
