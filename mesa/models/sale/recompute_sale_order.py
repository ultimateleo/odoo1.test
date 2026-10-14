from odoo import models, fields, api, _
from odoo.exceptions import UserError
import time as t
import logging

_logger = logging.getLogger(__name__)

# --- Override aman untuk bypass validasi lock di sale.order.line ---
class SaleOrderLine(models.Model):
    _inherit = 'sale.order.line'

    def write(self, vals):
        if self.env.context.get('bypass_lock'):
            # Lewati cek lock Odoo
            return super(models.Model, self).write(vals)
        return super().write(vals)

class SaleOrder(models.Model):
    _inherit = 'sale.order'

    def action_enqueue_update_taxes(self):
        """Kirim job queue untuk update taxes di order line."""
        if not self:
            return
        total = len(self)
        job_desc = _("Update taxes untuk %s Sale Order") % total

        # panggil via queue
        job = self.with_delay(
            priority=10,
            channel="update_taxes_sale_order",
            description=job_desc,
        ).action_update_taxes_from_product()

        if job:
            return {
                "type": "ir.actions.client",
                "tag": "display_notification",
                "params": {
                    "title": _("Job Dikirim ke Queue"),
                    "message": _("Proses update taxes untuk %s order sedang dijalankan di background.") % total,
                    "type": "success",
                    "next": {
                        "type": "ir.actions.client",
                        "tag": "reload",
                    },
                },
            }

    def action_update_taxes_from_product(self):
        """Update taxes semua line di sale order."""
        if not self:
            raise UserError(_("Tidak ada record yang dipilih."))

        all_ids = self.ids
        total_orders = len(all_ids)
        batch_size = 100  # lebih kecil karena 1 order bisa punya banyak lines
        total_batches = (total_orders + batch_size - 1) // batch_size

        _logger.info(">>> Start updating taxes for %s sale orders (%s batches)", total_orders, total_batches)
        batch_start_time = t.perf_counter()

        for batch_index, i in enumerate(range(0, total_orders, batch_size), start=1):
            batch_ids = all_ids[i:i + batch_size]
            orders = self.browse(batch_ids).sudo()
            start_no = i + 1
            end_no = i + len(batch_ids)

            _logger.info(">>> Processing batch %s/%s (orders %s - %s)", batch_index, total_batches, start_no, end_no)

            for order in orders:
                order_ctx = order.sudo().with_context(bypass_lock=True)

                for line in order_ctx.order_line:
                    if not line.product_id:
                        continue

                    taxes = line.product_id.taxes_id.filtered(
                        lambda t: not line.company_id or t.company_id == line.company_id
                    )

                    subtotal = line.price_unit * line.product_uom_qty
                    tax_amount = 0.0
                    if taxes:
                        tax_res = taxes.compute_all(
                            line.price_unit,
                            currency=order.currency_id,
                            quantity=line.product_uom_qty,
                            product=line.product_id,
                            partner=order.partner_id
                        )
                        tax_amount = tax_res["total_included"] - tax_res["total_excluded"]

                    line.with_context(bypass_lock=True).sudo().write({
                        "tax_id": [(6, 0, taxes.ids)],
                        "price_tax": tax_amount,
                        "price_total": subtotal + tax_amount,
                        "price_subtotal": subtotal,
                    })

                # Recompute pakai context bypass_lock juga
                order_ctx._recompute_taxes()
                order_ctx._compute_amounts()

            elapsed = t.perf_counter() - batch_start_time
            _logger.info(">>> Batch %s/%s completed (%s orders) in %.2f seconds.",
                         batch_index, total_batches, len(batch_ids), elapsed)
            batch_start_time = t.perf_counter()

        _logger.info(">>> Tax update completed for %s orders", total_orders)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Update Selesai'),
                'message': _('Taxes berhasil diupdate untuk %s Sale Order.') % total_orders,
                'type': 'success',
                'sticky': False,
            }
        }
