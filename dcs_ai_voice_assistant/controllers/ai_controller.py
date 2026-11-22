import base64
import binascii
import logging

from odoo import _, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..services.ai_service import AIProviderError
from ..services.speech_service import MAX_AUDIO_BYTES

_logger = logging.getLogger(__name__)

GROUP = "dcs_ai_voice_assistant.group_ai_user"


def _env(context=None):
    """The user's environment, with the companies currently selected in the web client.

    ``allowed_company_ids`` is checked against the user's companies by ``env.companies``;
    an unauthorized company raises AccessError.
    """
    if not request.env.user.has_group(GROUP):
        raise AccessError(_("You are not allowed to use the AI assistant."))
    context = context or {}
    overrides = {}
    company_ids = context.get("allowed_company_ids")
    if isinstance(company_ids, list) and company_ids and all(isinstance(c, int) for c in company_ids):
        overrides["allowed_company_ids"] = company_ids
    for key in ("lang", "tz"):
        if isinstance(context.get(key), str):
            overrides[key] = context[key]
    env = request.env(context=dict(request.env.context, **overrides))
    env.companies  # noqa: B018 - validates allowed_company_ids
    return env


def _conversation(env, conversation_id):
    Conversation = env["dcs.ai.conversation"]
    if conversation_id:
        conv = Conversation.browse(int(conversation_id)).exists()
        if conv and conv.active:
            conv.check_access("write")
            if conv.user_id != env.user:
                raise AccessError(_("This conversation belongs to another user."))
            return conv
    return Conversation._get_current()


class DcsAiController(http.Controller):

    @http.route("/dcs_ai/init", type="jsonrpc", auth="user")
    def init(self, context=None):
        env = _env(context)
        conv = env["dcs.ai.conversation"]._get_current(create=False)
        pending = conv._pending_actions() if conv else env["dcs.ai.action"]
        return {
            "config": env["dcs.ai.provider"]._get_public_config(),
            "user": {"name": env.user.name, "lang": env.user.lang or "en_US"},
            "conversation_id": conv.id if conv else False,
            "messages": conv._ui_messages() if conv else [],
            "pending_action_ids": pending.ids,
        }

    @http.route("/dcs_ai/chat", type="jsonrpc", auth="user")
    def chat(self, message, conversation_id=None, input_mode="text", context=None):
        env = _env(context)
        conv = _conversation(env, conversation_id)
        return env["dcs.ai.executor"].process(message, conversation=conv,
                                              input_mode="voice" if input_mode == "voice" else "text")

    @http.route("/dcs_ai/action/decide", type="jsonrpc", auth="user")
    def decide(self, action_ids, approve, context=None):
        env = _env(context)
        ids = [int(i) for i in (action_ids if isinstance(action_ids, list) else [action_ids])]
        return env["dcs.ai.executor"].decide(ids, bool(approve))

    @http.route("/dcs_ai/conversation/clear", type="jsonrpc", auth="user")
    def clear(self, conversation_id=None, context=None):
        env = _env(context)
        if conversation_id:
            conv = env["dcs.ai.conversation"].browse(int(conversation_id)).exists()
            if conv and conv.user_id == env.user:
                conv.action_clear()
        return {"conversation_id": False, "messages": []}

    @http.route("/dcs_ai/transcribe", type="jsonrpc", auth="user")
    def transcribe(self, audio, mimetype="audio/webm", context=None):
        env = _env(context)
        try:
            data = base64.b64decode(audio or "", validate=True)
        except (binascii.Error, ValueError) as e:
            raise UserError(_("The recording could not be read.")) from e
        if len(data) > MAX_AUDIO_BYTES:
            raise UserError(_("The recording is too long."))
        try:
            return {"text": env["dcs.ai.provider"]._transcribe(data, mimetype)}
        except AIProviderError as e:
            _logger.warning("Transcription failed (%s): %s", e.code, e.detail or e.user_message)
            return {"error": e.user_message}

    @http.route("/dcs_ai/speak", type="jsonrpc", auth="user")
    def speak(self, text, context=None):
        env = _env(context)
        try:
            audio, mimetype = env["dcs.ai.provider"]._synthesize(text)
        except AIProviderError as e:
            _logger.warning("Speech synthesis failed (%s): %s", e.code, e.detail or e.user_message)
            return {"error": e.user_message}
        return {"audio": base64.b64encode(audio).decode(), "mimetype": mimetype}
