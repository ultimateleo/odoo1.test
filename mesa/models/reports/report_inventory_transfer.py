from odoo import api, fields, models, tools, _
import logging

_logger = logging.getLogger(__name__)

class ReportInventoryTransfer(models.Model):
    _name = 'snk.report.inventory.transfer'
    _description = 'Inventory Transfer Report'

    from_date = fields.Datetime('From Date', required=True)
    to_date = fields.Datetime('To Date', required=True)
    date = fields.Datetime('Date', related='picking_id.date', store=True)
    move_line_id = fields.Many2one('stock.move.line', string='Stock Move Line Id', ondelete='cascade')
    picking_id = fields.Many2one('stock.picking', string='Reference', ondelete='cascade')
    snk_create_date = fields.Datetime('STO Date', related='picking_id.snk_create_date', store=True)
    scheduled_date = fields.Datetime('Scheduled Date', related='picking_id.scheduled_date', store=True)
    date_done = fields.Datetime('Effective Date', related='picking_id.date_done', store=True)

    # Product Template
    snk_product_template_id = fields.Many2one('product.template', string='Product Template')
    default_code_template = fields.Char('Template Reference', related='snk_product_template_id.default_code', store=True)
    categ_id = fields.Char('Product Category')
    snk_grade_id = fields.Many2one(string='Grade', related='snk_product_template_id.snk_grade_id', store=True) 
    
    
    # Source
    snk_site_id = fields.Many2one('snk.site', string='Source Site')
    snk_source_site_name = fields.Char('Source Site Name')
    location_id = fields.Many2one('stock.location', string='Source Location')
    
    # Destinationproduct_template_attribute_value_ids 
    snk_site_addres_id = fields.Many2one('snk.site', string='Destination Site')
    snk_dest_site_name = fields.Char('Destination Site Name')
    location_dest_id = fields.Many2one('stock.location', string='Destination Location')
    
    picking_type_code = fields.Selection([
        ('incoming', 'Receipt'),
        ('outgoing', 'Delivery'),
        ('internal', 'Internal'),
    ], string='Type of Operation')
    picking_type_id = fields.Many2one('stock.picking.type', string='Operation Type')
    snk_physical_date = fields.Date('Physical Delivery Date')

    # Product
    product_id = fields.Many2one('product.product', string='Product')
    default_code_variant = fields.Char('Variant Reference', related='product_id.default_code', store=True)
    snk_material_code = fields.Char('Material Code')
    product_template_attribute_value_ids = fields.Many2many(
        'product.template.attribute.value',
        'snk_report_transfer_attr_rel',
        'report_id',   # LEFT COLUMN
        'attribute_id', # RIGHT COLUMN
        string='Warna/Attribute'
    )
    currency_id = fields.Char('Currency')
    unit_price = fields.Monetary('Unit Price', currency_field='currency_id')
    total_amount = fields.Monetary('Total Amount', currency_field='currency_id')

    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number')
    analytic_ids = fields.Many2one('account.analytic.account', string='Brand')
    
    origin = fields.Char('Source Document')
    purchase_id = fields.Many2one('purchase.order', string='PO Number')
    quantity = fields.Float('Reserved Quantity')
    actual_quantity = fields.Float('Actual Quantity')
    product_uom_id = fields.Many2one('uom.uom', string='Uom')
    product_uom_qty = fields.Float('Initial Demand')
    note = fields.Html('Note')
    
    state = fields.Selection([
        ('draft', 'New'),
        ('waiting', 'Waiting Another Move'),
        ('confirmed', 'Waiting Availability'),
        ('partially_available', 'Partially Available'),
        ('assigned', 'Available'),
        ('done', 'Done'),
        ('cancel', 'Cancelled'),
    ], string='Status')
    
    company_id = fields.Many2one('res.company', string='Company')
    # create_uid = fields.Many2one('res.users', string='Created by')
    create_date = fields.Datetime('Created on')