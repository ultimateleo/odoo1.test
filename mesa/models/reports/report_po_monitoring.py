from odoo import api, fields, models, tools, _
import logging

_logger = logging.getLogger(__name__)

class ReportPoMonitoring(models.Model):
    _name = 'snk.report.po.monitoring'
    _description = 'PO Monitoring Report'
    _order = 'date_order desc'
    # _auto = False

    # 1. IDs & Relations
    order_line_id = fields.Many2one('purchase.order.line', string='Order Line ID')
    purchase_id = fields.Many2one('purchase.order', string='Purchase Order')
    picking_id = fields.Many2one('stock.picking', string='Reference (Picking)')
    move_line_id = fields.Many2one('stock.move.line', string='Stock Move Line')
    
    # 2. Header Info (Purchase Order)
    snk_po_number = fields.Char('PO Number')
    snk_pr_number = fields.Char('PR Number')
    snk_purchase_type_id = fields.Many2one('snk.purchase.type', string='Purchase Type')
    partner_id = fields.Many2one('res.partner', string='Vendor Name')
    origin = fields.Char('Source Document')
    date_approve = fields.Datetime('Confirmation Date')
    date_planned = fields.Datetime('Expected Arrival')
    snk_approve = fields.Text('Approver')
    note = fields.Html('Note')
    state = fields.Selection([
        ('draft', 'RFQ'),
        ('sent', 'RFQ Sent'),
        ('to approve', 'To Approve'),
        ('purchase', 'Purchase Order'),
        ('approved', 'Approved'),
        ('done', 'Locked'),
        ('cancel', 'Cancelled')
    ], string='Status')

    # 3. Site Info
    snk_site_address_id = fields.Char(string='Sender')
    snk_site_address_name = fields.Char('Sender Name')
    snk_site_id = fields.Char(string='Receiver')
    snk_site_name = fields.Char('Receiver Name')
    picking_type_id = fields.Many2one('stock.picking.type', string='Deliver To')

    # 4. Picking / Delivery Info
    scheduled_date = fields.Datetime('Schedule Date')
    date_done = fields.Datetime('Effective Date')
    snk_physical_date = fields.Datetime('Physical Delivery Date')
    receipt_status = fields.Selection([
        ('pending', 'Not Received'),
        ('partial', 'Partially Received'),
        ('full', 'Fully Received'),
    ], string='Receipt Status')

    # 5. Product Info
    product_id = fields.Many2one('product.product', 'Product')
    snk_description = fields.Text('Description')
    snk_material_code = fields.Char('Material Code')
    product_template_attribute_value_ids = fields.Many2many(
        'product.template.attribute.value', 
        related='product_id.product_template_attribute_value_ids',
        string='Attribute')
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number')
    analytic_id = fields.Many2one('account.analytic.account', string='Brand')

    # 6. Quantities & Pricing
    product_qty = fields.Float('Quantity PO', digits='Product Unit of Measure')
    qty_received = fields.Float('Quantity Received', digits='Product Unit of Measure')
    snk_remaining_order = fields.Float('Remaining', digits='Product Unit of Measure')
    product_uom_id = fields.Many2one('uom.uom', 'Uom')
    price_unit = fields.Float('Unit Price', digits='Product Price')
    snk_amount = fields.Float('Amount', digits='Product Price', help="price_unit * product_qty")
    
    company_id = fields.Many2one('res.company', string='Company')
    currency_id = fields.Many2one('res.currency', string='Currency')
    create_uid = fields.Many2one('res.users', string='Created By')
    date_order = fields.Datetime('Order Date')
