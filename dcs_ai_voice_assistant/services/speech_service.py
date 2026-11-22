"""Speech-to-text and text-to-speech providers.

Two kinds of provider exist for each direction:

* ``browser`` - handled entirely in the user's browser (Web Speech API). No audio leaves
  the browser for the Odoo server; nothing is implemented here.
* server providers (``openai`` here) - the browser sends audio / text to Odoo, which calls
  the provider with the key stored on the server. The key is never sent to the browser.
"""
import json
import logging

import requests

from .ai_service import AIProviderError, _error_detail

_logger = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_TTS_CHARS = 4000

OPENAI_VOICES = [
    ("alloy", "Alloy"), ("ash", "Ash"), ("ballad", "Ballad"), ("coral", "Coral"),
    ("echo", "Echo"), ("fable", "Fable"), ("nova", "Nova"), ("onyx", "Onyx"),
    ("sage", "Sage"), ("shimmer", "Shimmer"), ("verse", "Verse"),
]

_EXTENSIONS = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/aac": "aac",
}


def _request(method, url, api_key, timeout, **kwargs):
    headers = kwargs.pop("headers", {})
    if api_key:
        headers["Authorization"] = "Bearer %s" % api_key
    try:
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
    except requests.exceptions.Timeout as e:
        raise AIProviderError("The speech service did not answer in time.", code="timeout") from e
    except requests.exceptions.RequestException as e:
        raise AIProviderError("Could not reach the speech service.", code="network",
                              detail=str(e)) from e
    if resp.status_code in (401, 403):
        raise AIProviderError("The speech service refused the API key.", code="auth",
                              detail=_error_detail(resp))
    if resp.status_code == 429:
        raise AIProviderError("The speech service is rate limiting requests. Try again shortly.",
                              code="rate_limit", detail=_error_detail(resp))
    if resp.status_code >= 400:
        raise AIProviderError("The speech service returned an error (HTTP %s): %s"
                              % (resp.status_code, _error_detail(resp)[:200]),
                              code="bad_request" if resp.status_code < 500 else "unavailable")
    return resp


class OpenAITranscriber:
    code = "openai"
    label = "OpenAI transcription"

    def __init__(self, api_key, model, base_url=None, language=None, timeout=(10, 60)):
        self.api_key = api_key
        self.model = model or "gpt-transcribe"
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.language = (language or "").split("_")[0].lower() or None
        self.timeout = timeout

    def transcribe(self, audio_bytes, mimetype="audio/webm"):
        if not self.api_key:
            raise AIProviderError("No API key is configured for speech-to-text.", code="config")
        if not audio_bytes:
            raise AIProviderError("No audio was received.", code="bad_request")
        if len(audio_bytes) > MAX_AUDIO_BYTES:
            raise AIProviderError("The recording is too long. Please keep voice commands under a "
                                  "few minutes.", code="bad_request")
        base_type = (mimetype or "audio/webm").split(";")[0].strip().lower()
        filename = "speech.%s" % _EXTENSIONS.get(base_type, "webm")
        data = {"model": self.model, "response_format": "json"}
        if self.language:
            data["language"] = self.language
        resp = _request("POST", "%s/audio/transcriptions" % self.base_url, self.api_key, self.timeout,
                        data=data, files={"file": (filename, audio_bytes, base_type)})
        try:
            return (resp.json().get("text") or "").strip()
        except ValueError:
            return (resp.text or "").strip()


class OpenAISpeaker:
    code = "openai"
    label = "OpenAI speech"

    def __init__(self, api_key, model, voice="alloy", base_url=None, timeout=(10, 60)):
        self.api_key = api_key
        self.model = model or "gpt-4o-mini-tts"
        self.voice = voice or "alloy"
        self.base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        self.timeout = timeout

    def synthesize(self, text):
        """Return ``(audio_bytes, mimetype)``."""
        if not self.api_key:
            raise AIProviderError("No API key is configured for text-to-speech.", code="config")
        text = (text or "").strip()[:MAX_TTS_CHARS]
        if not text:
            raise AIProviderError("Nothing to read aloud.", code="bad_request")
        payload = {"model": self.model, "voice": self.voice, "input": text, "response_format": "mp3"}
        resp = _request("POST", "%s/audio/speech" % self.base_url, self.api_key, self.timeout,
                        headers={"Content-Type": "application/json"}, data=json.dumps(payload))
        return resp.content, "audio/mpeg"


STT_PROVIDERS = {"openai": OpenAITranscriber}
TTS_PROVIDERS = {"openai": OpenAISpeaker}


def get_stt_selection():
    return [("browser", "Browser (Web Speech API, free)")] + [
        (code, cls.label) for code, cls in STT_PROVIDERS.items()]


def get_tts_selection():
    return [("browser", "Browser (speech synthesis, free)")] + [
        (code, cls.label) for code, cls in TTS_PROVIDERS.items()]
