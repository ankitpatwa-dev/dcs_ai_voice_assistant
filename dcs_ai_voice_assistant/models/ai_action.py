import json
import logging
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError

from ..services import action_service

_logger = logging.getLogger(__name__)


class DcsAiAction(models.Model):
    """Audit log of everything the assistant did, and proposals awaiting confirmation.

    Users can read their own entries but cannot create, edit or delete them; the module
    writes them with ``sudo()`` and always stamps the acting user. Business operations
    themselves are never executed with sudo.
    """

    _name = "dcs.ai.action"
    _description = "AI Assistant Operation"
    _order = "id desc"
    _rec_name = "summary"

    summary = fields.Char(required=True, readonly=True)
    user_id = fields.Many2one("res.users", required=True, index=True, readonly=True, ondelete="cascade")
    company_id = fields.Many2one("res.company", index=True, readonly=True)
    conversation_id = fields.Many2one("dcs.ai.conversation", index=True, ondelete="set null", readonly=True)
    message_id = fields.Many2one("dcs.ai.message", index=True, ondelete="set null", readonly=True)
    operation = fields.Selection([
        ("read", "Read"), ("aggregate", "Report / totals"), ("create", "Create"), ("update", "Update"),
    ], required=True, readonly=True, index=True)
    model_name = fields.Char("Model", readonly=True, index=True)
    model_description = fields.Char(compute="_compute_model_description")
    res_id = fields.Integer("Record ID", readonly=True)
    record_name = fields.Char(readonly=True)
    record_count = fields.Integer(readonly=True)
    state = fields.Selection([
        ("pending", "Awaiting confirmation"), ("done", "Done"), ("failed", "Failed"),
        ("cancelled", "Cancelled"), ("expired", "Expired"),
    ], required=True, default="pending", readonly=True, index=True)
    input_text = fields.Text("User request", readonly=True)
    payload = fields.Json(readonly=True, groups="dcs_ai_voice_assistant.group_ai_manager")
    payload_text = fields.Text("Prepared values", compute="_compute_payload_text",
                               groups="dcs_ai_voice_assistant.group_ai_manager")
    preview = fields.Json(readonly=True)
    warnings = fields.Json(readonly=True)
    error_message = fields.Text(readonly=True)
    decided_via = fields.Selection([("button", "Confirmation button"), ("message", "Chat reply")], readonly=True)
    executed_at = fields.Datetime(readonly=True)
    expires_at = fields.Datetime(readonly=True)

    def _compute_payload_text(self):
        for action in self:
            action.payload_text = json.dumps(action.payload, indent=2, ensure_ascii=False, default=str) \
                if action.payload else False

    def _compute_model_description(self):
        names = {}
        for action in self:
            key = action.model_name
            if key and key not in names:
                names[key] = self.env["ir.model"].sudo()._get(key).name if key in self.env else key
            action.model_description = names.get(key) or key

    # ------------------------------------------------------------ logging
    @api.model
    def _base_vals(self, conversation, **kw):
        vals = {
            "user_id": self.env.uid,
            "company_id": self.env.company.id,
            "conversation_id": conversation.id if conversation else False,
        }
        vals.update(kw)
        return vals

    @api.model
    def _log_operation(self, conversation, operation, model_name, state="done", summary="",
                       record_count=0, input_text=""):
        return self.sudo().create(self._base_vals(
            conversation, operation=operation, model_name=model_name, state=state,
            summary=(_("%(op)s %(model)s", op=operation.capitalize(), model=model_name)
                     + (": %s" % summary if summary else ""))[:250],
            record_count=record_count, input_text=input_text, executed_at=fields.Datetime.now()))

    @api.model
    def _create_pending(self, conversation, operation, model_name, res_id, payload, summary, preview,
                        warnings, input_text):
        record_name = False
        if res_id and model_name in self.env:
            record_name = self.env[model_name].browse(res_id).display_name
        action = self.sudo().create(self._base_vals(
            conversation, operation=operation, model_name=model_name, res_id=res_id or 0,
            record_name=record_name, payload=payload, summary=summary[:250], preview=preview,
            warnings=warnings, input_text=input_text, state="pending",
            expires_at=fields.Datetime.now() + timedelta(minutes=action_service.PENDING_TTL_MINUTES)))
        return action.sudo(False)

    @api.model
    def _cancel_stale_pending(self, conversation, keep=None):
        if not conversation:
            return
        stale = self.search([("conversation_id", "=", conversation.id), ("state", "=", "pending"),
                             ("user_id", "=", self.env.uid)])
        if keep:
            stale -= keep
        stale._cancel(reason=_("Replaced by a newer proposal"))

    # ----------------------------------------------------------- decisions
    def _filter_valid(self):
        """Pending actions of the current user that have not expired (expires the others)."""
        now = fields.Datetime.now()
        expired = self.filtered(lambda a: a.state == "pending" and a.expires_at and a.expires_at < now)
        if expired:
            expired.sudo().write({"state": "expired"})
        return self.filtered(lambda a: a.state == "pending" and a.user_id.id == self.env.uid)

    def _cancel(self, reason=None):
        todo = self.filtered(lambda a: a.state == "pending")
        if todo:
            todo.sudo().write({"state": "cancelled", "error_message": reason or False,
                               "executed_at": fields.Datetime.now()})

    def _execute(self, via="button"):
        """Execute confirmed actions as the current user. Never raises; sets state per action."""
        if self.env.su:
            raise AccessError(_("AI operations cannot be executed in superuser mode."))
        for action in self:
            if action.user_id.id != self.env.uid:
                raise AccessError(_("You can only confirm your own assistant operations."))
            if action.state != "pending" or not action._filter_valid():
                continue
            payload = action.sudo().payload or {}
            try:
                with self.env.cr.savepoint():
                    record = action_service.execute_action(self.env, payload, action.model_name, action.res_id)
                    self.env.flush_all()
                vals = {"state": "done", "error_message": False}
                if record:
                    record = record[:1]
                    vals.update(res_id=record.id, model_name=record._name,
                                record_name=record.display_name)
            except (AccessError, UserError, ValidationError, MissingError) as e:
                vals = {"state": "failed", "error_message": action_service._clean(e)}
            except Exception as e:  # noqa: BLE001 - reported to the user, logged with traceback
                _logger.exception("AI assistant operation %s failed", action.id)
                vals = {"state": "failed", "error_message": _("Unexpected error: %s", str(e)[:300])}
            vals.update(executed_at=fields.Datetime.now(), decided_via=via)
            action.sudo().write(vals)
        return True

    # --------------------------------------------------------------- UI
    def _to_ui(self):
        self.ensure_one()
        return {
            "id": self.id,
            "state": self.state,
            "summary": self.summary,
            "operation": self.operation,
            "preview": self.preview or [],
            "warnings": self.warnings or [],
            "model": self.model_name,
            "res_id": self.res_id,
            "record_name": self.record_name,
            "error": self.error_message or "",
        }

    def action_open_record(self):
        self.ensure_one()
        if not (self.model_name in self.env and self.res_id):
            raise UserError(_("This operation is not linked to a record."))
        return {"type": "ir.actions.act_window", "res_model": self.model_name, "res_id": self.res_id,
                "views": [[False, "form"]], "target": "current"}

    @api.model
    def _cron_expire_pending(self):
        old = self.sudo().search([("state", "=", "pending"), ("expires_at", "<", fields.Datetime.now())])
        old.write({"state": "expired"})
