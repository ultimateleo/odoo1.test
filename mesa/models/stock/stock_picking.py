# pyrefly: ignore [missing-import]
from odoo import api, fields, models, tools, _, SUPERUSER_ID
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval
from odoo.osv import expression
from odoo.tools import format_datetime, format_date, format_list, groupby, SQL
from odoo.tools.float_utils import float_compare, float_is_zero
from collections import defaultdict
from datetime import timedelta
import logging
import io
import base64

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None

_logger = logging.getLogger(__name__)


class Picking(models.Model):
    _inherit = ["stock.picking", 'product.catalog.mixin']
    _name = "stock.picking"

    def _default_picking_type_id(self):
        # if self.env.user.has_group('mesa.snk_is_mutation'):
        #     picking_type_code = self.env.context.get('restricted_picking_type_code')
        #     _logger.info(">>>>> karena ini woi")
        #     return self._get_picking_type(self.env.context.get('company_id') or self.env.company.id, picking_type_code)
        # picking_type_code = self.env.context.get('restricted_picking_type_code')
        # site = self.snk_site_id
        # warehouse = (
        #     site.warehouse_id
        #     if site and site.warehouse_id
        #     else self.env.context.get('default_warehouse_id') and self.env['stock.warehouse'].browse(self.env.context['default_warehouse_id'])
        #     or self.env['stock.warehouse'].search([('company_id', '=', self.env.company.id)], limit=1)
        # )
        # if site:
        #     _logger.info(">>>>> Masuk nih picking type dengan site: %s, warehouse: %s", site.snk_code, warehouse.code)
        #     return self._get_picking_type(
        #         company_id= self.env.company.id,
        #         picking_type_code= picking_type_code,
        #         site=site,
        #         warehouse=warehouse
        #     )
        # else:
            return super()._default_picking_type_id()

    snk_approve = fields.Text(string="Approver")

    analytic_ids = fields.Many2many("account.analytic.account", copy=True, string="Brands", store=True, compute='_compute_analytic_ids', readonly=False, inverse='_inverse_analytic_ids')
    event_ids = fields.Many2many("project.project", copy=True, string="Events", store=True, compute='_compute_event_ids')

    city_id = fields.Many2one("res.country.city", string="City", compute='_compute_city_id', store=True, readonly=False, index=True)
    carrier_id = fields.Many2one(index=True)
    delivery_type = fields.Selection(related='carrier_id.delivery_type')
    delivery_message = fields.Text(readonly=True)
    snk_bill_id = fields.Many2one("account.move", string="Bill Reference")
    # price_rule_id = fields.Many2one(
    #     'delivery.price.rule',  'Pricing Rule', copy=False, compute='_compute_deliver_price_rule', store=True
    # )
    price_rule_id = fields.Many2one(
        'delivery.price.rule',
        string='Pricing Rule',
        copy=False, store=True
    )
    display_price = fields.Float(readonly=True)
    delivery_price = fields.Float(string='Total Shipping Cost', readonly=True)
    delivery_cost_price = fields.Float('Shipping Cost', compute='_compute_shipping_cost', store=True)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id')
    carrier_currency_id = fields.Many2one(related='carrier_id.currency_id')
    snk_pos_ref = fields.Char("POS Document", readonly=True, copy=False)
    snk_physical_date = fields.Datetime(string="Physical Delivery Date")
    # Disabled override to allow standard delivery module computed weight to function
    shipping_weight = fields.Float(store=True)
    weight = fields.Float(compute='_cal_weight', store=True)

    @api.depends('shipping_weight', 'move_ids.weight')
    def _cal_weight(self):
        super()._cal_weight()
        for picking in self:
            _logger.debug(">>>>> [_cal_weight] picking: %s, shipping_weight: %s, weight: %s",
                         picking.name, picking.shipping_weight, picking.weight)
            if picking.shipping_weight:
                picking.weight = picking.shipping_weight
                _logger.debug(">>>>> [_cal_weight] Overrode weight with shipping_weight: %s", picking.weight)

    snk_stock_order_id = fields.Many2one("snk.stock.transfer.order.line", string="Stock Order")
    transfer_id = fields.Many2one("snk.stock.transfer.order")
    transfer_line_ids = fields.Many2many("snk.stock.transfer.order.line", string="Transfer Lines")
    snk_create_date = fields.Datetime(
        string='STO Date',
        default=lambda self: fields.Datetime.now(),
        copy=False
    )

    snk_site_id = fields.Many2one('snk.site', string='Source Site', help="Source Site", compute='_compute_snk_site_id', store=True, readonly=False, index=True)
    snk_source_site_name = fields.Char(string='Source Site Name', help="Source Site", related='snk_site_id.full_name', store=True, readonly=True, index=True)
    snk_site_warehouse_id = fields.Many2one('stock.warehouse', related='snk_site_id.warehouse_id', store=True)
    snk_site_address_id = fields.Many2one('snk.site', string='Destination Site', help="Site", store=True, readonly=False, index=True)
    snk_dest_site_name = fields.Char(string='Destination Site Name', help="Destination Site",  related='snk_site_address_id.full_name', store=True, readonly=True, index=True)
    snk_warehouse_id = fields.Many2one('stock.warehouse', string='Source Warehouse')
    snk_warehouse_dest_id = fields.Many2one('stock.warehouse', string='Destination Warehouse')
    warehouse_address_id = fields.Many2one('res.partner', default=False, related='snk_site_id.partner_id')
    # partner_id = fields.Many2one(
    #     'res.partner', 'Contact',
    #     check_company=True, related='snk_site_address_id.partner_id', index='btree_not_null')
    # Transaction snapshots: these values must not change when the flags on the
    # Operation Type are edited after the picking has been created.
    snk_is_mutation = fields.Boolean("Is Mutation", default=False, store=True, index=True)
    snk_is_mrp = fields.Boolean("Is MRP", default=False, store=True, index=True)
    snk_mutation_id = fields.Many2one('stock.picking', string="Mutation(Receipt)", copy=False, store=True, index=True)
    snk_source_status = fields.Selection(related='snk_site_id.snk_pos_status', string="Source Site POS Status", store=True)
    snk_dest_status = fields.Selection(related='snk_site_address_id.snk_pos_status', string="Destination Site POS Status", store=True)
    snk_approve = fields.Text("Approver")
    snk_bill_ref = fields.Char("Carrier Bill Reference")
    delay_lead_time = fields.Integer(string='Delay Lead Time', help="Additional lead time for deliveries to this picking in days.")
    expected_arrival_time = fields.Integer(string='Expected Arrival Time', help="Expected arrival time for deliveries to this picking in days.")

    @api.model
    def _get_picking_type_snapshot_values(self, picking_type):
        """Return the transaction classification owned by an Operation Type."""
        return {
            'snk_is_mutation': bool(picking_type.snk_is_mutation),
            'snk_is_mrp': bool(picking_type.snk_is_mrp),
        }

    @api.onchange('picking_type_id')
    def _onchange_picking_type_snapshot(self):
        """Preview the snapshot immediately; create/write enforce persistence."""
        for picking in self:
            picking.update(
                picking._get_picking_type_snapshot_values(picking.picking_type_id)
            )

    def write(self, vals):
        _logger.debug(">>>>> [write] picking: %s, vals: %s", self.mapped('name'), vals)
        if 'picking_type_id' in vals:
            picking_type = self.env['stock.picking.type'].browse(
                vals['picking_type_id']
            ) if vals['picking_type_id'] else self.env['stock.picking.type']
            # A deliberate Operation Type change represents a new transaction
            # classification, so its flags are authoritative for the snapshot.
            vals = dict(vals)
            vals.update(self._get_picking_type_snapshot_values(picking_type))
        result = super(Picking, self).write(vals)

        # Sync origin to moves if changed
        if 'origin' in vals:
            for picking in self:
                picking.move_ids.write({'origin': picking.origin})
        
        return result

    @api.depends('sale_id')
    def _compute_snk_site_id(self):
        for rec in self:
            if rec.snk_site_id:
                continue

            if rec.sale_id:
                """ Dari sale order akan isi site pengirim dan penerima tanpa trigger onchange lainnya
                picking type tetap ambil default sale order
                """
                sender = self.env['snk.site'].search([
                    ('warehouse_id', '=', rec.sale_id.warehouse_id.id)
                ], limit=1)
                rec.snk_site_id = sender
                rec.warehouse_address_id = rec.snk_site_id.partner_id
                # rec.snk_site_address_id = rec.sale_id.snk_site_id
                # rec._onchange_snk_site_id()
    
    # @api.depends('snk_site_address_id')
    # def _compute_snk_site_address_id(self):
    #     # _logger.info(">>>>> Masuk nih woi")
    #     for rec in self:
    #         if rec.snk_site_address_id:
    #             rec.partner_id = rec.snk_site_address_id.partner_id

    @api.onchange('snk_site_id')
    def _onchange_snk_site_id(self):
        for rec in self:
            if rec.snk_site_id and not rec.sale_id:
                _logger.debug(">>>>> snk_site_id changed: %s", rec.snk_site_id.snk_code)
                rec.warehouse_address_id = rec.snk_site_id.partner_id
                picking_type = rec._get_picking_type(rec.company_id.id, rec.picking_type_code, rec.snk_site_id, rec.snk_site_id.warehouse_id)
                if picking_type:
                    rec.picking_type_id = picking_type
                    # _logger.info(">>>>> picking_type: %s", rec.picking_type_id)
                else:
                    rec.picking_type_id = rec._get_picking_type(rec.company_id.id, rec.picking_type_code, rec.snk_site_id.warehouse_id)
                _logger.debug(">>>>> picking_type: %s", rec.picking_type_id.display_name)
                _logger.debug(">>>>> location_id: %s", rec.picking_type_id.default_location_src_id.display_name)
                _logger.debug(">>>>> location_dest_id: %s", rec.picking_type_id.default_location_dest_id.display_name)
            elif rec.sale_id:
                _logger.debug(">>>>> snk_site_address_id: %s", rec.snk_site_address_id)
                continue

    # @api.onchange('picking_type_code')
    # def _onchange_snk_picking_type_code(self):
    #     for rec in self:
    #         rec.picking_type_id = rec._get_picking_type(rec.company_id.id, rec.picking_type_code, rec.snk_site_id, rec.snk_site_id.warehouse_id)

    @api.onchange('snk_warehouse_id')
    def _onchange_snk_warehouse_id(self):
        for rec in self:
            if rec.snk_warehouse_id:
                rec.location_id = rec.snk_warehouse_id.lot_stock_id

    @api.onchange('snk_warehouse_dest_id')
    def _onchange_snk_warehouse_dest_id(self):
        for rec in self:
            if rec.snk_warehouse_dest_id:
                rec.location_dest_id = rec.snk_warehouse_dest_id.lot_stock_id

    @api.onchange('location_id')
    def _onchange_snk_location_id(self):
        for rec in self:
            warehouse = self.env['stock.warehouse'].search([
                ('lot_stock_id', 'parent_of', rec.location_id.id)
            ], limit=1)
            rec.snk_warehouse_id = warehouse or False

    @api.onchange('location_dest_id')
    def _onchange_snk_location_dest_id(self):
        for rec in self:
            warehouse = self.env['stock.warehouse'].search([
                ('lot_stock_id', 'parent_of', rec.location_dest_id.id)
            ], limit=1)
            rec.snk_warehouse_dest_id = warehouse or False

    # @api.onchange('warehouse_address_id')
    # def _onchange_snk_warehouse_address_id(self):
    #     for rec in self:
    #         if rec.warehouse_address_id:
    #             site = self.env['snk.site'].search([('partner_id', '=', rec.warehouse_address_id.id)])
    #             if site:
    #                 rec.snk_site_id = site[:1]
    #             else:
    #                 rec.snk_site_id = False
    
    @api.onchange('snk_site_address_id')
    def _onchange_snk_site_address_id(self):
        for rec in self:
            if rec.snk_site_address_id:
                rec.partner_id = rec.snk_site_address_id.partner_id
                _logger.debug(">>>>> compute partner_id dari site address: %s", rec.partner_id)

    @api.onchange('picking_type_id')
    def _onchange_picking_type_id_force_location(self):
        for rec in self:
            if self.picking_type_id:
                self.location_id = self.picking_type_id.default_location_src_id
                self.location_dest_id = self.picking_type_id.default_location_dest_id
                _logger.debug(">>>>> location_id: %s", rec.picking_type_id.default_location_src_id.display_name)
                _logger.debug(">>>>> location_dest_id: %s", rec.picking_type_id.default_location_dest_id.display_name)

    # @api.onchange('partner_id')
    # def _onchange_snk_partner_id(self):
    #     for rec in self:
    #         if rec.partner_id:
    #             site = self.env['snk.site'].search([('partner_id', '=', rec.partner_id.id)])
    #             if site:
    #                 rec.snk_site_address_id = site[:1]
    #             else:
    #                 rec.snk_site_address_id = False

    @api.model
    def _get_picking_type(self, company_id, picking_type_code, site=None, warehouse=None):
        """Return picking type based on company, code, site, and warehouse
        with safe fallback logic.
        """
        _logger.debug(">>>>> _get_picking_type called with company_id: %s, picking_type_code: %s, site: %s, warehouse: %s", company_id, picking_type_code, site.snk_code if site else None, warehouse.code if warehouse else None)
        PickingType = self.env['stock.picking.type']

        # Ensure warehouse not None
        if not warehouse:
            warehouse = self.env['stock.warehouse'].search([
                ('company_id', '=', company_id)
            ], limit=1)

        # Case A — When site exists and sale_id empty → use site-specific type
        if site and not self.sale_id:
            _logger.debug(">>>>> Ada nih sitenya bro, sekarang cari picking type dengan type of operation: %s, site: %s, warehouse: %s", picking_type_code, site.snk_code, warehouse.code)
            picking_type = PickingType.search([
                ('code', '=', picking_type_code),
                ('site_id', '=', site.id),
                ('warehouse_id', '=', warehouse.id)
            ], limit=1)

            if picking_type:
                _logger.debug(">>>>> Ketemu nih picking type: %s", picking_type.display_name)
                return picking_type
        elif not site:
            # Case B — Normal, warehouse-based picking type
            picking_type = PickingType.search([
                ('code', '=', picking_type_code),
                ('company_id', '=', company_id),
                ('warehouse_id', '=', warehouse.id)
            ], limit=1)

            if picking_type:
                return picking_type.id
        else:
            # Case C — Fallback: pick any matching code within company
            picking_type = PickingType.search([
                ('code', '=', picking_type_code),
                ('company_id', '=', company_id),
            ], limit=1)

            return picking_type.id if picking_type else False
        if site and not self.sale_id and not self.snk_is_pos:
            picking_type = self.env['stock.picking.type'].search(
                [('code', '=', picking_type_code), ('site_id', '=', site.id), ('warehouse_id', '=', warehouse.id)])
            _logger.debug(">>>>> Ini gak dapet woi: %s", picking_type.display_name)
            if picking_type:
                return picking_type[:1]
            else:
                picking_types = self.env['stock.picking.type'].search([
                    ('code', '=', picking_type_code),
                    ('company_id', '=', company_id),
                    ('warehouse_id', '=', self.snk_site_id.warehouse_id.id)
                ])
                _logger.debug(">>>>> Ini dapet woi: %s", picking_type.display_name)
                return picking_types[:1].id
        else:
            if picking_type_code:
                picking_types = self.env['stock.picking.type'].search([
                    ('code', '=', picking_type_code),
                    ('company_id', '=', company_id),
                    ('warehouse_id', '=', warehouse.id)
                ])
                return picking_types[:1]
            

    @api.depends('sale_id')
    def _compute_city_id(self):
        for picking in self:
            if picking.city_id or not picking.sale_id:
                continue

            picking.city_id = picking.sale_id.city_id or False

    @api.depends('carrier_id', 'city_id', 'price_rule_id',
                 'price_rule_id.list_base_price', 'price_rule_id.list_price')
    def _compute_shipping_cost(self):
        """Hitung TARIF SATUAN shipping dari price rule yang match (bukan total).

        delivery_cost_price = list_base_price + list_price  → tarif per satuan
        variable_factor (mis. per kg). Nilai TOTAL-nya ada di field terpisah
        `delivery_price` ("Total Shipping Cost") = tarif x weight.

        Rumus ini disamakan dengan sale.order._compute_shipping_cost supaya angka
        "Shipping Cost" di SO dan di picking turunannya konsisten. Sebelumnya
        picking memakai snk_rate_shipment() yang mengembalikan TOTAL (base +
        price * weight), sehingga SO menampilkan 1.100 tapi picking-nya 3.300
        untuk price rule & weight yang sama.
        """
        for picking in self:
            # Data historis (done/cancel) tidak dihitung ulang: shipping cost sudah final.
            # Assign ulang nilai lama secara EKSPLISIT (bukan `continue` kosong): pada compute
            # store, record yang tak di-assign akan di-fallback ke 0 oleh ORM, jadi kita harus
            # menahannya agar angka historis & laporan tidak ikut berubah bila master data
            # shipping (price rule) diubah belakangan.
            # if picking.state in ('done', 'cancel'):
            #     picking.delivery_cost_price = picking.delivery_cost_price
            #     continue
            # Belum cukup data untuk menghitung → biarkan 0, jangan paksa error.
            if not picking.carrier_id or not picking.city_id:
                picking.delivery_cost_price = 0.0
                continue
            if picking.delivery_type not in ('fixed', 'base_on_rule'):
                picking.delivery_cost_price = 0.0
                continue

            rule = picking.price_rule_id
            picking.delivery_cost_price = (
                rule.list_base_price + rule.list_price
            ) if rule else 0.0

    # @api.depends('carrier_id', 'city_id')
    def _compute_deliver_price_rule(self):
        # Chunking manual untuk memproses data dengan aman
        for i in range(0, len(self), 100):
            batch = self[i:i+100]
            # Batasi prefetch hanya untuk 100 record per iterasi
            batch_sudo = batch.sudo().with_prefetch(batch.ids)
            
            for picking, picking_sudo in zip(batch, batch_sudo):
                # Jika tidak ada ekspedisi, pasti tidak ada price rule
                if not picking_sudo.carrier_id:
                    picking.price_rule_id = False
                    continue

                # Picking done/cancel: pertahankan price_rule yang sudah tersimpan supaya
                # angka shipping historis & laporan tidak bergeser saat master data price
                # rule diubah belakangan. Untuk memperbaiki picking lama yang salah, pakai
                # wizard recompute manual (action_recompute_shipping_price) yang dipanggil eksplisit.
                if picking_sudo.state in ('done', 'cancel') and picking_sudo.price_rule_id:
                    picking.price_rule_id = picking_sudo.price_rule_id
                    continue

                price_rule_id = False
                total = weight = volume = quantity = wv = 0
                total_delivery = 0.0
                
                city = picking_sudo.city_id.id if picking_sudo.city_id else False
                weight = picking_sudo.weight or 0.0
                volume = picking_sudo.shipping_volume or 0.0
                quantity = sum(move.quantity for move in picking_sudo.move_ids if move.state != 'cancel')
                wv = weight * volume * quantity

                total = 0
                # weight is either,
                # 1- weight chosen by user in choose.delivery.carrier wizard passed by context
                # 2- saved weight to use on sale order
                # 3- total order line weight as fallback
                weight = self.env.context.get('order_weight') or picking_sudo.shipping_weight or weight

                price_dict = picking_sudo.carrier_id._get_price_dict(total, weight, volume, quantity, wv=wv, city=city)
                for line in picking_sudo.carrier_id.price_rule_ids:
                    max_value = line.max_value
                    if line.variable == 'city':
                        max_value = line.city_id.id
                    
                    try:
                        test = safe_eval(line.variable + line.operator + str(max_value), price_dict)
                    except Exception:
                        test = False
                        
                    if test:
                        price_rule_id = line
                        break

                picking.price_rule_id = price_rule_id

    def _set_delivery_price_rule(self):
        for picking in self.sudo():
            price_rule_id = False

            if not picking.carrier_id:
                picking.price_rule_id = False
                continue

            city = picking.city_id.id if picking.city_id else False
            weight = picking.shipping_weight or picking.weight or 0.0
            volume = picking.shipping_volume or 0.0
            quantity = sum(move.quantity for move in picking.move_ids if move.state != 'cancel')
            wv = weight * volume * quantity

            price_dict = picking.carrier_id._get_price_dict(
                0, weight, volume, quantity, wv=wv, city=city
            )

            for line in picking.carrier_id.price_rule_ids:
                max_value = line.city_id.id if line.variable == 'city' else line.max_value
                try:
                    test = safe_eval(line.variable + line.operator + str(max_value), price_dict)
                except Exception:
                    test = False

                if test:
                    price_rule_id = line
                    break

            picking.price_rule_id = price_rule_id

    @api.onchange('carrier_id', 'shipping_weight', 'city_id')
    def _onchange_carrier_id(self):
        self._set_delivery_price_rule()

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

    def action_create_shipping_bills(self):
        ##### Gak perlu, user gudang perlu buat shipping bill sendiri
        # if not (self.env.user.has_group('purchase.group_purchase_manager') or self.env.user.has_group('account.group_account_invoice')):
        #     raise UserError(_("Only Purchase Administrators or Accounting Billing users can create shipping bills."))

        carrier_id = self.mapped('carrier_id')
        total_amount = sum(self.mapped('delivery_price'))
        if len(carrier_id) > 1:
            raise UserError(_("you can’t create shipping bills with different carrier"))

        return {
            'type': 'ir.actions.act_window',
            'name': _("Create Shipping Bills"),
            'res_model': 'snk.inventory.delivery.bill.wizard',
            'target': 'new',
            'views': [(self.env.ref('mesa.snk_stock_inventory_delivery_bill_wizard_form').id, 'form')],
            'context': {
                'default_snk_carrier_id': carrier_id.id,
                'default_snk_amount': total_amount
            },
        }

    @api.depends('sale_id')
    def _compute_analytic_ids(self):
        for rec in self:
            if rec.sale_id:
                rec.analytic_ids = rec.sale_id.analytic_account_ids

    def _inverse_analytic_ids(self):
        # Allow manual setting of analytic_ids without triggering compute
        pass

    @api.depends('sale_id')
    def _compute_event_ids(self):
        for rec in self:
            if rec.event_ids:
                continue

            if rec.sale_id:
                rec.event_ids = rec.sale_id.event_ids
            else:
                rec.event_ids = False

    # @api.onchange('carrier_id', 'shipping_weight', 'city_id')
    # def _onchange_carrier_id(self):
    #     if self.delivery_type in ('fixed', 'base_on_rule'):
    #         vals = self._get_delivery_rate()
    #         if vals.get('error_message'):
    #             self.delivery_message = vals.get('error_message', False)
    #             self.display_price = 0
    #             self.delivery_price = 0
    #             return {'error': vals['error_message']}
    #     else:
    #         self.display_price = 0
    #         self.delivery_price = 0

    def _get_delivery_rate(self):
        vals = self.carrier_id.with_context(order_weight=self.shipping_weight).snk_rate_shipment(self)

        if vals.get('success'):
            self.delivery_message = vals.get('warning_message', False)
            self.delivery_price = vals['price']
            self.display_price = vals['carrier_price']
            return {'no_rate': vals.get('no_rate', False)}
        return {'error_message': vals['error_message']}

    def action_open_shipping_bill(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window',
                'name': _('Shipping Bill'),
                'res_model': 'account.move',
                'view_mode': 'list,form',
                'domain': [('id', '=', self.snk_bill_id.id)],
                'target': 'current',
                }
    
    def _validation_pos_status(self):
        Config = self.env['ir.config_parameter'].sudo()
        active_integration = Config.get_param('mesa.snk_active_integration')
        normal_pickings = self.env['stock.picking']
        adjustment_pickings = self.env['stock.picking']

        for picking in self:
            # Jika tidak terintegrasi → log dan lanjut
            if not (active_integration and picking.picking_type_id.snk_is_integration and not picking.snk_is_pos):
                _logger.debug(
                    ">>>>> Picking %s tidak terintegrasi. Site %s pos status %s.",
                    picking.name,
                    picking.snk_site_id.snk_code if picking.snk_site_id else False,
                    picking.snk_site_id.snk_pos_status if picking.snk_site_id else False,
                )
                continue

            code = picking.picking_type_id.code   # outgoing / incoming
            site_status = picking.snk_site_id.snk_pos_status
            addr = picking.snk_site_address_id
            addr_status = addr.snk_pos_status if addr else None

            # ==========================================================
            # 🔵 POV OUTGOING
            # ==========================================================
            if code == "outgoing":
                # Site POS aktif + address ada = blokir
                if site_status == 'active' and addr and not picking.snk_is_pos:
                    _logger.debug(">>>>> Validasi outgoing dari active POS (site+address).")
                    raise ValidationError(
                        "You cannot process delivery for active POS. Please contact the responsible partner to validate this delivery."
                    )

                # Site inactive/gudang + address active = generate XML normal
                if site_status in ('inactive', 'gudang') and addr_status == 'active' and not picking.snk_is_pos:
                    _logger.debug(">>>>> Generating XML outgoing: %s", picking.name)
                    normal_pickings |= picking
                    continue
                
                # Delivery terbentuk dari inbound = tidak generate xml
                if site_status == 'active' and addr and picking.snk_is_pos:
                    _logger.debug(">>>>> Delivery %s dari POS: %s", picking.name, picking.snk_pos_ref)
                    # picking.generate_xml()
                    continue

                # Site active dan tidak ada address = adjustment
                if site_status == 'active' and not addr and not picking.snk_is_pos:
                    _logger.debug(">>>>> Generating XML outgoing adjustment: %s", picking.name)
                    adjustment_pickings |= picking
                    continue

                # Keduanya tidak active → hanya log
                if site_status != 'active' and addr_status != 'active':
                    _logger.debug(">>>>> Outgoing: Site receiver pos status: %s", addr_status)
                    continue

            # ==========================================================
            # 🔴 POV INCOMING
            # ==========================================================
            elif code == "incoming":

                # Site POS aktif + address ada = blokir
                if site_status == 'active' and addr:
                    _logger.debug(">>>>> Validasi incoming dari active POS (site+address).")
                    raise ValidationError(
                        "You cannot process receipt for active POS. Please contact the responsible partner to validate this receipt."
                    )

                # Site inactive/gudang + address active = generate XML normal
                # if site_status in ('inactive', 'gudang') and addr_status == 'active':
                #     _logger.info(">>>>> Generating XML incoming: %s", picking.name)
                #     picking.generate_xml()
                #     continue

                # Site active dan tidak ada address = adjustment
                if site_status == 'active' and not addr and not picking.snk_is_pos:
                    _logger.debug(">>>>> Generating XML incoming adjustment: %s", picking.name)
                    adjustment_pickings |= picking
                    continue

                # Keduanya tidak active → hanya log
                # if site_status != 'active' and addr_status != 'active':
                else:
                    _logger.debug(">>>>> Incoming: Site receiver pos status: %s", addr_status)
                    continue

        # Validate the complete selection before generating anything. If one or
        # more pickings already have outbound records, the helper raises one
        # consolidated UserError and neither generation flow is executed.
        pickings_to_generate = normal_pickings | adjustment_pickings
        pickings_to_generate._raise_if_existing_transfer_outbounds()

        if normal_pickings:
            normal_pickings.generate_xml()
        if adjustment_pickings:
            adjustment_pickings.generate_xml_adjustment()


    @api.model_create_multi
    def create(self, vals_list):
        # res = super().create()
        for vals in vals_list:
            if vals.get('purchase_id'):
                _logger.debug(">>>>> Masuk nih cek picking import dari purchase nih bro: %s", vals.get('purchase_id'))
                purchase = self.env['purchase.order'].browse(vals.get('purchase_id'))
                vals['snk_site_id'] = purchase.snk_site_id.id
                vals['origin'] = purchase.origin if purchase.origin else purchase.name
                vals['analytic_ids'] = purchase.analytic_account_ids.ids if purchase.analytic_account_ids else False
                _logger.debug(">>>>> Picking receiver: %s in picking values", purchase.snk_site_id.snk_name)
                _logger.debug(">>>>> Picking origin: %s in picking values", purchase.origin)
                _logger.debug(">>>>> Picking analytic_ids: %s in picking values", purchase.analytic_account_ids.ids)
            if vals.get('snk_is_pos'):
                # return super(Picking, self).create(vals_list)
                continue
            if vals.get('snk_site_id') and not vals.get('picking_type_id'):
                _logger.debug(">>>>> Masuk cek picking import nih bro: %s", vals.get('snk_site_id'))
                picking_type_code = self.env.context.get('restricted_picking_type_code')
                picking_type = self.env['stock.picking']
                type = picking_type._get_picking_type(self.env.company.id, picking_type_code, self.env['snk.site'].browse(vals.get('snk_site_id')), self.env['snk.site'].browse(vals.get('snk_site_id')).warehouse_id)
                vals['picking_type_id'] = type.id
                _logger.debug(">>>>> picking_type dari import: %s", type.display_name)
            if vals.get('snk_site_address_id'):
                partner = self.env['snk.site'].browse(vals.get('snk_site_address_id')).partner_id
                vals['partner_id'] = partner.id
            if not vals.get('snk_create_date'):
                vals['snk_create_date'] = fields.Datetime.now()
            if vals.get('scheduled_date'):
                vals['scheduled_date'] = vals.get('scheduled_date')

            if vals.get('picking_type_id'):
                picking_type = self.env['stock.picking.type'].browse(vals['picking_type_id'])
                # Do not use setdefault(): clients may send the readonly Boolean
                # fields as False, but the selected Operation Type owns the
                # transaction classification at creation time.
                vals.update(self._get_picking_type_snapshot_values(picking_type))

        pickings = super(Picking, self).create(vals_list)

        # Some flows let stock.picking apply its default Operation Type inside
        # super().create(). Snapshot from the actual created record in that case.
        for picking in pickings:
            snapshot_vals = self._get_picking_type_snapshot_values(
                picking.picking_type_id
            )
            changed_snapshot_vals = {
                field_name: value
                for field_name, value in snapshot_vals.items()
                if picking[field_name] != value
            }
            if changed_snapshot_vals:
                picking.write(changed_snapshot_vals)

        return pickings

    def create_incoming_for_mutation(self):
        for picking in self:
            if not picking.snk_is_mrp:
                _logger.debug(">>>>> Bukan MRP nih bro")
                next_receipt = [
                    ('code', '=', 'incoming'),
                    ('site_id', '=', picking.snk_site_address_id.id),
                    ('warehouse_id', '=', picking.snk_site_address_id.warehouse_id.id),
                    ('snk_is_mutation', "=", True),
                    ('snk_is_mrp', "=", False),
                    ('snk_mutation_code', "=", picking.picking_type_id.snk_mutation_code)
                ]
            elif picking.snk_is_mrp:
                _logger.debug(">>>>> Ini MRP nih bro")
                next_receipt = [
                    ('code', '=', 'incoming'),
                    ('site_id', '=', picking.snk_site_address_id.id),
                    ('warehouse_id', '=', picking.snk_site_address_id.warehouse_id.id),
                    ('snk_is_mutation', "=", True),
                    ('snk_is_mrp', "=", True),
                    ('snk_mutation_code', "=", picking.picking_type_id.snk_mutation_code)
                ]
                _logger.debug(">>>>> MRP document: %s, line number: %s.", picking.transfer_id.name, len(picking.transfer_line_ids))

            receipt_type = self.env['stock.picking.type'].search(next_receipt, limit=1)
            _logger.debug(">>>>> Next_receipt: %s, receipt_type: %s", next_receipt, receipt_type.display_name)
            if receipt_type:
                # delivery_lead_time = picking.snk_site_address_id.snk_lead_time or 0
                delay_lead_time = picking.snk_site_address_id.delay_lead_time or 0
                expected_arrival_time = picking.snk_site_address_id.expected_arrival_time or 0
                destination_tz = (
                    picking.snk_site_address_id.partner_id.tz
                    or picking.company_id.partner_id.tz
                    or self.env.user.tz
                    or 'UTC'
                )
                eta = picking.scheduled_date + timedelta(days=delay_lead_time + expected_arrival_time)
                # Datetimes are stored as UTC, but receiving-day availability
                # must follow the destination site's local calendar day.
                while fields.Datetime.context_timestamp(
                    picking.with_context(tz=destination_tz), eta
                ).weekday() >= 5:
                    eta += timedelta(days=1)
                _logger.debug(">>>>> Scheduled_date: %s, delay_lead_time: %s, expected_arrival_time: %s, eta: %s", picking.scheduled_date, delay_lead_time, expected_arrival_time, eta)
                # picking.show_next_pickings = True
                new_picking_vals = {
                    'picking_type_id': receipt_type.id,
                    'snk_site_address_id': picking.snk_site_id.id,
                    'partner_id': picking.snk_site_id.partner_id.id,
                    'snk_site_id': picking.snk_site_address_id.id,
                    'warehouse_address_id': picking.snk_site_address_id.partner_id.id,
                    'snk_create_date': eta,
                    'origin': picking.origin,
                    'snk_warehouse_dest_id': picking.snk_site_address_id.warehouse_id.id,
                    # 'location_dest_id': receipt_type.warehouse_id.lot_stock_id.id,
                    'analytic_ids': [(6, 0, picking.analytic_ids.ids)],
                    'snk_pos_ref': picking.name if not picking.snk_pos_ref else picking.snk_pos_ref,
                    'transfer_id': picking.transfer_id.id if picking.transfer_id else False,
                    'transfer_line_ids': [(6, 0, picking.transfer_line_ids.ids)] if picking.transfer_line_ids else False,
                    'shipping_weight': picking.shipping_weight,
                    'delay_lead_time': delay_lead_time,
                    'expected_arrival_time': expected_arrival_time,
                }
                new_picking = self.env['stock.picking'].create(new_picking_vals)
                new_picking._onchange_picking_type_id_force_location()
                picking.snk_mutation_id = new_picking.id 
                # new_picking.write({})
                
                # picking.move_ids.move_dest_ids.picking_id = new_picking.id
                
                _logger.debug(">>>>> Type: %s, Site: %s, Operation: %s, Address: %s, show_next_pickings: %s, ETA: %s",
                    receipt_type.code, new_picking.snk_site_id.snk_code, 
                    new_picking.picking_type_id.display_name, new_picking.snk_site_address_id.snk_code, picking.show_next_pickings, new_picking.scheduled_date)
                # Iterasi di level MOVE (bukan move line) supaya chain move_orig_ids
                # tetap terbentuk untuk picking POS yang qty-nya di-force langsung
                # (move POS standalone, move line bisa tidak ter-materialize).
                for move in picking.move_ids_without_package:
                    if move.state == 'cancel':
                        continue
                    source_qty = move.quantity or move.product_uom_qty
                    if not source_qty:
                        continue
                    new_move_vals = {
                        'picking_id': new_picking.id,
                        'product_id': move.product_id.id,
                        'snk_grade_id': move.snk_grade_id.id or False,
                        'name': move.product_id.display_name,
                        'product_uom': move.product_uom.id,
                        'product_uom_qty': source_qty,
                        'quantity': source_qty,
                        'location_id': receipt_type.default_location_src_id.id,
                        'location_dest_id': receipt_type.default_location_dest_id.id,
                        'date': eta,
                        'date_deadline': eta,
                        'origin': picking.origin,

                        # chain ke move lama → format many2many command
                        'move_orig_ids': [(6, 0, [move.id])],
                    }
                    new_move = self.env['stock.move'].create(new_move_vals)
                    if new_move:
                        move.write({'move_dest_ids': [(4, new_move.id)]})
                        new_picking.scheduled_date = eta
                new_picking.action_assign()
            else:
                raise UserError(_(f"No receipt for this mutation found for {picking.snk_site_address_id.full_name}"))
        # for move in self.move_ids_without_package:
        #     qty_done = move.quantity_done  # ini qty real yg tervalidate
        
        #     if qty_done <= 0:
        #         continue

    # def action_confirm(self):
    #     res = super().action_confirm()
    #     for picking in self:
    #         _logger.info(">>>>> Action confirm picking: %s", picking.name)
    #         picking._validation_pos_status()
    #     return res

    def _recompute_delivery_rate(self):
        """Hitung ulang & simpan delivery_price/display_price sebelum validate.

        delivery_price & display_price adalah field non-store yang biasanya hanya
        terisi lewat onchange di form. Saat validate onchange tidak jalan, sehingga
        rate bisa balik ke 0. Method ini memaksa recompute + write agar rate tetap
        tersimpan setelah picking Done.
        """
        for picking in self:
            if not picking.carrier_id:
                continue
            picking._set_delivery_price_rule()
            if picking.delivery_type in ('fixed', 'base_on_rule'):
                vals = picking.carrier_id.with_context(
                    order_weight=picking.shipping_weight
                ).snk_rate_shipment(picking)
                if vals.get('success'):
                    picking.write({
                        'delivery_message': vals.get('warning_message', False),
                        'delivery_price': vals['price'],
                        'display_price': vals['carrier_price'],
                    })

    def button_validate(self):
        # Simpan rate SEBELUM state jadi done (onchange tidak jalan saat validate)
        self._recompute_delivery_rate()
        res = super(Picking, self.sudo()).button_validate() # sudo() agar bisa baca standard_price saat buat SVL
        validated_pickings = self.env['stock.picking']
        for picking in self:
            if picking.picking_type_id.code == "outgoing" and picking.snk_is_mutation:
                _logger.debug(">>>>> Validasi... code: %s, site_id: %s, warehouse_id, %s, mutation: %s",
                    picking.picking_type_code, picking.snk_site_address_id.snk_code,
                    picking.snk_site_address_id.warehouse_id.code, picking.snk_is_mutation)
                
            # Hanya proses AFTER validated
            if picking.state != "done":
                continue
            else:
                validated_pickings |= picking
                if picking.picking_type_id.code == "outgoing" and picking.snk_is_mutation:
                    _logger.debug(">>>>> Ini termasuk muatsi bro, akan buat receiptnya ya")
                    picking.create_incoming_for_mutation()
                else:
                    _logger.debug(">>>>> Bukan mutation bro, gak usah buat receipt")
            for vals in picking.move_ids_without_package:
                try:
                    if picking and picking.scheduled_date:
                        vals['date'] = picking.scheduled_date
                        _logger.debug(f"Picking scheduled_date {picking.scheduled_date} in picking")
                        _logger.debug(f"Picking scheduled_date {vals['date']} in move values")
                    if picking and picking.date_deadline:
                        vals['date_deadline'] = picking.date_deadline
                        _logger.debug(f"Picking date_deadline {picking.date_deadline} in picking")
                        _logger.debug(f"Picking date_deadline {vals['date_deadline']} in move values")
                except Exception as e:
                    _logger.error(f"Error updating move dates: {e}")
        validated_pickings._validation_pos_status()
        return res
    
    def action_open_mutation_receipt(self):
        self.ensure_one()
        if self.snk_mutation_id:
            return {
                'name': 'Mutation Receipt',
                'type': 'ir.actions.act_window',
                'res_model': 'stock.picking',
                'view_mode': 'form',
                'res_id': self.snk_mutation_id.id,
            }

    def action_print_product_labels(self):
        """Open product label wizard for this picking"""
        self.ensure_one()

        # Get unique products from moves
        product_ids = self.move_ids_without_package.mapped('product_id').ids

        # Open the label wizard
        return {
            'type': 'ir.actions.act_window',
            'name': _('Print Labels'),
            'res_model': 'product.label.layout',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'stock.picking',
                'active_ids': [self.id],
                'default_product_ids': [(6, 0, product_ids)],
            },
        }

    # -------------------------------------------------------------------------
    # CATALOG
    # -------------------------------------------------------------------------

    def action_add_from_catalog(self):
        res = super().action_add_from_catalog()
        return res

    def _default_order_line_values(self, child_field=False):
        default_data = super()._default_order_line_values(child_field)
        new_default_data = self.env['stock.move']._get_product_catalog_lines_data(parent_record=self)

        return {**default_data, **new_default_data}

    # def _get_product_catalog_domain(self):
    #     return expression.AND([super()._get_product_catalog_domain(), [('analytic_id', 'in', self.analytic_ids)]])
    
    def _get_product_catalog_domain(self):
        domain = super()._get_product_catalog_domain()
        # Selalu filter purchase_ok yessssssssssss
        domain = expression.AND([domain, [('purchase_ok', '=', True)]])
        
        # Kalau ada brand yang dipilih, filter berdasarkan analytic_id okeee gasssssssssssssssssssssss
        brand_ids = self.analytic_ids.ids
        if brand_ids:
            domain = expression.AND([domain, [('analytic_id', 'in', brand_ids)]])
        
        return domain

    def _get_product_catalog_order_data(self, products, **kwargs):
        product_catalog = super()._get_product_catalog_order_data(products, **kwargs)
        for product in products:
            product_catalog[product.id] |= self._get_product_price_and_data(product)
        return product_catalog

    def action_print_daily_sto_excel(self):
        if not xlsxwriter:
            raise UserError(_("Modul 'xlsxwriter' belum terinstall di server."))

        output = io.BytesIO()
        workbook = xlsxwriter.Workbook(output, {'in_memory': True})
        sheet = workbook.add_worksheet('Sheet1')

        # Formats
        bold_format = workbook.add_format({'bold': True, 'border': 1, 'bg_color': '#E0E0E0', 'align': 'center', 'valign': 'vcenter', 'text_wrap': True})
        border_format = workbook.add_format({'border': 1, 'valign': 'vcenter'})
        datetime_format = workbook.add_format({'border': 1, 'num_format': 'dd/mm/yyyy', 'valign': 'vcenter'})
        
        # Headers
        headers = [
            "Site", "Nama Counter", "Brand", "No. STO", "Tanggal STO",
            "Delivery Date", "Order Qty", "Quantity", "Remark Text",
            "Tanggal Terima Order",
            "Tanggal Naik Order",
            "Tanggal Turun Order",
            "Keterangan"
        ]
        
        sheet.set_row(0, 30)
        for col, head in enumerate(headers):
            sheet.write(0, col, head, bold_format)
            sheet.set_column(col, col, max(len(head.split('\n')[0]) + 2, 12))

        # Data rows
        row = 1
        for picking in self:
            site_code = picking.snk_site_address_id.snk_code if picking.snk_site_address_id else ''
            site_name = picking.snk_site_address_id.full_name if picking.snk_site_address_id else ''
            brand_names = picking.analytic_ids.mapped('name') if picking.analytic_ids else []
            brand = ', '.join(brand_names)
            picking_name = picking.name or ''
            
            user_tz = self.env.user.tz or 'UTC'

            sto_date = fields.Datetime.context_timestamp(self, picking.snk_create_date).replace(tzinfo=None) if picking.snk_create_date else False
            scheduled_date = fields.Datetime.context_timestamp(self, picking.scheduled_date).replace(tzinfo=None) if picking.scheduled_date else False
            
            order_qty = sum(move.product_uom_qty for move in picking.move_ids_without_package)
            # Demand = product_uom_qty (direncanakan); Quantity = quantity (aktual/done)
            done_qty = sum(move.quantity for move in picking.move_ids_without_package)

            remark_text = picking.origin or ''
            
            sheet.write(row, 0, site_code, border_format)
            sheet.write(row, 1, site_name, border_format)
            sheet.write(row, 2, brand, border_format)
            sheet.write(row, 3, picking_name, border_format)
            
            if sto_date:
                sheet.write_datetime(row, 4, sto_date, datetime_format)
            else:
                sheet.write(row, 4, '', border_format)
                
            if scheduled_date:
                sheet.write_datetime(row, 5, scheduled_date, datetime_format)
            else:
                sheet.write(row, 5, '', border_format)
                
            sheet.write(row, 6, order_qty, border_format)
            sheet.write(row, 7, done_qty, border_format)
            sheet.write(row, 8, remark_text, border_format)
            sheet.write(row, 9, '', border_format)
            sheet.write(row, 10, '', border_format)
            sheet.write(row, 11, '', border_format)
            sheet.write(row, 12, '', border_format)
            
            row += 1

        workbook.close()
        output.seek(0)
        
        attachment = self.env['ir.attachment'].create({
            'name': 'Report_Harian_STO.xlsx',
            'type': 'binary',
            'datas': base64.b64encode(output.read()),
            'res_model': 'stock.picking',
            'res_id': self.ids[0] if self else False,
            'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
        })
        
        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'new',
        }

    def action_label_summary(self):
        """Open the Label Summary wizard showing qty & price per product for this picking."""
        self.ensure_one()
        wizard = self.env['snk.label.summary.wizard']._build_from_picking(self)
        return {
            'type': 'ir.actions.act_window',
            'name': _('Summary Label'),
            'res_model': 'snk.label.summary.wizard',
            'view_mode': 'form',
            'res_id': wizard.id,
            'target': 'new',
        }


    def _get_product_price_and_data(self, product):
        self.ensure_one()
        return {'price': product.list_price}

    def _get_product_catalog_record_lines(self, product_ids, **kwargs):
        grouped_lines = defaultdict(lambda: self.env['stock.move'])

        for line in self.move_ids_without_package:
            if line.product_id.id in product_ids:
                grouped_lines[line.product_id] |= line

        return grouped_lines

    def _update_order_line_info(self, product_id, quantity, **kwargs):
        # Simpan scheduled_date sebelum operasi untuk protect dari recompute
        original_scheduled_date = self.scheduled_date
        original_date_deadline = self.date_deadline

        move = self.move_ids_without_package.filtered(lambda e: e.product_id.id == product_id)
        if len({line.snk_grade_id.id for line in move}) > 1:
            raise UserError(_("This product has moves with different grades. Edit their quantities in Operations instead of the catalog."))
        if move:
            if quantity != 0:
                move.product_uom_qty = quantity
            else:
                move.unlink()
        elif quantity > 0:
            self.move_ids_without_package.create({
                'picking_id': self.id,
                'product_uom_qty': quantity,
                'product_id': product_id,
                'location_id': self.location_id.id,
                'location_dest_id': self.location_dest_id.id,
                'name': self.env['product.product'].browse(product_id).display_name,
                'description_picking': self.env['product.product'].browse(product_id).name,
                'date_deadline': original_date_deadline,
                'date': original_scheduled_date,
                'origin': self.origin,
            })

        # Restore scheduled_date jika ter-reset oleh recompute setelah create/update move
        if original_scheduled_date and self.scheduled_date != original_scheduled_date:
            self.scheduled_date = original_scheduled_date

        return self.env['product.product'].browse(product_id).list_price

    def action_update_source_document(self):
        for picking in self:
            if picking.origin:
                picking.move_ids_without_package.write({
                    'origin': picking.origin,
                })
                _logger.debug(">>>>> Update source document of picking %s for moves %s and move lines %s to %s", picking.name, picking.move_ids_without_package.mapped('name'), picking.move_line_ids_without_package.mapped('display_name'), picking.origin)

        
    def action_resequence(self):
        return {
            'type': 'ir.actions.act_window',
            'name': 'Resequence',
            'res_model': 'snk.picking.resequence.v2.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_ids': self.ids
            },
        }
        
    def regenerate_sequence(self):
        _logger.debug(">>>>> [START] action_resequence on %s records", len(self))
        # Batching: Grouping berdasarkan picking_type_id untuk efisiensi
        for p_type in self.mapped('picking_type_id'):
            sequence = p_type.sequence_id
            if not sequence:
                _logger.debug(">>>>> No sequence_id found for picking type: %s", p_type.name)
                continue
                
            # Filter record berdasarkan operation type (batch per operation type)
            type_pickings = self.filtered(lambda p: p.picking_type_id == p_type)
            _logger.debug(">>>>> Resequencing %s records for operation type: %s", len(type_pickings), p_type.name)
            
            for picking in type_pickings:
                new_name = sequence.next_by_id()
                picking.name = new_name
                _logger.debug(">>>>> New sequence assigned: %s for picking ID %s", new_name, picking.id)
                
        _logger.debug(">>>>> [END] action_resequence completed")

    # def action_recompute_shipping_price(self):
        # ============================================================
        # Server Action: Safe Backfill Delivery Fields
        # Model: stock.picking
        # Purpose:
        # - Update/recompute delivery_cost_price
        # - Backfill shipping_weight from weight if empty
        # - Recompute weight
        # - Recompute price_rule_id
        # ============================================================

        updated_count = 0
        skipped_count = 0
        error_count = 0
        error_messages = []

        # Safety:
        # records = selected records from list view
        pickings = self.sudo()

        for picking in pickings:
            try:
                # ------------------------------------------------------------
                # 1. Skip kalau tidak ada carrier
                # ------------------------------------------------------------
                if not picking.carrier_id:
                    skipped_count += 1
                    continue

                vals_to_write = {}

                # ------------------------------------------------------------
                # 2. Recompute weight dulu
                # ------------------------------------------------------------
                picking._cal_weight()

                # ------------------------------------------------------------
                # 3. Backfill shipping_weight secara aman
                # ------------------------------------------------------------
                # Jangan overwrite shipping_weight yang sudah ada.
                # Kalau shipping_weight kosong tapi weight ada, isi dari weight.
                if not picking.shipping_weight and picking.weight:
                    vals_to_write['shipping_weight'] = picking.weight

                if vals_to_write:
                    picking.write(vals_to_write)

                # ------------------------------------------------------------
                # 4. Recompute pricing rule
                # ------------------------------------------------------------
                # _compute_deliver_price_rule butuh carrier_id dan city_id.
                # Kalau city kosong, skip supaya tidak salah rule.
                if picking.carrier_id and picking.city_id:
                    picking._compute_deliver_price_rule()

                # ------------------------------------------------------------
                # 5. Recompute delivery_cost_price
                # ------------------------------------------------------------
                picking._compute_shipping_cost()

                updated_count += 1

            except Exception as e:
                error_count += 1
                error_messages.append("%s: %s" % (picking.name or picking.id, str(e)))
                continue

        # ------------------------------------------------------------
        # 7. Show notification
        # ------------------------------------------------------------
        message = (
            "Backfill Delivery Fields selesai. "
            "Updated: %s, Skipped no carrier: %s, Error: %s"
        ) % (updated_count, skipped_count, error_count)

        if error_messages:
            message += "\n\nSample errors:\n" + "\n".join(error_messages[:10])

        notif_type = 'warning' if error_count else 'success'

        self.env['bus.bus']._sendone(
            self.env.user.partner_id,
            'simple_notification',
            {
                'title': 'Delivery Backfill',
                'message': message,
                'type': notif_type,
                'sticky': True,
            }
        )


    def action_recompute_shipping_price(self):
        updated_count = 0
        skipped_count = 0
        error_count = 0
        error_messages = []

        pickings = self.sudo().with_context(
            skip_picking_write_log=True,
            skip_origin_sync=True,
        )

        for picking in pickings:
            try:
                if not picking.carrier_id:
                    skipped_count += 1
                    continue

                vals = {}

                # 1. Backfill shipping_weight secara aman
                shipping_weight = picking.shipping_weight or picking.weight or 0.0
                if not picking.shipping_weight and picking.weight:
                    vals['shipping_weight'] = picking.weight

                # 2. Hitung pricing rule secara lokal
                price_rule_id = False
                city = picking.city_id.id if picking.city_id else False
                volume = picking.shipping_volume or 0.0
                quantity = sum(move.quantity for move in picking.move_ids if move.state != 'cancel')
                wv = shipping_weight * volume * quantity

                price_dict = picking.carrier_id._get_price_dict(
                    0, shipping_weight, volume, quantity, wv=wv, city=city
                )

                for line in picking.carrier_id.price_rule_ids:
                    max_value = line.city_id.id if line.variable == 'city' else line.max_value
                    try:
                        test = safe_eval(line.variable + line.operator + str(max_value), price_dict)
                    except Exception:
                        test = False

                    if test:
                        price_rule_id = line
                        break

                # 3. Masukkan ke vals jika ada perubahan pada price_rule_id
                if picking.price_rule_id != price_rule_id:
                    vals['price_rule_id'] = price_rule_id.id if price_rule_id else False

                # 4. Single write call
                if vals:
                    picking.write(vals)

                updated_count += 1

            except Exception as e:
                error_count += 1
                error_messages.append("%s: %s" % (picking.name or picking.id, str(e)))
                continue

        message = (
            "Backfill Delivery Fields selesai. "
            "Updated: %s, Skipped no carrier: %s, Error: %s"
        ) % (updated_count, skipped_count, error_count)

        if error_messages:
            message += "\n\nSample errors:\n" + "\n".join(error_messages[:10])

        self.env['bus.bus']._sendone(
            self.env.user.partner_id,
            'simple_notification',
            {
                'title': 'Delivery Backfill',
                'message': message,
                'type': 'warning' if error_count else 'success',
                'sticky': False,
            }
        )

