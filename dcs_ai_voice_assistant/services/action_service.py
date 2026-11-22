"""Tool implementations: the only things the language model can make Odoo do.

Design rules
------------
* Every tool runs with the *calling user's* environment. No ``sudo()`` is used for business
  data, so access rights, record rules, field groups and multi-company rules all apply.
* The model never sends Python, SQL or raw domains. It names an *object* from
  :data:`OBJECTS` (a whitelist), fields that are validated against the model's metadata,
  and filters built from a fixed operator set.
* Read tools return real data. Write tools never write: they validate and resolve the
  request, then create a *pending action* (``dcs.ai.action``). The change is executed only
  after the user confirms, by :func:`execute_action`, again as that user.
* Tool results contain ``display`` blocks that the chat window renders as tables/cards
  straight from the database values, so figures shown to the user never pass through the
  model.
"""
import json
import logging
import re
from datetime import date, datetime, timedelta

import pytz
from dateutil.relativedelta import relativedelta

from odoo import fields as ofields
from odoo.exceptions import AccessError, MissingError, UserError, ValidationError
from odoo.fields import Command
from odoo.tools import format_amount, html2plaintext

_logger = logging.getLogger(__name__)

MAX_ROWS = 50
DEFAULT_ROWS = 10
MAX_TEXT = 400
PENDING_TTL_MINUTES = 30

FILTER_OPERATORS = {
    "=", "!=", ">", ">=", "<", "<=", "ilike", "not ilike", "in", "not in", "is_set", "is_not_set",
}
AGGREGATORS = {"sum", "avg", "min", "max", "count_distinct"}
DATE_GRANULARITY = {"day", "week", "month", "quarter", "year"}
BLOCKED_FIELD_RE = re.compile(r"(password|passwd|token|secret|api_key|totp|signature|_hash$)", re.I)
BLOCKED_TYPES = {"binary", "image", "properties", "properties_definition", "json", "reference",
                 "many2one_reference"}


class ToolError(Exception):
    """A problem the model should explain to the user or fix by asking a question."""

    def __init__(self, message, code="invalid_request", **extra):
        super().__init__(message)
        self.message = message
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------------------
# Object registry
# ---------------------------------------------------------------------------------------
# key -> spec. Only objects whose model is installed AND readable by the user are offered.
#   create/update: whitelists of fields the assistant may set (anything else is refused).
#   required: fields that must be provided to create.
OBJECTS = {
    "contact": {
        "model": "res.partner", "label": "Contact (customer / vendor / company / person)",
        "default_fields": ["name", "is_company", "parent_id", "email", "phone", "mobile", "city",
                           "country_id", "user_id"],
        "order": "name",
        "create": ["name", "is_company", "parent_id", "email", "phone", "mobile", "street", "street2",
                   "city", "zip", "state_id", "country_id", "website", "vat", "function", "comment",
                   "category_id", "user_id", "partner_role"],
        "update": ["name", "is_company", "parent_id", "email", "phone", "mobile", "street", "street2",
                   "city", "zip", "state_id", "country_id", "website", "vat", "function", "comment",
                   "category_id", "user_id", "partner_role"],
        "required": ["name"],
        "virtual_fields": {"partner_role": "customer | vendor | both (sets customer/vendor rank)"},
    },
    "lead": {
        "model": "crm.lead", "label": "CRM lead or opportunity",
        "default_fields": ["name", "type", "partner_id", "user_id", "stage_id", "expected_revenue",
                           "probability", "date_deadline", "activity_date_deadline"],
        "order": "create_date desc",
        "create": ["name", "type", "partner_id", "contact_name", "partner_name", "email_from", "phone",
                   "user_id", "team_id", "stage_id", "expected_revenue", "probability", "date_deadline",
                   "priority", "tag_ids", "description"],
        "update": ["name", "type", "partner_id", "contact_name", "partner_name", "email_from", "phone",
                   "user_id", "team_id", "stage_id", "expected_revenue", "probability", "date_deadline",
                   "priority", "tag_ids", "description"],
        "required": ["name"],
    },
    "crm_stage": {"model": "crm.stage", "label": "CRM pipeline stage",
                  "default_fields": ["name", "sequence", "is_won", "team_id"], "order": "sequence"},
    "sales_team": {"model": "crm.team", "label": "Sales team",
                   "default_fields": ["name", "user_id", "member_ids"], "order": "sequence"},
    "sale_order": {
        "model": "sale.order", "label": "Sales quotation / sales order",
        "default_fields": ["name", "partner_id", "date_order", "amount_total", "state", "user_id",
                           "invoice_status"],
        "order": "date_order desc",
        "update": ["validity_date", "user_id", "client_order_ref", "note", "commitment_date",
                   "payment_term_id", "partner_id"],
        "states_note": "state: draft=Quotation, sent=Quotation Sent, sale=Sales Order, cancel=Cancelled",
    },
    "sale_order_line": {
        "model": "sale.order.line", "label": "Sales order line (products sold)",
        "domain": [("display_type", "=", False)],
        "default_fields": ["order_id", "product_id", "product_uom_qty", "price_unit", "price_subtotal",
                           "qty_delivered", "qty_invoiced", "state"],
        "order": "id desc",
    },
    "purchase_order": {
        "model": "purchase.order", "label": "Purchase order / request for quotation",
        "default_fields": ["name", "partner_id", "date_order", "date_planned", "amount_total", "state",
                           "receipt_status", "invoice_status"],
        "order": "date_order desc",
        "update": ["partner_ref", "date_planned", "user_id", "notes", "payment_term_id", "partner_id"],
        "states_note": "state: draft=RFQ, sent=RFQ Sent, to approve, purchase=Purchase Order, done=Locked, cancel",
    },
    "purchase_order_line": {
        "model": "purchase.order.line", "label": "Purchase order line",
        "domain": [("display_type", "=", False)],
        "default_fields": ["order_id", "product_id", "product_qty", "price_unit", "price_subtotal",
                           "qty_received", "qty_invoiced", "state"],
        "order": "id desc",
    },
    "product": {
        "model": "product.product", "label": "Product (variant)",
        "default_fields": ["display_name", "default_code", "list_price", "standard_price", "type",
                           "qty_available", "virtual_available", "uom_id"],
        "order": "name",
        "create": ["name", "default_code", "barcode", "list_price", "standard_price", "type",
                   "is_storable", "categ_id", "description_sale", "sale_ok", "purchase_ok"],
        "update": ["name", "default_code", "barcode", "list_price", "standard_price", "categ_id",
                   "description_sale", "sale_ok", "purchase_ok"],
        "required": ["name"],
    },
    "product_category": {"model": "product.category", "label": "Product category",
                         "default_fields": ["complete_name", "parent_id"], "order": "complete_name"},
    "stock_quant": {
        "model": "stock.quant", "label": "Stock on hand per product and location",
        "domain": [("location_id.usage", "=", "internal")],
        "default_fields": ["product_id", "location_id", "warehouse_id", "quantity", "reserved_quantity",
                           "available_quantity", "lot_id"],
        "order": "product_id",
    },
    "warehouse": {"model": "stock.warehouse", "label": "Warehouse",
                  "default_fields": ["name", "code", "partner_id", "company_id"], "order": "sequence"},
    "stock_location": {"model": "stock.location", "label": "Stock location",
                       "domain": [("usage", "=", "internal")],
                       "default_fields": ["complete_name", "warehouse_id"], "order": "complete_name"},
    "transfer": {
        "model": "stock.picking", "label": "Inventory transfer (receipt, delivery, internal)",
        "default_fields": ["name", "partner_id", "picking_type_id", "scheduled_date", "state", "origin"],
        "order": "scheduled_date desc",
    },
    "invoice": {
        "model": "account.move", "label": "Customer invoice or credit note",
        "domain": [("move_type", "in", ("out_invoice", "out_refund"))],
        "default_fields": ["name", "partner_id", "invoice_date", "invoice_date_due", "amount_total",
                           "amount_residual", "payment_state", "state", "move_type"],
        "order": "invoice_date desc, id desc",
        "update": ["ref", "invoice_date", "invoice_date_due", "narration", "invoice_payment_term_id"],
        "states_note": "state: draft/posted/cancel; payment_state: not_paid, in_payment, paid, partial, "
                       "reversed. Company-currency amounts: amount_total_signed, amount_residual_signed.",
    },
    "vendor_bill": {
        "model": "account.move", "label": "Vendor bill or refund",
        "domain": [("move_type", "in", ("in_invoice", "in_refund"))],
        "default_fields": ["name", "partner_id", "invoice_date", "invoice_date_due", "amount_total",
                           "amount_residual", "payment_state", "state", "ref"],
        "order": "invoice_date desc, id desc",
        "update": ["ref", "invoice_date", "invoice_date_due", "narration", "invoice_payment_term_id"],
    },
    "payment": {
        "model": "account.payment", "label": "Customer or vendor payment",
        "default_fields": ["name", "partner_id", "date", "amount", "payment_type", "partner_type",
                           "state", "journal_id"],
        "order": "date desc",
    },
    "activity": {
        "model": "mail.activity", "label": "Scheduled activity / follow-up",
        "default_fields": ["res_name", "res_model", "activity_type_id", "summary", "date_deadline",
                           "user_id", "state"],
        "order": "date_deadline",
    },
    "user": {"model": "res.users", "label": "Internal user (salesperson, responsible)",
             "domain": [("share", "=", False)], "default_fields": ["name", "email"], "order": "name"},
}

