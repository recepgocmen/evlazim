from pydantic_settings import BaseSettings, SettingsConfigDict


SEARCH_URL = "https://www.sahibinden.com/satilik-daire?pagingSize=50&a812=40606&a812=1297863&a812=1297864&a812=1297865&a812=40605&a812=40604&a812=40603&a812=40602&address_region=2&a107889_min=80&address_district=2172&address_district=2171&address_town=446&price_max=11000000" # custom filtreleme idealtepe +80m2 0-10 yaş

SCAN_INTERVAL_MIN = 30

OPPORTUNITY_THRESHOLD = 0.15  # bölge medyanının %15+ altındaki ilanlar fırsat
MIN_COMPARABLE_LISTINGS = 5   # fırsat tespiti için gereken minimum örnek sayısı

CHROME_PROFILE_DIR = "/Users/recepgocmen/Library/Application Support/Google/Chrome/Profile_Emlak"


class Settings(BaseSettings):
    mongo_uri: str
    mongo_db_name: str = "realestate_hunter"
    telegram_bot_token: str
    telegram_chat_id: str

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
