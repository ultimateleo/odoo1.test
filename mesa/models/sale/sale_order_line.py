from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError
from odoo.fields import Command
import logging
_logger = logging.getLogger(__name__)

class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"
    _name = "sale.order.line"

    snk_grade_id = fields.Many2one(
        comodel_name="snk.product.grade",
        string="Grade",
        readonly=True,
        index=True,
        ondelete='restrict',
    )
    analytic_id = fields.Many2one("account.analytic.account", string="Brand", store=True,
                                  domain=[('snk_brand', '=', True)], related='product_id.analytic_id')
    event_ids = fields.Many2many("project.project",
        "sale_order_line_project_rel",  # nama tabel relasi buatan
        "order_line_id",
        "project_id",
        copy=True, string="Events")
    snk_discount_amount = fields.Monetary(
        string='Disc. Amount',
        store=True,
        currency_field='currency_id',
        aggregator=None,
        compute='_compute_snk_discount_amount',
        readonly=False,
    )
    template_code = fields.Char(
        string="Reference",
        compute="_compute_template_code"
    )

    @api.onchange('product_id')
    def _onchange_product_id_snk_grade(self):
        for line in self:
            line.snk_grade_id = line.product_id.snk_grade_id

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
            if vals.get('product_id') and 'snk_grade_id' not in vals:
                vals['snk_grade_id'] = grade_by_product.get(vals['product_id']) or False
        return super().create(vals_list)

    def write(self, vals):
        # Keep the snapshot aligned when an editable sales line changes product.
        if vals.get('product_id') and 'snk_grade_id' not in vals:
            product = self.env['product.product'].browse(vals['product_id'])
            vals['snk_grade_id'] = product.snk_grade_id.id or False
        return super().write(vals)

    @api.depends('product_id')
    def _compute_template_code(self):
        for rec in self:
            rec.template_code = rec.product_id.product_tmpl_id.default_code or ''

    # @api.depends('product_uom_qty', 'price_unit', 'discount')
    # def _compute_snk_discount_amount(self):
    #     for line in self:
    #         if line.discount:
    #             line.snk_discount_amount = (line.price_unit * line.discount) / 100
    #             _logger.info(f">>> Computed discount amount: {line.snk_discount_amount}")
            # else:
            #     line.snk_discount_amount = 0.0 #jadi 0 lagi dia vin

    @api.depends('event_ids')
    def _compute_analytic_distribution(self):
        super()._compute_analytic_distribution()
        for line in self:
            if line.display_type:
                continue

            distributions = line.analytic_distribution

            for project in line.event_ids:
                distributions.update(project._get_analytic_distribution())
            if line.event_ids:
                line.analytic_distribution = distributions

    def _prepare_procurement_values(self, group_id=False):
        values = super(SaleOrderLine, self)._prepare_procurement_values(group_id)
        values['event_ids'] = self.event_ids
        values['snk_grade_id'] = self.snk_grade_id.id or False
        values['_snk_grade_product_id'] = self.product_id.id
        # scheduled_date delivery mengikuti order date SO (bukan date_order + lead time).
        # date_planned -> stock.move.date -> stock.picking.scheduled_date (= min move.date).
        if self.order_id.date_order:
            values['date_planned'] = self.order_id.date_order
        return values

    def _prepare_base_line_for_taxes_computation(self, **kwargs):
        self.ensure_one()

        price_unit = self.price_unit
        discount = self.discount or 0.0

        if self.snk_discount_amount:
            discount = 0.0
            discount_per_unit = self.snk_discount_amount
            price_unit = self.price_unit - discount_per_unit

        return super()._prepare_base_line_for_taxes_computation(
            **{
                'price_unit': price_unit,
                'discount': discount,
                **kwargs,
            }
        )

    @api.depends('product_uom_qty', 'price_unit', 'tax_id', 'snk_discount_amount', 'discount')
    def _compute_amount(self):
        for line in self:
            price_unit = line.price_unit or 0.0
            qty = line.product_uom_qty or 0.0

            # 1. tentukan diskon per unit
            if line.snk_discount_amount:  # prioritas nominal
                discount_per_unit = line.snk_discount_amount
            elif line.discount:  # fallback persen
                discount_per_unit = (price_unit * line.discount) / 100
            else:
                discount_per_unit = 0.0

            # 2. hitung price efektif PER UNIT
            effective_price = price_unit - discount_per_unit

            # 3. compute pajak seperti biasa
            taxes = line.tax_id.compute_all(
                effective_price,
                line.order_id.currency_id,
                qty,
                product=line.product_id,
                partner=line.order_id.partner_id
            )

            _logger.info(f">>> Computed discount amount: {discount_per_unit}, discount: {line.discount}, price unit {effective_price}")
            _logger.info(f">>> Computed total_included: {taxes['total_included']}, total_excluded: {taxes['total_excluded']}")
            # 4. update hasil subtotal
            line.update({
                'price_tax': taxes['total_included'] - taxes['total_excluded'],
                'price_total': taxes['total_included'],
                'price_subtotal': taxes['total_excluded'],
            })

    # --- INVOICE LINE ---
    def _prepare_invoice_line(self, **kwargs):
        """Prepare invoice line with net unit price if snk_discount_amount is used."""
        self.ensure_one()
        res = super()._prepare_invoice_line(**kwargs)
        if self.snk_discount_amount:
            res.update({
                'price_unit': self.price_unit - self.snk_discount_amount,
                'discount': 0.0,
            })
        return res

