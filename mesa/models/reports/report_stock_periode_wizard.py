# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class SnkReportStockPeriodeWizard(models.TransientModel):
    _name = 'snk.report.stock.periode.wizard'
    _description = 'Wizard Default Stock Periode (Stock Summary)'

    # Filters
    site_id = fields.Many2one(
        'snk.site', string='Site', required=True,
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)],
    )
    interval_type = fields.Selection([
        ('daily', 'Daily'),
        ('weekly', 'Weekly'),
        ('monthly', 'Monthly'),
        ('cutoff', 'Cut-off')
    ], string='Interval', required=True, default='monthly')
    date_start = fields.Datetime(string='Start Date', required=True)
    date_end = fields.Datetime(string='End Date', required=True)
    
    # Multi Filters
    category_ids = fields.Many2many('product.category', string='Product Categories')
    template_ids = fields.Many2many('product.template', string='Product Templates')
    product_ids = fields.Many2many('product.product', string='Product Variants')
    analytic_ids = fields.Many2many(
        'account.analytic.account', string='Brands',
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ],
    )
    snk_grade_ids = fields.Many2many('snk.product.grade', string='Grades')

    def action_generate(self):
        self.ensure_one()
        _logger.info("================ START GENERATE STOCK PERIODE REPORT ================")

        if self.date_start > self.date_end:
            raise UserError(_("Start date must not be later than end date."))
        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        company_ids = self.env.companies.ids
        if not allowed_industry_ids:
            raise UserError(_("You do not have access to any site industry."))
        if not allowed_brand_ids:
            raise UserError(_("You do not have access to any brand."))
        if not company_ids:
            raise UserError(_("No allowed company is active for this report."))
        if self.site_id.industry_id.id not in allowed_industry_ids:
            raise UserError(_("You are not allowed to use site '%s'.") % self.site_id.display_name)
        invalid_brands = self.analytic_ids.filtered(lambda brand: brand.id not in allowed_brand_ids)
        if invalid_brands:
            raise UserError(_("You are not allowed to use the selected brand(s): %s") % ", ".join(invalid_brands.mapped('display_name')))

        warehouse = self.site_id.warehouse_id
        if not warehouse:
            raise UserError(_("Site '%s' wajib di-mapping dengan Warehouse!") % self.site_id.snk_name)
        if warehouse.company_id.id not in company_ids or self.site_id.company_id != warehouse.company_id:
            raise UserError(_("Site and warehouse company must match an active allowed company."))
            
        view_loc_id = warehouse.view_location_id.id
        internal_locs = self.env['stock.location'].search([
            ('id', 'child_of', view_loc_id),
            ('usage', '=', 'internal')
        ])
        if not internal_locs:
            raise UserError(_("Tidak ditemukan internal location pada warehouse %s." % warehouse.name))
        loc_ids = tuple(internal_locs.ids)

        report_obj = self.env['snk.report.stock.periode']
        report_obj._clean_previous_reports()

        rf_in_types = []
        if warehouse.in_type_id: rf_in_types.append(warehouse.in_type_id.id)
        if hasattr(warehouse, 'snk_pos_in_id') and warehouse.snk_pos_in_id: rf_in_types.append(warehouse.snk_pos_in_id.id)
        rf_in_types = tuple(rf_in_types) if rf_in_types else (-1,)

        sales_types = []
        if warehouse.out_type_id: sales_types.append(warehouse.out_type_id.id)
        if hasattr(warehouse, 'snk_pos_out_id') and warehouse.snk_pos_out_id: sales_types.append(warehouse.snk_pos_out_id.id)
        sales_types = tuple(sales_types) if sales_types else (-1,)

        import calendar
        from dateutil.relativedelta import relativedelta

        # Build dynamic where
        effective_brand_ids = self.analytic_ids.ids or allowed_brand_ids
        where_conds = ["sm.state = 'done'", "sm.company_id = %s", "(sml.location_dest_id IN %s OR sml.location_id IN %s)", "pp.analytic_id IN %s"]
        where_params = [warehouse.company_id.id, loc_ids, loc_ids, tuple(effective_brand_ids)]

        if self.category_ids:
            where_conds.append("pt.categ_id IN %s")
            where_params.append(tuple(self.category_ids.ids))
        if self.template_ids:
            where_conds.append("pt.id IN %s")
            where_params.append(tuple(self.template_ids.ids))
        if self.product_ids:
            where_conds.append("pp.id IN %s")
            where_params.append(tuple(self.product_ids.ids))
        if self.snk_grade_ids:
            where_conds.append("pt.snk_grade_id IN %s")
            where_params.append(tuple(self.snk_grade_ids.ids))

        base_where_clause = " AND ".join(where_conds)

        # Optimization 1: Temp Table for Pricelist (Do ROW_NUMBER() on entire table only ONCE)
        self.env.cr.execute("DROP TABLE IF EXISTS temp_valid_pricelists")
        self.env.cr.execute("""
            CREATE TEMP TABLE temp_valid_pricelists AS
            SELECT * FROM (
                SELECT
                    pli.fixed_price,
                    pli.product_id,
                    pli.product_tmpl_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(pli.product_id, 0), COALESCE(pli.product_tmpl_id, 0)
                        ORDER BY pli.date_start DESC NULLS LAST, pli.id DESC
                    ) AS rn
                FROM product_pricelist_item pli
            ) t WHERE rn = 1
        """)
        self.env.cr.execute("CREATE INDEX idx_tvp_prod ON temp_valid_pricelists(COALESCE(product_id, 0))")
        self.env.cr.execute("CREATE INDEX idx_tvp_tmpl ON temp_valid_pricelists(product_tmpl_id)")

        # Optimization 2: Pre-calculate Master Rolling Balances 
        # (Grab all products that have ANY movement until End Date)
        self.env.cr.execute("DROP TABLE IF EXISTS temp_rolling_balances")
        init_balances_sql = f"""
            CREATE TEMP TABLE temp_rolling_balances AS
            SELECT 
                pp.id as product_id,
                -- Menjadi saldo awal bulan 1 (Total pergerakan SAAT date_start ke bawah)
                SUM(CASE WHEN sm.date < %s THEN (CASE WHEN sml.location_dest_id IN %s THEN sml.quantity ELSE -sml.quantity END) ELSE 0 END) as balance
            FROM stock_move_line sml
            JOIN stock_move sm ON sm.id = sml.move_id
            JOIN product_product pp ON pp.id = sml.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
            WHERE {base_where_clause} AND sm.date <= %s
            GROUP BY pp.id
        """
        init_params = [self.date_start, loc_ids] + where_params + [self.date_end]
        self.env.cr.execute(init_balances_sql, init_params)
        self.env.cr.execute("CREATE INDEX idx_trb_prod ON temp_rolling_balances(product_id)")

        import pytz
        user_tz = pytz.timezone(self.env.user.tz or 'UTC')
        current_start = pytz.utc.localize(self.date_start).astimezone(user_tz)
        local_end_date = pytz.utc.localize(self.date_end).astimezone(user_tz)
        
        total_inserted = 0
        
        while current_start <= local_end_date:
            import datetime
            if self.interval_type == 'daily':
                period_end = current_start.replace(hour=23, minute=59, second=59)
                if period_end > local_end_date:
                    period_end = local_end_date
                period_name = current_start.strftime('%d %b %Y')
                period_date = current_start.replace(hour=0, minute=0, second=0).strftime('%Y-%m-%d')
                next_start = current_start.replace(hour=0, minute=0, second=0) + relativedelta(days=1)
                
            elif self.interval_type == 'weekly':
                days_to_sunday = 6 - current_start.weekday()
                period_end = current_start + relativedelta(days=days_to_sunday)
                period_end = period_end.replace(hour=23, minute=59, second=59)
                if period_end > local_end_date:
                    period_end = local_end_date
                period_name = f"{current_start.strftime('%d %b')} - {period_end.strftime('%d %b %Y')}"
                period_date = current_start.replace(hour=0, minute=0, second=0).strftime('%Y-%m-%d')
                next_start = period_end.replace(hour=0, minute=0, second=0) + relativedelta(days=1)
                
            elif self.interval_type == 'monthly':
                _, last_day = calendar.monthrange(current_start.year, current_start.month)
                period_end = current_start.replace(day=last_day, hour=23, minute=59, second=59)
                if period_end > local_end_date:
                    period_end = local_end_date
                period_name = current_start.strftime('%B %Y')
                period_date = current_start.replace(day=1, hour=0, minute=0, second=0).strftime('%Y-%m-%d')
                next_start = period_end.replace(day=1, hour=0, minute=0, second=0) + relativedelta(months=1)

            else:  # cutoff
                period_end = local_end_date
                if current_start.month == period_end.month and current_start.year == period_end.year:
                    # e.g. 1-10 April 2026
                    period_name = f"{current_start.day}-{period_end.day} {current_start.strftime('%B %Y')}"
                else:
                    # e.g. 25 Mar 2026 - 05 Apr 2026
                    period_name = f"{current_start.strftime('%d %b %Y')} - {period_end.strftime('%d %b %Y')}"
                period_date = current_start.strftime('%Y-%m-%d')
                # Ensure loop terminates after one run
                next_start = local_end_date + relativedelta(days=1)
                
            # Konversi kembali ke UTC untuk pencarian database yang persis
            utc_current_start = current_start.astimezone(pytz.utc).replace(tzinfo=None)
            utc_period_end = period_end.astimezone(pytz.utc).replace(tzinfo=None)
                
            sql_query = f"""
                INSERT INTO snk_report_stock_periode (
                    user_id, site_id, product_id, product_template_id, default_code, template_default_code,
                    category_id, analytic_id, snk_grade_id, period_name, period_date,
                    opening_balance, qty_in, qty_sales_return, qty_sales, qty_out, qty_adj_in, qty_adj_out, ending_balance,
                    price_unit, total_amount,
                    create_uid, create_date, write_uid, write_date
                )
                WITH sml_period AS (
                    SELECT 
                        sml.product_id,
                        sml.location_id,
                        loc_src.usage as src_usage,
                        sml.location_dest_id,
                        loc_dest.usage as dest_usage,
                        sm.picking_type_id,
                        sml.quantity
                    FROM stock_move_line sml
                    JOIN stock_move sm ON sm.id = sml.move_id
                    JOIN stock_location loc_src ON loc_src.id = sml.location_id
                    JOIN stock_location loc_dest ON loc_dest.id = sml.location_dest_id
                    WHERE sm.state = 'done' 
                      AND sm.company_id = %s
                      AND sm.date >= %s AND sm.date <= %s
                      AND (sml.location_id IN %s OR sml.location_dest_id IN %s)
                ),
                agg_data AS (
                    SELECT
                        rb.product_id,
                        rb.balance as opening_balance,
                        
                        -- IN
                        SUM(CASE WHEN sml.location_dest_id IN %s AND COALESCE(sml.picking_type_id, 0) NOT IN %s AND sml.src_usage != 'inventory' THEN sml.quantity ELSE 0 END) as qty_in,
                        
                        -- RETUR SALES
                        SUM(CASE WHEN sml.location_dest_id IN %s AND COALESCE(sml.picking_type_id, 0) IN %s AND sml.src_usage != 'inventory' THEN sml.quantity ELSE 0 END) as qty_rf_in,
                        
                        -- ADJ IN
                        SUM(CASE WHEN sml.location_dest_id IN %s AND sml.src_usage = 'inventory' THEN sml.quantity ELSE 0 END) as qty_adj_in,
                        
                        -- SALES
                        SUM(CASE WHEN sml.location_id IN %s AND COALESCE(sml.picking_type_id, 0) IN %s AND sml.dest_usage != 'inventory' THEN -sml.quantity ELSE 0 END) as qty_sales,
                        
                        -- OUT
                        SUM(CASE WHEN sml.location_id IN %s AND COALESCE(sml.picking_type_id, 0) NOT IN %s AND sml.dest_usage != 'inventory' THEN -sml.quantity ELSE 0 END) as qty_out,
                        
                        -- ADJ OUT
                        SUM(CASE WHEN sml.location_id IN %s AND sml.dest_usage = 'inventory' THEN -sml.quantity ELSE 0 END) as qty_adj_out
                    FROM temp_rolling_balances rb
                    LEFT JOIN sml_period sml ON sml.product_id = rb.product_id
                    GROUP BY rb.product_id, rb.balance
                )
                SELECT 
                    %s, %s, agg.product_id, pp.product_tmpl_id, pp.default_code, pt.default_code,
                    pt.categ_id, pp.analytic_id, pp.snk_grade_id, %s, %s,
                    
                    COALESCE(agg.opening_balance, 0), COALESCE(agg.qty_in, 0), COALESCE(agg.qty_rf_in, 0),
                    COALESCE(agg.qty_sales, 0), COALESCE(agg.qty_out, 0), COALESCE(agg.qty_adj_in, 0), COALESCE(agg.qty_adj_out, 0),
                    (COALESCE(agg.opening_balance, 0) + COALESCE(agg.qty_in, 0) + COALESCE(agg.qty_rf_in, 0) + COALESCE(agg.qty_sales, 0) + COALESCE(agg.qty_out, 0) + COALESCE(agg.qty_adj_in, 0) + COALESCE(agg.qty_adj_out, 0)) as ending_balance,
                    
                    COALESCE(vpi_var.fixed_price, vpi_tmpl.fixed_price, pt.list_price, 0.0) as price_unit,
                    (COALESCE(agg.opening_balance, 0) + COALESCE(agg.qty_in, 0) + COALESCE(agg.qty_rf_in, 0) + COALESCE(agg.qty_sales, 0) + COALESCE(agg.qty_out, 0) + COALESCE(agg.qty_adj_in, 0) + COALESCE(agg.qty_adj_out, 0)) * COALESCE(vpi_var.fixed_price, vpi_tmpl.fixed_price, pt.list_price, 0.0) as total_amount,

                    %s, timezone('UTC', current_timestamp), %s, timezone('UTC', current_timestamp)
                FROM agg_data agg
                JOIN product_product pp ON pp.id = agg.product_id
                JOIN product_template pt ON pt.id = pp.product_tmpl_id
                LEFT JOIN temp_valid_pricelists vpi_var
                    ON vpi_var.product_id = agg.product_id
                LEFT JOIN temp_valid_pricelists vpi_tmpl
                    ON vpi_tmpl.product_id IS NULL AND vpi_tmpl.product_tmpl_id = pp.product_tmpl_id
                WHERE (COALESCE(agg.opening_balance, 0) != 0 OR COALESCE(agg.qty_in, 0) != 0 OR COALESCE(agg.qty_rf_in, 0) != 0 OR COALESCE(agg.qty_sales, 0) != 0 OR COALESCE(agg.qty_out, 0) != 0 OR COALESCE(agg.qty_adj_in, 0) != 0 OR COALESCE(agg.qty_adj_out, 0) != 0)
            """

            # sml_period filtering mapping
            period_params = [warehouse.company_id.id, utc_current_start, utc_period_end, loc_ids, loc_ids]

            # agg_data case conditionals mapping
            agg_params = [
                loc_ids, rf_in_types,
                loc_ids, rf_in_types,
                loc_ids,
                loc_ids, sales_types,
                loc_ids, sales_types,
                loc_ids
            ]

            select_params = [self.env.uid, self.site_id.id, period_name, period_date, self.env.uid, self.env.uid]

            final_params = period_params + agg_params + select_params
            self.env.cr.execute(sql_query, final_params)
            
            inserted_rows = self.env.cr.rowcount
            _logger.info("Inserted %s rows for periode %s.", inserted_rows, period_name)
            total_inserted += inserted_rows
            
            # FAST FORWARD: Update opening balances for the next loop!
            update_sql = f"""
                UPDATE temp_rolling_balances rb
                SET balance = rb.balance + COALESCE(net.qty, 0)
                FROM (
                    SELECT 
                        sml.product_id,
                        SUM(CASE WHEN sml.location_dest_id IN %s THEN sml.quantity ELSE -sml.quantity END) as qty
                    FROM stock_move_line sml
                    JOIN stock_move sm ON sm.id = sml.move_id
                    WHERE sm.state = 'done'
                      AND sm.company_id = %s
                      AND sm.date >= %s AND sm.date <= %s
                      AND (sml.location_id IN %s OR sml.location_dest_id IN %s)
                    GROUP BY sml.product_id
                ) net
                WHERE rb.product_id = net.product_id
            """
            self.env.cr.execute(update_sql, [loc_ids, warehouse.company_id.id, utc_current_start, utc_period_end, loc_ids, loc_ids])

            # Maju ke awal bulan/minggu/hari berikutnya
            current_start = next_start
            
        # Cleanup
        self.env.cr.execute("DROP TABLE IF EXISTS temp_valid_pricelists")
        self.env.cr.execute("DROP TABLE IF EXISTS temp_rolling_balances")

        _logger.info("================ FINISH GENERATE STOCK PERIODE REPORT | TOTAL: %s rows ================", total_inserted)

        # return {
        #     'type': 'ir.actions.act_window',
        #     'name': 'Stock Of Periode (Stock Summary)',
        #     'res_model': 'snk.report.stock.periode',
        #     'view_mode': 'list,pivot',
        #     'target': 'current',
        #     'domain': [('user_id', '=', self.env.uid)]
        # }
