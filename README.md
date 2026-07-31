# Trading bot (paper-trade)

Bu repo basit bir paper-trading bot iskeleti içerir:
- Binance (ccxt) ve Yahoo Finance (yfinance) üzerinden veri çeker
- EMA, RSI, ATR gibi göstergeler hesaplar
- Basit EMA crossover + RSI filtresi ile sinyal üretir
- Paper trade gerçekleştirir (wallet.json ve trades_log.csv güncellenir)
- Telegram ile bildirim gönderir
- Bir scheduler ile periyodik çalışır

Hızlı başlangıç
1. Klonla:
   git clone https://github.com/erceliksari-hash/trading-bot-paper.git
   cd trading-bot-paper

2. Sanal ortam oluştur ve bağımlılıkları yükle:
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt

3. Ortam değişkenleri (önerilir):
   export TELEGRAM_TOKEN="your_token"
   export TELEGRAM_CHAT_ID="your_chat_id"

   veya config.json içindeki alanları doldurabilirsiniz (token'ları çevre değişkeninde tutmak daha güvenlidir).

4. Örnek wallet oluştur:
   {
     "USD": 10000.0
   }
   wallet.json dosyası yoksa bot başlangıçta bu değeri kullanır.

5. Botu çalıştır:
   python trading_bot.py

Docker:
   docker build -t trading-bot .
   docker run -e TELEGRAM_TOKEN=... -e TELEGRAM_CHAT_ID=... trading-bot

systemd:
  Repo içindeki örnek systemd dosyasını systemd/trading-bot.service olarak kullanabilirsiniz. /path/to/... alanlarını sunucunuza göre düzenleyin.

Uyarılar:
- Bu proje paper-trading içindir. Gerçek API anahtarlarıyla gerçek işlemler yapmayın veya gerçek işlemler yapmak için ek güvenlik/limit kontrolleri ekleyin.
- config.json içindeki asset listesini güncelleyebilirsiniz. Crypto sembolleri örn: "BTC/USDT", hisse senetleri için "AAPL" veya BIST için "GARAN.IS".
