
# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from collections import defaultdict
from odoo.tools import float_compare, float_is_zero, format_date, groupby, is_html_empty
from odoo.tools.mail import html_keep_url
from odoo.exceptions import (
    AccessError,
    RedirectWarning,
    UserError,
    ValidationError,
)
import logging

_logger = logging.getLogger(__name__)
INVOICE_STATUS = [
    ('upselling', 'Upselling Opportunity'),
    ('invoiced', 'Fully Invoiced'),
    ('to invoice', 'To Invoice'),
    ('no', 'Nothing to Invoice')
]
POS_ORDER_STATE = [('draft', 'New'),
    ('confirmed', 'Confirmed'),
    ('close', 'Closed'),
    ('cancel', 'Cancelled')
]

class SnkPOSOrder(models.Model):
    _name = "snk.pos.order"
    _inherit = ["analytic.mixin", "mail.thread", "mail.activity.mixin"]
    _description = "SNK POS Order"
    active = fields.Boolean(default=True)
    sequence = fields.Integer("Sequence")
    name = fields.Char(
        string="Order Reference",
        required=True, copy=False, readonly=False,
        index='trigram',
        default=lambda self: _('New'))
    # bisa double sequence
    # def create(self, values):
    #     seq = self.env['ir.sequence'].next_by_code(self._name) or 'New'
    #     if isinstance(values, list):
    #         for value in values:
    #             value['name'] = seq
    #     else:
    #         values['name'] = seq
    #     return super().create(values)
    
    
    state = fields.Selection(
        selection=POS_ORDER_STATE,
        string="Status",
        readonly=True, copy=False, index=True,
        default='draft')
    sale_id = fields.Many2one(
        'snk.pos.sale', 
        string="Sale Reference", 
        copy=False, 
        )
    return_id = fields.Many2one('snk.pos.order', string="Return Order", readonly=True)
    return_of_id = fields.Many2one('snk.pos.order', string="Return Of", readonly=True)
    date_order = fields.Datetime( 
        string="Order Date",
        required=True, copy=False,
        help="Creation date of draft/sent orders,\nConfirmation date of confirmed orders.",
        default=lambda self: fields.Datetime.now())
    reference = fields.Char(string="Reference")
    customer_id = fields.Many2one('res.partner', 'Customer', domain="[('customer_rank', '>', 0), ('is_company', '=', True)]", check_company=True, required=True)
    partner_id = fields.Many2one('res.partner', 'Delivery Address', domain="[('customer_rank', '>', 0), ('parent_id', '=', customer_id)]", check_company=True, required=True)
    warehouse_id = fields.Many2one('stock.warehouse', string="Warehouse",
        domain="[('partner_id', '=', partner_id)]", check_company=True, required=True)
    # company_registry = fields.Char(string="Site Code", related="partner_id.ref", store=True)
    company_registry = fields.Char(string="Site Code", related="partner_id.ref")
    snk_site_id = fields.Many2one('snk.site', string="Site")
    snk_site_name = fields.Char(string="Site Name", related="snk_site_id.full_name", store=True)
    
    picking_type_id = fields.Many2one('stock.picking.type', string="Operation Type", required=True)
    stock_picking_id = fields.Many2one('stock.picking', string="Stock Picking")
    company_id = fields.Many2one('res.company', required=True, default=lambda self: self.env.company)
    currency_id = fields.Many2one('res.currency', related='company_id.currency_id', store=True, readonly=True, groups="hr.group_hr_user")
    currency_rate = fields.Float(
        string="Currency Rate",
        compute='_compute_currency_rate',
        digits=0,
        store=True, precompute=True)
    promo_code = fields.Char(string="Promo Code")
    terms_type = fields.Selection(related='company_id.terms_type')
    invoice_status = fields.Selection(
        selection=INVOICE_STATUS,
        string="Invoice Status",
        related="sale_id.invoice_status",
        default="no",
        store="True",
        copy=False
    )
    delivery_status = fields.Selection([
        ('pending', 'Not Delivered'),
        ('started', 'Started'),
        ('partial', 'Partially Delivered'),
        ('full', 'Fully Delivered'),
    ], string='Delivery Status')
    fiscal_position_id = fields.Many2one(
        comodel_name='account.fiscal.position',
        string="Fiscal Position",
        compute='_compute_fiscal_position_id',
        store=True, readonly=False, precompute=True, check_company=True,
        help="Fiscal positions are used to adapt taxes and accounts for particular customers or sales orders/invoices."
             "The default value comes from the customer.",
    )
    analytic_account_id = fields.Many2one('account.analytic.account',
                                          string='Brand',
                                          related='product_id.analytic_id',
                                          domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
                                          store=True)
    # Generic configuration fields
    product_id = fields.Many2one(
        comodel_name='product.product',
        string="Product",
        required=True,
        change_default=True, ondelete='restrict', check_company=True, index='btree_not_null',
        domain="[('sale_ok', '=', True)]")
    product_variant_ref = fields.Char(string="Variant Reference", related="product_id.default_code", store=True)
    product_template_id = fields.Many2one(
        string="Product Template",
        comodel_name='product.template',
        readonly=True,
        store=True,
        related='product_id.product_tmpl_id'
        # search='_search_product_template_id',
        # compute='_compute_product_template_id',
        # not anymore since the field must be considered editable for product configurator logic
        # without modifying the related product_id when updated.
        # domain=[('sale_ok', '=', True)]
        )
    product_template_ref = fields.Char(string="Template Reference", related="product_template_id.default_code", store=True)
    product_uom_category_id = fields.Many2one(related='product_id.uom_id.category_id', depends=['product_id'])
    product_no_variant_attribute_value_ids = fields.Many2many(
        comodel_name='product.template.attribute.value',
        string="Extra Values",
        compute='_compute_no_variant_attribute_values',
        store=True, readonly=False, precompute=True, ondelete='restrict')
    product_type = fields.Selection(related='product_id.type', depends=['product_id'])
    combo_item_id = fields.Many2one(comodel_name='product.combo.item')
    description = fields.Text(
        string="Description",
        compute='_compute_description',
        store=True, readonly=False, required=True, precompute=True)
    grade_id = fields.Many2one(
        comodel_name='snk.product.grade',
        string="Grade",
        readonly=True,
        index=True,
        ondelete='restrict',
    )
    product_uom_qty = fields.Float(
        string="Quantity",
        compute='_compute_product_uom_qty',
        digits='Product Unit of Measure', default=1.0,
        store=True, readonly=False, required=True, precompute=True)
    

    product_uom = fields.Many2one(
        comodel_name='uom.uom',
        string="Unit of Measure",
        compute='_compute_product_uom',
        store=True, readonly=False, precompute=True, ondelete='restrict',
        domain="[('category_id', '=', product_uom_category_id)]")
    # Pricing fields
    tax_id = fields.Many2many(
        comodel_name='account.tax',
        string="Taxes",
        compute='_compute_tax_id',
        store=True, readonly=False, precompute=True,
        context={'active_test': False},
        check_company=True)
    tax_country_id = fields.Many2one(
        comodel_name='res.country',
        compute='_compute_tax_country_id',
        store=True,
        # Avoid access error on fiscal position when reading a sale order with company != user.company_ids
        compute_sudo=True)  # used to filter available taxes depending on the fiscal country and position
    
    # Tech field caching pricelist rule used for price & discount computation
    pricelist_item_id = fields.Many2one(
        comodel_name='product.pricelist.item',
        compute='_compute_pricelist_item_id',store=True)
    price_unit = fields.Monetary(
        string="Unit Price",
        compute='_compute_price_unit',
        currency_field='currency_id',
        store=True, readonly=False, required=True,
        precompute=False)
    discount = fields.Float(
        string="Discount (%)",
        compute='_compute_discount',
        inverse='_inverse_discount',
        store=True, readonly=False,
        precompute=False)
    price_subtotal = fields.Monetary(
        string="Subtotal",
        compute='_compute_amount',
        currency_field='currency_id',
        store=True, precompute=True)
    price_tax = fields.Monetary(
        string="Total Tax",
        compute='_compute_amount',
        currency_field='currency_id',
        store=True, precompute=True)
    price_total = fields.Monetary(
        string="Total",
        compute='_compute_amount',
        currency_field='currency_id',
        store=True, precompute=True)
    # Logistics/Delivery fields
    product_packaging_id = fields.Many2one(
        comodel_name='product.packaging',
        string="Packaging",
        compute='_compute_product_packaging_id',
        store=True, readonly=False, precompute=True,
        domain="[('sales', '=', True), ('product_id','=',product_id)]",
        check_company=True)
    product_packaging_qty = fields.Float(
        string="Packaging Quantity",
        compute='_compute_product_packaging_qty',
        store=True, readonly=False, precompute=True)
    untaxed_amount_invoiced = fields.Monetary(
        string="Untaxed Invoiced Amount",
        compute='_compute_untaxed_amount_invoiced',
        store=True)
    untaxed_amount_to_invoice = fields.Monetary(
        string="Untaxed Amount To Invoice",
        compute='_compute_untaxed_amount_to_invoice',
        store=True)
    note = fields.Html(
        string="Terms and conditions",
        compute='_compute_note',
        store=True, readonly=False, precompute=True)
    pricelist_id = fields.Many2one(
        comodel_name='product.pricelist',
        string="Pricelist",
        compute='_compute_pricelist_id',
        store=True, readonly=False, precompute=True, check_company=True,  # Unrequired company
        domain="['|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="If you change the pricelist, only newly added lines will be affected.")
    discount_amount = fields.Float(string='Discount')
    
    @api.model
    def _get_view(self, view_id=None, view_type='form', **options):
        # Safe call to super
        result = super()._get_view(view_id, view_type, **options)
        if not result:
            _logger.warning("View not found: view_id=%s, type=%s", view_id, view_type)
            return None, None
        arch, view = result

        if view_type == "form" and arch is not None:
            # Example: make all fields readonly based on state
            for node in arch.xpath("//field"):
                if node.get('readonly'):
                    readonly = node.get('readonly')
                    readonly += " or [('state', 'not in', ('draft'))]"
                    node.set("readonly", readonly)
                else:
                    node.set("readonly", "state not in ('draft')")

        return arch, view
    
    @api.model_create_multi
    def create(self, vals_list):
        product_ids = {
            vals['product_id'] for vals in vals_list if vals.get('product_id')
        }
        grade_by_product = {
            product.id: product.snk_grade_id.id
            for product in self.env['product.product'].browse(product_ids)
        }
        for vals in vals_list:
            vals['name'] = self.env['ir.sequence'].next_by_code(self._name) or _('New')
            if vals.get('product_id') and 'grade_id' not in vals:
                vals['grade_id'] = grade_by_product.get(vals['product_id']) or False
        return super().create(vals_list)

    def write(self, vals):
        # A product change is allowed only while the order is editable. Capture its
        # current grade explicitly so later product-master changes cannot rewrite history.
        if vals.get('product_id') and 'grade_id' not in vals:
            product = self.env['product.product'].browse(vals['product_id'])
            vals['grade_id'] = product.snk_grade_id.id or False
        return super().write(vals)

    def copy_data(self, default=None):
        default = dict(default or {})
        vals_list = super().copy_data(default=default)
        return vals_list
   
    @api.depends('currency_id', 'date_order', 'company_id')
    def _compute_currency_rate(self):
        for order in self:
            order.currency_rate = self.env['res.currency']._get_conversion_rate(
                from_currency=order.company_id.currency_id,
                to_currency=order.currency_id,
                company=order.company_id,
                date=(order.date_order or fields.Date.today()),
            )
    @api.depends('partner_id', 'company_id')
    def _compute_pricelist_id(self):
        for order in self:
            if order.state != 'draft':
                continue
            if not order.partner_id:
                order.pricelist_id = False
                continue
            order = order.with_company(order.company_id)
            order.pricelist_id = order.partner_id.property_product_pricelist
    

    

    # Compute methods
    @api.depends('company_id', 'fiscal_position_id')
    def _compute_tax_country_id(self):
        for record in self:
            if record.fiscal_position_id.foreign_vat:
                record.tax_country_id = record.fiscal_position_id.country_id
            else:
                record.tax_country_id = record.company_id.account_fiscal_country_id
    @api.depends('product_id')
    def _compute_product_template_id(self):
        for line in self:
            line.product_template_id = line.product_id.product_tmpl_id
    def _search_product_template_id(self, operator, value):
        return [('product_id.product_tmpl_id', operator, value)]
    @api.depends('product_id')
    def _compute_no_variant_attribute_values(self):
        for line in self:
            if not line.product_id:
                line.product_no_variant_attribute_value_ids = False
                continue
            if not line.product_no_variant_attribute_value_ids:
                continue
            valid_values = line.product_id.product_tmpl_id.valid_product_template_attribute_line_ids.product_template_value_ids
            # remove the no_variant attributes that don't belong to this template
            for ptav in line.product_no_variant_attribute_value_ids:
                if ptav._origin not in valid_values:
                    line.product_no_variant_attribute_value_ids -= ptav
    @api.depends('product_id')
    def _compute_description(self):
        for line in self:
            if not line.product_id:
                continue
            lang = line._get_lang()
            if lang != self.env.lang:
                line = line.with_context(lang=lang)
            description = line._get_order_line_multiline_description_sale()
            line.description = description
    @api.depends('product_id', 'product_uom_qty', 'product_uom')
    def _compute_product_packaging_id(self):
        for line in self:
            # remove packaging if not match the product
            if line.product_packaging_id.product_id != line.product_id:
                line.product_packaging_id = False
            # suggest biggest suitable packaging matching the SO's company
            if line.product_id and line.product_uom_qty and line.product_uom:
                suggested_packaging = line.product_id.packaging_ids \
                    .filtered(lambda p: p.sales and (p.product_id.company_id <= p.company_id <= line.company_id)) \
                    ._find_suitable_product_packaging(line.product_uom_qty, line.product_uom)
                line.product_packaging_id = suggested_packaging or line.product_packaging_id
    @api.depends('product_packaging_id', 'product_uom', 'product_uom_qty')
    def _compute_product_packaging_qty(self):
        self.product_packaging_qty = 0
        for line in self:
            if not line.product_packaging_id:
                continue
            line.product_packaging_qty = line.product_packaging_id._compute_qty(line.product_uom_qty, line.product_uom)
    def _get_order_line_multiline_description_sale(self):
        self.ensure_one()
        return self.product_id.get_product_multiline_description_sale()
    @api.depends('product_id')
    def _compute_product_uom(self):
        for line in self:
            if not line.product_uom or (line.product_id.uom_id.id != line.product_uom.id):
                line.product_uom = line.product_id.uom_id
    @api.depends('product_id', 'product_packaging_qty')
    def _compute_product_uom_qty(self):
        for line in self:
            if not line.product_packaging_id:
                continue
            packaging_uom = line.product_packaging_id.product_uom_id
            qty_per_packaging = line.product_packaging_id.qty
            product_uom_qty = packaging_uom._compute_quantity(
                line.product_packaging_qty * qty_per_packaging, line.product_uom)
            if float_compare(product_uom_qty, line.product_uom_qty, precision_rounding=line.product_uom.rounding) != 0:
                line.product_uom_qty = product_uom_qty
    
    @api.depends('product_id', 'company_id', 'fiscal_position_id')
    def _compute_tax_id(self):
        taxes_by_product_company = defaultdict(lambda: self.env['account.tax'])
        lines_by_company = defaultdict(lambda: self.env['snk.pos.order'])
        cached_taxes = {}
        for line in self:
            lines_by_company[line.company_id] += line
        for product in self.product_id:
            for tax in product.taxes_id:
                taxes_by_product_company[(product, tax.company_id)] += tax
        for company, lines in lines_by_company.items():
            for line in lines.with_company(company):
                taxes, comp = None, company
                while not taxes and comp:
                    taxes = taxes_by_product_company[(line.product_id, comp)]
                    comp = comp.parent_id
                if not line.product_id or not taxes:
                    # Nothing to map
                    line.tax_id = False
                    continue
                fiscal_position = line.fiscal_position_id
                cache_key = (fiscal_position.id, company.id, tuple(taxes.ids))
                if cache_key in cached_taxes:
                    result = cached_taxes[cache_key]
                else:
                    result = fiscal_position.map_tax(taxes)
                    cached_taxes[cache_key] = result
                # If company_id is set, always filter taxes by the company
                line.tax_id = result
    @api.depends('product_id', 'product_uom', 'product_uom_qty', 'pricelist_id', 'fiscal_position_id')
    def _compute_pricelist_item_id(self):
        for line in self:
            if not line.product_id or not line.pricelist_id:
                line.pricelist_item_id = False
            else:
                line.pricelist_item_id = line.pricelist_id._get_product_rule(
                    line.product_id,
                    quantity=line.product_uom_qty or 1.0,
                    uom=line.product_uom,
                    date=line.date_order,
                )
    @api.depends('product_id', 'product_uom', 'product_uom_qty', 'pricelist_id', 'fiscal_position_id')
    def _compute_price_unit(self):
        if self.env.context.get('from_pos_inbound'):
            return
        for line in self:
            if not line.product_uom or not line.product_id:
                line.price_unit = 0.0
            else:
                price = line.with_company(line.company_id)._get_display_price()
                line.price_unit = line.product_id._get_tax_included_unit_price(
                    line.company_id or line.env.company,
                    line.currency_id,
                    line.date_order,
                    'sale',
                    fiscal_position=line.fiscal_position_id,
                    product_price_unit=price,
                    product_currency=line.currency_id
                )
    @api.depends('discount_amount', 'price_unit', 'pricelist_id', 'pricelist_item_id')
    def _compute_discount(self):
        if self.env.context.get('from_pos_inbound'):
            return
        for line in self:
            if line.discount_amount and line.price_unit:
                line.discount = (line.discount_amount / line.price_unit) * 100
            else:
                line.discount = 0.0
                # fallback ke pricelist kalau discount_amount kosong
                discount_enabled = self.env['product.pricelist.item']._is_discount_feature_enabled()
                if line.pricelist_id and discount_enabled and line.pricelist_item_id and line.pricelist_item_id._show_discount():
                    line = line.with_company(line.company_id)
                    pricelist_price = line._get_pricelist_price()
                    base_price = line._get_pricelist_price_before_discount()
                    if base_price != 0:  # Avoid division by zero
                        discount = (base_price - pricelist_price) / base_price * 100
                        if (discount > 0 and base_price > 0) or (discount < 0 and base_price < 0):
                            # only show negative discounts if price is negative
                            # otherwise it's a surcharge which shouldn't be shown to the customer
                            line.discount = discount

    def _inverse_discount(self):
        for line in self:
            if line.discount and line.price_unit:
                line.discount_amount = line.price_unit * (line.discount / 100.0)
            else:
                line.discount_amount = 0.0
    def _prepare_base_line_for_taxes_computation(self, **kwargs):
        """ Convert the current record to a dictionary in order to use the generic taxes computation method
        defined on account.tax.
        :return: A python dictionary.
        """
        self.ensure_one()
        """ Override untuk support discount_amount.
        Kalau discount_amount terisi, pakai itu.
        Kalau tidak, fallback ke discount (%) standar.
        """
        # hitung price_unit setelah diskon
        if self.discount_amount:
            # langsung kurangi nominal
            price_unit = self.price_unit - self.discount_amount
            if price_unit < 0:
                price_unit = 0.0
            discount = 0.0
        elif self.discount and self.discount > 0:
            # default: diskon persentase
            price_unit = self.price_unit * (1 - (self.discount or 0.0) / 100.0)
            discount = 0.0
        else:
            price_unit = self.price_unit
            discount = 0.0
            
        
        return self.env['account.tax']._prepare_base_line_for_taxes_computation(
            self,
            **{
                'tax_ids': self.tax_id,
                'price_unit': price_unit,
                'quantity': self.product_uom_qty,
                'partner_id': self.customer_id,
                'currency_id': self.currency_id or self.company_id.currency_id,
                'rate': self.currency_rate,
                'discount': discount,
                **kwargs,
            },
        )
            
    @api.depends('product_uom_qty', 'price_unit', 'tax_id', 'discount', 'discount_amount', 'currency_id')
    def _compute_amount(self):
        AccountTax = self.env['account.tax']
        for line in self:
            base_line = line._prepare_base_line_for_taxes_computation()
            # tambahkan detail pajak
            AccountTax._add_tax_details_in_base_line(base_line, line.company_id)
            # normalisasi & akumulasi (pastikan qty & rounding ikut)
            AccountTax._round_base_lines_tax_details([base_line], line.company_id)

            tax_details = base_line.get('tax_details', {})
            line.price_subtotal = tax_details.get('total_excluded_currency', 0.0)
            line.price_total = tax_details.get('total_included_currency', line.price_subtotal)
            line.price_tax = line.price_total - line.price_subtotal

    @api.depends('state', 'sale_id.invoice_status', 'price_subtotal')
    def _compute_untaxed_amount_invoiced(self):
        """Track invoiced amount from the parent POS Sale.

        Invoice creation is owned by ``snk.pos.sale`` and its invoice lines do
        not keep a relation to an individual ``snk.pos.order``.  Therefore the
        parent invoice status is the only reliable source of truth here.
        """
        for line in self:
            is_invoiced = (
                line.state in ('confirmed', 'close')
                and line.sale_id.invoice_status == 'invoiced'
            )
            line.untaxed_amount_invoiced = line.price_subtotal if is_invoiced else 0.0

    @api.depends('state', 'price_subtotal', 'untaxed_amount_invoiced')
    def _compute_untaxed_amount_to_invoice(self):
        """Compute the remaining untaxed amount without invoice-line linkage."""
        for line in self:
            if line.state in ('confirmed', 'close'):
                line.untaxed_amount_to_invoice = (
                    line.price_subtotal - line.untaxed_amount_invoiced
                )
            else:
                line.untaxed_amount_to_invoice = 0.0

    def _get_pricelist_price(self):
        """Compute the price given by the pricelist for the given line information.
        :return: the product sales price in the order currency (without taxes)
        :rtype: float
        """
        self.ensure_one()
        self.product_id.ensure_one()
        price = self.pricelist_item_id._compute_price(
            product=self.product_id.with_context(**self._get_product_price_context()),
            quantity=self.product_uom_qty or 1.0,
            uom=self.product_uom,
            date=self.date_order,
            currency=self.currency_id,
        )
        return price
    def _get_product_price_context(self):
        """Gives the context for product price computation.
        :return: additional context to consider extra prices from attributes in the base product price.
        :rtype: dict
        """
        self.ensure_one()
        return self.product_id._get_product_price_context(
            self.product_no_variant_attribute_value_ids,
        )
    def _get_pricelist_price_context(self):
        """DO NOT USE in new code, this contextual logic should be dropped or heavily refactored soon"""
        self.ensure_one()
        return {
            'pricelist': self.pricelist_id.id,
            'uom': self.product_uom.id,
            'quantity': self.product_uom_qty,
            'date': self.date_order,
        }
    def _get_pricelist_price_before_discount(self):
        """Compute the price used as base for the pricelist price computation.
        :return: the product sales price in the order currency (without taxes)
        :rtype: float
        """
        self.ensure_one()
        self.product_id.ensure_one()
        return self.pricelist_item_id._compute_price_before_discount(
            product=self.product_id.with_context(**self._get_product_price_context()),
            quantity=self.product_uom_qty or 1.0,
            uom=self.product_uom,
            date=self.date_order,
            currency=self.currency_id,
        )
    @api.depends('partner_id', 'company_id')
    def _compute_fiscal_position_id(self):
        for order in self:
            order.fiscal_position_id = self.env['account.fiscal.position'].with_company(self.company_id)._get_fiscal_position(self.partner_id).id
    def _get_lang(self):
        self.ensure_one()
        if self.partner_id.lang and not self.partner_id.is_public:
            return self.partner_id.lang
        return self.env.lang
    
    def _get_display_price(self):
        """Compute the displayed unit price for a given line.
        Overridden in custom flows:
        * where the price is not specified by the pricelist
        * where the discount is not specified by the pricelist
        Note: self.ensure_one()
        """
        self.ensure_one()
        if self.product_type == 'combo':
            return 0  # The display price of a combo line should always be 0.
        if self.combo_item_id:
            return self._get_combo_item_display_price()
        return self._get_display_price_ignore_combo()
    def _get_display_price_ignore_combo(self):
        """ This helper method allows to compute the display price of a SOL, while ignoring combo
        logic.
        I.e. this method returns the display price of a SOL as if it were neither a combo line nor a
        combo item line.
        """
        self.ensure_one()
        pricelist_price = self._get_pricelist_price()
        if not self.pricelist_item_id or not self.pricelist_item_id._show_discount():
            # No pricelist rule found => no discount from pricelist
            return pricelist_price
        base_price = self._get_pricelist_price_before_discount()
        # negative discounts (= surcharge) are included in the display price
        return max(base_price, pricelist_price)
    def _get_pricelist_price(self):
        """Compute the price given by the pricelist for the given line information.
        :return: the product sales price in the order currency (without taxes)
        :rtype: float
        """
        self.ensure_one()
        self.product_id.ensure_one()
        price = self.pricelist_item_id._compute_price(
            product=self.product_id.with_context(**self._get_product_price_context()),
            quantity=self.product_uom_qty or 1.0,
            uom=self.product_uom,
            date=self.date_order,
            currency=self.currency_id,
        )
        return price
    def _get_product_price_context(self):
        """Gives the context for product price computation.
        :return: additional context to consider extra prices from attributes in the base product price.
        :rtype: dict
        """
        self.ensure_one()
        return self.product_id._get_product_price_context(
            self.product_no_variant_attribute_value_ids,
        )
    def _get_pricelist_price_context(self):
        """DO NOT USE in new code, this contextual logic should be dropped or heavily refactored soon"""
        self.ensure_one()
        return {
            'pricelist': self.pricelist_id.id,
            'uom': self.product_uom.id,
            'quantity': self.product_uom_qty,
            'date': self.date_order,
        }
    
    @api.depends('partner_id')
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
    tax_totals = fields.Binary(compute='_compute_tax_totals', exportable=False)
    
    @api.depends_context('lang')
    @api.depends('product_uom_qty', 'price_unit', 'tax_id', 'discount', 'discount_amount', 'currency_id', 'company_id')
    def _compute_tax_totals(self):
        AccountTax = self.env['account.tax']
        for order in self:
            base_lines = [order._prepare_base_line_for_taxes_computation()]
            AccountTax._add_tax_details_in_base_lines(base_lines, order.company_id)
            AccountTax._round_base_lines_tax_details(base_lines, order.company_id)
            order.tax_totals = AccountTax._get_tax_totals_summary(
                base_lines=base_lines,
                currency=order.currency_id or order.company_id.currency_id,
                company=order.company_id,
            )
    project_ids = fields.Many2many('project.project',
        "snk_pos_order_project_rel",  # nama tabel relasi buatan
        "id",
        "project_id",                         
        string='Events', copy=False)
    @api.depends('partner_id', 'analytic_account_id', 'project_ids')
    def _compute_analytic_distribution(self):
        super()._compute_analytic_distribution()
        for record in self:
            distributions = {}
            if record.partner_id:
                distribution_model = self.env['account.analytic.distribution.model'].search([
                    ('partner_id', '=', record.partner_id.id),
                ], limit=1)
                if distribution_model:
                    distributions.update(distribution_model.analytic_distribution)
            if record.analytic_account_id:
                distributions.update({record.analytic_account_id.id: 100})
            for project in record.project_ids:
                distributions.update(project._get_analytic_distribution())
            record.analytic_distribution = distributions
    
    # VERSI SEDERHANA (sesuaikan dengan struktur model snk.site Anda)
    @api.onchange('snk_site_id')
    def _onchange_snk_site_id_simple(self):
        """
        Versi sederhana - sesuaikan field sesuai dengan model snk.site Anda
        """
        if self.snk_site_id:
            # Sesuaikan nama field dengan yang ada di model snk.site
            self.customer_id = self.snk_site_id.customer_id  # atau field lain
            self.partner_id = self.snk_site_id.partner_id  # atau field lain  
            self.warehouse_id = self.snk_site_id.warehouse_id  # atau field lain
        else:
            self.customer_id = False
            self.partner_id = False
            self.warehouse_id = False

    @api.onchange('warehouse_id')
    def _onchange_warehouse_id(self):
        if self.product_uom_qty > 0:
            self.picking_type_id = self.warehouse_id.snk_pos_out_id.id
        else:
            self.picking_type_id = self.warehouse_id.snk_pos_in_id.id
    @api.onchange('product_uom_qty')
    def _onchange_product_uom_qty(self):
        if self.product_uom_qty > 0:
            self.picking_type_id = self.warehouse_id.snk_pos_out_id.id
        else:
            self.picking_type_id = self.warehouse_id.snk_pos_in_id.id
    
    # Action
    def action_open_sale(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window',
                'name': _('Sales'),
                'res_model': 'snk.pos.sale',
                'view_mode': 'list,form',
                'domain': [('id', '=', self.sale_id.id)],
                'target': 'current',
                }
    def action_open_picking(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window',
                'name': _('Pickings'),
                'res_model': 'stock.picking',
                'view_mode': 'list,form',
                'domain': [('id', '=', self.stock_picking_id.id)],
                'target': 'current',
                }
    def action_open_return(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window',
                'name': _('Return'),
                'res_model': 'snk.pos.order',
                'view_mode': 'list,form',
                'domain': [('id', '=', self.return_id.id)],
                'target': 'current',
                }
    def action_open_return_of(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window',
                'name': _('Return Of'),
                'res_model': 'snk.pos.order',
                'view_mode': 'list,form',
                'domain': [('id', '=', self.return_of_id.id)],
                'target': 'current',
                }
    
    def action_confirm(self):
        StockPicking = self.env['stock.picking']
        cr = self.env.cr

        all_values = []
        order_ids = []
        for order in self:
            qty = abs(order.product_uom_qty)
            lines = [(0, 0, {
                'product_id': order.product_id.id,
                'snk_grade_id': order.grade_id.id or False,
                'product_uom_qty': qty,
                'quantity': qty,
                'location_id': order.picking_type_id.default_location_src_id.id,
                'location_dest_id': order.picking_type_id.default_location_dest_id.id,
                'name': order.description,
            })]

            values = {
                'picking_type_id': order.picking_type_id.id,
                'location_id': order.picking_type_id.default_location_src_id.id,
                'location_dest_id': order.picking_type_id.default_location_dest_id.id,
                'scheduled_date': order.date_order,
                'snk_site_id': order.snk_site_id.id,
                'snk_pos_ref': order.name,
                'snk_is_pos': True,
                'move_ids': lines,
            }
            if order.analytic_account_id:
                values['analytic_ids'] = [(6, 0, [order.analytic_account_id.id])]
            all_values.append(values)
            order_ids.append(order.id)

        # Buat semua picking sekaligus
        pickings = StockPicking.create(all_values)

        # Bulk update order pakai ORM
        self.write({
            'state': 'confirmed',
        })
        for order, picking in zip(self, pickings):
            order.stock_picking_id = picking.id

        # Validate semua picking batch
        pickings.button_validate()

        return True


    def action_cancel(self):
        self.ensure_one()
        if self.stock_picking_id:
            self.stock_picking_id.action_cancel()
        self.write({'state': 'cancel'})
    def action_reset(self):
        self.ensure_one()
        self.write({'state': 'draft'})
    def action_return(self):
        self.ensure_one()
        product_uom_qty = -1 * self.product_uom_qty
        if product_uom_qty > 0:
            picking_type_id = self.warehouse_id.snk_pos_out_id.id
        else:
            picking_type_id = self.warehouse_id.snk_pos_in_id.id
        values = {
            'active': self.active,
            'date_order': self.date_order,
            'snk_site_id': self.snk_site_id.id,
            'customer_id': self.customer_id.id,
            'partner_id': self.partner_id.id,
            'warehouse_id': self.warehouse_id.id,
            'picking_type_id': picking_type_id,
            'company_id': self.company_id.id,
            'promo_code': self.promo_code,
            'delivery_status': self.delivery_status,
            'fiscal_position_id': self.fiscal_position_id.id,
            'analytic_account_id': self.analytic_account_id.id,
            'project_ids': self.project_ids.ids,
            'pricelist_id': self.pricelist_id.id,
            'product_id': self.product_id.id,
            'product_template_id': self.product_template_id.id,
            'product_no_variant_attribute_value_ids': self.product_no_variant_attribute_value_ids.ids,
            'combo_item_id': self.combo_item_id.id,
            'description': self.description,
            'grade_id': self.grade_id.id,
            'product_uom_qty': product_uom_qty,
            'product_uom': self.product_uom.id,
            'tax_id': self.tax_id.ids,
            'price_unit': self.price_unit,
            'discount_amount': self.discount_amount,
            'discount': self.discount,
            'product_packaging_id': self.product_packaging_id.id,
            'product_packaging_qty': self.product_packaging_qty,
            'note': self.note,
            'return_of_id': self.id,
            'reference': _("Return of %(ref)s" % { 'ref': self.name}),
        }
        SnkPOSOrderReturn = self.env['snk.pos.order'].create(values)
        self.write({'return_id': SnkPOSOrderReturn.id})
        return {'type': 'ir.actions.act_window',
                'name': _(SnkPOSOrderReturn.name),
                'res_model': 'snk.pos.order',
                'view_mode': 'list,form',
                'domain': [('id', '=', SnkPOSOrderReturn.id)],
                'target': 'current',
                }
    
    

    def action_select_orders(self):
        sale_id = self._context.get('default_sale_id')
        if not sale_id:
            raise UserError(_("Sale not set in context."))

        if not self:
            raise UserError(_("Please select at least one POS Order."))

        # Ambil sale dengan browse, bukan search
        sale = self.env['snk.pos.sale'].browse(sale_id)

        # Update Many2one di POS Order (kalau ada field sale_id di POS Order)
        self.write({'sale_id': sale.id})

        # Tambahkan POS Order yang dipilih ke Many2many di sale
        sale.order_ids = [(4, order.id) for order in self]

        _logger.info(">>> %s orders assigned to sale %s", len(self), sale_id)

        return {'type': 'ir.actions.act_window_close'}


    def action_remove_orders(self):
        sale_id = self._context.get('default_sale_id')
        if not sale_id:
            raise UserError(_("Sale not set in context."))
        
        if not self:
            raise UserError(_("Please select at least one POS Order."))

        # Hapus hubungan Many2many di sale
        for order in self:
            if order.sale_id:
                order.sale_id.order_ids = [(3, order.id)]

        # Hapus field sale_id di POS Order (kalau ada)
        self.write({'sale_id': False})

        _logger.info(">>> %s orders unassigned from their sales", len(self))

        return {'type': 'ir.actions.act_window_close'}


    
