#!/bin/bash
# evlazim haftalık koşu
# launchd tarafından cuma ve pazar 22:00'de çalıştırılır.
# Elle denemek için: bash scripts/weekly-run.sh
#
# Sıra anlamlı: önce --once (yeni veriyi çek), sonra --weekly (haftalık
# raporu taze veriyle çıkar). --once başarısızsa --weekly ÇALIŞMAZ; eski
# veriyle rapor göndermek, hiç göndermemekten daha kafa karıştırıcı.

set -u

PROJECT="/Users/recepgocmen/evlazim"
PY="$PROJECT/.venv/bin/python"
LOGDIR="$PROJECT/logs"
mkdir -p "$LOGDIR"

STAMP=$(date +%Y-%m-%d_%H%M)
LOG="$LOGDIR/run-$STAMP.log"
STATUS="$LOGDIR/last-run.json"

cd "$PROJECT" || { echo "proje dizini yok: $PROJECT"; exit 1; }

STARTED=$(date +%Y-%m-%dT%H:%M:%S)
{
  echo "=== evlazim koşusu — $STARTED ==="
  echo "python: $PY"
} >> "$LOG"

WEEKLY_OK=false
ONCE_OK=false
FAILED_STEP=""
LISTINGS=""

# --- adım 1: tek tur tarama ---------------------------------------------
# Mac uykudan uyanıp koşuyu geç başlattığında DNS henüz ayakta olmuyor ve
# mongodb+srv adresi "no nameservers" ile patlıyor (18.09) — 60 sn arayla 3 deneme.
# Zaman aşımı da şart: 13.09'da tarama Cloudflare beklerken asılıp 4 gün öyle kaldı.
ONCE_TIMEOUT=1200
ONCE_RETRIES=3

run_once() {  # --once'u zaman aşımlı çalıştırır; asılırsa öldürüp 124 döner
  "$PY" main.py --once > "$O_LOG" 2>&1 &
  local pid=$! waited=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$waited" -ge "$ONCE_TIMEOUT" ]; then
      kill "$pid" 2>/dev/null
      echo "once: ${ONCE_TIMEOUT}sn zaman asimi -- surec oldurudu" >> "$LOG"
      return 124
    fi
    sleep 10
    waited=$((waited + 10))
  done
  wait "$pid"
}

for attempt in $(seq 1 "$ONCE_RETRIES"); do
  O_LOG=$(mktemp)
  echo "--- adım 1: --once (deneme $attempt/$ONCE_RETRIES) ---" >> "$LOG"
  if run_once; then
    # Chrome açılamadıysa süreç 0 ile çıkmış olsa bile tarama yapılmamıştır.
    if grep -qiE "chrome not reachable|session not created|WebDriverException|no such window" "$O_LOG"; then
      FAILED_STEP="once: Chrome açılamadı — tarama yapılmadı"
    # Sahibinden engeli (olagan-disi-kullanim) yakalanıp 0 ile çıkıyor; tekrar denemek
    # engeli uzatır, o yüzden retry yok.
    elif ! grep -q "scrape done:" "$O_LOG"; then
      FAILED_STEP="once: tarama yapılmadı (oturum/engel — log'da 'scrape done' yok)"
      cat "$O_LOG" >> "$LOG"; rm -f "$O_LOG"
      break
    else
      ONCE_OK=true
      FAILED_STEP=""
      LISTINGS=$(grep -oE "scrape done: total=[0-9]+" "$O_LOG" | tail -1 | grep -oE "[0-9]+")
    fi
  else
    FAILED_STEP="once: komut hata verdi (deneme $attempt, çıkış kodu sıfır değil)"
  fi
  cat "$O_LOG" >> "$LOG"; rm -f "$O_LOG"
  [ "$ONCE_OK" = true ] && break
  [ "$attempt" -lt "$ONCE_RETRIES" ] && sleep 60
done

# --- adım 2: haftalık rapor ---------------------------------------------
if [ "$ONCE_OK" = true ]; then
  W_LOG=$(mktemp)
  echo "--- adım 2: --weekly ---" >> "$LOG"
  if "$PY" main.py --weekly > "$W_LOG" 2>&1; then
    # Çıkış kodu tek başına yeterli değil: gönderim hatası yakalanıp log'a
    # yazılıyor ve program yine 0 ile çıkabiliyor. Başarıyı log satırından doğrula.
    if grep -q "weekly report sent" "$W_LOG"; then
      WEEKLY_OK=true
    else
      FAILED_STEP="weekly: rapor gönderilemedi (log'da 'weekly report sent' yok)"
    fi
  else
    FAILED_STEP="weekly: komut hata verdi (çıkış kodu sıfır değil)"
  fi
  cat "$W_LOG" >> "$LOG"; rm -f "$W_LOG"
else
  echo "--- adım 2 atlandı: --once başarısız ---" >> "$LOG"
fi

# --- durum dosyası ------------------------------------------------------
FINISHED=$(date +%Y-%m-%dT%H:%M:%S)
if [ "$ONCE_OK" = true ] && [ "$WEEKLY_OK" = true ]; then
  RESULT="tamamlandi"
else
  RESULT="yarida_kaldi"
fi

printf '{\n  "started": "%s",\n  "finished": "%s",\n  "result": "%s",\n  "weekly_ok": %s,\n  "once_ok": %s,\n  "failed_step": "%s",\n  "listings": "%s",\n  "log": "%s"\n}\n' \
  "$STARTED" "$FINISHED" "$RESULT" "$WEEKLY_OK" "$ONCE_OK" "$FAILED_STEP" "$LISTINGS" "$LOG" > "$STATUS"

echo "=== bitti: $RESULT ($FINISHED) ===" >> "$LOG"

# Eski log'ları biriktirme: son 20 koşu yeter.
ls -1t "$LOGDIR"/run-*.log 2>/dev/null | tail -n +21 | while read -r old; do rm -f "$old"; done

[ "$RESULT" = "tamamlandi" ] && exit 0 || exit 1
