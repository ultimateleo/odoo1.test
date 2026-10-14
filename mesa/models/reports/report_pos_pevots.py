from odoo import api, fields, models, tools, _
import logging

_logger = logging.getLogger(__name__)


class ReportPosOrder(models.Model):
    _name = 'snk.report.pos.order_pevots'
    _description = 'POS Order Pivot'
    _rec_name = 'name'
    _order = 'date_order desc'
    _auto = True

    # Basic Info
    name = fields.Char('Order Reference')
    state = fields.Char('Status')
    company_id = fields.Many2one('res.company', string='Company')
    currency_id = fields.Many2one('res.currency', string='Currency')
    currency = fields.Char('Currency')

    # Site Info
    site_id = fields.Many2one('snk.site', string='Site')
    site_name = fields.Char('Site Name')
    industry = fields.Char('Industry')
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse')
    customer_id = fields.Many2one('res.partner', string='Customer')
    partner_id = fields.Many2one('res.partner', string='Partner')
    partner_name = fields.Char('Partner')

    # Product Info
    product_id = fields.Many2one('product.product', 'Product')
    product_template_id = fields.Many2one('product.template', string='Product Template')
    template_reference = fields.Char('Template Reference')
    variant_reference = fields.Char('Variant Reference')
    categ_id = fields.Many2one('product.category', string='Category')
    analytic_id = fields.Many2one('account.analytic.account', string='Brand')
    grade = fields.Char('Grade')
    description = fields.Text('Description')
    project_ids = fields.Many2many('project.project', string='Events')

    # Order Info
    date_order = fields.Datetime('Order Date')
    reference = fields.Char('Reference')
    sale_name = fields.Char('Sale Reference')
    delivery_status = fields.Char('Delivery Status')
    promo_code = fields.Char('Promo Code')

    # Pricing & Quantities
    product_uom_qty = fields.Float('Quantity', digits='Product Unit of Measure')
    product_uom = fields.Char('UOM')
    price_unit = fields.Monetary('Unit Price', currency_field='currency_id')
    discount = fields.Float('Discount %')
    discount_amount = fields.Monetary('Discount Amount', currency_field="currency_id")
    price_tax = fields.Monetary('Total Tax', currency_field="currency_id")
    price_subtotal = fields.Monetary('Subtotal', currency_field="currency_id")
    price_total = fields.Monetary('Total', currency_field="currency_id")
