"""The action execution engine: user text -> model -> validated tools -> answer.

Workflow (one call of :meth:`process`):

1. store the user's message (voice is already transcribed by then);
2. if a proposal is waiting and the user simply says yes/no, execute or cancel it directly,
   without asking the model (the model never decides that a change is confirmed);
3. otherwise send the history + tool definitions to the model;
4. run each requested tool with the user's environment (``action_service.dispatch``),
   return the real results to the model, repeat until it answers;
5. store the answer with the display blocks produced by the tools and the proposals.
"""
import json
import logging
import re
import uuid

from odoo import _, api, models
from odoo.exceptions import AccessError, UserError

from ..services import action_service, intent_service
from ..services.ai_service import AIProviderError

_logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 8
MAX_TOOL_RESULT_CHARS = 14000
MAX_INPUT_CHARS = 4000
MAX_DISPLAYS = 4

_YES = {
    "yes", "y", "yeah", "yep", "yup", "sure", "ok", "okay", "k", "confirm", "confirmed", "proceed",
    "go ahead", "do it", "please do", "yes please", "correct", "right", "absolutely", "of course",
    "go", "approve", "approved", "sounds good", "looks good", "perfect", "create it", "save it",
    "haan", "haa", "han", "ha", "ji", "ji haan", "haan ji", "theek hai", "thik hai", "kar do", "karo",
    "はい", "お願いします", "sí", "si", "oui", "ja", "da",
}
_NO = {
    "no", "n", "nope", "nah", "cancel", "stop", "dont", "do not", "abort", "never mind", "nevermind",
    "forget it", "no thanks", "no thank you", "not now", "reject", "nahi", "nahin", "mat karo", "rehne do",
    "いいえ", "キャンセル", "non", "nein", "niet",
}
_STARTERS_YES = ("yes", "yeah", "yep", "sure", "ok", "okay", "confirm", "proceed", "go ahead", "haan", "please do")
_STARTERS_NO = ("no", "nope", "cancel", "don't", "dont", "do not", "stop", "abort", "nahi")
_CHANGE_WORDS = re.compile(r"\b(but|change|instead|except|also|wait|actually|rather|modify|update|make it|"
                           r"different|another|not|wrong)\b")


