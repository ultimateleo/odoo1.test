# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class SnkPurchaseMultiProductWizard(models.TransientModel):
    _name = 'snk.purchase.multi.product.wizard'
    _description = 'SNK Purchase multi product Wizard'

    purchase_id = fields.Many2one('purchase.order', string='Order Reference')
    requisition_id = fields.Many2one('purchase.requisition', string='Purchase Agreement')
    product_ids = fields.Many2many('product.product', string='Product', domain=[('purchase_ok', '=', True)], change_default=True, index='btree_not_null')

    def action_confirm(self):
        parent_id = False
        parent_field = False
        child_env = False
        if self.purchase_id:
            parent_id = self.purchase_id.id
            parent_field = 'order_id'
            child_env = 'purchase.order.line'
        elif self.requisition_id:
            parent_id = self.requisition_id.id
            parent_field = 'requisition_id'
            child_env = 'purchase.requisition.line'

        if parent_id:
            for p in self.product_ids:
                self.env[child_env].create({
                    parent_field: parent_id,
                    'product_id': p.id,
                })

        return {'type': 'ir.actions.act_window_close'}
