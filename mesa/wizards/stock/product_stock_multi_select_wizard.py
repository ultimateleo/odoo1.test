from odoo import models, fields, api

class ProductStockMultiSelectWizard(models.TransientModel):
    _name = "product.stock.multi.select.wizard"
    _description = "Wizard for selecting multiple products"

    product_ids = fields.Many2many('product.product', string="Products")
    picking_id = fields.Many2one('stock.picking', string="Picking")

    def action_add_products(self):
        picking = self.picking_id
        for product in self.product_ids:
            picking.move_ids_without_package.create({
                'picking_id': picking.id,
                'product_id': product.id,
                'location_id': picking.location_id.id,
                'location_dest_id': picking.location_dest_id.id,
                'name': product.name,
            })
        return {'type': 'ir.actions.act_window_close'}