# Many2one fields resolved against a narrower domain than "any readable record".
M2O_DOMAINS = {
    "res.users": [("share", "=", False)],
}


def available_objects(env):
    """Objects whose model exists in this database and that the user may read."""
    result = {}
    for key, spec in OBJECTS.items():
        model = spec["model"]
        if model in env and env[model].has_access("read"):
            result[key] = spec
    return result


def _noun(spec):
    """Short object name for messages: 'Contact', 'Sales quotation / sales order'..."""
    return spec["label"].split(" (")[0]


def get_spec(env, key):
    if not key:
        raise ToolError("Missing 'object'.")
    spec = OBJECTS.get(key)
    if not spec:
        raise ToolError("Unknown object '%s'. Available: %s" % (key, ", ".join(available_objects(env))),
                        code="unknown_object")
    if spec["model"] not in env:
        raise ToolError("The %s app is not installed in this database." % spec["label"],
                        code="app_not_installed")
    if not env[spec["model"]].has_access("read"):
        raise ToolError("You do not have access to %s records." % spec["label"], code="permission_denied")
    return spec


# ---------------------------------------------------------------------------------------
# Tool context
# ---------------------------------------------------------------------------------------
class ToolContext:
    """State shared by the tools during one assistant turn."""

    def __init__(self, env, conversation, config, user_text=""):
        if env.su:
            raise AccessError("The AI assistant must not run in superuser mode.")
        self.env = env
        self.conversation = conversation
        self.config = config
        self.user_text = user_text or ""
        self.displays = []
        self.pending_actions = env["dcs.ai.action"]

    @property
    def allow_create(self):
        return self.config.get("allow_create", True)

    @property
    def allow_update(self):
        return self.config.get("allow_update", True)

    def today(self):
        return ofields.Date.context_today(self.env.user)

    def log_read(self, spec, operation, count, detail=None):
        if not self.config.get("log_reads", True):
            return
        self.env["dcs.ai.action"]._log_operation(
            conversation=self.conversation, operation=operation, model_name=spec["model"],
            state="done", summary=detail or "", record_count=count, input_text=self.user_text,
        )

    def create_pending(self, operation, spec, payload, summary, preview, res_id=False, warnings=None):
        # A new proposal replaces older proposals of the same conversation that were never answered.
        self.env["dcs.ai.action"]._cancel_stale_pending(self.conversation, keep=self.pending_actions)
        action = self.env["dcs.ai.action"]._create_pending(
            conversation=self.conversation, operation=operation, model_name=spec["model"],
            res_id=res_id, payload=payload, summary=summary, preview=preview,
            warnings=warnings or [], input_text=self.user_text,
        )
        self.pending_actions |= action
        return {
            "ok": True,
            "status": "awaiting_confirmation",
            "action_id": action.id,
            "summary": summary,
            "preview": preview,
            "warnings": warnings or [],
            "instruction": "Nothing has been saved yet. Tell the user what will happen in one or two "
                           "sentences, mention any warnings, and ask them to confirm. The user confirms "
                           "with the button or by saying yes. Never say it is done.",
        }


# ---------------------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------------------
def _field_accessible(model, field):
    return field.is_accessible(model.env)


def _check_field_name(model, fname, purpose="read"):
    field = model._fields.get(fname)
    if not field:
        raise ToolError("Field '%s' does not exist on %s." % (fname, model._name), code="invalid_field")
    if BLOCKED_FIELD_RE.search(fname) or field.type in BLOCKED_TYPES:
        raise ToolError("Field '%s' cannot be used by the assistant." % fname, code="invalid_field")
    if not _field_accessible(model, field):
        raise ToolError("You do not have access to the field '%s'." % (field.string or fname),
                        code="permission_denied")
    return field


def _check_path(model, path, purpose="filter"):
    """Validate a (possibly dotted) field path and return (final_model, final_field)."""
    parts = [p for p in (path or "").split(".") if p]
    if not parts or len(parts) > 3:
        raise ToolError("Invalid field path '%s'." % path, code="invalid_field")
    current = model
    field = None
    for i, part in enumerate(parts):
        field = _check_field_name(current, part, purpose)
        if purpose == "filter" and not field._description_searchable:
            raise ToolError("Field '%s' cannot be used in a filter." % path, code="invalid_field")
        if i < len(parts) - 1:
            if field.type not in ("many2one", "one2many", "many2many"):
                raise ToolError("'%s' is not a relational field, cannot follow '%s'." % (part, path),
                                code="invalid_field")
            current = model.env[field.comodel_name]
    return current, field


def _selection_map(model, field):
    try:
        return dict(field._description_selection(model.env) or [])
    except Exception:  # noqa: BLE001 - selection callables may fail on odd records
        return {}


def _short(text, limit=MAX_TEXT):
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _currency_of(record, field):
    try:
        cfield = field.get_currency_field(record)
        if cfield and cfield in record._fields:
            return record[cfield]
    except Exception:  # noqa: BLE001
        pass
    return record.env.company.currency_id


def format_value(record, fname):
    """Return ``(data_value, display_text)`` for one field of one record.

    ``data_value`` is compact JSON for the model, ``display_text`` is what the chat shows.
    """
    field = record._fields[fname]
    value = record[fname]
    ftype = field.type
    if ftype == "many2one":
        if not value:
            return None, ""
        name = value.sudo(False).display_name
        return {"id": value.id, "name": name}, name
    if ftype in ("one2many", "many2many"):
        names = value[:5].mapped("display_name")
        text = ", ".join(names) + (" (+%d)" % (len(value) - 5) if len(value) > 5 else "")
        return names, text
    if ftype == "selection":
        label = _selection_map(record, field).get(value, value or "")
        return value or None, label or ""
    if ftype == "monetary":
        currency = _currency_of(record, field)
        amount = round(value or 0.0, 2)
        text = format_amount(record.env, amount, currency) if currency else str(amount)
        return {"amount": amount, "currency": currency.name if currency else None}, text
    if ftype == "float":
        return round(value or 0.0, 4), ("%g" % round(value or 0.0, 2))
    if ftype == "integer":
        return value or 0, str(value or 0)
    if ftype == "boolean":
        return bool(value), "Yes" if value else "No"
    if ftype == "date":
        return (value.isoformat() if value else None), (ofields.Date.to_string(value) if value else "")
    if ftype == "datetime":
        if not value:
            return None, ""
        local = ofields.Datetime.context_timestamp(record, value)
        text = local.strftime("%Y-%m-%d %H:%M")
        return text, text
    if ftype == "html":
        text = _short(html2plaintext(value or "").strip())
        return text, text
    text = _short(value if isinstance(value, str) else str(value or ""))
    return text, text


def _column(model, fname):
    field = model._fields[fname]
    return {"key": fname, "label": field.string or fname, "type": field.type}


# ---------------------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------------------
def build_domain(env, spec, filters):
    model = env[spec["model"]]
    domain = list(spec.get("domain") or [])
    for flt in filters or []:
        if not isinstance(flt, dict):
            raise ToolError("Each filter must be an object with field, operator and value.")
        path = flt.get("field")
        operator = (flt.get("operator") or "=").strip().lower()
        value = flt.get("value")
        if operator not in FILTER_OPERATORS:
            raise ToolError("Unsupported operator '%s'. Use one of: %s"
                            % (operator, ", ".join(sorted(FILTER_OPERATORS))))
        _final_model, field = _check_path(model, path, "filter")
        if operator == "is_set":
            domain.append((path, "!=", False))
            continue
        if operator == "is_not_set":
            domain.append((path, "=", False))
            continue
        if operator in ("in", "not in"):
            if not isinstance(value, list):
                value = [value]
        if field.type == "many2one" and isinstance(value, str) and operator in ("=", "!="):
            # "partner_id = 'Azure Interior'" -> match on the related record's name
            operator = "ilike" if operator == "=" else "not ilike"
        if field.type == "selection" and isinstance(value, str):
            sel = _selection_map(model, field)
            if value not in sel:
                for key, label in sel.items():
                    if str(label).lower() == value.lower():
                        value = key
                        break
        if isinstance(value, (dict,)):
            raise ToolError("Filter values must be text, numbers, booleans or lists.")
        domain.append((path, operator, value))
    return domain


def _order_clause(model, spec, order_by):
    if not order_by:
        return spec.get("order") or "id desc"
    parts = []
    for chunk in str(order_by).split(","):
        bits = chunk.strip().split()
        if not bits:
            continue
        fname = bits[0]
        direction = bits[1].lower() if len(bits) > 1 else "asc"
        if direction not in ("asc", "desc"):
            raise ToolError("Invalid sort direction '%s'." % direction)
        field = _check_field_name(model, fname)
        if not field._description_sortable(model.env):
            raise ToolError("Cannot sort by '%s' (not stored)." % fname)
        parts.append("%s %s" % (fname, direction))
    return ", ".join(parts) or spec.get("order") or "id desc"


def _limit(value, default=DEFAULT_ROWS):
    try:
        value = int(value or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_ROWS))


