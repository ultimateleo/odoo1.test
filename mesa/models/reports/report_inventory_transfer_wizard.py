from odoo import models, fields, api
from odoo.exceptions import UserError
import logging
import time

_logger = logging.getLogger(__name__)

class SnkReportInventoryTransferWizard(models.TransientModel):
    _name = 'snk.report.inventory.transfer.wizard'
    _description = 'Inventory Transfer Wizard'

    from_date = fields.Datetime('From', required=True)
    to_date = fields.Datetime('To', required=True)
    snk_site_id = fields.Many2many('snk.site', string='Site',
        domain=lambda self: [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ])
    categ_ids = fields.Many2many('product.category', string='Categories')
    product_tmpl_ids = fields.Many2many('product.template', string='Product Templates')
    analytic_ids = fields.Many2many('account.analytic.account',
        string='Brands',
        relation="snk_transfer_wizard_rel",
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ]
    )
    snk_grade_ids = fields.Many2many('snk.product.grade', string='Grades')


    def _clear_old_data(self):
        report_model = self.env['snk.report.inventory.transfer']

        # Keep the same unlink permission check as the ORM path. Raw SQL bypasses
        # access rules, so the DELETE must remain explicitly scoped to env.uid.
        report_model.check_access('unlink')
        report_model.flush_model()
        self.env.cr.execute(
            """
            DELETE FROM snk_report_inventory_transfer
            WHERE create_uid = %s
            """,
            [self.env.uid],
        )
        deleted_count = self.env.cr.rowcount
        report_model.invalidate_model()
        _logger.info(
            ">>> [SUCCESS] Deleted %s previous Inventory Transfer rows for user %s.",
            deleted_count,
            self.env.user.name,
        )

    def _format_in_clause(self, ids):
        return f"({','.join(map(str, ids))})"

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

        # Prevent two Inventory Transfer generations for the same user from
        # deleting or mixing each other's result rows. The transaction-scoped
        # lock is released automatically on commit or rollback, while different
        # users can still generate concurrently.
        self.env.cr.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)
            """,
            ['snk.report.inventory.transfer', self.env.uid],
        )
        if not self.env.cr.fetchone()[0]:
            raise UserError(
                "Another Inventory Transfer generation is already running "
                "for your user."
            )

        params = {
            'from_date': self.from_date,
            'to_date': self.to_date,
            'uid': self.env.uid,
            'brand_ids': self.analytic_ids.ids or allowed_brand_ids,
            'company_ids': company_ids,
        }

        self._clear_old_data()

        picking_filters = [
            "sp.scheduled_date BETWEEN %(from_date)s AND %(to_date)s",
            "sp.company_id = ANY(%(company_ids)s)",
        ]
        product_filters = [
            "pp.analytic_id = ANY(%(brand_ids)s)",
        ]

        if self.snk_site_id:
            params['site_ids'] = self.snk_site_id.ids
            picking_filters.append(
                "(sp.snk_site_id = ANY(%(site_ids)s) "
                "OR sp.snk_site_address_id = ANY(%(site_ids)s))"
            )
        else:
            params['allowed_industry_ids'] = allowed_industry_ids
            picking_filters.append("""
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

        if self.categ_ids:
            params['categ_ids'] = self.categ_ids.ids
            product_filters.append("pt.categ_id = ANY(%(categ_ids)s)")

        if self.product_tmpl_ids:
            params['product_tmpl_ids'] = self.product_tmpl_ids.ids
            product_filters.append("pt.id = ANY(%(product_tmpl_ids)s)")

        if self.snk_grade_ids:
            params['grade_ids'] = self.snk_grade_ids.ids
            product_filters.append("pp.snk_grade_id = ANY(%(grade_ids)s)")

        picking_where_clause = " AND ".join(picking_filters)
        product_where_clause = " AND ".join(product_filters)

        insert_sql = f"""
            WITH filtered_pickings AS MATERIALIZED (
                SELECT
                    sp.id,
                    sp.snk_site_id,
                    sp.snk_site_address_id,
                    sp.picking_type_id,
                    sp.snk_physical_date,
                    sp.date,
                    sp.scheduled_date,
                    sp.date_done,
                    sp.note,
                    sp.snk_create_date,
                    sp.origin,
                    sp.company_id
                FROM stock_picking sp
                WHERE {picking_where_clause}
            )
            INSERT INTO snk_report_inventory_transfer (
                from_date,to_date,move_line_id, picking_id, snk_site_id, snk_source_site_name,
                location_id, snk_site_addres_id, snk_dest_site_name, location_dest_id,
                picking_type_code, picking_type_id, snk_physical_date, date, scheduled_date, date_done,
                product_id, categ_id, snk_material_code, lot_id, analytic_ids,
                origin, purchase_id, quantity,actual_quantity, product_uom_id,product_uom_qty, note,
                state, company_id, create_uid, create_date,snk_create_date,
                default_code_variant,default_code_template,snk_product_template_id, snk_grade_id, currency_id, unit_price, total_amount
            )
            SELECT
                %(from_date)s::timestamp,
                %(to_date)s::timestamp,
                sml.id,
                sm.picking_id,
                sp.snk_site_id,
                ss_src.snk_name,
                sm.location_id,
                sp.snk_site_address_id,
                ss_dest.snk_name,
                sm.location_dest_id,
                spt.code,
                sp.picking_type_id,
                sp.snk_physical_date,
                sp.date,
                sp.scheduled_date,
                sp.date_done,
                sm.product_id,
                pc.name AS categ_id,
                pp.snk_material_code,
                sml.lot_id,
                pp.analytic_id,
                sm.origin,
                COALESCE(pbp.purchase_id, origin_po.id),
                sml.quantity,
                CASE 
                    WHEN sml.state != 'done' THEN 0
                    WHEN spt.code = 'outgoing' THEN -sml.quantity
                    WHEN spt.code = 'incoming' THEN sml.quantity
                    WHEN spt.code = 'internal' THEN sml.quantity
                    ELSE 0 
                END AS actual_quantity,
                sml.product_uom_id,
                sm.product_uom_qty,
                sp.note,
                sm.state,
                sm.company_id,
                %(uid)s,
                NOW(),
                sp.snk_create_date,
                pp.default_code,
                pt.default_code,
                pt.id AS snk_product_template_id,
                pp.snk_grade_id,
                rc.symbol AS currency_id,
                COALESCE(variant_price.fixed_price, template_price.fixed_price) AS unit_price,
                (
                    COALESCE(variant_price.fixed_price, template_price.fixed_price)
                    * sml.quantity_product_uom
                ) AS total_amount
            FROM filtered_pickings sp
            JOIN stock_move sm ON sm.picking_id = sp.id
            JOIN stock_move_line sml ON sml.move_id = sm.id
            LEFT JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
            JOIN product_product pp ON pp.id = sm.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN snk_site ss_src ON ss_src.id = sp.snk_site_id
            LEFT JOIN snk_site ss_dest ON ss_dest.id = sp.snk_site_address_id
            LEFT JOIN product_category pc ON pc.id = pt.categ_id
            LEFT JOIN LATERAL (
                SELECT MIN(pol.order_id) AS purchase_id
                FROM stock_move sm_po
                JOIN purchase_order_line pol
                    ON pol.id = sm_po.purchase_line_id
                WHERE sm_po.picking_id = sp.id
                HAVING COUNT(DISTINCT pol.order_id) = 1
            ) pbp ON TRUE
            LEFT JOIN LATERAL (
                SELECT po.id
                FROM purchase_order po
                WHERE pbp.purchase_id IS NULL
                  AND sp.origin LIKE '%%PO%%'
                  AND po.name = sp.origin
                  AND po.company_id = sm.company_id
                ORDER BY po.id DESC
                LIMIT 1
            ) origin_po ON TRUE
            LEFT JOIN LATERAL (
                SELECT pli.id, pli.fixed_price, pli.currency_id
                FROM product_pricelist_item pli
                WHERE pli.product_id = sml.product_id
                  AND pli.applied_on = '0_product_variant'
                  AND (
                      pli.date_start IS NULL
                      OR pli.date_start <= sp.scheduled_date
                  )
                  AND (
                      pli.date_end IS NULL
                      OR pli.date_end >= sp.scheduled_date
                  )
                ORDER BY
                    COALESCE(
                        pli.date_start,
                        '-infinity'::timestamp
                    ) DESC,
                    pli.id DESC
                LIMIT 1
            ) variant_price ON TRUE
            LEFT JOIN LATERAL (
                SELECT pli.id, pli.fixed_price, pli.currency_id
                FROM product_pricelist_item pli
                WHERE variant_price.id IS NULL
                  AND pli.product_id IS NULL
                  AND pli.product_tmpl_id = pp.product_tmpl_id
                  AND pli.applied_on = '1_product'
                  AND (
                      pli.date_start IS NULL
                      OR pli.date_start <= sp.scheduled_date
                  )
                  AND (
                      pli.date_end IS NULL
                      OR pli.date_end >= sp.scheduled_date
                  )
                ORDER BY
                    COALESCE(
                        pli.date_start,
                        '-infinity'::timestamp
                    ) DESC,
                    pli.id DESC
                LIMIT 1
            ) template_price ON TRUE
            LEFT JOIN res_currency rc
                ON rc.id = COALESCE(
                    variant_price.currency_id,
                    template_price.currency_id
                )
            WHERE {product_where_clause}
              AND sm.company_id = ANY(%(company_ids)s)
        """

        insert_started_at = time.perf_counter()
        self.env.cr.execute(insert_sql, params)
        inserted_count = self.env.cr.rowcount
        _logger.info(
            ">>> [SUCCESS] Inserted %s Inventory Transfer rows for user %s "
            "in %.2fs.",
            inserted_count,
            self.env.user.name,
            time.perf_counter() - insert_started_at,
        )

        self._populate_product_attributes()
        
        return self._return_report_action()

    def _populate_product_attributes(self):
        """Populate variant attributes without loading report rows into ORM."""
        report_model = self.env['snk.report.inventory.transfer']
        report_model.flush_model()

        started_at = time.perf_counter()
        self.env.cr.execute(
            """
            INSERT INTO snk_report_transfer_attr_rel (report_id, attribute_id)
            SELECT
                report.id,
                combination.product_template_attribute_value_id
            FROM snk_report_inventory_transfer report
            JOIN product_variant_combination combination
                ON combination.product_product_id = report.product_id
            WHERE report.create_uid = %s
            ON CONFLICT (report_id, attribute_id) DO NOTHING
            """,
            [self.env.uid],
        )
        inserted_count = self.env.cr.rowcount
        report_model.invalidate_model(
            ['product_template_attribute_value_ids']
        )
        _logger.info(
            ">>> [SUCCESS] Inserted %s Inventory Transfer attribute links "
            "for user %s in %.2fs.",
            inserted_count,
            self.env.user.name,
            time.perf_counter() - started_at,
        )

    def _populate_extra_fields(self):
        """Legacy ORM enrichment retained for reference; no longer called."""
        reports = self.env['snk.report.inventory.transfer'].search([('create_uid', '=', self.env.uid)])
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
