import logging

from odoo import api, models

from ..services import ai_service, speech_service

_logger = logging.getLogger(__name__)

PREFIX = "dcs_ai_voice_assistant."

# key -> (type, default)
CONFIG_KEYS = {
    "provider": ("str", "openai"),
    "api_key": ("secret", ""),
    "model": ("str", "gpt-5.6-luna"),
    "base_url": ("str", ""),
    "temperature": ("float", 0.2),
    "max_tokens": ("int", 2000),
    "history_messages": ("int", 30),
    "stt_provider": ("str", "openai"),
    "stt_model": ("str", "gpt-transcribe"),
    "tts_provider": ("str", "browser"),
    "tts_model": ("str", "gpt-4o-mini-tts"),
    "tts_voice": ("str", "alloy"),
    "speech_api_key": ("secret", ""),
    "language": ("str", "auto"),
    "voice_enabled": ("bool", True),
    "text_enabled": ("bool", True),
    "allow_create": ("bool", True),
    "allow_update": ("bool", True),
    "log_reads": ("bool", True),
    "show_floating_button": ("bool", True),
    "auto_speak": ("bool", True),
    "retention_days": ("int", 90),
    "extra_instructions": ("str", ""),
}

# What the browser may know. Keys and endpoints never leave the server.
PUBLIC_KEYS = ("voice_enabled", "text_enabled", "stt_provider", "tts_provider", "tts_voice", "language",
               "show_floating_button", "auto_speak", "allow_create", "allow_update")


class DcsAiProvider(models.AbstractModel):
    """Access to the assistant configuration and to the AI / speech providers.

    Configuration lives in ``ir.config_parameter`` (readable by administrators only), so
    reading it requires ``sudo()``. That is the only elevated read the module performs and
    the values (notably API keys) are only used server-side.
    """

    _name = "dcs.ai.provider"
    _description = "AI Voice Assistant provider access"

    # ------------------------------------------------------------- config
    @api.model
    def _get_config(self):
        params = self.env["ir.config_parameter"].sudo()
        config = {}
        for key, (kind, default) in CONFIG_KEYS.items():
            raw = params.get_str(PREFIX + key, None)
            config[key] = _cast(kind, raw, default)
        return config

    @api.model
    def _set_config(self, values):
        params = self.env["ir.config_parameter"].sudo()
        for key, value in values.items():
            kind, _default = CONFIG_KEYS[key]
            if kind == "bool":
                value = "1" if value else "0"
            elif value is False or value is None:
                value = ""
            params.set_str(PREFIX + key, str(value))

    @api.model
    def _get_public_config(self):
        config = self._get_config()
        public = {key: config[key] for key in PUBLIC_KEYS}
        public["configured"] = bool(config["model"] and (config["api_key"] or config["provider"] != "openai"))
        return public

    # ---------------------------------------------------------- providers
    @api.model
    def _llm(self, config=None):
        config = config or self._get_config()
        return ai_service.make_provider(
            config["provider"],
            api_key=config["api_key"],
            model=config["model"],
            base_url=config["base_url"] or None,
            temperature=config["temperature"],
            max_tokens=config["max_tokens"],
        )

    def _speech_key(self, config):
        return config["speech_api_key"] or (config["api_key"] if config["provider"] == "openai" else "")

    @api.model
    def _transcribe(self, audio_bytes, mimetype):
        config = self._get_config()
        if not config["voice_enabled"]:
            raise ai_service.AIProviderError("Voice input is disabled by the administrator.", code="config")
        cls = speech_service.STT_PROVIDERS.get(config["stt_provider"])
        if not cls:
            raise ai_service.AIProviderError("Speech recognition runs in the browser with the current "
                                             "settings.", code="config")
        language = None if config["language"] == "auto" else config["language"]
        return cls(self._speech_key(config), config["stt_model"], language=language).transcribe(
            audio_bytes, mimetype)

    @api.model
    def _synthesize(self, text):
        config = self._get_config()
        if not config["voice_enabled"]:
            raise ai_service.AIProviderError("Voice output is disabled by the administrator.", code="config")
        cls = speech_service.TTS_PROVIDERS.get(config["tts_provider"])
        if not cls:
            raise ai_service.AIProviderError("Speech output runs in the browser with the current settings.",
                                             code="config")
        return cls(self._speech_key(config), config["tts_model"], voice=config["tts_voice"]).synthesize(text)


def _cast(kind, raw, default):
    if raw in (None, False):
        return default
    if kind == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(float(raw))
        except ValueError:
            return default
    if kind == "float":
        try:
            return float(raw)
        except ValueError:
            return default
    if raw == "" and kind == "str":
        return default
    return raw
