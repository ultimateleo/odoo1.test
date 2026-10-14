import logging
import time


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Backfill account_move_line.snk_grade_id lewat SQL, bukan ORM.

    snk_grade_id di account.move.line adalah related stored ke product_id.snk_grade_id.
    Kalau kolomnya dibiarkan dibuat oleh _auto_init, Odoo menandai SELURUH baris
    account_move_line untuk dihitung ulang lewat ORM: fetch per batch di Python lalu
    UPDATE ... FROM (VALUES ...) per batch. Di MESA_DEV ini berjalan >10 menit dan
    belum selesai; di production tabelnya jauh lebih besar.

    Dengan membuat kolom + mengisinya di sini (sebelum model di-load), _auto_init
    melihat kolom sudah ada → tidak dianggap field baru → tidak ada recompute.
    Satu UPDATE JOIN product_product, PostgreSQL yang kerja, tanpa Python.

    Idempotent: ADD COLUMN IF NOT EXISTS, dan UPDATE hanya baris yang masih NULL.
    Index dibuat di sini juga supaya _auto_init tidak perlu membuatnya lagi.
    """
    if not version:
        return  # fresh install: tabel kosong, biar _auto_init yang buat kolom

    started_at = time.monotonic()

    cr.execute("""
        ALTER TABLE account_move_line
        ADD COLUMN IF NOT EXISTS snk_grade_id integer
    """)

    cr.execute("""
        UPDATE account_move_line aml
           SET snk_grade_id = pp.snk_grade_id
          FROM product_product pp
         WHERE pp.id = aml.product_id
           AND aml.snk_grade_id IS NULL
           AND pp.snk_grade_id IS NOT NULL
    """)
    rows = cr.rowcount

    cr.execute("""
        CREATE INDEX IF NOT EXISTS account_move_line__snk_grade_id_index
        ON account_move_line (snk_grade_id)
    """)

    _logger.info(
        "[MIGRATION] backfill account_move_line.snk_grade_id dari versi %s: "
        "%d baris dalam %.2fs",
        version, rows, time.monotonic() - started_at,
    )
