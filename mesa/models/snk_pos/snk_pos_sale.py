
# Part of Odoo. See LICENSE file for full copyright and licensing details.
import json
import logging
from collections import defaultdict
from datetime import timedelta
from itertools import groupby
from odoo import SUPERUSER_ID, _, api, fields, models
# from odoo.addons.queue_job.job import job
# from odoo.addons.queue_job.decorators import job
from odoo.exceptions import (
    AccessError,
    RedirectWarning,
    UserError,
    ValidationError,
)
from odoo.fields import Command
from odoo.http import request
from odoo.osv import expression
from odoo.tools import (
    create_index,
    float_is_zero,
    format_amount,
    format_date,
    is_html_empty,
    SQL,
)
from odoo.tools.mail import html_keep_url
from odoo.addons.payment import utils as payment_utils
from datetime import date
from datetime import datetime
import time as t

_logger = logging.getLogger(__name__)
INVOICE_STATUS = [
    ('upselling', 'Upselling Opportunity'),
    ('invoiced', 'Fully Invoiced'),
    ('to invoice', 'To Invoice'),
    ('no', 'Nothing to Invoice')
]
POS_SALE_STATE = [
    ('draft', 'New'),
    ('confirmed', 'Confirmed'),
    ('close', 'Closed'),
    ('cancel', 'Cancelled')
]

