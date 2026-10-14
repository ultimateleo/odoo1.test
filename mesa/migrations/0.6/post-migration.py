import logging
import time


_logger = logging.getLogger(__name__)


INDEXES = (
    {
        'name': 'idx_pricelist_item_sales_analysis_product_validity',
        'table': 'product_pricelist_item',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_pricelist_item_sales_analysis_product_validity
            ON product_pricelist_item (
                product_id,
                (COALESCE(date_start, '-infinity'::timestamp)) DESC,
                id DESC
            )
            INCLUDE (date_end, fixed_price)
            WHERE product_id IS NOT NULL
        """,
        'definition_fragments': (
            '(product_id, COALESCE(date_start',
            "'-infinity'::timestamp without time zone",
            'id DESC)',
            'INCLUDE (date_end, fixed_price)',
            'product_id IS NOT NULL',
        ),
    },
    {
        'name': 'idx_pricelist_item_sales_analysis_template_validity',
        'table': 'product_pricelist_item',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_pricelist_item_sales_analysis_template_validity
            ON product_pricelist_item (
                product_tmpl_id,
                (COALESCE(date_start, '-infinity'::timestamp)) DESC,
                id DESC
            )
            INCLUDE (date_end, fixed_price)
            WHERE product_id IS NULL
              AND product_tmpl_id IS NOT NULL
        """,
        'definition_fragments': (
            '(product_tmpl_id, COALESCE(date_start',
            "'-infinity'::timestamp without time zone",
            'id DESC)',
            'INCLUDE (date_end, fixed_price)',
            'product_id IS NULL',
            'product_tmpl_id IS NOT NULL',
        ),
    },
)


def _assert_index_valid(cr, index):
    cr.execute(
        """
        SELECT i.indisready, i.indisvalid, pg_get_indexdef(i.indexrelid)
        FROM pg_index i
        JOIN pg_class idx ON idx.oid = i.indexrelid
        JOIN pg_class tbl ON tbl.oid = i.indrelid
        JOIN pg_namespace ns ON ns.oid = idx.relnamespace
        WHERE ns.nspname = 'public'
          AND idx.relname = %s
          AND tbl.relname = %s
        """,
        [index['name'], index['table']],
    )
    status = cr.fetchone()
    if not status:
        raise RuntimeError(
            f"PostgreSQL index {index['name']} was not created on {index['table']}"
        )

    is_ready, is_valid, definition = status
    normalized_definition = ' '.join(definition.split())
    missing_fragments = [
        fragment
        for fragment in index['definition_fragments']
        if fragment not in normalized_definition
    ]
    if not is_ready or not is_valid or missing_fragments:
        raise RuntimeError(
            "PostgreSQL index %(index)s has an unexpected definition/status. "
            "ready=%(ready)s valid=%(valid)s missing=%(missing)s definition=%(definition)s"
            % {
                'index': index['name'],
                'ready': is_ready,
                'valid': is_valid,
                'missing': missing_fragments,
                'definition': normalized_definition,
            }
        )


def migrate(cr, version):
    """Create indexed as-of pricelist access paths for Sales Analysis."""
    _logger.info(
        "Starting Sales Analysis pricelist index migration from module version %s",
        version,
    )

    cr.execute("SET LOCAL lock_timeout = '60s'")

    for index in INDEXES:
        started_at = time.monotonic()
        _logger.info("Creating/verifying PostgreSQL index %s", index['name'])
        cr.execute(index['ddl'])
        _assert_index_valid(cr, index)
        _logger.info(
            "PostgreSQL index %s is ready in %.2fs",
            index['name'],
            time.monotonic() - started_at,
        )

    cr.execute("ANALYZE product_pricelist_item")
    _logger.info("Sales Analysis pricelist index migration completed successfully")
