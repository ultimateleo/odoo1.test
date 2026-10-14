from odoo import models, fields, api
from odoo.exceptions import UserError
import logging
import time as t

_logger = logging.getLogger(__name__)

class SnkPosReportWizard(models.TransientModel):
    _name = 'snk.report.pos.wizard'
    _description = 'POS Report Wizard'

    date_start = fields.Datetime('Start Date', required=True)
    date_end = fields.Datetime('End Date', required=True)
    site_ids = fields.Many2many('snk.site', string='Sites',
        domain=lambda self: [('snk_state', '=', 'open')] if self.env.user.has_group(
            'sales_team.group_sale_manager'
        ) else [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ])
    categ_ids = fields.Many2many('product.category', string='Categories')
    product_tmpl_ids = fields.Many2many('product.template', string='Product Templates')
    analytic_ids = fields.Many2many('account.analytic.account',
        string='Brands',
        domain=lambda self: [('snk_brand', '=', True)] if self.env.user.has_group(
            'sales_team.group_sale_manager'
        ) else [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ],
    )

    def _format_in_clause(self, ids):
        return f"({','.join(map(str, ids))})"

    def action_generate_report(self):
        self.ensure_one()

        if not self.date_start or not self.date_end:
            raise UserError("Date start and end are required.")
        if self.date_start > self.date_end:
            raise UserError("Start date must not be later than end date.")

        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        company_ids = self.env.companies.ids
        is_sales_manager = self.env.user.has_group('sales_team.group_sale_manager')
        if not is_sales_manager and not allowed_brand_ids:
            raise UserError("You do not have access to any brand.")
        if not is_sales_manager and not allowed_industry_ids:
            raise UserError("You do not have access to any site industry.")
        if not company_ids:
            raise UserError("No allowed company is active for this report.")

        invalid_sites = not is_sales_manager and self.site_ids.filtered(
            lambda site: site.industry_id.id not in allowed_industry_ids
        )
        if invalid_sites:
            raise UserError(
                "You are not allowed to use the selected site(s): %s"
                % ", ".join(invalid_sites.mapped('display_name'))
            )
        invalid_brands = not is_sales_manager and self.analytic_ids.filtered(
            lambda brand: brand.id not in allowed_brand_ids
        )
        if invalid_brands:
            raise UserError(
                "You are not allowed to use the selected brand(s): %s"
                % ", ".join(invalid_brands.mapped('display_name'))
            )

        _logger.info(">>> [START] Generating POS Report from %s to %s for user %s",
                     self.date_start, self.date_end, self.env.user.name)

        ReportModel = self.env['snk.report.pos.order_pevots']

        # Hapus data lama milik user yang sama
        reports_to_clear = ReportModel.search([('create_uid', '=', self.env.uid)])
        if reports_to_clear:
            _logger.info(">>> [PROCESSING] Menghapus %s data report lama milik user %s.",
                        len(reports_to_clear), self.env.user.name)
            reports_to_clear.unlink()

        # Build WHERE clause for SQL
        params = {
            'date_start': self.date_start,
            'date_end': self.date_end,
            'uid': self.env.uid,
            'company_ids': company_ids,
        }
        filters = [
            "po.date_order BETWEEN %(date_start)s AND %(date_end)s",
            "po.company_id = ANY(%(company_ids)s)",
        ]

        if self.analytic_ids:
            params['brand_ids'] = self.analytic_ids.ids
            filters.append("pp.analytic_id = ANY(%(brand_ids)s)")
        elif not is_sales_manager:
            params['brand_ids'] = allowed_brand_ids
            filters.append("pp.analytic_id = ANY(%(brand_ids)s)")

        if self.site_ids:
            params['site_ids'] = self.site_ids.ids
            filters.append("po.snk_site_id = ANY(%(site_ids)s)")
        elif not is_sales_manager:
            params['allowed_industry_ids'] = allowed_industry_ids
            filters.append("""
                EXISTS (
                    SELECT 1 FROM snk_site allowed_site
                    WHERE allowed_site.id = po.snk_site_id
                      AND allowed_site.industry_id = ANY(%(allowed_industry_ids)s)
                )
            """)

        if self.categ_ids:
            params['categ_ids'] = self.categ_ids.ids
            filters.append("ppt.categ_id = ANY(%(categ_ids)s)")

        if self.product_tmpl_ids:
            params['product_tmpl_ids'] = self.product_tmpl_ids.ids
            filters.append("ppt.id = ANY(%(product_tmpl_ids)s)")

        where_clause = " AND ".join(filters)
        start_time = t.perf_counter()

        _logger.info(">>> [PROCESSING] Generating POS Report where: %s", where_clause)

        # Insert data ke report table. Harga berasal langsung dari transaksi POS.
        insert_sql = f"""
            INSERT INTO snk_report_pos_order_pevots (
                name, state, company_id, currency_id, currency,
                site_id, site_name, industry, warehouse_id, customer_id, partner_id, partner_name,
                product_id, product_template_id, template_reference, variant_reference, categ_id, analytic_id, grade, description,
                date_order, reference, sale_name, delivery_status, promo_code,
                product_uom_qty, product_uom, price_unit, discount, discount_amount,
                price_tax, price_subtotal, price_total,
                create_uid, create_date
            )
            SELECT
                po.name AS name,
                po.state AS state,
                po.company_id AS company_id,
                po.currency_id AS currency_id,
                rc.symbol AS currency,
                s.id AS site_id,
                s.full_name AS site_name,
                rpi.name->>'en_US' AS industry,
                s.warehouse_id AS warehouse_id,
                s.customer_id AS customer_id,
                s.partner_id AS partner_id,
                rp.name AS partner_name,
                po.product_id AS product_id,
                pp.product_tmpl_id AS product_template_id,
                po.product_template_ref AS template_reference,
                po.product_variant_ref AS variant_reference,
                ppt.categ_id AS categ_id,
                pp.analytic_id AS analytic_id,
                sg.snk_name AS grade,
                po.description AS description,
                po.date_order AS date_order,
                po.reference AS reference,
                pos.name AS sale_name,
                po.delivery_status AS delivery_status,
                po.promo_code AS promo_code,
                po.product_uom_qty AS product_uom_qty,
                uo.name->>'en_US' AS product_uom,
                po.price_unit AS price_unit,
                po.discount AS discount,
                po.discount_amount AS discount_amount,
                po.price_tax AS price_tax,
                po.price_subtotal AS price_subtotal,
                po.price_total AS price_total,
                %(uid)s,
                NOW()
            FROM snk_pos_order po
            LEFT JOIN snk_pos_sale pos ON pos.id = po.sale_id
            LEFT JOIN snk_site s ON po.snk_site_id = s.id
            LEFT JOIN res_partner rp ON rp.id = s.partner_id
            LEFT JOIN res_partner_industry rpi ON s.industry_id = rpi.id
            LEFT JOIN snk_product_grade sg ON po.grade_id = sg.id
            LEFT JOIN res_currency rc on rc.id = po.currency_id
            LEFT JOIN uom_uom uo on uo.id = po.product_uom
            LEFT JOIN product_product pp ON pp.id = po.product_id
            LEFT JOIN product_template ppt ON ppt.id = pp.product_tmpl_id
            WHERE {where_clause}
        """

        self.env.cr.execute(insert_sql, params)
        data_count = self.env.cr.rowcount
        elapsed = t.perf_counter() - start_time

        if data_count:
            _logger.info(">>> [SUCCESS] Generated %s data for user %s in %.2fs",
                        data_count, self.env.user.name, elapsed)
        else:
            _logger.info(">>> [FAILED] No data generated for user %s", self.env.user.name)

        # Return action to open report
        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }
