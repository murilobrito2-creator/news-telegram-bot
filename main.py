# main.py
# Bot de notícias diário (NYTimes, G1, CNN) com resumo + áudio no Telegram
# TTS: Google Cloud Text-to-Speech (Neural2/WaveNet) + SSML para naturalidade

import os, io, time, json, hashlib, requests, feedparser, yaml
from datetime import datetime
from dateutil import parser as dtparser
from urllib.parse import urlparse

from lxml import html
from readability import Document

from telegram import Bot

# Resumo extrativo leve (sem API paga)
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

# Google TTS
from google.cloud import texttospeech as tts

# -------------------------
# Variáveis de ambiente
# -------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -------------------------
# Carrega configurações
# -------------------------
CFG_PATH = "sources.yaml"
with open(CFG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

STATE_FILE = "state.json"
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        SEEN = set(json.load(f))
else:
    SEEN = set()

# -------------------------
# Utilidades
# -------------------------
def init_google_credentials():
    """
    Se você colocou o JSON da credencial do Google no Secret
    GOOGLE_APPLICATION_CREDENTIALS_JSON, esta função cria um arquivo temporário
    e define a variável GOOGLE_APPLICATION_CREDENTIALS para o client usar.
    """
    cred_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if cred_json and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        path = "/tmp/gcred.json"
        with open(path, "w", encoding="utf-8") as f:
            f.write(cred_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path

def clean(s):
    return " ".join((s or "").split())

def fetch_fulltext(url, timeout=12):
    """Tenta puxar o texto completo da página; se falhar, retorna string vazia."""
    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        doc = Document(r.text)
        content_html = doc.summary()
        tree = html.fromstring(content_html)
        text = " ".join(tree.xpath("//text()")).strip()

        # Fallback: tenta direto do HTML bruto
        if len(text) < 200:
            tree2 = html.fromstring(r.text)
            text2 = " ".join(tree2.xpath("//p//text()")).strip()
            return text2 if len(text2) > len(text) else text
        return text
    except Exception:
        return ""

def summarize_text(text, lang="en", max_sentences=5, min_chars=None):
    """Resumo extrativo (TextRank). Se texto for curto, devolve o próprio texto."""
    text = clean(text)
    if not min_chars:
        min_chars = CFG.get("min_chars_to_summarize", 800)
    if len(text) < min_chars:
        return text

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
        return " ".join(text.split(". ")[:max_sentences]) + "."

def make_tts(text, voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0):
    """
    Gera áudio com voz neural do Google (mais natural).
    Sugestões de vozes: 'pt-BR-Neural2-A/B/C/D' ou 'pt-BR-Wavenet-A/B/C/D'.
    speaking_rate ~1.02–1.05 e pitch ~+1.0st dão "vibe podcast".
    """
    init_google_credentials()
    client = tts.TextToSpeechClient()

    ssml = f"""
<speak>
  <p>
    <s><prosody rate="{speaking_rate}" pitch="{pitch_semitones:+.1f}st">
      {text}
    </prosody></s>
  </p>
</speak>
    """.strip()

    synthesis_input = tts.SynthesisInput(ssml=ssml)
    # como estamos usando pt-BR em todas as fontes deste exemplo:
    voice = tts.VoiceSelectionParams(language_code="pt-BR", name=voice_name)
    audio_config = tts.AudioConfig(audio_encoding=tts.AudioEncoding.MP3)

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    buf = io.BytesIO(response.audio_content)
    buf.seek(0)
    return buf

def send_to_telegram(title, summary, link, audio_buf, source_name):
    """Envia texto + (opcional) áudio. Usa HTML para evitar problemas de formatação."""
    bot = Bot(token=BOT_TOKEN)
    text = f"📰 <b>{source_name}</b>\n<b>{title}</b>\n\n{summary}\n\n🔗 {link}"
    bot.send_message(chat_id=CHAT_ID, text=text, parse_mode="HTML", disable_web_page_preview=False)
    if audio_buf:
        filename = f"{source_name}_{int(time.time())}.mp3"
        bot.send_audio(chat_id=CHAT_ID, audio=audio_buf, title=title, performer=source_name, filename=filename)

def item_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

# -------------------------
# Fluxo principal
# -------------------------
def run():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID como variáveis de ambiente.")

    # Mensagem de prova de vida
    bot = Bot(token=BOT_TOKEN)
    bot.send_message(chat_id=CHAT_ID, text="🚀 Iniciei o workflow. Vou tentar enviar notícias…", parse_mode="HTML")

    # Áudio de teste (remova depois que confirmar)
    try:
        test_audio = make_tts("Este é um teste de voz neural. Se você está ouvindo isso, o TTS funcionou.", voice_name="pt-BR-Neural2-B")
        bot.send_audio(chat_id=CHAT_ID, audio=test_audio, title="Teste de Voz", performer="Bot", filename="teste.mp3")
    except Exception as e:
        bot.send_message(chat_id=CHAT_ID, text=f"❌ Falha no TTS: {e}", parse_mode="HTML")

    sent = 0
    limit = CFG.get("limit_per_source", 3)

    for feed in CFG["feeds"]:
        source = feed["name"]
        lang = feed["lang"]  # "pt" ou "en"
        count = 0

        for url in feed["urls"]:
            try:
                d = feedparser.parse(url)
            except Exception:
                continue

            for entry in d.entries:
                if count >= limit:
                    break

                iid = item_id(entry)
                if iid in SEEN:
                    continue

                title = clean(entry.get("title", ""))
                link = entry.get("link", "")
                desc = clean(getattr(entry, "summary", "") or "")

                # tenta capturar o texto completo
                fulltext = fetch_fulltext(link)
                base_text = fulltext if len(fulltext) >= 300 else (fulltext + "\n" + desc)
                if not base_text.strip():
                    base_text = f"{title}. {desc}"

                # resumo (define idioma para sumarização)
                sum_lang = "pt" if lang.startswith("pt") else "en"
                summary = summarize_text(base_text, lang=sum_lang, max_sentences=5)

                # TTS em pt-BR (como padrão)
                try:
                    audio = make_tts(f"{title}. {summary}", voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0)
                except Exception:
                    # Se falhar TTS, envia só texto
                    audio = None

                send_to_telegram(title, summary, link, audio, source)

                SEEN.add(iid)
                count += 1
                sent += 1

    # salva histórico para evitar repetição
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(SEEN), f)
    except Exception:
        pass

    print(f"[INFO] Total de itens enviados: {sent}")
    if sent == 0:
        # ajuda no diagnóstico se nada foi enviado
        Bot(token=BOT_TOKEN).send_message(chat_id=CHAT_ID, text="ℹ️ Nenhuma notícia nova para enviar (ou limites/feeds sem novidades).", parse_mode="HTML")

if __name__ == "__main__":
    run()
