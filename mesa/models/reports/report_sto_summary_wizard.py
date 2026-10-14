# -*- coding: utf-8 -*-
import logging
import time

from odoo import fields, models, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class ReportSTOSummaryWizard(models.TransientModel):
    """
    Wizard untuk generate STO Summary Report.

    Arsitektur Performa:
    - INSERT...SELECT: satu round-trip ke DB, tanpa Python loop, tanpa ORM create overhead.
    - delivery_pickings MATERIALIZED: filter picking relevan sekali, semua CTE JOIN ke sini.
    - Planner dipaksa mulai dari N baris hasil filter, bukan full scan 2.3jt baris.
    """
    _name = 'report.sto.summary.wizard'
    _description = 'STO Summary Report Wizard'

    date_start = fields.Datetime(
        string='Start Date', required=True,
        default=fields.Datetime.today,
    )
    date_end = fields.Datetime(
        string='End Date', required=True,
        default=fields.Datetime.today,
    )
    site_ids = fields.Many2many(
        'snk.site', 'report_sto_summary_site_rel',
        'wizard_id', 'site_id', string='Source Sites',
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)],
    )
    destination_site_ids = fields.Many2many(
        'snk.site', 'report_sto_summary_dest_site_rel',
        'wizard_id', 'site_id', string='Destination Sites',
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)],
    )
    analytic_ids = fields.Many2many(
        'account.analytic.account', 'report_sto_summary_analytic_rel',
        'wizard_id', 'analytic_id', string='Brands',
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ],
    )
    carrier_ids = fields.Many2many(
        'delivery.carrier', 'report_sto_summary_carrier_rel',
        'wizard_id', 'carrier_id', string='Ekspedisi',
    )

    def _get_access_context(self):
        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        company_ids = self.env.companies.ids
        if not allowed_industry_ids:
            raise UserError(_("You do not have access to any site industry."))
        if not allowed_brand_ids:
            raise UserError(_("You do not have access to any brand."))
        if not company_ids:
            raise UserError(_("No allowed company is active for this report."))
        invalid_sites = (self.site_ids | self.destination_site_ids).filtered(
            lambda site: site.industry_id.id not in allowed_industry_ids
        )
        if invalid_sites:
            raise UserError(_("You are not allowed to use the selected site(s): %s") % ", ".join(invalid_sites.mapped('display_name')))
        invalid_brands = self.analytic_ids.filtered(
            lambda brand: brand.id not in allowed_brand_ids
        )
        if invalid_brands:
            raise UserError(_("You are not allowed to use the selected brand(s): %s") % ", ".join(invalid_brands.mapped('display_name')))
        return allowed_industry_ids, allowed_brand_ids, company_ids

    # =========================================================================
    # MAIN QUERY
    # =========================================================================

    def _build_delivery_query(self):
        """
        Optimasi berdasarkan EXPLAIN ANALYZE (125s → ~1s):

        Root cause lama:
        - receipt_actual loops=13: scan 2.3jt baris × 13 = ~70 detik
        - move_amounts + net_amounts: 2 scan terpisah 2.3jt baris = ~32 detik
        - brand_agg: full scan tanpa filter picking_id
        - Main SELECT: Parallel Seq Scan stock_picking 2.3jt + account_move 800rb baris

        Perbaikan:
        1. delivery_pickings MATERIALIZED → filter picking sekali, simpan di memory
        2. receipt_picking_ids MATERIALIZED → receipt IDs dikumpulkan sekali, tidak loops=N
        3. combined_amounts → gabung move_amounts + net_amounts jadi 1 scan stock_move
        4. Semua CTE aggregate filter via IN (SELECT id FROM delivery_pickings)
        5. FROM delivery_pickings dp JOIN stock_picking sp ON sp.id = dp.id →
           planner dipaksa mulai dari N baris, bukan Parallel Seq Scan 2.3jt baris
        """
        allowed_industry_ids, allowed_brand_ids, company_ids = self._get_access_context()
        params = {
            'uid': self.env.uid,
            'date_start': self.date_start,
            'date_end': self.date_end,
            'allowed_industry_ids': allowed_industry_ids,
            'allowed_brand_ids': allowed_brand_ids,
            'company_ids': company_ids,
        }

        extra_where = [
            "sp.company_id = ANY(%(company_ids)s)",
            "EXISTS (SELECT 1 FROM account_analytic_account_stock_picking_rel access_brand WHERE access_brand.stock_picking_id = sp.id)",
            "NOT EXISTS (SELECT 1 FROM account_analytic_account_stock_picking_rel access_brand WHERE access_brand.stock_picking_id = sp.id AND NOT (access_brand.account_analytic_account_id = ANY(%(allowed_brand_ids)s)))",
        ]
        if self.site_ids:
            extra_where.append("sp.snk_site_id = ANY(%(site_ids)s)")
            params['site_ids'] = self.site_ids.ids
        else:
            extra_where.append("EXISTS (SELECT 1 FROM snk_site access_site WHERE access_site.id = sp.snk_site_id AND access_site.industry_id = ANY(%(allowed_industry_ids)s))")
        if self.destination_site_ids:
            extra_where.append("sp.snk_site_address_id = ANY(%(destination_site_ids)s)")
            params['destination_site_ids'] = self.destination_site_ids.ids
        else:
            extra_where.append("EXISTS (SELECT 1 FROM snk_site access_site WHERE access_site.id = sp.snk_site_address_id AND access_site.industry_id = ANY(%(allowed_industry_ids)s))")
        if self.analytic_ids:
            extra_where.append("""
                EXISTS (
                    SELECT 1 FROM account_analytic_account_stock_picking_rel aar
                    WHERE aar.stock_picking_id = sp.id
                      AND aar.account_analytic_account_id = ANY(%(analytic_ids)s)
                )
            """)
            params['analytic_ids'] = self.analytic_ids.ids
        if self.carrier_ids:
            extra_where.append("sp.carrier_id IN %(carrier_ids)s")
            params['carrier_ids'] = tuple(self.carrier_ids.ids)

        extra_where_sql = (" AND " + " AND ".join(extra_where)) if extra_where else ""
        _logger.info(f">>>> START BUILDING DELIVERY QUERY -- WHERE CLAUSE: {extra_where_sql} <<<<")

        query = f"""
        WITH RECURSIVE
        -- Anchor: picking relevan disimpan sekali. Semua CTE JOIN ke sini.
        delivery_pickings AS MATERIALIZED (
            SELECT sp.id, sp.snk_site_id, sp.snk_site_address_id
            FROM stock_picking sp
            JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
            WHERE spt.code = 'outgoing'
              AND sp.state != 'cancel'
              AND sp.snk_is_mutation = TRUE
              AND sp.snk_create_date >= %(date_start)s
              AND sp.snk_create_date <= %(date_end)s
              {extra_where_sql}
        ),

        -- Seed: relasi move hanya menjamin receipt awal. Move backorder pada
        -- data aktual tidak mewarisi relasi ini.
        direct_receipt_picking_ids AS MATERIALIZED (
            SELECT DISTINCT
                sm_src.picking_id AS delivery_picking_id,
                sm_dest.picking_id AS receipt_picking_id
            FROM stock_move_move_rel mmr
            JOIN stock_move sm_src ON sm_src.id = mmr.move_orig_id
            JOIN stock_move sm_dest ON sm_dest.id = mmr.move_dest_id
            JOIN stock_picking sp_dest ON sp_dest.id = sm_dest.picking_id
            JOIN stock_picking_type spt_dest ON spt_dest.id = sp_dest.picking_type_id
            WHERE sm_src.picking_id IN (SELECT id FROM delivery_pickings)
              AND spt_dest.code = 'incoming'
              AND sm_dest.company_id = sm_src.company_id
              AND sm_dest.company_id = ANY(%(company_ids)s)
        ),

        -- Telusuri seluruh turunan receipt melalui relasi picking backorder.
        -- visited_ids menjaga query tetap aman bila ada data relasi yang cycle.
        receipt_chain AS (
            SELECT
                direct.delivery_picking_id,
                direct.receipt_picking_id,
                0 AS chain_depth,
                ARRAY[direct.receipt_picking_id] AS visited_ids
            FROM direct_receipt_picking_ids direct

            UNION ALL

            SELECT
                chain.delivery_picking_id,
                child.id,
                chain.chain_depth + 1,
                chain.visited_ids || child.id
            FROM receipt_chain chain
            JOIN stock_picking child
              ON child.backorder_id = chain.receipt_picking_id
            JOIN stock_picking_type child_type
              ON child_type.id = child.picking_type_id
            WHERE child_type.code = 'incoming'
              AND child.company_id = ANY(%(company_ids)s)
              AND NOT child.id = ANY(chain.visited_ids)
        ),

        -- Deduplikasi defensif jika satu receipt juga ditemukan dari seed lain.
        receipt_picking_ids AS MATERIALIZED (
            SELECT
                delivery_picking_id,
                receipt_picking_id,
                MIN(chain_depth) AS chain_depth
            FROM receipt_chain
            GROUP BY delivery_picking_id, receipt_picking_id
        ),

        move_demand AS (
            SELECT sm.picking_id, SUM(sm.product_uom_qty) AS total_demand
            FROM stock_move sm
            WHERE sm.picking_id IN (SELECT id FROM delivery_pickings)
            GROUP BY sm.picking_id
        ),

        move_actual AS (
            SELECT sml.picking_id, SUM(sml.quantity) AS total_actual
            FROM stock_move_line sml
            WHERE sml.state = 'done'
              AND sml.picking_id IN (SELECT id FROM delivery_pickings)
            GROUP BY sml.picking_id
        ),

        receipt_actual AS (
            SELECT sml.picking_id, SUM(sml.quantity) AS total_receipt_qty
            FROM stock_move_line sml
            WHERE sml.state = 'done'
              AND sml.picking_id IN (SELECT receipt_picking_id FROM receipt_picking_ids)
            GROUP BY sml.picking_id
        ),

        -- Satu baris summary per Delivery agar nilai Demand, Actual, amount,
        -- dan biaya tidak berulang ketika receipt mempunyai backorder.
        receipt_summary AS (
            SELECT
                chain.delivery_picking_id,
                STRING_AGG(
                    receipt.name,
                    ', ' ORDER BY chain.chain_depth, receipt.id
                ) AS receipt_references,
                MIN(receipt.scheduled_date) AS first_receipt_date,
                SUM(COALESCE(actual.total_receipt_qty, 0)) AS total_receipt_qty,
                MAX(receipt.snk_physical_date) AS last_physical_receipt_date,
                COUNT(*) AS receipt_count,
                COUNT(*) FILTER (WHERE receipt.state = 'cancel') AS cancelled_receipt_count,
                COUNT(*) FILTER (
                    WHERE receipt.state IN ('draft', 'waiting', 'confirmed', 'assigned')
                ) AS open_receipt_count
            FROM receipt_picking_ids chain
            JOIN stock_picking receipt
              ON receipt.id = chain.receipt_picking_id
            LEFT JOIN receipt_actual actual
              ON actual.picking_id = receipt.id
            GROUP BY chain.delivery_picking_id
        ),

        -- Fixed-price rule per company dan product pada scheduled_date delivery.
        -- Scope yang didukung hanya variant (0_product_variant) dan template (1_product).
        -- Rule tanpa company berlaku lintas company; rule spesifik harus sama dengan company move.
        move_pricelist_rule AS (
            SELECT DISTINCT ON (sm.id)
                sm.id AS move_id,
                ppi.fixed_price
            FROM stock_move sm
            JOIN stock_picking sp_mv ON sp_mv.id = sm.picking_id
            JOIN product_product pp ON pp.id = sm.product_id
            JOIN product_pricelist_item ppi
                ON ppi.compute_price = 'fixed'
               AND (ppi.company_id IS NULL OR ppi.company_id = sm.company_id)
               AND (ppi.date_start IS NULL OR ppi.date_start <= sp_mv.scheduled_date)
               AND (ppi.date_end IS NULL OR ppi.date_end >= sp_mv.scheduled_date)
               AND (
                    (ppi.applied_on = '0_product_variant' AND ppi.product_id = pp.id)
                    OR (ppi.applied_on = '1_product' AND ppi.product_tmpl_id = pp.product_tmpl_id)
               )
            WHERE sm.state != 'cancel'
              AND sm.picking_id IN (SELECT id FROM delivery_pickings)
            ORDER BY sm.id,
                CASE ppi.applied_on
                    WHEN '0_product_variant' THEN 1
                    ELSE 2
                END,
                ppi.date_start DESC NULLS LAST,
                ppi.id DESC
        ),

        -- Gabungan move_amounts + net_amounts: eliminasi satu full scan stock_move.
        -- total_amount: harga dari pricelist yang berlaku pada scheduled_date (fallback price_unit/normal_price).
        -- net_amount (subtotal): harga normal product (snk_normal_price), independen dari pricelist.
        combined_amounts AS (
            SELECT
                sm.picking_id,
                SUM(COALESCE(mpr.fixed_price, NULLIF(sm.price_unit, 0), pt.list_price, 0) *
                    CASE WHEN sm.quantity > 0 THEN sm.quantity ELSE sm.product_uom_qty END
                ) AS total_amount,
                SUM(COALESCE(pt.snk_normal_price, 0) *
                    CASE WHEN sm.quantity > 0 THEN sm.quantity ELSE sm.product_uom_qty END
                ) AS net_amount
            FROM stock_move sm
            JOIN product_product pp ON pp.id = sm.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            LEFT JOIN move_pricelist_rule mpr ON mpr.move_id = sm.id
            WHERE sm.state != 'cancel'
              AND sm.picking_id IN (SELECT id FROM delivery_pickings)
            GROUP BY sm.picking_id
        ),

        brand_agg AS (
            SELECT
                aar.stock_picking_id AS picking_id,
                STRING_AGG(
                    DISTINCT COALESCE(aaa.name->>'en_US', aaa.name->>'id_ID', aaa.name::text),
                    ', ' ORDER BY COALESCE(aaa.name->>'en_US', aaa.name->>'id_ID', aaa.name::text)
                ) AS brand_names
            FROM account_analytic_account_stock_picking_rel aar
            JOIN account_analytic_account aaa ON aaa.id = aar.account_analytic_account_id
            WHERE aar.stock_picking_id IN (SELECT id FROM delivery_pickings)
            GROUP BY aar.stock_picking_id
        ),

        no_dus AS (
            SELECT
                sp_inner.id AS picking_id,
                CONCAT_WS(
                    '-',
                    NULLIF(UPPER(BTRIM(aaa_first.code)), ''),
                    NULLIF(substring(sp_inner.name from '(\\d+)[^0-9]*$'), '')
                ) AS no_dus_code
            FROM stock_picking sp_inner
            LEFT JOIN LATERAL (
                SELECT aaa.code
                FROM account_analytic_account_stock_picking_rel aar
                JOIN account_analytic_account aaa ON aaa.id = aar.account_analytic_account_id
                WHERE aar.stock_picking_id = sp_inner.id
                ORDER BY aaa.id
                LIMIT 1
            ) aaa_first ON TRUE
            WHERE sp_inner.id IN (SELECT id FROM delivery_pickings)
        )

        SELECT
            -- Info Delivery
            sp.name                                     AS delivery_no,
            sp.name                                     AS delivery_picking_id,
            site.snk_code                               AS site_id,
            COALESCE(site.full_name, site.snk_name)     AS site_name,
            dest_site.snk_code                          AS destination_site_id,
            COALESCE(dest_site.full_name, dest_site.snk_name) AS destination_site_name,
            COALESCE(dest_site.snk_name, rp.name)       AS destination_name,
            ba.brand_names                              AS brands,
            nd.no_dus_code                              AS no_dus,
            COALESCE(creator.name, ru.login)            AS requisitioner,
            CASE sp.state
                WHEN 'draft'     THEN 'New'
                WHEN 'waiting'   THEN 'Waiting'
                WHEN 'confirmed' THEN 'Waiting Availability'
                WHEN 'assigned'  THEN 'Ready'
                WHEN 'done'      THEN 'Done'
                WHEN 'cancel'    THEN 'Cancelled'
                ELSE sp.state
            END                                         AS state,
            sp.scheduled_date                           AS delivery_date,
            sp.snk_create_date                          AS sto_date,
            sp.date_done                                AS validate_date,
            COALESCE(md.total_demand, 0)                AS demand_qty,
            COALESCE(ma.total_actual, 0)                AS actual_qty,
            sp.snk_physical_date                        AS physical_delivery_date,
            
            -- Info Ekspedisi
            COALESCE(dc.name->>'en_US', dc.name->>'id_ID', dc.name::text) AS carrier_name,
            city.name                                   AS city_name,
            dist.snk_name                               AS district_name,
            sp.carrier_tracking_ref                     AS tracking_ref,
            COALESCE(sp.delivery_cost_price, 0)         AS shipping_cost,
            COALESCE(NULLIF(sp.shipping_weight, 0), sp.weight, 0) AS shipping_weight,
            COALESCE(sp.delivery_price, 0)              AS total_shipping_cost,
            am.name                                     AS bill_no,

            -- Info Receipt
            rs.receipt_references                       AS receipt_no,
            rs.receipt_references                       AS receipt_picking_id,
            rs.first_receipt_date                       AS receipt_date,
            COALESCE(rs.total_receipt_qty, 0)           AS receipt_qty,
            rs.last_physical_receipt_date               AS physical_receipt_date,
            CASE
                WHEN sp.date_done IS NOT NULL AND sp.snk_create_date IS NOT NULL
                THEN EXTRACT(DAY FROM (sp.date_done - sp.snk_create_date))::INTEGER
                ELSE 0
            END                                         AS jumlah_hari,
            CASE
                WHEN COALESCE(ma.total_actual, 0) > 0
                THEN ROUND(COALESCE(rs.total_receipt_qty, 0) / ma.total_actual * 100, 2)
                ELSE 0
            END                                         AS akurat,
            sp.origin                                   AS remarks,
            sp.note                                     AS notes,
            COALESCE(ca.total_amount, 0)                AS total_amount,
            COALESCE(ca.net_amount, 0)                  AS net_amount,
            sp.snk_pos_ref                              AS snk_pos_ref,
            dest_site.snk_pos_status                    AS pos_status_site,
            CASE
                WHEN rs.delivery_picking_id IS NULL
                THEN 'No Receipt'
                WHEN rs.cancelled_receipt_count = rs.receipt_count
                THEN 'Cancelled'
                WHEN COALESCE(ma.total_actual, 0) > 0
                     AND COALESCE(rs.total_receipt_qty, 0) > ma.total_actual
                THEN 'Over Received'
                WHEN COALESCE(ma.total_actual, 0) > 0
                     AND COALESCE(rs.total_receipt_qty, 0) >= ma.total_actual
                THEN 'Fully Received'
                WHEN COALESCE(rs.total_receipt_qty, 0) > 0
                     AND rs.open_receipt_count > 0
                THEN 'Partially Received'
                WHEN COALESCE(rs.total_receipt_qty, 0) > 0
                THEN 'Receipt Short'
                WHEN rs.open_receipt_count > 0
                THEN 'Waiting Receipt'
                ELSE 'Receipt Short'
            END                                         AS receipt_status

        -- Mulai dari delivery_pickings (N baris), bukan full scan stock_picking (2.3jt baris).
        FROM delivery_pickings dp
        JOIN stock_picking sp ON sp.id = dp.id
        JOIN stock_picking_type spt ON spt.id = sp.picking_type_id
        LEFT JOIN snk_site site ON site.id = sp.snk_site_id
        LEFT JOIN snk_site dest_site ON dest_site.id = sp.snk_site_address_id
        LEFT JOIN res_users ru ON ru.id = sp.create_uid
        LEFT JOIN res_partner creator ON creator.id = ru.partner_id
        LEFT JOIN delivery_carrier dc ON dc.id = sp.carrier_id
        LEFT JOIN res_country_city city ON city.id = sp.city_id
        LEFT JOIN res_partner rp ON rp.id = sp.partner_id
        LEFT JOIN snk_district dist ON dist.id = rp.snk_district_id
        LEFT JOIN account_move am ON am.id = sp.snk_bill_id
        LEFT JOIN move_demand md ON md.picking_id = sp.id
        LEFT JOIN move_actual ma ON ma.picking_id = sp.id
        LEFT JOIN receipt_summary rs ON rs.delivery_picking_id = sp.id
        LEFT JOIN combined_amounts ca ON ca.picking_id = sp.id
        LEFT JOIN brand_agg ba ON ba.picking_id = sp.id
        LEFT JOIN no_dus nd ON nd.picking_id = sp.id

        ORDER BY sp.name, sp.id
        """
        return query, params

    # =========================================================================
    # INSERT...SELECT WRAPPER
    # =========================================================================

    def _build_insert_select_query(self):
        """
        Bungkus _build_delivery_query() dalam INSERT...SELECT.
        Satu round-trip ke PostgreSQL: CTE jalan, hasil langsung masuk tabel.
        Tidak ada dictfetchall(), Python loop, atau ORM create().
        """
        inner_query, params = self._build_delivery_query()

        _logger.info(f">>>> START BUILDING INNER QUERY <<<< ")

        insert_query = f"""
        INSERT INTO report_sto_summary (
            user_id,
            delivery_no, delivery_picking_id,
            site_id, site_name,
            destination_site_id, destination_site_name, destination_name,
            brands, no_dus, requisitioner, state,
            delivery_date, sto_date, validate_date,
            demand_qty, actual_qty, physical_delivery_date,
            carrier_name, city_name, district_name, tracking_ref,
            shipping_cost, shipping_weight, total_shipping_cost, bill_no,
            receipt_no, receipt_picking_id, receipt_date,
            receipt_qty, physical_receipt_date,
            jumlah_hari, akurat,
            remarks, notes, total_amount, net_amount,
            snk_pos_ref, pos_status_site, receipt_status
        )
        SELECT
            %(uid)s,
            sub.delivery_no, sub.delivery_picking_id,
            sub.site_id, sub.site_name,
            sub.destination_site_id, sub.destination_site_name, sub.destination_name,
            sub.brands, sub.no_dus, sub.requisitioner, sub.state,
            sub.delivery_date, sub.sto_date, sub.validate_date,
            sub.demand_qty, sub.actual_qty, sub.physical_delivery_date,
            sub.carrier_name, sub.city_name, sub.district_name, sub.tracking_ref,
            sub.shipping_cost, sub.shipping_weight, sub.total_shipping_cost, sub.bill_no,
            sub.receipt_no, sub.receipt_picking_id, sub.receipt_date,
            sub.receipt_qty, sub.physical_receipt_date,
            sub.jumlah_hari, sub.akurat,
            sub.remarks, sub.notes, sub.total_amount, sub.net_amount,
            sub.snk_pos_ref, sub.pos_status_site, sub.receipt_status
        FROM (
            {inner_query}
        ) sub
        """
        return insert_query, params

    # =========================================================================
    # DEBUG — comment block ini jika tidak dibutuhkan
    # =========================================================================

    # def action_explain_query(self):
    #     """EXPLAIN ANALYZE untuk profiling query. Aktifkan saat debugging performa."""
    #     self.ensure_one()
    #     inner_query, params = self._build_delivery_query()
    #     explain_sql = f"EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) {inner_query}"
    #     t0 = time.perf_counter()
    #     self.env.cr.execute(explain_sql, params)
    #     rows = self.env.cr.fetchall()
    #     elapsed = time.perf_counter() - t0
    #     plan_text = "\n".join(r[0] for r in rows)
    #     _logger.info("[STO EXPLAIN] Query selesai dalam %.3fs\n%s", elapsed, plan_text)
    #     return {'type': 'ir.actions.act_window_close'}

    # =========================================================================
    # ACTION: Generate Report
    # =========================================================================

    def action_generate(self):
        """
        Flow:
        1. DELETE data lama milik user ini.
        2. INSERT...SELECT — satu round-trip, CTE berjalan di PostgreSQL.
        3. Return window action filtered by user_id.
        """
        self.ensure_one()
        if not self.date_start or not self.date_end:
            raise UserError(_("Date start and end are required."))
        if self.date_start > self.date_end:
            raise UserError(_("Start date must not be later than end date."))
        self._get_access_context()
        _logger.info(f">>>> START DELETE QUERY <<<<")
        self.env.cr.execute(
            "DELETE FROM report_sto_summary WHERE user_id = %(uid)s",
            {'uid': self.env.uid},
        )

        _logger.info(">>>> DONE DELETE QUERY <<<<")

        t0 = time.perf_counter()
        _logger.info(">>>> START INSERT QUERY <<<<")
        insert_query, params = self._build_insert_select_query()
        self.env.cr.execute(insert_query, params)
        row_count = self.env.cr.rowcount
        _logger.info(
            "[STO REPORT] INSERT...SELECT: %.3fs — %d rows",
            time.perf_counter() - t0, row_count,
        )

        if row_count == 0:
            raise UserError(_("Tidak ada data STO Summary pada periode yang dipilih."))

        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }

        # return {
        #     'type': 'ir.actions.act_window',
        #     'name': _('STO Summary'),
        #     'res_model': 'report.sto.summary',
        #     'view_mode': 'list,pivot',
        #     'target': 'current',
        #     'domain': [('user_id', '=', self.env.uid)],
        # }