class SnkPOSSale(models.Model):
    _name = 'snk.pos.sale'
    _inherit = ['portal.mixin', 'product.catalog.mixin', 'mail.thread', 'mail.activity.mixin', 'utm.mixin', ]
    # 'queue.job.mixin'
    
    _description = "SNK POS Sale"
    _order = 'start_date desc, id desc'
    _check_company_auto = True
    active = fields.Boolean(default=True)
    name = fields.Char(
        string="Name",
        required=True, copy=False, readonly=False,
        index='trigram',
        default=lambda self: _('New'))
    state = fields.Selection(
        selection=POS_SALE_STATE,
        string="Status",
        readonly=True, copy=False, index=True,
        default='draft')
    reference = fields.Char(string="Reference")
    start_date = fields.Date(string='Period')
    end_date = fields.Date(string='End Date')
    project_ids = fields.Many2many('project.project', string='Events', copy=False)
    is_invoiced = fields.Boolean(string="Closing with invoice", default=True)
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', readonly=True, groups="hr.group_hr_user")
    payment_term_id = fields.Many2one(
        comodel_name='account.payment.term',
        string="Payment Terms",
        compute='_compute_payment_term_id',
        store=True, readonly=False, precompute=True, check_company=True,  # Unrequired company
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]")
    customer_id = fields.Many2one(
        'res.partner',
        string="Customer",
        domain=[('customer_rank', '>', 0)],
    )
    site_ids = fields.Many2many(
        'snk.site',
        'snk_pos_sale_site_rel',
        'sale_id',
        'site_id',
        string="Sites",
        domain="[('customer_id', '=', customer_id)]",
    )
    partner_ids = fields.Many2many(
        'res.partner',
        'snk_pos_sale_partner_rel',
        'sale_id',
        'partner_id',
        string="Delivery Address",
    )
    analytic_account_ids = fields.Many2many(
        'account.analytic.account',
        'snk_pos_sale_analytic_rel',
        'sale_id',
        'analytic_id',
        domain=[('snk_brand', '=', True), ('active', '=', True)],
        string="Brands",
    )
    account_move_id = fields.Many2one(comodel_name='account.move', string='Invoice Journal', copy=False)
    # account_move_ids = fields.Many2many(
    #     string="Invoices",
    #     comodel_name='account.move', readonly=True,
    #     # domain = [('pos_sale_id', '=', self.id)],
    #     # compute='_compute_account_move_ids',
    #     copy=False
    # )
    account_move_ids = fields.One2many(
        'account.move',
        inverse_name='pos_sale_id',
        string='Invoices'
    )
    terms_type = fields.Selection(related='company_id.terms_type')
    journal_id = fields.Many2one(
        'account.journal',
        check_company=True,
        default= lambda self: self._default_journal(),
        string="Invoicing Journal"
    )
    invoice_status = fields.Selection(
        selection=INVOICE_STATUS,
        string="Invoice Status",
        compute="_compute_invoice_status",
        store="True"
    )
    fiscal_position_id = fields.Many2one(
        comodel_name='account.fiscal.position',
        string="Fiscal Position",
        compute='_compute_fiscal_position_id',
        store=True, readonly=False, precompute=True, check_company=True,
        help="Fiscal positions are used to adapt taxes and accounts for particular customers or sales orders/invoices."
             "The default value comes from the customer.",
    )
    tax_totals = fields.Binary(compute='_compute_tax_totals', exportable=False)
    order_domain_ids = fields.Many2many('snk.pos.order', 'snk_sale_order_domain_rel', string="Orders Domain" )
    order_ids = fields.Many2many(
        comodel_name='snk.pos.order',
        relation='snk_pos_order_rel',
        column1='sale_id',
        column2='order_id',
        string="Orders",
        copy=False
        )
    order_lines = fields.One2many(
        comodel_name='snk.pos.order',
        inverse_name='sale_id',
        string="Order Lines")
    note = fields.Html(
        string="Terms and conditions",
        compute='_compute_note',
        store=True, readonly=False, precompute=True)
    invoice_count = fields.Integer(
        string="Invoices",
        compute='_compute_invoice_count',
        store=False)
    amount_untaxed = fields.Monetary(
        string="Untaxed Amount",
        compute='_compute_amounts',
        store=True,
        currency_field='currency_id')
    amount_tax = fields.Monetary(
        string="Tax Amount",
        compute='_compute_amounts',
        store=True,
        currency_field='currency_id')
    amount_total = fields.Monetary(
        string="Total Amount",
        compute='_compute_amounts',
        store=True,
        currency_field='currency_id')

    # Common methods
    # ----------------
    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == "form":
            for node in arch.xpath("//field"):
                if node.get('readonly'):
                    readonly = node.get('readonly')
                    readonly += " or [('state', 'not in', ('draft'))]"
                else:
                    node.set("readonly", "state not in ('draft')")
        return arch, view
    
    def create(self, values):
        seq = self.env['ir.sequence'].next_by_code(self._name) or 'New'
        if isinstance(values, list):
            for value in values:
                value['name'] = seq
        else:
            values['name'] = seq
        return super().create(values)
    
    @api.depends('customer_id')
    def _compute_payment_term_id(self):
        for order in self:
            order = order.with_company(order.company_id)
            order.payment_term_id = order.customer_id.property_payment_term_id
        

    def _default_journal(self):
        return self.env['account.journal'].search(
            [('type', '=', 'sale')],
            order="sequence, id",
            limit=1
        )

    @api.depends('state', 'account_move_id.state')
    def _compute_invoice_status(self):
        for rec in self:
            rec.invoice_status = 'no'
            if rec.account_move_id:
                if rec.account_move_id.state == 'draft':
                    rec.invoice_status = 'to invoice'
                    rec.state = 'confirmed'

                elif rec.account_move_id.state == 'posted':
                    rec.invoice_status = 'invoiced'
                    # otomatis close jika sudah invoiced
                    if rec.state not in ('close', 'cancel'):
                        rec.state = 'close'
    
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec.account_move_ids)
        return rec.invoice_count
    
    @api.depends('order_lines.price_subtotal', 'order_lines.price_total', 'order_lines.price_tax')
    def _compute_amounts(self):
        # Skip kalau lagi install/upgrade module
        if self.env.context.get('install_mode') or self.env.context.get('module'):
            return

        # Batch compute pakai read_group
        # order_lines adalah O2M ke snk.pos.order (inverse sale_id)
        result = self.env['snk.pos.order'].read_group(
            domain=[('sale_id', 'in', self.ids)],
            fields=['price_subtotal:sum', 'price_total:sum'],
            groupby=['sale_id']
        )
        
        # Mapping hasil
        amounts = {r['sale_id'][0]: r for r in result}

        for sale in self:
            data = amounts.get(sale.id, {'price_subtotal': 0, 'price_total': 0})
            sale.amount_untaxed = data['price_subtotal']
            sale.amount_total = data['price_total']
            sale.amount_tax = sale.amount_total - sale.amount_untaxed
            # sale.amount_untaxed = sum(sale.order_lines.mapped('price_subtotal'))
            # sale.amount_total = sum(sale.order_lines.mapped('price_total'))
            # sale.amount_tax = sale.amount_total - sale.amount_untaxed
    
    @api.depends('customer_id', 'company_id')
    def _compute_fiscal_position_id(self):
        for order in self:
            order.fiscal_position_id = self.env['account.fiscal.position'].with_company(
                self.company_id)._get_fiscal_position(self.customer_id).id
    
    @api.depends('customer_id')
    def _compute_note(self):
        use_invoice_terms = self.env['ir.config_parameter'].sudo().get_param('account.use_invoice_terms')
        if not use_invoice_terms:
            return
        for order in self:
            order = order.with_company(order.company_id)
            if order.terms_type == 'html' and self.env.company.invoice_terms_html:
                baseurl = html_keep_url(order._get_note_url() + '/terms')
                context = {'lang': order.partner_id.lang or self.env.user.lang}
                order.note = _('Terms & Conditions: %s', baseurl)
                del context
            elif not is_html_empty(self.env.company.invoice_terms):
                if order.partner_id.lang:
                    order = order.with_context(lang=order.partner_id.lang)
                order.note = order.env.company.invoice_terms
    
    @api.depends_context('lang')
    @api.depends('order_lines.price_subtotal', 'order_lines.price_total', 'order_lines.price_tax', 'order_lines.tax_id', 'currency_id', 'company_id')
    def _compute_tax_totals(self):
        AccountTax = self.env['account.tax']
        for order in self:
            order_lines = order.order_lines
            base_lines = [line._prepare_base_line_for_taxes_computation() for line in order_lines]
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            order.tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )
            
    
    # Onchange methods
    # ----------------
    @api.onchange('site_ids')
    def _onchange_site_ids(self):
        for rec in self:
            _logger.info(">>> Customer ID: %s", rec.customer_id.id if rec.customer_id else None)
            _logger.info(">>> Sites: %s", rec.site_ids.ids)
            if rec.site_ids:
                rec.partner_ids = [(6, 0, rec.site_ids.mapped('partner_id').ids)]
            else:
                rec.partner_ids = [(5, 0, 0)]

    @api.onchange('invoice_status')
    def _onchange_invoice_status(self):
         """Jika invoice_status jadi 'invoiced', otomatis ubah state ke 'close'."""
         for rec in self:
            if rec.invoice_status == 'invoiced':
                rec.state = 'close'
            elif rec.invoice_status != 'invoiced' and rec.state != 'draft':
                rec.state = 'confirmed'

    # Actions methods
    # ----------------
    def action_open_invoices(self):
        self.ensure_one()
        count = self._compute_invoice_count()
        _logger.info(">>> Invoice count: %d", count)
        return {
        'type': 'ir.actions.act_window',
            'name': _('Invoices'),
            'res_model': 'account.move',
            'view_mode': 'list,form',
            'domain': [('id', 'in', self.account_move_ids.ids)],  # <-- pakai field one2many
            'target': 'current',
        }
    def action_confirm(self):
        self.ensure_one()
        self.write({'state': 'confirmed'})

    def action_cancel(self):
        self.ensure_one()
        invoice = self.account_move_id
        invoices = self.account_move_ids.filtered(lambda inv: inv.state in ['draft', 'posted'])
        if invoices:
            _logger.info(">>> Found %d related Invoices", len(invoices))
            if invoice:
                _logger.info(">>> Invoice state: %s", invoice.state)
                if invoice.state == "draft":
                    raise UserError(_("Cannot cancel a Sale with an Invoice. Please cancel the Invoice first."))
                elif invoice.state == "posted":
                    raise UserError(_("Cannot cancel a Sale with posted Invoice. Please cancel the Invoice first."))
                else:
                    self.write({'state': 'cancel'})
            else:
                raise UserError(_("No Invoice found. Please check account_move_id."))
        else:
            self.write({'state': 'cancel'})
    
    def action_reset(self):
        self.ensure_one()
        self.write({
            'state': 'draft',
            'account_move_id': False
        })
    
    def action_close(self):
        self.ensure_one()
        self.write({'state': 'close'})
        for order in self.order_lines:
            order.write({'state': 'close'})

    def action_add_orders(self):
        """Open selectable list of POS Orders"""
        self.ensure_one()

        if not self.customer_id:
            raise UserError(_("Please set a Customer first."))

        domain = [
            ('sale_id', '=', False),
            ('state', '=', 'confirmed'),
            ('customer_id', '=', self.customer_id.id),
        ]

        if self.site_ids:
            domain.append(('snk_site_id', 'in', self.site_ids.ids))
        if self.start_date:
            domain.append(('date_order', '>=', datetime.combine(self.start_date, datetime.min.time())))
        if self.end_date:
            domain.append(('date_order', '<=', datetime.combine(self.end_date, datetime.max.time())))
        if self.analytic_account_ids:
            domain.append(('analytic_account_id', 'in', self.analytic_account_ids.ids))
        if self.project_ids:
            domain.append(('project_ids', 'in', self.project_ids.ids))

        _logger.info(">>> Open order selection domain: %s", domain)
        return {
            'name': 'Select Orders',
            'type': 'ir.actions.act_window',
            'res_model': 'snk.pos.order',
            'view_mode': 'list',
            'views': [(self.env.ref('mesa.snk_pos_order_view_list_wizard').id, 'list')],
            'target': 'current',
            'domain': domain,
            'context': {
                'default_sale_id': self.id,
            },
        }
    
    def action_remove_orders(self):
        """Open wizard to remove POS Orders from this Sale"""
        self.ensure_one()

        if not self.order_lines:
            raise UserError(_("Please add an order first."))

        domain = [
            ('sale_id', '=', self.id),
        ]
        _logger.info(">>> Open order removal selection domain: %s", domain)

        return {
            'name': 'Remove POS Orders',
            'type': 'ir.actions.act_window',
            'res_model': 'snk.pos.order',
            'view_mode': 'list',
            'view_id': self.env.ref('mesa.snk_pos_order_view_list_remove_wizard').id,
            'target': 'current',
            'domain': domain,
            'context': {
                'default_sale_id': self.id,
            },
        }