from odoo.exceptions import AccessError
from odoo.tests import tagged

from ..services import action_service
from .common import AiAssistantCase, answer, tool_call


@tagged("post_install", "-at_install", "dcs_ai")
class TestSecurity(AiAssistantCase):

    def test_user_without_app_rights_cannot_read(self):
        if "sale.order" not in self.env:
            self.skipTest("Sales not installed")
        ctx = self.ctx(user=self.restricted)
        self.assertNotIn("sale_order", action_service.available_objects(ctx.env))
        res = action_service.dispatch(ctx, "search_records", {"object": "sale_order"})
        self.assertEqual(res["error"], "permission_denied")
        res = action_service.dispatch(ctx, "aggregate_records", {"object": "sale_order", "group_by": ["partner_id"]})
        self.assertEqual(res["error"], "permission_denied")
        res = action_service.dispatch(ctx, "business_summary", {"topic": "sales"})
        self.assertFalse(res["ok"])

    def test_accounting_hidden_from_non_accountants(self):
        if "account.move" not in self.env:
            self.skipTest("Invoicing not installed")
        ctx = self.ctx(user=self.restricted)
        res = action_service.dispatch(ctx, "business_summary", {"topic": "receivables"})
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"], "permission_denied")

    def test_create_refused_without_create_right(self):
        if "crm.lead" not in self.env:
            self.skipTest("CRM not installed")
        res = action_service.dispatch(self.ctx(user=self.restricted), "prepare_create_record", {
            "object": "lead", "values": {"name": "Sneaky lead"}})
        self.assertIn(res["error"], ("permission_denied", "unknown_object"))

    def test_record_rules_and_multi_company(self):
        company_b = self.env["res.company"].create({"name": "Other Company B"})
        hidden = self.env["res.partner"].create({"name": "Hidden B Partner", "company_id": company_b.id})
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "search_records", {
            "object": "contact", "filters": [{"field": "name", "operator": "=", "value": "Hidden B Partner"}]})
        self.assertEqual(res["total_matching"], 0)
        res = action_service.dispatch(ctx, "prepare_update_record", {
            "object": "contact", "record_id": hidden.id, "values": {"phone": "1"}})
        self.assertEqual(res["error"], "not_found")
        res = action_service.dispatch(ctx, "get_record", {"object": "contact", "name": "Hidden B Partner"})
        self.assertFalse(res["ok"])

    def test_conversations_and_log_are_private(self):
        with self.fake_llm([tool_call("prepare_create_record", object="contact", values={"name": "Private Co"}),
                            answer("Confirm?")]):
            res = self.uenv()["dcs.ai.executor"].process("create Private Co")
        action_id = res["messages"][-1]["actions"][0]["id"]
        other = self.uenv(self.restricted)
        self.assertFalse(other["dcs.ai.conversation"].search([("id", "=", res["conversation_id"])]))
        self.assertFalse(other["dcs.ai.action"].search([("id", "=", action_id)]))
        with self.assertRaises(AccessError):
            other["dcs.ai.executor"].decide([action_id], True)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Private Co")]))
        # users cannot tamper with the audit log
        with self.assertRaises(AccessError):
            self.uenv()["dcs.ai.action"].browse(action_id).write({"state": "done"})
        with self.assertRaises(AccessError):
            self.uenv()["dcs.ai.action"].create({"summary": "fake", "user_id": self.user.id, "operation": "read"})

    def test_superuser_mode_refused(self):
        with self.assertRaises(AccessError):
            action_service.ToolContext(self.env(su=True), self.env["dcs.ai.conversation"], {})
        with self.assertRaises(AccessError):
            self.env["dcs.ai.executor"].sudo().process("hi")

    def test_api_key_not_public(self):
        public = self.uenv()["dcs.ai.provider"]._get_public_config()
        self.assertNotIn("api_key", public)
        self.assertNotIn("sk-test", str(public))
        self.assertTrue(public["configured"])
        # normal users cannot read the parameter directly
        with self.assertRaises(AccessError):
            self.uenv()["ir.config_parameter"].get_str("dcs_ai_voice_assistant.api_key")

    def test_settings_keep_key_when_left_empty(self):
        settings = self.env["res.config.settings"].create({"dcs_ai_model": "other-model"})
        self.assertFalse(settings.dcs_ai_api_key)
        self.assertTrue(settings.dcs_ai_api_key_set)
        settings.execute()
        config = self.env["dcs.ai.provider"]._get_config()
        self.assertEqual(config["api_key"], "sk-test")
        self.assertEqual(config["model"], "other-model")
        settings = self.env["res.config.settings"].create({"dcs_ai_voice_enabled": False})
        settings.execute()
        self.assertFalse(self.env["dcs.ai.provider"]._get_config()["voice_enabled"])
