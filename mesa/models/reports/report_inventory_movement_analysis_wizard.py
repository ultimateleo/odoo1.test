from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class InventoryMovementAnalysisWizard(models.TransientModel):
    _name = 'snk.report.inventory.movement.analysis.wizard'
    _description = 'Inventory Movement Analysis Wizard'

    from_date = fields.Datetime(string='From', required=True)
    to_date = fields.Datetime(string='To', required=True)
    snk_site_id = fields.Many2many('snk.site', string='Site',
        domain=lambda self: [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ])
    product_id = fields.Many2one('product.product', string='Product')
    location_id = fields.Many2one('stock.location', string='Location')

    def _clear_old_data(self):
        """Hapus data lama untuk user ini"""
        ReportModel = self.env['snk.report.inventory.movement.analysis']
        reports_to_clear = ReportModel.search([('create_uid', '=', self.env.uid)])
        if reports_to_clear:
            reports_to_clear.unlink()

    def action_confirm(self):
        self.ensure_one()
        self._clear_old_data()

        if not self.from_date or not self.to_date:
            raise UserError("Date start and end are required.")

        filters = [
            f"sml.date BETWEEN '{self.from_date}' AND '{self.to_date}'",
            "sml.state IN ('waiting', 'confirmed', 'partially_available', 'assigned')"
        ]

        if self.snk_site_id:
            if len(self.snk_site_id) == 1:
                site_id = self.snk_site_id.id
                filters.append(f"(sp.snk_site_id = {site_id} OR sp.snk_site_address_id = {site_id})")
            else:
                site_ids = tuple(self.snk_site_id.ids)
                filters.append(f"(sp.snk_site_id IN {site_ids} OR sp.snk_site_address_id IN {site_ids})")
        if self.product_id:
            filters.append(f"sml.product_id = {self.product_id.id}")
        if self.location_id:
            filters.append(f"sml.location_id = {self.location_id.id}")

        where_clause = " AND ".join(filters)

        insert_sql = f"""
            INSERT INTO snk_report_inventory_movement_analysis (
                from_date, to_date, move_line_id, picking_id, snk_site_id, snk_source_site_name,
                location_id, snk_site_addres_id, snk_dest_site_name, location_dest_id,
                picking_type_code, picking_type_id, snk_physical_date, date, scheduled_date, date_done,
                product_id, snk_material_code, lot_id, analytic_ids,
                origin, quantity, actual_quantity, product_uom_id, product_uom_qty, note,
                state, company_id, create_uid, create_date,snk_create_date,default_code_variant,default_code_template,snk_product_template_id, snk_grade_id
            )
            SELECT
                '{self.from_date}'::timestamp,
                '{self.to_date}'::timestamp,
                sml.id,
                sml.picking_id,
                sp.snk_site_id,
                ss_src.snk_name,
                sml.location_id,
                sp.snk_site_address_id,
                ss_dest.snk_name,
                sml.location_dest_id,
                spt.code,
                sp.picking_type_id,
                sp.snk_physical_date,
                sp.date,
                sp.scheduled_date,
                sp.date_done,
                sml.product_id,
                pt.snk_material_code,
                sml.lot_id,
                pt.analytic_id,
                sm.origin,
                sml.quantity,
                CASE WHEN spt.code = 'outgoing' THEN -sml.quantity ELSE sml.quantity END AS actual_quantity,
                sml.product_uom_id,
                sml.quantity_product_uom,
                sp.note,
                sml.state,
                sml.company_id,
                {self.env.uid},
                NOW(),
                sp.snk_create_date,
                pp.default_code,
                pt.default_code,
                pt.id AS snk_product_template_id,
                pt.snk_grade_id
            FROM stock_move_line sml
            LEFT JOIN stock_move sm ON sm.id = sml.move_id
            LEFT JOIN stock_picking sp ON sp.id = sml.picking_id
            LEFT JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
            LEFT JOIN product_product pp ON pp.id = sml.product_id
            LEFT JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN snk_site ss_src ON ss_src.id = sp.snk_site_id
            LEFT JOIN snk_site ss_dest ON ss_dest.id = sp.snk_site_address_id
            WHERE {where_clause}
        """

        self.env.cr.execute(insert_sql)
        self._populate_extra_fields()
        return self._return_report_action()

    def _populate_extra_fields(self):
        """Populates fields that are not easily accessible via raw SQL"""
        reports = self.env['snk.report.inventory.movement.analysis'].search([('create_uid', '=', self.env.uid)])
        for report in reports:
            if report.product_id:
                report.product_template_attribute_value_ids = report.product_id.product_template_attribute_value_ids

            if report.picking_id:
                purchase_order = getattr(report.picking_id, 'purchase_id', False)
                if purchase_order:
                    report.purchase_id = purchase_order
                elif report.picking_id.origin:
                    if 'PO' in report.picking_id.origin:
                        po = self.env['purchase.order'].search([('name', '=', report.picking_id.origin)], limit=1)
                        if po:
                            report.purchase_id = po

    def _return_report_action(self):
        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }
