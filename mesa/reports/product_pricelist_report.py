# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import api, models


class ReportPricelistMesa(models.AbstractModel):
    _name = 'report.mesa.report_pricelist_mesa'
    _description = 'Mesa Pricelist Report'

    def _get_report_values(self, docids, data):
        return self._get_report_data(data, 'pdf')

    @api.model
    def get_html(self, data):
        render_values = self._get_report_data(data, 'html')
        return self.env['ir.qweb']._render('mesa.report_pricelist_mesa', render_values)

    def _get_report_data(self, data, report_type='html'):
        quantities = data.get('quantities', [1])
        data_pricelist_id = data.get('pricelist_id')
        pricelist_id = data_pricelist_id and int(data_pricelist_id)
        pricelist = self.env['product.pricelist'].browse(pricelist_id).exists()
        if not pricelist:
            pricelist = self.env['product.pricelist'].search([], limit=1)

        active_model = data.get('active_model', 'product.template')
        active_ids = data.get('active_ids') or []
        is_product_tmpl = active_model == 'product.template'
        ProductClass = self.env[active_model]

        products = ProductClass.browse(active_ids) if active_ids else []
        
        # Buat list produk dengan harga
        products_with_price = []
        for product in products:
            if is_product_tmpl:
                # Jika product template, ambil semua variantnya
                if product.product_variant_count > 1:
                    for variant in product.product_variant_ids:
                        products_with_price.append({
                            'product': variant,
                            'price': pricelist._get_product_price(variant, quantities[0] if quantities else 1),
                        })
                else:
                    # Jika hanya satu variant
                    variant = product.product_variant_ids[0] if product.product_variant_ids else product
                    products_with_price.append({
                        'product': variant,
                        'price': pricelist._get_product_price(variant, quantities[0] if quantities else 1),
                    })
            else:
                # Jika sudah product variant
                products_with_price.append({
                    'product': product,
                    'price': pricelist._get_product_price(product, quantities[0] if quantities else 1),
                })

        return {
            'is_html_type': report_type == 'html',
            'is_product_tmpl': is_product_tmpl,
            'display_pricelist_title': data.get('display_pricelist_title', False) and bool(data['display_pricelist_title']),
            'pricelist': pricelist,
            'products_with_price': products_with_price,  # Data produk dengan harga
            'quantities': quantities,
            'docs': [pricelist],  # Wrapper dalam list untuk t-foreach
        }