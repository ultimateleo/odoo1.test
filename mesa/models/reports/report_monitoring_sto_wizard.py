# -*- coding: utf-8 -*-

from odoo import api, fields, models, _
from odoo.exceptions import UserError
import logging
import time

_logger = logging.getLogger(__name__)

class ReportMonitoringSTOWizard(models.TransientModel):
    _name = 'report.monitoring.sto.wizard'
    _description = 'Report Monitoring STO Wizard'

    date_start = fields.Datetime(string='Start Date', required=True, default=fields.Datetime.today)
    date_end = fields.Datetime(string='End Date', required=True, default=fields.Datetime.today)

    source_site_ids = fields.Many2many('snk.site', 'report_monitoring_sto_src_site_rel', 'wizard_id', 'site_id', string='Source Sites',
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)])
    destination_site_ids = fields.Many2many('snk.site', 'report_monitoring_sto_dest_site_rel', 'wizard_id', 'site_id', string='Destination Sites',
        domain=lambda self: [('industry_id', 'in', self.env.user.allowed_industry_ids.ids)])
    operation_ids = fields.Many2many('stock.picking.type', 'report_monitoring_sto_operation_rel', 'wizard_id', 'operation_id', string='Operations')

    analytic_ids = fields.Many2many('account.analytic.account', 'report_monitoring_sto_analytic_rel', 'wizard_id', 'analytic_id', string='Brand',
        domain=lambda self: [
            ('snk_brand', '=', True),
            ('id', 'in', self.env.user.allowed_analytic_ids.ids),
        ])
    category_ids = fields.Many2many('product.category', 'report_monitoring_sto_category_rel', 'wizard_id', 'category_id', string='Category')
    grade_ids = fields.Many2many('snk.product.grade', 'report_monitoring_sto_grade_rel', 'wizard_id', 'grade_id', string='Grade')
    product_tmpl_ids = fields.Many2many('product.template', 'report_monitoring_sto_prod_tmpl_rel', 'wizard_id', 'product_tmpl_id', string='Product Template')
    product_ids = fields.Many2many('product.product', 'report_monitoring_sto_product_rel', 'wizard_id', 'product_id', string='Product Variant')

    def _build_pricelist_cache(self, product_ids=None):
        """Preload fixed pricelist items hanya untuk produk yang diproses untuk performa cepat.
        Cache key uses product or template ID and does not depend on grade.
        """
        if product_ids is not None and not product_ids:
            return {'by_product': {}, 'by_tmpl': {}}

        if product_ids:
            products = self.env['product.product'].browse(product_ids)

        query = """
            SELECT ppi.product_id, ppi.product_tmpl_id, ppi.fixed_price,
                   ppi.date_start, ppi.date_end
            FROM product_pricelist_item ppi
            WHERE ppi.compute_price = 'fixed'
        """
        params = []
        if product_ids:
            tmpl_ids = products.mapped('product_tmpl_id').ids
            if tmpl_ids:
                query += " AND (ppi.product_id IN %s OR ppi.product_tmpl_id IN %s)"
                params = [tuple(product_ids), tuple(tmpl_ids)]
            else:
                query += " AND ppi.product_id IN %s"
                params = [tuple(product_ids)]

        query += " ORDER BY ppi.date_start DESC NULLS LAST, ppi.id DESC"

        self.env.cr.execute(query, params or None)
        rows = self.env.cr.fetchall()
        # Cache key: product/template ID -> [(price, date_start, date_end), ...]
        cache = {'by_product': {}, 'by_tmpl': {}}
        for product_id, tmpl_id, price, date_start, date_end in rows:
            if product_id:
                cache['by_product'].setdefault(product_id, []).append((price, date_start, date_end))
            elif tmpl_id:
                cache['by_tmpl'].setdefault(tmpl_id, []).append((price, date_start, date_end))
        return cache

    def _get_unit_price_from_cache(self, cache, product, date):
        """Resolve price by variant, then template, for the reference date."""
        def date_matches(date_start, date_end, ref_date):
            if not ref_date:
                return True
            if date_start and date_start.date() > ref_date.date():
                return False
            if date_end and date_end.date() < ref_date.date():
                return False
            return True

        tmpl_id = product.product_tmpl_id.id

        # A variant-specific rule is more specific than a template rule.
        for price, ds, de in cache['by_product'].get(product.id, []):
            if date_matches(ds, de, date):
                return price
        for price, ds, de in cache['by_tmpl'].get(tmpl_id, []):
            if date_matches(ds, de, date):
                return price
        return 0.0

    def _get_qty_done(self, move):
        """Helper for Odoo version compatibility for quantity done."""
        if hasattr(move, 'quantity_done') and move.quantity_done > 0:
            return move.quantity_done
        if hasattr(move, 'quantity'):
            return move.quantity
        return 0.0

    def _get_delivery_pickings(self, sto):
        """Ambil DO pickings; brand authority diterapkan pada stock.move."""
        picking_domain = [
            ('transfer_id', '=', sto.id),
            ('state', '!=', 'cancel'),
        ]
        if self.operation_ids:
            picking_domain.append(('picking_type_id', 'in', self.operation_ids.ids))
        else:
            picking_domain.append(('picking_type_id.code', '=', 'outgoing'))

        return self.env['stock.picking'].search(picking_domain)

    def action_generate(self):
        self.ensure_one()
        report_obj = self.env['report.monitoring.sto']

        if not self.date_start or not self.date_end:
            raise UserError(_("Date start and end are required."))
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
        invalid_sites = (self.source_site_ids | self.destination_site_ids).filtered(
            lambda site: site.industry_id.id not in allowed_industry_ids
        )
        if invalid_sites:
            raise UserError(_("You are not allowed to use the selected site(s): %s") % ", ".join(invalid_sites.mapped('display_name')))
        invalid_brands = self.analytic_ids.filtered(lambda brand: brand.id not in allowed_brand_ids)
        if invalid_brands:
            raise UserError(_("You are not allowed to use the selected brand(s): %s") % ", ".join(invalid_brands.mapped('display_name')))

        accessible_sites = self.env['snk.site'].search([
            ('industry_id', 'in', allowed_industry_ids),
        ])
        source_site_ids = self.source_site_ids.ids or accessible_sites.ids
        destination_site_ids = self.destination_site_ids.ids or accessible_sites.ids
        effective_brand_ids = self.analytic_ids.ids or allowed_brand_ids

        # 1. Clean previous session
        _logger.info(">>> [STEP 1] Cleaning previous session reports...")
        t0 = time.time()
        report_obj._clean_previous_reports()
        t1 = time.time()
        _logger.info(">>> [PROFILING] STEP 1 took %.2f seconds", t1 - t0)
        
        # Track allocated receipt quantities globally to prevent over-counting in consolidated pickings
        receipt_demand_allocated = {}
        receipt_done_allocated = {}

        # Build Valid Products Domain
        product_domain = [('analytic_id', 'in', effective_brand_ids)]
        if self.category_ids:
            product_domain.append(('categ_id', 'in', self.category_ids.ids))
        if self.grade_ids:
            product_domain.append(('snk_grade_id', 'in', self.grade_ids.ids))
        if self.product_tmpl_ids:
            product_domain.append(('product_tmpl_id', 'in', self.product_tmpl_ids.ids))
        if self.product_ids:
            product_domain.append(('id', 'in', self.product_ids.ids))

        valid_product_ids = None
        if product_domain:
            valid_product_ids = self.env['product.product'].search(product_domain).ids
            if not valid_product_ids:
                raise UserError(_("Tidak ada produk yang sesuai dengan kriteria filter yang dipilih."))

        vals_list = []
        # (Pricelist cache akan di-load secara dinamis per flow)
        _logger.info(">>> [STEP 2] Preparing dependencies...")
        t2 = time.time()

        # FIX: track receipt picking yang sudah dihitung agar tidak double-count
        # Key: (receipt_picking_id, product_id) → mencegah 1 receipt dihitung N kali lintas DO
        attributed_receipt_keys = set()

        # Helper method for bulk receipt mapping
        def _get_receipt_mapping(do_move_ids):
            mapping = {}
            if not do_move_ids:
                return mapping
            
            # Use raw SQL to traverse stock_move_move_rel instantly
            query = """
                SELECT smr.move_orig_id, smr.move_dest_id
                FROM stock_move_move_rel smr
                JOIN stock_move orig ON smr.move_orig_id = orig.id
                JOIN stock_move dest ON smr.move_dest_id = dest.id
                JOIN stock_picking_type pt ON dest.picking_type_id = pt.id
                WHERE smr.move_orig_id IN %s
                AND pt.code = 'incoming'
                AND dest.state != 'cancel'
                AND orig.product_id = dest.product_id
                AND orig.company_id IN %s
                AND dest.company_id = orig.company_id
            """
            self.env.cr.execute(query, (tuple(do_move_ids), tuple(company_ids)))
            rel_data = self.env.cr.fetchall()
            
            for m_id, dest_id in rel_data:
                if m_id not in mapping:
                    mapping[m_id] = []
                mapping[m_id].append(dest_id)
            return mapping

        # ==========================================
        # 2A. Process MRP/STO Driven Flow
        # ==========================================
        domain_sto = [
            ('sto_date', '>=', self.date_start),
            ('sto_date', '<=', self.date_end),
            ('state', 'not in', ['draft', 'cancel'])
        ]
        domain_sto.append(('source_site_ids', 'in', source_site_ids))
        domain_sto.append(('destination_site_id', 'in', destination_site_ids))

        stos = self.env['snk.stock.transfer.order'].search(domain_sto)
        t3 = time.time()
        _logger.info(">>> [PROFILING] STEP 2 (Dependencies) took %.2f seconds", t3 - t2)
        _logger.info(">>> [STEP 3A] Found %s STO Documents to process.", len(stos))

        if stos:
            # BULK FETCH: STO Lines
            sto_lines = self.env['snk.stock.transfer.order.line'].search([('transfer_id', 'in', stos.ids)])
            if valid_product_ids is not None:
                sto_lines = sto_lines.filtered(lambda l: l.product_id.id in valid_product_ids)
            
            sto_lines_by_sto = {}
            for line in sto_lines:
                if line.transfer_id.id not in sto_lines_by_sto:
                    sto_lines_by_sto[line.transfer_id.id] = []
                sto_lines_by_sto[line.transfer_id.id].append(line)

            # BULK FETCH: Delivery Pickings
            picking_domain = [('transfer_id', 'in', stos.ids), ('state', '!=', 'cancel')]
            if self.operation_ids:
                picking_domain.append(('picking_type_id', 'in', self.operation_ids.ids))
            else:
                picking_domain.append(('picking_type_id.code', '=', 'outgoing'))
            delivery_pickings = self.env['stock.picking'].search(picking_domain)
            dos_by_sto = {}
            for dp in delivery_pickings:
                if dp.transfer_id.id not in dos_by_sto:
                    dos_by_sto[dp.transfer_id.id] = []
                dos_by_sto[dp.transfer_id.id].append(dp)

            # BULK FETCH: DO Moves
            do_moves_domain = [
                ('picking_id', 'in', delivery_pickings.ids),
                ('state', '!=', 'cancel'),
                ('company_id', 'in', company_ids),
            ]
            if valid_product_ids is not None:
                do_moves_domain.append(('product_id', 'in', valid_product_ids))
            all_do_moves = self.env['stock.move'].search(do_moves_domain)
            
            # Group moves by picking and product
            _logger.info(">>> [STEP 3A.1] Grouping %s DO moves...", len(all_do_moves))
            do_moves_grouped = {}
            for m in all_do_moves:
                pid = m.picking_id.id
                prod_id = m.product_id.id
                if pid not in do_moves_grouped:
                    do_moves_grouped[pid] = {}
                if prod_id not in do_moves_grouped[pid]:
                    do_moves_grouped[pid][prod_id] = []
                do_moves_grouped[pid][prod_id].append(m)

            # BULK FETCH: Receipt Moves Mapping
            receipt_mapping = _get_receipt_mapping(all_do_moves.ids)

            # Build Pricelist Cache STO
            sto_prod_ids = {m.product_id.id for m in all_do_moves}
            for sto in stos:
                for line in sto_lines_by_sto.get(sto.id, []):
                    sto_prod_ids.add(line.product_id.id)
            pricelist_cache_sto = self._build_pricelist_cache(list(sto_prod_ids))

            t3_bulk = time.time()
            _logger.info(">>> [PROFILING] STEP 3A Bulk Fetch took %.2f seconds", t3_bulk - t3)
            # In-Memory Processing
            _logger.info(">>> [STEP 3A.2] Iterating over %s STO documents...", len(stos))
            for sto in stos:
                pending_pr_lines = list(sto_lines_by_sto.get(sto.id, []))
                sto_dos = dos_by_sto.get(sto.id, [])

                for do in sto_dos:
                    moves_by_product = do_moves_grouped.get(do.id, {})
                    
                    for product_id_key, moves in moves_by_product.items():
                        product = moves[0].product_id

                        matched_line = next((l for l in pending_pr_lines if l.product_id.id == product_id_key), False)
                        pr_demand_qty = matched_line.to_order if matched_line else 0.0
                        transfer_line_id = matched_line.id if matched_line else False
                        if matched_line:
                            pending_pr_lines.remove(matched_line)

                        do_initial_qty = sum(m.product_uom_qty for m in moves)
                        do_actual_qty = sum(self._get_qty_done(m) for m in moves)

                        receipt_move_ids = []
                        for m in moves:
                            if m.id in receipt_mapping:
                                receipt_move_ids.extend(receipt_mapping[m.id])
                        receipt_moves = self.env['stock.move'].browse(list(set(receipt_move_ids)))
                        
                        receipt_picking_id = False
                        in_initial_qty = 0.0
                        in_actual_qty = 0.0
                        if receipt_moves:
                            rp_id = receipt_moves[0].picking_id.id
                            # Include do.id agar tiap pasangan DO-Receipt dihitung independen
                            # (fix: STO split 2 DO Above/Below Min ke receipt yang sama)
                            rkey = (do.id, rp_id, product_id_key)
                            receipt_picking_id = rp_id
                            if rkey not in attributed_receipt_keys:
                                attributed_receipt_keys.add(rkey)
                                in_initial_qty = sum(rm.product_uom_qty for rm in receipt_moves)
                                in_actual_qty = sum(self._get_qty_done(rm) for rm in receipt_moves)
                            receipt_picking_id = receipt_moves[0].picking_id.id

                        src_site = matched_line.source_site_id if matched_line else (sto.source_site_ids[0] if sto.source_site_ids else False)
                        dest_site = matched_line.destination_site_id if matched_line else sto.destination_site_id

                        do_source_doc = moves[0].origin if moves else False
                        
                        do_received_qty = 0.0
                        do_actual_recv_qty = 0.0
                        for rm in receipt_moves:
                            avail_demand = rm.product_uom_qty - receipt_demand_allocated.get(rm.id, 0.0)
                            alloc_demand = min(max(0.0, do_initial_qty - do_received_qty), avail_demand)
                            if alloc_demand > 0:
                                do_received_qty += alloc_demand
                                receipt_demand_allocated[rm.id] = receipt_demand_allocated.get(rm.id, 0.0) + alloc_demand
                            
                            if rm.state == 'done':
                                avail_done = self._get_qty_done(rm) - receipt_done_allocated.get(rm.id, 0.0)
                                alloc_done = min(max(0.0, do_actual_qty - do_actual_recv_qty), avail_done)
                                if alloc_done > 0:
                                    do_actual_recv_qty += alloc_done
                                    receipt_done_allocated[rm.id] = receipt_done_allocated.get(rm.id, 0.0) + alloc_done

                        do_unit_price = self._get_unit_price_from_cache(pricelist_cache_sto, product, do.scheduled_date)

                        vals_list.append({
                            'user_id': self.env.uid,
                            'date': sto.sto_date,
                            'transfer_id': sto.id,
                            'transfer_line_id': transfer_line_id,
                            'delivery_picking_id': do.id,
                            'receipt_picking_id': receipt_picking_id,
                            'product_template_id': product.product_tmpl_id.id,
                            'product_id': product.id,
                            'analytic_id': product.analytic_id.id,
                            'snk_grade_id': product.snk_grade_id.id,
                            'category_id': product.categ_id.id,
                            'snk_material_code': product.snk_material_code,
                            'source_site_id': src_site.id if src_site else False,
                            'destination_site_id': dest_site.id if dest_site else False,
                            'pr_demand_qty': pr_demand_qty,
                            'pr_actual_qty': do_initial_qty,
                            'do_initial_qty': do_initial_qty,
                            'do_actual_qty': do_actual_qty,
                            'in_initial_qty': do_received_qty,
                            'in_actual_qty': do_actual_recv_qty,
                            'snk_pos_ref': do.snk_pos_ref,
                            'pos_status_site': dest_site.snk_pos_status if dest_site else False,
                            'source_document': do_source_doc,
                            'received_qty': do_received_qty,
                            'actual_received_qty': do_actual_recv_qty,
                            'unit_price': do_unit_price,
                        })

                # Remaining STO lines
                for line in pending_pr_lines:
                    src_site = line.source_site_id or (sto.source_site_ids[0] if sto.source_site_ids else False)
                    dest_site = line.destination_site_id or sto.destination_site_id
                    if not src_site or src_site.company_id.id not in company_ids:
                        continue
                    line_unit_price = self._get_unit_price_from_cache(
                        pricelist_cache_sto, line.product_id, sto.sto_date
                    )
                    vals_list.append({
                        'user_id': self.env.uid,
                        'date': sto.sto_date,
                        'transfer_id': sto.id,
                        'transfer_line_id': line.id,
                        'delivery_picking_id': False,
                        'receipt_picking_id': False,
                        'product_template_id': line.product_id.product_tmpl_id.id,
                        'product_id': line.product_id.id,
                        'analytic_id': line.product_id.analytic_id.id,
                        'snk_grade_id': line.product_id.snk_grade_id.id,
                        'category_id': line.product_id.categ_id.id,
                        'snk_material_code': line.product_id.snk_material_code,
                        'source_site_id': src_site.id if src_site else False,
                        'destination_site_id': dest_site.id if dest_site else False,
                        'pr_demand_qty': line.to_order,
                        'pr_actual_qty': 0.0,
                        'do_initial_qty': 0.0,
                        'do_actual_qty': 0.0,
                        'in_initial_qty': 0.0,
                        'in_actual_qty': 0.0,
                        'snk_pos_ref': False,
                        'pos_status_site': dest_site.snk_pos_status if dest_site else False,
                        'source_document': False,
                        'received_qty': 0.0,
                        'actual_received_qty': 0.0,
                        'unit_price': line_unit_price,
                    })
            t3_loop = time.time()
            _logger.info(">>> [PROFILING] STEP 3A Looping took %.2f seconds", t3_loop - t3_bulk)

        # ==========================================
        # 2B. Process Manual DO Flow
        # ==========================================
        domain_do = [
            ('snk_create_date', '>=', self.date_start),
            ('snk_create_date', '<=', self.date_end),
            ('state', '!=', 'cancel'), ### Hanya exclude cancelled DO
            ('snk_is_mutation', '=', True),
            ('transfer_id', '=', False),
            ('snk_site_address_id', '!=', False)
        ]
        if self.operation_ids:
            domain_do.append(('picking_type_id', 'in', self.operation_ids.ids))
        else:
            domain_do.append(('picking_type_id.code', '=', 'outgoing'))
        domain_do.append(('snk_site_id', 'in', source_site_ids))
        domain_do.append(('snk_site_address_id', 'in', destination_site_ids))

        t4 = time.time()
        manual_dos = self.env['stock.picking'].search(domain_do)
        _logger.info("Tuning: Found %s Manual Delivery Documents", len(manual_dos))
        _logger.info(">>> [STEP 3B] Found %s Manual Delivery Documents to process.", len(manual_dos))

        if manual_dos:
            do_moves_domain_m = [
                ('picking_id', 'in', manual_dos.ids),
                ('state', '!=', 'cancel'),
                ('company_id', 'in', company_ids),
            ]
            if valid_product_ids is not None:
                do_moves_domain_m.append(('product_id', 'in', valid_product_ids))
            all_do_moves_m = self.env['stock.move'].search(do_moves_domain_m)
            
            _logger.info(">>> [STEP 3B.1] Grouping %s Manual DO moves...", len(all_do_moves_m))
            do_moves_grouped_m = {}
            for m in all_do_moves_m:
                pid = m.picking_id.id
                prod_id = m.product_id.id
                if pid not in do_moves_grouped_m:
                    do_moves_grouped_m[pid] = {}
                if prod_id not in do_moves_grouped_m[pid]:
                    do_moves_grouped_m[pid][prod_id] = []
                do_moves_grouped_m[pid][prod_id].append(m)
                
            receipt_mapping_m = _get_receipt_mapping(all_do_moves_m.ids)
            
            # Build Pricelist Cache Manual DO
            manual_prod_ids = {m.product_id.id for m in all_do_moves_m}
            pricelist_cache_manual = self._build_pricelist_cache(list(manual_prod_ids))

            t4_bulk = time.time()
            _logger.info(">>> [PROFILING] STEP 3B Bulk Fetch took %.2f seconds", t4_bulk - t4)

            _logger.info(">>> [STEP 3B.2] Iterating over %s Manual DO documents...", len(manual_dos))
            for do in manual_dos:
                moves_by_product = do_moves_grouped_m.get(do.id, {})
                
                for product_id_key, moves in moves_by_product.items():
                    product = moves[0].product_id

                    do_initial_qty = sum(m.product_uom_qty for m in moves)
                    do_actual_qty = sum(self._get_qty_done(m) for m in moves)

                    receipt_move_ids = []
                    for m in moves:
                        if m.id in receipt_mapping_m:
                            receipt_move_ids.extend(receipt_mapping_m[m.id])
                    receipt_moves = self.env['stock.move'].browse(list(set(receipt_move_ids)))
                    
                    receipt_picking_id = False
                    in_initial_qty = 0.0
                    in_actual_qty = 0.0
                    if receipt_moves:
                        rp_id = receipt_moves[0].picking_id.id
                        rkey = (do.id, rp_id, product_id_key)
                        receipt_picking_id = rp_id
                        if rkey not in attributed_receipt_keys:
                            attributed_receipt_keys.add(rkey)
                            in_initial_qty = sum(rm.product_uom_qty for rm in receipt_moves)
                            in_actual_qty = sum(self._get_qty_done(rm) for rm in receipt_moves)

                    manual_source_doc = moves[0].origin if moves else False
                    
                    manual_received_qty = 0.0
                    manual_actual_recv_qty = 0.0
                    for rm in receipt_moves:
                        avail_demand = rm.product_uom_qty - receipt_demand_allocated.get(rm.id, 0.0)
                        alloc_demand = min(max(0.0, do_initial_qty - manual_received_qty), avail_demand)
                        if alloc_demand > 0:
                            manual_received_qty += alloc_demand
                            receipt_demand_allocated[rm.id] = receipt_demand_allocated.get(rm.id, 0.0) + alloc_demand
                        
                        if rm.state == 'done':
                            avail_done = self._get_qty_done(rm) - receipt_done_allocated.get(rm.id, 0.0)
                            alloc_done = min(max(0.0, do_actual_qty - manual_actual_recv_qty), avail_done)
                            if alloc_done > 0:
                                manual_actual_recv_qty += alloc_done
                                receipt_done_allocated[rm.id] = receipt_done_allocated.get(rm.id, 0.0) + alloc_done

                    manual_unit_price = self._get_unit_price_from_cache(pricelist_cache_manual, product, do.scheduled_date)

                    vals_list.append({
                        'user_id': self.env.uid,
                        'date': do.snk_create_date,
                        'transfer_id': False,
                        'transfer_line_id': False,
                        'delivery_picking_id': do.id,
                        'receipt_picking_id': receipt_picking_id,
                        'product_template_id': product.product_tmpl_id.id,
                        'product_id': product.id,
                        'analytic_id': product.analytic_id.id,
                        'snk_grade_id': product.snk_grade_id.id,
                        'category_id': product.categ_id.id,
                        'snk_material_code': product.snk_material_code,
                        'source_site_id': do.snk_site_id.id if do.snk_site_id else False,
                        'destination_site_id': do.snk_site_address_id.id if do.snk_site_address_id else False,
                        'pr_demand_qty': 0.0,
                        'pr_actual_qty': do_initial_qty,
                        'do_initial_qty': do_initial_qty,
                        'do_actual_qty': do_actual_qty,
                        'in_initial_qty': manual_received_qty,
                        'in_actual_qty': manual_actual_recv_qty,
                        'snk_pos_ref': do.snk_pos_ref,
                        'pos_status_site': do.snk_site_address_id.snk_pos_status if do.snk_site_address_id else False,
                        'source_document': manual_source_doc,
                        'received_qty': manual_received_qty,
                        'actual_received_qty': manual_actual_recv_qty,
                        'unit_price': manual_unit_price,
                    })
            t4_loop = time.time()
            _logger.info(">>> [PROFILING] STEP 3B Looping took %.2f seconds", t4_loop - t4_bulk)

        # 3. Create Bulk Records
        _logger.info(">>> [STEP 4] Finished processing. Bulk creating %d report records...", len(vals_list))
        if not vals_list:
            raise UserError(_("Tidak ada data distribusi STO/Manual Transit pada periode yang dipilih."))

        t5 = time.time()
        for i in range(0, len(vals_list), 1000):
            report_obj.create(vals_list[i:i + 1000])
        t6 = time.time()
        _logger.info(">>> [PROFILING] STEP 4 Bulk Create %d records took %.2f seconds", len(vals_list), t6 - t5)

        _logger.info("Tuning: Monitoring STO: Created %d report records. Total Elapsed: %.2f seconds", len(vals_list), t6 - t0)

        # 4. Soft Refresh Action
        return {
            'type': 'ir.actions.client',
            'tag': 'soft_reload',
        }
    # --- OLD METHOD BACKUP ---
    #     def action_generate(self):
    #         self.ensure_one()
    #         report_obj = self.env['report.monitoring.sto']
    #
    #         # 1. Clean previous session
    #         report_obj._clean_previous_reports()
    #
    #         # Build Valid Products Domain
    #         product_domain = []
    #         if self.analytic_ids:
    #             product_domain.append(('analytic_id', 'in', self.analytic_ids.ids))
    #         if self.category_ids:
    #             product_domain.append(('categ_id', 'in', self.category_ids.ids))
    #         if self.grade_ids:
    #             product_domain.append(('snk_grade_id', 'in', self.grade_ids.ids))
    #         if self.product_tmpl_ids:
    #             product_domain.append(('product_tmpl_id', 'in', self.product_tmpl_ids.ids))
    #         if self.product_ids:
    #             product_domain.append(('id', 'in', self.product_ids.ids))
    #
    #         valid_product_ids = None
    #         if product_domain:
    #             valid_product_ids = self.env['product.product'].search(product_domain).ids
    #             if not valid_product_ids:
    #                 raise UserError(_("Tidak ada produk yang sesuai dengan kriteria filter yang dipilih."))
    #
    #         vals_list = []
    #
    #         # FIX: track receipt picking yang sudah dihitung agar tidak double-count
    #         # Key: (receipt_picking_id, product_id) → mencegah 1 receipt dihitung N kali lintas DO
    #         attributed_receipt_keys = set()
    #
    #         # ==========================================
    #         # 2A. Process MRP/STO Driven Flow
    #         # ==========================================
    #         domain_sto = [
    #             ('sto_date', '>=', self.date_start),
    #             ('sto_date', '<=', self.date_end),
    #             ('state', 'not in', ['draft', 'cancel'])
    #         ]
    #         if self.source_site_ids:
    #             domain_sto.append(('source_site_ids', 'in', self.source_site_ids.ids))
    #         if self.destination_site_ids:
    #             domain_sto.append(('destination_site_id', 'in', self.destination_site_ids.ids))
    #
    #         stos = self.env['snk.stock.transfer.order'].search(domain_sto)
    #         _logger.info("Found %s STO Documents", len(stos))
    #
    #         for sto in stos:
    #             _logger.info("Processing STO %s (ID: %s) with %s lines", sto.name, sto.id, len(sto.line_ids))
    #
    #             # FIX: filter delivery pickings by brand di level picking (bukan hanya di move)
    #             delivery_pickings = self._get_delivery_pickings(sto)
    #             if delivery_pickings:
    #                 _logger.info("- Found Delivery Pickings: %s", delivery_pickings.mapped('name'))
    #
    #             pending_pr_lines = [
    #                 l for l in sto.line_ids
    #                 if (valid_product_ids is None or l.product_id.id in valid_product_ids)
    #             ]
    #
    #             # Process DOs iterating through grouped product variants
    #             for do in delivery_pickings:
    #                 product_moves = {}
    #                 for m in do.move_ids_without_package.filtered(lambda x: x.state != 'cancel'):
    #                     if valid_product_ids is not None and m.product_id.id not in valid_product_ids:
    #                         continue
    #                     if m.product_id.id not in product_moves:
    #                         product_moves[m.product_id.id] = self.env['stock.move'].browse()
    #                     product_moves[m.product_id.id] |= m
    #
    #                 for product_id_key, moves in product_moves.items():
    #                     # Find matching STO line for PR demand mapping
    #                     matched_line = next(
    #                         (l for l in pending_pr_lines if l.product_id.id == product_id_key), False
    #                     )
    #                     pr_demand_qty = 0.0
    #                     transfer_line_id = False
    #                     if matched_line:
    #                         pr_demand_qty = matched_line.to_order
    #                         transfer_line_id = matched_line.id
    #                         pending_pr_lines.remove(matched_line)
    #
    #                     product = moves[0].product_id
    #
    #                     # FIX: gunakan product_uom_qty (demand aktual move ini),
    #                     # bukan snk_original_demand yang bisa menyimpan demand sebelum split/backorder
    #                     do_initial_qty = sum(
    #                         m.product_uom_qty for m in moves if m.product_id.id == product_id_key
    #                     )
    #                     do_actual_qty = sum(
    #                         self._get_qty_done(m) for m in moves if m.product_id.id == product_id_key
    #                     )
    #
    #                     raw_receipt_moves = moves.mapped('move_dest_ids').filtered(
    #                         lambda rm: rm.picking_id.picking_type_id.code == 'incoming'
    #                                    and rm.state != 'cancel'
    #                     )
    #                     receipt_moves = raw_receipt_moves.filtered(
    #                         lambda rm: rm.product_id.id == product_id_key
    #                     )
    #
    #                     # FIX: hanya hitung receipt jika (receipt_picking, product) belum pernah dihitung
    #                     # Mencegah qty 3x ketika beberapa DO mengarah ke receipt picking yang sama
    #                     receipt_picking_id = False
    #                     in_initial_qty = 0.0
    #                     in_actual_qty = 0.0
    #                     if receipt_moves:
    #                         rp = receipt_moves.mapped('picking_id')[:1]
    #                         if rp:
    #                             rkey = (rp.id, product_id_key)
    #                             receipt_picking_id = rp.id
    #                             if rkey not in attributed_receipt_keys:
    #                                 attributed_receipt_keys.add(rkey)
    #                                 in_initial_qty = sum(rm.product_uom_qty for rm in receipt_moves)
    #                                 in_actual_qty = sum(self._get_qty_done(rm) for rm in receipt_moves)
    #
    #                     src_site = matched_line.source_site_id if matched_line else sto.source_site_ids[:1]
    #                     dest_site = matched_line.destination_site_id if matched_line else sto.destination_site_id
    #
    #                     vals_list.append({
    #                         'user_id': self.env.uid,
    #                         'date': sto.sto_date,
    #                         'transfer_id': sto.id,
    #                         'transfer_line_id': transfer_line_id,
    #                         'delivery_picking_id': do.id,
    #                         'receipt_picking_id': receipt_picking_id,
    #                         'product_template_id': product.product_tmpl_id.id,
    #                         'product_id': product.id,
    #                         'analytic_id': product.analytic_id.id,
    #                         'snk_grade_id': product.snk_grade_id.id,
    #                         'category_id': product.categ_id.id,
    #                         'snk_material_code': product.snk_material_code,
    #                         'source_site_id': src_site.id if src_site else False,
    #                         'destination_site_id': dest_site.id if dest_site else False,
    #                         'pr_demand_qty': pr_demand_qty,
    #                         'pr_actual_qty': do_initial_qty,
    #                         'do_initial_qty': do_initial_qty,
    #                         'do_actual_qty': do_actual_qty,
    #                         'in_initial_qty': in_initial_qty,
    #                         'in_actual_qty': in_actual_qty,
    #                         'snk_pos_ref': do.snk_pos_ref,
    #                         'pos_status_site': dest_site.snk_pos_status if dest_site else False,
    #                     })
    #
    #             # Append remaining STO lines that do not have any DO moves
    #             for line in pending_pr_lines:
    #                 src_site = line.source_site_id or sto.source_site_ids[:1]
    #                 dest_site = line.destination_site_id or sto.destination_site_id
    #                 vals_list.append({
    #                     'user_id': self.env.uid,
    #                     'date': sto.sto_date,
    #                     'transfer_id': sto.id,
    #                     'transfer_line_id': line.id,
    #                     'delivery_picking_id': False,
    #                     'receipt_picking_id': False,
    #                     'product_template_id': line.product_id.product_tmpl_id.id,
    #                     'product_id': line.product_id.id,
    #                     'analytic_id': line.product_id.analytic_id.id,
    #                     'snk_grade_id': line.product_id.snk_grade_id.id,
    #                     'category_id': line.product_id.categ_id.id,
    #                     'snk_material_code': line.product_id.snk_material_code,
    #                     'source_site_id': src_site.id if src_site else False,
    #                     'destination_site_id': dest_site.id if dest_site else False,
    #                     'pr_demand_qty': line.to_order,
    #                     'pr_actual_qty': 0.0,
    #                     'do_initial_qty': 0.0,
    #                     'do_actual_qty': 0.0,
    #                     'in_initial_qty': 0.0,
    #                     'in_actual_qty': 0.0,
    #                     'snk_pos_ref': False,
    #                     'pos_status_site': dest_site.snk_pos_status if dest_site else False,
    #                 })
    #
    #         # ==========================================
    #         # 2B. Process Manual DO Flow (Non-MRP Transit)
    #         # ==========================================
    #         domain_do = [
    #             ('snk_create_date', '>=', self.date_start),
    #             ('snk_create_date', '<=', self.date_end),
    #             ('state', 'in', ['assigned', 'done']),
    #             ('snk_is_mutation', '=', True),
    #             ('transfer_id', '=', False),
    #             ('snk_site_address_id', '!=', False),
    #         ]
    #         if self.operation_ids:
    #             domain_do.append(('picking_type_id', 'in', self.operation_ids.ids))
    #         else:
    #             domain_do.append(('picking_type_id.code', '=', 'outgoing'))
    #
    #         if self.source_site_ids:
    #             domain_do.append(('snk_site_id', 'in', self.source_site_ids.ids))
    #         if self.destination_site_ids:
    #             domain_do.append(('snk_site_address_id', 'in', self.destination_site_ids.ids))
    #
    #         # FIX: filter by brand di level DO domain agar hanya DO brand yang dipilih masuk
    #         if self.analytic_ids:
    #             domain_do.append(('analytic_ids', 'in', self.analytic_ids.ids))
    #
    #         manual_dos = self.env['stock.picking'].search(domain_do)
    #         _logger.info("Found %s Manual Delivery Documents", len(manual_dos))
    #
    #         for do in manual_dos:
    #             product_moves = {}
    #             for m in do.move_ids_without_package.filtered(lambda x: x.state != 'cancel'):
    #                 if valid_product_ids is not None and m.product_id.id not in valid_product_ids:
    #                     continue
    #                 if m.product_id.id not in product_moves:
    #                     product_moves[m.product_id.id] = self.env['stock.move'].browse()
    #                 product_moves[m.product_id.id] |= m
    #
    #             for product_id_key, moves in product_moves.items():
    #                 product = moves[0].product_id
    #
    #                 # FIX: pakai product_uom_qty bukan snk_original_demand
    #                 do_initial_qty = sum(
    #                     m.product_uom_qty for m in moves if m.product_id.id == product_id_key
    #                 )
    #                 do_actual_qty = sum(
    #                     self._get_qty_done(m) for m in moves if m.product_id.id == product_id_key
    #                 )
    #
    #                 raw_receipt_moves = moves.mapped('move_dest_ids').filtered(
    #                     lambda rm: rm.picking_id.picking_type_id.code == 'incoming'
    #                                and rm.state != 'cancel'
    #                 )
    #                 receipt_moves = raw_receipt_moves.filtered(
    #                     lambda rm: rm.product_id.id == product_id_key
    #                 )
    #
    #                 # FIX: deduplikasi receipt — hanya hitung jika belum pernah dihitung
    #                 receipt_picking_id = False
    #                 in_initial_qty = 0.0
    #                 in_actual_qty = 0.0
    #                 if receipt_moves:
    #                     rp = receipt_moves.mapped('picking_id')[:1]
    #                     if rp:
    #                         rkey = (rp.id, product_id_key)
    #                         receipt_picking_id = rp.id
    #                         if rkey not in attributed_receipt_keys:
    #                             attributed_receipt_keys.add(rkey)
    #                             in_initial_qty = sum(rm.product_uom_qty for rm in receipt_moves)
    #                             in_actual_qty = sum(self._get_qty_done(rm) for rm in receipt_moves)
    #
    #                 vals_list.append({
    #                     'user_id': self.env.uid,
    #                     'date': do.snk_create_date,
    #                     'transfer_id': False,
    #                     'transfer_line_id': False,
    #                     'delivery_picking_id': do.id,
    #                     'receipt_picking_id': receipt_picking_id,
    #                     'product_template_id': product.product_tmpl_id.id,
    #                     'product_id': product.id,
    #                     'analytic_id': product.analytic_id.id,
    #                     'snk_grade_id': product.snk_grade_id.id,
    #                     'category_id': product.categ_id.id,
    #                     'snk_material_code': product.snk_material_code,
    #                     'source_site_id': do.snk_site_id.id if do.snk_site_id else False,
    #                     'destination_site_id': do.snk_site_address_id.id if do.snk_site_address_id else False,
    #                     'pr_demand_qty': 0.0,
    #                     'pr_actual_qty': do_initial_qty,
    #                     'do_initial_qty': do_initial_qty,
    #                     'do_actual_qty': do_actual_qty,
    #                     'in_initial_qty': in_initial_qty,
    #                     'in_actual_qty': in_actual_qty,
    #                     'snk_pos_ref': do.snk_pos_ref,
    #                     'pos_status_site': do.snk_site_address_id.snk_pos_status if do.snk_site_address_id else False,
    #                 })
    #
    #         # 3. Create Bulk Records
    #         if not vals_list:
    #             raise UserError(_("Tidak ada data distribusi STO/Manual Transit pada periode yang dipilih."))
    #
    #         for i in range(0, len(vals_list), 1000):
    #             report_obj.create(vals_list[i:i + 1000])
    #
    #         _logger.info("Monitoring STO: Created %d report records", len(vals_list))
    #
    #         # 4. Return window action dengan domain per-user
    #         return {
    #             'type': 'ir.actions.act_window',
    #             'name': _('Monitoring STO'),
    #             'res_model': 'report.monitoring.sto',
    #             'view_mode': 'list,pivot',
    #             'target': 'current',
    #             'domain': [('user_id', '=', self.env.uid)],
    #         }
