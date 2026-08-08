import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from database import Listing

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self) -> None:
        self._bot = Bot(token=settings.telegram_bot_token)
        self._chat_id = settings.telegram_chat_id

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def send_new_listing(self, listing: Listing, opportunity=None) -> None:
        lines = []
        if opportunity:
            pct = round(opportunity.discount_pct * 100)
            lines.append(
                f"🚨 *FIRSAT İLANI* — bölge medyanının %{pct} altında\\!"
            )
        lines.append(f"🏠 *{_escape(listing.title)}*")
        lines.append(f"💰 {_escape(listing.price)}")
        lines.append(f"📍 {_escape(listing.location)}")
        room = listing.room_count or "?"
        area = f"{listing.area_m2} m²" if listing.area_m2 else "?"
        lines.append(f"🛏 {_escape(room)} \\| 📐 {_escape(area)}")
        if opportunity:
            b = opportunity.baseline
            lines.append(
                f"📊 Medyan: {_escape(f'{b.median:,.0f}')} TL/m² \\| "
                f"Ort: {_escape(f'{b.average:,.0f}')} TL/m² "
                f"\\({b.sample_size} ilan\\)"
            )
        lines.append(f"🔗 [İlana git]({listing.url})")

        await self._send("\n".join(lines))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def send_price_change(
        self,
        listing: Listing,
        old_price: str,
        direction: str,
        opportunity=None,
        price_drop_count: int = 1,
        price_chain: list[tuple[str, datetime | None]] | None = None,
    ) -> None:
        lines = []
        if direction == "drop":
            repeat = f" \\(bu satıcının {price_drop_count}\\. indirimi\\)" if price_drop_count > 1 else ""
            chain = price_chain or [(old_price, None), (listing.price, None)]
            chain_str = " → ".join(_format_step(price, at) for price, at in chain)
            lines.append(f"📉 *Fiyat Düştü\\!*{repeat} {chain_str}")
        else:
            lines.append(
                f"📈 *Fiyat Arttı* "
                f"Eski: {_escape(old_price)} → Yeni: {_escape(listing.price)}"
            )
        if opportunity:
            pct = round(opportunity.discount_pct * 100)
            lines.append(
                f"🚨 *FIRSAT İLANI* — bölge medyanının %{pct} altında\\!"
            )
        lines.append(f"🏠 {_escape(listing.title)}")
        lines.append(f"📍 {_escape(listing.location)}")
        room = listing.room_count or "?"
        area = f"{listing.area_m2} m²" if listing.area_m2 else "?"
        lines.append(f"🛏 {_escape(room)} \\| 📐 {_escape(area)}")
        if opportunity:
            b = opportunity.baseline
            lines.append(
                f"📊 Medyan: {_escape(f'{b.median:,.0f}')} TL/m² \\| "
                f"Ort: {_escape(f'{b.average:,.0f}')} TL/m² "
                f"\\({b.sample_size} ilan\\)"
            )
        lines.append(f"🔗 [İlana git]({listing.url})")

        await self._send("\n".join(lines))

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    async def send_weekly_report(self, text: str, image_bytes: bytes) -> None:
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.05)
        await self._bot.send_photo(chat_id=self._chat_id, photo=image_bytes)
        await asyncio.sleep(0.05)

    async def send_alert(self, text: str) -> None:
        await self._send(_escape(text))

    async def _send(self, text: str) -> None:
        await self._bot.send_message(
            chat_id=self._chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
            disable_web_page_preview=True,
        )
        await asyncio.sleep(0.05)  # Telegram rate-limit buffer


_MONTHS_TR = "Oca Şub Mar Nis May Haz Tem Ağu Eyl Eki Kas Ara".split()
_TR_TZ = ZoneInfo("Europe/Istanbul")


def _format_step(price: str, at: datetime | None) -> str:
    if at is None:
        return _escape(price)
    if at.tzinfo is None:  # Mongo naive UTC döner
        at = at.replace(tzinfo=timezone.utc)
    local = at.astimezone(_TR_TZ)
    return f"{_escape(price)} _\\({local.day} {_MONTHS_TR[local.month - 1]}\\)_"


def _escape(text: str) -> str:
    """Escape special chars for MarkdownV2."""
    special = r"\_*[]()~`>#+-=|{}.!"
    return "".join(f"\\{c}" if c in special else c for c in str(text))


if __name__ == "__main__":
    from datetime import timedelta
    logging.basicConfig(level=logging.INFO)

    now_utc = datetime.now(tz=timezone.utc)
    assert _format_step("5.000.000 TL", None) == "5\\.000\\.000 TL"
    assert _format_step("5.000.000 TL", datetime(2026, 8, 8, tzinfo=timezone.utc)).endswith(
        "_\\(8 Ağu\\)_"
    )

    async def _test():
        from analytics import Baseline, Opportunity
        notifier = TelegramNotifier()
        now = datetime.now(tz=timezone.utc)
        listing = Listing(
            listing_id="test-123",
            title="Test İlan — 3+1 Daire",
            price="4.600.000 TL",
            price_value=4_600_000,
            location="Maltepe / İdealtepe",
            room_count="3+1",
            area_m2=95,
            building_age=None,
            url="https://www.sahibinden.com/ilan/test-123",
            created_at=now,
            updated_at=now,
        )
        baseline = Baseline(median=57_000, average=59_000, sample_size=8)
        opportunity = Opportunity(discount_pct=0.20, baseline=baseline)

        print("→ send_new_listing (fırsat):")
        await notifier.send_new_listing(listing, opportunity)

        print("→ send_price_change (düşüş + fırsat):")
        await notifier.send_price_change(
            listing,
            old_price="5.000.000 TL",
            direction="drop",
            opportunity=opportunity,
            price_drop_count=3,
            price_chain=[
                ("5.400.000 TL", now_utc - timedelta(days=40)),
                ("5.200.000 TL", now_utc - timedelta(days=21)),
                ("5.000.000 TL", now_utc - timedelta(days=6)),
                ("4.600.000 TL", now_utc),
            ],
        )

        print("→ send_price_change (artış, fırsat yok):")
        await notifier.send_price_change(
            listing,
            old_price="4.200.000 TL",
            direction="rise",
            opportunity=None,
        )
        print("Tüm test mesajları gönderildi.")

    asyncio.run(_test())
