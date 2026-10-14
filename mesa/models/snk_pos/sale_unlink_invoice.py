from odoo import models, fields

class AccountMove(models.Model):
    _inherit = 'account.move'

    pos_sale_id = fields.Many2one('snk.pos.sale', 'POS Sale')

    def action_post(self):
        res = super().action_post()
        for move in self:
            sale = self.env['snk.pos.sale'].search([('account_move_ids', 'in', move.id)], limit=1)
            if sale:
                sale.write({'account_move_id': move.id})
        return res

    def button_cancel(self):
        res = super().button_cancel()
        for move in self:
            sale = self.env['snk.pos.sale'].search([('account_move_id', '=', move.id)], limit=1)
            if sale:
                sale._cancel_invoice_flag()
        return res

    def unlink(self):
        for move in self:
            sale = self.env['snk.pos.sale'].search([('account_move_id', '=', move.id)], limit=1)
            if sale:
                sale._reset_invoice_flag()
        return super().unlink()