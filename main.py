# main.py
# Bot de notícias diário (G1, NYTimes, CNN) com resumo + ÁUDIO POR FONTE em PT-BR
# TTS: Google Cloud Text-to-Speech (Neural2/WaveNet) + SSML
# Tradução para PT-BR: deep-translator (usa serviços públicos gratuitos)

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

# Tradução
from deep_translator import GoogleTranslator

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
    """Cria /tmp/gcred.json se GOOGLE_APPLICATION_CREDENTIALS_JSON vier nos Secrets."""
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
        if len(clean(summary)) < 100:
            summary = " ".join(text.split(". ")[:max_sentences]) + "."
        return summary
    except Exception:
        return " ".join(text.split(". ")[:max_sentences]) + "."

def translate_to_pt(text, src_lang):
    """Traduz para PT-BR se a fonte não for PT. Mantém em PT se já estiver em PT."""
    if not text:
        return text
    if src_lang.lower().startswith("pt"):
        return text
    try:
        return GoogleTranslator(source="auto", target="pt").translate(text)
    except Exception:
        # fallback: devolve original se der erro ao traduzir
        return text

def make_tts(text, voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0):
    """
    Gera áudio com voz neural do Google (mais natural).
    Sugestões de voz: 'pt-BR-Neural2-A/B/C/D' ou 'pt-BR-Wavenet-A/B/C/D'.
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
    voice = tts.VoiceSelectionParams(language_code="pt-BR", name=voice_name)
    audio_config = tts.AudioConfig(audio_encoding=tts.AudioEncoding.MP3)

    response = client.synthesize_speech(
        input=synthesis_input, voice=voice, audio_config=audio_config
    )

    buf = io.BytesIO(response.audio_content)
    buf.seek(0)
    return buf

def send_text(chat_id, text):
    Bot(token=BOT_TOKEN).send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)

def send_audio(chat_id, audio_buf, title="Boletim", performer="Bot", filename="boletim.mp3"):
    Bot(token=BOT_TOKEN).send_audio(chat_id=chat_id, audio=audio_buf, title=title, performer=performer, filename=filename)

def item_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

def chunk_text(text, max_chars=4500):
    """Divide texto grande em blocos (Google TTS aceita ~5000 chars)."""
    text = text.strip()
    chunks = []
    while len(text) > max_chars:
        # corta em um ponto próximo de quebra de frase
        cut = text.rfind(". ", 0, max_chars)
        if cut == -1:
            cut = max_chars
        chunks.append(text[:cut+1].strip())
        text = text[cut+1:].strip()
    if text:
        chunks.append(text)
    return chunks

def build_audio_script_pt(source_name, items_pt):
    """
    Monta o roteiro em PT-BR para um único áudio da fonte:
    - Abertura + lista numerada (título + resumo) + encerramento curto.
    """
    linhas = []
    hoje = datetime.now().strftime("%d/%m/%Y")
    linhas.append(f"Boletim de notícias do {source_name}, {hoje}.")
    linhas.append("Confira os destaques:")

    for i, it in enumerate(items_pt, start=1):
        titulo = it["title_pt"]
        resumo = it["summary_pt"]
        linhas.append(f"{i}. {titulo}. {resumo}")

    linhas.append("Esses foram os principais destaques. Até a próxima edição.")
    return " ".join(linhas)

# -------------------------
# Fluxo principal
# -------------------------
def run():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID como variáveis de ambiente.")

    # Prova de vida
    send_text(CHAT_ID, "🚀 Iniciei o workflow. Vou coletar, resumir e gerar 1 áudio por fonte em PT-BR…")

    # Coletar itens por fonte (sem enviar ainda)
    per_source_items = {}  # { source_name: [ {title, link, summary_pt, title_pt}, ... ] }
    limit = CFG.get("limit_per_source", 3)

    for feed in CFG["feeds"]:
        source = feed["name"]
        lang = feed["lang"]  # "pt" ou "en"
        per_source_items.setdefault(source, [])
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

                fulltext = fetch_fulltext(link)
                base_text = fulltext if len(fulltext) >= 300 else (fulltext + "\n" + desc)
                if not base_text.strip():
                    base_text = f"{title}. {desc}"

                # sumariza no idioma de origem
                sum_lang = "pt" if lang.startswith("pt") else "en"
                summary = summarize_text(base_text, lang=sum_lang, max_sentences=5)

                # traduz tudo para PT-BR (título e resumo)
                title_pt = translate_to_pt(title, lang)
                summary_pt = translate_to_pt(summary, lang)

                per_source_items[source].append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "title_pt": title_pt,
                    "summary_pt": summary_pt
                })

                SEEN.add(iid)
                count += 1

    # Enviar ÁUDIO ÚNICO por fonte (dividido em partes se necessário)
    total_boletins = 0
    for source_name, items in per_source_items.items():
        if not items:
            continue

        # Monta roteiro (PT-BR) com títulos e resumos traduzidos
        script = build_audio_script_pt(source_name, items)

        # Divide se for muito grande para o TTS
        partes = chunk_text(script, max_chars=4500)

        # Envia caption/links em texto (opcional)
        try:
            bullets = "\n".join([f"• <b>{clean(it['title_pt'])}</b>\n🔗 {it['link']}" for it in items])
            send_text(CHAT_ID, f"📰 <b>{source_name}</b> — Destaques da edição:\n\n{bullets}")
        except Exception:
            pass

        # Gera e envia o(s) áudio(s)
        for idx, parte in enumerate(partes, start=1):
            try:
                audio_buf = make_tts(parte, voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0)
                titulo_audio = f"{source_name} — Boletim ({idx}/{len(partes)})" if len(partes) > 1 else f"{source_name} — Boletim"
                filename = f"{source_name}_boletim_{idx}.mp3" if len(partes) > 1 else f"{source_name}_boletim.mp3"
                send_audio(CHAT_ID, audio_buf, title=titulo_audio, performer=source_name, filename=filename)
            except Exception as e:
                send_text(CHAT_ID, f"❌ Falha ao sintetizar {source_name} (parte {idx}): {e}")

        total_boletins += 1
        time.sleep(1)  # pequeno respiro entre fontes

    # salva histórico
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(list(SEEN), f)
    except Exception:
        pass

    if total_boletins == 0:
        send_text(CHAT_ID, "ℹ️ Sem novidades para montar boletins hoje (ou já lidas).")
    else:
        send_text(CHAT_ID, f"✅ Boletins gerados: {total_boletins} fonte(s).")

if __name__ == "__main__":
    run()
