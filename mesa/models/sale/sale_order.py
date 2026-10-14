from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
from odoo.tools.safe_eval import safe_eval
from odoo.osv import expression


class SaleOrder(models.Model):
    _inherit = "sale.order"
    _name = "sale.order"

    analytic_account_ids = fields.Many2many("account.analytic.account", string="Brands", copy=True,
        domain=lambda self: 
            [('snk_brand', '=', True)] if self.env.user.has_group('sales_team.group_sale_manager')
        else [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids)
        ],
        tracking=True)
    event_ids = fields.Many2many("project.project", string="Events", copy=True, domain=[('snk_event', '=', True)])
    city_id = fields.Many2one("res.country.city", string="City", copy=True)
    delivery_type = fields.Selection(related='carrier_id.delivery_type')
    delivery_message = fields.Text(readonly=True)
    price_rule_id = fields.Many2one(
        'delivery.price.rule',  'Pricing Rule', copy=False, compute='_compute_deliver_price_rule'
    )
    display_price = fields.Float(readonly=True)
    delivery_price = fields.Float(string='Total Shipping Cost', readonly=True)
    delivery_cost_price = fields.Float('Shipping Cost', compute='_compute_shipping_cost')
    carrier_currency_id = fields.Many2one(related='carrier_id.currency_id')

    snk_has_voucher = fields.Float("Has Voucher")

    snk_site_id = fields.Many2one('snk.site', string='Site', help="Site",domain= "[('snk_state', '=', 'open')]"
        # domain=lambda self: 
        #     [('snk_state', '=', 'open')] if self.env.user.has_group('sales_team.group_sale_manager')
        # else [
        #     ('snk_state', '=', 'open'),
        #     ('industry_id', 'in', self.env.user.allowed_industry_ids.ids)
        # ]
    )
    snk_site_name = fields.Char(string='Site Name', related='snk_site_id.snk_name', store=True)

    ### Computed methods
    @api.depends('user_id', 'company_id', 'snk_site_id')
    def _compute_warehouse_id(self):
        for order in self:
            if order.snk_site_id:
                order.warehouse_id = order.snk_site_id.warehouse_id
            else:
                super()._compute_warehouse_id()


    def _get_product_catalog_domain(self):
        domain = super()._get_product_catalog_domain()
        # Selalu filter purchase_ok yessssssssssss
        domain = expression.AND([domain, [('purchase_ok', '=', True)]])
        
        # Kalau ada brand yang dipilih, filter berdasarkan analytic_id okeee gasssssssssssssssssssssss
        brand_ids = self.analytic_account_ids.ids
        if brand_ids:
            domain = expression.AND([domain, [('analytic_id', 'in', brand_ids)]])
        
        return domain
    
    def _get_delivery_rate(self):
        vals = self.carrier_id.with_context(order_weight=self.shipping_weight).rate_shipment(self)
        if vals.get('success'):
            self.delivery_message = vals.get('warning_message', False)
            self.delivery_price = vals['price']
            self.display_price = vals['carrier_price']
            return {'no_rate': vals.get('no_rate', False)}
        return {'error_message': vals['error_message']}

    @api.depends('delivery_price', 'price_rule_id')
    def _compute_shipping_cost(self):
        for order in self:
            if order.price_rule_id:
                order.delivery_cost_price = order.price_rule_id.list_base_price + order.price_rule_id.list_price
            else:
                order.delivery_cost_price = order.display_price

    @api.depends('carrier_id', 'city_id')
    def _compute_deliver_price_rule(self):
        for order in self:
            for carrier in order.carrier_id:
                if(not carrier._match_address(order.partner_id)):
                    order.price_rule_id = False
                    continue

            price_rule_id = False
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

            total = order.carrier_id._compute_currency(order, total, 'pricelist_to_company')
            # weight is either,
            # 1- weight chosen by user in choose.delivery.carrier wizard passed by context
            # 2- saved weight to use on sale order
            # 3- total order line weight as fallback
            weight = self.env.context.get('order_weight') or order.shipping_weight or weight

            price_dict = order.carrier_id._get_price_dict(total, weight, volume, quantity, wv=wv, city=city)
            for line in order.carrier_id.price_rule_ids:
                max_value = line.max_value
                if line.variable == 'city':
                    max_value = line.city_id.id
                test = safe_eval(line.variable + line.operator + str(max_value), price_dict)
                if test:
                    price_rule_id = line
                    break

            order.price_rule_id = price_rule_id

    ### Onchange methods
    @api.onchange('carrier_id', 'shipping_weight', 'city_id')
    def _onchange_carrier_id(self):
        if self.delivery_type in ('fixed', 'base_on_rule'):
            vals = self._get_delivery_rate()
            if vals.get('error_message'):
                self.delivery_message = vals.get('error_message', False)
                self.display_price = 0
                self.delivery_price = 0
                return {'error': vals['error_message']}
        else:
            self.display_price = 0
            self.delivery_price = 0

    @api.onchange('snk_site_id')
    def _onchange_snk_site_id(self):
        if self.snk_site_id:
            self.partner_id = self.snk_site_id.customer_id
            self.partner_invoice_id = self.snk_site_id.customer_id
            self.partner_shipping_id = self.snk_site_id.partner_id

    ### Action methods
    @api.model_create_multi
    def create(self, vals_list):
        Config = self.env['ir.config_parameter'].sudo()
        sale_has_voucher = Config.get_param('mesa.snk_sale_has_voucher')

        if sale_has_voucher:
            sale_voucher_product_id = Config.get_param('mesa.snk_sale_voucher_product_id')
            if not sale_voucher_product_id:
                raise ValidationError("Product voucher not configured.")

            for vals in vals_list:
                if "snk_has_voucher" in vals:
                    voucher = vals.get("snk_has_voucher")

                    if voucher > 0 :
                        vals["order_line"].append(
                            Command.create({
                                'product_id': int(sale_voucher_product_id),
                                'product_uom_qty': 1,
                                'price_unit': -1 * voucher,
                            })
                        )

        return super(SaleOrder, self).create(vals_list)

    def snk_action_bulk_confirm(self):
        msg_validation = ""

        for record in self:
            if record.state in "draft, sent":
                record.action_confirm()
            else:
                msg_validation +=  _(
                    "This order '%(order)s' status not Quotation or Quotation Send. \n" % {
                        'order': record.name,
                    }
                )

        if msg_validation:
            raise ValidationError(msg_validation)

        return None

    def snk_action_bulk_return(self):
        msg_validation = ""
        for record in self:
            if not record.picking_ids:
                msg_validation += _(
                    "Nothing to return delivery from this order '%(order)s' \n" % {
                        'order': record.name,
                    }
                )

            for picking in record.picking_ids:
                if picking.state != 'done':
                    msg_validation += _(
                        "Nothing to return delivery from this order '%(order)s' \n" % {
                            'order': record.name,
                        }
                    )
                else:
                    # Create return
                    if picking.return_count == 0:
                        return_wizard = self.env['stock.return.picking'].with_context(
                            active_id=picking.id,
                            active_ids=[picking.id],
                            active_model='stock.picking').create({})
                        for move in return_wizard.product_return_moves:
                            move.write({'quantity': move.move_quantity})

                        return_wizard.action_create_returns()
                    else:
                        msg_validation += _(
                            "Nothing to return delivery from this order '%(order)s'" % {
                                'order': record.name,
                            }
                        )

        if msg_validation:
            raise ValidationError(msg_validation)

        return None

    def _prepare_confirmation_values(self):
        """Pertahankan ``date_order`` HANYA untuk confirm lewat ``snk_action_confirm``.

        Standard Odoo menimpa ``date_order`` dengan ``fields.Datetime.now()``
        lewat write di awal ``action_confirm()``, dan write itu terjadi SEBELUM
        ``_action_confirm()`` melempar procurement. Akibatnya
        ``_prepare_procurement_values()`` (lihat ``sale_order_line.py``) membaca
        date_order yang sudah ter-reset, sehingga ``scheduled_date`` delivery
        ikut jam konfirmasi — bukan order date SO.

        Kasus nyata: SO order date 17/08 00:00 dikonfirmasi jam 23:20, DO-nya
        ter-schedule 17/08 23:20. Mengembalikan date_order setelah
        ``action_confirm()`` selesai tidak menolong, karena picking sudah
        terlanjur dibuat dengan tanggal yang salah — jadi harus dicegah di sini,
        sebelum procurement jalan.

        Dibatasi context ``snk_keep_date_order`` supaya tombol Confirm standard
        di form (dan jalur confirm lain) tetap berperilaku bawaan Odoo: order
        date ikut waktu konfirmasi.
        """
        values = super()._prepare_confirmation_values()
        if self.env.context.get('snk_keep_date_order'):
            values.pop('date_order', None)
        return values

    def snk_action_confirm(self):
        # Confirm tanpa mengubah order date SO, supaya scheduled_date DO ikut
        # order date. Flag dibaca di _prepare_confirmation_values().
        return self.with_context(snk_keep_date_order=True).action_confirm()

    def snk_action_upload_order_id(self):
        return {
            'name': 'Upload Order IDs',
            'type': 'ir.actions.act_window',
            'res_model': 'snk.sale.order.filter',
            'view_mode': 'form',  # list + form
            'target': 'new',  # tetap popup
            'context': {
                'import_enabled': True  # enable tombol import
            },
        }

    def action_auto_fill_brands(self):
        """Auto fill brands from product lines, voucher and shipping products"""
        Config = self.env['ir.config_parameter'].sudo()

        for sale_order in self:
            # Skip if no order lines
            if not sale_order.order_line:
                continue

            # Collect brands from product lines
            product_brands = {}
            for line in sale_order.order_line:
                if line.product_id and line.product_id.analytic_id:
                    product_brands[line.product_id.analytic_id.id] = line.product_id.analytic_id.name

            # ALWAYS add voucher brand if voucher product is configured
            sale_has_voucher = Config.get_param('mesa.snk_sale_has_voucher')
            if sale_has_voucher:
                sale_voucher_product_id = Config.get_param('mesa.snk_sale_voucher_product_id')
                if sale_voucher_product_id:
                    voucher_product = self.env['product.product'].sudo().browse(int(sale_voucher_product_id))
                    if voucher_product and voucher_product.analytic_id:
                        if voucher_product.analytic_id.id not in product_brands:
                            product_brands[voucher_product.analytic_id.id] = voucher_product.analytic_id.name

            # ALWAYS add shipping brand if shipping product is configured
            use_default_shipping = Config.get_param('mesa.snk_default_shipping')
            if use_default_shipping:
                default_shipping_product_id = Config.get_param('mesa.snk_default_shipping_product_id')
                if default_shipping_product_id:
                    shipping_product = self.env['product.product'].sudo().browse(int(default_shipping_product_id))
                    if shipping_product and shipping_product.analytic_id:
                        if shipping_product.analytic_id.id not in product_brands:
                            product_brands[shipping_product.analytic_id.id] = shipping_product.analytic_id.name

            # Check discount product ONLY if discount line exists
            discount_product = self.env['product.product'].sudo().search([
                ('name', '=', 'Discount')
            ], limit=1)
            if discount_product and discount_product.analytic_id:
                has_discount_line = sale_order.order_line.filtered(
                    lambda l: l.product_id.id == discount_product.id
                )
                if has_discount_line and discount_product.analytic_id.id not in product_brands:
                    product_brands[discount_product.analytic_id.id] = discount_product.analytic_id.name

            # Update sale order with all collected brands
            if product_brands:
                brands = list(product_brands.keys())
                sale_order.write({
                    'analytic_account_ids': [(6, 0, brands)]
                })