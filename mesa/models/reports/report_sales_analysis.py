from odoo import api, fields, models, tools


class ReportSalesAnalysis(models.Model):
    _name = 'snk.report.sales.analysis'
    _description = 'Sales Analysis Report'
    _rec_name = 'name'
    _order = 'date_order desc'
    # _auto = False
    # _table = 'snk_sales_report'

    # ================= IDENTITAS =================
    name = fields.Char('Name')
    type = fields.Selection([('sale', 'Sales Order'), ('pos', 'POS Order')], string='Type')

    company_id = fields.Many2one('res.company', string='Company', ondelete='set null')
    currency_id = fields.Many2one('res.currency', string='Currency', store=True, ondelete='set null')

    # ================= SITE / CUSTOMER =================
    site_id = fields.Many2one('snk.site', 'Site', ondelete='set null')
    site_name = fields.Char('Site Name', store=True, ondelete='set null')
    warehouse_id = fields.Many2one('stock.warehouse', 'Warehouse', related='site_id.warehouse_id', store=True, ondelete='set null')
    industry_id = fields.Many2one('res.partner.industry', 'Industry', related='site_id.industry_id', store=True, ondelete='set null')
    customer_id = fields.Many2one('res.partner', 'Customer', related='site_id.customer_id', store=True, ondelete='set null')
    # site_tag_ids = fields.Many2many('snk.site.tag', string='Site Tags', related='site_id.snk_tag_ids')
    site_tag_ids = fields.Char('Site Tags')
    site_grade_id = fields.Many2one('snk.site.grade', 'Site Grade', related='site_id.snk_grade_id', store=True, ondelete='set null')
    partner_id = fields.Many2one('res.partner', 'Delivery Address', related='site_id.partner_id', store=True, ondelete='set null')
    city_id = fields.Many2one('res.country.city', 'City', related='partner_id.city_id', store=True, ondelete='set null')
    district_id = fields.Many2one('snk.district', 'District', related='partner_id.snk_district_id', store=True, ondelete='set null')

    # ================= PIC / MANAGER =================
    # pic_id = fields.Char('PIC')       # string_agg dari partner.contact_ids function='PIC'
    # manager_id = fields.Char('ASMEN') # string_agg dari partner.contact_ids function='ASMEN'
    pic_id = fields.Char('PIC')
    manager_id = fields.Char('ASMEN')

    # ================= PRODUCT =================
    product_id = fields.Many2one('product.product', 'Product', ondelete='set null')
    product_template_id = fields.Many2one('product.template', 'Template', related='product_id.product_tmpl_id', store=True, ondelete='set null')
    variant_reference = fields.Char('Variant Reference', related='product_id.default_code', store=True)
    template_reference = fields.Char('Template Reference', related='product_template_id.default_code', store=True)
    categ_id = fields.Many2one('product.category', 'Category', store=True, ondelete='set null')
    grade_id = fields.Many2one('snk.product.grade', 'Grade', store=True, ondelete='set null')
    analytic_id = fields.Many2one('account.analytic.account', 'Brand', store=True, ondelete='set null')
    uom_id = fields.Many2one('uom.uom', string='UoM', store=True, ondelete='set null')
    project_ids = fields.Char('Events')            # string_agg dari project.project
    product_tag_ids = fields.Char('Collections/Tags') # string_agg dari product.tag
    

    # ================= FINANCIAL / SALES =================
    date_order = fields.Datetime('Order Date')
    product_uom_qty = fields.Float('Quantity')
    price_unit = fields.Monetary('Sales Price', currency_field='currency_id')
    gross_sales = fields.Monetary('Gross Sales', currency_field='currency_id')
    discount = fields.Float('Discount %')
    discount_amount = fields.Monetary('Discount Amount', currency_field='currency_id')
    total_discount_amount = fields.Monetary('Total Discount Amount', currency_field='currency_id')
    price_subtotal = fields.Monetary('Subtotal', currency_field='currency_id')
    price_total = fields.Monetary('Total', currency_field='currency_id')
    normal_price = fields.Monetary('Normal Price', currency_field='currency_id')
    price_avg = fields.Monetary('Sales Price (AVG)', currency_field='currency_id')
    cost_avg = fields.Monetary('Cost (AVG)', currency_field='currency_id')
    list_price = fields.Monetary('Original Price', currency_field='currency_id')
    tax_id = fields.Char('Taxes')   # string_agg dari account.tax
    tax_amount = fields.Monetary('Tax Amount', currency_field='currency_id')

    # # =========================================================
    # # SQL View Definition
    # # =========================================================
    # def init(self):
    #     tools.drop_view_if_exists(self.env.cr, self._table)
    #     self.env.cr.execute(f"""
    #         CREATE OR REPLACE VIEW {self._table} AS (
    #             -- =============== SALE ORDER LINES ===============
    #             SELECT
    #                 sol.id AS id,
    #                 'sale'::varchar AS type,
    #                 so.company_id AS company_id,
    #                 so.date_order AS date_order,
    #                 so.snk_site_id AS site_id,
    #                 so.warehouse_id AS warehouse_id,

    #                 site.snk_name AS site_name,
    #                 site.industry_id AS industry_id,
    #                 site.snk_grade_id AS site_grade_id,

    #                 partner_customer.id AS customer_id,
    #                 partner_delivery.id AS partner_id,
    #                 partner_delivery.city_id AS city_id,
    #                 partner_delivery.snk_district_id AS district_id,

    #                 (
    #                     SELECT c.id
    #                     FROM snk_site_contact_rel rel
    #                     JOIN res_partner c ON c.id = rel.res_partner_id
    #                     WHERE rel.snk_site_id = site.id
    #                     AND c.type = 'contact'
    #                     AND c.function = 'PIC'
    #                     LIMIT 1
    #                 ) AS pic_id,
    #                 (
    #                     SELECT c.id
    #                     FROM snk_site_contact_rel rel
    #                     JOIN res_partner c ON c.id = rel.res_partner_id
    #                     WHERE rel.snk_site_id = site.id
    #                     AND c.type = 'contact'
    #                     AND c.function = 'ASMEN'
    #                     LIMIT 1
    #                 ) AS manager_id,

    #                 sol.product_id AS product_id,
    #                 sol.product_template_id AS product_template_id,
    #                 sol.price_unit AS price_unit,
    #                 sol.product_uom_qty AS product_uom_qty,
    #                 sol.discount AS discount,
    #                 sol.price_subtotal AS price_subtotal,
    #                 sol.price_total AS price_total,
    #                 (p.snk_normal_price) AS normal_price,
    #                 NULL::integer AS pricelipricelist_item_id,
    #                 NULL::integer AS tax_id,
    #                 NULL::integer AS cost_avg,
    #                 so.name AS name

    #             FROM sale_order_line sol
    #             JOIN sale_order so ON sol.order_id = so.id
    #             JOIN product_product p ON sol.product_id = p.id
    #             LEFT JOIN snk_site site ON site.id = so.snk_site_id
    #             LEFT JOIN res_partner partner_customer ON partner_customer.id = site.customer_id
    #             LEFT JOIN res_partner partner_delivery ON partner_delivery.id = site.partner_id
    #             LEFT JOIN res_country_city city ON city.id = partner_delivery.city_id
    #             LEFT JOIN snk_district district ON district.id = partner_delivery.snk_district_id
    #             WHERE so.state NOT IN ('draft', 'cancel')

    #             UNION ALL

    #             -- =============== POS ORDER LINES ===============
    #             SELECT
    #                 po.id + 1000000 AS id,
    #                 'pos'::varchar AS type,
    #                 po.company_id AS company_id,
    #                 po.date_order AS date_order,
    #                 po.snk_site_id AS site_id,
    #                 po.warehouse_id AS warehouse_id,

    #                 site.snk_name AS site_name,
    #                 site.industry_id AS industry_id,
    #                 site.snk_grade_id AS site_grade_id,

    #                 partner_customer.id AS customer_id,
    #                 partner_delivery.id AS partner_id,
    #                 partner_delivery.city_id AS city_id,
    #                 partner_delivery.snk_district_id AS district_id,

    #                 (
    #                     SELECT c.id
    #                     FROM snk_site_contact_rel rel
    #                     JOIN res_partner c ON c.id = rel.res_partner_id
    #                     WHERE rel.snk_site_id = site.id
    #                     AND c.type = 'contact'
    #                     AND c.function = 'PIC'
    #                     LIMIT 1
    #                 ) AS pic_id,
    #                 (
    #                     SELECT c.id
    #                     FROM snk_site_contact_rel rel
    #                     JOIN res_partner c ON c.id = rel.res_partner_id
    #                     WHERE rel.snk_site_id = site.id
    #                     AND c.type = 'contact'
    #                     AND c.function = 'ASMEN'
    #                     LIMIT 1
    #                 ) AS manager_id,

    #                 po.product_id AS product_id,
    #                 po.product_template_id AS product_template_id,
    #                 po.price_unit AS price_unit,
    #                 po.product_uom_qty AS product_uom_qty,
    #                 po.discount AS discount,
    #                 po.price_subtotal AS price_subtotal,
    #                 po.price_total AS price_total,
    #                 (p.snk_normal_price) AS normal_price,
    #                 NULL::integer AS pricelipricelist_item_id,
    #                 NULL::integer AS tax_id,
    #                 NULL::integer AS cost_avg,
    #                 po.name AS name
    #             FROM snk_pos_order po
    #             JOIN product_product p ON po.product_id = p.id
    #             LEFT JOIN snk_site site ON site.id = po.snk_site_id
    #             LEFT JOIN res_partner partner_customer ON partner_customer.id = site.customer_id
    #             LEFT JOIN res_partner partner_delivery ON partner_delivery.id = site.partner_id
    #             LEFT JOIN res_country_city city ON city.id = partner_delivery.city_id
    #             LEFT JOIN snk_district district ON district.id = partner_delivery.snk_district_id
    #             WHERE po.state NOT IN ('draft', 'cancel')
    #         )
    #     """)
