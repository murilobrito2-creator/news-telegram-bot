import os, io, time, json, hashlib, requests, feedparser
from datetime import datetime
from dateutil import parser as dtparser
from gtts import gTTS
from telegram import Bot
from readability import Document
from urllib.parse import urlparse
from lxml import html
import yaml

# SUMY - resumo extrativo (leve e gratuito)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Carrega as fontes configuradas
with open("sources.yaml", "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

# Histórico para não repetir notícia
STATE_FILE = "state.json"
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        SEEN = set(json.load(f))
else:
    SEEN = set()

def clean(s):
    return " ".join((s or "").split())

def fetch_fulltext(url, timeout=12):
    """Tenta puxar o texto completo da página; se não der, retorna vazio."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        doc = Document(r.text)
        content_html = doc.summary()
        tree = html.fromstring(content_html)
        text = " ".join(tree.xpath("//text()")).strip()

        # fallback: tenta buscar direto <p> do HTML bruto se vier muito curto
        if len(text) < 200:
            tree2 = html.fromstring(r.text)
            text2 = " ".join(tree2.xpath("//p//text()")).strip()
            return text2 if len(text2) > len(text) else text
        return text
    except Exception:
        return ""

def summarize_text(text, lang="pt", max_sentences=5):
    """Resumo extrativo com TextRank (leve, sem IA paga).
       Se o texto for curto, devolve o próprio texto."""
    text = clean(text)
    if len(text) < CFG.get("min_chars_to_summarize", 800):
        return text

    # Mapeia idiomas do YAML para o tokenizador do sumy
    lang_map = {"pt": "portuguese", "en": "english"}
    sumy_lang = lang_map.get(lang, "english")

    try:
        parser = PlaintextParser.from_string(text, Tokenizer(sumy_lang))
        summarizer = TextRankSummarizer()
        sentences = summarizer(parser.document, max_sentences)
        summary = " ".join(str(s) for s in sentences)
        if len(clean(summary)) < 100:  # fallback se vier pouco
            summary = " ".join(text.split(". ")[:max_sentences]) + "."
        return summary
    except Exception:
        # fallback bem simples
        return " ".join(text.split(". ")[:max_sentences]) + "."

def make_tts(text, lang_code):
    tts = gTTS(text=text, lang=lang_code, slow=False)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    buf.seek(0)
    return buf

def item_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title","")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def send_to_telegram(title, summary, link, audio_buf, source_name):
    bot = Bot(token=BOT_TOKEN)

    # Mensagem de texto (resumo + link)
    msg = f"📰 *{source_name}*\n*{title}*\n\n{summary}\n\n🔗 {link}"
    bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="Markdown")

    # Áudio (mp3)
    filename = f"{source_name}_{int(time.time())}.mp3"
    bot.send_audio(chat_id=CHAT_ID, audio=audio_buf, title=title, performer=source_name, filename=filename)

def run():
    sent = 0
    limit = CFG.get("limit_per_source", 3)

    for feed in CFG["feeds"]:
        source = feed["name"]
        lang = feed["lang"]  # "pt" ou "en"
        count = 0
        for url in feed["urls"]:
            d = feedparser.parse(url)
            for entry in d.entries:
                if count >= limit:
                    break

                iid = item_id(entry)
                if iid in SEEN:
                    continue

                title = clean(entry.get("title", ""))
                link = entry.get("link", "")
                desc = clean(getattr(entry, "summary", "") or "")

                # tenta pegar texto completo da matéria
                fulltext = fetch_fulltext(link)
                base_text = fulltext if len(fulltext) >= 300 else (fulltext + "\n" + desc)
                if not base_text.strip():
                    base_text = title + ". " + desc

                # resumo
                summary = summarize_text(base_text, lang=lang)

                # TTS idioma: PT para G1; EN para NYT/CNN
                tts_lang = "pt" if lang.startswith("pt") else "pt"
                audio = make_tts(f"{title}. {summary}", lang_code=tts_lang)

                # envia
                send_to_telegram(title, summary, link, audio, source)

                SEEN.add(iid)
                count += 1
                sent += 1

    # salva histórico
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(list(SEEN), f)
    return sent

if __name__ == "__main__":
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID como variáveis de ambiente.")
    total = run()
    print(f"Sent {total} items.")
