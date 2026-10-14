from odoo import _, api, fields, models


class ResPartner(models.Model):
    _inherit = "res.partner"

    city_id = fields.Many2one("res.country.city", string="City", copy=True)
    snk_fax = fields.Char(string="Fax")
    snk_status = fields.Selection([('0', 'Inactive'),
                                   ('1', 'Active'),
                                   ('2', 'Gudang')], string="POS Status", default="0", tracking=True, copy=True)
    snk_is_not_efaktur = fields.Boolean("Is Not Use Efaktur")
    snk_district_id = fields.Many2one("snk.district", string="District")

    def action_change_snk_status(self, status):
        for rec in self:
            rec.snk_status = status
            rec._onchange_snk_status()

    @api.onchange('city_id')
    def _onchange_city_id(self):
        city = False
        zip = False
        if self.city_id:
            city = self.city_id.name
            zip = self.city_id.zip_id.name

        self.city = city
        self.zip = zip

    @api.onchange('snk_status')
    def _onchange_snk_status(self):
        data = self.env['stock.warehouse'].sudo().search([
                    ('partner_id', 'in', self.ids)
                ])

        for warehouse in data:
            if self.snk_status == '1':
                warehouse.snk_active_or_create_pos_picking_type()
            else:
                warehouse.snk_inactive_pos_picking_type()


