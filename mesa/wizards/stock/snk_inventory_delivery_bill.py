# -*- coding: utf-8 -*-
from odoo import models, fields, api, _
from odoo.exceptions import UserError, ValidationError


class SnkInventoryDeliveryBillWizard(models.TransientModel):
    _name = 'snk.inventory.delivery.bill.wizard'
    _description = 'Inventory Shipping Bill Wizard'

    snk_carrier_id = fields.Many2one('delivery.carrier', 'Carrier', required=True)
    snk_partner_id = fields.Many2one('res.partner', 'Vendor', required=True)
    snk_journal_id = fields.Many2one('account.journal', string='Journal', required=True)
    snk_bill_date = fields.Date('Bill Date', default=fields.Date.context_today)
    # JANGAN pakai related + readonly=False di sini. Related yang writable memasang
    # inverse otomatis, sehingga nilai yang diisi user justru ditulis balik ke
    # delivery.carrier.product_id (mengubah produk carrier untuk semua transaksi).
    # Pola compute + store + readonly=False memberi default yang sama, tapi hanya
    # menulis ke record wizard.
    snk_product_id = fields.Many2one('product.product', string='Delivery Product',
                                     compute='_compute_snk_product_id',
                                     store=True, readonly=False, precompute=True)
    snk_amount = fields.Monetary(string='Amount', currency_field='currency_id')
    company_id = fields.Many2one(comodel_name='res.company', string='Company', required=True, readonly=True,
                                 default=lambda self: self.env.company)
    # Idem snk_product_id: related writable ke company_id.currency_id menulis balik ke
    # res.company. Selain salah secara bisnis, write ke res.company memicu modified()
    # yang men-search seluruh tabel transaksi (account_move_line dkk) -> MemoryError.
    currency_id = fields.Many2one(comodel_name='res.currency', string='Currency',
                                  compute='_compute_currency_id',
                                  store=True, readonly=False, precompute=True)
    snk_memo = fields.Text('Memo')
    fiscal_position_id = fields.Many2one('account.fiscal.position', string='Fiscal Position',
                                         domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")

    @api.depends('snk_carrier_id')
    def _compute_snk_product_id(self):
        """Default produk pengiriman diambil dari carrier, tetap bisa diubah user."""
        for wizard in self:
            wizard.snk_product_id = wizard.snk_carrier_id.product_id

    @api.depends('company_id')
    def _compute_currency_id(self):
        """Default currency dari company wizard, tetap bisa diubah user."""
        for wizard in self:
            wizard.currency_id = wizard.company_id.currency_id

    @api.onchange('snk_partner_id', 'company_id')
    def onchange_partner_id(self):
        # Ensures all properties and fiscal positions
        # are taken with the company of the order
        # if not defined, with_company doesn't change anything.
        self = self.with_company(self.company_id)
        default_currency = self._context.get("default_currency_id")
        if not self.snk_partner_id:
            self.fiscal_position_id = False
            self.currency_id = default_currency or self.company_id.currency_id.id
        else:
            self.fiscal_position_id = self.env['account.fiscal.position']._get_fiscal_position(self.snk_partner_id)
            self.currency_id = (default_currency
                                or self.snk_partner_id.property_purchase_currency_id.id
                                or self.company_id.currency_id.id)
        return {}

    def action_create_bill(self):
        active_id = self._context.get('active_id')
        active_ids = self._context.get('active_ids', [active_id] if active_id else [])
        result = self.env['ir.actions.act_window']._for_xml_id('account.action_move_in_invoice_type')
        if self._context.get('active_model') == 'stock.picking' and active_ids:
            picks = self.env['stock.picking'].sudo().search([('id', 'in', active_ids)])

            pick_ids = []
            for pick in picks:
                pick_ids.append(pick.id)

            partner_invoice = self.env['res.partner'].sudo().browse(self.snk_partner_id.address_get(['invoice'])['invoice'])
            partner_bank_id = self.snk_partner_id.commercial_partner_id.bank_ids.filtered_domain(
                ['|', ('company_id', '=', False), ('company_id', '=', self.company_id.id)])[:1]

            bill_vals = {
                'move_type': 'in_invoice',
                'invoice_date': self.snk_bill_date,
                'narration': self.snk_memo,
                'currency_id': self.currency_id.id,
                'partner_id': partner_invoice.id,
                'fiscal_position_id': (self.fiscal_position_id or self.fiscal_position_id._get_fiscal_position(
                    partner_invoice)).id,
                'partner_bank_id': partner_bank_id.id,
                'invoice_line_ids': [(0, 0, {
                    'product_id': self.snk_product_id.id,
                    'quantity': 1,
                    'price_unit': self.snk_amount,
                    'tax_ids': []
                })],
                'snk_picking_ids': [[6, 0, pick_ids]],
                'company_id': self.company_id.id,
            }

            bill = self.env['account.move'].sudo().create(bill_vals)
            # choose the view_mode accordingly
            if bill:
                res = self.env.ref('account.view_move_form', False)
                form_view = [(res and res.id or False, 'form')]
                if 'views' in result:
                    result['views'] = form_view + [(state, view) for state, view in result['views'] if view != 'form']
                else:
                    result['views'] = form_view
                result['res_id'] = bill.id
            else:
                result = {'type': 'ir.actions.act_window_close'}

        return result
