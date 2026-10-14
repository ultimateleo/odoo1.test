import logging
import time

from psycopg2 import sql


_logger = logging.getLogger(__name__)


# Fixed internal table names only. ANALYZE refreshes planner statistics after
# an upgrade/restore without changing source or report business data.
TABLES = (
    'sale_order',
    'sale_order_line',
    'snk_pos_order',
    'account_tax_sale_order_line_rel',
    'account_tax_snk_pos_order_rel',
    'project_project_sale_order_line_rel',
    'snk_pos_order_project_rel',
    'snk_site_contact_rel',
    'snk_site_snk_site_tag_rel',
    'product_tag_product_product_rel',
    'product_tag_product_template_rel',
)


def _can_analyze(cr, table_name):
    """Return whether the module-upgrade role may analyze the table."""
    cr.execute(
        """
        SELECT
            active_role.rolsuper
            OR relation.relowner = active_role.oid
            OR pg_has_role(active_role.oid, relation.relowner, 'USAGE')
            OR EXISTS (
                SELECT 1
                FROM pg_roles maintenance_role
                WHERE maintenance_role.rolname = 'pg_maintain'
                  AND pg_has_role(
                      active_role.oid,
                      maintenance_role.oid,
                      'MEMBER'
                  )
            ) AS allowed
        FROM pg_class relation
        JOIN pg_roles active_role ON active_role.rolname = current_user
        WHERE relation.oid = to_regclass(%s)
        """,
        [table_name],
    )
    row = cr.fetchone()
    return bool(row and row[0])


def migrate(cr, version):
    """Refresh statistics used by the bounded Sales Analysis query."""
    _logger.info(
        "Starting Sales Analysis planner-statistics migration from module version %s",
        version,
    )

    for table_name in TABLES:
        if not _can_analyze(cr, table_name):
            _logger.warning(
                "Skipping ANALYZE %s: the module-upgrade database role is not "
                "the owner and does not have maintenance privileges",
                table_name,
            )
            continue

        started_at = time.monotonic()
        cr.execute(
            sql.SQL("ANALYZE {}").format(sql.Identifier(table_name))
        )
        _logger.info(
            "Analyzed %s in %.2fs",
            table_name,
            time.monotonic() - started_at,
        )

    _logger.info("Sales Analysis planner-statistics migration completed")
