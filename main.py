# main.py
# Boletim diário: 1 ÁUDIO POR FONTE em PT-BR (~6 min), por temas.
# TTS: Azure Speech REST (voz masculina, estilo "podcast").
# CNN/NYTimes (EN) -> roteiro final SEMPRE traduzido para PT-BR.
# Nomes em inglês com pronúncia americana via <lang xml:lang="en-US">.

import os, io, time, json, re, hashlib, requests, feedparser, yaml
from datetime import datetime
from lxml import html
from readability import Document
from telegram import Bot

# Sumário extrativo
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

from deep_translator import GoogleTranslator

# =========================
# Variáveis de ambiente
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

AZ_REGION = os.getenv("AZURE_SPEECH_REGION")
AZ_KEY    = os.getenv("AZURE_SPEECH_KEY")

# =========================
# Configurações
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

# Alvo de duração e detalhamento
TARGET_MINUTES = 6.0
WPM_ESTIMATE   = 160                    # ritmo conversacional
MAX_WORDS      = int(TARGET_MINUTES * WPM_ESTIMATE)  # ~960 palavras

SENTENCES_PER_ITEM   = 4
MAX_ITEMS_PER_TOPIC  = 4
LIMIT_PER_SOURCE_DEF = 8

# Voz/estilo Azure (podcast masculino)
VOICE_PRIMARY   = "pt-BR-AntonioNeural"
VOICE_FALLBACKS = ["pt-BR-AndreNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaNeural"]  # se precisar
AZURE_STYLE     = "narration-relaxed"   # ou "newscast-casual", "chat"
AZURE_RATE      = 1.04                  # levemente mais vivo
AZURE_PITCH_ST  = +0.2                  # presença

# Limites
MAX_TEXT_BYTES = 4300   # por chunk antes de virar SSML (margem segura)
MAX_SSML_BYTES = 9500   # Azure aceita mais que 5k; ainda assim, deixamos margem

# =========================
# Utilitários
# =========================
def clean(s):
    return " ".join((s or "").split())

def strip_urls(text: str) -> str:
    return re.sub(r'https?://\S+|www\.\S+', ' (link na descrição) ', text or "", flags=re.IGNORECASE)

def strip_ctrl(text: str) -> str:
    return re.sub(r'[\x00-\x08\x0B-\x0C\x0E-\x1F]', ' ', text or "")

def html_esc(text: str) -> str:
    return text.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")

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
# Nomes em inglês (pronúncia en-US)
# =========================
ENG_STOP = set(x.lower() for x in [
    "The","A","An","And","Of","On","At","In","To","For","With","By","From",
    "Is","Are","Be","Was","Were","As","Not","But","Or",
    "New","Old","Over","After","Before","More","Less",
])

def extract_english_names(items, source_lang: str):
    names = set()
    if not str(source_lang).lower().startswith("en"):
        return names
    for it in items:
        title = it.get("title", "") or ""
        tokens = re.findall(r"\b[A-Z][a-zA-Z\-]+\b", title)
        group = []
        for t in tokens:
            if t.lower() in ENG_STOP:
                if group:
                    names.add(" ".join(group))
                    group = []
                continue
            group.append(t)
        if group:
            names.add(" ".join(group))
    names = {n for n in names if len(n) >= 3}
    return names

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
# Roteiro final em PT-BR
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

    # Força PT-BR no roteiro final (CNN/NYT inclusive)
    script_pt = translate_to_pt(script)

    if word_count(script_pt) > MAX_WORDS:
        script_pt = limit_words(script_pt, MAX_WORDS)
    return script_pt

# =========================
# SSML Azure "podcast" + pronúncia EN-US
# =========================
def build_ssml_podcast_pt(text_pt: str,
                          voice="pt-BR-AntonioNeural",
                          style="narration-relaxed",
                          rate=AZURE_RATE,
                          pitch_st=AZURE_PITCH_ST,
                          names_en: set = None) -> str:
    t = text_pt.replace("¦", " <LONG_BREAK> ")
    t = clean(strip_ctrl(strip_urls(t)))

    # marca nomes para pronúncia americana
    if names_en:
        for name in sorted(names_en, key=len, reverse=True):
            t = re.sub(r'\b' + re.escape(name) + r'\b',
                       f"<ENNAME>{name}</ENNAME>", t)

    sentences = re.split(r'(?<=[\.\!\?])\s+', t)
    parts = []
    for s in sentences:
        s = s.strip()
        if not s:
            continue
        long_break = False
        if "<LONG_BREAK>" in s:
            s = s.replace("<LONG_BREAK>", "").strip()
            long_break = True

        s = html_esc(s)
        s = re.sub(r"&lt;ENNAME&gt;(.*?)&lt;/ENNAME&gt;",
                   r'<lang xml:lang="en-US">\1</lang>', s)

        br = "260ms" if len(s) < 140 else "380ms"
        parts.append(f"<s>{s}</s><break time='700ms'/>" if long_break else f"<s>{s}</s><break time='{br}'/>")

    ssml = f"""
<speak version="1.0" xml:lang="pt-BR"
       xmlns:mstts="https://www.w3.org/2001/mstts"
       xmlns="http://www.w3.org/2001/10/synthesis">
  <voice name="{voice}">
    <mstts:express-as style="{style}" styledegree="1.5">
      <prosody rate="{rate}" pitch="{pitch_st:+.1f}st">
        {' '.join(parts)}
      </prosody>
    </mstts:express-as>
  </voice>
</speak>""".strip()
    return ssml

