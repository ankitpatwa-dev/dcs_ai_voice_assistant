from odoo.tests import tagged

from ..services import action_service
from .common import AiAssistantCase


@tagged("post_install", "-at_install", "dcs_ai")
class TestTools(AiAssistantCase):

    def test_search_returns_real_rows_and_display(self):
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "search_records", {
            "object": "contact", "filters": [{"field": "name", "operator": "ilike", "value": "Azure Interior Test"}],
            "fields": ["name", "email", "phone"]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["total_matching"], 1)
        self.assertEqual(res["rows"][0]["id"], self.partner.id)
        self.assertEqual(res["rows"][0]["email"], "azure@example.com")
        table = ctx.displays[-1]
        self.assertEqual(table["type"], "table")
        self.assertEqual(table["rows"][0]["id"], self.partner.id)
        log = self.env["dcs.ai.action"].search([("conversation_id", "=", ctx.conversation.id)])
        self.assertEqual(log.operation, "read")
        self.assertEqual(log.user_id, self.user)

    def test_invalid_and_blocked_fields_are_refused(self):
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "search_records", {"object": "contact", "fields": ["nope_field"]})
        self.assertEqual(res["error"], "invalid_field")
        res = action_service.dispatch(ctx, "search_records", {"object": "user", "fields": ["password"]})
        self.assertFalse(res["ok"])
        res = action_service.dispatch(ctx, "search_records", {
            "object": "contact", "filters": [{"field": "name", "operator": "=like; DROP", "value": "x"}]})
        self.assertFalse(res["ok"])
        res = action_service.dispatch(ctx, "search_records", {"object": "res.users"})
        self.assertEqual(res["error"], "unknown_object")

    def test_unknown_tool_and_bad_json(self):
        ctx = self.ctx()
        self.assertEqual(action_service.dispatch(ctx, "execute_python", {"code": "1"})["error"], "unknown_tool")
        self.assertEqual(action_service.dispatch(ctx, "search_records", "{not json")["error"], "invalid_arguments")

    def test_aggregate_counts(self):
        ctx = self.ctx()
        self.env["res.partner"].create([{"name": "Agg %s" % i, "city": "Aggville"} for i in range(3)])
        res = action_service.dispatch(ctx, "aggregate_records", {
            "object": "contact", "group_by": ["city"],
            "filters": [{"field": "city", "operator": "=", "value": "Aggville"}]})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["groups"][0]["count"], 3)

    def test_prepare_create_does_not_write(self):
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "prepare_create_record", {
            "object": "contact", "values": {"name": "ABC Technologies", "phone": "9876543210"}})
        self.assertEqual(res["status"], "awaiting_confirmation", res)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "ABC Technologies")]))
        action = self.env["dcs.ai.action"].browse(res["action_id"])
        self.assertEqual(action.state, "pending")
        self.assertEqual(action.operation, "create")
        action.with_user(self.user)._execute()
        self.assertEqual(action.state, "done")
        partner = self.env["res.partner"].search([("name", "=", "ABC Technologies")])
        self.assertEqual(partner.phone, "9876543210")
        self.assertEqual(action.res_id, partner.id)
        self.assertEqual(partner.create_uid, self.user, "the record is created as the user, not as superuser")

    def test_duplicate_warning(self):
        res = action_service.dispatch(self.ctx(), "prepare_create_record", {
            "object": "contact", "values": {"name": "azure interior test"}})
        self.assertTrue(res["warnings"])
        self.assertEqual(res["possible_duplicates"][0]["id"], self.partner.id)

    def test_missing_and_not_allowed_values(self):
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "prepare_create_record", {"object": "contact", "values": {"email": "x@y.z"}})
        self.assertEqual(res["error"], "missing_information")
        res = action_service.dispatch(ctx, "prepare_create_record", {
            "object": "contact", "values": {"name": "X", "company_id": 1}})
        self.assertEqual(res["error"], "field_not_allowed")

    def test_update_preview_and_execution(self):
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "prepare_update_record", {
            "object": "contact", "name": "Azure Interior Test", "values": {"phone": "9876543210"}})
        self.assertEqual(res["status"], "awaiting_confirmation", res)
        self.assertEqual(res["preview"][0]["old"], "+1 555 0100")
        self.assertEqual(self.partner.phone, "+1 555 0100")
        self.env["dcs.ai.action"].browse(res["action_id"]).with_user(self.user)._execute()
        self.assertEqual(self.partner.phone, "9876543210")

    def test_ambiguous_name(self):
        self.env["res.partner"].create([{"name": "Twin Corp North"}, {"name": "Twin Corp South"}])
        res = action_service.dispatch(self.ctx(), "prepare_update_record", {
            "object": "contact", "name": "Twin Corp", "values": {"phone": "1"}})
        self.assertEqual(res["error"], "ambiguous")
        self.assertEqual(len(res["options"]), 2)

    def test_failed_execution_is_reported_not_success(self):
        ctx = self.ctx()
        tmp = self.env["res.partner"].create({"name": "Temp Partner To Delete"})
        res = action_service.dispatch(ctx, "prepare_update_record", {
            "object": "contact", "record_id": tmp.id, "values": {"phone": "1"}})
        tmp.unlink()
        action = self.env["dcs.ai.action"].browse(res["action_id"])
        action.with_user(self.user)._execute()
        self.assertEqual(action.state, "failed")
        self.assertTrue(action.error_message)

    def test_expired_proposal_is_not_executed(self):
        res = action_service.dispatch(self.ctx(), "prepare_create_record", {
            "object": "contact", "values": {"name": "Too Late Ltd"}})
        action = self.env["dcs.ai.action"].browse(res["action_id"])
        action.sudo().expires_at = "2000-01-01 00:00:00"
        action.with_user(self.user)._execute()
        self.assertEqual(action.state, "expired")
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Too Late Ltd")]))

    def test_creation_disabled_by_admin(self):
        ctx = self.ctx(allow_create=False, allow_update=False)
        res = action_service.dispatch(ctx, "prepare_create_record", {"object": "contact", "values": {"name": "N"}})
        self.assertEqual(res["error"], "disabled")

    def test_sale_quotation_flow(self):
        if "sale.order" not in self.env:
            self.skipTest("Sales not installed")
        product = self.env["product.product"].create({"name": "AI Desk", "list_price": 250.0, "sale_ok": True})
        ctx = self.ctx()
        res = action_service.dispatch(ctx, "prepare_sale_quotation", {
            "customer": "Azure Interior Test", "lines": [{"product": "AI Desk", "quantity": 3}]})
        self.assertEqual(res["status"], "awaiting_confirmation", res)
        action = self.env["dcs.ai.action"].browse(res["action_id"])
        action.with_user(self.user)._execute()
        self.assertEqual(action.state, "done", action.error_message)
        order = self.env["sale.order"].browse(action.res_id)
        self.assertEqual(order.partner_id, self.partner)
        self.assertEqual(order.order_line.product_id, product)
        self.assertEqual(order.order_line.product_uom_qty, 3)
        self.assertEqual(order.amount_untaxed, 750.0)
        # top customers by sales
        order.action_confirm()
        res = action_service.dispatch(ctx, "aggregate_records", {
            "object": "sale_order", "group_by": ["partner_id"], "measures": ["amount_untaxed:sum"],
            "filters": [{"field": "state", "operator": "=", "value": "sale"}],
            "order_by": "amount_untaxed:sum desc", "limit": 10})
        self.assertTrue(res["ok"], res)
        row = next(g for g in res["groups"] if g["partner_id"] and g["partner_id"]["id"] == self.partner.id)
        self.assertEqual(row["amount_untaxed:sum"], 750.0)
        res = action_service.dispatch(ctx, "business_summary", {"topic": "sales"})
        self.assertTrue(res["ok"], res)
        self.assertTrue(any(c["id"] == self.partner.id for c in res["top_customers"]))

    def test_lead_stage_update(self):
        if "crm.lead" not in self.env:
            self.skipTest("CRM not installed")
        stage = self.env["crm.stage"].create({"name": "Qualified Test Stage", "sequence": 50})
        lead = self.env["crm.lead"].create({"name": "XYZ opportunity", "type": "opportunity"})
        res = action_service.dispatch(self.ctx(), "prepare_update_record", {
            "object": "lead", "name": "XYZ opportunity", "values": {"stage_id": "Qualified Test Stage",
                                                                    "user_id": "AI Test User"}})
        self.assertEqual(res["status"], "awaiting_confirmation", res)
        self.env["dcs.ai.action"].browse(res["action_id"]).with_user(self.user)._execute()
        self.assertEqual(lead.stage_id, stage)
        self.assertEqual(lead.user_id, self.user)

    def test_low_stock_summary(self):
        if "stock.quant" not in self.env:
            self.skipTest("Inventory not installed")
        self.env["product.product"].create({"name": "Scarce Widget", "is_storable": True})
        res = action_service.dispatch(self.ctx(), "business_summary", {"topic": "out_of_stock"})
        self.assertTrue(res["ok"], res)
        self.assertIn("Scarce Widget", [p["name"] for p in res["products"]])
