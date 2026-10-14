from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError


class ResCountryCity(models.Model):
    _name = "res.country.city"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "Master Country City"

    name = fields.Char("Name", tracking=True, help="“City can be mapped with the zip prefix like city, regency, municipality, district and other.”")
    zip_id = fields.Many2one("delivery.zip.prefix", tracking=True, help="“The city code.”")
    state_id = fields.Many2one("res.country.state", string="State", tracking=True)

    @api.constrains('name', 'zip_id')
    def _check_unique_name_zip(self):
        for record in self:
            domain = [
                ('name', '=', record.name),
                ('zip_id', '=', record.zip_id.id),
                ('id', '!=', record.id)
            ]
            if self.search_count(domain) > 0:
                raise ValidationError("The combination of City Name and Zip Code must be unique.")
