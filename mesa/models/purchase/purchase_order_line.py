from odoo import _, api, fields, models


class PurchaseOrderLine(models.Model):
    _inherit = "purchase.order.line"

    snk_description = fields.Text(string="Description")
    snk_requisition_line_id = fields.Many2one(
        'purchase.requisition.line', string='PR Ref Line')
    snk_material_code = fields.Char(string="Material Code")
    analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Brands", readonly=True)
    snk_remaining_order = fields.Float("Remaining", readonly=False)
    product_product_id = fields.Integer("Product_id", related='product_id.id')
    template_code = fields.Char(
        string="Reference",
        compute="_compute_template_code"
    )

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [self._sync_product_brand_values(vals) for vals in vals_list]
        lines = super().create(vals_list)
        if not self.env.context.get('snk_skip_po_line_security_check'):
            orders = lines.mapped('order_id')
            if self.env.context.get('import_file'):
                orders = orders.filtered('analytic_account_ids')
            orders._check_purchase_access_security()
        return lines

    def write(self, vals):
        vals = self._sync_product_brand_values(vals, lines=self)
        result = super().write(vals)
        orders = self.mapped('order_id')
        if not self.env.context.get('snk_skip_po_line_security_check'):
            if self.env.context.get('import_file'):
                orders = orders.filtered('analytic_account_ids')
            orders._check_purchase_access_security()
        return result

    @api.model
    def _sync_product_brand_values(self, vals, lines=None):
        """Make the product master the authority for the PO line brand."""
        vals = dict(vals)
        if vals.get('display_type'):
            vals['analytic_account_id'] = False
            return vals

        product = False
        if vals.get('product_id'):
            product = self.env['product.product'].browse(vals['product_id'])
        elif lines and len(lines) == 1:
            product = lines.product_id

        if product:
            if not product.analytic_id:
                raise ValidationError(_(
                    "Product %(product)s must have a brand before it can be "
                    "added to a purchase order.",
                    product=product.display_name,
                ))
            vals['analytic_account_id'] = product.analytic_id.id
        return vals

    @api.depends('product_id')
    def _compute_template_code(self):
        for rec in self:
            rec.template_code = rec.product_id.product_tmpl_id.default_code or ''

    def _prepare_account_move_line(self):
        self.ensure_one()
        result = super(PurchaseOrderLine, self)._prepare_account_move_line()
        result.update({
            'snk_description': self.snk_description,
            'snk_material_code': self.snk_material_code,
        })
        return result

    @api.depends('product_qty', 'product_uom', 'company_id', 'order_id.partner_id')
    def _compute_price_unit_and_date_planned_and_name(self):
        for rec in self:
            return super(PurchaseOrderLine, rec)._compute_price_unit_and_date_planned_and_name()

    @api.onchange('product_id')
    def _onchange_product(self):
        for rec in self:
            brand = False
            if rec.product_id:
                brand = rec.product_id.analytic_id
            rec.analytic_account_id = brand
