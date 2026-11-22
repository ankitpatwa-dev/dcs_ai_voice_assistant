import json

from odoo.tests import BaseCase, tagged

from ..services import ai_service
from ..services.ai_service import AIProviderError, ChatCompletionsProvider, OpenAIResponsesProvider


class FakeResponse:
    def __init__(self, status, body, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, data=None, timeout=None):
        self.requests.append({"url": url, "headers": headers, "payload": json.loads(data)})
        return self.responses.pop(0)


HISTORY = [
    {"role": "user", "content": "Find Azure"},
    {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "search_records",
                                                         "arguments": "{\"object\": \"contact\"}"}]},
    {"role": "tool", "tool_call_id": "c1", "name": "search_records", "content": "{\"ok\": true}"},
    {"role": "assistant", "content": "Found one."},
    {"role": "user", "content": "Their phone?"},
]


@tagged("post_install", "-at_install", "dcs_ai")
class TestProviders(BaseCase):

    def setUp(self):
        super().setUp()
        ai_service._UNSUPPORTED_PARAMS.clear()

    def test_responses_full_history_and_tool_call(self):
        http = FakeHttp([FakeResponse(200, {
            "id": "resp_1", "status": "completed",
            "output": [{"type": "reasoning", "id": "rs_1"},
                       {"type": "function_call", "call_id": "c2", "name": "get_record",
                        "arguments": "{\"object\": \"contact\", \"record_id\": 7}"}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        })])
        p = OpenAIResponsesProvider("sk-x", "gpt-test", session=http)
        resp = p.chat("instr", HISTORY, [{"name": "get_record", "description": "d", "parameters": {}}])
        sent = http.requests[0]
        self.assertTrue(sent["url"].endswith("/responses"))
        self.assertEqual(sent["headers"]["Authorization"], "Bearer sk-x")
        items = sent["payload"]["input"]
        # the function_call item is replayed before its output
        kinds = [i.get("type") or i.get("role") for i in items]
        self.assertEqual(kinds, ["user", "function_call", "function_call_output", "assistant", "user"])
        self.assertEqual(sent["payload"]["tools"][0]["type"], "function")
        self.assertEqual(resp.tool_calls[0]["id"], "c2")
        self.assertEqual(resp.state["previous_response_id"], "resp_1")
        self.assertEqual(resp.usage, {"input": 100, "output": 20})

    def test_responses_chains_tool_outputs(self):
        http = FakeHttp([FakeResponse(200, {"id": "resp_2", "status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Their phone is +1 555."}]}]})])
        p = OpenAIResponsesProvider("sk-x", "gpt-test", session=http)
        messages = HISTORY + [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2", "name": "get_record", "arguments": "{}"}]},
            {"role": "tool", "tool_call_id": "c2", "name": "get_record", "content": "{\"phone\": \"+1 555\"}"},
        ]
        resp = p.chat("instr", messages, [], state={"previous_response_id": "resp_1", "sent": len(HISTORY)})
        payload = http.requests[0]["payload"]
        self.assertEqual(payload["previous_response_id"], "resp_1")
        self.assertEqual(payload["input"], [{"type": "function_call_output", "call_id": "c2",
                                             "output": "{\"phone\": \"+1 555\"}"}])
        self.assertEqual(resp.content, "Their phone is +1 555.")

    def test_unsupported_temperature_is_dropped_and_remembered(self):
        http = FakeHttp([
            FakeResponse(400, {"error": {"message": "Unsupported parameter: 'temperature' is not supported."}}),
            FakeResponse(200, {"id": "r", "output": [{"type": "message", "content": [{"type": "output_text",
                                                                                     "text": "OK"}]}]}),
            FakeResponse(200, {"id": "r2", "output": [{"type": "message", "content": [{"type": "output_text",
                                                                                      "text": "OK"}]}]}),
        ])
        p = OpenAIResponsesProvider("sk-x", "reasoner", session=http)
        self.assertEqual(p.chat("i", [{"role": "user", "content": "hi"}]).content, "OK")
        self.assertIn("temperature", http.requests[0]["payload"])
        self.assertNotIn("temperature", http.requests[1]["payload"])
        p.chat("i", [{"role": "user", "content": "hi"}])
        self.assertNotIn("temperature", http.requests[2]["payload"])

    def test_errors_are_mapped(self):
        for status, code in ((401, "auth"), (404, "config"), (400, "bad_request")):
            http = FakeHttp([FakeResponse(status, {"error": {"message": "nope"}})])
            with self.assertRaises(AIProviderError) as cm:
                OpenAIResponsesProvider("sk-x", "m", session=http).chat("i", [{"role": "user", "content": "x"}])
            self.assertEqual(cm.exception.code, code)
            self.assertNotIn("sk-x", cm.exception.user_message)
        with self.assertRaises(AIProviderError) as cm:
            OpenAIResponsesProvider("", "m").chat("i", [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.code, "config")

    def test_chat_completions_format(self):
        http = FakeHttp([FakeResponse(200, {"choices": [{"finish_reason": "tool_calls", "message": {
            "content": None, "tool_calls": [{"id": "t9", "type": "function", "function": {
                "name": "search_records", "arguments": "{\"object\": \"lead\"}"}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})])
        p = ChatCompletionsProvider("k", "llama", base_url="http://local/v1/", session=http)
        resp = p.chat("instr", HISTORY, [{"name": "search_records", "description": "d", "parameters": {}}])
        sent = http.requests[0]
        self.assertEqual(sent["url"], "http://local/v1/chat/completions")
        roles = [m["role"] for m in sent["payload"]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant", "user"])
        self.assertEqual(sent["payload"]["messages"][2]["tool_calls"][0]["function"]["name"], "search_records")
        self.assertEqual(resp.tool_calls[0]["name"], "search_records")

    def test_registry(self):
        codes = dict(ai_service.get_provider_selection())
        self.assertIn("openai", codes)
        self.assertIn("openai_compatible", codes)
        with self.assertRaises(AIProviderError):
            ai_service.make_provider("does-not-exist", api_key="", model="m")
