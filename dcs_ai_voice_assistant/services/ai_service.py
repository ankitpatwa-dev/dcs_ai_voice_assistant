"""Language-model providers.

The rest of the module talks to the model through one provider-neutral interface:

    provider.chat(instructions, messages, tools, state) -> LLMResponse

``messages`` is a list of plain dicts in a neutral format::

    {"role": "user", "content": "..."}
    {"role": "assistant", "content": "...", "tool_calls": [{"id", "name", "arguments"}]}
    {"role": "tool", "tool_call_id": "...", "name": "...", "content": "<json string>"}

``tools`` is a list of ``{"name", "description", "parameters"}`` (JSON schema).

New providers subclass :class:`BaseLLMProvider` and call :func:`register_provider`.
Nothing in this file knows about Odoo models.
"""
import json
import logging
import time

import requests

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = (10, 90)  # (connect, read) seconds
MAX_RETRIES = 1

# Models that rejected a parameter once; remembered per worker process so the
# extra round trip only happens the first time.
_UNSUPPORTED_PARAMS = {}


class AIProviderError(Exception):
    """Error talking to an AI provider.

    ``user_message`` is safe to show to the end user (no keys, no raw payloads).
    ``code`` is one of: config, auth, rate_limit, timeout, network, unavailable,
    bad_request, invalid_response.
    """

    def __init__(self, user_message, code="error", detail=None):
        super().__init__(user_message)
        self.user_message = user_message
        self.code = code
        self.detail = detail


class LLMResponse:
    __slots__ = ("content", "tool_calls", "usage", "state", "finish_reason")

    def __init__(self, content="", tool_calls=None, usage=None, state=None, finish_reason=None):
        self.content = content or ""
        self.tool_calls = tool_calls or []
        self.usage = usage or {}
        self.state = state if state is not None else {}
        self.finish_reason = finish_reason


