from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)

class SnkReportInventoryReserveWizard(models.TransientModel):
    _name = 'snk.report.inventory.reserve.wizard'
    _description = 'Inventory Reserved Wizard'

    from_date = fields.Datetime('From', required=True)
    to_date = fields.Datetime('TO', required=True)
    snk_site_id = fields.Many2many('snk.site', string='Site',
        domain=lambda self: [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ])
    analytic_ids = fields.Many2many('account.analytic.account',
        'snk_inv_reserve_wizard_analytic_rel', 'wizard_id', 'analytic_id',
        string='Brands',
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ])

    def _clear_old_data(self):
        ReportModel = self.env['snk.report.inventory.reserve']
        # Clear old data for current user
        reports_to_clear = ReportModel.search([('create_uid', '=', self.env.uid)])
        if reports_to_clear:
            reports_to_clear.unlink()

    def action_confirm(self):
        self.ensure_one()

        if not self.from_date or not self.to_date:
            raise UserError("Date start and end are required.")
        if self.from_date > self.to_date:
            raise UserError("Start date must not be later than end date.")

        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        company_ids = self.env.companies.ids
        if not allowed_brand_ids:
            raise UserError("You do not have access to any brand.")
        if not allowed_industry_ids:
            raise UserError("You do not have access to any site industry.")
        if not company_ids:
            raise UserError("No allowed company is active for this report.")

        invalid_sites = self.snk_site_id.filtered(
            lambda site: site.industry_id.id not in allowed_industry_ids
        )
        if invalid_sites:
            raise UserError(
                "You are not allowed to use the selected site(s): %s"
                % ", ".join(invalid_sites.mapped('display_name'))
            )
        invalid_brands = self.analytic_ids.filtered(
            lambda brand: brand.id not in allowed_brand_ids
        )
        if invalid_brands:
            raise UserError(
                "You are not allowed to use the selected brand(s): %s"
                % ", ".join(invalid_brands.mapped('display_name'))
            )

        effective_brand_ids = self.analytic_ids.ids or allowed_brand_ids
        params = {
            'from_date': self.from_date,
            'to_date': self.to_date,
            'uid': self.env.uid,
            'brand_ids': effective_brand_ids,
            'company_ids': company_ids,
        }

        self._clear_old_data()

        filters = [
            "sml.date BETWEEN %(from_date)s AND %(to_date)s",
            "sml.state IN ('waiting', 'confirmed', 'partially_available', 'assigned')",
            "sml.company_id = ANY(%(company_ids)s)",
            "pt.analytic_id = ANY(%(brand_ids)s)",
        ]

        if self.snk_site_id:
            params['site_ids'] = self.snk_site_id.ids
            filters.append(
                "(sp.snk_site_id = ANY(%(site_ids)s) "
                "OR sp.snk_site_address_id = ANY(%(site_ids)s))"
            )
        else:
            params['allowed_industry_ids'] = allowed_industry_ids
            filters.append("""
                (
                    EXISTS (
                        SELECT 1 FROM snk_site allowed_source
                        WHERE allowed_source.id = sp.snk_site_id
                          AND allowed_source.industry_id = ANY(%(allowed_industry_ids)s)
                    )
                    OR EXISTS (
                        SELECT 1 FROM snk_site allowed_destination
                        WHERE allowed_destination.id = sp.snk_site_address_id
                          AND allowed_destination.industry_id = ANY(%(allowed_industry_ids)s)
                    )
                )
            """)

        where_clause = " AND ".join(filters)

        insert_sql = f"""
            INSERT INTO snk_report_inventory_reserve (
                move_line_id, picking_id, snk_site_id, snk_source_site_name,
                location_id, snk_site_addres_id, snk_dest_site_name, location_dest_id,
                picking_type_code, picking_type_id, snk_physical_date, scheduled_date, date_done,
                product_id, snk_material_code, lot_id, analytic_ids,
                origin, quantity, product_uom_id, note,
                state, company_id, create_uid, create_date
            )
            SELECT
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
                sp.scheduled_date,
                sp.date_done,
                sml.product_id,
                pt.snk_material_code,
                sml.lot_id,
                pt.analytic_id,
                sm.origin,
                sml.quantity,
                sml.product_uom_id,
                sp.note,
                sml.state,
                sml.company_id,
                %(uid)s,
                NOW()
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

        self.env.cr.execute(insert_sql, params)
        self._populate_extra_fields()
        
        return self._return_report_action()

    def _populate_extra_fields(self):
        """ Populates fields that are not easily accessible via raw SQL or not stored in DB """
        reports = self.env['snk.report.inventory.reserve'].search([('create_uid', '=', self.env.uid)])
        for report in reports:
            # Populate Many2many attributes
            if report.product_id:
                report.product_template_attribute_value_ids = report.product_id.product_template_attribute_value_ids
            
            # Populate purchase_id (PO Number) via ORM as it might be a non-stored compute field
            if report.picking_id:
                # Use getattr to safely check for purchase_id on picking
                purchase_order = getattr(report.picking_id, 'purchase_id', False)
                if purchase_order:
                    report.purchase_id = purchase_order
                elif report.picking_id.origin:
                    # Fallback to search by origin if needed, but only if it looks like a PO
                    if 'PO' in report.picking_id.origin:
                        po = self.env['purchase.order'].search([('name', '=', report.picking_id.origin)], limit=1)
                        if po:
                            report.purchase_id = po

    def _return_report_action(self):
        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }
