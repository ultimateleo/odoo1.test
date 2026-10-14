
from odoo import models, api, _

class StockRule(models.Model):
    _inherit = 'stock.rule'

    def _get_stock_move_values(self, product_id, product_qty, product_uom, location_dest_id, name, origin, company_id, values):
        vals = super(StockRule, self)._get_stock_move_values(product_id, product_qty, product_uom, location_dest_id, name, origin, company_id, values)
        if values.get('event_ids'):
            vals['event_ids'] = values['event_ids']
        if ('snk_grade_id' in values
                and values.get('_snk_grade_product_id', product_id.id) == product_id.id):
            vals['snk_grade_id'] = values['snk_grade_id']
        return vals