def classify_reply(text):
    """Return 'yes', 'no' or None for a reply to a pending proposal."""
    norm = re.sub(r"[^\w\s'぀-ヿ]", " ", (text or "").lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    if not norm:
        return None
    if norm in _YES:
        return "yes"
    if norm in _NO:
        return "no"
    words = norm.split(" ")
    rest = " ".join(words[1:])
    if len(words) <= 6 and not _CHANGE_WORDS.search(rest):
        if any(norm == s or norm.startswith(s + " ") for s in _STARTERS_NO):
            return "no"
        if any(norm == s or norm.startswith(s + " ") for s in _STARTERS_YES):
            return "yes"
    return None


class DcsAiExecutor(models.AbstractModel):
    _name = "dcs.ai.executor"
    _description = "AI Voice Assistant execution engine"

    # ------------------------------------------------------------ entry
    @api.model
    def process(self, text, conversation=None, input_mode="text"):
        if self.env.su:
            raise AccessError(_("The AI assistant cannot be used in superuser mode."))
        config = self.env["dcs.ai.provider"]._get_config()
        if input_mode == "voice" and not config["voice_enabled"]:
            raise UserError(_("Voice commands are disabled by the administrator."))
        if input_mode != "voice" and not config["text_enabled"]:
            raise UserError(_("Text chat is disabled by the administrator."))
        text = (text or "").strip()
        if not text:
            raise UserError(_("Please type or say something."))
        text = text[:MAX_INPUT_CHARS]

        Conversation = self.env["dcs.ai.conversation"]
        conversation = conversation or Conversation._get_current()
        if conversation.user_id != self.env.user:
            raise AccessError(_("This conversation belongs to another user."))
        Message = self.env["dcs.ai.message"]
        user_msg = Message.create({"conversation_id": conversation.id, "role": "user", "content": text,
                                   "input_mode": "voice" if input_mode == "voice" else "text"})
        conversation._touch(title_from=text)
        created = user_msg

        pending = conversation._pending_actions()
        if pending:
            decision = classify_reply(text)
            if decision:
                created |= self._decide(conversation, pending, decision == "yes", via="message")
                return self._result(conversation, created, pending)

        created |= self._run_model(conversation, text, config, voice=input_mode == "voice")
        return self._result(conversation, created)

    @api.model
    def decide(self, action_ids, approve):
        actions = self.env["dcs.ai.action"].browse(action_ids).exists()
        if not actions:
            raise UserError(_("This proposal no longer exists."))
        actions.check_access("read")
        if actions.filtered(lambda a: a.user_id != self.env.user):
            raise AccessError(_("You can only confirm your own assistant operations."))
        conversation = actions[0].conversation_id
        if not conversation:
            raise UserError(_("This proposal no longer belongs to a conversation."))
        valid = actions._filter_valid()
        if not valid:
            msg = self._assistant(conversation, _("This proposal is no longer pending (it was already handled, "
                                                  "replaced or expired). Ask me again if needed."))
            return self._result(conversation, msg, actions)
        msg = self._decide(conversation, valid, approve, via="button")
        return self._result(conversation, msg, actions)

    # ---------------------------------------------------------- helpers
    def _result(self, conversation, messages, actions=None):
        return {
            "conversation_id": conversation.id,
            "messages": [m._to_ui() for m in messages.filtered("visible").sorted("id")],
            "updated_actions": [a._to_ui() for a in (actions or self.env["dcs.ai.action"])],
        }

    def _assistant(self, conversation, content, display=None, is_error=False, llm_content=None):
        return self.env["dcs.ai.message"].create({
            "conversation_id": conversation.id, "role": "assistant", "content": content,
            "llm_content": llm_content, "display": display or [], "is_error": is_error,
            "input_mode": "system" if is_error else "text",
        })

    def _decide(self, conversation, actions, approve, via):
        if approve:
            actions._execute(via=via)
        else:
            actions._cancel(reason=_("Cancelled by the user"))
            actions.sudo().write({"decided_via": via})
        lines, llm_lines, display = [], [], []
        for action in actions:
            if action.state == "done":
                lines.append(_("✓ %s.", _past_tense(action.summary)))
                llm_lines.append("[Executed successfully] %s -> %s id %s (%s)" % (
                    action.summary, action.model_name, action.res_id, action.record_name))
            elif action.state == "failed":
                lines.append(_("I could not complete \"%(s)s\": %(e)s", s=action.summary, e=action.error_message))
                llm_lines.append("[Failed, nothing saved] %s: %s" % (action.summary, action.error_message))
            elif action.state == "cancelled":
                lines.append(_("Okay, I cancelled \"%s\". Nothing was changed.", action.summary))
                llm_lines.append("[Cancelled by user] %s" % action.summary)
            else:
                lines.append(_("\"%s\" is no longer pending.", action.summary))
                llm_lines.append("[Not executed: %s] %s" % (action.state, action.summary))
            display.append({"type": "action_result", "action": action._to_ui()})
        failed = any(a.state == "failed" for a in actions)
        return self._assistant(conversation, "\n".join(lines), display=display, is_error=failed,
                               llm_content="\n".join(llm_lines))

    def _run_model(self, conversation, text, config, voice=False):
        Message = self.env["dcs.ai.message"]
        provider = self.env["dcs.ai.provider"]._llm(config)
        instructions = intent_service.build_system_prompt(self.env, config, voice=voice)
        tools = intent_service.build_tools(self.env, config)
        history = conversation._llm_history(config["history_messages"])
        ctx = action_service.ToolContext(self.env, conversation, config, user_text=text)
        state, usage = {}, {"input": 0, "output": 0}
        content = None
        created = Message.browse()
        try:
            for _round in range(MAX_TOOL_ROUNDS):
                response = provider.chat(instructions, history, tools, state)
                state = response.state
                usage["input"] += response.usage.get("input", 0)
                usage["output"] += response.usage.get("output", 0)
                if not response.tool_calls:
                    content = response.content
                    break
                calls = [{"id": c.get("id") or "call_%s" % uuid.uuid4().hex[:12], "name": c.get("name") or "",
                          "arguments": c.get("arguments") or "{}"} for c in response.tool_calls]
                step = Message.create({"conversation_id": conversation.id, "role": "assistant",
                                       "content": response.content or "", "tool_calls": calls})
                history.append(step._to_llm())
                for call in calls:
                    result = action_service.dispatch(ctx, call["name"], call["arguments"])
                    if not result.get("ok"):
                        _logger.info("AI tool %s returned %s: %s", call["name"], result.get("error"),
                                     result.get("message"))
                    tool_msg = Message.create({
                        "conversation_id": conversation.id, "role": "tool", "tool_call_id": call["id"],
                        "tool_name": call["name"], "content": _compact(result),
                    })
                    history.append(tool_msg._to_llm())
            else:
                content = _("I could not finish this request in a reasonable number of steps. Could you "
                            "rephrase it or split it into smaller questions?")
        except AIProviderError as e:
            _logger.warning("AI provider error (%s): %s %s", e.code, e.user_message, e.detail or "")
            created |= self._assistant(conversation, e.user_message, display=ctx.displays[-MAX_DISPLAYS:],
                                       is_error=True)
            ctx.pending_actions._cancel(reason=_("The AI service failed before the proposal was presented"))
            conversation._touch(usage=usage)
            return created
        if not content:
            content = _("Done.") if ctx.displays else _("I have no answer for that.")
        final = self._assistant(conversation, content, display=ctx.displays[-MAX_DISPLAYS:])
        if ctx.pending_actions:
            live = ctx.pending_actions.filtered(lambda a: a.state == "pending")
            live.sudo().write({"message_id": final.id})
        conversation._touch(usage=usage)
        return created | final


_PAST = [
    ("Create a draft", "Created a draft"), ("Create a quotation", "Created a quotation"),
    ("Create a request", "Created a request"), ("Create", "Created"), ("Update", "Updated"),
    ("Confirm", "Confirmed"), ("Post", "Posted"), ("Mark as", "Marked as"), ("Schedule", "Scheduled"),
    ("Set on-hand", "Set on-hand"), ("Add lines to", "Added lines to"),
    ("Change quantities on", "Changed quantities on"), ("Remove lines from", "Removed lines from"),
]


def _past_tense(summary):
    """'Create contact "ABC"' -> 'Created contact "ABC"' (English summaries only)."""
    for present, past in _PAST:
        if summary.startswith(present + " "):
            return past + summary[len(present):]
    return _("Done: %s", summary)


def _compact(result, limit=MAX_TOOL_RESULT_CHARS):
    """JSON for the model, shortening long lists instead of cutting the JSON in half."""
    def dump(value):
        return json.dumps(value, default=str, ensure_ascii=False)

    text = dump(result)
    if len(text) <= limit:
        return text
    result = dict(result)
    for key in ("rows", "groups", "products", "top_customers", "top_products", "fields", "lines"):
        items = result.get(key)
        while isinstance(items, list) and len(items) > 1 and len(dump(result)) > limit:
            items = items[: len(items) // 2]
            result[key] = items
            result["truncated"] = "Only part of the rows is included; the user sees the full table."
    text = dump(result)
    return text if len(text) <= limit else text[:limit]