# ---------------------------------------------------------------------------------------
# Record resolution
# ---------------------------------------------------------------------------------------
def resolve_record(env, model_name, value, extra_domain=None, label=None):
    """Find exactly one readable record from an id, ``{"id": ..}`` or a name.

    Raises ToolError(code="ambiguous") with options when several records match, and
    ToolError(code="not_found") when none does.
    """
    Model = env[model_name]
    label = label or env["ir.model"]._get(model_name).name or model_name
    domain = list(extra_domain or []) + list(M2O_DOMAINS.get(model_name, []))
    if isinstance(value, dict):
        value = value.get("id") or value.get("name")
    if isinstance(value, bool) or value in (None, ""):
        raise ToolError("No %s given." % label, code="missing_information")
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()
                                           and model_name not in ("product.product",)):
        rec = Model.search([("id", "=", int(value))] + domain, limit=1)
        if rec:
            return rec
        if not isinstance(value, str):
            raise ToolError("%s with id %s was not found or you cannot access it." % (label, value),
                            code="not_found")
    name = str(value).strip()
    rec_name = Model._rec_name or "name"
    if rec_name in Model._fields:
        exact = Model.search([(rec_name, "=ilike", name)] + domain, limit=6)
        if len(exact) == 1:
            return exact
    else:
        exact = Model.browse()
    found = Model.browse([rid for rid, _n in Model.name_search(name, domain, "ilike", limit=6)])
    if exact and len(exact) > 1:
        found = exact
    if len(found) == 1:
        return found
    if not found:
        raise ToolError("No %s matching '%s' was found (or you cannot access it)." % (label, name),
                        code="not_found", searched=name)
    raise ToolError("Several %s records match '%s'. Ask the user which one they mean." % (label, name),
                    code="ambiguous", options=[{"id": r.id, "name": r.display_name} for r in found[:6]])


def _parse_number(value, label):
    if isinstance(value, bool):
        raise ToolError("'%s' must be a number." % label, code="invalid_value")
    if isinstance(value, (int, float)):
        return value
    text = re.sub(r"[^\d.,\-]", "", str(value or ""))
    if text.count(",") and text.count("."):
        text = text.replace(",", "")
    elif text.count(",") == 1 and len(text.split(",")[1]) != 3:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError as e:
        raise ToolError("'%s' must be a number (got '%s')." % (label, value), code="invalid_value") from e


def _parse_date(value, label):
    if isinstance(value, date):
        return value
    try:
        return ofields.Date.to_date(str(value).strip()[:10])
    except (ValueError, TypeError) as e:
        raise ToolError("'%s' must be a date as YYYY-MM-DD (got '%s')." % (label, value),
                        code="invalid_value") from e


def _parse_datetime(env, value, label):
    text = str(value or "").strip().replace("T", " ")
    try:
        if len(text) <= 10:
            local = datetime.combine(_parse_date(text, label), datetime.min.time()).replace(hour=9)
        else:
            local = datetime.strptime(text[:16], "%Y-%m-%d %H:%M")
    except ValueError as e:
        raise ToolError("'%s' must be a date and time as YYYY-MM-DD HH:MM." % label,
                        code="invalid_value") from e
    tz = pytz.timezone(env.user.tz or "UTC")
    return tz.localize(local).astimezone(pytz.utc).replace(tzinfo=None)


def convert_values(env, spec, values, operation, record=None):
    """Validate and convert model-supplied values into ORM values.

    Returns ``(vals, preview)`` where preview is a list of ``{"label", "value", "old"}``.
    """
    if not isinstance(values, dict) or not values:
        raise ToolError("No field values were given.", code="missing_information")
    allowed = spec.get(operation) or []
    model = env[spec["model"]]
    vals, preview = {}, []
    for fname, raw in values.items():
        if fname not in allowed:
            raise ToolError("The assistant cannot set '%s' on %s. Allowed fields: %s"
                            % (fname, spec["label"], ", ".join(allowed)), code="field_not_allowed")
        if fname == "partner_role":
            role = str(raw or "").strip().lower()
            if role not in ("customer", "vendor", "supplier", "both"):
                raise ToolError("partner_role must be customer, vendor or both.", code="invalid_value")
            if "customer_rank" in model._fields:
                if role in ("customer", "both"):
                    vals["customer_rank"] = max(record.customer_rank if record else 0, 1)
                if role in ("vendor", "supplier", "both"):
                    vals["supplier_rank"] = max(record.supplier_rank if record else 0, 1)
            preview.append({"label": "Type", "value": role.capitalize()})
            continue
        if fname not in model._fields:
            continue  # field of an app that is not installed
        field = _check_field_name(model, fname, "write")
        label = field.string or fname
        ftype = field.type
        if ftype == "many2one":
            if raw in (None, False, ""):
                vals[fname], shown = False, ""
            else:
                rec = resolve_record(env, field.comodel_name, raw, label=label)
                vals[fname], shown = rec.id, rec.display_name
        elif ftype == "many2many":
            items = raw if isinstance(raw, list) else [raw]
            recs = env[field.comodel_name]
            for item in items:
                recs |= resolve_record(env, field.comodel_name, item, label=label)
            if operation == "create":
                vals[fname] = [Command.set(recs.ids)]
            else:
                vals[fname] = [Command.link(rid) for rid in recs.ids]
            shown = ", ".join(recs.mapped("display_name")) + ("" if operation == "create" else " (added)")
        elif ftype == "selection":
            sel = _selection_map(model, field)
            key = None
            for k, lbl in sel.items():
                if str(raw).lower() in (str(k).lower(), str(lbl).lower()):
                    key = k
                    break
            if key is None:
                raise ToolError("'%s' is not a valid %s. Options: %s"
                                % (raw, label, ", ".join("%s (%s)" % (v, k) for k, v in sel.items())),
                                code="invalid_value")
            vals[fname], shown = key, sel[key]
        elif ftype == "boolean":
            if isinstance(raw, str):
                raw = raw.strip().lower() in ("1", "true", "yes", "y", "on")
            vals[fname] = bool(raw)
            shown = "Yes" if raw else "No"
        elif ftype in ("integer", "float", "monetary"):
            number = _parse_number(raw, label)
            vals[fname] = int(number) if ftype == "integer" else number
            if ftype == "monetary":
                shown = format_amount(env, number, record and _currency_of(record, field)
                                      or env.company.currency_id)
            else:
                shown = "%g" % number
        elif ftype == "date":
            vals[fname] = _parse_date(raw, label)
            shown = ofields.Date.to_string(vals[fname])
        elif ftype == "datetime":
            vals[fname] = _parse_datetime(env, raw, label)
            shown = str(raw)
        elif ftype in ("char", "text", "html"):
            vals[fname] = str(raw if raw is not None else "").strip()
            shown = _short(vals[fname], 120)
        else:
            raise ToolError("Field '%s' cannot be set by the assistant." % fname, code="field_not_allowed")
        item = {"label": label, "value": shown}
        if record is not None:
            item["old"] = format_value(record, fname)[1]
        preview.append(item)
    return vals, preview


def _json_safe(vals):
    """Make ORM values JSON-serialisable for storage in the pending action."""
    out = {}
    for key, value in vals.items():
        if isinstance(value, (date, datetime)):
            out[key] = {"__date__": ofields.Datetime.to_string(value)
                        if isinstance(value, datetime) else ofields.Date.to_string(value),
                        "dt": isinstance(value, datetime)}
        elif isinstance(value, list):
            out[key] = [list(c) if isinstance(c, (tuple, list)) else c for c in value]
        else:
            out[key] = value
    return out


def _from_json(vals):
    out = {}
    for key, value in (vals or {}).items():
        if isinstance(value, dict) and "__date__" in value:
            out[key] = (ofields.Datetime.to_datetime(value["__date__"]) if value.get("dt")
                        else ofields.Date.to_date(value["__date__"]))
        elif isinstance(value, list) and value and isinstance(value[0], list):
            out[key] = [tuple(c) for c in value]
        else:
            out[key] = value
    return out


def _find_duplicates(env, spec, vals):
    model = env[spec["model"]]
    checks = []
    if spec["model"] == "res.partner":
        if vals.get("name"):
            checks.append([("name", "=ilike", vals["name"])])
        if vals.get("email"):
            checks.append([("email", "=ilike", vals["email"])])
        if vals.get("vat"):
            checks.append([("vat", "=", vals["vat"])])
    elif spec["model"] == "crm.lead":
        if vals.get("name"):
            checks.append([("name", "=ilike", vals["name"])])
    elif spec["model"] == "product.product":
        if vals.get("name"):
            checks.append([("name", "=ilike", vals["name"])])
        if vals.get("default_code"):
            checks.append([("default_code", "=", vals["default_code"])])
    found = model.browse()
    for dom in checks:
        found |= model.search(list(spec.get("domain") or []) + dom, limit=5)
    return [{"id": r.id, "name": r.display_name} for r in found[:5]]


def _record_link(record):
    return {"model": record._name, "id": record.id, "name": record.display_name}


def _check_model_access(env, model_name, operation):
    if not env[model_name].has_access(operation):
        label = env["ir.model"]._get(model_name).name or model_name
        raise ToolError("You do not have the rights to %s %s records." % (operation, label),
                        code="permission_denied")


# ---------------------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------------------
TOOLS = {}


def tool(name, write=False):
    def decorator(func):
        TOOLS[name] = {"func": func, "write": write}
        return func
    return decorator


