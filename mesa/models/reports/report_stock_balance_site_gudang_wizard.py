# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError
import io
import base64
import logging

_logger = logging.getLogger(__name__)

try:
    import xlsxwriter
except ImportError:
    xlsxwriter = None

class ReportStockBalanceSiteGudangWizard(models.TransientModel):
    _name = 'report.stock.balance.site.gudang.wizard'
    _description = 'Report Stock Balance Site vs Gudang Wizard'

    site_ids = fields.Many2many('snk.site', 'report_wizard_site_rel', 'wizard_id', 'site_id', string='Site', required=True,
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)],
        help="Pilih Site. Available stock akan di-generate berdasarkan mapping Location dari Warehouse di site ini.")
    gudang_ids = fields.Many2many('snk.site', 'report_wizard_gudang_rel', 'wizard_id', 'gudang_id', string='Gudang', required=True,
        domain=lambda self: [
            ('snk_pos_status', '=', 'gudang'),
            ('industry_id', 'in', self.env.user.allowed_industry_ids.ids),
        ],
        help="Pilih Gudang. Stock gudang akan di-generate berdasarkan mapping Location dari Warehouse di gudang ini.")


    analytic_ids = fields.Many2many('account.analytic.account', 'report_wizard_analytic_rel', 'wizard_id', 'analytic_id', string='Brand',
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ])
    category_ids = fields.Many2many('product.category', 'report_wizard_category_rel', 'wizard_id', 'category_id', string='Category')
    grade_ids = fields.Many2many('snk.product.grade', 'report_wizard_grade_rel', 'wizard_id', 'grade_id', string='Grade')
    product_tmpl_ids = fields.Many2many('product.template', 'report_wizard_product_tmpl_rel', 'wizard_id', 'product_tmpl_id', string='Product Template')
    product_ids = fields.Many2many('product.product', 'report_wizard_product_rel', 'wizard_id', 'product_id', string='Product Variant')

    def action_generate(self):
        self.ensure_one()
        if not self.site_ids or not self.gudang_ids:
            raise UserError(_("Site and Gudang are required."))
        allowed_industry_ids = self.env.user.allowed_industry_ids.ids
        allowed_brand_ids = self.env.user.allowed_analytic_ids.ids
        company_ids = self.env.companies.ids
        if not allowed_industry_ids:
            raise UserError(_("You do not have access to any site industry."))
        if not allowed_brand_ids:
            raise UserError(_("You do not have access to any brand."))
        if not company_ids:
            raise UserError(_("No allowed company is active for this report."))
        invalid_sites = (self.site_ids | self.gudang_ids).filtered(
            lambda site: site.industry_id.id not in allowed_industry_ids
        )
        if invalid_sites:
            raise UserError(_("You are not allowed to use the selected site(s): %s") % ", ".join(invalid_sites.mapped('display_name')))
        invalid_brands = self.analytic_ids.filtered(lambda brand: brand.id not in allowed_brand_ids)
        if invalid_brands:
            raise UserError(_("You are not allowed to use the selected brand(s): %s") % ", ".join(invalid_brands.mapped('display_name')))

        # 1. Validation & Basic Location Mapping
        for site in self.site_ids:
            if not site.warehouse_id:
                raise UserError(_("Site '%s' wajib di-mapping dengan Warehouse!" % site.snk_name))
        for gudang in self.gudang_ids:
            if not gudang.warehouse_id:
                raise UserError(_("Gudang '%s' wajib di-mapping dengan Warehouse!" % gudang.snk_name))
        invalid_company_sites = (self.site_ids | self.gudang_ids).filtered(
            lambda site: site.warehouse_id.company_id.id not in company_ids
            or site.company_id != site.warehouse_id.company_id
        )
        if invalid_company_sites:
            raise UserError(_("Site and warehouse company must match an active allowed company: %s") % ", ".join(invalid_company_sites.mapped('display_name')))

        # 2. Get All Relevant Products (Filter by Wiz or Movement)
        view_loc_site_ids = self.site_ids.mapped('warehouse_id.view_location_id').ids
        view_loc_gudang_ids = self.gudang_ids.mapped('warehouse_id.view_location_id').ids
        
        loc_site_ids_all = self.env['stock.location'].search([
            ('id', 'child_of', view_loc_site_ids), ('usage', '=', 'internal')
        ]).ids
        loc_gudang_ids_all = self.env['stock.location'].search([
            ('id', 'child_of', view_loc_gudang_ids), ('usage', '=', 'internal')
        ]).ids
        all_loc_ids = list(set(loc_site_ids_all + loc_gudang_ids_all))

        if not all_loc_ids:
            raise UserError(_("Lokasi Internal tidak ditemukan."))

        report_obj = self.env['report_stock_balance_site_gudang']
        report_obj._clean_previous_reports()

        product_domain = []
        if self.product_ids:
            product_domain.append(('id', 'in', self.product_ids.ids))
        else:
            query = """
                SELECT DISTINCT sml.product_id
                FROM stock_move_line sml
                JOIN stock_move sm ON sm.id = sml.move_id
                WHERE (sml.location_id IN %s OR sml.location_dest_id IN %s)
                  AND sm.company_id IN %s
            """
            self.env.cr.execute(query, (tuple(all_loc_ids), tuple(all_loc_ids), tuple(company_ids)))
            product_move_ids = [r[0] for r in self.env.cr.fetchall()]
            if not product_move_ids:
                raise UserError(_("Data transaksi tidak ditemukan."))
            product_domain.append(('id', 'in', product_move_ids))

        product_domain.append(('analytic_id', 'in', self.analytic_ids.ids or allowed_brand_ids))
        if self.category_ids: product_domain.append(('categ_id', 'in', self.category_ids.ids))
        if self.grade_ids: product_domain.append(('snk_grade_id', 'in', self.grade_ids.ids))
        if self.product_tmpl_ids: product_domain.append(('product_tmpl_id', 'in', self.product_tmpl_ids.ids))

        products = self.env['product.product'].search(product_domain)
        if not products:
            raise UserError(_("Tidak ada produk yang sesuai dengan kriteria."))

        # 3. Calculate Gudang Stock Map (Aggregate all selected gudangs)
        # Groups by (product_id, lot_id) across ALL gudang locations
        gudang_quants = self.env['stock.quant'].read_group(
            [('location_id', 'in', loc_gudang_ids_all), ('product_id', 'in', products.ids)],
            ['product_id', 'lot_id', 'quantity'], ['product_id', 'lot_id'], lazy=False
        )
        gudang_stock_map = {}
        for gq in gudang_quants:
            p_id = gq['product_id'][0]
            l_name = gq['lot_id'][1] if gq['lot_id'] else ''
            key = (p_id, l_name)
            gudang_stock_map[key] = gudang_stock_map.get(key, 0.0) + gq['quantity']

        # 4. Global Price Lookup (Hybrid: ORM for lst_price fallback, SQL for Pricelist overlay)
        vals_list = []
        _logger.info("Generating Automated Price Map (Hybrid ORM + SQL)")
        
        # 4.1. Base Price via ORM Bulk Read (Handles computed/non-stored lst_price safely)
        price_map = {item['id']: item['lst_price'] for item in products.read(['lst_price'])}
        
        # 4.2. Overlay Pricelist Prices via SQL (Match best valid price across all pricelists)
        price_query = """
            SELECT product_id, fixed_price FROM (
                SELECT 
                    pp.id as product_id,
                    pli.fixed_price,
                    ROW_NUMBER() OVER (
                        PARTITION BY pp.id
                        ORDER BY 
                            (CASE 
                                WHEN pli.product_id IS NOT NULL THEN 1 
                                WHEN pli.product_tmpl_id IS NOT NULL THEN 2 
                                WHEN pli.categ_id IS NOT NULL THEN 3 
                                ELSE 4 
                            END) ASC,
                            pli.min_quantity DESC,
                            pli.date_start DESC NULLS LAST, 
                            pli.id DESC
                    ) AS rn
                FROM product_product pp
                JOIN product_template pt ON pp.product_tmpl_id = pt.id
                JOIN product_pricelist_item pli ON (
                    (
                        pli.product_id = pp.id OR 
                        pli.product_tmpl_id = pt.id OR 
                        pli.categ_id = pt.categ_id OR 
                        (pli.product_id IS NULL AND pli.product_tmpl_id IS NULL AND pli.categ_id IS NULL)
                    )
                    AND (pli.date_start IS NULL OR pli.date_start <= CURRENT_DATE)
                    AND (pli.date_end IS NULL OR pli.date_end >= CURRENT_DATE)
                )
                WHERE pp.id IN %s
            ) t WHERE rn = 1
        """
        self.env.cr.execute(price_query, (tuple(products.ids),))
        for r in self.env.cr.fetchall():
            price_map[r[0]] = r[1]

        # Bulk compute Forecast (Incoming/Outgoing) per Location + Product via SQL
        _logger.info("Computing Granular Forecast (Incoming/Outgoing) via SQL...")
        incoming_map = {}
        outgoing_map = {}
        
        # Incoming: Moves heading towards our internal locations
        self.env.cr.execute("""
            SELECT location_dest_id, product_id, SUM(product_qty)
            FROM stock_move
            WHERE state IN ('confirmed', 'waiting', 'assigned', 'partially_available')
              AND location_dest_id IN %s
              AND product_id IN %s
              AND company_id IN %s
            GROUP BY location_dest_id, product_id
        """, (tuple(loc_site_ids_all), tuple(products.ids), tuple(company_ids)))
        for r in self.env.cr.fetchall():
            incoming_map[(r[0], r[1])] = r[2]

        # Outgoing: Moves leaving our internal locations
        self.env.cr.execute("""
            SELECT location_id, product_id, SUM(product_qty)
            FROM stock_move
            WHERE state IN ('confirmed', 'waiting', 'assigned', 'partially_available')
              AND location_id IN %s
              AND product_id IN %s
              AND company_id IN %s
            GROUP BY location_id, product_id
        """, (tuple(loc_site_ids_all), tuple(products.ids), tuple(company_ids)))
        for r in self.env.cr.fetchall():
            outgoing_map[(r[0], r[1])] = r[2]

        for site in self.site_ids:
            _logger.info("Processing Site: %s", site.snk_name)
            loc_site_ids = self.env['stock.location'].search([
                ('id', 'child_of', site.warehouse_id.view_location_id.id), ('usage', '=', 'internal')
            ]).ids
            
            # Get current available stock per (location, product, lot) for this site
            quants = self.env['stock.quant'].read_group(
                [('location_id', 'in', loc_site_ids), ('product_id', 'in', products.ids)],
                ['location_id', 'product_id', 'lot_id', 'quantity'], ['location_id', 'product_id', 'lot_id'], lazy=False
            )

            for q in quants:
                loc_id = q['location_id'][0]
                prod_id = q['product_id'][0]
                lot_name = q['lot_id'][1] if q['lot_id'] else ''
                
                # Retrieve price and forecast
                price_unit = price_map.get(prod_id, 0.0)
                
                vals_list.append({
                    'user_id': self.env.uid,
                    'site_id': site.id,
                    'location_id': loc_id,
                    'product_id': prod_id,
                    'lot_name': lot_name,
                    'qty_available': q['quantity'],
                    'stock_gudang': gudang_stock_map.get((prod_id, lot_name), 0.0),
                    'incoming_qty': incoming_map.get((loc_id, prod_id), 0.0),
                    'outgoing_qty': outgoing_map.get((loc_id, prod_id), 0.0),
                    'price_unit': price_unit,
                })

        # 5. Insert results
        _logger.info("Inserting %s records into report table...", len(vals_list))
        if vals_list:
            for i in range(0, len(vals_list), 1000):
                report_obj.create(vals_list[i:i + 1000])

        # return {
        #     'type': 'ir.actions.act_window',
        #     'name': 'Stock Balance Site vs Gudang',
        #     'res_model': 'report_stock_balance_site_gudang',
        #     'view_mode': 'list,pivot',
        #     'target': 'current',
        #     'domain': [('user_id', '=', self.env.uid)],
        # }
