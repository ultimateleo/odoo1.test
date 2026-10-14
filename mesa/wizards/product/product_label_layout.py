from odoo import fields, models, api, _
from odoo.exceptions import UserError
import math

import logging
_logger = logging.getLogger(__name__)

class ProductLabelLayout(models.TransientModel):
    _inherit = 'product.label.layout'

    print_format = fields.Selection(selection_add=[
        ('3x18', '3 x 1.8'),
        ('3x18xsku', '3 x 1.8 with SKU'),
    ], ondelete={
        '3x18': 'set default',
        '3x18xsku': 'set default',
    })

    @api.depends('print_format')
    def _compute_dimensions(self):
        for wizard in self:
            if wizard.print_format in ('3x18', '3x18xsku'):
                wizard.columns = 3
                wizard.rows = 4
            elif 'x' in wizard.print_format:
                columns, rows = wizard.print_format.split('x')[:2]
                wizard.columns = int(columns) if columns.isdigit() else 1
                wizard.rows = int(rows) if rows.isdigit() else 1
            else:
                wizard.columns, wizard.rows = 1, 1

    def _get_stock_move_quantities(self):
        """Get quantities from stock.move when called from stock.picking"""
        self.ensure_one()

        # Check if we're coming from stock.picking
        context = self.env.context
        if context.get('active_model') == 'stock.picking' and context.get('active_ids'):
            picking_ids = context.get('active_ids')
            pickings = self.env['stock.picking'].browse(picking_ids)

            # _logger.info(f"=== Mengambil quantity dari {len(pickings)} picking(s) ===")

            # Group quantities by product
            quantity_by_product = {}
            for picking in pickings:
                # _logger.info(f"Picking: {picking.name}")

                for move in picking.move_ids_without_package.filtered(lambda m: m.state != 'cancel'):
                    product = move.product_id
                    qty = int(move.quantity or 0)

                    if qty <= 0:
                        continue

                    product_id = product.id
                    if product_id not in quantity_by_product:
                        quantity_by_product[product_id] = 0
                    quantity_by_product[product_id] += qty

                    # _logger.info(f"  - Product: {product.name}, Qty: {qty}")

            # _logger.info(f"Total quantity: {sum(quantity_by_product.values())}")
            # _logger.info(f"Quantity by product: {quantity_by_product}")

            return quantity_by_product

        return {}

    def _generate_filler_data(self, original_products, original_quantity_by_product, target_total):
        """
        Generate filler data to reach target_total labels
        flow:
        - convert all quantity to integer
        - count how many label to add (if < 15 to fill 15 label)
        - get product list for filtering
        - if there's no available product, get first product and fill to 15 label
        - generate random for pick in avail product
        """
        original_quantity_by_product = {
            k: int(v) for k, v in original_quantity_by_product.items()
        }

        total_labels = sum(original_quantity_by_product.values())
        if total_labels >= target_total:
            return original_quantity_by_product

        filler_needed = target_total - total_labels

        last_product_id = list(original_quantity_by_product.keys())[-1] if original_quantity_by_product else (
            original_products[0] if original_products else None)

        filler_quantity_by_product = original_quantity_by_product.copy()

        if last_product_id:
            filler_quantity_by_product[last_product_id] = filler_quantity_by_product.get(last_product_id, 0) + filler_needed

        return filler_quantity_by_product

    def _prepare_report_data(self):
        xml_id, data = super()._prepare_report_data()

        if self.custom_quantity <= 0:
            raise UserError(_('You need to set a positive quantity.'))

        # Hardcode untuk format 3x18
        if self.print_format in ('3x18', '3x18xsku'):
            xml_id = 'mesa.report_product_template_label_mesa_3x18'
            if 'sku' in self.print_format:
                xml_id += '_sku'
        elif 'x' in self.print_format:
            xml_id = 'mesa.report_product_template_label_mesa_%sx%s' % (self.columns, self.rows)
            if 'sku' in self.print_format:
                xml_id += '_sku'
        else:
            xml_id = ''

        active_model = ''
        products = []
        if self.product_tmpl_ids:
            products = self.product_tmpl_ids.ids
            active_model = 'product.template'
        elif self.product_ids:
            products = self.product_ids.ids
            active_model = 'product.product'
        else:
            raise UserError(
                _("No product to print, if the product is archived please unarchive it before printing its label."))

        # Get quantities from stock.move if available
        quantity_by_product = {}
        stock_move_quantities = self._get_stock_move_quantities()

        if stock_move_quantities:
            # Use quantities from stock.move
            quantity_by_product = stock_move_quantities
        else:
            # Fall back to custom quantity - konversi ke integer
            for product in products:
                quantity_by_product[product] = int(self.custom_quantity or 0)

        MINIMUM_LABELS = 15

        # Generate filler data untuk mencapai minimal 15 labels
        quantity_by_product = self._generate_filler_data(products, quantity_by_product, MINIMUM_LABELS)

        # Build data to pass to the report
        data = {
            'active_model': active_model,
            'quantity_by_product': quantity_by_product,
            'layout_wizard': self.id,
            'price_included': 'xprice' in self.print_format,
            'sku_included': 'sku' in self.print_format,
        }
        return xml_id, data

    def process(self):
        self.ensure_one()
        xml_id, data = self._prepare_report_data()
        if not xml_id:
            raise UserError(_('Unable to find report template for %s format', self.print_format))
        report_action = self.env.ref(xml_id).report_action(None, data=data, config=False)
        report_action.update({'close_on_report_download': True})
        return report_action