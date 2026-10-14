from odoo import api, fields, models, _


class AnalyticAccount(models.Model):
    _inherit = "account.analytic.account"

    snk_brand = fields.Boolean("Brand")
    snk_site = fields.Boolean("Site")
    snk_event = fields.Boolean("Event")