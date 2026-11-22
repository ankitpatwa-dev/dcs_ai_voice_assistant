import json
from unittest.mock import patch

from odoo.tests import HttpCase, new_test_user, tagged

from ..services.ai_service import LLMResponse


@tagged("post_install", "-at_install", "dcs_ai")
class TestController(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["dcs.ai.provider"]._set_config({"api_key": "sk-test", "model": "fake"})
        cls.user = new_test_user(cls.env, login="ai_http", password="ai_http_pw1",
                                 groups="base.group_user,base.group_partner_manager")
        cls.portal = new_test_user(cls.env, login="ai_portal", password="ai_portal_pw1", groups="base.group_portal")

    def rpc(self, route, **params):
        resp = self.url_open(route, data=json.dumps({"jsonrpc": "2.0", "method": "call", "params": params}),
                             headers={"Content-Type": "application/json"})
        return resp.json()

    def test_init_and_chat(self):
        self.authenticate("ai_http", "ai_http_pw1")
        res = self.rpc("/dcs_ai/init")["result"]
        self.assertNotIn("api_key", json.dumps(res["config"]))
        self.assertEqual(res["messages"], [])

        class Llm:
            def chat(self, *a, **k):
                return LLMResponse(content="Hello from the test model")

        with patch.object(self.registry["dcs.ai.provider"], "_llm", lambda self_, config=None: Llm()):
            res = self.rpc("/dcs_ai/chat", message="hi", context={"allowed_company_ids": [self.env.company.id]})
        self.assertEqual(res["result"]["messages"][-1]["content"], "Hello from the test model")

    def test_unauthorized_company_rejected(self):
        other = self.env["res.company"].create({"name": "Not Mine"})
        self.authenticate("ai_http", "ai_http_pw1")
        res = self.rpc("/dcs_ai/init", context={"allowed_company_ids": [other.id]})
        self.assertIn("error", res)

    def test_portal_user_refused(self):
        self.authenticate("ai_portal", "ai_portal_pw1")
        res = self.rpc("/dcs_ai/init")
        self.assertIn("error", res)
