# Copyright 2019 Ecosoft Co., Ltd. (http://ecosoft.co.th)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from odoo.osv import expression
from collections import defaultdict
from odoo.tools import float_is_zero


class PurchaseRequisition(models.Model):
    _name = "purchase.requisition"
    _inherit = ["purchase.requisition",
                "tier.validation", 'product.catalog.mixin',]
    _state_from = ["draft"]
    _state_to = ["ongoing", "in_progress"]

    _tier_validation_manual_config = False

    @api.model
    def _default_picking_type(self):
        return self._get_picking_type(self.env.context.get('company_id') or self.env.company.id)

    snk_purchase_type_id = fields.Many2one(
        "snk.purchase.type", string="Purchase Type", index=True)
    analytic_account_ids = fields.Many2many("account.analytic.account", string="Brands", index=True,
        domain=lambda self: 
            [('snk_brand', '=', True)] if self.env.user.has_group('purchase.group_purchase_manager')
        else [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids)
        ],
        tracking=True)
    snk_pr_number = fields.Char("PR Number")
    snk_approve = fields.Text("Approver")

    snk_site_id = fields.Many2one(
        'snk.site', string='Site', help="Site", index=True, domain= "[('snk_state', '=', 'open')]")
    snk_site_name = fields.Char(
        string='Site Name', related='snk_site_id.snk_name', store=True)
    snk_site_warehouse_id = fields.Many2one(
        'stock.warehouse', related='snk_site_id.warehouse_id', store=True, index=True)

    date_planned = fields.Date(
        string='Expected Arrival',
        default=fields.Date.context_today,
        help="Delivery date promised by vendor."
    )

    @api.onchange('snk_site_id')
    def _onchange_snk_site_id(self):
        self.picking_type_id = self._get_picking_type(
            self.company_id.id, site=self.snk_site_id)

    @api.model
    def _get_picking_type(self, company_id, site=None):
        if site:
            picking_type = self.env['stock.picking.type'].search(
                [('code', '=', 'incoming'), ('warehouse_id', '=', site.warehouse_id.id)])
            return picking_type[:1]
        else:
            picking_type = self.env['stock.picking.type'].search(
                [('warehouse_id.company_id', '=', company_id), ('code', '=', 'incoming')], limit=1)
            if not picking_type:
                self.env['stock.warehouse']._warehouse_redirect_warning()
            return picking_type

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if not vals.get("snk_pr_number"):
                pr_number = False
                type_id = vals.get("snk_purchase_type_id")

                if type_id:
                    type = self.env["snk.purchase.type"].browse(type_id)
                    if type.snk_sequence_id:
                        pr_number = type.snk_sequence_id.next_by_id()

                    else:
                        pr_number = type.snk_code + \
                            self.env["ir.sequence"].sudo().next_by_code(
                                "snk.default.type.number")

                vals["snk_pr_number"] = pr_number

        return super(PurchaseRequisition, self).create(vals_list)

    def write(self, vals):
        for record in self:
            if "snk_pr_number" in vals and record.snk_pr_number:
                raise ValidationError(
                    "Nomor PR tidak dapat diubah setelah terbentuk.")

            if vals.get("snk_purchase_type_id"):
                type = self.env["snk.purchase.type"].browse(
                    vals["snk_purchase_type_id"])
                if type.snk_sequence_id:
                    vals["snk_pr_number"] = type.snk_sequence_id.next_by_id()
                else:
                    vals["snk_pr_number"] = type.snk_code + \
                        self.env["ir.sequence"].sudo().next_by_code(
                            "snk.default.type.number")

        return super(PurchaseRequisition, self).write(vals)

    def snk_action_multi_product(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Select multiple product"),
            'res_model': 'snk.purchase.multi.product.wizard',
            'target': 'new',
            'views': [(self.env.ref('mesa.snk_purchase_multi_product_wizard_form').id, 'form')],
            'context': {
                'default_requisition_id': self.id,
            },
        }

    # -------------------------------------------------------------------------
    # CATALOG
    # -------------------------------------------------------------------------

    def action_add_from_catalog(self):
        res = super().action_add_from_catalog()
        # if res['context'].get('product_catalog_order_model') == 'purchase.requisition':
        #     res['search_view_id'] = [self.env.ref('repair.product_view_search_catalog').id, 'search']
        return res

    def _get_product_catalog_domain(self):
        domain = super()._get_product_catalog_domain()
        # Selalu filter purchase_ok yessssssssssss
        domain = expression.AND([domain, [('purchase_ok', '=', True)]])

        # Kalau ada brand yang dipilih, filter berdasarkan analytic_id okeee gasssssssssssssssssssssss
        brand_ids = self.analytic_account_ids.ids
        if brand_ids:
            domain = expression.AND(
                [domain, [('analytic_id', 'in', brand_ids)]])

        return domain

    # def _get_product_catalog_domain(self):
    #     return expression.AND([super()._get_product_catalog_domain(), [('purchase_ok', '=', True)]])

    def _default_order_line_values(self, child_field=False):
        default_data = super()._default_order_line_values(child_field)
        new_default_data = self.env['purchase.requisition.line']._get_product_catalog_lines_data(
            parent_record=self)

        return {**default_data, **new_default_data}

    def _get_product_catalog_order_data(self, products, **kwargs):
        product_catalog = super()._get_product_catalog_order_data(products, **kwargs)
        for product in products:
            product_catalog[product.id] |= self._get_product_price_and_data(
                product)
        return product_catalog

    def _get_product_price_and_data(self, product):
        self.ensure_one()
        return {'price': product.list_price}

    def _get_product_catalog_record_lines(self, product_ids, **kwargs):
        grouped_lines = defaultdict(
            lambda: self.env['purchase.requisition.line'])

        for line in self.line_ids:
            if line.product_id.id in product_ids:
                grouped_lines[line.product_id] |= line

        return grouped_lines

    def _update_order_line_info(self, product_id, quantity, **kwargs):
        line = self.line_ids.filtered(lambda e: e.product_id.id == product_id)
        price_unit = self.env['product.product'].browse(product_id).list_price
        if line:
            if quantity != 0:
                line.product_qty = quantity
            else:
                line.unlink()
        elif quantity > 0:
            line = self.env['purchase.requisition.line'].create({
                'requisition_id': self.id,
                'product_id': product_id,
                'product_qty': quantity,
                'price_unit': price_unit,
            })

        return price_unit

    def snk_action_print_requisition(self):
        return self.env.ref('mesa.snk_report_purchase_requisition').report_action(self)


