# -*- coding: utf-8 -*-
import logging
from odoo import models, fields, api, _
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

class SnkReportStockCardWizard(models.TransientModel):
    _name = 'snk.report.stock.card.wizard'
    _description = 'Wizard Generate Stock Card MESA'

    # Filter dasar
    site_id = fields.Many2one('snk.site', string='Site', required=True,
                              domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)],
                              help="Pilih Site. Stock card akan di-generate berdasarkan mapping Location dari Warehouse di site ini.")
    date_start = fields.Datetime(string='Start Date', required=True,
                                 help="Titik awal waktu penarikan data mutasi. Mutasi sebelumnya akan diakumulasi menjadi Initial Balance.")
    date_end = fields.Datetime(string='End Date', required=True,
                               help="Titik akhir waktu (inklusif jam/menit). Biasanya 23:59:59 di akhir bulan.")

    # Opsi Grouping - Menentukan default Context Grouping di View List Result
    group_by = fields.Selection([
        ('location', 'Location'),
        ('category', 'Category'),
        ('analytic_id', 'Brand (Analytic)'),
        ('snk_grade_id', 'Grade'),
        ('product_template', 'Product Template'),
        ('product', 'Product'),
        ('lot_id', 'Lot/Serial Number')
    ], string="Default Group By", default='location', 
    help="Opsi ini menentukan tampilan awal List View ketika report selesai di-generate.")

    def action_generate(self):
        """
        Fungsi utama menggunakan SQL tingkat lanjut (CTE, UNION ALL dan SUM() OVER()).
        Hal ini wajib dilakukan karena performa ORM standard Odoo sangat lambat 
        jika harus menarik jutaan stock_move_line untuk sebuah perusahaan/site besar.
        """
        self.ensure_one()
        _logger.info("================ START GENERATE STOCK CARD REPORT ================")
        _logger.info("Parameters -> User: %s | Site: %s | Start Date: %s | End Date: %s", 
                     self.env.user.name, self.site_id.snk_name, self.date_start, self.date_end)

        report_obj = self.env['snk.report.stock.card']

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

        # [CHECKPOINT 2] Location Mapping Rules
        if not self.site_id.warehouse_id:
            raise UserError(_("Site '%s' wajib di-mapping dengan Warehouse. Harap lengkapi konfigurasi site terlebih dahulu!" % self.site_id.snk_name))
        warehouse = self.site_id.warehouse_id
        if warehouse.company_id.id not in company_ids or self.site_id.company_id != warehouse.company_id:
            raise UserError(_("Site and warehouse company must match an active allowed company."))
            
        view_loc_id = self.site_id.warehouse_id.view_location_id.id
        internal_locs = self.env['stock.location'].search([
            ('id', 'child_of', view_loc_id),
            ('usage', '=', 'internal')
        ])
        
        if not internal_locs:
            raise UserError(_("Tidak ditemukan internal location pada warehouse %s." % self.site_id.warehouse_id.name))

        # Clear only after all access and mapping validations pass.
        _logger.info("[SQL CTE - STEP 1] Membersihkan data report terdahulu milik ID User %s untuk penghematan space DB.", self.env.user.id)
        report_obj._clean_previous_reports()
            
        loc_ids = tuple(internal_locs.ids)
        _logger.info("[SQL CTE - STEP 2] Ditemukan %d Internal Locations untuk Site %s", len(loc_ids), self.site_id.snk_name)

        # [CHECKPOINT 3] Eksekusi Raw SQL
        _logger.info("[SQL CTE - STEP 3] Membangun temporary aggregate table (CTE) dan executing insert SQL...")

        sql_query = """
            INSERT INTO snk_report_stock_card (
                date, date_done, reference, origin, initial_balance, qty_in, qty_out, balance, 
                product_id, product_template_id, location_id, lot_id, site_id, 
                user_id, analytic_id, snk_grade_id, category_id, snk_material_code,
                create_uid, create_date, write_uid, write_date
            )
            WITH initial_balances AS (
                -- CTE: Total IN s/d sebelum date_start
                SELECT 
                    sml.product_id, 
                    sml.location_dest_id as location_id, 
                    sml.lot_id, 
                    SUM(sml.quantity) as qty
                FROM stock_move_line sml
                JOIN stock_move sm ON sm.id = sml.move_id
                LEFT JOIN stock_picking sp ON sp.id = sm.picking_id
                WHERE sm.state = 'done' 
                  AND sm.company_id = %s
                  AND sml.product_id IN (SELECT id FROM product_product WHERE analytic_id IN %s)
                  AND COALESCE(sp.scheduled_date, sm.date) < %s 
                  AND sml.location_dest_id IN %s
                GROUP BY sml.product_id, sml.location_dest_id, sml.lot_id
                
                UNION ALL
                
                -- CTE: Total OUT s/d sebelum date_start
                SELECT 
                    sml.product_id, 
                    sml.location_id as location_id, 
                    sml.lot_id, 
                    -SUM(sml.quantity) as qty
                FROM stock_move_line sml
                JOIN stock_move sm ON sm.id = sml.move_id
                LEFT JOIN stock_picking sp ON sp.id = sm.picking_id
                WHERE sm.state = 'done' 
                  AND sm.company_id = %s
                  AND sml.product_id IN (SELECT id FROM product_product WHERE analytic_id IN %s)
                  AND COALESCE(sp.scheduled_date, sm.date) < %s 
                  AND sml.location_id IN %s
                GROUP BY sml.product_id, sml.location_id, sml.lot_id
            ),
            grouped_initial AS (
                -- Kompresi Saldo Awal per Produk-Lokasi-Lot
                SELECT 
                    product_id, location_id, lot_id, SUM(qty) as initial_qty
                FROM initial_balances
                GROUP BY product_id, location_id, lot_id
                HAVING SUM(qty) != 0
            ),
            period_movements AS (
                -- CTE: Transaksi Masuk (IN) di dalam periode
                SELECT 
                    COALESCE(sp.scheduled_date, sm.date) as move_date,
                    sp.date_done,
                    sm.reference as move_ref,
                    COALESCE(sp.origin, sm.origin) as origin,
                    sml.product_id,
                    sml.location_dest_id as location_id,
                    sml.lot_id,
                    sml.id as origin_sml_id,
                    sml.quantity as qty_in,
                    0.0 as qty_out
                FROM stock_move_line sml
                JOIN stock_move sm ON sm.id = sml.move_id
                LEFT JOIN stock_picking sp ON sp.id = sm.picking_id
                WHERE sm.state = 'done' 
                  AND sm.company_id = %s
                  AND sml.product_id IN (SELECT id FROM product_product WHERE analytic_id IN %s)
                  AND COALESCE(sp.scheduled_date, sm.date) >= %s AND COALESCE(sp.scheduled_date, sm.date) <= %s
                  AND sml.location_dest_id IN %s
                  
                UNION ALL
                
                -- CTE: Transaksi Keluar (OUT) di dalam periode
                SELECT 
                    COALESCE(sp.scheduled_date, sm.date) as move_date,
                    sp.date_done,
                    sm.reference as move_ref,
                    COALESCE(sp.origin, sm.origin) as origin,
                    sml.product_id,
                    sml.location_id as location_id,
                    sml.lot_id,
                    sml.id as origin_sml_id,
                    0.0 as qty_in,
                    sml.quantity as qty_out
                FROM stock_move_line sml
                JOIN stock_move sm ON sm.id = sml.move_id
                LEFT JOIN stock_picking sp ON sp.id = sm.picking_id
                WHERE sm.state = 'done'
                  AND sm.company_id = %s
                  AND sml.product_id IN (SELECT id FROM product_product WHERE analytic_id IN %s)
                  AND COALESCE(sp.scheduled_date, sm.date) >= %s AND COALESCE(sp.scheduled_date, sm.date) <= %s
                  AND sml.location_id IN %s
            ),
            combined_card AS (
                -- Injeksi 'Initial Balance' sebagai baris pertama (Pseudo Transaction)
                SELECT 
                    0 as sort_order, 
                    0 origin_sml_id,
                    %s::timestamp as date,
                    NULL::timestamp as date_done,
                    'Initial Balance' as reference,
                    NULL::varchar as origin,
                    product_id, location_id, lot_id,
                    0.0 as qty_in, 0.0 as qty_out, 
                    initial_qty as running_qty
                FROM grouped_initial
                
                UNION ALL

                -- Injeksi 'Initial Balance' = 0 untuk produk yang HANYA ada di period_movements
                -- tapi TIDAK ada di grouped_initial (produk baru / saldo awal = 0)
                SELECT DISTINCT
                    0 as sort_order,
                    0 as origin_sml_id,
                    %s::timestamp as date,
                    NULL::timestamp as date_done,
                    'Initial Balance' as reference,
                    NULL::varchar as origin,
                    pm.product_id, pm.location_id, pm.lot_id,
                    0.0 as qty_in, 0.0 as qty_out,
                    0.0 as running_qty
                FROM period_movements pm
                WHERE NOT EXISTS (
                    SELECT 1 FROM grouped_initial gi
                    WHERE gi.product_id = pm.product_id
                      AND gi.location_id = pm.location_id
                      AND COALESCE(gi.lot_id, 0) = COALESCE(pm.lot_id, 0)
                )

                UNION ALL
                
                -- Gabungkan baris transaksi periode (In/Out)
                SELECT 
                    1 as sort_order,
                    origin_sml_id,
                    move_date as date,
                    date_done,
                    move_ref as reference,
                    origin,
                    product_id, location_id, lot_id,
                    qty_in, qty_out, (qty_in - qty_out) as running_qty
                FROM period_movements

                UNION ALL

                -- Injeksi 'Ending Balance' sebagai baris terakhir per grup
                -- running_qty diset 0 agar tidak merusak kalkulasi Balance berjalan di baris ini
                SELECT 
                    2 as sort_order,
                    999999999 as origin_sml_id,
                    %s::timestamp as date,
                    NULL::timestamp as date_done,
                    'Ending Balance' as reference,
                    NULL::varchar as origin,
                    product_id, location_id, lot_id,
                    SUM(qty_in) as qty_in, 
                    SUM(qty_out) as qty_out,
                    0.0 as running_qty
                FROM (
                    SELECT product_id, location_id, lot_id, qty_in, qty_out FROM period_movements
                    UNION ALL
                    -- Tambahkan keys dari saldo awal agar produk yang vakum tetap muncul baris Ending-nya
                    SELECT product_id, location_id, lot_id, 0.0 as qty_in, 0.0 as qty_out FROM grouped_initial
                ) summary_data
                GROUP BY product_id, location_id, lot_id
            )
            -- Main SELECT: Memasukkan ke dalam tabel snk_report_stock_card dengan Mapping
            SELECT 
                cd.date,
                cd.date_done,
                cd.reference,
                cd.origin,
                -- Initial balance: Saldo sebelum mutasi.
                -- Khusus baris Ending Balance (sort_order=2), ambil nilai dari baris pertama (Initial Balance row)
                CASE 
                    WHEN cd.sort_order = 2 THEN 
                        COALESCE(MAX(CASE WHEN cd.sort_order = 0 THEN cd.running_qty END) OVER (
                            PARTITION BY cd.product_id, cd.location_id, cd.lot_id
                        ), 0.0)
                    ELSE (SUM(cd.running_qty) OVER (
                        PARTITION BY cd.product_id, cd.location_id, cd.lot_id 
                        ORDER BY cd.sort_order, cd.date, cd.origin_sml_id
                    ) - cd.qty_in + cd.qty_out)
                END as initial_balance,
                cd.qty_in,
                cd.qty_out,
                -- Database level Kumulatif Running Balance
                SUM(cd.running_qty) OVER (
                    PARTITION BY cd.product_id, cd.location_id, cd.lot_id 
                    ORDER BY cd.sort_order, cd.date, cd.origin_sml_id
                ) as balance,
                cd.product_id,
                pp.product_tmpl_id as product_template_id,
                cd.location_id,
                cd.lot_id,
                %s as site_id,
                %s as user_id,
                -- 7 Parameter custom Odoo 18 / PT Megariamas
                pp.analytic_id as analytic_id,
                pt.snk_grade_id as snk_grade_id,
                pt.categ_id as category_id,
                pp.snk_material_code as snk_material_code,
                -- Standard ORM fields injection required
                %s as create_uid,
                timezone('UTC', current_timestamp) as create_date,
                %s as write_uid,
                timezone('UTC', current_timestamp) as write_date
            FROM combined_card cd
            JOIN product_product pp ON pp.id = cd.product_id
            JOIN product_template pt ON pt.id = pp.product_tmpl_id
        """

        params = (
            # Parameter untk cte_initial_balance_in
            warehouse.company_id.id, tuple(allowed_brand_ids), self.date_start, loc_ids,
            # Parameter untk cte_initial_balance_out
            warehouse.company_id.id, tuple(allowed_brand_ids), self.date_start, loc_ids,
            # Parameter untk cte_period_in
            warehouse.company_id.id, tuple(allowed_brand_ids), self.date_start, self.date_end, loc_ids,
            # Parameter untk cte_period_out
            warehouse.company_id.id, tuple(allowed_brand_ids), self.date_start, self.date_end, loc_ids,
            # Parameter date cast dummy 'Initial Balance' (grouped_initial), 'Initial Balance' (zero-balance) & 'Ending Balance'
            self.date_start, self.date_start, self.date_end,
            # Konstanta inject fields user & site info
            self.site_id.id, self.env.uid,
            self.env.uid, self.env.uid,
        )

        self.env.cr.execute(sql_query, params)
        inserted_rows = self.env.cr.rowcount
        _logger.info("[SQL CTE - FINISH] Sukses generate data! Total baris termutasi/dimasukkan: %s rows.", inserted_rows)

        # [NEW] [CHECKPOINT 4] Historical Incoming Backlog Snapshot
        _logger.info("[SQL] Computing Historical Incoming Backlog Snapshots (Initial & Ending)...")
        
        snapshot_sql = """
            UPDATE snk_report_stock_card rc
            SET incoming_qty = snapshot.qty
            FROM (
                SELECT sm.product_id, sm.location_dest_id, SUM(sm.product_uom_qty) as qty
                FROM stock_move sm
                WHERE sm.state NOT IN ('draft', 'cancel')
                  AND sm.company_id = %s
                  AND sm.product_id IN (SELECT id FROM product_product WHERE analytic_id IN %s)
                  AND sm.create_date <= %s
                  AND (sm.state != 'done' OR sm.date > %s)
                  AND sm.location_dest_id IN %s
                GROUP BY sm.product_id, sm.location_dest_id
            ) snapshot
            WHERE rc.user_id = %s
              AND rc.reference = %s
              AND rc.product_id = snapshot.product_id
              AND rc.location_id = snapshot.location_dest_id
        """
        
        # 4.1 Update Initial Balance Incoming Qty
        self.env.cr.execute(snapshot_sql, (warehouse.company_id.id, tuple(allowed_brand_ids), self.date_start, self.date_start, loc_ids, self.env.uid, 'Initial Balance'))
        _logger.info("Updated Initial Balance Incoming Qty snapshots.")
        
        # 4.2 Update Ending Balance Incoming Qty
        self.env.cr.execute(snapshot_sql, (warehouse.company_id.id, tuple(allowed_brand_ids), self.date_end, self.date_end, loc_ids, self.env.uid, 'Ending Balance'))
        _logger.info("Updated Ending Balance Incoming Qty snapshots.")
        _logger.info("================ FINISH GENERATE STOCK CARD REPORT ================")

        # [CHECKPOINT 4] Mengembalikan return Action 
        # (Odoo 18 View Mode list alih-alih tree) dengan context default group_by user
        
        # context_dict = {}
        # if self.group_by:
        #     # Map search_default sesuai UI requirement
        #     key = f'search_default_group_by_{self.group_by}'
        #     context_dict[key] = 1

        # return {
        #     'type': 'ir.actions.act_window',
        #     'name': 'Stock Card Generated',
        #     'res_model': 'snk.report.stock.card',
        #     'view_mode': 'list,pivot',
        #     'target': 'current',
        #     'domain': [('user_id', '=', self.env.uid)],
        #     'context': context_dict,
        # }
