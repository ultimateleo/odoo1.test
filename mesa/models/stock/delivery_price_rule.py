from odoo import api, fields, models, _

VARIABLE_SELECTION = [
    ('weight', "Weight"),
    ('volume', "Volume"),
    ('wv', "Weight * Volume"),
    ('price', "Price"),
    ('quantity', "Quantity"),
    ('city', 'City')
]


class DeliveryPriceRule(models.Model):
    _inherit = "delivery.price.rule"

    variable = fields.Selection(selection=VARIABLE_SELECTION, required=True, default='quantity')
    operator_city = fields.Selection(
        [('==', '=')],
        default='==')
    city_id = fields.Many2one("res.country.city", string="City")

    @api.onchange('operator_city')
    def onchange_operator_city(self):
        self.operator = self.operator_city

    @api.depends('variable', 'operator', 'max_value', 'list_base_price', 'list_price', 'variable_factor', 'currency_id',
                 'city_id')
    def _compute_name(self):
        super(DeliveryPriceRule, self)._compute_name()

        for rule in self:
            if rule.variable == 'city':
                base_name = rule.name.split('then')[-1].strip()
                rule.name = "if %s %s %s then %s" % (
                    rule.variable, rule.operator, rule.city_id.name or '', base_name
                )




