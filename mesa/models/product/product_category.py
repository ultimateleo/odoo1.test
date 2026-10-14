from odoo import _, api, fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    snk_description = fields.Text("Description")
