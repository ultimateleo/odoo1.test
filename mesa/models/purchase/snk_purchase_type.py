from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError



class SnkPurchaseType(models.Model):
    _name = "snk.purchase.type"
    _description = "Purchase Type"
    _rec_name = "display_name"

    snk_name = fields.Char("Name")
    snk_code = fields.Char("Code")
    snk_description = fields.Char("Description")
    snk_sequence_id = fields.Many2one("ir.sequence", string="Sequence")
    snk_type = fields.Selection([('agreement', 'Purchase Agreement'),
                                 ('order', 'Purchase Order')], string="Type")

    @api.depends('snk_name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.snk_name

    @api.constrains('snk_code')
    def _check_unique_code(self):
        for record in self:
            if record.snk_code:
                domain = [
                    ('snk_code', '=', record.snk_code),
                    ('id', '!=', record.id)
                ]
                if self.search_count(domain) > 0:
                    raise ValidationError("You cannot have the same code")