def dispatch(ctx, name, arguments):
    """Run one tool call and return a JSON-serialisable result for the model."""
    entry = TOOLS.get(name)
    if not entry:
        return {"ok": False, "error": "unknown_tool", "message": "Unknown tool '%s'." % name}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return {"ok": False, "error": "invalid_arguments", "message": "Arguments were not valid JSON."}
    if not isinstance(arguments, dict):
        arguments = {}
    if entry["write"] and not (ctx.allow_create or ctx.allow_update):
        return {"ok": False, "error": "disabled",
                "message": "Creating and updating records is disabled by the administrator."}
    try:
        with ctx.env.cr.savepoint():
            return entry["func"](ctx, **arguments)
    except ToolError as e:
        result = {"ok": False, "error": e.code, "message": e.message}
        result.update(e.extra)
        return result
    except AccessError as e:
        return {"ok": False, "error": "permission_denied", "message": _clean(e)}
    except MissingError as e:
        return {"ok": False, "error": "not_found", "message": _clean(e)}
    except (UserError, ValidationError) as e:
        return {"ok": False, "error": "rejected_by_odoo", "message": _clean(e)}
    except TypeError as e:
        _logger.info("AI tool %s called with bad arguments: %s", name, e)
        return {"ok": False, "error": "invalid_arguments", "message": "Invalid arguments for %s: %s" % (name, e)}
    except ValueError as e:
        return {"ok": False, "error": "invalid_value", "message": str(e)[:300]}


def _clean(exc):
    text = getattr(exc, "args", [str(exc)])[0] if getattr(exc, "args", None) else str(exc)
    return str(text).split("\n\n")[0][:500]


# ---------------------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------------------
@tool("describe_object")
def describe_object(ctx, object):  # noqa: A002 - name is part of the tool schema
    spec = get_spec(ctx.env, object)
    model = ctx.env[spec["model"]]
    out = []
    writable = set(spec.get("create") or []) | set(spec.get("update") or [])
    for fname, field in sorted(model._fields.items()):
        if fname.startswith("__") or field.type in BLOCKED_TYPES or BLOCKED_FIELD_RE.search(fname):
            continue
        if not _field_accessible(model, field):
            continue
        if not (field.store or field._description_searchable) and fname not in spec.get("default_fields", []):
            continue
        item = {"name": fname, "label": field.string, "type": field.type}
        if field.type == "selection":
            item["options"] = list(_selection_map(model, field).keys())[:15]
        if field.relational:
            item["relation"] = field.comodel_name
        if fname in writable:
            item["settable"] = True
        out.append(item)
    for vname, help_text in (spec.get("virtual_fields") or {}).items():
        out.append({"name": vname, "type": "virtual", "help": help_text, "settable": True})
    return {"ok": True, "object": object, "model": spec["model"], "notes": spec.get("states_note"),
            "fields": out[:150]}


@tool("search_records")
def search_records(ctx, object, filters=None, fields=None, order_by=None, limit=None):  # noqa: A002
    spec = get_spec(ctx.env, object)
    model = ctx.env[spec["model"]]
    domain = build_domain(ctx.env, spec, filters)
    fnames = [f for f in (fields or spec.get("default_fields") or ["display_name"]) if f]
    for fname in fnames:
        _check_field_name(model, fname)
    order = _order_clause(model, spec, order_by)
    limit = _limit(limit)
    records = model.search(domain, order=order, limit=limit)
    total = model.search_count(domain) if len(records) == limit else len(records)
    rows, display_rows = [], []
    for rec in records:
        row, cells = {"id": rec.id}, {}
        for fname in fnames:
            row[fname], cells[fname] = format_value(rec, fname)
        rows.append(row)
        display_rows.append({"id": rec.id, "cells": cells})
    ctx.displays.append({
        "type": "table", "model": spec["model"], "title": spec["label"],
        "columns": [_column(model, f) for f in fnames], "rows": display_rows, "total": total,
    })
    ctx.log_read(spec, "read", len(records))
    return {"ok": True, "object": object, "total_matching": total, "returned": len(rows), "rows": rows,
            "note": "The table is shown to the user; summarise it, do not repeat every row."}


@tool("aggregate_records")
def aggregate_records(ctx, object, measures=None, group_by=None, filters=None,  # noqa: A002
                      order_by=None, limit=None):
    spec = get_spec(ctx.env, object)
    model = ctx.env[spec["model"]]
    domain = build_domain(ctx.env, spec, filters)
    groupby = []
    for gb in group_by or []:
        fname, _sep, gran = str(gb).partition(":")
        field = _check_field_name(model, fname)
        if not field._description_groupable(ctx.env):
            raise ToolError("Cannot group by '%s'." % fname)
        if field.type in ("date", "datetime"):
            gran = gran or "month"
            if gran not in DATE_GRANULARITY:
                raise ToolError("Date grouping must be one of %s." % ", ".join(sorted(DATE_GRANULARITY)))
            groupby.append("%s:%s" % (fname, gran))
        else:
            groupby.append(fname)
    aggregates = ["__count"]
    measure_fields = []
    for measure in measures or []:
        fname, _sep, agg = str(measure).partition(":")
        agg = agg or "sum"
        if fname in ("count", "__count"):
            continue
        field = _check_field_name(model, fname)
        if agg not in AGGREGATORS:
            raise ToolError("Aggregation must be one of %s." % ", ".join(sorted(AGGREGATORS)))
        if agg != "count_distinct" and field.type not in ("integer", "float", "monetary"):
            raise ToolError("'%s' is not numeric; use count_distinct or a numeric field." % fname)
        if not field.store:
            raise ToolError("'%s' is computed and not stored, it cannot be totalled." % fname)
        aggregates.append("%s:%s" % (fname, agg))
        measure_fields.append((fname, agg, field))
    order = None
    if order_by:
        key, _sp, direction = str(order_by).strip().partition(" ")
        direction = (direction or "desc").strip().lower()
        if direction not in ("asc", "desc"):
            raise ToolError("Invalid sort direction.")
        if key in ("count", "__count") or key in aggregates or key in groupby:
            order = "%s %s" % (key, direction)
        else:
            raise ToolError("order_by must be one of the measures (e.g. 'amount_total:sum desc'), "
                            "'__count' or a group_by field.")
    limit = _limit(limit, default=MAX_ROWS) if groupby else None
    groups = model._read_group(domain, groupby=groupby, aggregates=aggregates, order=order, limit=limit)

    currency = ctx.env.company.currency_id
    columns = [{"key": g, "label": model._fields[g.split(":")[0]].string, "type": "group"} for g in groupby]
    columns.append({"key": "__count", "label": "Count", "type": "integer"})
    for fname, agg, field in measure_fields:
        label = field.string + ("" if agg == "sum" else " (%s)" % agg)
        columns.append({"key": "%s:%s" % (fname, agg), "label": label, "type": field.type})

    rows, display_rows = [], []
    for group in groups:
        row, cells, link = {}, {}, None
        for i, gkey in enumerate(groupby):
            value = group[i]
            if hasattr(value, "_name"):
                text = value.display_name if value else "(none)"
                row[gkey] = {"id": value.id, "name": text} if value else None
                if value and len(groupby) == 1:
                    link = {"model": value._name, "id": value.id}
            elif isinstance(value, (date, datetime)):
                text = _format_period(value, gkey.split(":")[1])
                row[gkey] = text
            elif value is False or value is None:
                text = "(none)"
                row[gkey] = None
            else:
                field = model._fields[gkey.split(":")[0]]
                text = _selection_map(model, field).get(value, value) if field.type == "selection" else value
                row[gkey] = value
            cells[gkey] = str(text)
        offset = len(groupby)
        row["count"] = group[offset]
        cells["__count"] = str(group[offset])
        for j, (fname, agg, field) in enumerate(measure_fields):
            value = group[offset + 1 + j] or 0
            key = "%s:%s" % (fname, agg)
            row[key] = round(value, 2) if isinstance(value, float) else value
            cells[key] = (format_amount(ctx.env, value, currency) if field.type == "monetary"
                          and agg != "count_distinct" else ("%g" % value if isinstance(value, float) else str(value)))
        rows.append(row)
        display_rows.append({"cells": cells, "link": link})

    multi_currency = _currencies_in(ctx.env, model, domain, measure_fields)
    ctx.displays.append({"type": "table", "model": spec["model"], "title": spec["label"] + " — summary",
                         "columns": columns, "rows": display_rows, "total": len(rows), "grouped": True})
    ctx.log_read(spec, "aggregate", len(rows), detail="group by %s" % (", ".join(groupby) or "-"))
    result = {"ok": True, "object": object, "groups": rows}
    if multi_currency:
        result["warning"] = ("Amounts are in different currencies (%s) and were added as raw numbers. "
                             "Prefer the *_signed company-currency fields where available."
                             % ", ".join(multi_currency))
    return result


def _currencies_in(env, model, domain, measure_fields):
    if "currency_id" not in model._fields or not any(f.type == "monetary" for _n, _a, f in measure_fields):
        return []
    groups = model._read_group(domain, groupby=["currency_id"], aggregates=["__count"], limit=5)
    names = [g[0].name for g in groups if g[0]]
    return names if len(names) > 1 else []


