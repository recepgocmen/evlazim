from datetime import date

from pydantic_settings import BaseSettings, SettingsConfigDict


SEARCH_URL = "https://www.sahibinden.com/satilik-daire?a24_max=140&pagingSize=50&a812=40606&a812=1297863&a812=1297864&a812=1297865&a812=40605&a812=40604&a812=40603&a812=40602&address_region=2&sorting=price_desc&a107889_min=80&address_district=2172&address_district=2171&address_town=446&price_max=15000000&a107889_max=120" # custom filtreleme idealtepe +80m2 0-10 max 15M yaş
# Grafikte bu tarihe dikey çizgi çizilir: sıçrama piyasa değil, örneklem değişimi.
FILTER_CHANGED_ON = date(2026, 9, 18)
# SEARCH_URL'deki price_max yalnızca kur alınamazsa kullanılır; normalde bu tavan × güncel kur.
# 307.500$ = filtrenin kurulduğu 18.09.2026'da 15M TL (kur 48.785).
PRICE_MAX_USD = 307_500

SCAN_INTERVAL_MIN = 30

OPPORTUNITY_THRESHOLD = 0.15  # bölge medyanının %15+ altındaki ilanlar fırsat
MIN_COMPARABLE_LISTINGS = 5   # fırsat tespiti için gereken minimum örnek sayısı
NEW_LISTING_FLOOD_LIMIT = 30  # bir taramada bundan fazla yeni ilan = filtre değişmiş, tek tek bildirme

CHROME_PROFILE_DIR = "/Users/recepgocmen/Library/Application Support/Google/Chrome/Profile_Emlak"


class Settings(BaseSettings):
    mongo_uri: str
    mongo_db_name: str = "realestate_hunter"
    telegram_bot_token: str
    telegram_chat_id: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
