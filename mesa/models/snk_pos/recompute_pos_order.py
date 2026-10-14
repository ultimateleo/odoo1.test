from odoo import models, api, _
from odoo.exceptions import UserError
import logging
import time as t

_logger = logging.getLogger(__name__)

class SnkPosOrder(models.Model):
    _name = 'snk.pos.order'
    _inherit = ['snk.pos.order']

    def action_enqueue_update_taxes(self):
        """Tombol untuk mendaftarkan job update taxes ke queue."""
        if not self:
            return
        total = len(self)
        job_desc = _("Update taxes untuk %s record") % total
        job = self.with_delay(
                priority=10,
                channel="update_taxes_pos_order",
                description=job_desc,
            ).action_update_taxes_from_product()
        
        if job:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Job Dikirim ke Queue'),
                    'message': _('Proses update taxes (%s record) sedang dijalankan di background.') % total,
                    'type': 'success',
                }
            }


    def action_update_taxes_from_product(self):
        """Update taxes berdasarkan product_id, dan hitung ulang total"""
        if not self:
            raise UserError(_("Tidak ada record yang dipilih."))

        batch_size = 5000
        all_ids = self.ids
        total_records = len(all_ids)
        _logger.info(">>> Start updating taxes for %s records", total_records)
        total_batches = (total_records + batch_size - 1) // batch_size  # ceil division
        batch_start_time = t.perf_counter()
        for i in range(0, total_records, batch_size):
            batch_ids = all_ids[i:i + batch_size]
            batch = self.browse(batch_ids)
            _logger.info(">>> Processing batch %s / %s", i + 1, total_batches)

            for rec in batch:
                if not rec.product_id:
                    continue

                # ambil taxes dari product
                taxes = rec.product_id.taxes_id.filtered(
                    lambda t: t.company_id == rec.env.company
                )

                # hitung tax amount manual (pakai Odoo compute tax helper)
                tax_amount = 0.0
                subtotal = rec.price_subtotal or 0.0
                if taxes:
                    tax_res = taxes.compute_all(subtotal, currency=rec.env.company.currency_id)
                    tax_amount = tax_res.get('total_included', 0.0) - tax_res.get('total_excluded', 0.0)

                # update record
                rec.write({
                    'tax_id': [(6, 0, taxes.ids)],
                    'price_tax': tax_amount,
                    'price_total': subtotal + tax_amount,
                    # 'price_subtotal':
                })

            _logger.info(">>> Taxes: %s", taxes.display_name)
            # Commit tiap batch untuk stabilitas
            # self.env.cr.commit() # commit sudah dihandle job queue
            elapsed = t.perf_counter() - batch_start_time
            _logger.info(">>> Batch %s - %s completed in %.2f.", i + 1, total_batches, elapsed)
            batch_start_time = t.perf_counter()
            


        _logger.info(">>> Tax update completed for %s records", total_records)
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Update Selesai'),
                'message': _('Taxes berhasil diupdate untuk %s record.') % total_records,
                'type': 'success',
                'sticky': False,
            }
        }
