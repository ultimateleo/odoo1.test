from odoo import fields, models, api, _


class SnkLabelSummaryLine(models.TransientModel):
    _name = 'snk.label.summary.line'
    _description = 'Label Summary Line'

    wizard_id = fields.Many2one('snk.label.summary.wizard', ondelete='cascade')
    # product_id = fields.Many2one('product.product', string='Product', readonly=True)
    # product_tmpl_id = fields.Many2one('product.template', string='Product Template', readonly=True)
    qty = fields.Integer(string='Qty (Labels)', readonly=True)
    price_unit = fields.Float(string='Price / Label', readonly=True, digits='Product Price')
    total_price = fields.Float(string='Total Price', readonly=True, digits='Product Price')
    currency_id = fields.Many2one('res.currency', readonly=True)
    # is_filler = fields.Boolean(string='Filler', readonly=True, default=False)


class SnkLabelSummaryWizard(models.TransientModel):
    _name = 'snk.label.summary.wizard'
    _description = 'Label Summary Wizard'

    picking_id = fields.Many2one('stock.picking', readonly=True)
    line_ids = fields.One2many('snk.label.summary.line', 'wizard_id', readonly=True)
    total_labels = fields.Integer(string='Total Labels', compute='_compute_totals')
    total_price = fields.Float(string='Grand Total', compute='_compute_totals', digits='Product Price')
    currency_id = fields.Many2one('res.currency', related='picking_id.company_id.currency_id')

    @api.depends('line_ids')
    def _compute_totals(self):
        for rec in self:
            rec.total_labels = sum(rec.line_ids.mapped('qty'))
            rec.total_price = sum(rec.line_ids.mapped('total_price'))

    @api.model
    def _build_from_picking(self, picking):
        """Create wizard with summary data from a stock.picking record.
        """
        # MINIMUM_LABELS = 15

        pricelist = picking.env['product.pricelist'].search(
            [('company_id', 'in', [picking.company_id.id, False])],
            order='sequence asc', limit=1
        )

        groups = {}

        for move in picking.move_ids_without_package.filtered(lambda m: m.state != 'cancel'):
            product = move.product_id
            qty = int(move.quantity or 0)
            if qty <= 0:
                continue

            if pricelist:
                price = pricelist._get_product_price(product, 1, pricelist.currency_id or product.currency_id)
                currency = pricelist.currency_id or product.currency_id
            else:
                price = product.lst_price
                currency = product.currency_id

            price_key = round(price, 6)

            if price_key not in groups:
                groups[price_key] = {
                    'qty': 0,
                    'price_unit': price,
                    'currency': currency,
                    # 'is_filler': False,
                }
            groups[price_key]['qty'] += qty

        # total_labels = sum(g['qty'] for g in groups.values())
        # filler_added = 0
        # filler_key = None

        # if total_labels < MINIMUM_LABELS and groups:
        #     filler_added = MINIMUM_LABELS - total_labels
        #     filler_key = list(groups.keys())[-1]
        #     groups[filler_key]['qty'] += filler_added
        #     groups[filler_key]['is_filler'] = True

        lines = []
        for group in groups.values():
            qty = group['qty']
            price = group['price_unit']
            lines.append({
                'qty': qty,
                'price_unit': price,
                'total_price': price * qty,
                'currency_id': group['currency'].id,
                # 'is_filler': group['is_filler'],
            })

        wizard = picking.env['snk.label.summary.wizard'].create({
            'picking_id': picking.id,
            'line_ids': [(0, 0, l) for l in lines],
        })
        return wizard

    def action_print_labels(self):
        """Proceed to the standard print label wizard from here."""
        self.ensure_one()
        return self.picking_id.action_print_product_labels()