class PickingType(models.Model):
    _inherit = "stock.picking.type"

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        domain = args.copy()
        positive_operators = ['=', 'ilike', '=ilike', 'like', '=like']

        if operator in positive_operators:
            if name:
                operation_type = self.search_fetch(expression.AND([domain,[('barcode', operator, name)]]), ['display_name'], limit=limit)\
                           or self.search_fetch(expression.AND([domain,[('warehouse_id', operator, name)]]), ['display_name'], limit=limit)
                if operation_type:
                    return [(p.id, p.display_name) for p in operation_type.sudo()]

        return super().name_search(name=name, args=args, operator=operator, limit=limit)
    
# class StockReturnPicking(models.TransientModel):
#     _inherit = "stock.return.picking"

#     def _validation_pos_status(self, _return_picking=False):
#         Config = self.env['ir.config_parameter'].sudo()
#         active_integration = Config.get_param('mesa.snk_active_integration')
#         for picking in self:
#             if active_integration and picking.picking_type_id.snk_is_integration and not picking.snk_is_pos:
#                 if picking.snk_site_address_id.snk_pos_status == 'active':
#                     _logger.info(">>>>> Validasi by pos status dari Site berjalan pada return picking.")
#                     raise ValidationError(
#                         "You cannot process return for active POS. Please contact the responsible partner to validate this return.")

#     def action_create_return(self):
#         _logger.info(">>>>> action_create_return called.")
#         # for picking in self:
#             # _logger.info(">>>>> Validating return picking site pos status: %s", picking.snk_site_address_id.pos_status if picking.snk_site_address_id else False)
#         #     _return_picking = True
#         #     valid = picking._validation_pos_status(_return_picking=_return_picking)
#         #     if valid:
#         #         _logger.info(">>>>> Return picking validasi POS inactive.")
#         #         res = super().action_create_return()
#         # return res
