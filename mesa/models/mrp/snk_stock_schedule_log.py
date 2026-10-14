from odoo import models, fields
import logging

_logger = logging.getLogger(__name__)


class SnkStockScheduleLog(models.Model):
    """Catatan eksekusi MRP per periode.

    Satu baris = satu percobaan pemrosesan satu periode schedule. Gunanya dua:

    1. Anti eksekusi ganda. Kunci periode adalah (schedule_id, effective_date),
       dijaga unique index parsial khusus state='success'. Status di
       snk.stock.schedule tidak cukup sebagai penjaga - status bisa di-reset
       cron harian, cron stuck-checker, atau manual dari UI, dan begitu itu
       terjadi periode yang sama akan diproses ulang dan menghasilkan Order
       Point serta Transfer Order duplikat.

    2. Forensik. Kalau satu site tidak jalan, log ini yang menjawab kapan
       terakhir dicoba, gagal karena apa, dan berapa lama prosesnya.
    """
    _name = 'snk.stock.schedule.log'
    _description = 'SNK Stock Schedule Execution Log'
    _order = 'effective_date desc, id desc'

    schedule_id = fields.Many2one(
        'snk.stock.schedule', string='Schedule',
        required=True, index=True, ondelete='cascade')
    site_id = fields.Many2one(
        'snk.site', string='Site',
        related='schedule_id.site_id', store=True, index=True)
    effective_date = fields.Date(
        string='Effective Date', required=True, index=True,
        help='Tanggal periode yang diproses (Schedule Day, bukan tanggal cron berjalan).')
    state = fields.Selection([
        ('success', 'Success'),
        ('failed', 'Failed'),
    ], string='Status', required=True, index=True)
    run_date = fields.Datetime(
        string='Run Date', default=fields.Datetime.now,
        help='Kapan cron benar-benar menjalankan periode ini.')
    duration = fields.Float(
        string='Duration (seconds)', help='Lama pemrosesan periode ini.')
    order_point_id = fields.Many2one(
        'snk.order.point', string='Order Point', ondelete='set null')
    error = fields.Text(string='Error')

    def init(self):
        """Unique index parsial: satu periode hanya boleh SUKSES sekali.

        Harus lewat init(), bukan _sql_constraints - PostgreSQL partial index
        (WHERE state = 'success') tidak bisa diekspresikan sebagai UNIQUE
        constraint biasa. Percobaan yang gagal sengaja boleh berkali-kali
        supaya retry tetap tercatat.
        """
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_snk_stock_schedule_log_period_success
            ON snk_stock_schedule_log USING btree(schedule_id, effective_date)
            WHERE state = 'success'
        """)
        _logger.debug("✓ Created partial unique index: idx_snk_stock_schedule_log_period_success")
