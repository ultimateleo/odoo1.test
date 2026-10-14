from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError
from datetime import date, datetime
import io
import base64
import xlsxwriter
import logging
import time

_logger = logging.getLogger(__name__)


class PricelistItem(models.Model):
    _inherit = "product.pricelist.item"

    template_reference = fields.Char(
        "Template Reference", related="product_tmpl_id.default_code", store=True, readonly=True)
    variant_reference = fields.Char(
        "Variant Reference", related="product_id.default_code", store=True, readonly=True)
    snk_grade_id = fields.Many2one(
        "snk.product.grade", "Grade", compute="_compute_product_info", store=True)
    analytic_id = fields.Many2one(
        "account.analytic.account", "Brand", compute="_compute_product_info", store=True)
    product_active = fields.Boolean(
        "Product Active", related='product_id.active', store=True)
    product_template_active = fields.Boolean(
        "Product Template Active", related='product_tmpl_id.active', store=True)
    pricelist_active = fields.Boolean(
        "Pricelist Active", related="pricelist_id.active", store=True, readonly=True)
    active = fields.Boolean(
        "Active", default=True, help="If unchecked, it will allow you to hide the pricelist item without deleting it.")

    # Field untuk import - menerima Internal Reference dan auto cari product
    product_ref_import = fields.Char(
        "Product Reference (Import)",
        help="Field untuk import menggunakan Internal Reference. Isi dengan kode product (default_code) dan akan otomatis mencari product yang sesuai.")

    @api.depends("product_id", "product_tmpl_id")
    def _compute_product_info(self):
        for rec in self:
            if rec.product_id:
                rec.snk_grade_id = rec.product_id.snk_grade_id or rec.product_tmpl_id.snk_grade_id
                rec.analytic_id = rec.product_id.analytic_id or rec.product_tmpl_id.analytic_id
            elif rec.product_tmpl_id and not rec.product_id:
                rec.snk_grade_id = rec.product_tmpl_id.snk_grade_id
                rec.analytic_id = rec.product_tmpl_id.analytic_id
            else:
                rec.snk_grade_id = False
                rec.analytic_id = False

    def _find_product_by_reference(self, ref_code, get_all_variants=False, return_template=False):
        """Helper method untuk cari product berdasarkan reference code

        Args:
            ref_code: Internal Reference / default_code
            get_all_variants: Jika True dan ref_code adalah template, return semua variants
            return_template: Jika True, return tuple (products, template)

        Returns:
            product.product recordset (bisa single atau multiple tergantung get_all_variants)
            atau tuple (products, template) jika return_template=True
        """
        # Normalisasi ref_code: trim whitespace
        ref_code = ref_code.strip() if ref_code else ''

        _logger.info(f"=== SEARCH PRODUCT: ref_code='{ref_code}' ===")

        # 1. Cari di product variant dulu (case-insensitive)
        product = self.env['product.product'].search([
            ('default_code', '=ilike', ref_code)
        ], limit=1)

        _logger.info(f"=== SEARCH VARIANT: Found {len(product)} variant(s) ===")

        product_template = self.env['product.template']

        # 2. Kalau ga ketemu, cari di product template
        if not product:
            product_template = self.env['product.template'].search([
                ('default_code', '=ilike', ref_code)
            ], limit=1)

            _logger.info(f"=== SEARCH TEMPLATE: Found {len(product_template)} template(s) ===")

            if product_template:
                # Ambil SEMUA variant atau cuma pertama
                if get_all_variants:
                    products = self.env['product.product'].search([
                        ('product_tmpl_id', '=', product_template.id)
                    ])
                    _logger.info(
                        f"=== RESULT: Found template '{product_template.name}' with {len(products)} variant(s) ===")
                    if return_template:
                        return products, product_template
                    return products
                else:
                    product = self.env['product.product'].search([
                        ('product_tmpl_id', '=', product_template.id)
                    ], limit=1)
            else:
                _logger.warning(f"=== RESULT: Product/Template NOT FOUND for ref_code='{ref_code}' ===")

        if return_template:
            return product, product_template
        return product

    @api.model
    def create(self, vals):
        """Override create untuk handle import dengan product reference"""
        # Check if we should skip variant expansion (for recursive calls)
        skip_expansion = self.env.context.get('skip_variant_expansion', False)
        if skip_expansion:
            return super(PricelistItem, self).create(vals)

        if 'product_ref_import' in vals and vals.get('product_ref_import'):
            ref_code = vals['product_ref_import']

            # STEP 0: Cek dulu apakah ini variant specific atau template code
            # Cari variant dulu dengan ref_code
            specific_variant = self.env['product.product'].search([
                ('default_code', '=ilike', ref_code.strip())
            ], limit=1)

            if specific_variant:
                # Ini adalah VARIANT SPECIFIC CODE - create untuk variant ini saja
                _logger.info(f"=== IMPORT: Variant specific code '{ref_code}' - Creating single variant item ===")

                # Prepare vals untuk variant item
                variant_vals = vals.copy()
                del variant_vals['product_ref_import']
                variant_vals['applied_on'] = '0_product_variant'
                variant_vals['product_id'] = specific_variant.id
                # Force product_tmpl_id jadi False (kita cuma mau variant, bukan template)
                variant_vals['product_tmpl_id'] = False

                # Check existing variant item
                pricelist_id = vals.get('pricelist_id')
                date_start = vals.get('date_start')
                date_end = vals.get('date_end')

                existing_item = None
                if pricelist_id:
                    existing_item = self.search([
                        ('pricelist_id', '=', pricelist_id),
                        ('product_id', '=', specific_variant.id),
                        ('date_start', '=', date_start if date_start else False),
                        ('date_end', '=', date_end if date_end else False),
                    ], limit=1)

                if existing_item:
                    # Update existing
                    existing_item.write({
                        'fixed_price': vals.get('fixed_price', 0.0),
                        'compute_price': vals.get('compute_price', 'fixed'),
                    })
                    result_item = existing_item
                else:
                    # Create new
                    result_item = self.with_context(skip_variant_expansion=True).create(variant_vals)

                # Reset flag snk_is_synch
                if pricelist_id:
                    pricelist = self.env['product.pricelist'].browse(pricelist_id)
                    if hasattr(pricelist, 'snk_is_synch'):
                        pricelist.sudo().write({'snk_is_synch': False})

                return result_item

            # Jika bukan variant specific, berarti TEMPLATE CODE
            # Import hanya create template item, TIDAK create variant items
            _logger.info(f"=== IMPORT: Template code '{ref_code}' - Creating template item only ===")
            products, product_template = self._find_product_by_reference(
                ref_code, get_all_variants=True, return_template=True)

            if not products and not product_template:
                raise ValidationError(
                    f"Product dengan Internal Reference '{ref_code}' tidak ditemukan. "
                    f"Pastikan product sudah ada di database."
                )

            # Get common values
            pricelist_id = vals.get('pricelist_id')
            date_start = vals.get('date_start')
            date_end = vals.get('date_end')
            fixed_price = vals.get('fixed_price', 0.0)
            compute_price = vals.get('compute_price', 'fixed')

            created_items = self.env['product.pricelist.item']

            # STEP 1: Create template item ONLY (tidak create variants)
            if len(products) > 0:
                # Ada variants, ambil template dari variant pertama
                template = products[0].product_tmpl_id
            elif product_template:
                # Template ditemukan tapi tidak punya variants
                template = product_template
            else:
                template = None

            if template:
                template_vals = vals.copy()
                del template_vals['product_ref_import']
                template_vals['applied_on'] = '1_product'
                template_vals['product_tmpl_id'] = template.id
                # PENTING: Force product_id jadi False untuk template item
                template_vals['product_id'] = False

                # Check existing template item
                existing_template = None
                if pricelist_id:
                    existing_template = self.search([
                        ('pricelist_id', '=', pricelist_id),
                        ('product_tmpl_id', '=', template.id),
                        ('applied_on', '=', '1_product'),
                        ('date_start', '=', date_start if date_start else False),
                        ('date_end', '=', date_end if date_end else False),
                    ], limit=1)

                if existing_template:
                    existing_template.write({
                        'fixed_price': fixed_price,
                        'compute_price': compute_price,
                    })
                    created_items |= existing_template
                else:
                    template_item = self.with_context(
                        skip_variant_expansion=True).create(template_vals)
                    created_items |= template_item

            # STEP 2: TIDAK create variant items - hanya return template item
            _logger.info(
                f"=== IMPORT: Template with {len(products)} variant(s) - Returning template item only (no variant items created) ===")

            # Reset flag snk_is_synch di parent pricelist
            if pricelist_id:
                pricelist = self.env['product.pricelist'].browse(pricelist_id)
                if hasattr(pricelist, 'snk_is_synch'):
                    pricelist.sudo().write({'snk_is_synch': False})

            # Return first item (untuk kompatibilitas Odoo import)
            return created_items[0] if created_items else self.env['product.pricelist.item']

        # MANUAL CREATE: Handle variant creation
        if vals.get('product_id') and not vals.get('product_tmpl_id'):
            # User memilih variant secara manual - create variant item saja
            variant = self.env['product.product'].browse(vals['product_id'])
            _logger.info(f"=== MANUAL CREATE: Variant {variant.default_code} - Creating variant item only ===")

            # Set applied_on ke variant
            vals['applied_on'] = '0_product_variant'
            # Force product_tmpl_id jadi False
            vals['product_tmpl_id'] = False

            # Check existing variant item
            pricelist_id = vals.get('pricelist_id')
            date_start = vals.get('date_start')
            date_end = vals.get('date_end')

            existing_item = None
            if pricelist_id:
                existing_item = self.search([
                    ('pricelist_id', '=', pricelist_id),
                    ('product_id', '=', variant.id),
                    ('date_start', '=', date_start if date_start else False),
                    ('date_end', '=', date_end if date_end else False),
                ], limit=1)

            if existing_item:
                # Update existing
                existing_item.write({
                    'fixed_price': vals.get('fixed_price', 0.0),
                    'compute_price': vals.get('compute_price', 'fixed'),
                })
                result_item = existing_item
            else:
                # Create new variant item (skip expansion)
                result_item = self.with_context(skip_variant_expansion=True).create(vals)

            # Reset flag snk_is_synch
            if pricelist_id:
                pricelist = self.env['product.pricelist'].browse(pricelist_id)
                if hasattr(pricelist, 'snk_is_synch'):
                    pricelist.sudo().write({'snk_is_synch': False})

            return result_item

        # MANUAL CREATE: Handle template creation
        if vals.get('product_tmpl_id') and not vals.get('product_id'):
            template = self.env['product.template'].browse(
                vals['product_tmpl_id'])
            variants = self.env['product.product'].search([
                ('product_tmpl_id', '=', template.id)
            ])

            _logger.info(
                f"=== PRICELIST CREATE: Template {template.name} (ID: {template.id}) has {len(variants)} variants ===")

            # Get common values
            pricelist_id = vals.get('pricelist_id')
            date_start = vals.get('date_start')
            date_end = vals.get('date_end')
            fixed_price = vals.get('fixed_price', 0.0)
            compute_price = vals.get('compute_price', 'fixed')

            created_items = self.env['product.pricelist.item']

            # STEP 1: Create template item (untuk semua kasus - ada/tidak ada varian)
            template_vals = vals.copy()
            template_vals['applied_on'] = '1_product'
            template_vals['product_tmpl_id'] = template.id
            # PENTING: Force product_id jadi False untuk template item
            template_vals['product_id'] = False

            # Check existing template item
            existing_template = None
            if pricelist_id:
                existing_template = self.search([
                    ('pricelist_id', '=', pricelist_id),
                    ('product_tmpl_id', '=', template.id),
                    ('applied_on', '=', '1_product'),
                    ('date_start', '=', date_start if date_start else False),
                    ('date_end', '=', date_end if date_end else False),
                ], limit=1)

            if existing_template:
                existing_template.write({
                    'fixed_price': fixed_price,
                    'compute_price': compute_price,
                })
                created_items |= existing_template
            else:
                template_item = self.with_context(
                    skip_variant_expansion=True).create(template_vals)
                created_items |= template_item

            # STEP 2: TIDAK create variant items - hanya return template item saja
            # Manual creation hanya create generic/template item, tidak expand ke variants
            _logger.info(
                f"=== MANUAL CREATE: Template {template.name} with {len(variants)} variant(s) - Creating template item only ===")

            # Reset flag snk_is_synch di parent pricelist
            if pricelist_id:
                pricelist = self.env['product.pricelist'].browse(pricelist_id)
                if hasattr(pricelist, 'snk_is_synch'):
                    pricelist.sudo().write({'snk_is_synch': False})

            return created_items[0] if created_items else self.env['product.pricelist.item']

        result = super(PricelistItem, self).create(vals)

        # Reset flag snk_is_synch di parent pricelist karena ada item baru
        if result.pricelist_id and hasattr(result.pricelist_id, 'snk_is_synch'):
            result.pricelist_id.sudo().write({'snk_is_synch': False})

        return result

    def write(self, vals):
        """Override write untuk handle update dengan product reference"""
        if 'product_ref_import' in vals and vals.get('product_ref_import'):
            ref_code = vals['product_ref_import']
            product = self._find_product_by_reference(ref_code)

            if not product:
                raise ValidationError(
                    f"Product dengan Internal Reference '{ref_code}' tidak ditemukan. "
                    f"Pastikan product sudah ada di database."
                )

            vals['product_id'] = product.id
            del vals['product_ref_import']

        result = super(PricelistItem, self).write(vals)

        # Reset flag snk_is_synch di parent pricelist karena ada perubahan di item
        # Hanya reset jika ada perubahan field yang mempengaruhi sinkronisasi
        fields_to_check = ['product_id', 'product_tmpl_id', 'fixed_price', 'date_start',
                           'date_end', 'compute_price', 'currency_id', 'active']
        if any(field in vals for field in fields_to_check):
            for rec in self:
                if rec.pricelist_id and hasattr(rec.pricelist_id, 'snk_is_synch'):
                    rec.pricelist_id.sudo().write({'snk_is_synch': False})

        return result

    def _search_product_active(self, operator, value):
        """Custom search untuk filter berdasarkan status active product"""
        if operator == '=' and value:
            # Cari yang activenya True
            return ['|',
                    '&', ('product_id', '!=',
                          False), ('product_id.active', '=', True),
                    '&', ('product_id', '=', False), ('product_tmpl_id.active', '=', True)]
        elif operator == '=' and not value:
            # Cari yang activenya False (archived)
            return ['|',
                    '&', ('product_id', '!=',
                          False), ('product_id.active', '=', False),
                    '&', ('product_id', '=', False), ('product_tmpl_id.active', '=', False)]
        return []

    # def action_export_latest_items(self):
    #     """Export latest pricelist items to Excel - Include template and all variants"""
    #     today = datetime.today().replace(microsecond=0)

    #     # Ambil domain aktif dari list view
    #     domain = self.env.context.get('active_domain', [])

    #     # Fallback: jika tidak ada active_domain, ambil semua active items
    #     # if not domain:
    #     #     domain = [('pricelist_active', '=', True), ('product_active', '=', True)]

    #     records = self.env['product.pricelist.item'].search(domain)

    #     if not records:
    #         raise UserError(
    #             "Tidak ada data pricelist item yang ditemukan. "
    #             "Pastikan ada data di list view atau coba refresh halaman."
    #         )

    #     # Cari record terbaru per product (hanya template items dari list view)
    #     # Logic: Ambil item dengan start_date terbaru yang aktif hari ini
    #     self.env.cr.execute("""
    #         WITH ranked_items AS (
    #             SELECT
    #                 pli.id,
    #                 pli.date_start,
    #                 pli.date_end,
    #                 ROW_NUMBER() OVER (
    #                     PARTITION BY pli.pricelist_id,
    #                                  COALESCE(pli.product_id, 0),
    #                                  COALESCE(pli.product_tmpl_id, 0)
    #                     ORDER BY pli.date_start DESC NULLS LAST,
    #                              pli.id DESC
    #                 ) AS rn
    #             FROM product_pricelist_item pli
    #             WHERE pli.id = ANY(%s)
    #         )
    #         SELECT id
    #         FROM ranked_items
    #         WHERE rn = 1
    #           AND (date_start IS NULL OR date_start <= %s)
    #           AND (date_end IS NULL OR date_end >= %s)
    #     """, (list(records.ids), today, today))

    #     ids = [r[0] for r in self.env.cr.fetchall()]
    #     template_items = self.browse(ids)

    #     # Generate data untuk export: template + variants dari product template
    #     # Karena variant items tidak ada di pricelist (hanya template),
    #     # kita generate data untuk semua variants dari product template
    #     export_data = []

    #     for template_item in template_items:
    #         if not template_item.product_tmpl_id:
    #             continue

    #         # Ambil semua variants dari product template
    #         product_template = template_item.product_tmpl_id
    #         variants = self.env['product.product'].search([
    #             ('product_tmpl_id', '=', product_template.id),
    #             ('active', '=', True)
    #         ])

    #         # Jika ada variants (2+), export template + semua variants
    #         # Jika tidak ada atau cuma 1, export template saja
    #         if len(variants) > 1:
    #             # Export template item
    #             export_data.append({
    #                 'pricelist': template_item.pricelist_id.display_name or "",
    #                 'type': 'Template',
    #                 'template_ref': product_template.default_code or "",
    #                 'product_ref': product_template.default_code or "",
    #                 'product_name': product_template.name or "",
    #                 'barcode': "",
    #                 'category': product_template.categ_id.complete_name or "",
    #                 'brand': product_template.analytic_id.name or "",
    #                 'grade': product_template.snk_grade_id.snk_name or "",
    #                 'normal_price': product_template.snk_normal_price or 0.0,
    #                 'sales_price': template_item.fixed_price or 0.0,
    #                 'uom': template_item.product_uom or "",
    #                 'date_start': template_item.date_start.strftime("%b %d, %Y") if template_item.date_start else "",
    #                 'date_end': template_item.date_end.strftime("%b %d, %Y") if template_item.date_end else "",
    #             })

    #             # Export semua variants dengan harga dari template
    #             for variant in variants:
    #                 export_data.append({
    #                     'pricelist': template_item.pricelist_id.display_name or "",
    #                     'type': 'Variant',
    #                     'template_ref': product_template.default_code or "",
    #                     'product_ref': variant.default_code or "",
    #                     'product_name': variant.name or "",
    #                     'barcode': variant.barcode or "",
    #                     'category': variant.categ_id.complete_name or "",
    #                     'brand': variant.analytic_id.name or "",
    #                     'grade': variant.snk_grade_id.snk_name or "",
    #                     'normal_price': variant.snk_normal_price or 0.0,
    #                     'sales_price': template_item.fixed_price or 0.0,  # Pakai harga dari template
    #                     'uom': template_item.product_uom or "",
    #                     'date_start': template_item.date_start.strftime("%b %d, %Y") if template_item.date_start else "",
    #                     'date_end': template_item.date_end.strftime("%b %d, %Y") if template_item.date_end else "",
    #                 })
    #         else:
    #             # Product generic (tidak ada varian atau cuma 1), export template saja
    #             product = variants[0] if variants else product_template
    #             export_data.append({
    #                 'pricelist': template_item.pricelist_id.display_name or "",
    #                 'type': 'Template',
    #                 'template_ref': product_template.default_code or "",
    #                 'product_ref': product.default_code or "",
    #                 'product_name': product.name or "",
    #                 'barcode': getattr(product, 'barcode', "") or "",
    #                 'category': product.categ_id.complete_name or "",
    #                 'brand': product.analytic_id.name or "",
    #                 'grade': product.snk_grade_id.snk_name or "",
    #                 'normal_price': getattr(product, 'snk_normal_price', 0.0) or 0.0,
    #                 'sales_price': template_item.fixed_price or 0.0,
    #                 'uom': template_item.product_uom or "",
    #                 'date_start': template_item.date_start.strftime("%b %d, %Y") if template_item.date_start else "",
    #                 'date_end': template_item.date_end.strftime("%b %d, %Y") if template_item.date_end else "",
    #             })

    #     # Generate Excel
    #     output = io.BytesIO()
    #     workbook = xlsxwriter.Workbook(output, {'in_memory': True})
    #     sheet = workbook.add_worksheet("Latest Items")

    #     headers = ["Pricelist", "Type", "Template", "Reference", "Product", "Barcode", "Category",
    #                "Brand", "Grade", "Normal Price", "Sales Price", "UOM", "Start Date", "End Date", ]
    #     header_format = workbook.add_format(
    #         {'bold': True, 'bg_color': '#DCE6F1'})
    #     for col, header in enumerate(headers):
    #         sheet.write(0, col, header, header_format)

    #     row = 1
    #     for data in export_data:
    #         sheet.write(row, 0, data['pricelist'])
    #         sheet.write(row, 1, data['type'])
    #         sheet.write(row, 2, data['template_ref'])
    #         sheet.write(row, 3, data['product_ref'])
    #         sheet.write(row, 4, data['product_name'])
    #         sheet.write(row, 5, data['barcode'])
    #         sheet.write(row, 6, data['category'])
    #         sheet.write(row, 7, data['brand'])
    #         sheet.write(row, 8, data['grade'])
    #         sheet.write(row, 9, data['normal_price'])
    #         sheet.write(row, 10, data['sales_price'])
    #         sheet.write(row, 11, data['uom'])
    #         sheet.write(row, 12, data['date_start'])
    #         sheet.write(row, 13, data['date_end'])
    #         row += 1

    #     workbook.close()
    #     file_data = base64.b64encode(output.getvalue())
    #     output.close()

    #     # Buat attachment
    #     attachment = self.env['ir.attachment'].create({
    #         'name': f'pricelist_items_latest_{today}.xlsx',
    #         'type': 'binary',
    #         'datas': file_data,
    #         'res_model': 'product.pricelist.item',
    #         'res_id': 0,  # fix singleton (bukan self.id)
    #         'mimetype': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    #     })

    #     # Trigger download file
    #     return {
    #         'type': 'ir.actions.act_url',
    #         'url': f'/web/content/{attachment.id}?download=true',
    #         'target': 'self',
    #     }

    # Helper date function
    def _write_date(self, sheet, row, col, value, date_format):
        if not value:
            sheet.write(row, col, '')
            return
        # convert ke timezone user
        user_dt = fields.Datetime.context_timestamp(self, value).replace(tzinfo=None, microsecond=0)
        sheet.write_datetime(row, col, user_dt, date_format)

    def action_export_latest_items(self):
        """
        Entry point Action Server
        """

        _logger.info(">>>>> Action export latest pricelist dipanggil")
        output = io.BytesIO()

        workbook = xlsxwriter.Workbook(
            output,
            {'constant_memory': True}
        )
        sheet = workbook.add_worksheet('Latest Pricelist')
        date_format = workbook.add_format({'num_format': 'yyyy-mm-dd'})

        headers = [
            'Pricelist', 'Type', 'Template Ref', 'Product Ref',
            'Product Name', 'Barcode', 'Category', 'Brand', 'Grade',
            'Normal Price', 'Sales Price', 'UOM', 'Date Start', 'Date End'
        ]
        for col, header in enumerate(headers):
            sheet.write(0, col, header)

        # ================= CORE LOGIC =================
        self._export_latest_pricelist(workbook, sheet, date_format)
        # ==============================================

        workbook.close()
        output.seek(0)

        export_time = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"pricelist_{export_time}.xlsx"

        _logger.info(">>>>> Workbook selesai dibangun: %s", filename)

        # ================= DOWNLOAD ===================
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(output.getvalue()),
            'res_model': 'product.pricelist.item',
            'res_id': False,
            'mimetype': (
                'application/vnd.openxmlformats-officedocument.'
                'spreadsheetml.sheet'
            ),
        })

        _logger.info(
            ">>>>> Attachment dibuat untuk download: id=%s",
            attachment.id
        )

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }

    def _export_latest_pricelist(self, workbook, sheet, date_format):
        """
        Export latest pricelist items to Excel
        - SQL DISTINCT ON for latest rule
        - Overlap-safe
        - Variant override template
        - Output: template first, then variants
        - If only 1 variant -> template only
        """
        
        # ==========================================================
        # TIMER SETUP
        # ==========================================================
        t0 = time.perf_counter()
        step_time = t0

        def _log_step(name):
            nonlocal step_time
            now = time.perf_counter()
            _logger.info(
                ">>>>> STEP %-35s | +%.3fs | total %.3fs",
                name,
                now - step_time,
                now - t0
            )
            step_time = now

        _logger.info(">>>>> Mulai export latest pricelist (SQL DISTINCT ON)")

        today = datetime.today().replace(microsecond=0)

        # ==========================================================
        # STEP 0 — AMBIL DOMAIN DARI LIST VIEW
        # ==========================================================
        domain = self.env.context.get('active_domain', [])
        records = self.env['product.pricelist.item'].search(domain)

        if not records:
            raise UserError(
                "Tidak ada data pricelist item yang ditemukan. "
                "Pastikan ada data di list view atau coba refresh halaman."
            )

        _logger.info(">>>>> Total pricelist item ditemukan: %s", len(records))
        _log_step("search pricelist item")

        # ==========================================================
        # STEP 1 — FAIL FAST applied_on
        # ==========================================================
        unsupported = records.filtered(
            lambda r: r.applied_on not in ('1_product', '0_product_variant')
        )

        if unsupported:
            applied_on_vals = sorted(set(unsupported.mapped('applied_on')))
            _logger.info(
                ">>>>> Export dibatalkan, applied_on tidak didukung: %s",
                applied_on_vals
            )
            raise ValidationError(
                "Export dibatalkan.\n\n"
                "Pricelist item dengan applied_on berikut belum didukung:\n"
                f"- {', '.join(applied_on_vals)}\n\n"
                "Saat ini export hanya mendukung:\n"
                "- Product (1_product)\n"
                "- Product Variant (0_product_variant)"
            )

        _log_step("fail fast applied_on")

        # ==========================================================
        # STEP 2 — SQL DISTINCT ON (LATEST RULE PER OBJECT)
        # ==========================================================
        query = """
            SELECT DISTINCT ON (
                applied_on,
                COALESCE(product_id, 0),
                COALESCE(product_tmpl_id, 0)
            )
            id
            FROM product_pricelist_item
            WHERE id = ANY(%s)
            AND (date_start IS NULL OR date_start <= %s)
            AND (date_end IS NULL OR date_end >= %s)
            ORDER BY
                applied_on,
                COALESCE(product_id, 0),
                COALESCE(product_tmpl_id, 0),
                date_start DESC NULLS LAST,
                id DESC
        """

        self.env.cr.execute(query, (records.ids, today, today))
        latest_ids = [row[0] for row in self.env.cr.fetchall()]

        if not latest_ids:
            raise UserError("Tidak ada pricelist item valid pada periode saat ini.")

        records = self.env['product.pricelist.item'].browse(latest_ids)

        _logger.info(
            ">>>>> SQL DISTINCT ON menghasilkan %s latest rules",
            len(records)
        )
        _log_step("sql distinct on latest rule")

        # ==========================================================
        # STEP 3 — BUILD LATEST MAP
        # ==========================================================
        latest_map = {}

        for item in records:
            if item.applied_on == '0_product_variant':
                key = ('variant', item.product_id.id)
                data_name = item.product_id.display_name
            else:
                key = ('template', item.product_tmpl_id.id)
                data_name = item.product_tmpl_id.name

            latest_map[key] = item

            _logger.info(
                ">>>>> Latest rule dipilih: %s | applied_on=%s | date_start=%s",
                data_name, item.applied_on, item.date_start
            )

        _log_step("build latest_map")

        # ==========================================================
        # STEP 4 — TEMPLATE & VARIANT PRIORITY MAP
        # ==========================================================
        template_ids = set()
        variant_override = {}

        for (k_type, _), item in latest_map.items():
            if k_type == 'variant':
                variant_override[item.product_id.id] = item
                template_ids.add(item.product_id.product_tmpl_id.id)

        for (k_type, tmpl_id), item in latest_map.items():
            if k_type == 'template':
                template_ids.add(tmpl_id)

        _logger.info(">>>>> Total template terlibat: %s", len(template_ids))
        _logger.info(">>>>> Total variant override: %s", len(variant_override))
        _log_step("build priority map")

        # ==========================================================
        # STEP 5 — BULK LOAD TEMPLATE & VARIANT
        # ==========================================================
        templates = self.env['product.template'].browse(list(template_ids))
        variants = self.env['product.product'].search([
            ('product_tmpl_id', 'in', list(template_ids))
        ])

        _logger.info(">>>>> Total variant ditemukan: %s", len(variants))
        _log_step("bulk load template & variant")

        # ==========================================================
        # STEP 6 — GROUP VARIANT PER TEMPLATE
        # ==========================================================
        variants_by_template = {}
        for v in variants:
            variants_by_template.setdefault(v.product_tmpl_id.id, []).append(v)

        _log_step("group variant per template")

        # ==========================================================
        # STEP 7 — BUILD EXPORT DATA (ORDERED)
        # ==========================================================
        export_data = []

        for template in templates.sorted(key=lambda t: (t.default_code or '', t.name)):
            tmpl_item = latest_map.get(('template', template.id))
            tmpl_variants = variants_by_template.get(template.id, [])

            _logger.info(
                ">>>>> Proses template: %s | total variant=%s",
                template.name, len(tmpl_variants)
            )

            # TEMPLATE ROW
            # Untuk single variant: isi product_ref dengan default_code dari variant
            single_variant = tmpl_variants[0] if len(tmpl_variants) == 1 else None

            export_data.append({
                'pricelist': tmpl_item.pricelist_id.name if tmpl_item else '',
                'type': 'Template',
                'template_ref': template.default_code or '',
                'product_ref': (single_variant.default_code or '') if single_variant else '',
                'product_name': single_variant.display_name if single_variant else template.name,
                'barcode': (single_variant.barcode or '') if single_variant else (template.barcode or ''),
                'category': template.categ_id.complete_name or '',
                'brand': getattr(template, 'analytic_id', False) and template.analytic_id.name or '',
                'grade': getattr(template, 'snk_grade_id', False) and template.snk_grade_id.snk_name or '',
                'normal_price': single_variant.snk_normal_price if single_variant else template.snk_normal_price,
                'sales_price': tmpl_item.fixed_price if tmpl_item else 0.0,
                'uom': template.uom_id.name,
                'date_start': tmpl_item.date_start if tmpl_item and tmpl_item.date_start else '',
                'date_end': tmpl_item.date_end if tmpl_item and tmpl_item.date_end else '',
            })

            _logger.info(">>>>> Template ditambahkan: %s", template.name)

            # VARIANT ROW (JIKA > 1)
            if len(tmpl_variants) <= 1:
                _logger.info(
                    ">>>>> Template %s hanya 1 variant, skip variant row (data sudah di template row)",
                    template.name
                )
                continue

            for variant in sorted(tmpl_variants, key=lambda v: (v.default_code or '')):
                item = variant_override.get(variant.id) or tmpl_item

                export_data.append({
                    'pricelist': item.pricelist_id.name if item else '',
                    'type': 'Variant',
                    'template_ref': template.default_code or '',
                    'product_ref': variant.default_code or '',
                    'product_name': variant.name,
                    'barcode': variant.barcode or '',
                    'category': variant.categ_id.complete_name or '',
                    'brand': variant.analytic_id.name or '',
                    'grade': variant.snk_grade_id.snk_name or '',
                    'normal_price': variant.snk_normal_price,
                    'sales_price': item.fixed_price if item else 0.0,
                    'uom': variant.uom_id.name,
                    'date_start': item.date_start if item else "",
                    'date_end': item.date_end if item else "",
                })

                _logger.info(
                    ">>>>> Variant ditambahkan: %s | harga=%s",
                    variant.display_name,
                    item.fixed_price if item else 0.0
                )

        _logger.info(">>>>> Total baris export: %s", len(export_data))
        _log_step("build export data")

        # ==========================================================
        # STEP 8 — WRITE TO EXCEL (MEMORY SAFE)
        # ==========================================================
        row = 1
        for data in export_data:
            sheet.write(row, 0, data['pricelist'])
            sheet.write(row, 1, data['type'])
            sheet.write(row, 2, data['template_ref'])
            sheet.write(row, 3, data['product_ref'])
            sheet.write(row, 4, data['product_name'])
            sheet.write(row, 5, data['barcode'])
            sheet.write(row, 6, data['category'])
            sheet.write(row, 7, data['brand'])
            sheet.write(row, 8, data['grade'])
            sheet.write(row, 9, data['normal_price'])
            sheet.write(row, 10, data['sales_price'])
            sheet.write(row, 11, data['uom'])
            self._write_date(sheet, row, 12, data['date_start'], date_format)
            self._write_date(sheet, row, 13, data['date_end'], date_format)
            row += 1

        _log_step("write excel")
        _logger.info(">>>>> Export latest pricelist selesai")

    def action_archive_items(self):
        """Action untuk archive pricelist items"""
        for rec in self:
            rec.active = False
            # Reset flag snk_is_synch agar pricelist item tidak turun ke pos 
            rec.snk_is_synch = True
        return True
    
    def action_unarchive_items(self):
        """Action untuk unarchive pricelist items"""
        for rec in self:
            rec.active = True
            # Reset flag snk_is_synch agar pricelist item tidak turun ke pos 
            rec.snk_is_synch = False
        return True