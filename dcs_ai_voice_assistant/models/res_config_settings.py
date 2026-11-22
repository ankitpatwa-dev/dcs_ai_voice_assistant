from odoo import _, api, fields, models
from odoo.exceptions import UserError

from ..services import ai_service, speech_service

LANGUAGES = [
    ("auto", "Same as the user / detect"), ("en", "English"), ("hi", "Hindi"), ("ja", "Japanese"),
    ("fr", "French"), ("de", "German"), ("es", "Spanish"), ("ar", "Arabic"), ("it", "Italian"),
    ("pt", "Portuguese"), ("nl", "Dutch"), ("zh", "Chinese"), ("gu", "Gujarati"), ("mr", "Marathi"),
    ("ta", "Tamil"), ("bn", "Bengali"),
]

# settings field -> config key
FIELD_MAP = {
    "dcs_ai_provider": "provider",
    "dcs_ai_model": "model",
    "dcs_ai_base_url": "base_url",
    "dcs_ai_temperature": "temperature",
    "dcs_ai_max_tokens": "max_tokens",
    "dcs_ai_history_messages": "history_messages",
    "dcs_ai_stt_provider": "stt_provider",
    "dcs_ai_stt_model": "stt_model",
    "dcs_ai_tts_provider": "tts_provider",
    "dcs_ai_tts_model": "tts_model",
    "dcs_ai_tts_voice": "tts_voice",
    "dcs_ai_language": "language",
    "dcs_ai_voice_enabled": "voice_enabled",
    "dcs_ai_text_enabled": "text_enabled",
    "dcs_ai_allow_create": "allow_create",
    "dcs_ai_allow_update": "allow_update",
    "dcs_ai_log_reads": "log_reads",
    "dcs_ai_show_floating_button": "show_floating_button",
    "dcs_ai_auto_speak": "auto_speak",
    "dcs_ai_retention_days": "retention_days",
    "dcs_ai_extra_instructions": "extra_instructions",
}


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    dcs_ai_provider = fields.Selection(selection=lambda self: ai_service.get_provider_selection(),
                                       string="AI provider")
    dcs_ai_api_key = fields.Char("API key", help="Stored on the server only. Leave empty to keep the current key.")
    dcs_ai_api_key_set = fields.Boolean("API key saved", readonly=True)
    dcs_ai_model = fields.Char("AI model")
    dcs_ai_base_url = fields.Char("API endpoint",
                                  help="Leave empty for OpenAI. For OpenAI-compatible providers, the base URL "
                                       "ending in /v1 (e.g. https://openrouter.ai/api/v1).")
    dcs_ai_temperature = fields.Float("Temperature", digits=(3, 2))
    dcs_ai_max_tokens = fields.Integer("Maximum response tokens")
    dcs_ai_history_messages = fields.Integer("Conversation memory (messages)")
    dcs_ai_stt_provider = fields.Selection(selection=lambda self: speech_service.get_stt_selection(),
                                           string="Speech-to-text")
    dcs_ai_stt_model = fields.Char("Transcription model")
    dcs_ai_tts_provider = fields.Selection(selection=lambda self: speech_service.get_tts_selection(),
                                           string="Text-to-speech")
    dcs_ai_tts_model = fields.Char("Speech model")
    dcs_ai_tts_voice = fields.Selection(speech_service.OPENAI_VOICES, string="Speech voice")
    dcs_ai_speech_api_key = fields.Char("Speech API key",
                                        help="OpenAI key for transcription/speech when the AI provider is not "
                                             "OpenAI. Leave empty to reuse the AI provider key.")
    dcs_ai_speech_api_key_set = fields.Boolean(readonly=True)
    dcs_ai_language = fields.Selection(LANGUAGES, string="Language")
    dcs_ai_voice_enabled = fields.Boolean("Voice commands")
    dcs_ai_text_enabled = fields.Boolean("Text chat")
    dcs_ai_allow_create = fields.Boolean("Record creation")
    dcs_ai_allow_update = fields.Boolean("Record updates")
    dcs_ai_log_reads = fields.Boolean("Log data queries")
    dcs_ai_show_floating_button = fields.Boolean("Floating button")
    dcs_ai_auto_speak = fields.Boolean("Read answers aloud after voice commands")
    dcs_ai_retention_days = fields.Integer("Keep cleared conversations (days)")
    dcs_ai_extra_instructions = fields.Text("Additional instructions")

    @api.model
    def get_values(self):
        res = super().get_values()
        config = self.env["dcs.ai.provider"]._get_config()
        for fname, key in FIELD_MAP.items():
            res[fname] = config[key]
        res["dcs_ai_api_key"] = False
        res["dcs_ai_speech_api_key"] = False
        res["dcs_ai_api_key_set"] = bool(config["api_key"])
        res["dcs_ai_speech_api_key_set"] = bool(config["speech_api_key"])
        return res

    def set_values(self):
        super().set_values()
        if not self.env.user.has_group("base.group_system"):
            return
        values = {key: self[fname] for fname, key in FIELD_MAP.items()}
        values["temperature"] = min(max(values["temperature"] or 0.0, 0.0), 2.0)
        values["max_tokens"] = min(max(values["max_tokens"] or 2000, 200), 32000)
        values["history_messages"] = min(max(values["history_messages"] or 30, 4), 200)
        if self.dcs_ai_api_key:
            values["api_key"] = self.dcs_ai_api_key.strip()
        if self.dcs_ai_speech_api_key:
            values["speech_api_key"] = self.dcs_ai_speech_api_key.strip()
        self.env["dcs.ai.provider"]._set_config(values)

    def action_dcs_ai_clear_api_keys(self):
        self.env["dcs.ai.provider"]._set_config({"api_key": "", "speech_api_key": ""})
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_dcs_ai_test_connection(self):
        self.ensure_one()
        stored = self.env["dcs.ai.provider"]._get_config()
        provider = ai_service.make_provider(
            self.dcs_ai_provider or stored["provider"],
            api_key=(self.dcs_ai_api_key or stored["api_key"] or "").strip(),
            model=self.dcs_ai_model or stored["model"],
            base_url=self.dcs_ai_base_url or None,
            temperature=self.dcs_ai_temperature,
            max_tokens=1000,
        )
        try:
            answer = provider.test_connection()
        except ai_service.AIProviderError as e:
            raise UserError(_("Connection failed: %s", e.user_message)) from e
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "success",
                "title": _("AI provider connected"),
                "message": _("Model %(model)s answered: %(answer)s",
                             model=provider.model, answer=(answer or "")[:80]),
                "sticky": False,
            },
        }
