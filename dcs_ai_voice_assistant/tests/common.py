import json
from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user

from ..services import action_service
from ..services.ai_service import LLMResponse


class FakeProvider:
    """Replays scripted model responses and records what it was sent."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.model = "fake-model"

    def chat(self, instructions, messages, tools=None, state=None):
        self.calls.append({"instructions": instructions, "messages": [dict(m) for m in messages],
                           "tools": [t["name"] for t in tools or []]})
        if not self.script:
            return LLMResponse(content="(no more scripted answers)")
        item = self.script.pop(0)
        if callable(item):
            item = item(messages)
        return item


def tool_call(name, call_id="call_1", **arguments):
    return LLMResponse(tool_calls=[{"id": call_id, "name": name, "arguments": json.dumps(arguments)}])


def answer(text):
    return LLMResponse(content=text)


class AiAssistantCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["dcs.ai.provider"]._set_config({
            "api_key": "sk-test", "model": "fake-model", "allow_create": True, "allow_update": True,
            "log_reads": True, "text_enabled": True, "voice_enabled": True,
        })
        groups = "base.group_user,base.group_partner_manager"
        if "sale.order" in cls.env:
            groups += ",sales_team.group_sale_salesman_all_leads"
        if "stock.quant" in cls.env:
            groups += ",stock.group_stock_user"
        cls.user = new_test_user(cls.env, login="ai_user", groups=groups, name="AI Test User")
        cls.restricted = new_test_user(cls.env, login="ai_restricted", groups="base.group_user",
                                       name="Restricted User")
        cls.partner = cls.env["res.partner"].create({"name": "Azure Interior Test", "email": "azure@example.com",
                                                     "phone": "+1 555 0100"})

    def uenv(self, user=None):
        return self.env(user=user or self.user, context=dict(self.env.context, tz="UTC"))

    def ctx(self, user=None, **config):
        env = self.uenv(user)
        conf = env["dcs.ai.provider"]._get_config()
        conf.update(config)
        conversation = env["dcs.ai.conversation"].create({})
        return action_service.ToolContext(env, conversation, conf, user_text="test")

    @contextmanager
    def fake_llm(self, script):
        provider = FakeProvider(script)
        Provider = self.registry["dcs.ai.provider"]
        with patch.object(Provider, "_llm", lambda self_, config=None: provider):
            yield provider
