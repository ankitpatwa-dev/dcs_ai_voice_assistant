from odoo import api, fields, models


class DcsAiMessage(models.Model):
    _name = "dcs.ai.message"
    _description = "AI Assistant Message"
    _order = "id"

    conversation_id = fields.Many2one("dcs.ai.conversation", required=True, index=True, ondelete="cascade")
    user_id = fields.Many2one(related="conversation_id.user_id", store=True, index=True)
    company_id = fields.Many2one(related="conversation_id.company_id", store=True)
    role = fields.Selection([("user", "User"), ("assistant", "Assistant"), ("tool", "Tool result")],
                            required=True)
    content = fields.Text()
    llm_content = fields.Text(help="Text given to the model instead of 'content' (e.g. with record ids).")
    input_mode = fields.Selection([("text", "Text"), ("voice", "Voice"), ("system", "System")], default="text")
    tool_calls = fields.Json()
    tool_call_id = fields.Char()
    tool_name = fields.Char()
    display = fields.Json(help="Structured blocks (tables, cards) rendered by the chat window.")
    is_error = fields.Boolean()
    visible = fields.Boolean(compute="_compute_visible", store=True)
    action_ids = fields.One2many("dcs.ai.action", "message_id", string="Proposed operations")

    @api.depends("role", "tool_calls")
    def _compute_visible(self):
        for msg in self:
            msg.visible = msg.role == "user" or (msg.role == "assistant" and not msg.tool_calls)

    def _to_llm(self):
        self.ensure_one()
        if self.role == "tool":
            return {"role": "tool", "tool_call_id": self.tool_call_id, "name": self.tool_name,
                    "content": self.content or "{}"}
        msg = {"role": self.role, "content": self.llm_content or self.content or ""}
        if self.role == "assistant" and self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg

    def _to_ui(self):
        self.ensure_one()
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content or "",
            "display": self.display or [],
            "is_error": self.is_error,
            "input_mode": self.input_mode,
            "date": fields.Datetime.to_string(self.create_date),
            "actions": [a._to_ui() for a in self.action_ids.sorted("id")],
        }
