# main.py
# Boletim diário: 1 ÁUDIO POR FONTE em PT-BR, detalhado por temas (~6 min).
# Correções:
# - Chunking por bytes p/ respeitar 5000 bytes de SSML
# - Fallback de voz (Neural2-C -> Neural2-A -> Wavenet-B)
# - SSML seguro p/ Neural2
# - Tradução forçada do roteiro final p/ PT-BR

import os, io, time, json, re, hashlib, requests, feedparser, yaml
from datetime import datetime
from lxml import html
from readability import Document
from telegram import Bot

# Resumo extrativo
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

# Google TTS
from google.cloud import texttospeech as tts

# Tradução
from deep_translator import GoogleTranslator

# =========================
# Variáveis de ambiente
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# Configurações gerais
# =========================
CFG_PATH = "sources.yaml"
with open(CFG_PATH, "r", encoding="utf-8") as f:
    CFG = yaml.safe_load(f)

STATE_FILE = "state.json"
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        SEEN = set(json.load(f))
else:
    SEEN = set()

# =========================
# Alvo de duração e detalhes
# =========================
TARGET_MINUTES = 6.0
WPM_ESTIMATE   = 160              # palavras/minuto (rate ~1.02)
MAX_WORDS      = int(TARGET_MINUTES * WPM_ESTIMATE)  # ~960 palavras

SENTENCES_PER_ITEM   = 4
MAX_ITEMS_PER_TOPIC  = 4
LIMIT_PER_SOURCE_DEF = 8

# Voz/prosódia
VOICE_PREFERENCE = ["pt-BR-Neural2-C", "pt-BR-Neural2-A", "pt-BR-Wavenet-B"]
VOICE_RATE  = 1.02
VOICE_PITCH = +0.2

# Limites de segurança
MAX_SSML_BYTES = 4800   # margem segura < 5000
MAX_TEXT_BYTES = 4300   # alvo de bytes de texto bruto por chunk antes de virar SSML

# =========================
# Utilidades
# =========================
def init_google_credentials():
    cred_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if cred_json and not os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        path = "/tmp/gcred.json"
        with open(path, "w", encoding="utf-8") as f:
            f.write(cred_json)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = path

def clean(s):
    return " ".join((s or "").split())

def strip_urls(text: str) -> str:
    return re.sub(r'https?://\S+|www\.\S+', ' (link na descrição) ', text or "", flags=re.IGNORECASE)

def strip_unsupported(text: str) -> str:
    text = re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', ' ', text)  # controles
    text = re.sub(r'(?<!\w)&(?!\w)', ' e ', text)               # & isolado
    return text

