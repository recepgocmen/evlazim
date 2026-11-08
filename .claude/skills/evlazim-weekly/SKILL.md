---
name: evlazim-weekly
description: evlazim (Sahibinden ilan takip botu) projesinin cuma/pazar koşusunu yönetir — koşunun olup olmadığını ve temiz bitip bitmediğini logtan doğrulayıp Türkçe rapor çıkarır, tam erişim varsa koşuyu kendisi başlatır. "evlazim haftalık", "evlazim çalıştır", "koşu oldu mu", "bu hafta tarama yapıldı mı", "evlazim log" gibi ifadelerde kullan. Cuma/pazar koşusunu kontrol eden zamanlanmış görev tetiklendiğinde de bunu kullan. evlazim projesinde bir şeyin çalışıp çalışmadığı sorulduğunda ilk buraya bak.
---

# evlazim — haftalık koşu

`evlazim`, Sahibinden ilanlarını tarayıp fiyat düşüşlerini Telegram'a bildiren
bir bot. Haftada iki kez, cuma ve pazar 22:00'de iki komut çalışıyor.

İki komutun **sırası anlamlı**: önce `--once` yeni veriyi çekiyor, sonra
`--weekly` haftanın raporunu bu taze veriyle çıkarıyor. Ters sırada rapor
o günün taramasını görmez ve "bu hafta ne oldu" sorusunu eksik cevaplar.

## Mimari — kim neyi çalıştırıyor

Koşuyu **launchd** çalıştırıyor, sen değil. Sebebi şu: komutlar MongoDB,
Telegram ve gerçek (headless olmayan) Chrome istiyor; uzak oturumdaki kabuk
bunların hiçbirine ulaşamıyor — yalnızca bağlı klasörü görüyor. launchd ise
Mac'in kendi oturumunda çalışıyor ve **Mac uykudaysa işi atlamıyor, uyanınca
çalıştırıyor.**

```
launchd (cuma+pazar 22:00)
  └── scripts/weekly-run.sh
        ├── .venv/bin/python main.py --once     → Chrome ile tarama + bildirim
        ├── (once başarısızsa burada durur)
        └── .venv/bin/python main.py --weekly   → Telegram'a haftalık rapor
        └── yazar: logs/run-<tarih>.log ve logs/last-run.json
```

Senin işin çoğu zaman **koşmak değil, koştuğunu doğrulamak.** Bu koşunun
sessizce bozulmasının klasik yolları: launchd hiç tetiklenmemiştir, `--once`
patlayıp `--weekly` atlanmıştır, ya da Chrome açılamadığı hâlde süreç sıfırla
çıkıp "başarılı" görünmüştür. Üçü de logta ayırt edilebilir.

## Mod A — kontrol ve rapor (varsayılan)

Uzak oturumdan yapabildiğin şey bu. Ağ gerektirmez, çünkü loglar bağlı
klasörün içinde.

1. Durum dosyasını oku:
   ```bash
   cat ~/mnt/evlazim/logs/last-run.json
   ```
2. **Tarihe bak.** `finished` bugünden çok geriyse koşu hiç tetiklenmemiş
   demektir — bu bir başarısızlıktır, sadece "eski kayıt" değil. Beklenen son
   koşu: en son geçen cuma ya da pazar 22:00.
3. `result` `yarida_kaldi` ise ilgili log dosyasının son 40 satırını oku:
   ```bash
   tail -40 ~/mnt/evlazim/logs/run-<tarih>.log
   ```
4. Raporu aşağıdaki formatta yaz.

Durum dosyası yoksa: script hiç çalışmamış. launchd yüklenmiş mi diye sorman
gerekir, tahmin etme.

## Mod B — elle çalıştırma (yalnızca tam erişimli ortamda)

Yerel Claude Code/Cursor gibi gerçek kabuk erişimin varsa koşuyu kendin
başlatabilirsin:

```bash
cd ~/evlazim && bash scripts/weekly-run.sh
```

Chrome açılıp sayfa gezdiği için uzun sürer; kabuk zaman aşımı olan bir
ortamdaysan arka planda başlat ve `logs/last-run.json` oluşana kadar yokla.
Komutları tek tek elle çalıştırma — script sıra ve doğrulama kurallarını
zaten içeriyor, elle çalıştırınca o kurallar devre dışı kalır.

