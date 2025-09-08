from telegram import Bot  # (se já existir no topo, não duplique)

def run():
    # MENSAGEM DE TESTE (confirma que TOKEN/CHAT_ID estão certos)
    bot = Bot(token=BOT_TOKEN)
    bot.send_message(chat_id=CHAT_ID, text="✅ Bot iniciou. Vou enviar as notícias…", parse_mode="HTML")
    # --- seu código existente continua daqui pra baixo ---
from google.cloud import texttospeech as tts  # << NOVO
import os, io  # (provavelmente já existe; se existir, mantém)
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
def make_tts(text, voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0):
    """
    Gera áudio com voz neural do Google (mais natural).
    Ajuste voice_name para 'pt-BR-Neural2-A/B/C/D' ou 'pt-BR-Wavenet-A/B/C/D'.
    speaking_rate ~1.02–1.05 e pitch ~+1.0st deixam mais "podcast".
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
