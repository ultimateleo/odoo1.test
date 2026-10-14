from odoo import models, fields

class SnkReportInventoryReserve(models.Model):
    _name = 'snk.report.inventory.reserve'
    _description = 'Inventory Reserved Report'

    move_line_id = fields.Many2one('stock.move.line', string='Stock Move Line Id')
    picking_id = fields.Many2one('stock.picking', string='Reference')
    
    # Source
    snk_site_id = fields.Many2one('snk.site', string='Source Site')
    snk_source_site_name = fields.Char('Source Site Name')
    location_id = fields.Many2one('stock.location', string='Source Location')
    
    # Destination
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
    scheduled_date = fields.Datetime('Schedule Date')
    date_done = fields.Datetime('Effective Date')
    
    # Product
    product_id = fields.Many2one('product.product', string='Product')
    snk_material_code = fields.Char('Material Code')
    product_template_attribute_value_ids = fields.Many2many(
        'product.template.attribute.value', 
        relation='snk_report_reserve_attr_rel', 
        string='Warna/Attribute'
    )
    lot_id = fields.Many2one('stock.lot', string='Lot/Serial Number')
    analytic_ids = fields.Many2one('account.analytic.account', string='Brand')
    
    origin = fields.Char('Source Document')
    purchase_id = fields.Many2one('purchase.order', string='PO Number')
    quantity = fields.Float('Reserved Quantity')
    product_uom_id = fields.Many2one('uom.uom', string='Uom')
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
    create_uid = fields.Many2one('res.users', string='Created by')
    create_date = fields.Datetime('Created on')
