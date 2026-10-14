# -*- coding: utf-8 -*-
from odoo import models, fields, api


class IrActionsServer(models.Model):
    _inherit = 'ir.actions.server'

    confirmation_active = fields.Boolean('Activate Confirmation')
    confirmation = fields.Char('Confirmation Text')

    @api.model
    def get_confirmation(self, id, name):
        server_id = self.env['ir.actions.server'].sudo().search([('id', '=', id), ('name', '=', name)])
        if not server_id or not server_id.confirmation_active:
            return False
        return server_id.confirmation
