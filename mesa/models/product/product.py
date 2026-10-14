from odoo import _, api, fields, models
from odoo.osv import expression
import logging

_logger = logging.getLogger(__name__)
from odoo.exceptions import AccessError


class ProductProduct(models.Model):
    _inherit = "product.product"

    analytic_id = fields.Many2one("account.analytic.account", string="Brand", copy=True,
                                  domain=[('snk_brand', '=', True)], tracking=True)
    snk_grade_id = fields.Many2one(
        "snk.product.grade", string="Grade", copy=True, tracking=True)
    snk_material_code = fields.Char(
        string="Material Code", copy=True, tracking=True)
    # NOTE: sengaja TIDAK memakai groups= di level field ORM untuk field cost.
    # groups= pada field ORM memblokir READ di seluruh backend (bukan cuma sembunyi di view),
    # sehingga proses internal yang membaca cost (scrap, valuation stock.move) ikut AccessError
    # untuk user non-cost. Penyembunyian cost dari layar dijaga di level VIEW
    # (groups="mesa.group_snk_view_cost_price"), dan proteksi export dijaga di export_data().
    avg_cost = fields.Monetary()
    total_value = fields.Monetary()
    standard_price = fields.Float(digits='Product Price')
    product_template_variant_value_ids = fields.Many2many('product.template.attribute.value',
                                                          relation='product_variant_combination',
                                                          domain=[
                                                              ('attribute_line_id.value_count', '>', 0)],
                                                          string="Variant Values", ondelete='restrict')

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
            # Field cost/price yang tidak boleh diekspor user non-cost.
            # Karena groups= sudah dilepas dari field ORM (agar scrap/valuation jalan),
            # proteksi export harus eksplisit di sini.
            blocked_cost_fields = {'standard_price', 'avg_cost', 'total_value'}
            if blocked_cost_fields.intersection(fields_to_export):
                raise AccessError(_("You don't have permission to export cost/price fields"))
        return super(ProductProduct, self).export_data(fields_to_export)

    _sql_constraints = [
        ('uniq_product_product_default_code',
         'unique(default_code)', 'Reference already exists.'),
    ]
    snk_grade_manual = fields.Boolean(string="Manual Grade", default=False)
    snk_normal_price = fields.Float(
        'Normal Price', default=1.0,
        digits='Product Price',
        tracking=True)

    @api.model_create_multi
    def create(self, vals_list):
        return super().create(vals_list)

    def write(self, vals):
        if 'snk_grade_id' in vals and not self.env.context.get('from_template'):
            vals['snk_grade_manual'] = True
        return super(ProductProduct, self).write(vals)

    def action_generate_reference(self):
        for rec in self:
            template_code = rec.product_tmpl_id.with_context(active_test=False).sudo().read(['default_code'])[0][
                'default_code'] or ''
            
            # Jika template code kosong, jangan generate apapun (sesuai request)
            if not template_code:
                continue

            variant_parts = []
            for val in rec.product_template_variant_value_ids:
                cleaned_val = val.name.replace(" ", "")
                variant_parts.append(cleaned_val)
            variant_str = "".join(variant_parts) if variant_parts else ""
            rec.default_code = f"{template_code}{variant_str}" if variant_str else template_code

    @api.model
    def _name_search(self, name='', domain=None, operator='ilike', limit=None, order=None):
        """
        Override _name_search to support searching by default_code (internal reference)
        This is used during imports and Many2one field searches
        """
        domain = domain or []

        if name:
            # Try to search by default_code first (internal reference) - EXACT MATCH
            # This allows importing products using their internal reference
            products = self._search(
                expression.AND([domain, [('default_code', '=', name)]]),
                limit=limit,
                order=order
            )

            # If exact match not found and name is a digit, try searching by ID
            if not products and isinstance(name, str) and name.isdigit():
                try:
                    products = self._search(
                        expression.AND([domain, [('id', '=', int(name))]]),
                        limit=limit,
                        order=order
                    )
                except (ValueError, TypeError):
                    pass

            # If no exact match on default_code, try other fields with the operator
            if not products:
                products = self._search(
                    expression.AND([
                        domain,
                        '|', '|', '|',
                        ('default_code', operator, name),
                        ('name', operator, name),
                        ('barcode', operator, name),
                        ('snk_material_code', operator, name)
                    ]),
                    limit=limit,
                    order=order
                )

            if products:
                return products

        return super()._name_search(name=name, domain=domain, operator=operator, limit=limit, order=order)

    @api.model
    def _rec_names_search(self, name='', domain=None, operator='ilike', limit=None, order=None):
        """
        Override _rec_names_search to support searching by default_code during imports
        This method is called when importing data with external identifiers
        """
        domain = domain or []

        if name and operator in ('=', 'ilike', '=ilike', 'like', '=like'):
            # First try exact match on default_code
            product_domain = expression.AND([domain, [('default_code', '=', name)]])
            product_ids = self._search(product_domain, limit=limit, order=order)

            if product_ids:
                return product_ids

            # If no exact match, try with the operator on multiple fields
            product_domain = expression.AND([
                domain,
                '|', '|', '|',
                ('default_code', operator, name),
                ('name', operator, name),
                ('barcode', operator, name),
                ('snk_material_code', operator, name)
            ])
            product_ids = self._search(product_domain, limit=limit, order=order)

            if product_ids:
                return product_ids

        return super()._rec_names_search(name=name, domain=domain, operator=operator, limit=limit, order=order)

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        domain = args.copy()
        positive_operators = ['=', 'ilike', '=ilike', 'like', '=like']

        if operator in positive_operators:
            # Cari berdasarkan default_code terlebih dahulu (internal reference)
            products = self.search_fetch(expression.AND([domain, [('default_code', operator, name)]]), ['display_name'], limit=limit)

            if not products:
                # Jika name adalah angka, coba cari berdasarkan ID
                if name and name.isdigit():
                    products = self.search_fetch(expression.AND([domain, [('id', operator, int(name))]]), ['display_name'], limit=limit)

            if not products:
                # Cari berdasarkan name
                products = self.search_fetch(expression.AND([domain, [('name', operator, name)]]), ['display_name'], limit=limit)

            if not products:
                # Cari berdasarkan barcode
                products = self.search_fetch(expression.AND([domain, [('barcode', operator, name)]]), ['display_name'], limit=limit)

            if not products:
                # Cari berdasarkan material code
                products = self.search_fetch(expression.AND([domain, [('snk_material_code', operator, name)]]), ['display_name'], limit=limit)

            if products:
                return [(p.id, p.display_name) for p in products]

        return super().name_search(name=name, args=args, operator=operator, limit=limit)

    @api.model
    def cron_run_fifo_vacuum(self, limit=30, lock_key=1122334455):
        """
        Ditingkatkan (Next Level):
        Cron untuk FIFO vacuum (dipisah dari inventory agar ringan)
        
        Optimasi:
        - Advisory lock (Anti-overlap)
        - Multi-company awareness
        - Commit per product
        - Silent mode (no chatter)
        - Sudo for system access
        """
        # 1. Advisory Lock
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        locked = self.env.cr.fetchone()[0]
        if not locked:
            _logger.warning("FIFO vacuum skipped: already running")
            return

        try:
            companies = self.env['res.company'].search([])
            for company in companies:
                # 2. Cari produk yang punya SVL untuk company ini via SQL (FIELD NON-STORED!)
                self.env.cr.execute("""
                    SELECT product_id 
                    FROM stock_valuation_layer 
                    WHERE company_id = %s AND remaining_qty != 0 
                    GROUP BY product_id 
                    LIMIT %s
                """, (company.id, limit))
                product_ids = [r[0] for r in self.env.cr.fetchall()]

                if not product_ids:
                    continue

                products = self.sudo().browse(product_ids)
                _logger.info("FIFO vacuum start | Company=%s | Products Found=%s", company.name, len(products))

                processed = 0
                for product in products:
                    try:
                        # Jalankan vacuum dalam silent mode (biar kencang)
                        product.sudo().with_context(
                            mail_notrack=True,
                            mail_create_nolog=True,
                            tracking_disable=True
                        )._run_fifo_vacuum(company)

                        # Commit per product agar progres gak hilang
                        self.env.cr.commit()
                        processed += 1
                        
                    except Exception:
                        self.env.cr.rollback()
                        _logger.exception("FIFO vacuum failed for product %s in %s", product.id, company.name)

                _logger.info("FIFO vacuum DONE | Company=%s | Processed=%s", company.name, processed)

        finally:
            # Release Lock
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
