def send_to_telegram(title, summary, link, audio_buf, source_name):
    bot = Bot(token=BOT_TOKEN)
    # Use HTML para evitar erros comuns do Markdown
    caption = f"<b>{source_name}</b> — <b>{title}</b>\n{link}"
    text = f"📰 <b>{source_name}</b>\n<b>{title}</b>\n\n{summary}\n\n🔗 {link}"
    # Envia texto
    bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=False)
    # Envia áudio
    filename = f"{source_name}_{int(time.time())}.mp3"
    bot.send_audio(chat_id=CHAT_ID, audio=audio_buf, title=title, performer=source_name, filename=filename)
