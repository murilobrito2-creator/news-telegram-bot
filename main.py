# main.py
# Bot de notícias diário com resumo detalhado, agrupado por assuntos,
# e 1 ÁUDIO POR FONTE em PT-BR (G1 / NYTimes / CNN).
# TTS: Google Cloud Text-to-Speech (Neural2/WaveNet) + SSML.

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

# Tradução automática
from deep_translator import GoogleTranslator


# -------------------------
# Variáveis de ambiente
# -------------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -------------------------
# Configurações gerais
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
# Parâmetros de detalhamento / voz
# -------------------------
SENTENCES_PER_ITEM = 4        # mais conteúdo por notícia
MAX_ITEMS_PER_TOPIC = 4       # até 4 por tema (ajusta se quiser)
VOICE_NAME = "pt-BR-Neural2-A"  # timbre mais quente; teste A ou C
VOICE_RATE = 0.99             # um pouco mais lento, soa menos “robótico”
VOICE_PITCH = +0.5            # sutilmente mais alto, mas natural


# -------------------------
# Utilidades
# -------------------------
def init_google_credentials():
    """
    Cria /tmp/gcred.json se o Secret GOOGLE_APPLICATION_CREDENTIALS_JSON
    estiver configurado no GitHub Actions.
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
        if len(text) < 200:
            tree2 = html.fromstring(r.text)
            text2 = " ".join(tree2.xpath("//p//text()")).strip()
            return text2 if len(text2) > len(text) else text
        return text
    except Exception:
        return ""

def summarize_text(text, lang="en", max_sentences=None, min_chars=None):
    """
    Resumo extrativo (TextRank) com saída mais substanciosa.
    Ajusta nº de frases com base no tamanho do texto.
    Se o texto for curto, devolve o próprio texto.
    """
    text = clean(text)
    if not min_chars:
        min_chars = CFG.get("min_chars_to_summarize", 600)
    if len(text) < min_chars:
        return text

    if max_sentences is None:
        if len(text) < 1200:
            max_sentences = max(2, SENTENCES_PER_ITEM)        # curta
        elif len(text) < 2500:
            max_sentences = max(3, SENTENCES_PER_ITEM + 1)    # média
        else:
            max_sentences = max(4, SENTENCES_PER_ITEM + 2)    # longa

    lang_map = {"pt": "portuguese", "en": "english"}
    sumy_lang = lang_map.get(lang, "english")

    try:
        parser = PlaintextParser.from_string(text, Tokenizer(sumy_lang))
        summarizer = TextRankSummarizer()
        sentences = summarizer(parser.document, max_sentences)
        summary = " ".join(str(s) for s in sentences)
        if len(clean(summary)) < 120:
            summary = " ".join(text.split(". ")[:max_sentences]) + "."
        return summary
    except Exception:
        return " ".join(text.split(". ")[:max_sentences]) + "."

def translate_to_pt(text, src_lang):
    """Traduz para PT-BR se a fonte não for PT. Mantém em PT se já estiver em PT."""
    if not text:
        return text
    if str(src_lang).lower().startswith("pt"):
        return text
    try:
        return GoogleTranslator(source="auto", target="pt").translate(text)
    except Exception:
        return text  # fallback: original

def make_tts(text, voice_name=VOICE_NAME, speaking_rate=VOICE_RATE, pitch_semitones=VOICE_PITCH):
    """
    Gera áudio com voz neural do Google (mais natural).
    Sugestões: 'pt-BR-Neural2-A/B/C/D' ou 'pt-BR-Wavenet-A/B/C/D'.
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

def chunk_text(text, max_chars=4400):
    """Divide texto grande em blocos (~5k chars é o limite seguro do TTS)."""
    text = text.strip()
    chunks = []
    while len(text) > max_chars:
        cut = text.rfind(". ", 0, max_chars)
        if cut == -1:
            cut = max_chars
        chunks.append(text[:cut+1].strip())
        text = text[cut+1:].strip()
    if text:
        chunks.append(text)
    return chunks