**Uzak/ağsız oturumda Mod B'yi denemeye kalkma.** `.venv/bin/python` bağlı
klasörün dışına işaret ediyor ve ağ yok; komut "no such file" ya da `gaierror`
ile döner. Bu durumda doğru cevap koşuyu çalıştırmak değil, çalıştırılamadığını
söylemek.

## Rapor formatı

Her seferinde aynı yapı; boş kalacak bölümü "yok" yazmak yerine at:

```markdown
# evlazim koşusu — [tarih], [gün]

**Sonuç:** [tamamlandı / yarıda kaldı — hangi adımda / hiç çalışmadı]

## Komutlar
- `--once` → [tamamlandı / hata] — [saat] — [işlenen ilan, logta varsa]
- `--weekly` → [gönderildi / atlandı / hata] — [saat]

## Dikkat çeken çıktı
- [uyarı veya hata satırı, olduğu gibi]

## Ne yapmalı
- [somut tek adım — ör. "Mac uyanıkken `launchctl start com.rexho.evlazim.weekly`"]
```

## Her seferinde geçerli kurallar

- **Çıkış kodu tek başına yeterli değil.** Bu projede gönderim ve tarama
  hataları yakalanıp loga yazılıyor; başarıyı log satırından doğrula
  (`weekly report sent`, Chrome hata satırlarının yokluğu).
- **Eski log, koşu sayılmaz.** Tarihi kontrol etmeden "tamamlandı" deme.
- **Çıktıyı yorumlama, aktar.** Hata satırlarını kendi cümlelerinle özetleme,
  olduğu gibi yapıştır. Bir hatanın önemsiz olduğuna karar vermek senin işin
  değil.
- **`.env` içeriğini asla yazdırma, aktarma, loga taşıma.** Telegram token'ı
  ve MongoDB bağlantısı orada.
- **Git'e dokunma.** Bu skill koşuyu yapar ve raporlar; commit ve push ayrı
  bir karar, o karar Rexho'nun.
- **Koşu gerçek.** Elle tetiklerken Telegram'a gerçek mesaj gideceğini
  önceden söyle.
- **Türkçe raporla.**

## Bilinen tuzaklar

Bunlar tahmin değil, kurulum sırasında gerçekten yaşananlar:

- **`Operation not permitted` = macOS TCC izni, dosya hatası değil.** Proje
  `~/Desktop` altındayken launchd'nin başlattığı bash script'i okuyamıyordu;
  Terminal ve VS Code çalışıyordu çünkü onlara bu izin bir kez verilmiş.
  Çözüm projeyi TCC korumasız ev dizinine (`~/evlazim`) taşımak oldu. Log'da
  bu satırı görürsen ilk bakacağın yer projenin yolu.
- **Chrome, `nodriver` ile `headless=False` açılıyor** ve `Profile_Emlak`
  adlı ayrı bir Chrome profilini kullanıyor. Sahibinden'i aşan şey bu gerçek
  tarayıcı; VS Code'un veya Terminal'in bir katkısı yok. "VS Code'da
  çalışıyor" denirse asıl fark izinlerdedir, araçta değil.
- **Uzak oturumun kabuğu yalnızca bağlı klasörü görür.** `.venv/bin/python`
  ev dizininin dışındaki framework python'a işaret ediyor, ağ da yok. O yüzden
  koşuyu uzaktan çalıştırmak mümkün değil — Mod A bu yüzden var.

## Kurulum (bir kez)

launchd görevi `~/Library/LaunchAgents` altına kurulur — orası bağlı klasörün
dışında, o yüzden bu komutu Rexho kendi Terminal'inde çalıştırır:

```bash
cp ~/evlazim/scripts/com.rexho.evlazim.weekly.plist ~/Library/LaunchAgents/ && \
launchctl unload ~/Library/LaunchAgents/com.rexho.evlazim.weekly.plist 2>/dev/null; \
launchctl load ~/Library/LaunchAgents/com.rexho.evlazim.weekly.plist && \
launchctl list | grep evlazim
```

Elle test: `launchctl start com.rexho.evlazim.weekly`
Kaldırmak: `launchctl unload ~/Library/LaunchAgents/com.rexho.evlazim.weekly.plist`
