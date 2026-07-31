# trading-bot-paper

Bu repository, Telegram üzerinden bildirim veren ve paper-trading (sanal portföy) destekli bir trading botu örneğidir.

Bu patchte yapılan değişiklikler:
- bot.py: Menü fonksiyonundaki girinti hatası düzeltildi, mesajlarda parse_mode HTML olarak tutarlı hale getirildi.
- .vscode/launch.json: VS Code debug konfigürasyonu eklendi (program: bot.py). Böylece config.json yerine bot.py çalıştırılacak.
- README.md: Kurulum ve çalıştırma adımları eklendi.
- requirements.txt: python-telegram-bot sürümü 20.3 olarak sabitlendi (uyumluluk için öneri).

Branch: fix/telegram-v20-and-readme

Notlar:
- .env veya ortam değişkenleri ile TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID tanımlanmalıdır.
- Yürütme öncesi `pip install -r requirements.txt` çalıştırın.