class BaseLLMProvider:
    code = None
    label = None
    default_base_url = None

    def __init__(self, api_key, model, base_url=None, temperature=0.2, max_tokens=1500,
                 timeout=DEFAULT_TIMEOUT, session=None):
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip()
        self.base_url = (base_url or self.default_base_url or "").rstrip("/")
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.http = session or requests

    # ------------------------------------------------------------------ API
    def chat(self, instructions, messages, tools=None, state=None):
        raise NotImplementedError

    def test_connection(self):
        """Send a tiny request; raise AIProviderError on failure."""
        resp = self.chat("Reply with the single word OK.", [{"role": "user", "content": "ping"}])
        return resp.content

    # -------------------------------------------------------------- helpers
    def _check_config(self):
        if not self.model:
            raise AIProviderError("No AI model is configured. Ask your administrator to set one "
                                  "in Settings > AI Voice Assistant.", code="config")
        if not self.base_url:
            raise AIProviderError("No API endpoint is configured for the AI provider.", code="config")

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        return headers

    def _unsupported(self):
        return _UNSUPPORTED_PARAMS.setdefault((self.base_url, self.model), set())

    def _post_json(self, path, payload, optional_params=()):
        """POST ``payload`` and return the decoded JSON body.

        ``optional_params`` are payload keys that may be dropped (and remembered as
        unsupported for this model) when the provider rejects them with HTTP 400.
        """
        url = "%s%s" % (self.base_url, path)
        payload = dict(payload)
        for key in self._unsupported():
            payload.pop(key, None)
        attempt = 0
        while True:
            try:
                resp = self.http.post(url, headers=self._headers(), data=json.dumps(payload),
                                      timeout=self.timeout)
            except requests.exceptions.Timeout as e:
                raise AIProviderError("The AI service did not answer in time. Please try again.",
                                      code="timeout", detail=str(e)) from e
            except requests.exceptions.RequestException as e:
                raise AIProviderError("Could not reach the AI service. Check the server's internet "
                                      "connection and the configured endpoint.",
                                      code="network", detail=str(e)) from e

            if resp.status_code < 300:
                try:
                    return resp.json()
                except ValueError as e:
                    raise AIProviderError("The AI service returned an unreadable answer.",
                                          code="invalid_response", detail=resp.text[:500]) from e

            detail = _error_detail(resp)
            if resp.status_code == 400:
                dropped = [k for k in optional_params if k in payload and k in detail.lower()]
                if dropped:
                    for key in dropped:
                        payload.pop(key, None)
                        self._unsupported().add(key)
                    _logger.info("AI provider rejected %s for model %s; retrying without it.",
                                 dropped, self.model)
                    continue
                raise AIProviderError("The AI service rejected the request: %s" % detail[:300],
                                      code="bad_request", detail=detail)
            if resp.status_code in (401, 403):
                raise AIProviderError("The AI service refused the API key. Ask your administrator "
                                      "to check it in Settings > AI Voice Assistant.",
                                      code="auth", detail=detail)
            if resp.status_code == 404:
                raise AIProviderError("The AI model or endpoint was not found (%s). Check the model "
                                      "name in the settings." % self.model, code="config", detail=detail)
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                attempt += 1
                wait = _retry_after(resp)
                _logger.info("AI provider returned %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code == 429:
                raise AIProviderError("The AI service is rate limiting requests or the account has "
                                      "no credit left. Please try again shortly.",
                                      code="rate_limit", detail=detail)
            raise AIProviderError("The AI service is temporarily unavailable (HTTP %s). Please try "
                                  "again." % resp.status_code, code="unavailable", detail=detail)


def _error_detail(resp):
    try:
        body = resp.json()
        err = body.get("error") if isinstance(body, dict) else None
        if isinstance(err, dict):
            return str(err.get("message") or err)
        if err:
            return str(err)
        return json.dumps(body)[:500]
    except ValueError:
        return (resp.text or "")[:500]


def _retry_after(resp):
    try:
        return min(max(float(resp.headers.get("Retry-After", 2)), 0.5), 5.0)
    except (TypeError, ValueError):
        return 2.0


class OpenAIResponsesProvider(BaseLLMProvider):
    """OpenAI Responses API (``POST /v1/responses``) with function tools.

    Within one assistant turn, follow-up requests (after tool results) are chained with
    ``previous_response_id`` so reasoning models keep their reasoning items server-side.
    Across turns the module sends its own stored history.
    """

    code = "openai"
    label = "OpenAI (Responses API)"
    default_base_url = "https://api.openai.com/v1"

    def chat(self, instructions, messages, tools=None, state=None):
        self._check_config()
        if not self.api_key:
            raise AIProviderError("No OpenAI API key is configured. Ask your administrator to add "
                                  "it in Settings > AI Voice Assistant.", code="config")
        state = dict(state or {})
        previous = state.get("previous_response_id")
        if previous:
            items = [_responses_item(m) for m in messages[state.get("sent", 0):] if m["role"] == "tool"]
        else:
            items = responses_input(messages)
        payload = {
            "model": self.model,
            "instructions": instructions,
            "input": items,
            "max_output_tokens": int(self.max_tokens or 1500),
            "temperature": float(self.temperature),
        }
        if previous:
            payload["previous_response_id"] = previous
        if tools:
            payload["tools"] = [{
                "type": "function",
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
                "strict": False,
            } for t in tools]
            payload["tool_choice"] = "auto"
        body = self._post_json("/responses", payload, optional_params=("temperature",))
        return self._parse(body, state, len(messages))

    def _parse(self, body, state, sent):
        texts, calls = [], []
        for item in body.get("output") or []:
            kind = item.get("type")
            if kind == "message":
                for part in item.get("content") or []:
                    if part.get("type") in ("output_text", "text") and part.get("text"):
                        texts.append(part["text"])
            elif kind == "function_call":
                calls.append({
                    "id": item.get("call_id") or item.get("id"),
                    "name": item.get("name"),
                    "arguments": item.get("arguments") or "{}",
                })
        if not texts and not calls and body.get("output_text"):
            texts.append(body["output_text"])
        status = body.get("status")
        if not texts and not calls:
            reason = (body.get("incomplete_details") or {}).get("reason")
            if status == "incomplete" and reason == "max_output_tokens":
                raise AIProviderError("The AI ran out of response tokens before answering. Increase "
                                      "'Maximum response tokens' in the settings.",
                                      code="bad_request")
            raise AIProviderError("The AI service returned an empty answer.", code="invalid_response")
        state.update({"previous_response_id": body.get("id"), "sent": sent})
        usage = body.get("usage") or {}
        return LLMResponse(
            content="\n".join(texts).strip(),
            tool_calls=calls,
            usage={"input": usage.get("input_tokens", 0), "output": usage.get("output_tokens", 0)},
            state=state,
            finish_reason=status,
        )


def _responses_item(msg):
    role = msg["role"]
    if role == "user":
        return {"role": "user", "content": msg.get("content") or ""}
    if role == "assistant":
        return {"role": "assistant", "content": msg.get("content") or ""}
    if role == "tool":
        return {"type": "function_call_output", "call_id": msg["tool_call_id"],
                "output": msg.get("content") or ""}
    return None


def responses_input(messages):
    """Neutral history -> Responses ``input`` items.

    Each ``function_call_output`` must be preceded by its ``function_call`` item, so an
    assistant message with tool calls is expanded into those items.
    """
    out = []
    for msg in messages:
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            if msg.get("content"):
                out.append({"role": "assistant", "content": msg["content"]})
            for call in msg["tool_calls"]:
                out.append({"type": "function_call", "call_id": call["id"], "name": call["name"],
                            "arguments": call.get("arguments") or "{}"})
            continue
        item = _responses_item(msg)
        if item:
            out.append(item)
    return out


class ChatCompletionsProvider(BaseLLMProvider):
    """Any OpenAI-compatible ``/chat/completions`` endpoint with tool calling
    (OpenRouter, Groq, Mistral, Together, Azure OpenAI v1, a local Ollama or vLLM...)."""

    code = "openai_compatible"
    label = "OpenAI-compatible (Chat Completions)"
    default_base_url = None

    def chat(self, instructions, messages, tools=None, state=None):
        self._check_config()
        chat_messages = [{"role": "system", "content": instructions}]
        for msg in messages:
            role = msg["role"]
            if role == "assistant" and msg.get("tool_calls"):
                chat_messages.append({
                    "role": "assistant",
                    "content": msg.get("content") or None,
                    "tool_calls": [{
                        "id": c["id"], "type": "function",
                        "function": {"name": c["name"], "arguments": c.get("arguments") or "{}"},
                    } for c in msg["tool_calls"]],
                })
            elif role == "tool":
                chat_messages.append({"role": "tool", "tool_call_id": msg["tool_call_id"],
                                      "content": msg.get("content") or ""})
            elif role in ("user", "assistant"):
                chat_messages.append({"role": role, "content": msg.get("content") or ""})
        payload = {
            "model": self.model,
            "messages": chat_messages,
            "temperature": float(self.temperature),
            "max_tokens": int(self.max_tokens or 1500),
        }
        if tools:
            payload["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"], "parameters": t["parameters"],
            }} for t in tools]
            payload["tool_choice"] = "auto"
        body = self._post_json("/chat/completions", payload, optional_params=("temperature",))
        try:
            choice = body["choices"][0]
            message = choice.get("message") or {}
        except (KeyError, IndexError, TypeError) as e:
            raise AIProviderError("The AI service returned an unexpected answer.",
                                  code="invalid_response", detail=json.dumps(body)[:500]) from e
        calls = [{
            "id": c.get("id"),
            "name": (c.get("function") or {}).get("name"),
            "arguments": (c.get("function") or {}).get("arguments") or "{}",
        } for c in message.get("tool_calls") or []]
        content = message.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not content and not calls:
            if choice.get("finish_reason") == "length":
                raise AIProviderError("The AI ran out of response tokens before answering. Increase "
                                      "'Maximum response tokens' in the settings.", code="bad_request")
            raise AIProviderError("The AI service returned an empty answer.", code="invalid_response")
        usage = body.get("usage") or {}
        return LLMResponse(
            content=content.strip(),
            tool_calls=calls,
            usage={"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)},
            state=dict(state or {}),
            finish_reason=choice.get("finish_reason"),
        )


PROVIDERS = {}


def register_provider(cls):
    """Register a provider class (usable as a decorator)."""
    PROVIDERS[cls.code] = cls
    return cls


register_provider(OpenAIResponsesProvider)
register_provider(ChatCompletionsProvider)


def get_provider_selection():
    return [(code, cls.label) for code, cls in PROVIDERS.items()]


def make_provider(code, **kwargs):
    cls = PROVIDERS.get(code)
    if not cls:
        raise AIProviderError("Unknown AI provider '%s'." % code, code="config")
    return cls(**kwargs)
