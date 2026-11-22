import json

from odoo.tests import tagged

from ..services.ai_service import AIProviderError
from .common import AiAssistantCase, answer, tool_call


@tagged("post_install", "-at_install", "dcs_ai")
class TestExecutor(AiAssistantCase):

    def process(self, text, user=None, **kw):
        return self.uenv(user)["dcs.ai.executor"].process(text, **kw)

    def test_question_answered_from_tool_results(self):
        script = [
            tool_call("search_records", object="contact",
                      filters=[{"field": "name", "operator": "ilike", "value": "Azure Interior Test"}]),
            lambda messages: answer("Found %s." % json.loads(messages[-1]["content"])["rows"][0]["name"]),
        ]
        with self.fake_llm(script) as llm:
            res = self.process("Find Azure Interior Test")
        msgs = res["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(msgs[1]["content"], "Found Azure Interior Test.")
        self.assertEqual(msgs[1]["display"][0]["type"], "table")
        # second model call received the real tool result
        tool_msg = llm.calls[1]["messages"][-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("azure@example.com", tool_msg["content"])
        self.assertIn("search_records", llm.calls[0]["tools"])

    def test_create_with_confirmation_by_voice_reply(self):
        script = [
            tool_call("prepare_create_record", object="contact",
                      values={"name": "ABC Technologies", "partner_role": "customer"}),
            answer("I can create a customer named ABC Technologies. Should I proceed?"),
        ]
        with self.fake_llm(script):
            res = self.process("Create a new customer named ABC Technologies", input_mode="voice")
        reply = res["messages"][-1]
        self.assertEqual(reply["actions"][0]["state"], "pending")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "ABC Technologies")]),
                         "nothing is created before confirmation")
        # "Yes." is handled without calling the model at all
        with self.fake_llm([]) as llm:
            res = self.process("Yes.", input_mode="voice")
        self.assertFalse(llm.calls)
        partner = self.env["res.partner"].search([("name", "=", "ABC Technologies")])
        self.assertTrue(partner)
        if "customer_rank" in partner._fields:
            self.assertGreaterEqual(partner.customer_rank, 1)
        self.assertIn("Created contact", res["messages"][-1]["content"])
        self.assertEqual(res["updated_actions"][0]["state"], "done")
        # the model is told what really happened on the next turn
        with self.fake_llm([answer("ok")]) as llm:
            self.process("thanks")
        history = llm.calls[0]["messages"]
        self.assertTrue(any("[Executed successfully]" in (m.get("content") or "") for m in history))

    def test_no_cancels(self):
        with self.fake_llm([tool_call("prepare_create_record", object="contact", values={"name": "Never Ltd"}),
                            answer("Confirm?")]):
            self.process("create contact Never Ltd")
        with self.fake_llm([]):
            res = self.process("no")
        self.assertEqual(res["updated_actions"][0]["state"], "cancelled")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Never Ltd")]))

    def test_button_decision(self):
        with self.fake_llm([tool_call("prepare_update_record", object="contact", record_id=self.partner.id,
                                      values={"phone": "111"}), answer("Confirm?")]):
            res = self.process("change Azure's phone to 111")
        action_id = res["messages"][-1]["actions"][0]["id"]
        res = self.uenv()["dcs.ai.executor"].decide([action_id], True)
        self.assertEqual(self.partner.phone, "111")
        self.assertEqual(res["updated_actions"][0]["state"], "done")
        # deciding twice does nothing
        res = self.uenv()["dcs.ai.executor"].decide([action_id], True)
        self.assertIn("no longer pending", res["messages"][-1]["content"])

    def test_model_cannot_claim_success_without_odoo(self):
        """Even if the model says 'created', nothing exists unless a confirmed action ran."""
        with self.fake_llm([answer("The customer Ghost Inc has been created successfully.")]):
            self.process("create customer Ghost Inc")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Ghost Inc")]))
        self.assertFalse(self.env["dcs.ai.action"].search([("summary", "ilike", "Ghost")]))

    def test_follow_up_uses_history(self):
        with self.fake_llm([tool_call("search_records", object="contact",
                                      filters=[{"field": "name", "operator": "ilike", "value": "Azure"}]),
                            answer("Here they are.")]):
            self.process("Show me Azure contacts")
        with self.fake_llm([answer("Their phone is +1 555 0100.")]) as llm:
            self.process("What about their phone?")
        history = llm.calls[0]["messages"]
        self.assertEqual(history[0]["role"], "user")
        self.assertTrue(any(m["role"] == "tool" and "+1 555 0100" in m["content"] for m in history))
        self.assertEqual(history[-1]["content"], "What about their phone?")

    def test_provider_error_is_reported(self):
        def boom(messages):
            raise AIProviderError("The AI service is temporarily unavailable.", code="unavailable")
        with self.fake_llm([boom]):
            res = self.process("hello")
        self.assertTrue(res["messages"][-1]["is_error"])
        self.assertIn("temporarily unavailable", res["messages"][-1]["content"])

    def test_tool_loop_is_bounded(self):
        script = [tool_call("search_records", call_id="c%d" % i, object="contact") for i in range(20)]
        with self.fake_llm(script) as llm:
            res = self.process("loop forever")
        self.assertLessEqual(len(llm.calls), 8)
        self.assertIn("reasonable number of steps", res["messages"][-1]["content"])

    def test_disabled_text_chat(self):
        self.env["dcs.ai.provider"]._set_config({"text_enabled": False})
        with self.assertRaises(Exception):
            self.process("hello")
        self.env["dcs.ai.provider"]._set_config({"text_enabled": True})

    def test_write_tools_hidden_when_disabled(self):
        self.env["dcs.ai.provider"]._set_config({"allow_create": False, "allow_update": False})
        with self.fake_llm([answer("hi")]) as llm:
            self.process("hello")
        self.assertFalse([t for t in llm.calls[0]["tools"] if t.startswith("prepare_")])
        self.env["dcs.ai.provider"]._set_config({"allow_create": True, "allow_update": True})

    def test_clear_conversation(self):
        with self.fake_llm([answer("hi")]):
            res = self.process("hello")
        conv = self.env["dcs.ai.conversation"].browse(res["conversation_id"])
        conv.with_user(self.user).action_clear()
        self.assertFalse(conv.active)
        with self.fake_llm([answer("hi again")]):
            res2 = self.process("hello")
        self.assertNotEqual(res2["conversation_id"], conv.id)
