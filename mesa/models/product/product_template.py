from odoo import _, api, fields, models
from odoo.osv import expression
import itertools
from odoo.exceptions import AccessError

class ProductTemplate(models.Model):
    _inherit = "product.template"

    analytic_id = fields.Many2one("account.analytic.account", string="Brand", copy=True,
                                  domain=[('snk_brand', '=', True)], tracking=True)
    snk_grade_id = fields.Many2one(
        "snk.product.grade", string="Grade", copy=True, index=True, tracking=True)
    snk_material_code = fields.Char(
        string="Material Code", copy=True, tracking=True)
    type = fields.Selection(tracking=True)
    invoice_policy = fields.Selection(tracking=True)
    is_storable = fields.Boolean(tracking=True)
    taxes_id = fields.Many2many(tracking=True)
    supplier_taxes_id = fields.Many2many(tracking=True)
    categ_id = fields.Many2one(tracking=True)
    default_code = fields.Char(
        tracking=True, compute=False, inverse=False, store=True)
    barcode = fields.Char(tracking=True)

    snk_normal_price = fields.Float(
        'Normal Price', default=1.0,
        digits='Product Price',
        tracking=True)
    # NOTE: sengaja TIDAK memakai groups= di level field ORM.
    # groups= pada field ORM memblokir READ di seluruh backend (bukan cuma sembunyi di view),
    # sehingga proses internal yang membaca cost (scrap, valuation stock.move) ikut AccessError
    # untuk user non-cost. Penyembunyian cost dari layar sudah dijaga di level VIEW
    # (groups="mesa.group_snk_view_cost_price" pada field standard_price di semua view),
    # dan proteksi export dijaga di export_data(). Lihat product.py.
    standard_price = fields.Float(digits='Product Price')

    def _is_product_create_blocked(self):
        """Cek apakah user ini diblokir dari create product"""
        if self.env.su:
            return False
        return (
            self.env.user.has_group('mesa.group_admin_gudang_view_history')
            or (
                self.env.user.has_group('stock.group_stock_user')
                and not self.env.user.has_group('stock.group_stock_manager')
            )
        )

    @api.model
    def get_views(self, views, options=None):
        res = super().get_views(views, options)
        if self._is_product_create_blocked():
            from lxml import etree
            for view_type, view_data in res.get('views', {}).items():
                if view_type in ('kanban', 'list', 'form') and 'arch' in view_data:
                    arch_tree = etree.fromstring(view_data['arch'])
                    arch_tree.set('create', '0')
                    view_data['arch'] = etree.tostring(arch_tree, encoding='unicode')
        return res

    @api.model
    def check_access_rights(self, operation, raise_exception=True):
        if operation == 'create' and self._is_product_create_blocked():
            if raise_exception:
                raise AccessError(_(
                    "Akses Ditolak: Anda tidak diizinkan membuat Produk baru. "
                    "Silakan hubungi Administrator."
                ))
            return False
        return super().check_access_rights(operation, raise_exception=raise_exception)

    def export_data(self, fields_to_export):
        if not self.env.user.has_group('mesa.group_snk_view_cost_price'):
            if 'standard_price' in fields_to_export:
                raise AccessError(_("You don't have permission to export standard price"))
        return super(ProductTemplate, self).export_data(fields_to_export)

    _sql_constraints = [
        ('uniq_product_template_default_code',
         'unique(default_code)', 'Reference already exists.'),
    ]

    @api.model_create_multi
    def create(self, vals_list):
        templates = super().create(vals_list)

        # Update variants setelah template dibuat
        for template in templates:
            if template.product_variant_ids:
                update_vals = {}

                if template.analytic_id:
                    update_vals['analytic_id'] = template.analytic_id.id
                if template.snk_material_code:
                    update_vals['snk_material_code'] = template.snk_material_code
                if template.snk_grade_id:
                    update_vals['snk_grade_id'] = template.snk_grade_id.id
                if template.snk_normal_price:
                    update_vals['snk_normal_price'] = template.snk_normal_price

                if update_vals:
                    template.product_variant_ids.write(update_vals)

        return templates

    def write(self, vals):
        res = super().write(vals)

        for rec in self:
            update_vals = {}

            if 'analytic_id' in vals:
                update_vals['analytic_id'] = rec.analytic_id.id

            if 'snk_material_code' in vals:
                update_vals['snk_material_code'] = rec.snk_material_code

            # Hanya kalau grade diubah di TEMPLATE
            if 'snk_grade_id' in vals:
                rec.product_variant_ids.with_context(from_template=True).write({
                    'snk_grade_id': rec.snk_grade_id.id,
                    'snk_grade_manual': False
                })

            if 'snk_normal_price' in vals:
                update_vals['snk_normal_price'] = rec.snk_normal_price

            if update_vals:
                rec.product_variant_ids.write(update_vals)

        if 'attribute_line_ids' in vals:
            self._add_value_to_variants()
            for template in self:
                template.product_variant_ids.action_generate_reference()

        return res

    def _compute_template_field_from_variant_field(self, fname, default=False):
        super()._compute_template_field_from_variant_field(fname, default)
        if fname == 'standard_price':
            for template in self:
                if template.product_variant_ids:
                    # ambil varian pertama yang punya cost > 0, kalau tidak ada pakai template sendiri
                    variant_with_cost = template.product_variant_ids.filtered(
                        lambda v: v.standard_price > 0)[:1]
                    if variant_with_cost:
                        template[fname] = variant_with_cost.standard_price
                    elif template[fname]:
                        template[fname] = template[fname]  # biarkan tetap
                    else:
                        template[fname] = default
                else:
                    template[fname] = default

    def _set_product_variant_field(self, fname):
        super()._set_product_variant_field(fname)
        if fname == 'standard_price':
            for template in self:
                if len(template.product_variant_ids) > 1:
                    for variant in template.product_variant_ids:
                        variant[fname] = template[fname]

    @api.model
    def _create_variant_ids(self):
        """Override untuk memastikan data template di-copy ke variant baru"""
        res = super()._create_variant_ids()

        # Copy data template ke variant baru yang dibuat
        for template in self:
            new_variants = template.product_variant_ids.filtered(
                lambda v: not v.analytic_id and template.analytic_id
            )

            if new_variants:
                update_vals = {}

                if template.analytic_id:
                    update_vals['analytic_id'] = template.analytic_id.id
                if template.snk_material_code:
                    update_vals['snk_material_code'] = template.snk_material_code
                if template.snk_grade_id:
                    update_vals['snk_grade_id'] = template.snk_grade_id.id
                if template.snk_normal_price:
                    update_vals['snk_normal_price'] = template.snk_normal_price
                if template.standard_price:
                    update_vals['standard_price'] = template.standard_price
                if template.default_code:
                    update_vals['default_code'] = template.default_code

                if update_vals:
                    new_variants.write(update_vals)
                
                # Trigger auto generate reference untuk varian baru
                new_variants.action_generate_reference()

        return res
        
    @api.model
    def _name_search(self, name='', domain=None, operator='ilike', limit=None, order=None):
        """
        Override _name_search to support searching by default_code (internal reference)
        This is used during imports and Many2one field searches
        Useful for products with variants where default_code is at template level
        """
        domain = domain or []

        if name:
            # Try to search by default_code first (internal reference)
            # This allows importing product templates using their internal reference
            templates = self._search(
                expression.AND([domain, [('default_code', '=', name)]]),
                limit=limit,
                order=order
            )

            # If no exact match on default_code, try other fields
            if not templates:
                templates = self._search(
                    expression.AND([
                        domain,
                        '|', '|',
                        ('default_code', operator, name),
                        ('name', operator, name),
                        ('barcode', operator, name)
                    ]),
                    limit=limit,
                    order=order
                )

            if templates:
                return templates

        return super()._name_search(name=name, domain=domain, operator=operator, limit=limit, order=order)

    def _add_value_to_variants(self):
        for template in self:
            for line in template.attribute_line_ids:
                if len(line.value_ids) == 1:
                    value = line.value_ids[0]
                    attr = line.attribute_id

                    ptav = self.env['product.template.attribute.value'].search([
                        ('product_tmpl_id', '=', template.id),
                        ('product_attribute_value_id', '=', value.id)
                    ], limit=1)

                    if not ptav:
                        continue

                    for variant in template.product_variant_ids:
                        variant.write({
                            'product_template_variant_value_ids': [(4, ptav.id)]
                        })
                        # print(variant.id)
                        # print(variant.name)
                        # has_value = variant.product_template_attribute_value_ids.filtered(
                        #     lambda v: v.product_attribute_value_id == value
                        # )
                        # if has_value:
                        #     print(ptav.id)
                        # variant.write({
                        #     'product_template_variant_value_ids': [(4, ptav.id)]
                        # })

    # def _ensure_variants(self):
    #     for template in self:
    #         if not template.attribute_line_ids:
    #             continue
    #
    #         all_values = [line.value_ids.ids for line in template.attribute_line_ids]
    #         combinations = list(itertools.product(*all_values))
    #
    #         existing_combinations = [
    #             set(v.product_template_attribute_value_ids.mapped('product_attribute_value_id').ids)
    #             for v in template.product_variant_ids
    #         ]
    #
    #         for combo in combinations:
    #             combo_set = set(combo)
    #             if combo_set not in existing_combinations:
    #                 template._create_variant_ids()

    # @api.depends('product_variant_ids.default_code')
    # def _compute_default_code(self):
    #     super()._compute_default_code()
    #
    #     for template in self:
    #         if not template.default_code and template.product_variant_ids:
    #             first_variant_code = template.product_variant_ids[0].default_code
    #             if first_variant_code:
    #                 template.default_code = first_variant_code.split('-')[0]

    # def _prepare_variant_values(self, combination):
    #     res = super()._prepare_variant_values(combination)
    #
    #     self.ensure_one()
    #     base_code = self.default_code or ''
    #     suffix = ''.join(combination.mapped('name')).replace(" ", "")
    #     res['default_code'] = f"{base_code}{suffix}" if suffix else base_code
    #
    #     return res