class PurchaseRequisitionLine(models.Model):
    _inherit = "purchase.requisition.line"

    analytic_account_id = fields.Many2one(
        "account.analytic.account", string="Brand", related="product_id.analytic_id", index=True)

    snk_material_code = fields.Char(
        string="Material Code", compute="_compute_snk_material_code", readonly=False, store=True)
    snk_remaining_order = fields.Float("Remaining")
    product_product_id = fields.Integer("Product_id", related='product_id.id')
    template_code = fields.Char(
        string="Template Reference",
        related='product_id.product_tmpl_id.default_code',
        store=True,
    )
    variant_reference = fields.Char(
        string="Variant Reference",
        related='product_id.default_code',
        store=True,
    )

    # Field untuk import menggunakan internal reference
    product_default_code = fields.Char(
        string="Product Internal Reference",
        compute="_compute_product_default_code",
        inverse="_inverse_product_default_code",
        store=False,
        help="Use this field to import products by their internal reference (default_code)"
    )

    @api.depends('product_id')
    def _compute_product_default_code(self):
        for line in self:
            line.product_default_code = line.product_id.default_code or ''

    def _inverse_product_default_code(self):
        """
        Inverse function untuk mencari product berdasarkan default_code
        Digunakan saat import data
        """
        for line in self:
            if line.product_default_code and not line.product_id:
                # Cari product berdasarkan default_code (internal reference)
                product = self.env['product.product'].search([
                    ('default_code', '=', line.product_default_code),
                    ('active', '=', True)
                ], limit=1)

                if product:
                    line.product_id = product
                else:
                    # Jika tidak ditemukan, coba cari yang mirip atau raise error
                    raise ValueError(
                        f"Product dengan Internal Reference '{line.product_default_code}' tidak ditemukan. "
                        f"Pastikan product sudah dibuat dan memiliki Internal Reference yang benar."
                    )

    @api.model
    def _extract_records(self, fields, data, log=lambda a: None):
        """
        Override untuk handle import product menggunakan default_code (internal reference)
        """
        # Check jika ada field product_id yang berisi non-integer (kemungkinan default_code)
        if 'product_id' in fields:
            product_idx = fields.index('product_id')
            for row_idx, row in enumerate(data):
                if row[product_idx]:
                    product_val = row[product_idx]
                    # Jika bukan integer atau bukan string yang bisa diconvert ke integer
                    if isinstance(product_val, str) and not product_val.isdigit():
                        # Cari product berdasarkan default_code
                        product = self.env['product.product'].search([
                            ('default_code', '=', product_val),
                            ('active', '=', True)
                        ], limit=1)

                        if product:
                            # Replace dengan ID product
                            row[product_idx] = str(product.id)
                        else:
                            # Coba cari berdasarkan name jika default_code tidak ketemu
                            product = self.env['product.product'].search([
                                ('name', '=', product_val),
                                ('active', '=', True)
                            ], limit=1)
                            if product:
                                row[product_idx] = str(product.id)
                            else:
                                log(
                                    f"Row {row_idx + 1}: Product dengan Internal Reference atau Name '{product_val}' tidak ditemukan."
                                )

        return super()._extract_records(fields, data, log=log)

    @api.depends('product_id')
    def _compute_snk_material_code(self):
        for line in self:
            line.snk_material_code = line.product_id.snk_material_code or ""

    def _prepare_purchase_order_line(self, name, product_qty=0.0, price_unit=0.0, taxes_ids=False):
        self.ensure_one()
        res = super(PurchaseRequisitionLine, self)._prepare_purchase_order_line(
            name, product_qty, price_unit, taxes_ids)
        if self.product_id:
            res.update({
                'snk_description': self.product_description_variants,
                'snk_requisition_line_id': self.id,
                'snk_material_code': self.snk_material_code,
                'analytic_account_id': self.analytic_account_id,
                'snk_remaining_order': self.snk_remaining_order,
            })

        return res

    # -------------------------------------------------------------------------
    # CATALOG
    # -------------------------------------------------------------------------

    def action_add_from_catalog(self):
        requisition = self.env['purchase.requisition'].browse(
            self.env.context.get('order_id'))
        return requisition.with_context(child_field='line_ids').action_add_from_catalog()

    def _get_product_catalog_lines_data(self, parent_record=False, **kwargs):
        if not (parent_record and self):
            return {
                'quantity': 0,
            }
        self.product_id.ensure_one()
        return {
            **parent_record._get_product_price_and_data(self.product_id),
            'quantity': sum(
                self.mapped(
                    lambda line: line.product_uom_id._compute_quantity(
                        qty=line.product_qty,
                        to_unit=line.product_uom_id,
                    ),
                ),
            ),
            'readOnly': False,
        }
