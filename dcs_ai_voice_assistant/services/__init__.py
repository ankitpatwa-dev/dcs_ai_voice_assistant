"""Plain-Python services.

* ``ai_service``      - language-model providers (OpenAI Responses API, OpenAI-compatible
                        Chat Completions). No Odoo business logic in here.
* ``speech_service``  - speech-to-text and text-to-speech providers.
* ``intent_service``  - system prompt and tool (function) definitions given to the model.
* ``action_service``  - the tool implementations: validated, ORM-only operations that run
                        with the calling user's environment.
"""
from . import ai_service
from . import speech_service
from . import intent_service
from . import action_service
