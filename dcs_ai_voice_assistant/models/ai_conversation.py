from odoo import _, api, fields, models


class DcsAiConversation(models.Model):
    _name = "dcs.ai.conversation"
    _description = "AI Assistant Conversation"
    _order = "last_message_date desc, id desc"

    name = fields.Char(default=lambda self: _("New conversation"), required=True)
    user_id = fields.Many2one("res.users", required=True, index=True, ondelete="cascade",
                              default=lambda self: self.env.user, readonly=True)
    company_id = fields.Many2one("res.company", required=True, index=True, readonly=True,
                                 default=lambda self: self.env.company)
    message_ids = fields.One2many("dcs.ai.message", "conversation_id", string="Messages")
    action_ids = fields.One2many("dcs.ai.action", "conversation_id", string="Operations")
    message_count = fields.Integer(compute="_compute_counts")
    action_count = fields.Integer(compute="_compute_counts")
    last_message_date = fields.Datetime(readonly=True, index=True)
    active = fields.Boolean(default=True)
    input_tokens = fields.Integer(readonly=True)
    output_tokens = fields.Integer(readonly=True)

    @api.depends("message_ids", "action_ids")
    def _compute_counts(self):
        for conv in self:
            conv.message_count = len(conv.message_ids.filtered(lambda m: m.role in ("user", "assistant")
                                                               and not m.tool_calls))
            conv.action_count = len(conv.action_ids)

    @api.model
    def _get_current(self, create=True):
        conv = self.search([("user_id", "=", self.env.uid), ("company_id", "=", self.env.company.id)],
                           order="last_message_date desc, id desc", limit=1)
        if not conv and create:
            conv = self.create({})
        return conv

    def _pending_actions(self):
        self.ensure_one()
        actions = self.action_ids.filtered(lambda a: a.state == "pending")
        return actions._filter_valid()

    def _llm_history(self, limit=30):
        """Last ``limit`` messages in the provider-neutral format, starting at a user message
        so that every tool result keeps its tool call."""
        self.ensure_one()
        messages = self.env["dcs.ai.message"].search(
            [("conversation_id", "=", self.id), ("role", "in", ("user", "assistant", "tool"))],
            order="id desc", limit=max(limit, 4))
        messages = messages.sorted("id")
        while messages and messages[0].role != "user":
            messages = messages[1:]
        return [m._to_llm() for m in messages]

    def _ui_messages(self, limit=60):
        self.ensure_one()
        messages = self.env["dcs.ai.message"].search(
            [("conversation_id", "=", self.id), ("visible", "=", True)], order="id desc", limit=limit)
        return [m._to_ui() for m in messages.sorted("id")]

    def _touch(self, title_from=None, usage=None):
        vals = {"last_message_date": fields.Datetime.now()}
        if title_from and self.name == _("New conversation"):
            vals["name"] = title_from.strip().replace("\n", " ")[:60]
        if usage:
            vals["input_tokens"] = self.input_tokens + int(usage.get("input") or 0)
            vals["output_tokens"] = self.output_tokens + int(usage.get("output") or 0)
        self.write(vals)

    def action_clear(self):
        """Archive the conversation; a fresh one is created on the next message."""
        self._pending_actions()._cancel(reason="Conversation cleared")
        self.write({"active": False})
        return True

    @api.autovacuum
    def _gc_old_conversations(self):
        days = self.env["dcs.ai.provider"]._get_config()["retention_days"]
        if days <= 0:
            return
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        old = self.with_context(active_test=False).search([
            ("active", "=", False), ("write_date", "<", limit)], limit=1000)
        # Operations (audit log) are kept: their conversation link is set to null.
        old.unlink()
