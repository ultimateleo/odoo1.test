from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.safe_eval import safe_eval


class DeliveryCarrier(models.Model):
    _inherit = "delivery.carrier"

    city_ids = fields.Many2many("res.country.city", string="Cities", copy=True)

    def _match_address(self, partner):
        # bypass match address partner
        return True

    def _match_city(self, city):
        self.ensure_one()
        if self.city_ids and city not in self.city_ids:
            return False
        return True

    def fixed_rate_shipment(self, order):
        carrier = self._match_city(order.city_id)
        if not carrier:
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error: this delivery method is not available for this city.'),
                    'warning_message': False}
        else:
            return super().fixed_rate_shipment(order)

    def base_on_rule_rate_shipment(self, order):
        carrier = self._match_city(order.city_id)
        if not carrier:
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error: this delivery method is not available for this city.'),
                    'warning_message': False}
        else:
            return super().base_on_rule_rate_shipment(order)

    def _get_price_available(self, order):
        self.ensure_one()
        self = self.sudo()
        order = order.sudo()
        total = weight = volume = quantity = wv = 0
        total_delivery = 0.0
        city = order.city_id.id
        for line in order.order_line:
            if line.state == 'cancel':
                continue
            if line.is_delivery:
                total_delivery += line.price_total
            if not line.product_id or line.is_delivery:
                continue
            if line.product_id.type == "service":
                continue
            qty = line.product_uom._compute_quantity(line.product_uom_qty, line.product_id.uom_id)
            weight += (line.product_id.weight or 0.0) * qty
            volume += (line.product_id.volume or 0.0) * qty
            wv += (line.product_id.weight or 0.0) * (line.product_id.volume or 0.0) * qty
            quantity += qty
        total = (order.amount_total or 0.0) - total_delivery

        total = self._compute_currency(order, total, 'pricelist_to_company')
        # weight is either,
        # 1- weight chosen by user in choose.delivery.carrier wizard passed by context
        # 2- saved weight to use on sale order
        # 3- total order line weight as fallback
        weight = self.env.context.get('order_weight') or order.shipping_weight or weight
        return self._get_price_from_picking(total, weight, volume, quantity, wv=wv, city=city)

    def _get_price_dict(self, total, weight, volume, quantity, wv=0., city=False):
        '''Hook allowing to retrieve dict to be used in _get_price_from_picking() function.
        Hook to be overridden when we need to add some field to product and use it in variable factor from price rules. '''
        return {
            'price': total,
            'volume': volume,
            'weight': weight,
            'wv': wv or volume * weight,
            'quantity': quantity,
            'city': city
        }

    def _get_price_from_picking(self, total, weight, volume, quantity, wv=0., city=False):
        price = 0.0
        criteria_found = False
        price_dict = self._get_price_dict(total, weight, volume, quantity, wv=wv, city=city)
        for line in self.price_rule_ids:
            max_value = line.max_value
            if line.variable == 'city':
                max_value = line.city_id.id
            test = safe_eval(line.variable + line.operator + str(max_value), price_dict)
            if test:
                price = line.list_base_price + line.list_price * price_dict[line.variable_factor]
                criteria_found = True
                break
        if not criteria_found:
            raise UserError(_("Not available for current order"))

        return price

    def snk_rate_shipment(self, picking):
        self.ensure_one()
        if hasattr(self, '%s_snk_rate_shipment' % self.delivery_type):
            res = getattr(self, '%s_snk_rate_shipment' % self.delivery_type)(picking)
            # apply fiscal position
            company = self.company_id or picking.company_id or self.env.company
            res['price'] = self.product_id._get_tax_included_unit_price(
                company,
                company.currency_id,
                picking.scheduled_date,
                'sale',
                product_price_unit=res['price'],
                product_currency=company.currency_id
            )
            # apply margin on computed price
            res['price'] = self._apply_margins(res['price'])
            # save the real price in case a free_over rule overide it to 0
            res['carrier_price'] = res['price']
            return res
        else:
            return {
                'success': False,
                'price': 0.0,
                'error_message': _('Error: this delivery method is not available.'),
                'warning_message': False,
            }

    def fixed_snk_rate_shipment(self, picking):
        carrier = self._match_city(picking.city_id)
        if not carrier:
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error: this delivery method is not available for this city.'),
                    'warning_message': False}

        price = picking.partner_id.property_product_pricelist._get_product_price(self.product_id, 1.0)
        return {'success': True,
                'price': price,
                'error_message': False,
                'warning_message': False}
    def base_on_rule_snk_rate_shipment(self, picking):
        carrier = self._match_city(picking.city_id)
        if not carrier:
            return {'success': False,
                    'price': 0.0,
                    'error_message': _('Error: this delivery method is not available for this city.'),
                    'warning_message': False}

        try:
            price_unit = self._get_snk_price_available(picking)
        except UserError as e:
            return {'success': False,
                    'price': 0.0,
                    'error_message': e.args[0],
                    'warning_message': False}

        price_unit = self._compute_snk_currency(picking, price_unit, 'company_to_pricelist')

        return {'success': True,
                'price': price_unit,
                'error_message': False,
                'warning_message': False}

    def _get_snk_price_available(self, picking):
        self.ensure_one()
        self = self.sudo()
        picking = picking.sudo()
        total = weight = volume = quantity = wv = 0
        total_delivery = 0.0
        city = picking.city_id.id
        weight = picking.weight or 0.0
        volume = picking.shipping_volume or 0.0
        quantity = sum(move.quantity for move in picking.move_ids if move.state != 'cancel')
        wv = weight * volume * quantity

        total = 0
        # weight is either,
        # 1- weight chosen by user in choose.delivery.carrier wizard passed by context
        # 2- saved weight to use on sale order
        # 3- total order line weight as fallback
        weight = self.env.context.get('order_weight') or picking.shipping_weight or weight
        return self._get_price_from_picking(total, weight, volume, quantity, wv=wv, city=city)

    def _get_snk_conversion_currencies(self, picking, conversion):
        company_currency = (self.company_id or self.env['res.company']._get_main_company()).currency_id
        pricelist_currency = picking.currency_id

        if conversion == 'company_to_pricelist':
            return company_currency, pricelist_currency
        elif conversion == 'pricelist_to_company':
            return pricelist_currency, company_currency

    def _compute_snk_currency(self, picking, price, conversion):
        from_currency, to_currency = self._get_conversion_currencies(picking, conversion)
        if from_currency.id == to_currency.id:
            return price
        return from_currency._convert(price, to_currency, picking.company_id, picking.scheduled_date or fields.Date.today())