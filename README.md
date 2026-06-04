# 🏠 evlazim

Sahibinden'de ilan takibi yapan, fiyat düşüşlerini yakalayan ve Telegram'a bildirim atan bir bot. Ev arıyorsun, ekranı sürekli yenilemeye gerek yok.

---

## 🔍 Ne yapıyor?

- Belirlediğin Sahibinden arama URL'sini manuel tetikleyerek tarar.
- Tüm ilanları MongoDB'ye kaydeder, fiyat geçmişini tutar
- İlk çalıştırmada sadece veri doldurur, bildirim atmaz
- Sonraki taramalarda şunlar için Telegram mesajı atar:
  - 🆕 **Yeni ilan** — eğer bölge medyanının %15+ altındaysa 🚨 fırsat rozeti de gelir
  - 📉 **Fiyat düştü** / 📈 **Fiyat arttı** — fiyat düşüşlerinde yine fırsat analizi yapar
- Fırsat analizi: ilanın fiyat/m²'sini aynı konum + oda tipindeki benzer ilanların medyanıyla kıyaslar (en az 5 örnek lazım)

---

## 🗂️ Dosyalar

```
main.py       → zamanlayıcı, tüm akışı yönetir
scraper.py    → Chrome otomasyonu + HTML parse
database.py   → MongoDB modelleri, upsert, fiyat geçmişi
analytics.py  → medyan hesabı, fırsat tespiti
notifier.py   → Telegram mesajları
config.py     → ayarlar, URL, eşikler
```

---

## ⚙️ Gereksinimler

- 🐍 Python 3.10+
- 🌐 Google Chrome yüklü (gerçek tarayıcı kullanıyor, headless değil)
- 🍃 MongoDB Atlas hesabı (ücretsiz tier yeterli)
- 💬 Telegram bot token + chat ID

---

## 🚀 Kurulum

**1. Repo'yu klonla, venv oluştur**

```bash
git clone <repo-url>
cd evlazim
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**2. `.env` dosyasını oluştur**

```env
MONGO_URI=mongodb+srv://<kullanici>:<sifre>@<cluster>.mongodb.net/
MONGO_DB_NAME=realestate_hunter
TELEGRAM_BOT_TOKEN=<bot-token>
TELEGRAM_CHAT_ID=<chat-id>
```

**3. 🔐 Chrome session kaydet (bir kere yapılır)**

Sahibinden Cloudflare kullanıyor. Bunu geçmek için kayıtlı bir Chrome profili lazım:

```bash
python scraper.py --setup
```

Chrome açılır, giriş yap / CAPTCHA'yı geç, sonra terminale dön ve Enter'a bas. Session kaydedilir. `config.py` içindeki `CHROME_PROFILE_DIR`'ı kendi path'ine göre güncelle.

**4. 🔗 Arama URL'ini ayarla**

`config.py` içindeki `SEARCH_URL`'yi Sahibinden'de filtrelerini ayarladıktan sonra kopyaladığın URL ile değiştir.

---

## ▶️ Çalıştırma

**Sürekli mod** (her 30 dakikada bir):

```bash
python main.py
```

**Tek seferlik çalıştır** (test için):

```bash
python main.py --once
```

**Sadece scrape, DB/bildirim yok** (JSON çıktısı verir):

```bash
python scraper.py
```

**Telegram mesajlarını test et:**

```bash
python notifier.py
```

---

## 🎛️ Ayarlar

`config.py` içinde değiştirebileceklerin:

| Değişken | Varsayılan | Ne işe yarar |
|---|---|---|
| `SEARCH_URL` | — | Sahibinden filtreleme URL'i |
| `SCAN_INTERVAL_MIN` | `30` | Taramalar arası süre (dakika) |
| `OPPORTUNITY_THRESHOLD` | `0.15` | Fırsat sayılması için medyan altı oran (%15) |
| `MIN_COMPARABLE_LISTINGS` | `5` | Fırsat analizi için gereken minimum ilan sayısı |
| `CHROME_PROFILE_DIR` | macOS path | Session'ın saklandığı Chrome profil dizini |

---

## 📲 Telegram mesaj örnekleri

**🚨 Fırsat ilanı:**
```
🚨 FIRSAT İLANI — bölge medyanının %20 altında!
🏠 Maltepe İdealtepe 3+1 Satılık Daire
💰 4.600.000 TL
📍 Maltepe / İdealtepe
🛏 3+1 | 📐 95 m²
📊 Medyan: 57,000 TL/m² | Ort: 59,000 TL/m² (8 ilan)
🔗 İlana git
```

**📉 Fiyat düşüşü:**
```
📉 Fiyat Düştü! Eski: 5.000.000 TL → Yeni: 4.600.000 TL
🏠 Maltepe İdealtepe 3+1 Satılık Daire
📍 Maltepe / İdealtepe
🛏 3+1 | 📐 95 m²
🔗 İlana git
```

---

## ⚠️ Bilmeni gerekenler

- Tarayıcı gerçekten açılıyor, arka planda görünür bir Chrome penceresi çıkıyor. Tarama sırasında kapatma.
- Session geçerliliğini yitirirse (Cloudflare engeli, 0 ilan dönüyorsa) `--setup` ile yeniden kaydet.
- Her ilanın fiyat geçmişi MongoDB'de tutuluyor, istersen sonradan sorgulanabilir.
