from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools import DEFAULT_SERVER_DATETIME_FORMAT
from markupsafe import Markup
import datetime
import pytz


class SnkStagingSale(models.Model):
    _name = 'snk.staging.sale'
    _description = "Staging for Sale Order"

    # mapping to header
    name = fields.Char("Order Reference", required=True)
    date_order = fields.Datetime("Order Date", required=True)
    partner_id = fields.Char("Customer", required=False)
    analytic_account_ids = fields.Text('Brands', required=False)
    warehouse_id = fields.Char("Warehouse")
    carrier_id = fields.Char("Delivery Method")
    city_id = fields.Char("City")
    shipping_weight = fields.Float("Shipping Weight")
    # mapping to lines
    product_id = fields.Char("Product")
    product_uom_qty = fields.Float("Quantity")
    price_unit = fields.Float("Unit Price")
    discount_amount = fields.Float("Disc. Amount")
    snk_has_voucher = fields.Float("Has Voucher")
    shipping_charge = fields.Float("Shipping Charge")
    snk_discount = fields.Float("Discount")
    # status staging
    status = fields.Selection([('pending', 'Pending'), ('post', 'Posted'), ('duplicate', 'Duplicate'), ('failed', 'Failed')],
                              string="Status", default='pending')
    snk_site_id = fields.Char(string='Site')

    # log info
    log_info = fields.Html(string="Log Info")

    def _get_user_datetime_now(self):
        """Get current datetime in user's timezone"""
        # Get user timezone
        tz_name = self.env.context.get('tz') or self.env.user.tz or 'UTC'
        tz = pytz.timezone(tz_name)

        # Convert UTC now to user timezone
        utc_now = datetime.datetime.utcnow()
        utc_dt = pytz.UTC.localize(utc_now)
        user_dt = utc_dt.astimezone(tz)

        return user_dt.strftime('%d/%m/%Y %H:%M:%S')

    def action_execute_staging(self):
        self = self.sudo()
        grouped = {}
        for staging in self.filtered(lambda x: x.status not in ['post', 'duplicate']):
            grouped.setdefault(staging.name, []).append(staging)

        for order_ref, stagings in grouped.items():
            try:
                first = stagings[0]
                log_info = Markup(
                    '<p><b>Executed - %s</b></p>\n') % self._get_user_datetime_now()
                log_info += Markup('<ul>')

                success_check = True
                status = 'post'
                errors = []

                # Cari site berdasarkan code jika snk_site_id terisi
                site = None
                if first.snk_site_id:
                    site = self.env['snk.site'].sudo().search(
                        [('snk_code', '=', first.snk_site_id)], limit=1
                    )
                    if not site:
                        errors.append(_('Site `%s` not found.') % first.snk_site_id)
                    else:
                        if not site.partner_id:
                            errors.append(_('Site `%s` does not have a delivery address assigned.') % first.snk_site_id)

                # Jika site ditemukan, gunakan partner dari site
                # Jika tidak ada site, cari partner dari field partner_id
                partner = None
                if site and site.partner_id:
                    # Prioritas: ambil dari delivery address
                    partner = site.partner_id
                elif site and site.customer_id:
                    # Fallback: ambil dari customer (invoice address)
                    partner = site.customer_id
                elif first.partner_id:
                    partner = self.env['res.partner'].sudo().search(
                        [('name', '=', first.partner_id)], limit=1
                    )
                    if not partner:
                        errors.append(_('Partner `%s` not found.') % first.partner_id)
                else:
                    errors.append(_('Customer is required.'))

                warehouse = self.env['stock.warehouse'].sudo().search(
                    [('name', '=', first.warehouse_id)], limit=1
                )
                if not warehouse and first.warehouse_id:
                    errors.append(_('Warehouse `%s` not found.') %
                                  first.warehouse_id)

                carrier = self.env['delivery.carrier'].sudo().search(
                    [('name', '=', first.carrier_id)], limit=1
                )
                if not carrier and first.carrier_id:
                    errors.append(
                        _('Delivery Method `%s` not found.') % first.carrier_id)

                city = self.env['res.country.city'].sudo().search(
                    [('name', 'ilike', first.city_id)], limit=1
                )
                if not city and first.city_id:
                    errors.append(_('City `%s` not found.') % first.city_id)

                brands = []
                if first.analytic_account_ids:
                    brand_names = [
                        b.strip() for b in first.analytic_account_ids.split(",") if b.strip()]
                    for name in brand_names:
                        analytic_account = self.env['account.analytic.account'].sudo().search([
                            ('name', '=', name),
                            ('snk_brand', '=', True)
                        ], limit=1)
                        if analytic_account:
                            brands.append(analytic_account.id)
                        else:
                            errors.append(_('Brand `%s` not found.') % name)
                else:
                    # Jika analytic account kosong, ambil dari brand product
                    product_brands = {}  # Gunakan dict untuk menyimpan id dan name
                    for staging in stagings:
                        product = self.env['product.product'].sudo().search([
                            ('default_code', '=', staging.product_id)
                        ], limit=1)
                        if product and product.analytic_id:
                            product_brands[product.analytic_id.id] = product.analytic_id.name
                    brands = list(product_brands.keys())

                    # JANGAN update staging dulu, tunggu sampai brands lengkap dengan voucher & shipping

                # VALIDASI EXTRA LINES
                Config = self.env['ir.config_parameter'].sudo()

                # --- Voucher check ---
                voucher_product = None
                if first.snk_has_voucher > 0:
                    sale_has_voucher = Config.get_param(
                        'mesa.snk_sale_has_voucher')
                    if sale_has_voucher:
                        sale_voucher_product_id = Config.get_param(
                            'mesa.snk_sale_voucher_product_id')
                        if not sale_voucher_product_id:
                            errors.append(_('Product voucher not configured.'))
                        else:
                            voucher_product = self.env['product.product'].sudo().browse(
                                int(sale_voucher_product_id))
                            if not voucher_product:
                                errors.append(_('Voucher product not found.'))
                    else:
                        errors.append(
                            _('Voucher setting not enabled in config.'))

                # --- Discount check ---
                staging_discount = self.env['snk.staging.sale'].sudo().search([
                    ('name', '=', first.name),
                    ('snk_discount', '>', 0),
                ], limit=1)

                discount_product = None
                if staging_discount:
                    discount_product = self.env['product.product'].sudo().search([
                        ('name', '=', 'Discount')
                    ], limit=1)
                    if not discount_product:
                        errors.append(_('Product `Discount` not found.'))

                # --- Shipping check ---
                shipping_product = None

                if first.shipping_charge > 0:
                    use_default_shipping = Config.get_param(
                        'mesa.snk_default_shipping')
                    if use_default_shipping:
                        default_shipping_product_id = Config.get_param(
                            'mesa.snk_default_shipping_product_id')
                        if default_shipping_product_id:
                            shipping_product = self.env['product.product'].sudo().browse(
                                int(default_shipping_product_id))
                        else:
                            errors.append(
                                _('Default shipping product not configured.'))
                    else:
                        errors.append(
                            _('Shipping not provided in staging because default shipping product not enabled.'))

                # Tambahkan brand dari voucher dan shipping product ke brands (jika brands diambil dari product)
                # Cek apakah product_brands terdefinisi (berarti brands diambil dari product, bukan dari Excel)
                if 'product_brands' in locals() and isinstance(product_brands, dict):
                    # Cek voucher product
                    if voucher_product and voucher_product.analytic_id:
                        if voucher_product.analytic_id.id not in brands:
                            product_brands[voucher_product.analytic_id.id] = voucher_product.analytic_id.name
                            brands.append(voucher_product.analytic_id.id)

                    # Cek shipping product
                    if shipping_product and shipping_product.analytic_id:
                        if shipping_product.analytic_id.id not in brands:
                            product_brands[shipping_product.analytic_id.id] = shipping_product.analytic_id.name
                            brands.append(shipping_product.analytic_id.id)

                    # Update staging dengan brands yang lengkap (termasuk voucher & shipping)
                    if product_brands:
                        brand_names_str = ', '.join(product_brands.values())
                        for staging in stagings:
                            staging.write({'analytic_account_ids': brand_names_str})

                # VALIDASI PRODUCT LINES
                for staging in stagings:
                    product = self.env['product.product'].search([
                        ('default_code', '=', staging.product_id)], limit=1
                    )
                    if not product:
                        errors.append(_('Product %s not found.') %
                                      staging.product_id)

                # CEK ERROR
                if errors:
                    status = 'failed'
                    success_check = False
                    for err in errors:
                        log_info += Markup('<li>%s</li>') % err
                    log_info += Markup('</ul>')

                    for staging in stagings:
                        staging.write({'status': status, 'log_info': log_info})
                    continue

                if success_check:
                    # CREATE SALE ORDER
                    sale_order = self.env['sale.order'].search(
                        [('name', '=', order_ref)], limit=1)
                    if not sale_order:
                        sale_order = self.env['sale.order'].create({
                            'name': order_ref,
                            'partner_id': partner.id,
                            'date_order': first.date_order,
                            'warehouse_id': warehouse.id if warehouse else False,
                            'carrier_id': carrier.id if carrier else False,
                            'analytic_account_ids': [(6, 0, brands)],
                            'snk_site_id': site.id if site else False,
                        })

                    # Buat order lines dari staging
                    for staging in stagings:
                        product = self.env['product.product'].search([
                            ('default_code', '=', staging.product_id)
                        ], limit=1)

                        if not product:
                            added_log_info = staging.log_info or Markup('<ul>')
                            added_log_info += Markup(
                                '<li>Product not found</li>')
                            added_log_info += Markup('</ul>')
                            staging.write({
                                'status': 'failed',
                                'log_info': added_log_info
                            })
                            continue

                        existing_line = sale_order.order_line.filtered(
                            lambda l: l.product_id.id == product.id)

                        if existing_line:
                            added_log_info = staging.log_info or Markup('<ul>')
                            added_log_info += Markup(
                                '<li>Duplicate product %s</li>' % product.display_name)
                            added_log_info += Markup('</ul>')
                            staging.write({
                                'status': 'duplicate',
                                'log_info': added_log_info
                            })
                            continue

                        sale_order.order_line.create({
                            'order_id': sale_order.id,
                            'product_id': product.id,
                            'product_uom_qty': staging.product_uom_qty,
                            'price_unit': staging.price_unit,
                            'snk_discount_amount': staging.discount_amount or 0,
                        })
                        log_info += Markup('</ul>')
                        staging.write({'status': 'post', 'log_info': log_info})

                    # --- Tambahkan voucher line ---
                    if first.snk_has_voucher > 0 and voucher_product:
                        existing_voucher = sale_order.order_line.filtered(
                            lambda l: l.product_id.id == voucher_product.id)
                        if not existing_voucher:
                            last_seq = max(
                                sale_order.order_line.mapped('sequence') or [10])
                            self.env['sale.order.line'].sudo().create({
                                'order_id': sale_order.id,
                                'product_id': voucher_product.id,
                                'product_uom_qty': 1,
                                'price_unit': -abs(first.snk_has_voucher),
                                'sequence': last_seq + 10,
                            })

                    # --- Tambahkan discount line ---
                    if staging_discount and discount_product:
                        existing_discount = sale_order.order_line.filtered(
                            lambda l: l.product_id.id == discount_product.id)
                        if not existing_discount:
                            last_seq = max(
                                sale_order.order_line.mapped('sequence') or [10])
                            self.env['sale.order.line'].sudo().create({
                                'order_id': sale_order.id,
                                'product_id': discount_product.id,
                                'product_uom_qty': 1,
                                'price_unit': -abs(staging_discount.snk_discount),
                                'sequence': last_seq + 10,
                            })

                    # --- Tambahkan shipping line ---
                    if first.shipping_charge > 0 and shipping_product:
                        existing_shipping = sale_order.order_line.filtered(
                            lambda l: l.product_id.id == shipping_product.id)
                        if not existing_shipping:
                            last_seq = max(
                                sale_order.order_line.mapped('sequence') or [10])
                            self.env['sale.order.line'].sudo().create({
                                'order_id': sale_order.id,
                                'product_id': shipping_product.id,
                                'product_uom_qty': 1,
                                'price_unit': first.shipping_charge,
                                'sequence': last_seq + 10,
                            })

                    # Update brands di Sale Order dengan brands yang lengkap (termasuk voucher & shipping)
                    if 'product_brands' in locals() and isinstance(product_brands, dict) and brands:
                        sale_order.write({
                            'analytic_account_ids': [(6, 0, brands)]
                        })

            except Exception as e:
                self.env.cr.rollback()
                log_info = Markup(
                    '<p><b>Exception - %s</b></p>\n') % self._get_user_datetime_now()
                log_info += Markup('<ul><li>`%s`</li></ul>') % str(e)
                for staging in stagings:
                    staging.write({'status': 'failed', 'log_info': log_info})


class SnkStagingSaleOrderLine(models.Model):
    _name = "snk.staging.sale.line"
    _description = "Staging for Sale Order Line"

    staging_sale_id = fields.Many2one(
        'snk.staging.sale', ondelete='cascade', string="Staging Ref")
    order_reference = fields.Char("Order ID")
    product_template_id = fields.Char("Product")
    product_code = fields.Char("Product Code")
    analytic_id = fields.Char("Brand")
    warehouse_id = fields.Char("Warehouse")
    snk_discount_amount = fields.Float("Disc. Amount")
    quantity = fields.Float("Quantity")
    price_unit = fields.Float("Unit Price")
    discount = fields.Float("Discount")
    snk_has_voucher = fields.Float("Has Voucher")
    snk_discount = fields.Float("Discount Line")
    status = fields.Selection([('pending', 'Pending'), ('post', 'Posted'), ('duplicate', 'Duplicate'), ('failed', 'Failed')],
                              string="Status", default='pending')

    # log info
    log_info = fields.Html(string="Log Info")
