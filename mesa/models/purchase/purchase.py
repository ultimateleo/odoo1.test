# Copyright 2017 ForgeFlow S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.osv import expression

import logging
_logger = logging.getLogger(__name__)


class PurchaseOrder(models.Model):
    _name = "purchase.order"
    _inherit = ["purchase.order", "tier.validation"]
    _state_from = ["draft", "sent", "to approve"]
    _state_to = ["purchase", "approved"]

    _tier_validation_manual_config = False

    snk_purchase_type_id = fields.Many2one(
        "snk.purchase.type", string="Purchase Type")
    snk_po_number = fields.Char("PO Number")
    analytic_account_ids = fields.Many2many("account.analytic.account", string="Brands", copy=True,
        domain=lambda self: 
            [('snk_brand', '=', True)] if self.env.user.has_group('purchase.group_purchase_manager')
        else [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids)
        ],
        tracking=True)
    snk_approve = fields.Text("Approver")

    snk_site_id = fields.Many2one('snk.site', string='Site', help="Site",
        # domain= "[('snk_state', '=', 'open')]"
        domain=lambda self: 
            [('snk_state', '=', 'open')] if self.env.user.has_group('purchase.group_purchase_manager')
        else [
            ('snk_state', '=', 'open'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids)
        ]
    )
    snk_site_name = fields.Char(
        string='Site Name', related='snk_site_id.snk_name')
    snk_site_warehouse_id = fields.Many2one(
        'stock.warehouse', related='snk_site_id.warehouse_id', store=True)

    ### Onchange methods
    @api.onchange('snk_site_id')
    def _onchange_snk_site_id(self):
        if self.snk_site_id:
            self.picking_type_id = self._get_picking_type(
                self.company_id.id, site=self.snk_site_id)
       
    ### Computed methods
    @api.model
    def _get_picking_type(self, company_id, site=None):
        if site or self.env.context.get('default_snk_site_id'):
            if not site:
                site = self.env['snk.site'].sudo().browse(
                    self.env.context.get('default_snk_site_id'))
            picking_type = self.env['stock.picking.type'].search(
                [('code', '=', 'incoming'), ('warehouse_id', '=', site.warehouse_id.id)])
            return picking_type[:1]
        else:
            return super()._get_picking_type(company_id)

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

    ### Action methods
    @api.model
    def _prepare_site_picking_type_values(self, vals):
        """Keep site and receipt operation type consistent outside onchange."""
        vals = dict(vals)
        site_id = vals.get('snk_site_id')
        if not site_id:
            site_id = self.default_get(['snk_site_id']).get('snk_site_id')
            if site_id:
                vals['snk_site_id'] = site_id
        if not site_id:
            return vals

        site = self.env['snk.site'].browse(site_id)
        company_id = (
            vals.get('company_id')
            or site.company_id.id
            or self.env.company.id
        )
        picking_type = self._get_picking_type(company_id, site=site)
        if not picking_type:
            raise ValidationError(_(
                "No incoming operation type is configured for warehouse "
                "%(warehouse)s from site %(site)s.",
                warehouse=site.warehouse_id.display_name,
                site=site.display_name,
            ))
        vals['picking_type_id'] = picking_type.id
        return vals

    def _is_purchase_administrator(self):
        """Keep the role check centralized and explicit for security decisions."""
        return self.env.user.has_group('purchase.group_purchase_manager')

    def _check_purchase_access_security(self):
        """Validate PO access and product-brand integrity outside the UI layer."""
        is_administrator = self._is_purchase_administrator()
        current_user = self.env.user
        allowed_brand_ids = set(current_user.allowed_analytic_ids.ids)
        allowed_industry_ids = set(current_user.allowed_industry_ids.ids)

        for order in self:
            header_brand_ids = set(order.analytic_account_ids.ids)
            if not header_brand_ids:
                raise ValidationError(_("At least one PO brand is required."))
            if not order.snk_site_id:
                raise ValidationError(_("A PO site is required."))

            order._check_purchase_company_integrity()

            if not is_administrator:
                is_owner = (
                    order.create_uid == current_user
                    or order.user_id == current_user
                )
                if not is_owner:
                    raise AccessError(_(
                        "You can only access purchase orders that you created "
                        "or for which you are the assigned buyer."
                    ))

                if (
                    not order.snk_site_id
                    or order.snk_site_id.industry_id.id not in allowed_industry_ids
                ):
                    raise AccessError(_(
                        "The selected site is outside your allowed industries."
                    ))

                invalid_brand_ids = header_brand_ids - allowed_brand_ids
                if invalid_brand_ids:
                    invalid_brands = order.analytic_account_ids.filtered(
                        lambda brand: brand.id in invalid_brand_ids
                    )
                    raise AccessError(_(
                        "You do not have access to all PO brands: %(brands)s",
                        brands=", ".join(invalid_brands.mapped('display_name')),
                    ))

            order._check_assigned_buyer_authority()
            order._check_purchase_line_brand_integrity()

    def _check_purchase_company_integrity(self):
        for order in self:
            site = order.snk_site_id
            if site.company_id and site.company_id != order.company_id:
                raise ValidationError(_(
                    "PO site %(site)s belongs to company %(site_company)s, not "
                    "PO company %(po_company)s.",
                    site=site.display_name,
                    site_company=site.company_id.display_name,
                    po_company=order.company_id.display_name,
                ))

            if (
                site.warehouse_id
                and site.warehouse_id.company_id != order.company_id
            ):
                raise ValidationError(_(
                    "Warehouse %(warehouse)s for site %(site)s does not belong "
                    "to PO company %(company)s.",
                    warehouse=site.warehouse_id.display_name,
                    site=site.display_name,
                    company=order.company_id.display_name,
                ))

            if (
                order.picking_type_id
                and order.picking_type_id.warehouse_id != site.warehouse_id
            ):
                raise ValidationError(_(
                    "The PO operation type must use warehouse %(warehouse)s "
                    "from the selected site.",
                    warehouse=site.warehouse_id.display_name,
                ))

            invalid_company_brands = order.analytic_account_ids.filtered(
                lambda brand: brand.company_id and brand.company_id != order.company_id
            )
            if invalid_company_brands:
                raise ValidationError(_(
                    "These PO brands belong to another company: %(brands)s",
                    brands=", ".join(
                        invalid_company_brands.mapped('display_name')
                    ),
                ))

    def _check_assigned_buyer_authority(self):
        for order in self.filtered('user_id'):
            buyer = order.user_id
            if buyer.has_group('purchase.group_purchase_manager'):
                continue
            if not buyer.has_group('purchase.group_purchase_user'):
                raise ValidationError(_(
                    "Assigned buyer %(buyer)s must have the Purchase User role.",
                    buyer=buyer.display_name,
                ))

            header_brand_ids = set(order.analytic_account_ids.ids)
            buyer_brand_ids = set(buyer.allowed_analytic_ids.ids)
            if not header_brand_ids.issubset(buyer_brand_ids):
                raise ValidationError(_(
                    "Assigned buyer %(buyer)s does not have access to all PO brands.",
                    buyer=buyer.display_name,
                ))

            if order.snk_site_id.industry_id not in buyer.allowed_industry_ids:
                raise ValidationError(_(
                    "Assigned buyer %(buyer)s does not have access to the PO site industry.",
                    buyer=buyer.display_name,
                ))

            if order.company_id not in buyer.company_ids:
                raise ValidationError(_(
                    "Assigned buyer %(buyer)s does not have access to company %(company)s.",
                    buyer=buyer.display_name,
                    company=order.company_id.display_name,
                ))

    def _check_purchase_line_brand_integrity(self):
        for order in self:
            header_brand_ids = set(order.analytic_account_ids.ids)
            for line in order.order_line.filtered(lambda item: not item.display_type):
                product_brand = line.product_id.analytic_id
                if not product_brand:
                    raise ValidationError(_(
                        "Product %(product)s must have a brand before it can be "
                        "added to purchase order %(order)s.",
                        product=line.product_id.display_name,
                        order=order.display_name,
                    ))
                if product_brand.id not in header_brand_ids:
                    raise ValidationError(_(
                        "Brand %(brand)s for product %(product)s is not included "
                        "in the PO header brands.",
                        brand=product_brand.display_name,
                        product=line.product_id.display_name,
                    ))
                if line.analytic_account_id != product_brand:
                    raise ValidationError(_(
                        "The line brand for product %(product)s must match its "
                        "product brand %(brand)s.",
                        product=line.product_id.display_name,
                        brand=product_brand.display_name,
                    ))

    @api.constrains('snk_site_id', 'analytic_account_ids', 'order_line', 'user_id')
    def _constrains_purchase_access_security(self):
        # The complete validation is executed explicitly at the end of
        # purchase.order.create(). During that create, relational fields can
        # still be incomplete when ORM constraints are triggered.
        if self.env.context.get('snk_skip_po_line_security_check'):
            return
        # During a native one2many import, Odoo can temporarily create the
        # parent before all relational values are attached. The constraint
        # will run again when the mapped brand/header values are written.
        if self.env.context.get('import_file'):
            self.filtered('analytic_account_ids')._check_purchase_access_security()
            return
        self._check_purchase_access_security()

    @api.model_create_multi
    def create(self, vals_list):
        vals_list = [
            self._prepare_site_picking_type_values(vals)
            for vals in vals_list
        ]
        for vals in vals_list:
            if not vals.get("snk_po_number"):
                type_id = vals.get("snk_purchase_type_id")

                if type_id:
                    type = self.env["snk.purchase.type"].browse(type_id)
                    if type.snk_sequence_id:
                        vals["snk_po_number"] = type.snk_sequence_id.next_by_id()

        # Creating a PO with inline order lines can invoke the line-level
        # security check before all parent relational fields (notably brands)
        # have finished being persisted. Defer that nested check and validate
        # the complete order immediately after the parent create finishes.
        orders = super(
            PurchaseOrder,
            self.with_context(snk_skip_po_line_security_check=True),
        ).create(vals_list)
        if not self.env.context.get('import_file'):
            orders._check_purchase_access_security()
        return orders

    def write(self, vals):
        if vals.get('snk_site_id'):
            vals = self._prepare_site_picking_type_values(vals)
        for record in self:
            if "snk_po_number" in vals and record.snk_po_number:
                raise ValidationError(
                    "Nomor PO tidak dapat diubah setelah terbentuk.")

            if vals.get("snk_purchase_type_id"):
                type = self.env["snk.purchase.type"].browse(
                    vals["snk_purchase_type_id"])
                if type.snk_sequence_id:
                    vals["snk_po_number"] = type.snk_sequence_id.next_by_id()

        return super(PurchaseOrder, self).write(vals)

    def action_rfq_send(self):
        self._check_purchase_access_security()
        return super().action_rfq_send()

    def button_confirm(self):
        self._check_purchase_access_security()
        return super().button_confirm()

    def button_approve(self, force=False):
        self._check_purchase_access_security()
        return super().button_approve(force=force)

    def action_create_invoice(self):
        self._check_purchase_access_security()
        return super().action_create_invoice()

    def _prepare_invoice(self):
        self.ensure_one()
        result = super(PurchaseOrder, self)._prepare_invoice()
        result.update({
            'snk_po_number': self.snk_po_number,
            'snk_pr_id': self.requisition_id
        })
        return result

    @api.onchange('requisition_id')
    def _onchange_requisition_id(self):
        super(PurchaseOrder, self)._onchange_requisition_id()

        if self.requisition_id:
            for line in self.requisition_id.line_ids:
                qty = line.product_qty or 0

                for po_line in self.order_line.filtered(lambda x: x.snk_requisition_line_id == line):
                    po_line.update({
                        'product_qty': qty
                    })

    def snk_action_multi_product(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Select multiple product"),
            'res_model': 'snk.purchase.multi.product.wizard',
            'target': 'new',
            'views': [(self.env.ref('mesa.snk_purchase_multi_product_wizard_form').id, 'form')],
            'context': {
                'default_purchase_id': self.id,
            },
        }

    @api.model
    def default_get(self, fields):
        res = super().default_get(fields)

        if self.env.context.get('active_model') == 'purchase.requisition' and self.env.context.get('active_id'):
            pr = self.env['purchase.requisition'].browse(
                self.env.context['active_id'])
            if pr and pr.reference:
                res.pop('origin', False)
                res['origin'] = pr.reference.strip()
        return res

    def _prepare_picking(self):
        vals = super()._prepare_picking()
        # vals['origin'] = self.origin if self.origin else self.name
        # vals['analytic_ids'] = self.analytic_account_ids if self.analytic_account_ids else None
        # return vals
        vals['origin'] = self.origin if self.origin else self.name
        if self.analytic_account_ids:
            vals['analytic_ids'] = [(6, 0, self.analytic_account_ids.ids)]
        if self.snk_site_id:
            vals['snk_site_id'] = self.snk_site_id.id
        _logger.info(f"Picking receiver Site({vals['snk_site_id']}), origin({vals['origin']}), brands({vals['analytic_ids']}) in picking values")
        return vals

    # ============= CATALOG ============

    def _update_order_line_info(self, product_id, quantity, **kwargs):
        price_unit = super()._update_order_line_info(product_id, quantity, **kwargs)

        pol = self.order_line.filtered(
            lambda line: line.product_id.id == product_id)

        product = self.env['product.product'].browse(product_id)
        brand = product.analytic_id
        material = product.snk_material_code

        if pol:
            pol.analytic_account_id = brand.id
            pol.snk_material_code = material

        return price_unit

    def _name_search(self, name='', args=None, operator='ilike', limit=100, order=None):
        """Override _name_search untuk support search by custom PO number (snk_po_number)"""
        args = args or []

        if name and operator in ('ilike', 'like', '=', '=like', '=ilike'):
            # Search di snk_po_number, name, dan partner_ref
            domain = [
                '|', '|',
                ('snk_po_number', operator, name),
                ('name', operator, name),
                ('partner_ref', operator, name)
            ]

            # Gabungkan dengan args
            if args:
                domain = expression.AND([domain, args])

            # Return hasil search
            return self._search(domain, limit=limit, order=order)

        return super()._name_search(name=name, args=args, operator=operator, limit=limit, order=order)
    
    def action_update_source_document(self):
        for record in self:
            if record.picking_ids:
                record.picking_ids.write({
                    'origin': record.origin,
                })
            _logger.info(f"Picking receiver origin is updated to {record.picking_ids.origin} in picking values")
