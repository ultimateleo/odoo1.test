from odoo import models, fields, api
from odoo.exceptions import UserError
import logging

_logger = logging.getLogger(__name__)


class SnkPoMonitoringReportWizard(models.TransientModel):
    _name = 'snk.report.po.monitoring.wizard'
    _description = 'PO Monitoring Report Wizard'

    from_date = fields.Datetime('From Date', required=True)
    to_date = fields.Datetime('To Date', required=True)
    snk_site_id = fields.Many2many('snk.site', string='Site',
        domain=lambda self: [('snk_state', '=', 'open')]
        if self.env.user.has_group('purchase.group_purchase_manager')
        else [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ])

    def action_generate_report(self):
        self.ensure_one()

        if not self.from_date or not self.to_date:
            raise UserError("From Date and To Date are required.")
        if self.from_date > self.to_date:
            raise UserError("From Date must not be later than To Date.")

        is_purchase_manager = self.env.user.has_group(
            'purchase.group_purchase_manager'
        )
        company_ids = self.env.companies.ids
        if not company_ids:
            raise UserError("No allowed company is active for this report.")

        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        if not is_purchase_manager:
            if not allowed_brand_ids:
                raise UserError(
                    "You do not have access to any brand. "
                    "Please contact your administrator."
                )
            if not allowed_industry_ids:
                raise UserError(
                    "You do not have access to any site industry. "
                    "Please contact your administrator."
                )
            invalid_sites = self.snk_site_id.filtered(
                lambda site: site.industry_id.id not in allowed_industry_ids
            )
            if invalid_sites:
                raise UserError(
                    "You are not allowed to use the selected site(s): %s"
                    % ", ".join(invalid_sites.mapped('display_name'))
                )

        _logger.info(">>> [START] Generating PO Monitoring Report for user %s", self.env.user.name)

        # 1. Hapus data lama milik user ini
        self.env.cr.execute(
            "DELETE FROM snk_report_po_monitoring WHERE create_uid = %s",
            (self.env.uid,)
        )

        # 2. Build parameterized filters
        params = {
            'from_date': self.from_date,
            'to_date': self.to_date,
            'uid': self.env.uid,
            'company_ids': company_ids,
        }
        if self.snk_site_id:
            site_filter_sql = "AND po.snk_site_id = ANY(%(site_ids)s)"
            params['site_ids'] = self.snk_site_id.ids
        elif not is_purchase_manager:
            site_filter_sql = """
                AND EXISTS (
                    SELECT 1
                    FROM snk_site allowed_site
                    WHERE allowed_site.id = po.snk_site_id
                      AND allowed_site.industry_id = ANY(%(allowed_industry_ids)s)
                )
            """
            params['allowed_industry_ids'] = allowed_industry_ids
        else:
            site_filter_sql = ""

        if is_purchase_manager:
            user_access_filter_sql = ""
        else:
            params['allowed_brand_ids'] = allowed_brand_ids
            user_access_filter_sql = """
                AND (po.create_uid = %(uid)s OR po.user_id = %(uid)s)
                AND EXISTS (
                    SELECT 1
                    FROM account_analytic_account_purchase_order_rel po_brand
                    WHERE po_brand.purchase_order_id = po.id
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM account_analytic_account_purchase_order_rel po_brand
                    WHERE po_brand.purchase_order_id = po.id
                      AND NOT (
                          po_brand.account_analytic_account_id
                          = ANY(%(allowed_brand_ids)s)
                      )
                )
                AND NOT EXISTS (
                    SELECT 1
                    FROM purchase_order_line access_line
                    WHERE access_line.order_id = po.id
                      AND access_line.display_type IS NULL
                      AND (
                          access_line.analytic_account_id IS NULL
                          OR NOT (
                              access_line.analytic_account_id
                              = ANY(%(allowed_brand_ids)s)
                          )
                      )
                )
            """

        # 3. INSERT via CTE — satu row per PO line, received hanya dari GR done
        insert_sql = f"""
            WITH
            -- ================================================================
            -- CTE 1: Received qty — hanya dari move lines yang benar-benar DONE
            -- ================================================================
            done_received AS (
                SELECT
                    sm.purchase_line_id,
                    SUM(sml.quantity) AS total_received
                FROM stock_move sm
                JOIN stock_move_line sml ON sml.move_id = sm.id
                WHERE sml.state = 'done'
                  AND sm.purchase_line_id IS NOT NULL
                  AND sm.state != 'cancel'
                GROUP BY sm.purchase_line_id
            ),

            -- ================================================================
            -- CTE 2: Picking terbaru per PO line (untuk info jadwal & site)
            -- ================================================================
            latest_picking AS (
                SELECT DISTINCT ON (sm.purchase_line_id)
                    sm.purchase_line_id,
                    pick.id                 AS picking_id,
                    pick.scheduled_date,
                    pick.date_done,
                    pick.snk_physical_date,
                    pick.snk_site_id,
                    pick.snk_site_address_id,
                    pick.partner_id
                FROM stock_move sm
                JOIN stock_picking pick ON pick.id = sm.picking_id
                WHERE sm.purchase_line_id IS NOT NULL
                  AND sm.state != 'cancel'
                ORDER BY sm.purchase_line_id, pick.id DESC
            ),

            -- ================================================================
            -- CTE 3: Lot pertama yang sudah di-GR per PO line
            -- ================================================================
            done_lot AS (
                SELECT DISTINCT ON (sm.purchase_line_id)
                    sm.purchase_line_id,
                    sml.lot_id
                FROM stock_move sm
                JOIN stock_move_line sml ON sml.move_id = sm.id
                WHERE sml.state = 'done'
                  AND sm.purchase_line_id IS NOT NULL
                ORDER BY sm.purchase_line_id, sml.id
            )

            INSERT INTO snk_report_po_monitoring (
                order_line_id, purchase_id, picking_id, move_line_id,
                snk_po_number, snk_pr_number, snk_purchase_type_id, partner_id, origin,
                date_approve, date_planned, snk_approve, note, state,
                snk_site_address_id, snk_site_address_name,
                snk_site_id, snk_site_name, picking_type_id,
                scheduled_date, date_done, snk_physical_date,
                receipt_status,
                product_id, snk_description, snk_material_code,
                lot_id, analytic_id,
                product_qty, qty_received, snk_remaining_order,
                product_uom_id, price_unit, snk_amount,
                company_id, currency_id, create_uid, date_order,
                create_date
            )
            SELECT
                pol.id                                          AS order_line_id,
                po.id                                           AS purchase_id,
                lp.picking_id,
                NULL::INTEGER                                   AS move_line_id,
                po.snk_po_number,
                pr.snk_pr_number                                AS snk_pr_number,
                po.snk_purchase_type_id,
                po.partner_id,
                po.origin,
                po.date_approve,
                po.date_planned,
                po.snk_approve,
                po.notes                                        AS note,
                po.state,
                COALESCE(sd.snk_code, '')                       AS snk_site_address_id,
                COALESCE(sd.full_name, rp.name)                 AS snk_site_address_name,
                COALESCE(s.snk_code, '')                        AS snk_site_id,
                s.full_name                                     AS snk_site_name,
                po.picking_type_id,
                lp.scheduled_date,
                lp.date_done,
                lp.snk_physical_date,
                po.receipt_status,
                pol.product_id,
                pol.snk_description,
                pp.snk_material_code,
                dl.lot_id,
                pol.analytic_account_id                         AS analytic_id,
                pol.product_qty,
                COALESCE(dr.total_received, 0)                  AS qty_received,
                pol.snk_remaining_order,
                pol.product_uom                                 AS product_uom_id,
                pol.price_unit,
                (pol.price_unit * pol.product_qty)              AS snk_amount,
                po.company_id,
                po.currency_id,
                %(uid)s,
                po.date_order,
                NOW()
            FROM purchase_order_line pol
            JOIN purchase_order po ON pol.order_id = po.id
            LEFT JOIN purchase_requisition pr ON pr.id = po.requisition_id
            LEFT JOIN done_received dr ON dr.purchase_line_id = pol.id
            LEFT JOIN latest_picking lp ON lp.purchase_line_id = pol.id
            LEFT JOIN done_lot dl ON dl.purchase_line_id = pol.id
            LEFT JOIN snk_site s ON s.id = lp.snk_site_id
            LEFT JOIN snk_site sd ON sd.id = lp.snk_site_address_id
            JOIN product_product pp ON pp.id = pol.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN res_partner rp ON rp.id = lp.partner_id
            WHERE po.state != 'cancel'
              AND po.date_order BETWEEN %(from_date)s AND %(to_date)s
              AND po.company_id = ANY(%(company_ids)s)
              AND pol.display_type IS NULL
              AND pt.type = 'consu'
              {site_filter_sql}
              {user_access_filter_sql}
            ORDER BY po.date_order DESC, po.id, pol.id
        """

        self.env.cr.execute(insert_sql, params)

        _logger.info(">>> [DONE] PO Monitoring Report generated for user %s", self.env.user.name)

        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }
