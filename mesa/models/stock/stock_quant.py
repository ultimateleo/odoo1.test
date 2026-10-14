import logging
import time
import threading
from odoo import api, fields, models, _


_logger = logging.getLogger(__name__)

class StockQuantPackage(models.Model):
    _inherit = "stock.quant.package"

    name = fields.Char(
        'Package Reference', copy=False, index='trigram', default=False, required=False)
    sequence_id = fields.Many2one("ir.sequence", string="Sequence Numbering", related="package_type_id.sequence_id")

    @api.model_create_multi
    def create(self, vals_list):
        package_type_id = self._context.get('default_package_type_id')

        for vals in vals_list:
            if not vals.get('package_type_id') and package_type_id:
                vals['package_type_id'] = package_type_id
            if not vals.get('name'):
                if vals.get('package_type_id'):
                    package_type = self.env['stock.package.type'].browse(vals['package_type_id'])
                    sequence = package_type.sequence_id
                    barcode = package_type.barcode or ''
                    if sequence:
                        seq_number = sequence.next_by_id()
                        vals['name'] = f"{barcode}/{seq_number}" if barcode else seq_number
                    else:
                        vals['name'] = self.env['ir.sequence'].next_by_code('stock.quant.package')
                else:
                    vals['name'] = self.env['ir.sequence'].next_by_code('stock.quant.package')

        return super().create(vals_list)
    
class StockQuant(models.Model):
    _inherit = 'stock.quant'

    # @api.model
    # def cron_apply_inventory_quantity(self, batch_size=300):
    #     """
    #     Periodic scheduler to apply inventory adjustment
    #     based on inventory_quantity (absolute),
    #     for quants already marked with inventory_quantity_set = True.
    #     """
    #     # Fetch quants to process
    #     domain = [
    #         ('inventory_quantity_set', '=', True),
    #         ('location_id.usage', 'in', ('internal', 'transit')),
    #     ]
    #     _logger.info("domain for inventory adjustment quants: %s", domain)

    #     # === FETCH BATCH ===
    #     quant_ids = self.search(domain, limit=batch_size, order='location_id asc, id asc')

    #     if not quant_ids:
    #         _logger.debug("No inventory quant to apply")
    #         return

    #     _logger.info(
    #         "Applying inventory adjustment for %s quants",
    #         len(quant_ids)
    #     )

    #     # === APPLY (STANDARD ODOO ENGINE) ===
    #     start_time = time.perf_counter()

    #     try:
    #         if quant_ids:
    #             quant_ids._compute_inventory_diff_quantity()
    #             # Apply inventory
    #             quant_ids.with_context(
    #                 inventory_mode=True,          # MUST
    #                 no_at_date=True,               # MUST
    #                 allowed_company_ids=self.env.company.ids,
    #             )._apply_inventory()
    #             # ).action_apply_inventory() ### Kena outdated
    #             # ).action_keep_counted_quantity() ### Tidak nyambung ke wizard

    #             duration = time.perf_counter() - start_time
    #             _logger.info(
    #                 "Inventory adjustment cron completed for %s quants in %.3f seconds.",
    #                 len(quant_ids),
    #                 duration
    #             )
    #         else:
    #             duration = time.perf_counter() - start_time
    #             _logger.warning(
    #                 "No inventory adjustment applied (0 quants). Took %.3f seconds.",
    #                 duration
    #             )

    #     except Exception:
    #         duration = time.perf_counter() - start_time
    #         _logger.exception(
    #             "Inventory adjustment cron failed after %.3f seconds.",
    #             duration
    #         )
    #         # let cron retry next run
    #         raise
    #     self.env.cr.commit()

    @api.model
    def cron_apply_inventory_quantity(self, batch_size=300, workers=2, chunk_size=10, max_runtime=300, slow_time=80, lock_key=987654321):
        """
        PRODUCTION VERSION (1-Worker-1-Location Strategy)
        """
        start_time = time.perf_counter()

        # =========================================================
        # 1. ANTI OVERLAP LOCK (Manager)
        # =========================================================
        self.env.cr.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        locked = self.env.cr.fetchone()[0]

        if not locked:
            _logger.warning("Inventory cron skipped: already running")
            return

        try:
            # =====================================================
            # 2. FIND ACTIVE LOCATIONS
            # =====================================================
            # Mencari lokasi mana saja yang butuh di-apply
            self.env.cr.execute("""
                SELECT location_id 
                FROM stock_quant 
                WHERE inventory_quantity_set = True 
                  AND location_id IS NOT NULL 
                GROUP BY location_id 
                ORDER BY MIN(write_date) ASC
                LIMIT %s
            """, (workers,))
            active_location_ids = [r[0] for r in self.env.cr.fetchall()]

            if not active_location_ids:
                _logger.info("No location/quant to process")
                return

            total_locs = len(active_location_ids)
            
            _logger.info(
                "Inventory Manager: Found %s locations. Starting with %s workers | batch_size=%s",
                total_locs, total_locs, batch_size
            )

            # =====================================================
            # 3. QUOTA DISTRIBUTION
            # =====================================================
            limit_per_worker = batch_size // total_locs
            remainder = batch_size % total_locs

            threads = []
            for i, loc_id in enumerate(active_location_ids):
                # Worker 1 mengambil sisa porsi jika tidak genap
                this_limit = limit_per_worker + (remainder if i == 0 else 0)
                
                t = threading.Thread(
                    target=self._run_inventory_worker,
                    args=(loc_id, i + 1, this_limit, chunk_size, slow_time, max_runtime, start_time)
                )
                threads.append(t)
                t.start()

            # Tunggu worker selesai
            for t in threads:
                t.join()

            # =====================================================
            # 4. FINAL LOG
            # =====================================================
            total_time = time.perf_counter() - start_time
            # Re-check remaining total
            remaining = self.search_count([
                ('inventory_quantity_set', '=', True),
                ('location_id.usage', 'in', ('internal', 'transit'))
            ])

            _logger.info(
                "Inventory cron DONE | total_locations=%s | remaining=%s | time=%.2fs",
                total_locs, remaining, total_time
            )

        finally:
            self.env.cr.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))

    def _run_inventory_worker(self, location_id, worker_id, limit, chunk_size, slow_time, max_runtime, start_time):
        """Worker Thread: 1 Worker = 1 Location"""
        new_cr = self.pool.cursor()
        try:
            new_env = api.Environment(new_cr, self.env.uid, self.env.context)
            location_name = new_env['stock.location'].browse(location_id).display_name
            _logger.info("Worker %s: Started for Location [%s] | limit=%s", worker_id, location_name, limit)
            
            self_worker = self.with_env(new_env).sudo()
            
            # 1. Fetch data khusus lokasi ini
            domain = [
                ('inventory_quantity_set', '=', True),
                ('location_id', '=', location_id)
            ]
            quant_batch = self_worker.search(domain, limit=limit, order='product_id, write_date asc')
            
            if not quant_batch:
                _logger.info("Worker %s: No data for Location [%s]", worker_id, location_name)
                return

            # 2. Pre-compute (Bulk per Worker)
            quant_batch._compute_inventory_diff_quantity()
            new_cr.commit()

            processed = 0
            total_to_process = len(quant_batch)

            # 3. Process Chunks
            for i in range(0, total_to_process, chunk_size):
                
                # Check Global Runtime
                if time.perf_counter() - start_time > max_runtime:
                    _logger.warning("Worker %s: Stop early due to runtime | processed=%s/%s", 
                                    worker_id, processed, total_to_process)
                    break

                chunk = quant_batch[i:i + chunk_size]

                # Retry Logic per Chunk
                for attempt in range(2):
                    try:
                        chunk_start = time.perf_counter()
                        with new_cr.savepoint():
                            # Row-level lock on journals to prevent account.move naming collision
                            # Standard Stock Journal is in product category (property_stock_journal)
                            # Sort IDs to prevent potential deadlocks between workers
                            journal_ids = chunk.mapped('product_id.categ_id.property_stock_journal.id')
                            valid_journal_ids = sorted(list(set(filter(None, journal_ids))))
                            
                            if valid_journal_ids:
                                # Lock the journal record(s) until commit
                                # This ensures only one worker assigns names for this journal at a time
                                new_cr.execute("SELECT id FROM account_journal WHERE id IN %s FOR UPDATE", [tuple(valid_journal_ids)])

                            chunk.with_context(
                                inventory_mode=True,
                                no_at_date=True,
                                skip_fifo_vacuum=True,
                                mail_notrack=True,         # Matikan Chatter (User opted for this in last diff)
                                mail_create_nolog=True,    # Matikan log pesan
                                tracking_disable=True,     # Matikan tracking field
                                allowed_company_ids=new_env.company.ids,
                            )._apply_inventory()

                        # commit tiap chunk
                        new_cr.commit()
                        processed += len(chunk)

                        duration = time.perf_counter() - chunk_start
                        _logger.info("Worker %s: Completed chunk %s-%s (%s rec) in %.2fs", 
                                     worker_id, i, i + len(chunk), len(chunk), duration)

                        if duration > slow_time:
                            _logger.warning("Worker %s: Chunk getting slow, stopping early", worker_id)
                            break
                        break
                    except Exception as e:
                        new_cr.rollback()
                        _logger.warning("Worker %s: Retry %s chunk %s-%s | error=%s", 
                                        worker_id, attempt + 1, i, i + len(chunk), str(e))
                        if attempt == 1:
                            _logger.error("Worker %s: FAILED chunk %s-%s permanently", worker_id, i, i + len(chunk))
            
            _logger.info("Worker %s: Finished processing %s quants for [%s]", worker_id, processed, location_name)

        finally:
            new_cr.close()