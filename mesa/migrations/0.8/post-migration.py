import logging
import time


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Index current-user report cleanup and initial report loading."""
    started_at = time.monotonic()
    cr.execute(
        """
        CREATE INDEX IF NOT EXISTS
            snk_report_inventory_transfer__create_uid_id_index
        ON snk_report_inventory_transfer (create_uid, id DESC)
        """
    )
    cr.execute("ANALYZE snk_report_inventory_transfer")
    _logger.info(
        "Inventory Transfer report index migration from version %s completed "
        "in %.2fs",
        version,
        time.monotonic() - started_at,
    )
