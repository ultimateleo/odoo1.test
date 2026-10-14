import logging
import time


_logger = logging.getLogger(__name__)


INDEXES = (
    {
        'name': 'idx_snk_report_sales_analysis_uid_date',
        'table': 'snk_report_sales_analysis',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_snk_report_sales_analysis_uid_date
            ON snk_report_sales_analysis (create_uid, date_order DESC)
        """,
        'definition_fragments': (
            '(create_uid, date_order DESC',
        ),
    },
    {
        'name': 'idx_sale_order_sales_analysis_company_date',
        'table': 'sale_order',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_sale_order_sales_analysis_company_date
            ON sale_order (company_id, date_order)
            WHERE state NOT IN ('draft', 'cancel')
        """,
        'definition_fragments': (
            '(company_id, date_order)',
            "'draft'",
            "'cancel'",
        ),
    },
    {
        'name': 'idx_snk_pos_order_sales_analysis_company_date',
        'table': 'snk_pos_order',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_snk_pos_order_sales_analysis_company_date
            ON snk_pos_order (company_id, date_order)
            WHERE state NOT IN ('draft', 'cancel')
        """,
        'definition_fragments': (
            '(company_id, date_order)',
            "'draft'",
            "'cancel'",
        ),
    },
    {
        'name': 'idx_snk_pos_order_sales_analysis_company_site_date',
        'table': 'snk_pos_order',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_snk_pos_order_sales_analysis_company_site_date
            ON snk_pos_order (company_id, snk_site_id, date_order)
            WHERE state NOT IN ('draft', 'cancel')
              AND snk_site_id IS NOT NULL
        """,
        'definition_fragments': (
            '(company_id, snk_site_id, date_order)',
            "'draft'",
            "'cancel'",
            'snk_site_id IS NOT NULL',
        ),
    },
    {
        'name': 'idx_snk_pos_order_sales_analysis_company_product_tmpl_date',
        'table': 'snk_pos_order',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_snk_pos_order_sales_analysis_company_product_tmpl_date
            ON snk_pos_order (company_id, product_template_id, date_order)
            WHERE state NOT IN ('draft', 'cancel')
              AND product_template_id IS NOT NULL
        """,
        'definition_fragments': (
            '(company_id, product_template_id, date_order)',
            "'draft'",
            "'cancel'",
            'product_template_id IS NOT NULL',
        ),
    },
    {
        'name': 'idx_snk_pos_order_sales_analysis_company_brand_date',
        'table': 'snk_pos_order',
        'ddl': """
            CREATE INDEX IF NOT EXISTS
                idx_snk_pos_order_sales_analysis_company_brand_date
            ON snk_pos_order (company_id, analytic_account_id, date_order)
            WHERE state NOT IN ('draft', 'cancel')
              AND analytic_account_id IS NOT NULL
        """,
        'definition_fragments': (
            '(company_id, analytic_account_id, date_order)',
            "'draft'",
            "'cancel'",
            'analytic_account_id IS NOT NULL',
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
    """Create Sales Analysis source indexes during the controlled 0.5 upgrade."""
    _logger.info(
        "Starting Sales Analysis source index migration from module version %s",
        version,
    )

    # Fail instead of waiting indefinitely when application/cron writers were not
    # fully stopped before the maintenance upgrade.
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

    # Refresh planner statistics after the source indexes are available. This is
    # intentionally transactional with the module upgrade.
    cr.execute("ANALYZE sale_order")
    cr.execute("ANALYZE snk_pos_order")
    cr.execute("ANALYZE snk_report_sales_analysis")
    _logger.info("Sales Analysis source index migration completed successfully")