# -------------------------
# Agrupamento por assuntos (PT)
# -------------------------
def detect_topic_pt(title_pt, summary_pt):
    """
    Classificação simples por palavras-chave (PT-BR).
    Se não bater, cai em 'Outros'.
    """
    text = f"{title_pt} {summary_pt}".lower()

    topics = {
        "Política":    ["política", "governo", "congresso", "câmara", "senado", "eleição", "ministro", "prefeitura", "presidente", "plano diretor"],
        "Economia":    ["economia", "inflação", "juros", "banco central", "dólar", "balanço", "mercado", "crescimento", "desemprego", "investimento"],
        "Mundo":       ["mundo", "internacional", "guerra", "acordo", "otan", "onu", "rússia", "china", "eua", "europeu"],
        "Tecnologia":  ["tecnologia", " ia ", "inteligência artificial", "startup", "software", "app", "privacidade", "segurança digital"],
        "Esportes":    ["esporte", "futebol", "basquete", "vôlei", "olimpíada", "campeonato", "técnico", "clube", "seleção"],
        "Saúde":       ["saúde", "covid", "vacina", "hiv", "h1n1", "hospital", "sus"],
        "Cultura":     ["cultura", "cinema", "série", "filme", "música", "artes", "teatro", "festival"],
        "Ciência":     ["ciência", "pesquisa", "universidade", "estudo científico", "descoberta"],
        "Negócios":    ["negócio", "empresa", "lucro", "fusão", "aquisição", "resultado", "receita", "expansão", "contrato"]
    }

    for topic, keywords in topics.items():
        if any(k in text for k in keywords):
            return topic
    return "Outros"

def group_by_topic_pt(items_pt):
    """
    items_pt: lista de dicts com 'title_pt' e 'summary_pt'
    Retorna: dict { tópico: [itens...] }
    Limita itens por tópico em MAX_ITEMS_PER_TOPIC (para áudio ficar agradável).
    """
    grouped = {}
    for it in items_pt:
        topic = detect_topic_pt(it["title_pt"], it["summary_pt"])
        grouped.setdefault(topic, [])
        if len(grouped[topic]) < MAX_ITEMS_PER_TOPIC:
            grouped[topic].append(it)

    order = ["Política", "Economia", "Mundo", "Tecnologia", "Negócios", "Saúde", "Esportes", "Cultura", "Ciência", "Outros"]
    grouped_sorted = {t: grouped[t] for t in order if t in grouped and grouped[t]}
    return grouped_sorted

def build_audio_script_pt(source_name, grouped_topics):
    """
    Monta o roteiro em PT-BR por tópicos:
    - Abertura
    - Para cada tópico: título do tópico + itens numerados com título + resumo
    - Encerramento
    """
    partes = []
    hoje = datetime.now().strftime("%d/%m/%Y")
    partes.append(f"Boletim de notícias do {source_name}, {hoje}.")
    partes.append("Principais destaques organizados por assunto:")

    for topic, items in grouped_topics.items():
        partes.append(f"Seção: {topic}.")
        for i, it in enumerate(items, start=1):
            titulo = clean(it["title_pt"])
            resumo = clean(it["summary_pt"])
            partes.append(f"{i}. {titulo}. {resumo}")

    partes.append("Esses foram os principais temas. Até a próxima edição.")
    return " ".join(partes)


# -------------------------
# Fluxo principal
# -------------------------
def run():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID como variáveis de ambiente.")

    # Prova de vida
    send_text(CHAT_ID, "🚀 Iniciei o workflow. Vou coletar, resumir e gerar 1 áudio por fonte em PT-BR…")

    # Coletar itens por fonte (sem enviar ainda)
    per_source_items = {}  # { source_name: [ {title, link, summary, title_pt, summary_pt}, ... ] }
    limit = CFG.get("limit_per_source", 6)

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

                sum_lang = "pt" if str(lang).startswith("pt") else "en"
                summary = summarize_text(base_text, lang=sum_lang, max_sentences=None)

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

        # Agrupa por tópicos (PT) e monta roteiro mais completo
        grouped = group_by_topic_pt(items)
        script = build_audio_script_pt(source_name, grouped)

        # Envia um texto com a lista por tópico (útil para acompanhar com links)
        try:
            blocks = []
            for topic, itlist in grouped.items():
                bullets = "\n".join([f"• <b>{clean(it['title_pt'])}</b>\n🔗 {it['link']}" for it in itlist])
                blocks.append(f"<u><b>{topic}</b></u>\n{bullets}")
            send_text(CHAT_ID, f"📰 <b>{source_name}</b> — Destaques por assunto:\n\n" + "\n\n".join(blocks))
        except Exception:
            pass

        # Divide se for muito grande para o TTS
        partes = chunk_text(script, max_chars=4400)

        # Gera e envia o(s) áudio(s)
        for idx, parte in enumerate(partes, start=1):
            try:
                audio_buf = make_tts(parte, voice_name=VOICE_NAME, speaking_rate=VOICE_RATE, pitch_semitones=VOICE_PITCH)
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
