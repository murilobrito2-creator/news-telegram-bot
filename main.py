from telegram import Bot

def run():
    # Mensagem de teste imediata
    bot = Bot(token=BOT_TOKEN)
    bot.send_message(chat_id=CHAT_ID, text="🚀 Iniciei o workflow. Vou tentar enviar notícias…", parse_mode="HTML")
    sent = 0
    limit = CFG.get("limit_per_source", 3)
    ...