# =========================
# Chunking e join de MP3
# =========================
def chunk_script_by_bytes(text: str, max_text_bytes: int = MAX_TEXT_BYTES):
    parts = []
    sections = [s.strip() for s in text.split("¦") if s.strip()]
    current = ""
    for sec in sections:
        cand = (current + " " + sec).strip() if current else sec
        if len(cand.encode("utf-8")) <= max_text_bytes:
            current = cand
        else:
            if current:
                parts.append(current)
            if len(sec.encode("utf-8")) <= max_text_bytes:
                current = sec
            else:
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
                current = chunk
    if current:
        parts.append(current)
    return parts if parts else [text]

def join_mp3(buffers):
    out = io.BytesIO()
    for b in buffers:
        b.seek(0)
        out.write(b.read())
    out.seek(0)
    return out

# =========================
# Azure Speech REST (MP3)
# =========================
def azure_speech_tts_mp3(ssml: str,
                         voices=None,
                         audio_format="audio-24khz-48kbitrate-mono-mp3") -> io.BytesIO:
    if not AZ_REGION or not AZ_KEY:
        raise RuntimeError("Defina AZURE_SPEECH_REGION e AZURE_SPEECH_KEY nos Secrets.")
    if voices is None:
        voices = [VOICE_PRIMARY] + VOICE_FALLBACKS

    url = f"https://{AZ_REGION}.tts.speech.microsoft.com/cognitiveservices/v1"
    last_err = None
    for v in voices:
        ssml_v = re.sub(r'name="pt-BR-[A-Za-z]+Neural"', f'name="{v}"', ssml)
        headers = {
            "Ocp-Apim-Subscription-Key": AZ_KEY,
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": audio_format,
            "User-Agent": "news-telegram-bot"
        }
        try:
            r = requests.post(url, headers=headers, data=ssml_v.encode("utf-8"), timeout=45)
            if r.status_code == 200 and r.content:
                buf = io.BytesIO(r.content)
                buf.seek(0)
                return buf
            last_err = f"{r.status_code} {r.text[:200]}"
        except Exception as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"Azure TTS falhou: {last_err}")

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
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.")
    if not AZ_REGION or not AZ_KEY:
        raise SystemExit("Defina AZURE_SPEECH_REGION e AZURE_SPEECH_KEY.")

    send_text(CHAT_ID, "🎙️ Iniciando: vou coletar, resumir por temas e enviar 1 áudio por fonte (voz masculina, estilo podcast)…")

    per_source_items = {}
    limit = CFG.get("limit_per_source", LIMIT_PER_SOURCE_DEF)

    # Coleta por feed
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

                # traduz SEMPRE para PT-BR (garante áudio 100% PT, CNN incluída)
                title_pt   = translate_to_pt(title)
                summary_pt = translate_to_pt(summary)

                per_source_items[source].append({
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "title_pt": title_pt,
                    "summary_pt": summary_pt,
                    "lang": lang
                })

                SEEN.add(iid)
                count += 1

    total_boletins = 0
    for feed in CFG["feeds"]:
        source_name = feed["name"]
        lang        = feed.get("lang", "pt")
        items       = per_source_items.get(source_name, [])
        if not items:
            continue

        names_en   = extract_english_names(items, lang)
        grouped    = group_by_topic_pt(items)
        script_pt  = build_audio_script_pt(source_name, grouped)

        # Texto com links por tema (útil para acompanhar)
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

        # Chunk por bytes (segurança) -> sintetiza cada chunk -> junta em 1 MP3
        text_chunks = chunk_script_by_bytes(script_pt, max_text_bytes=MAX_TEXT_BYTES)
        part_buffers = []
        for chunk_text in text_chunks:
            ssml = build_ssml_podcast_pt(
                chunk_text,
                voice=VOICE_PRIMARY,
                style=AZURE_STYLE,
                rate=AZURE_RATE,
                pitch_st=AZURE_PITCH_ST,
                names_en=names_en
            )
            # se ainda exceder, quebra por sentenças e sintetiza
            if len(ssml.encode("utf-8")) > MAX_SSML_BYTES:
                sentences = re.split(r'(?<=[\.\!\?])\s+', chunk_text)
                sub = []
                cur = ""
                subparts = []
                for s in sentences:
                    cand = (cur + " " + s).strip() if cur else s
                    tmp_ssml = build_ssml_podcast_pt(cand, voice=VOICE_PRIMARY, style=AZURE_STYLE,
                                                     rate=AZURE_RATE, pitch_st=AZURE_PITCH_ST, names_en=names_en)
                    if len(tmp_ssml.encode("utf-8")) <= MAX_SSML_BYTES:
                        cur = cand
                    else:
                        if cur:
                            subparts.append(cur)
                        cur = s
                if cur:
                    subparts.append(cur)

                for sp in subparts:
                    ssml_sub = build_ssml_podcast_pt(sp, voice=VOICE_PRIMARY, style=AZURE_STYLE,
                                                     rate=AZURE_RATE, pitch_st=AZURE_PITCH_ST, names_en=names_en)
                    audio_buf = azure_speech_tts_mp3(ssml_sub)
                    part_buffers.append(audio_buf)
            else:
                audio_buf = azure_speech_tts_mp3(ssml)
                part_buffers.append(audio_buf)

        if part_buffers:
            full_mp3 = join_mp3(part_buffers)
            send_audio(CHAT_ID, full_mp3,
                       title=f"{source_name} — Boletim",
                       performer=source_name,
                       filename=f"{source_name}_boletim.mp3")

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
