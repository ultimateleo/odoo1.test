from odoo import fields, models
from odoo.exceptions import UserError
import logging
import time as t

_logger = logging.getLogger(__name__)


class SnkSalesReportWizard(models.TransientModel):
    _name = 'snk.report.sales.wizard'
    _description = 'Sales Analysis Wizard'

    date_start = fields.Datetime('Start Date')
    date_end = fields.Datetime('End Date')
    site_ids = fields.Many2many(
        'snk.site',
        string='Sites',
        domain=lambda self: [
            ('snk_state', '=', 'open'),
        ] if self.env.user.has_group('sales_team.group_sale_manager') else [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ],
    )
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

        effective_at = fields.Datetime.now()
        _logger.info(
            ">>> [START] Generating Sales Analysis Report on %s for user %s",
            effective_at,
            self.env.user.name,
        )

        report_model = self.env['snk.report.sales.analysis']
        # Mass ORM unlink builds a huge protected recordset and can exhaust
        # worker memory for million-row reports. This delete intentionally stays
        # in the same transaction as the report insert so failures roll it back.
        report_model.check_access('unlink')
        self.env.cr.execute(
            """
            SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)
            """,
            ['snk.report.sales.analysis', self.env.uid],
        )
        if not self.env.cr.fetchone()[0]:
            raise UserError(
                "Another Sales Analysis generation is already running for your user."
            )

        report_model.flush_model()
        cleanup_started_at = t.perf_counter()
        self.env.cr.execute(
            """
            DELETE FROM snk_report_sales_analysis
            WHERE create_uid = %s
            """,
            [self.env.uid],
        )
        deleted_count = self.env.cr.rowcount
        report_model.invalidate_model()
        _logger.info(
            ">>> [SUCCESS] Deleted %s previous Sales Analysis rows for user %s in %.2fs.",
            deleted_count,
            self.env.user.name,
            t.perf_counter() - cleanup_started_at,
        )

        params = {
            'user_id': self.env.uid,
            'date_start': self.date_start,
            'date_end': self.date_end,
            'company_ids': company_ids,
        }
        filters_sale = [
            "so.date_order BETWEEN %(date_start)s AND %(date_end)s",
            "so.state NOT IN ('draft', 'cancel')",
            "so.company_id = ANY(%(company_ids)s)",
        ]
        filters_pos = [
            "po.date_order BETWEEN %(date_start)s AND %(date_end)s",
            "po.state NOT IN ('draft', 'cancel')",
            "po.company_id = ANY(%(company_ids)s)",
        ]

        if self.analytic_ids:
            params['brand_ids'] = self.analytic_ids.ids
            filters_sale.append("pp.analytic_id = ANY(%(brand_ids)s)")
            filters_pos.append("pp.analytic_id = ANY(%(brand_ids)s)")
        elif not is_sales_manager:
            params['brand_ids'] = allowed_brand_ids
            filters_sale.append("pp.analytic_id = ANY(%(brand_ids)s)")
            filters_pos.append("pp.analytic_id = ANY(%(brand_ids)s)")

        _logger.info(
            ">>> [START] Generating Sales Analysis Report from %s to %s for user %s",
            self.date_start,
            self.date_end,
            self.env.user.name,
        )
        if self.site_ids:
            params['site_ids'] = self.site_ids.ids
            filters_sale.append("so.snk_site_id = ANY(%(site_ids)s)")
            filters_pos.append("po.snk_site_id = ANY(%(site_ids)s)")
        elif not is_sales_manager:
            params['allowed_industry_ids'] = allowed_industry_ids
            filters_sale.append(
                "s.industry_id = ANY(%(allowed_industry_ids)s)"
            )
            filters_pos.append(
                "s.industry_id = ANY(%(allowed_industry_ids)s)"
            )

        if self.categ_ids:
            params['categ_ids'] = self.categ_ids.ids
            filters_sale.append("ppt.categ_id = ANY(%(categ_ids)s)")
            filters_pos.append("ppt.categ_id = ANY(%(categ_ids)s)")

        if self.product_tmpl_ids:
            params['product_tmpl_ids'] = self.product_tmpl_ids.ids
            filters_sale.append("ppt.id = ANY(%(product_tmpl_ids)s)")
            filters_pos.append("po.product_template_id = ANY(%(product_tmpl_ids)s)")

        where_sale = " AND ".join(filters_sale)
        where_pos = " AND ".join(filters_pos)
        start_time = t.perf_counter()
        _logger.info(">>> [PROCESSING] Generating Sales Analysis Report where_sale: %s and where_pos %s", where_sale, where_pos)
        insert_sql = f"""
            WITH
            site_contact_agg AS (
                SELECT
                    scr.snk_site_id AS site_id,
                    string_agg(rp2.name, ', ') FILTER (
                        WHERE rp2.function = 'PIC'
                          AND rp2.type = 'contact'
                    ) AS pic_id,
                    string_agg(rp2.name, ', ') FILTER (
                        WHERE rp2.function = 'ASMEN'
                          AND rp2.type = 'contact'
                    ) AS manager_id
                FROM snk_site_contact_rel scr
                JOIN res_partner rp2 ON rp2.id = scr.res_partner_id
                WHERE rp2.type = 'contact'
                  AND rp2.function IN ('PIC', 'ASMEN')
                GROUP BY scr.snk_site_id
            ),
            site_tag_agg AS (
                SELECT
                    rel.snk_site_id AS site_id,
                    string_agg(stag.snk_name, ', ') AS site_tag_ids
                FROM snk_site_snk_site_tag_rel rel
                JOIN snk_site_tag stag ON stag.id = rel.snk_site_tag_id
                GROUP BY rel.snk_site_id
            ),
            sale_product_tag_agg AS (
                SELECT
                    rel.product_product_id AS product_id,
                    string_agg(ptag.name::text, ', ') AS product_tag_ids
                FROM product_tag_product_product_rel rel
                JOIN product_tag ptag ON ptag.id = rel.product_tag_id
                GROUP BY rel.product_product_id
            ),
            pos_template_tag_agg AS (
                SELECT
                    rel.product_template_id,
                    string_agg(ptag.name->>'en_US'::text, ', ') AS product_tag_ids
                FROM product_tag_product_template_rel rel
                JOIN product_tag ptag ON ptag.id = rel.product_tag_id
                GROUP BY rel.product_template_id
            ),
            lookup_maps AS (
                SELECT
                    (
                        SELECT jsonb_object_agg(
                            tax.id::text,
                            tax.name->>'en_US'
                        )
                        FROM account_tax tax
                    ) AS tax_names,
                    (
                        SELECT jsonb_object_agg(
                            project.id::text,
                            project.name::text
                        )
                        FROM project_project project
                    ) AS sale_project_names,
                    (
                        SELECT jsonb_object_agg(
                            project.id::text,
                            project.name->>'en_US'
                        )
                        FROM project_project project
                    ) AS pos_project_names
            )
            INSERT INTO snk_report_sales_analysis (
                name, type, company_id, currency_id,
                site_id, site_name, warehouse_id, industry_id, customer_id, site_grade_id, partner_id, city_id, district_id,
                pic_id, manager_id,
                product_id, product_template_id, variant_reference, template_reference, categ_id, grade_id, analytic_id,
                uom_id, product_tag_ids, site_tag_ids, project_ids,
                date_order, product_uom_qty, price_unit, gross_sales, discount, discount_amount, total_discount_amount,
                price_subtotal, price_total, normal_price, price_avg, cost_avg, list_price, tax_id, tax_amount,create_uid, create_date
            )
            SELECT
                name, type, company_id, currency_id,
                site_id, site_name, warehouse_id, industry_id, customer_id, site_grade_id, partner_id, city_id, district_id,
                pic_id, manager_id,
                product_id, product_template_id, variant_reference, template_reference, categ_id, grade_id, analytic_id,
                uom_id, product_tag_ids, site_tag_ids, project_ids,
                date_order, product_uom_qty, price_unit, gross_sales, discount, discount_amount, total_discount_amount,
                price_subtotal, price_total, normal_price, price_avg, cost_avg, list_price, tax_id, tax_amount,
                %(user_id)s, NOW()
            FROM (
                -- SALE
                SELECT
                    so.name AS name,
                    'sale' AS type,
                    so.company_id AS company_id,
                    so.currency_id AS currency_id,
                    s.id AS site_id,
                    s.snk_name AS site_name,
                    s.warehouse_id,
                    s.industry_id,
                    s.customer_id,
                    s.snk_grade_id AS site_grade_id,
                    s.partner_id AS partner_id,
                    rp.city_id AS city_id,
                    rp.snk_district_id AS district_id,
                    contacts.pic_id,
                    contacts.manager_id,
                    sol.product_id AS product_id,
                    pp.product_tmpl_id AS product_template_id,
                    pp.default_code AS variant_reference,
                    ppt.default_code AS template_reference,
                    ppt.categ_id,
                    sol.snk_grade_id AS grade_id,
                    pp.analytic_id AS analytic_id,
                    sol.product_uom AS uom_id,
                    sale_product_tags.product_tag_ids,
                    site_tags.site_tag_ids,
                    (SELECT string_agg(
                            maps.sale_project_names
                                ->> ppsolr.project_project_id::text,
                            ', '
                        )
                        FROM project_project_sale_order_line_rel ppsolr
                        WHERE ppsolr.sale_order_line_id = sol.id
                    ) AS project_ids,
                    so.date_order AS date_order,
                    sol.product_uom_qty AS product_uom_qty,
                    sol.price_unit AS price_unit,
                    sol.price_unit * sol.product_uom_qty AS gross_sales,
                    sol.discount AS discount,
                    sol.snk_discount_amount AS discount_amount,
                    sol.snk_discount_amount * sol.product_uom_qty AS total_discount_amount,
                    sol.price_subtotal AS price_subtotal,
                    sol.price_total AS price_total,
                    pp.snk_normal_price AS normal_price,
                    COALESCE(sol.price_subtotal / NULLIF(sol.product_uom_qty, 0), 0) AS price_avg,
                    COALESCE( (pp.standard_price->>'value')::numeric, 0 ) AS cost_avg,
                    COALESCE(vpi_prod.fixed_price, vpi_tmpl.fixed_price, sol.price_unit) AS list_price,
                    (SELECT string_agg(
                            maps.tax_names ->> atr.account_tax_id::text,
                            ', '
                        )
                        FROM account_tax_sale_order_line_rel atr
                        WHERE atr.sale_order_line_id = sol.id
                    ) AS tax_id,
                    sol.price_tax AS tax_amount
                FROM sale_order_line sol
                LEFT JOIN sale_order so ON sol.order_id = so.id
                LEFT JOIN snk_site s ON so.snk_site_id = s.id
                LEFT JOIN res_partner rp ON rp.id = s.partner_id
                LEFT JOIN product_product pp ON pp.id = sol.product_id
                LEFT JOIN product_template ppt ON ppt.id = pp.product_tmpl_id
                LEFT JOIN site_contact_agg contacts ON contacts.site_id = s.id
                LEFT JOIN site_tag_agg site_tags ON site_tags.site_id = s.id
                LEFT JOIN sale_product_tag_agg sale_product_tags
                    ON sale_product_tags.product_id = pp.id
                CROSS JOIN lookup_maps maps
                LEFT JOIN LATERAL (
                    SELECT price_item.fixed_price
                    FROM product_pricelist_item price_item
                    WHERE price_item.product_id = pp.id
                      AND (
                          price_item.date_start IS NULL
                          OR price_item.date_start <= so.date_order
                      )
                      AND (
                          price_item.date_end IS NULL
                          OR price_item.date_end >= so.date_order
                      )
                    ORDER BY price_item.date_start DESC NULLS LAST,
                             price_item.id DESC
                    LIMIT 1
                ) vpi_prod ON TRUE
                LEFT JOIN LATERAL (
                    SELECT price_item.fixed_price
                    FROM product_pricelist_item price_item
                    WHERE price_item.product_id IS NULL
                      AND price_item.product_tmpl_id = ppt.id
                      AND (
                          price_item.date_start IS NULL
                          OR price_item.date_start <= so.date_order
                      )
                      AND (
                          price_item.date_end IS NULL
                          OR price_item.date_end >= so.date_order
                      )
                    ORDER BY price_item.date_start DESC NULLS LAST,
                             price_item.id DESC
                    LIMIT 1
                ) vpi_tmpl ON TRUE
                WHERE {where_sale}

                UNION ALL

                -- POS
                SELECT
                    po.name AS name,
                    'pos' AS type,
                    po.company_id AS company_id,
                    po.currency_id AS currency_id,
                    s.id AS site_id,
                    s.snk_name AS site_name,
                    s.warehouse_id,
                    s.industry_id,
                    s.customer_id,
                    s.snk_grade_id AS site_grade_id,
                    s.partner_id AS partner_id,
                    rp.city_id AS city_id,
                    rp.snk_district_id AS district_id,
                    contacts.pic_id,
                    contacts.manager_id,
                    po.product_id AS product_id,
                    pp.product_tmpl_id AS product_template_id,
                    pp.default_code AS variant_reference,
                    ppt.default_code AS template_reference,
                    ppt.categ_id,
                    po.grade_id AS grade_id,
                    pp.analytic_id AS analytic_id,
                    ppt.uom_id AS uom_id,
                    pos_template_tags.product_tag_ids,
                    site_tags.site_tag_ids,
                    (SELECT string_agg(
                            maps.pos_project_names ->> ppposr.project_id::text,
                            ', '
                        )
                        FROM snk_pos_order_project_rel ppposr
                        WHERE ppposr.id = po.id
                    ) AS project_ids,
                    po.date_order AS date_order,
                    po.product_uom_qty AS product_uom_qty,
                    po.price_unit AS price_unit,
                    po.price_unit * po.product_uom_qty AS gross_sales,
                    po.discount AS discount,
                    po.discount_amount AS discount_amount,
                    po.discount_amount * po.product_uom_qty AS total_discount_amount,
                    po.price_subtotal AS price_subtotal,
                    po.price_total AS price_total,
                    pp.snk_normal_price AS normal_price,
                    COALESCE(po.price_subtotal / NULLIF(po.product_uom_qty, 0), 0) AS price_avg,
                    COALESCE( (pp.standard_price->>'value')::numeric, 0 ) AS cost_avg,
                    COALESCE(vpi_prod.fixed_price, vpi_tmpl.fixed_price, po.price_unit) AS list_price,
                    (SELECT string_agg(
                            maps.tax_names ->> atr.account_tax_id::text,
                            ', '
                        )
                        FROM account_tax_snk_pos_order_rel atr
                        WHERE atr.snk_pos_order_id = po.id
                    ) AS tax_id,
                    po.price_tax AS tax_amount
                FROM snk_pos_order po
                LEFT JOIN snk_site s ON po.snk_site_id = s.id
                LEFT JOIN res_partner rp ON rp.id = s.partner_id
                LEFT JOIN product_product pp ON pp.id = po.product_id
                LEFT JOIN product_template ppt ON ppt.id = pp.product_tmpl_id
                LEFT JOIN site_contact_agg contacts ON contacts.site_id = s.id
                LEFT JOIN site_tag_agg site_tags ON site_tags.site_id = s.id
                LEFT JOIN pos_template_tag_agg pos_template_tags
                    ON pos_template_tags.product_template_id = ppt.id
                CROSS JOIN lookup_maps maps
                LEFT JOIN LATERAL (
                    SELECT price_item.fixed_price
                    FROM product_pricelist_item price_item
                    WHERE price_item.product_id = pp.id
                      AND (
                          price_item.date_start IS NULL
                          OR price_item.date_start <= po.date_order
                      )
                      AND (
                          price_item.date_end IS NULL
                          OR price_item.date_end >= po.date_order
                      )
                    ORDER BY price_item.date_start DESC NULLS LAST,
                             price_item.id DESC
                    LIMIT 1
                ) vpi_prod ON TRUE
                LEFT JOIN LATERAL (
                    SELECT price_item.fixed_price
                    FROM product_pricelist_item price_item
                    WHERE price_item.product_id IS NULL
                      AND price_item.product_tmpl_id = ppt.id
                      AND (
                          price_item.date_start IS NULL
                          OR price_item.date_start <= po.date_order
                      )
                      AND (
                          price_item.date_end IS NULL
                          OR price_item.date_end >= po.date_order
                      )
                    ORDER BY price_item.date_start DESC NULLS LAST,
                             price_item.id DESC
                    LIMIT 1
                ) vpi_tmpl ON TRUE
                WHERE {where_pos}
            ) AS combined;
            """
        # _logger.warning(">>>>>> FINAL SALES REPORT QUERY:\n%s", insert_sql)
        self.env.cr.execute(insert_sql, params)
        data = self.env.cr.rowcount
        elapsed = t.perf_counter() - start_time
        if data:
            _logger.info(">>> [SUCCEED] Generate %s data report untuk user %s for elapsed: %.2fs", data, self.env.user.name, elapsed)
        else:
            _logger.info(">>> [FAILED] No data to be generated (%s data report untuk user %s)", data, self.env.user.name)

        # ---------------------------------------------------------------------
        # Balikin action ke list view snk.report.sales.analysis biar auto refresh
        # ---------------------------------------------------------------------
        # return {
        #     'type': 'ir.actions.act_window',
        #     'name': 'Sales Analysis Report',
        #     'res_model': 'snk.report.sales.analysis',
        #     'view_mode': 'list,pivot,graph',
        #     'target': 'current',
        # }