def _format_period(value, granularity):
    if granularity == "year":
        return value.strftime("%Y")
    if granularity == "quarter":
        return "Q%d %d" % ((value.month - 1) // 3 + 1, value.year)
    if granularity == "month":
        return value.strftime("%B %Y")
    if granularity == "week":
        return "Week of %s" % value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%d")


DETAIL_LINES = {
    "sale.order": ("order_line", ["product_id", "name", "product_uom_qty", "price_unit", "discount",
                                  "price_subtotal"]),
    "purchase.order": ("order_line", ["product_id", "name", "product_qty", "price_unit", "price_subtotal",
                                      "qty_received"]),
    "account.move": ("invoice_line_ids", ["product_id", "name", "quantity", "price_unit", "price_subtotal"]),
    "stock.picking": ("move_ids", ["product_id", "product_uom_qty", "quantity", "state"]),
}

EXTRA_DETAIL_FIELDS = {
    "res.partner": ["street", "zip", "state_id", "website", "vat", "function", "category_id",
                    "customer_rank", "supplier_rank", "credit", "debit", "total_invoiced"],
    "crm.lead": ["contact_name", "partner_name", "email_from", "phone", "team_id", "priority", "tag_ids",
                 "prorated_revenue", "description"],
    "sale.order": ["validity_date", "client_order_ref", "amount_untaxed", "amount_tax", "pricelist_id",
                   "payment_term_id", "commitment_date"],
    "purchase.order": ["partner_ref", "amount_untaxed", "amount_tax", "user_id", "payment_term_id"],
    "account.move": ["ref", "amount_untaxed", "amount_tax", "invoice_origin", "invoice_payment_term_id",
                     "amount_residual_signed"],
    "product.product": ["categ_id", "barcode", "is_storable", "sale_ok", "purchase_ok", "description_sale"],
}


@tool("get_record")
def get_record(ctx, object, record_id=None, name=None):  # noqa: A002
    spec = get_spec(ctx.env, object)
    model = ctx.env[spec["model"]]
    record = resolve_record(ctx.env, spec["model"], record_id or name, extra_domain=spec.get("domain"),
                            label=_noun(spec))
    fnames = list(dict.fromkeys((spec.get("default_fields") or [])
                                + EXTRA_DETAIL_FIELDS.get(spec["model"], [])))
    data, display_fields = {"id": record.id, "name": record.display_name}, []
    for fname in fnames:
        if fname not in model._fields or not _field_accessible(model, model._fields[fname]):
            continue
        try:
            data[fname], text = format_value(record, fname)
        except AccessError:
            continue
        if text not in ("", None):
            display_fields.append({"label": model._fields[fname].string, "value": text})
    display = {"type": "record", "model": spec["model"], "id": record.id, "title": record.display_name,
               "fields": display_fields}
    line_info = DETAIL_LINES.get(spec["model"])
    if line_info and line_info[0] in model._fields:
        lines = record[line_info[0]]
        if "display_type" in lines._fields:
            lines = lines.filtered(lambda l: not l.display_type)
        lmodel = lines.browse()
        lfields = [f for f in line_info[1] if f in lmodel._fields]
        data["lines"] = [{f: format_value(line, f)[0] for f in lfields} for line in lines[:30]]
        display["lines"] = {"columns": [_column(lmodel, f) for f in lfields],
                            "rows": [{"cells": {f: format_value(line, f)[1] for f in lfields}}
                                     for line in lines[:30]]}
    ctx.displays.append(display)
    ctx.log_read(spec, "read", 1, detail=record.display_name)
    return {"ok": True, "object": object, "record": data}


def _period(ctx, date_from, date_to):
    today = ctx.today()
    start = _parse_date(date_from, "date_from") if date_from else today.replace(day=1)
    end = _parse_date(date_to, "date_to") if date_to else (start + relativedelta(months=1, days=-1)
                                                           if not date_from else today)
    if end < start:
        start, end = end, start
    return start, end


def _money(ctx, amount):
    return format_amount(ctx.env, amount or 0.0, ctx.env.company.currency_id)


def _sum(model, domain, field):
    groups = model._read_group(domain, aggregates=["%s:sum" % field, "__count"])
    total, count = groups[0] if groups else (0.0, 0)
    return total or 0.0, count or 0


@tool("business_summary")
def business_summary(ctx, topic, date_from=None, date_to=None, limit=None, threshold=None):
    """Pre-built, reliable summaries used for common management questions."""
    env = ctx.env
    topic = (topic or "").lower()
    limit = _limit(limit, default=5)
    stats, tables, data = [], [], {"topic": topic}
    title = topic.replace("_", " ").title()

    def need(model_name, app):
        if model_name not in env:
            raise ToolError("The %s app is not installed." % app, code="app_not_installed")
        _check_model_access(env, model_name, "read")
        return env[model_name]

    if topic == "sales":
        SO = need("sale.order", "Sales")
        start, end = _period(ctx, date_from, date_to)
        title = "Sales %s → %s" % (start, end)
        dom = [("state", "=", "sale"), ("date_order", ">=", ofields.Datetime.to_string(
            datetime.combine(start, datetime.min.time()))),
            ("date_order", "<", ofields.Datetime.to_string(datetime.combine(end + timedelta(days=1),
                                                                             datetime.min.time())))]
        by_cur = SO._read_group(dom, groupby=["currency_id"], aggregates=["amount_untaxed:sum",
                                                                           "amount_total:sum", "__count"])
        data["confirmed_orders"] = []
        for cur, untaxed, total, count in by_cur:
            data["confirmed_orders"].append({"currency": cur.name, "count": count,
                                             "untaxed": round(untaxed, 2), "total": round(total, 2)})
            stats.append({"label": "Orders (%s)" % cur.name, "value": str(count)})
            stats.append({"label": "Revenue untaxed (%s)" % cur.name,
                          "value": format_amount(env, untaxed, cur)})
        if not by_cur:
            stats.append({"label": "Confirmed orders", "value": "0"})
        quotes = SO._read_group([("state", "in", ("draft", "sent"))], aggregates=["__count", "amount_total:sum"])
        data["open_quotations"] = {"count": quotes[0][0], "amount_total": round(quotes[0][1] or 0, 2)}
        stats.append({"label": "Open quotations", "value": str(quotes[0][0])})
        top = SO._read_group(dom, groupby=["partner_id"], aggregates=["amount_untaxed:sum"],
                             order="amount_untaxed:sum desc", limit=limit)
        data["top_customers"] = [{"id": p.id, "name": p.display_name, "untaxed": round(a, 2)} for p, a in top]
        tables.append({"title": "Top customers (untaxed)", "columns": [
            {"key": "name", "label": "Customer"}, {"key": "amount", "label": "Untaxed", "type": "monetary"}],
            "model": "res.partner", "rows": [{"id": p.id, "cells": {"name": p.display_name,
                                                                    "amount": _money(ctx, a)}} for p, a in top]})
        if "sale.order.line" in env:
            SOL = env["sale.order.line"]
            ldom = [("order_id", "in", SO.search(dom).ids), ("display_type", "=", False)]
            topp = SOL._read_group(ldom, groupby=["product_id"], aggregates=["price_subtotal:sum",
                                                                              "product_uom_qty:sum"],
                                   order="price_subtotal:sum desc", limit=limit)
            data["top_products"] = [{"id": p.id, "name": p.display_name, "revenue": round(a, 2),
                                     "quantity": q} for p, a, q in topp if p]
            tables.append({"title": "Top products", "model": "product.product", "columns": [
                {"key": "name", "label": "Product"}, {"key": "qty", "label": "Qty", "type": "float"},
                {"key": "amount", "label": "Revenue", "type": "monetary"}],
                "rows": [{"id": p.id, "cells": {"name": p.display_name, "qty": "%g" % q,
                                                "amount": _money(ctx, a)}} for p, a, q in topp if p]})
    elif topic == "pipeline":
        Lead = need("crm.lead", "CRM")
        dom = [("type", "=", "opportunity"), ("probability", "<", 100), ("active", "=", True)]
        groups = Lead._read_group(dom, aggregates=["__count", "expected_revenue:sum", "prorated_revenue:sum"])
        count, expected, weighted = groups[0]
        data.update({"open_opportunities": count, "expected_revenue": round(expected or 0, 2),
                     "weighted_revenue": round(weighted or 0, 2)})
        stats += [{"label": "Open opportunities", "value": str(count)},
                  {"label": "Pipeline value", "value": _money(ctx, expected)},
                  {"label": "Weighted value", "value": _money(ctx, weighted)}]
        mine = Lead.search_count(dom + [("user_id", "=", env.uid)])
        overdue = Lead.search_count([("activity_date_deadline", "<", ofields.Date.to_string(ctx.today())),
                                     ("active", "=", True), ("probability", "<", 100)])
        no_activity = Lead.search_count(dom + [("activity_ids", "=", False)])
        data.update({"assigned_to_me": mine, "overdue_follow_ups": overdue, "without_next_activity": no_activity})
        stats += [{"label": "Assigned to me", "value": str(mine)},
                  {"label": "Overdue follow-ups", "value": str(overdue)},
                  {"label": "No next activity", "value": str(no_activity)}]
        by_stage = Lead._read_group(dom, groupby=["stage_id"], aggregates=["__count", "expected_revenue:sum"])
        data["by_stage"] = [{"stage": s.display_name, "count": c, "expected_revenue": round(a or 0, 2)}
                            for s, c, a in by_stage]
        tables.append({"title": "By stage", "columns": [{"key": "stage", "label": "Stage"},
                                                         {"key": "count", "label": "Count", "type": "integer"},
                                                         {"key": "amount", "label": "Expected", "type": "monetary"}],
                       "rows": [{"cells": {"stage": s.display_name or "(none)", "count": str(c),
                                           "amount": _money(ctx, a)}} for s, c, a in by_stage]})
    elif topic in ("receivables", "payables"):
        Move = need("account.move", "Invoicing")
        types = ("out_invoice", "out_refund") if topic == "receivables" else ("in_invoice", "in_refund")
        dom = [("move_type", "in", types), ("state", "=", "posted"),
               ("payment_state", "in", ("not_paid", "partial"))]
        total, count = _sum(Move, dom, "amount_residual_signed")
        today = ofields.Date.to_string(ctx.today())
        overdue, ocount = _sum(Move, dom + [("invoice_date_due", "<", today)], "amount_residual_signed")
        sign = 1 if topic == "receivables" else -1
        title = "Customer receivables" if topic == "receivables" else "Vendor payables"
        data.update({"outstanding_total": round(sign * total, 2), "open_documents": count,
                     "overdue_total": round(sign * overdue, 2), "overdue_documents": ocount,
                     "currency": env.company.currency_id.name})
        stats += [{"label": "Outstanding", "value": _money(ctx, sign * total)},
                  {"label": "Open documents", "value": str(count)},
                  {"label": "Overdue", "value": _money(ctx, sign * overdue)},
                  {"label": "Overdue documents", "value": str(ocount)}]
        top = Move._read_group(dom, groupby=["partner_id"], aggregates=["amount_residual_signed:sum"],
                               order="amount_residual_signed:sum %s" % ("desc" if sign > 0 else "asc"),
                               limit=limit)
        data["top_partners"] = [{"id": p.id, "name": p.display_name, "outstanding": round(sign * a, 2)}
                                for p, a in top]
        tables.append({"title": "Largest balances", "model": "res.partner",
                       "columns": [{"key": "name", "label": "Partner"}, {"key": "amount", "label": "Outstanding", "type": "monetary"}],
                       "rows": [{"id": p.id, "cells": {"name": p.display_name, "amount": _money(ctx, sign * a)}}
                                for p, a in top]})
    elif topic == "financial":
        Move = need("account.move", "Invoicing")
        start, end = _period(ctx, date_from, date_to)
        title = "Financial summary %s → %s" % (start, end)
        period = [("state", "=", "posted"), ("invoice_date", ">=", ofields.Date.to_string(start)),
                  ("invoice_date", "<=", ofields.Date.to_string(end))]
        revenue, rcount = _sum(Move, period + [("move_type", "in", ("out_invoice", "out_refund"))],
                               "amount_untaxed_signed")
        bills, bcount = _sum(Move, period + [("move_type", "in", ("in_invoice", "in_refund"))],
                             "amount_untaxed_signed")
        open_dom = [("state", "=", "posted"), ("payment_state", "in", ("not_paid", "partial"))]
        receivable, _c = _sum(Move, open_dom + [("move_type", "in", ("out_invoice", "out_refund"))],
                              "amount_residual_signed")
        payable, _c2 = _sum(Move, open_dom + [("move_type", "in", ("in_invoice", "in_refund"))],
                            "amount_residual_signed")
        bills = -bills
        data.update({"period": [str(start), str(end)], "invoiced_revenue_untaxed": round(revenue, 2),
                     "customer_invoices": rcount, "vendor_bills_untaxed": round(bills, 2),
                     "vendor_bills": bcount, "revenue_minus_bills": round(revenue - bills, 2),
                     "receivables_outstanding_now": round(receivable, 2),
                     "payables_outstanding_now": round(-payable, 2),
                     "currency": env.company.currency_id.name,
                     "basis": "posted invoices and bills by invoice date, untaxed, company currency"})
        stats += [{"label": "Invoiced revenue (untaxed)", "value": _money(ctx, revenue)},
                  {"label": "Vendor bills (untaxed)", "value": _money(ctx, bills)},
                  {"label": "Revenue − bills", "value": _money(ctx, revenue - bills)},
                  {"label": "Customer invoices", "value": str(rcount)},
                  {"label": "Receivables now", "value": _money(ctx, receivable)},
                  {"label": "Payables now", "value": _money(ctx, -payable)}]
        monthly = Move._read_group(period + [("move_type", "in", ("out_invoice", "out_refund"))],
                                   groupby=["invoice_date:month"], aggregates=["amount_untaxed_signed:sum"])
        if len(monthly) > 1:
            tables.append({"title": "Revenue by month", "columns": [{"key": "m", "label": "Month"},
                                                                    {"key": "a", "label": "Revenue", "type": "monetary"}],
                           "rows": [{"cells": {"m": _format_period(m, "month"), "a": _money(ctx, a)}}
                                    for m, a in monthly]})
    elif topic in ("low_stock", "out_of_stock"):
        Product = need("product.product", "Inventory")
        if "is_storable" not in Product._fields or "qty_available" not in Product._fields:
            raise ToolError("The Inventory app is not installed.", code="app_not_installed")
        limit_value = 0.0 if topic == "out_of_stock" else float(_parse_number(threshold, "threshold")
                                                                if threshold not in (None, "") else 5)
        dom = [("is_storable", "=", True), ("qty_available", "<=", limit_value)]
        products = Product.search(dom, limit=MAX_ROWS)
        title = "Out of stock" if topic == "out_of_stock" else "Low stock (≤ %g)" % limit_value
        data["products"] = [{"id": p.id, "name": p.display_name, "on_hand": p.qty_available,
                             "forecast": p.virtual_available} for p in products]
        data["threshold"] = limit_value
        stats.append({"label": "Products", "value": str(len(products))})
        tables.append({"title": title, "model": "product.product",
                       "columns": [{"key": "name", "label": "Product"}, {"key": "qty", "label": "On hand", "type": "float"},
                                   {"key": "fc", "label": "Forecast", "type": "float"}],
                       "rows": [{"id": p.id, "cells": {"name": p.display_name, "qty": "%g" % p.qty_available,
                                                       "fc": "%g" % p.virtual_available}} for p in products]})
        if "stock.warehouse.orderpoint" in env and env["stock.warehouse.orderpoint"].has_access("read"):
            below = env["stock.warehouse.orderpoint"].search([("trigger", "in", ("auto", "manual"))], limit=200)
            below = below.filtered(lambda o: o.qty_forecast < o.product_min_qty)[:limit]
            data["below_reorder_minimum"] = [{"product": o.product_id.display_name, "forecast": o.qty_forecast,
                                              "minimum": o.product_min_qty} for o in below]
    else:
        raise ToolError("Unknown topic. Use one of: sales, pipeline, receivables, payables, financial, "
                        "low_stock, out_of_stock.")
    ctx.displays.append({"type": "summary", "title": title, "stats": stats, "tables": tables})
    ctx.log_read({"model": "dcs.ai.summary"}, "aggregate", len(stats), detail=topic)
    data["ok"] = True
    data["note"] = "These figures come from the database and are shown to the user as cards."
    return data


# ---------------------------------------------------------------------------------------
# Write tools (prepare only)
# ---------------------------------------------------------------------------------------
def _require(ctx, kind):
    if kind == "create" and not ctx.allow_create:
        raise ToolError("Creating records with the assistant is disabled by the administrator.",
                        code="disabled")
    if kind == "update" and not ctx.allow_update:
        raise ToolError("Updating records with the assistant is disabled by the administrator.",
                        code="disabled")


@tool("prepare_create_record", write=True)
def prepare_create_record(ctx, object, values=None):  # noqa: A002
    _require(ctx, "create")
    spec = get_spec(ctx.env, object)
    if not spec.get("create"):
        raise ToolError("The assistant cannot create %s records directly. %s"
                        % (spec["label"], _create_hint(object)), code="unsupported")
    _check_model_access(ctx.env, spec["model"], "create")
    values = dict(values or {})
    missing = [f for f in spec.get("required", []) if not values.get(f)]
    if missing:
        raise ToolError("Missing required information: %s." % ", ".join(missing),
                        code="missing_information", missing=missing)
    if object == "lead" and "type" not in values:
        values["type"] = "opportunity" if any(k in values for k in ("expected_revenue", "stage_id")) else "lead"
    vals, preview = convert_values(ctx.env, spec, values, "create")
    duplicates = _find_duplicates(ctx.env, spec, vals)
    warnings = []
    if duplicates:
        warnings.append("Possible duplicate: %s already exist(s)." % ", ".join(d["name"] for d in duplicates))
    summary = "Create %s \"%s\"" % (spec["label"].split(" (")[0].lower(), vals.get("name", ""))
    result = ctx.create_pending("create", spec, {"tool": "create_record", "object": object,
                                                 "vals": _json_safe(vals)}, summary, preview,
                                warnings=warnings)
    if duplicates:
        result["possible_duplicates"] = duplicates
    return result


def _create_hint(object):  # noqa: A002
    return {
        "sale_order": "Use prepare_sale_quotation.",
        "purchase_order": "Use prepare_purchase_order.",
        "invoice": "Use prepare_invoice.",
        "vendor_bill": "Use prepare_invoice with move_type in_invoice.",
        "activity": "Use prepare_schedule_activity.",
        "stock_quant": "Use prepare_inventory_adjustment.",
    }.get(object, "")


@tool("prepare_update_record", write=True)
def prepare_update_record(ctx, object, values=None, record_id=None, name=None):  # noqa: A002
    _require(ctx, "update")
    spec = get_spec(ctx.env, object)
    if not spec.get("update"):
        raise ToolError("The assistant cannot update %s records." % spec["label"], code="unsupported")
    record = resolve_record(ctx.env, spec["model"], record_id or name, extra_domain=spec.get("domain"),
                            label=_noun(spec))
    _check_model_access(ctx.env, spec["model"], "write")
    record.check_access("write")
    vals, preview = convert_values(ctx.env, spec, values, "update", record=record)
    if not vals:
        raise ToolError("Nothing to change.", code="missing_information")
    summary = "Update %s \"%s\"" % (spec["label"].split(" (")[0].lower(), record.display_name)
    return ctx.create_pending("update", spec, {"tool": "update_record", "object": object,
                                               "vals": _json_safe(vals)}, summary, preview, res_id=record.id)


def _resolve_lines(ctx, lines, product_domain, qty_key):
    if not lines or not isinstance(lines, list):
        raise ToolError("Which products and quantities?", code="missing_information")
    out, preview = [], []
    for line in lines:
        if not isinstance(line, dict):
            raise ToolError("Each line must be an object with product and quantity.")
        product = resolve_record(ctx.env, "product.product", line.get("product"), extra_domain=product_domain,
                                 label="product")
        qty = _parse_number(line.get("quantity", 1), "quantity")
        if qty <= 0:
            raise ToolError("Quantities must be positive.", code="invalid_value")
        vals = {"product_id": product.id, qty_key: qty}
        text = "%s × %g" % (product.display_name, qty)
        if line.get("price_unit") not in (None, ""):
            vals["price_unit"] = _parse_number(line["price_unit"], "price_unit")
            text += " @ %s" % _money(ctx, vals["price_unit"])
        if line.get("discount") not in (None, ""):
            vals["discount"] = _parse_number(line["discount"], "discount")
            text += " (−%g%%)" % vals["discount"]
        out.append(vals)
        preview.append({"label": "Line", "value": text})
    return out, preview


@tool("prepare_sale_quotation", write=True)
def prepare_sale_quotation(ctx, customer=None, lines=None, validity_date=None, salesperson=None, note=None):
    _require(ctx, "create")
    spec = get_spec(ctx.env, "sale_order")
    _check_model_access(ctx.env, "sale.order", "create")
    if not customer:
        raise ToolError("For which customer should the quotation be created?", code="missing_information",
                        missing=["customer"])
    partner = resolve_record(ctx.env, "res.partner", customer, label="customer")
    line_vals, preview = _resolve_lines(ctx, lines, [("sale_ok", "=", True)], "product_uom_qty")
    vals = {"partner_id": partner.id}
    preview.insert(0, {"label": "Customer", "value": partner.display_name})
    if validity_date:
        vals["validity_date"] = ofields.Date.to_string(_parse_date(validity_date, "validity_date"))
        preview.append({"label": "Expiration", "value": vals["validity_date"]})
    if salesperson:
        user = resolve_record(ctx.env, "res.users", salesperson, label="salesperson")
        vals["user_id"] = user.id
        preview.append({"label": "Salesperson", "value": user.display_name})
    if note:
        vals["note"] = str(note)
    warnings = [] if all("price_unit" in l for l in line_vals) else [
        "Unit prices without an explicit price come from the customer's pricelist."]
    return ctx.create_pending("create", spec, {"tool": "sale_quotation", "vals": vals, "lines": line_vals},
                              "Create a quotation for %s" % partner.display_name, preview, warnings=warnings)


@tool("prepare_purchase_order", write=True)
def prepare_purchase_order(ctx, vendor=None, lines=None, date_planned=None, vendor_reference=None):
    _require(ctx, "create")
    spec = get_spec(ctx.env, "purchase_order")
    _check_model_access(ctx.env, "purchase.order", "create")
    if not vendor:
        raise ToolError("From which vendor?", code="missing_information", missing=["vendor"])
    partner = resolve_record(ctx.env, "res.partner", vendor, label="vendor")
    line_vals, preview = _resolve_lines(ctx, lines, [("purchase_ok", "=", True)], "product_qty")
    vals = {"partner_id": partner.id}
    preview.insert(0, {"label": "Vendor", "value": partner.display_name})
    if date_planned:
        vals["date_planned"] = ofields.Datetime.to_string(_parse_datetime(ctx.env, date_planned, "date_planned"))
        preview.append({"label": "Expected arrival", "value": str(date_planned)})
    if vendor_reference:
        vals["partner_ref"] = str(vendor_reference)
    warnings = [] if all("price_unit" in l for l in line_vals) else [
        "Unit prices without an explicit price come from the vendor price list or product cost."]
    return ctx.create_pending("create", spec, {"tool": "purchase_order", "vals": vals, "lines": line_vals},
                              "Create a request for quotation to %s" % partner.display_name, preview,
                              warnings=warnings)


@tool("prepare_order_lines_update", write=True)
def prepare_order_lines_update(ctx, object, lines=None, record_id=None, name=None, mode="add"):  # noqa: A002
    _require(ctx, "update")
    if object not in ("sale_order", "purchase_order"):
        raise ToolError("Only sale_order and purchase_order lines can be changed.")
    spec = get_spec(ctx.env, object)
    order = resolve_record(ctx.env, spec["model"], record_id or name, label=_noun(spec))
    order.check_access("write")
    if order.state not in ("draft", "sent"):
        raise ToolError("%s is already confirmed; its lines cannot be changed by the assistant."
                        % order.display_name, code="rejected_by_odoo")
    if mode not in ("add", "set_quantity", "remove"):
        raise ToolError("mode must be add, set_quantity or remove.")
    qty_key = "product_uom_qty" if object == "sale_order" else "product_qty"
    pdomain = [("sale_ok", "=", True)] if object == "sale_order" else [("purchase_ok", "=", True)]
    if mode == "remove":
        for line in lines or []:
            line.setdefault("quantity", 1)
    line_vals, preview = _resolve_lines(ctx, lines, pdomain, qty_key)
    if mode != "add":
        existing = order.order_line.mapped("product_id").ids
        missing = [l for l in line_vals if l["product_id"] not in existing]
        if missing:
            names = ctx.env["product.product"].browse([l["product_id"] for l in missing]).mapped("display_name")
            raise ToolError("%s has no line for: %s." % (order.display_name, ", ".join(names)), code="not_found")
    verb = {"add": "Add lines to", "set_quantity": "Change quantities on", "remove": "Remove lines from"}[mode]
    if mode == "remove":
        preview = [{"label": "Remove", "value": p["value"].split(" × ")[0]} for p in preview]
    return ctx.create_pending("update", spec, {"tool": "order_lines", "object": object, "mode": mode,
                                               "lines": line_vals, "qty_key": qty_key},
                              "%s %s" % (verb, order.display_name), preview, res_id=order.id)


@tool("prepare_invoice", write=True)
def prepare_invoice(ctx, partner=None, lines=None, move_type="out_invoice", invoice_date=None, reference=None):
    _require(ctx, "create")
    if move_type not in ("out_invoice", "in_invoice", "out_refund", "in_refund"):
        raise ToolError("move_type must be out_invoice, in_invoice, out_refund or in_refund.")
    spec = get_spec(ctx.env, "invoice" if move_type.startswith("out") else "vendor_bill")
    _check_model_access(ctx.env, "account.move", "create")
    if not partner:
        raise ToolError("For which customer or vendor?", code="missing_information", missing=["partner"])
    partner_rec = resolve_record(ctx.env, "res.partner", partner, label="partner")
    if not lines:
        raise ToolError("What should be invoiced (product or description, quantity and price)?",
                        code="missing_information", missing=["lines"])
    line_vals, preview = [], [{"label": "Partner", "value": partner_rec.display_name}]
    total = 0.0
    for line in lines:
        vals = {"quantity": _parse_number(line.get("quantity", 1), "quantity")}
        if line.get("product"):
            product = resolve_record(ctx.env, "product.product", line["product"], label="product")
            vals["product_id"] = product.id
            label = product.display_name
        else:
            if not line.get("description"):
                raise ToolError("Each line needs a product or a description.", code="missing_information")
            label = str(line["description"])
        if line.get("description"):
            vals["name"] = str(line["description"])
        if line.get("price_unit") not in (None, ""):
            vals["price_unit"] = _parse_number(line["price_unit"], "price_unit")
        elif not vals.get("product_id"):
            raise ToolError("What is the price for '%s'?" % label, code="missing_information")
        line_vals.append(vals)
        text = "%s × %g" % (label, vals["quantity"])
        if "price_unit" in vals:
            text += " @ %s" % _money(ctx, vals["price_unit"])
            total += vals["price_unit"] * vals["quantity"]
        preview.append({"label": "Line", "value": text})
    vals = {"partner_id": partner_rec.id, "move_type": move_type}
    if invoice_date:
        vals["invoice_date"] = ofields.Date.to_string(_parse_date(invoice_date, "invoice_date"))
        preview.append({"label": "Date", "value": vals["invoice_date"]})
    if reference:
        vals["ref"] = str(reference)
    kind = {"out_invoice": "customer invoice", "in_invoice": "vendor bill", "out_refund": "credit note",
            "in_refund": "vendor refund"}[move_type]
    warnings = ["It will be created as a draft; taxes come from the product or default accounts. "
                "Post it in Odoo (or ask me to post it) when it is correct."]
    return ctx.create_pending("create", spec, {"tool": "invoice", "vals": vals, "lines": line_vals},
                              "Create a draft %s for %s%s" % (kind, partner_rec.display_name,
                                                             (" (≈ %s before tax)" % _money(ctx, total)) if total else ""),
                              preview, warnings=warnings)


@tool("prepare_document_action", write=True)
def prepare_document_action(ctx, object, action, record_id=None, name=None):  # noqa: A002
    _require(ctx, "update")
    allowed = {"sale_order": {"confirm"}, "purchase_order": {"confirm"}, "invoice": {"post"},
               "vendor_bill": {"post"}, "lead": {"mark_won", "mark_lost"}}
    if action not in allowed.get(object, set()):
        raise ToolError("Supported actions: %s" % "; ".join("%s: %s" % (k, ", ".join(v)) for k, v in allowed.items()),
                        code="unsupported")
    spec = get_spec(ctx.env, object)
    record = resolve_record(ctx.env, spec["model"], record_id or name, extra_domain=spec.get("domain"),
                            label=_noun(spec))
    record.check_access("write")
    labels = {"confirm": "Confirm", "post": "Post", "mark_won": "Mark as won", "mark_lost": "Mark as lost"}
    preview = [{"label": "Document", "value": record.display_name}]
    if "amount_total" in record._fields:
        preview.append({"label": "Total", "value": format_value(record, "amount_total")[1]})
    return ctx.create_pending("update", spec, {"tool": "document_action", "object": object, "action": action},
                              "%s %s" % (labels[action], record.display_name), preview, res_id=record.id,
                              warnings=["This is a business action that may send documents, reserve stock "
                                        "or create journal entries."] if action in ("confirm", "post") else [])


@tool("prepare_schedule_activity", write=True)
def prepare_schedule_activity(ctx, object, summary=None, date_deadline=None, record_id=None, name=None,  # noqa: A002
                              activity_type=None, assigned_to=None, note=None):
    _require(ctx, "create")
    spec = get_spec(ctx.env, object)
    record = resolve_record(ctx.env, spec["model"], record_id or name, extra_domain=spec.get("domain"),
                            label=_noun(spec))
    if not hasattr(record, "activity_schedule"):
        raise ToolError("Activities cannot be scheduled on %s." % spec["label"], code="unsupported")
    _check_model_access(ctx.env, "mail.activity", "create")
    deadline = _parse_date(date_deadline, "date_deadline") if date_deadline else ctx.today()
    vals = {"summary": str(summary or "Follow-up"), "date_deadline": ofields.Date.to_string(deadline)}
    preview = [{"label": "On", "value": record.display_name}, {"label": "Summary", "value": vals["summary"]},
               {"label": "Due", "value": vals["date_deadline"]}]
    if activity_type:
        atype = resolve_record(ctx.env, "mail.activity.type", activity_type, label="activity type")
        vals["activity_type_id"] = atype.id
        preview.append({"label": "Type", "value": atype.display_name})
    user = resolve_record(ctx.env, "res.users", assigned_to, label="user") if assigned_to else ctx.env.user
    vals["user_id"] = user.id
    preview.append({"label": "Assigned to", "value": user.display_name})
    if note:
        vals["note"] = str(note)
    return ctx.create_pending("create", spec, {"tool": "activity", "object": object, "vals": vals},
                              "Schedule \"%s\" on %s" % (vals["summary"], record.display_name), preview,
                              res_id=record.id)


@tool("prepare_inventory_adjustment", write=True)
def prepare_inventory_adjustment(ctx, product=None, quantity=None, warehouse=None):
    _require(ctx, "update")
    env = ctx.env
    if "stock.quant" not in env:
        raise ToolError("The Inventory app is not installed.", code="app_not_installed")
    if not env.user.has_group("stock.group_stock_manager"):
        raise ToolError("Only inventory administrators can adjust stock quantities.", code="permission_denied")
    if product in (None, "") or quantity in (None, ""):
        raise ToolError("Which product, and what should the new on-hand quantity be?",
                        code="missing_information", missing=["product", "quantity"])
    prod = resolve_record(env, "product.product", product, label="product")
    if not prod.is_storable:
        raise ToolError("%s is not tracked in inventory." % prod.display_name, code="invalid_value")
    if prod.tracking != "none":
        raise ToolError("%s is tracked by lots/serial numbers; adjust it in Inventory." % prod.display_name,
                        code="unsupported")
    qty = _parse_number(quantity, "quantity")
    if qty < 0:
        raise ToolError("The quantity cannot be negative.", code="invalid_value")
    if warehouse:
        wh = resolve_record(env, "stock.warehouse", warehouse, label="warehouse")
    else:
        wh = env["stock.warehouse"].search([("company_id", "=", env.company.id)], limit=1)
    if not wh:
        raise ToolError("No warehouse found.", code="not_found")
    location = wh.lot_stock_id
    current = prod.with_context(location=location.id).qty_available
    spec = get_spec(env, "stock_quant")
    preview = [{"label": "Product", "value": prod.display_name}, {"label": "Location", "value": location.display_name},
               {"label": "On hand", "value": "%g" % qty, "old": "%g" % current}]
    return ctx.create_pending("update", spec, {"tool": "inventory", "product_id": prod.id, "location_id": location.id,
                                               "quantity": qty},
                              "Set on-hand quantity of %s to %g in %s" % (prod.display_name, qty, wh.display_name),
                              preview, warnings=["This posts an inventory adjustment (stock move)."])


# ---------------------------------------------------------------------------------------
# Execution of confirmed actions
# ---------------------------------------------------------------------------------------
def execute_action(env, payload, model_name, res_id):
    """Execute a confirmed pending action as ``env.user``. Returns the affected record.

    Called inside a savepoint by ``dcs.ai.action``; any exception rolls the change back.
    """
    if env.su:
        raise AccessError("AI actions cannot be executed in superuser mode.")
    kind = payload.get("tool")
    if kind == "create_record":
        spec = OBJECTS[payload["object"]]
        return env[spec["model"]].create(_from_json(payload["vals"]))
    if kind == "update_record":
        record = env[model_name].browse(res_id).exists()
        if not record:
            raise MissingError("The record no longer exists.")
        record.write(_from_json(payload["vals"]))
        return record
    if kind == "sale_quotation":
        vals = dict(payload["vals"])
        vals["order_line"] = [Command.create(line) for line in payload["lines"]]
        return env["sale.order"].create(vals)
    if kind == "purchase_order":
        vals = dict(payload["vals"])
        vals["order_line"] = [Command.create(line) for line in payload["lines"]]
        return env["purchase.order"].create(vals)
    if kind == "invoice":
        vals = dict(payload["vals"])
        vals["invoice_line_ids"] = [Command.create(line) for line in payload["lines"]]
        return env["account.move"].create(vals)
    if kind == "order_lines":
        order = env[model_name].browse(res_id).exists()
        if not order:
            raise MissingError("The order no longer exists.")
        if order.state not in ("draft", "sent"):
            raise UserError("%s has been confirmed in the meantime." % order.display_name)
        qty_key, mode = payload["qty_key"], payload["mode"]
        commands = []
        for line in payload["lines"]:
            existing = order.order_line.filtered(lambda l, p=line["product_id"]: l.product_id.id == p)
            if mode == "add":
                commands.append(Command.create(line))
            elif mode == "set_quantity":
                for ol in existing[:1]:
                    vals = {qty_key: line[qty_key]}
                    if "price_unit" in line:
                        vals["price_unit"] = line["price_unit"]
                    commands.append(Command.update(ol.id, vals))
            else:
                commands.extend(Command.delete(ol.id) for ol in existing)
        order.write({"order_line": commands})
        return order
    if kind == "document_action":
        record = env[model_name].browse(res_id).exists()
        if not record:
            raise MissingError("The record no longer exists.")
        action = payload["action"]
        if action == "confirm" and model_name == "sale.order":
            record.action_confirm()
        elif action == "confirm" and model_name == "purchase.order":
            record.button_confirm()
        elif action == "post":
            record.action_post()
        elif action == "mark_won":
            record.action_set_won()
        elif action == "mark_lost":
            record.action_set_lost()
        return record
    if kind == "activity":
        spec = OBJECTS[payload["object"]]
        record = env[spec["model"]].browse(res_id).exists()
        if not record:
            raise MissingError("The record no longer exists.")
        vals = dict(payload["vals"])
        if not vals.get("activity_type_id"):
            todo = env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
            if todo:
                vals["activity_type_id"] = todo.id
        record.activity_schedule(**vals)
        return record.activity_ids.sorted("id")[-1:] or record
    if kind == "inventory":
        if not env.user.has_group("stock.group_stock_manager"):
            raise AccessError("Only inventory administrators can adjust stock quantities.")
        quant = env["stock.quant"].with_context(inventory_mode=True).create({
            "product_id": payload["product_id"],
            "location_id": payload["location_id"],
            "inventory_quantity": payload["quantity"],
        })
        result = quant.action_apply_inventory()
        if isinstance(result, dict):
            raise UserError("Odoo needs more information to apply this adjustment; please do it from Inventory.")
        return quant.product_id
    raise UserError("Unknown action type.")