def html_escape_basic(text: str) -> str:
    return (text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;"))

def fetch_fulltext(url, timeout=12):
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
    text = clean(text)
    if not min_chars:
        min_chars = CFG.get("min_chars_to_summarize", 700)
    if len(text) < min_chars:
        return text

    if max_sentences is None:
        if len(text) < 1200:
            max_sentences = max(2, SENTENCES_PER_ITEM)
        elif len(text) < 2500:
            max_sentences = max(3, SENTENCES_PER_ITEM + 1)
        else:
            max_sentences = max(4, SENTENCES_PER_ITEM + 2)

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

def translate_to_pt(text):
    if not text:
        return text
    try:
        return GoogleTranslator(source="auto", target="pt").translate(text)
    except Exception:
        return text

def word_count(s: str) -> int:
    return len(clean(s).split())

def limit_words(text: str, max_words: int) -> str:
    words = clean(text).split()
    if len(words) <= max_words:
        return " ".join(words)
    trimmed = " ".join(words[:max_words])
    return trimmed.rstrip(" ,;") + "."

def item_id(entry):
    base = entry.get("id") or entry.get("link") or entry.get("title", "")
    return hashlib.sha1(base.encode("utf-8")).hexdigest()

# =========================
# Agrupamento por assuntos (PT)
# =========================
def detect_topic_pt(title_pt, summary_pt):
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
    grouped = {}
    for it in items_pt:
        topic = detect_topic_pt(it["title_pt"], it["summary_pt"])
        grouped.setdefault(topic, [])
        if len(grouped[topic]) < MAX_ITEMS_PER_TOPIC:
            grouped[topic].append(it)
    order = ["Política", "Economia", "Mundo", "Tecnologia", "Negócios", "Saúde", "Esportes", "Cultura", "Ciência", "Outros"]
    return {t: grouped[t] for t in order if t in grouped and grouped[t]}

# =========================
# Roteiro e SSML
# =========================
def build_audio_script_pt(source_name, grouped_topics):
    partes = []
    hoje = datetime.now().strftime("%d/%m/%Y")
    partes.append(f"Boletim de notícias do {source_name}, {hoje}.")
    partes.append("Vamos aos destaques organizados por assunto.")
    partes.append("¦")

    for topic, items in grouped_topics.items():
        partes.append(f"Seção: {topic}.")
        partes.append("Principais pontos:")
        for i, it in enumerate(items, start=1):
            titulo = clean(it["title_pt"])
            resumo = clean(it["summary_pt"])
            partes.append(f"Notícia {i}: {titulo}.")
            partes.append(f"Resumo: {resumo}")
        partes.append("Fechamos esta seção.")
        partes.append("¦")

    partes.append("Esses foram os assuntos mais relevantes de hoje.")
    partes.append("Até a próxima edição.")
    script = " ".join(partes)

    # PT-BR garantido no roteiro final
    script_pt = translate_to_pt(script)

    # Controle de duração (~6min)
    if word_count(script_pt) > MAX_WORDS:
        script_pt = limit_words(script_pt, MAX_WORDS)

    return script_pt

def text_to_valid_ssml(text: str, rate: float, pitch_st: float) -> str:
    """
    Constrói SSML válido para Neural2:
    - remove URLs, caracteres de controle; escapa &, <, >
    - divide por sentenças; insere <s>…</s> + <break>
    - converte '¦' em pausa longa (~700ms)
    """
    text = strip_urls(text)
    text = strip_unsupported(text)
    text = text.replace("¦", " <LONG_BREAK> ")
    text = clean(text)

    sentences = re.split(r'(?<=[\.\!\?])\s+', text)
    ssml_parts = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        if "<LONG_BREAK>" in s:
            s = s.replace("<LONG_BREAK>", "").strip()
            if s:
                s = html_escape_basic(s)
                ssml_parts.append(f"<s>{s}</s><break time='700ms'/>")
            else:
                ssml_parts.append("<break time='700ms'/>")
        else:
            s = html_escape_basic(s)
            br = "220ms" if len(s) < 120 else "300ms"
            ssml_parts.append(f"<s>{s}</s><break time='{br}'/>")

    ssml_body = " ".join(ssml_parts)
    ssml = f"""
<speak>
  <prosody rate="{rate}" pitch="{pitch_st:+.1f}st">
    <p>{ssml_body}</p>
  </prosody>
</speak>
    """.strip()
    return ssml

def chunk_script_by_bytes(text: str, max_text_bytes: int = MAX_TEXT_BYTES):
    """
    Divide o texto bruto em partes com base em bytes (UTF-8),
    separando preferencialmente por '¦' (seções) e depois por frases.
    """
    parts = []
    # 1) tenta dividir pelas seções (marcador '¦')
    sections = [s.strip() for s in text.split("¦") if s.strip()]
    current = ""
    for sec in sections:
        candidate = (current + " " + sec).strip() if current else sec
        if len(candidate.encode("utf-8")) <= max_text_bytes:
            current = candidate
        else:
            if current:
                parts.append(current)
            # se a seção sozinha é grande, divide por sentenças
            if len(sec.encode("utf-8")) <= max_text_bytes:
                current = sec
            else:
                # divide por sentenças para caber
                sentences = re.split(r'(?<=[\.\!\?])\s+', sec)
                chunk = ""
                for s in sentences:
                    test = (chunk + " " + s).strip() if chunk else s
                    if len(test.encode("utf-8")) <= max_text_bytes:
                        chunk = test
                    else:
                        if chunk:
                            parts.append(chunk)
                        chunk = s
                if chunk:
                    current = chunk
                else:
                    current = ""
    if current:
        parts.append(current)
    return parts if parts else [text]

def synthesize_with_fallback(ssml: str):
    """
    Tenta sintetizar com as vozes na ordem de VOICE_PREFERENCE.
    Se uma voz falhar (ex: inexistente), tenta a próxima.
    """
    init_google_credentials()
    client = tts.TextToSpeechClient()
    for voice_name in VOICE_PREFERENCE:
        try:
            synthesis_input = tts.SynthesisInput(ssml=ssml)
            voice = tts.VoiceSelectionParams(language_code="pt-BR", name=voice_name)
            audio_config = tts.AudioConfig(audio_encoding=tts.AudioEncoding.MP3)
            response = client.synthesize_speech(
                input=synthesis_input, voice=voice, audio_config=audio_config
            )
            buf = io.BytesIO(response.audio_content)
            buf.seek(0)
            return buf, voice_name
        except Exception as e:
            last_error = e
            continue
    raise last_error

# =========================
# Telegram
# =========================
def send_text(chat_id, text):
    Bot(token=BOT_TOKEN).send_message(chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview=False)

def send_audio(chat_id, audio_buf, title="Boletim", performer="Bot", filename="boletim.mp3"):
    Bot(token=BOT_TOKEN).send_audio(chat_id=chat_id, audio=audio_buf, title=title, performer=performer, filename=filename)

# =========================
# Fluxo principal
# =========================
def run():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID como variáveis de ambiente.")

    send_text(CHAT_ID, "🚀 Iniciei o workflow. Vou coletar, resumir e gerar 1 áudio por fonte em PT-BR…")

    per_source_items = {}
    limit = CFG.get("limit_per_source", LIMIT_PER_SOURCE_DEF)

    for feed in CFG["feeds"]:
        source = feed["name"]
        lang   = feed.get("lang", "pt")
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
                link  = entry.get("link", "")
                desc  = clean(getattr(entry, "summary", "") or "")

                fulltext = fetch_fulltext(link)
                base_text = fulltext if len(fulltext) >= 300 else (fulltext + "\n" + desc)
                if not base_text.strip():
                    base_text = f"{title}. {desc}"

                sum_lang = "pt" if str(lang).startswith("pt") else "en"
                summary  = summarize_text(base_text, lang=sum_lang, max_sentences=None)

                # traduz sempre para PT-BR (garante áudio 100% PT)
                title_pt   = translate_to_pt(title)
                summary_pt = translate_to_pt(summary)

                per_source_items[source].append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "title_pt": title_pt,
                    "summary_pt": summary_pt
                })

                SEEN.add(iid)
                count += 1

    total_boletins = 0
    for source_name, items in per_source_items.items():
        if not items:
            continue

        grouped   = group_by_topic_pt(items)
        script_pt = build_audio_script_pt(source_name, grouped)

        # texto com links por tema
        try:
            blocks = []
            for topic, itlist in grouped.items():
                if not itlist:
                    continue
                bullets = "\n".join([f"• <b>{clean(it['title_pt'])}</b>\n🔗 {it['link']}" for it in itlist])
                blocks.append(f"<u><b>{topic}</b></u>\n{bullets}")
            if blocks:
                send_text(CHAT_ID, f"📰 <b>{source_name}</b> — Destaques por assunto:\n\n" + "\n\n".join(blocks))
        except Exception:
            pass

        # --- Chunking por bytes (antes de virar SSML) ---
        text_chunks = chunk_script_by_bytes(script_pt, max_text_bytes=MAX_TEXT_BYTES)

        # Gera e envia o(s) áudios (Parte 1/2/…)
        for idx, chunk_text in enumerate(text_chunks, start=1):
            try:
                ssml = text_to_valid_ssml(chunk_text, VOICE_RATE, VOICE_PITCH)

                # Checagem final de bytes do SSML; se ainda estiver grande, fatiamos por frases
                if len(ssml.encode("utf-8")) > MAX_SSML_BYTES:
                    # divide por frases de novo
                    sentences = re.split(r'(?<=[\.\!\?])\s+', chunk_text)
                    sub = []
                    current = ""
                    subparts = []
                    for s in sentences:
                        cand = (current + " " + s).strip() if current else s
                        # cria ssml temporário para aferir bytes
                        tmp_ssml = text_to_valid_ssml(cand, VOICE_RATE, VOICE_PITCH)
                        if len(tmp_ssml.encode("utf-8")) <= MAX_SSML_BYTES:
                            current = cand
                        else:
                            if current:
                                subparts.append(current)
                            current = s
                    if current:
                        subparts.append(current)

                    # sintetiza subpartes
                    for j, subtext in enumerate(subparts, start=1):
                        ssml_sub = text_to_valid_ssml(subtext, VOICE_RATE, VOICE_PITCH)
                        audio_buf, used_voice = synthesize_with_fallback(ssml_sub)
                        title_audio = f"{source_name} — Boletim (parte {idx}.{j}) — {used_voice}"
                        filename    = f"{source_name}_boletim_{idx}_{j}.mp3"
                        send_audio(CHAT_ID, audio_buf, title=title_audio, performer=source_name, filename=filename)
                else:
                    audio_buf, used_voice = synthesize_with_fallback(ssml)
                    title_audio = f"{source_name} — Boletim (parte {idx}/{len(text_chunks)}) — {used_voice}"
                    filename    = f"{source_name}_boletim_{idx}.mp3" if len(text_chunks) > 1 else f"{source_name}_boletim.mp3"
                    send_audio(CHAT_ID, audio_buf, title=title_audio, performer=source_name, filename=filename)

            except Exception as e:
                send_text(CHAT_ID, f"❌ Falha ao sintetizar {source_name} (parte {idx}): {e}")

        total_boletins += 1
        time.sleep(1)

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
