from odoo import api, SUPERUSER_ID
import logging

_logger = logging.getLogger(__name__)

def migrate(cr, version):
    """
    Post-migration 0.3
    Membuat index untuk optimasi pricelist
    """

    # Index untuk kolom name → cek duplicate cepat
    cr.execute("""
        CREATE INDEX IF NOT EXISTS product_pricelist_item_idx
        ON product_pricelist_item (pricelist_id, product_id, product_tmpl_id, date_start, date_end);
    """)
    _logger.info(">>>>> Migrate optimasi pricelist succesfully.")