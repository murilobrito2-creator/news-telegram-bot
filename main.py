from google.cloud import texttospeech as tts
import os, io

def make_tts(text, voice_name="pt-BR-Neural2-B", speaking_rate=1.03, pitch_semitones=+1.0):
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
