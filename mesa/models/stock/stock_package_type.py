from odoo import api, fields, models, _


class StockPackageType(models.Model):
    _inherit = "stock.package.type"

    sequence_id = fields.Many2one("ir.sequence", string="Sequence Numbering")
