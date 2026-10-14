from odoo import models, _
import time as t
import logging

_logger = logging.getLogger(__name__)


class SnkPosOrder(models.Model):
    _inherit = 'snk.pos.order'

    RECOMPUTE_BATCH_SIZE = 2000

    def action_enqueue_recompute_amounts(self):
        """Pecah record terpilih menjadi job queue: 1 job per batch.

        Kenapa tidak 1 job raksasa: tiap job = 1 transaksi pendek, jadi lock
        cepat dilepas antar batch, dan batch yang gagal bisa di-retry sendiri
        tanpa mengulang seluruh selection.
        """
        if not self:
            return
        ids = self.ids
        total = len(ids)
        batch_size = self.RECOMPUTE_BATCH_SIZE
        job_count = 0
        for i in range(0, total, batch_size):
            batch = self.browse(ids[i:i + batch_size])
            batch.with_delay(
                priority=10,
                channel='recompute_pos_amounts',
                description=_("Recompute amounts POS order %s-%s") % (i + 1, i + len(batch)),
            ).action_recompute_amounts()
            job_count += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Job Dikirim ke Queue'),
                'message': _('%s POS order dipecah ke %s job @%s record.') % (total, job_count, batch_size),
                'type': 'success',
            },
        }

    def action_recompute_amounts(self):
        """Dieksekusi worker queue. Pakai compute native (_compute_amount) supaya
        logika price_include, discount_amount per unit, dan rounding currency
        tetap sama dengan perhitungan normal — bukan hitung manual.
        """
        start = t.perf_counter()
        for rec in self:
            rec._compute_amount()
        self.env.flush_all()
        _logger.info(
            ">>> Recompute amounts %s snk.pos.order selesai dalam %.2fs",
            len(self), t.perf_counter() - start)


class SnkPosSale(models.Model):
    _inherit = 'snk.pos.sale'

    RECOMPUTE_BATCH_SIZE = 500  # lebih kecil: 1 header mengagregasi banyak lines

    def action_enqueue_recompute_amounts(self):
        """Pecah record terpilih menjadi job queue: 1 job per batch."""
        if not self:
            return
        ids = self.ids
        total = len(ids)
        batch_size = self.RECOMPUTE_BATCH_SIZE
        job_count = 0
        for i in range(0, total, batch_size):
            batch = self.browse(ids[i:i + batch_size])
            batch.with_delay(
                priority=10,
                channel='recompute_pos_amounts',
                description=_("Recompute amounts POS sale %s-%s") % (i + 1, i + len(batch)),
            ).action_recompute_amounts()
            job_count += 1

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Job Dikirim ke Queue'),
                'message': _('%s POS sale dipecah ke %s job @%s record.') % (total, job_count, batch_size),
                'type': 'success',
            },
        }

    def action_recompute_amounts(self):
        """Dieksekusi worker queue. Header menjumlahkan price_subtotal/price_total
        line (snk.pos.order), jadi pastikan line sudah di-recompute lebih dulu.
        """
        start = t.perf_counter()
        self._compute_amounts()
        self.env.flush_all()
        _logger.info(
            ">>> Recompute amounts %s snk.pos.sale selesai dalam %.2fs",
            len(self), t.perf_counter() - start)
