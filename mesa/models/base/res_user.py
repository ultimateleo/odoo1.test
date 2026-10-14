from odoo import models, fields, api

class ResUser(models.Model):
    _inherit = 'res.users'

    # allowed_site_ids = fields.Many2many('snk.site', string='Allowed Sites', domain="[('snk_state','=','open'),('active','=',True)]")
    allowed_analytic_ids = fields.Many2many('account.analytic.account', string='Allowed Brands', domain="[('snk_brand','=',True),('active','=',True)]")
    allowed_industry_ids = fields.Many2many('res.partner.industry', string='Allowed Industries')