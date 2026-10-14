# -*- coding: utf-8 -*-
import os
import json
import shutil
import logging
import posixpath
import paramiko
from copy import deepcopy
from lxml import etree
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime as _dt, time as _time_of_day
import time as _time

from psycopg2.errors import UniqueViolation
from markupsafe import Markup

from odoo import models, api, fields, _
from odoo.exceptions import UserError, ValidationError
from odoo.tools.safe_eval import safe_eval

MODEL_TYPE = [
    ('none', ''),
    ('TI', 'Transfer In'),
    ('TO', 'Transfer Out'),
    ('SALES', 'Pos Sales'),
    ('ORDER', 'Pos Order'),
]

_logger = logging.getLogger(__name__)

# --- Redam log Paramiko agar tidak flooding ---
for noisy in ("paramiko.transport", "paramiko.sftp_client", "paramiko.channel"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


# _sql_constraints = [
#     ('unique_name_ref_data',
#      'unique(name, ref_data)',
#      'Duplicate inbound name + ref_data not allowed!')
# ]

class SnkPiInbound(models.Model):
    _name = "snk.pi.inbound"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "POS Integration Inbound"
    _order = 'id desc'

    active = fields.Boolean(default=True, tracking=60)
    name = fields.Char("Ref", tracking=10)
    data = fields.Text(string="Data XML")
    ref_data = fields.Selection(MODEL_TYPE, "Ref Data", tracking=20)
    ref_model = fields.Char("Ref Model", copy=False, tracking=30)
    ref_id = fields.Integer("Ref ID", copy=False)
    output = fields.Char("Output", copy=False, tracking=50)


    state = fields.Selection([
        ('pending', 'Pending'),
        ('duplicate', 'Duplicated'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('cancel', 'Canceled'),
    ], string='Status Integration', default='pending', tracking=40)

    log_info = fields.Html(string="Log Info", readonly=True, copy=False)


    @api.model
    def _get_view(self, view_id=None, view_type='list', **options):
        arch, view = super()._get_view(view_id, view_type, **options)
        if view_type == "list":
            for node in arch.xpath("//field"):
                if node.get('readonly'):
                    readonly = node.get('readonly')
                    readonly += "True"
                else:
                    node.set("readonly", "True")

        return arch, view

    def FindOrCreate(self, vals_list):
        """
        Hybrid batch-safe create inbound records.
        - Pre-check existing untuk batch cepat.
        - Fallback ke unique constraint untuk thread safety.
        """

        # Queue records can be created in large batches. Skip the generic
        # "record created" chatter/follower side effects; subsequent tracked
        # lifecycle writes remain audited normally.
        Inbound = self.sudo().with_context(
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
        )

        # --- Step 1: Normalize input ---
        if isinstance(vals_list, str):
            try:
                vals_list = json.loads(vals_list)
            except json.JSONDecodeError as e:
                raise ValueError(f"Invalid JSON input: {e}")
        if isinstance(vals_list, dict):
            vals_list = [vals_list]

        normalized = []
        for v in vals_list:
            if isinstance(v, str):
                try:
                    normalized.append(json.loads(v))
                except json.JSONDecodeError:
                    raise ValueError(f"Invalid JSON string in list: {v}")
            elif isinstance(v, dict):
                normalized.append(v)
            else:
                raise ValueError(f"Unsupported item type: {type(v)}")
        vals_list = normalized
        if not vals_list:
            return []

        # --- Step 2: Ambil semua nama yang mungkin dipakai ---
        all_names = [v["name"] for v in vals_list]
        all_ref_data = {v["ref_data"] for v in vals_list}

        existing = Inbound.search([
            ("name", "in", all_names + [n + "-copy" for n in all_names]),
            ("ref_data", "in", list(all_ref_data))
        ])
        existing_map = {(rec.name, rec.ref_data) for rec in existing}

        # --- Step 3: Build list untuk create ---
        to_create = []
        for vals in vals_list:
            name, ref_data = vals["name"], vals["ref_data"]

            # Cek apakah name sudah ada (langsung atau versi -copy)
            if (name, ref_data) in existing_map or (name + "-copy", ref_data) in existing_map:
                vals.update({
                    "name": name + "-copy",
                    "state": "duplicate",
                })
            else:
                vals.update({
                    "state": "pending",
                })
            to_create.append(vals)

        # --- Step 4: Bulk create (fallback ke unique constraint kalau race condition) ---
        created = self.env[self._name]
        if to_create:
            try:
                created = Inbound.create(to_create)
            except Exception as e:
                if isinstance(getattr(e, "pgcode", None), str) and e.pgcode == UniqueViolation.sqlstate:
                    _logger.warning(
                        "UniqueViolation, retrying with -copy fallback")
                    retried = []
                    for vals in to_create:
                        try:
                            rec = Inbound.create(vals)
                            retried.append(rec.id)
                        except Exception:
                            vals["name"] = vals["name"] + "-copy"
                            vals["state"] = "duplicate"
                            rec = Inbound.create(vals)
                            retried.append(rec.id)
                    created = Inbound.browse(retried)
                else:
                    raise
        return created

    # -----------------------------
    # 2.1 Entry point untuk cron / pipeline
    # -----------------------------
    # @api.model
    def action_process(self, process_type=None, limit_data=None, batch_size=500):
        """
        Pipeline utama untuk process_type tertentu: pos_sales / pos_order / TI / TO
        Uses FOR UPDATE SKIP LOCKED so multiple workers can run without contention.
        """
        if not process_type:
            _logger.warning(
                "action_process dipanggil tanpa process_type, skip")
            return

        mapping = {
            "transfer_in": "TI",
            "transfer_out": "TO",
            "pos_sales": "SALES",
            "pos_order": "ORDER",
        }
        ref_data = mapping.get(process_type)
        if not ref_data:
            _logger.warning("Unknown process_type=%s", process_type)
            return

        # Acquire a set of pending ids using SKIP LOCKED to allow safe parallel workers
        domain_sql = "ref_data = %s AND state = 'pending'"
        params = [ref_data]
        limit_clause = f"LIMIT {int(limit_data)}" if limit_data else ""

        # We select ids FOR UPDATE SKIP LOCKED to let other workers skip locked rows
        query = f"""
            SELECT id FROM snk_pi_inbound
            WHERE {domain_sql}
            ORDER BY id
            {limit_clause}
            FOR UPDATE SKIP LOCKED
        """

        self.env.cr.execute(query, params)
        rows = self.env.cr.fetchall()
        inbound_ids = [r[0] for r in rows] if rows else []
        if not inbound_ids:
            _logger.info(
                "No pending records for process_type=%s (or they are locked by other workers)", process_type)
            return

        inbound_records = self.browse(inbound_ids)
        _logger.info(
            "Processing %s records type=%s batch_size=%s (ids: %s)",
            len(inbound_records),
            process_type,
            batch_size,
            inbound_ids,
        )

        if process_type == "pos_order":
            inbound_records.process_order(batch_size=batch_size)
        elif process_type == "pos_sales":
            inbound_records.process_sales(batch_size=batch_size)
        elif ref_data == "TI":
            for rec in inbound_records:
                rec.process_TI()
        elif ref_data == "TO":
            inbound_records.process_TO(batch_size=batch_size)

    # -----------------------------
    # 2.2 Synchronize SFTP
    # -----------------------------
    def synchronize_sftp(self, download_workers=5):
        """
        Download files dari SFTP → parse → insert inbound SALES
        """
        local_dt = fields.Datetime.context_timestamp(
            self, _dt.now()).replace(microsecond=0, tzinfo=None)

        Config = self.env['ir.config_parameter'].sudo()
        sftp_host = Config.get_param('mesa.snk_sftp_host')
        sftp_username = Config.get_param('mesa.snk_sftp_username')
        sftp_password = Config.get_param('mesa.snk_sftp_password')
        sftp_port = int(Config.get_param('mesa.snk_sftp_port'))
        sftp_path_source = Config.get_param('mesa.snk_sftp_path_local_inbound')
        sftp_path_inbound = Config.get_param('mesa.snk_sftp_path_inbound')
        sftp_path_archive = Config.get_param(
            'mesa.snk_sftp_path_inbound_archive')
        # sftp_path_archive = posixpath.join(sftp_path_inbound, "archive")

        # --- helper untuk cek file exist di SFTP ---
        def _sftp_exists(sftp_client, path):
            try:
                sftp_client.stat(path)
                return True
            except IOError:
                return False

        # --- helper untuk move file ke archive dengan nama unik ---
        def _move_to_archive_unique(sftp_client, remote_path, archive_dir, local_dt=None):
            fname = posixpath.basename(remote_path)
            base, ext = os.path.splitext(fname)
            target = posixpath.join(archive_dir, fname)

            if not _sftp_exists(sftp_client, target):
                sftp_client.rename(remote_path, target)
                return target

            # kalau sudah ada → tambah -copyN
            i = 2
            while True:
                candidate_name = f"{base}-copy{i}{ext}"
                candidate_path = posixpath.join(archive_dir, candidate_name)
                if not _sftp_exists(sftp_client, candidate_path):
                    sftp_client.rename(remote_path, candidate_path)
                    _logger.info("**Executed at %s:** [INFO] Archived %s → %s",
                                 local_dt, fname, candidate_name)
                    return candidate_path
                i += 1

        _logger.warning(">>>> sftp path inbound: %s", sftp_path_inbound)
        transport = paramiko.Transport((sftp_host, sftp_port))
        transport.connect(username=sftp_username, password=sftp_password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        files = [f.filename for f in sftp.listdir_attr(
            sftp_path_inbound) if '.' in f.filename]
        
        if files:
            _logger.info(
                "**Executed at %s:** [INFO] SFTP listdir returned %d files: %s. user: %s, pass: %s",
                local_dt, len(files), files, sftp_username, sftp_password
            )
        else:
            _logger.info(
                "**Executed at %s:** [INFO] SFTP listdir returned 0 files, user: %s, pass: %s",
                local_dt, sftp_username, sftp_password
            )

        sftp.close()
        transport.close()

        # --- bagi file ke worker ---
        file_chunks = [[] for _ in range(download_workers)]
        for i, fname in enumerate(files):
            file_chunks[i % download_workers].append(fname)

        def worker(file_list):
            t = paramiko.Transport((sftp_host, sftp_port))
            t.connect(username=sftp_username, password=sftp_password)
            s = paramiko.SFTPClient.from_transport(t)
            parsed = []
            try:
                for fname in file_list:
                    local_path = os.path.join(sftp_path_source, fname)
                    remote_path = posixpath.join(sftp_path_inbound, fname)
                    archive_path = posixpath.join(sftp_path_archive, fname)

                    # download file
                    s.get(remote_path, local_path)

                    with open(local_path, 'r') as f:
                        data = f.read()

                    name, ext = os.path.splitext(fname)
                    file_type = ext.lstrip('.').upper()
                    _logger.info("fname: %s, ext: %s, file_type: %s", fname, ext, file_type)
                    parsed.append(
                        {'name': name, 'ref_data': file_type, 'data': data})
                    # _logger.info(">>> Parsed file %s", parsed['ref_data'])

                    os.remove(local_path)

                    try:
                        _move_to_archive_unique(
                            s, remote_path, sftp_path_archive, local_dt=local_dt)
                        _logger.info(
                            "**Executed at %s:** [INFO] Moved %s → archive", local_dt, fname)
                    except Exception as e:
                        _logger.error(
                            "**Executed at %s:** [ERROR] Failed to archive %s: %s", local_dt, fname, e)
            finally:
                s.close()
                t.close()
            return parsed

        # --- jalankan parallel download ---
        all_parsed = []
        with ThreadPoolExecutor(max_workers=download_workers) as executor:
            futures = [executor.submit(worker, chunk) for chunk in file_chunks]
            for future in as_completed(futures):
                all_parsed.extend(future.result())

        # _logger.info(">>>> File name: %s, file ext: %s", all_parsed('fname'), all_parsed('ref_data'))
        # --- insert ke inbound ---
        if all_parsed:
            created = self.FindOrCreate(
                [{"name": f["name"], "ref_data": f["ref_data"], "data": f["data"]}
                 for f in all_parsed]
            )
        else:
            created = self.browse()

        _logger.info(
            "**Executed at %s:** [INFO] SFTP synchronize completed, %d files processed (%d new, %d duplicate)",
            local_dt,
            len(all_parsed),
            sum(1 for r in created if r.state == "pending"),
            sum(1 for r in created if r.state == "duplicate"),
        )

        return created

    # -----------------------------
    # 2.3 Process SALES → inbound ORDER
    # -----------------------------
    # def process_sales(self, batch_size=500):
    #     all_order_records = []
    #     done_ids = []         # inbound IDs sukses dibuat ORDER
    #     failed_updates = []   # list of tuples (log_msg, rec_id)
    #     failed_count = 0

    #     # Step 1: parse all inbound, check duplicates, build records
    #     for rec in self:
    #         local_dt = fields.Datetime.context_timestamp(rec, _dt.now()).replace(microsecond=0, tzinfo=None)
    #         try:
    #             root = etree.fromstring(rec.data)

    #             # base info untuk header
    #             base_str = "".join([
    #                 etree.tostring(root.find(tag)).decode()
    #                 for tag in ['Store', 'Location', 'Company', 'SalesDate']
    #                 if root.find(tag) is not None
    #             ])

    #             lines = root.findall('Lines/*')
    #             # cek duplicate berdasarkan name
    #             row_names = [f"{rec.name}-Row{i+1}" for i in range(len(lines))]
    #             existing_names = {
    #                 x['name'] for x in self.search([('name', 'in', row_names)]).read(['name'])
    #             }

    #             for idx, row in enumerate(lines, start=1):
    #                 fname = f"{rec.name}-Row{idx}"
    #                 if fname in existing_names:
    #                     # duplicate → gagal
    #                     failed_updates.append((
    #                         rec.id,
    #                         f"<b>Executed at {local_dt}:</b><br>Duplicate record detected, ORDER not created"
    #                     ))
    #                     failed_count += 1
    #                 else:
    #                     # unique row → build ORDER record
    #                     row_str = "".join([etree.tostring(c).decode() for c in row])
    #                     all_order_records.append({
    #                         "name": fname,
    #                         "ref_data": "ORDER",
    #                         "data": f"<Order>{base_str}{row_str}</Order>",
    #                     })

    #         except Exception as e:
    #             failed_updates.append((
    #                 rec.id,
    #                 f"<b>Executed at {local_dt}:</b><br>Failed to parse XML: {str(e)}"
    #             ))
    #             failed_count += 1

    #     # Step 2: batch create ORDER records with per-row fallback
    #     for i in range(0, len(all_order_records), batch_size):
    #         chunk = all_order_records[i:i + batch_size]
    #         try:
    #             orders = self.create(chunk)
    #             self.env.cr.commit()
    #         except Exception as e:
    #             _logger.exception("Batch create failed, falling back per-row: %s", e)
    #             try:
    #                 self.env.cr.rollback()
    #             except Exception:
    #                 pass

    #             for vals in chunk:
    #                 try:
    #                     self.create(vals)
    #                     self.env.cr.commit()
    #                 except Exception as ex2:
    #                     try:
    #                         self.env.cr.rollback()
    #                     except Exception:
    #                         pass
    #                     local_dt = fields.Datetime.context_timestamp(self, _dt.now()).replace(microsecond=0, tzinfo=None)
    #                     failed_updates.append((
    #                         f"<b>Executed at {local_dt}:</b><br>Row create failed: {str(ex2)}"
    #                     ))
    #                     failed_count += 1

    #     # Step 3: batch SQL update status sukses
    #     if done_ids:
    #         local_dt = fields.Datetime.context_timestamp(self, _dt.now()).replace(microsecond=0, tzinfo=None)
    #         log_info = f"<b>Executed at {local_dt}:</b><br>Order record created successfully."
    #         self.env.cr.executemany(
    #             "UPDATE snk_pi_inbound SET state='done', log_info=%s WHERE id=%s",
    #             [(log_info, rec_id) for rec_id in done_ids if rec_id]
    #         )

    #     # Step 4: batch SQL update untuk gagal (duplicate, parse error, per-row create gagal)
    #     if failed_updates:
    #         self.env.cr.executemany(
    #             "UPDATE snk_pi_inbound SET state='failed', log_info=%s WHERE id=%s",
    #             [(msg, rec_id) for rec_id, msg in failed_updates if rec_id]
    #         )

    #     # Step 5: final log summary
    #     _logger.info(
    #         "Executed at %s: Process sales completed: %d Order records created, failed=%d",
    #         fields.Datetime.context_timestamp(self, _dt.now()).replace(microsecond=0, tzinfo=None),
    #         len(done_ids),
    #         failed_count
    #     )

    def _try_lock_inbound_business_keys(self, keys):
        """Return business keys locked by this transaction without waiting.

        Advisory locks are justified here because separate inbound rows can carry
        the same business reference and are intentionally selected concurrently
        with ``SKIP LOCKED``. Values remain query parameters; payload text is
        never interpolated into SQL.
        """
        unique_keys = sorted(set(keys))
        if not unique_keys:
            return set()

        # One round-trip for the full batch. A contested key returns False
        # immediately, allowing unrelated records to continue in parallel.
        self.env.cr.execute("""
            SELECT requested.lock_key,
                   pg_try_advisory_xact_lock(
                       hashtextextended(requested.lock_key, 0)
                   ) AS acquired
            FROM unnest(%s::text[]) AS requested(lock_key)
            ORDER BY requested.lock_key
        """, [unique_keys])
        return {
            key for key, acquired in self.env.cr.fetchall() if acquired
        }

    def process_sales(self, batch_size=500):
        """Atomically expand SALES headers into pending ORDER queue rows."""
        inbound_model = self.env['snk.pi.inbound'].with_context(
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
        )
        sales_records = self.filtered(
            lambda rec: rec.state == 'pending' and rec.ref_data == 'SALES'
        )
        successful_results = []  # tuples (rec_id, created_count)
        failed_updates = []      # tuples (rec_id, msg)
        duplicate_updates = []   # tuples (rec_id, msg)
        processed_names = set()
        deferred_count = 0

        sales_lock_keys = {
            rec.id: f"snk.pi.inbound:SALES:{rec.name or ''}"
            for rec in sales_records
        }
        acquired_sales_keys = self._try_lock_inbound_business_keys(
            sales_lock_keys.values()
        )

        for rec in sales_records:
            local_dt = fields.Datetime.context_timestamp(
                rec, fields.Datetime.now()
            ).replace(microsecond=0, tzinfo=None)

            if sales_lock_keys[rec.id] not in acquired_sales_keys:
                rec.write({
                    'log_info': (
                        f"<b>Executed at {local_dt}:</b><br>SALES processing "
                        "deferred because another worker is processing the "
                        "same reference. State remains pending."
                    ),
                })
                deferred_count += 1
                continue

            if rec.name in processed_names:
                duplicate_updates.append((
                    rec.id,
                    f"<b>Executed at {local_dt}:</b><br>"
                    "Duplicate SALES in the same run; the first successful "
                    "header was retained.",
                ))
                continue

            try:
                duplicate_msg = None
                success_result = None
                with self.env.cr.savepoint():
                    # Recheck only after the advisory lock is held. A concurrent
                    # worker that committed first is now visible.
                    existing_done = self.search([
                        ('name', '=', rec.name),
                        ('ref_data', '=', 'SALES'),
                        ('state', '=', 'done'),
                        ('id', '!=', rec.id),
                    ], limit=1)
                    if existing_done:
                        duplicate_msg = (
                            f"<b>Executed at {local_dt}:</b><br>Duplicate SALES; "
                            f"header {existing_done.display_name} is already done."
                        )
                    else:
                        if not rec.name:
                            raise ValidationError(_(
                                "SALES reference is empty for inbound %(id)s.",
                                id=rec.id,
                            ))
                        root = etree.fromstring(rec.data)
                        base_str = "".join(
                            etree.tostring(root.find(tag)).decode()
                            for tag in (
                                'Store', 'Location', 'Company', 'SalesDate'
                            )
                            if root.find(tag) is not None
                        )
                        lines = root.findall('Lines/*') or []
                        if not lines:
                            raise ValidationError(_(
                                "No lines found in SALES %(reference)s.",
                                reference=rec.name or rec.id,
                            ))

                        order_values = []
                        for index, row in enumerate(lines, start=1):
                            row_str = "".join(
                                etree.tostring(child).decode()
                                for child in row
                            )
                            order_values.append({
                                'name': f"{rec.name}-Row{index}",
                                'ref_data': 'ORDER',
                                'data': f"<Order>{base_str}{row_str}</Order>",
                                'ref_id': rec.id,
                                'state': 'pending',
                            })

                        created_count = 0
                        created_names = []
                        for index in range(0, len(order_values), batch_size):
                            created = inbound_model.create(
                                order_values[index:index + batch_size]
                            )
                            created_count += len(created)
                            created_names.extend(created.mapped('name'))

                        if created_count != len(order_values):
                            raise ValidationError(_(
                                "SALES %(reference)s expected %(expected)s ORDER "
                                "rows but created %(created)s.",
                                reference=rec.name or rec.id,
                                expected=len(order_values),
                                created=created_count,
                            ))

                        rec.write({
                            'state': 'done',
                            'log_info': (
                                f"<b>Executed at {local_dt}:</b><br>SALES "
                                f"processed successfully (created "
                                f"{created_count} ORDER rows)."
                            ),
                            'output': ", ".join(created_names),
                            'ref_model': False,
                            'ref_id': False,
                        })
                        success_result = (rec.id, created_count)

                if duplicate_msg:
                    duplicate_updates.append((rec.id, duplicate_msg))
                    continue

                # Python state is updated only after the savepoint succeeds.
                successful_results.append(success_result)
                processed_names.add(rec.name)
            except Exception as error:
                _logger.exception(
                    "SALES processing failed for inbound %s: %s",
                    rec.id,
                    error,
                )
                failed_updates.append((
                    rec.id,
                    f"<b>Executed at {local_dt}:</b><br>SALES processing "
                    "failed and all created ORDER rows were rolled back: "
                    f"{Markup.escape(str(error))}",
                ))

        for rec_id, msg in failed_updates:
            self.browse(rec_id).exists().write({
                'state': 'failed',
                'log_info': msg,
                'output': False,
                'ref_model': False,
                'ref_id': False,
            })

        for rec_id, msg in duplicate_updates:
            self.browse(rec_id).exists().write({
                'state': 'duplicate',
                'log_info': msg,
                'output': False,
                'ref_model': False,
                'ref_id': False,
            })

        _logger.info(
            "Executed at %s: SALES processing completed: %d ORDER rows "
            "created, failed=%d, duplicate=%d",
            fields.Datetime.context_timestamp(
                self, fields.Datetime.now()
            ).replace(microsecond=0, tzinfo=None),
            sum(result[1] for result in successful_results),
            len(failed_updates),
            len(duplicate_updates),
        )
        if deferred_count:
            _logger.info(
                "SALES processing deferred %d contested inbound records; "
                "they remain pending for the next run.",
                deferred_count,
            )

    # -----------------------------
    # 2.4 Process ORDER → POS Order
    # -----------------------------
    def process_order(self, batch_size=500):
        """
        Optimized process_order:
        - single-pass XML parse
        - preload products/sites/warehouses
        - deduplicate references
        - atomic batch create + confirm + inbound success update
        - savepoint-isolated fallback per record on batch failure
        - ORM updates for duplicate/failed inbound states
        - per-batch and total timing logs

        Transaction invariant:
        POS Order + done picking + inbound state=done are persisted together.
        This method intentionally does not commit or fully rollback the cursor;
        the outer cron/request owns the final transaction boundary.
        """
        # start timer
        start_total = _time.time()
        RefModel = 'snk.pos.order'

        all_values = []          # list of dicts to create (in order)
        created_mapping = []     # parallel list: inbound_rec_id for each all_values item
        company_mapping = []     # parallel list: company_id for each value
        successful_results = []  # recorded only after atomic savepoint success
        failed_updates = []      # list of tuples (inbound_id, log_info)
        duplicate_updates = []   # list of tuples (inbound_id, log_info)
        deferred_updates = []    # list of tuples (inbound_id, log_info)
        failed_count = 0

        # Keep this invariant synchronized with ORDER_TRANSACTION_NOTES.md.
        def _escape_html(value):
            """Inbound payload/error text is untrusted and log_info is HTML."""
            return str(Markup.escape(str(value)))

        def _ensure_orders_processed(orders):
            """A successful validate call must leave every picking in ``done``."""
            orders.invalidate_recordset(['stock_picking_id'])
            orders.mapped('stock_picking_id').invalidate_recordset(['state'])
            incomplete_orders = orders.filtered(
                lambda order: not order.stock_picking_id
                or order.stock_picking_id.state != 'done'
            )
            if not incomplete_orders:
                return

            details = ", ".join(
                "%s (picking=%s, state=%s)" % (
                    order.display_name,
                    order.stock_picking_id.display_name or "missing",
                    order.stock_picking_id.state or "missing",
                )
                for order in incomplete_orders
            )
            raise ValidationError(_(
                "POS Order processing incomplete; every picking must be done: %(details)s",
                details=details,
            ))

        def _mark_inbound_done(rec_id, order):
            """Write success inside the same savepoint as the business records."""
            inbound = self.browse(rec_id).exists()
            if not inbound:
                raise ValidationError(_(
                    "Inbound record %(rec_id)s no longer exists.",
                    rec_id=rec_id,
                ))

            local_dt = fields.Datetime.context_timestamp(
                inbound, fields.Datetime.now()
            ).replace(microsecond=0, tzinfo=None)
            inbound.write({
                'state': 'done',
                'log_info': (
                    f"<b>Executed at {local_dt}:</b><br>"
                    "POS Order and stock picking processed successfully."
                ),
                'output': order.name,
                'ref_model': RefModel,
                'ref_id': order.id,
            })
            return rec_id, order.name

        # -----------------------
        # Step A: single-pass parse all inbound -> collect sets + parsed map
        # -----------------------
        parsed = {}     # rec.id -> parsed fields dict
        store_codes = set()
        item_codes = set()
        references = []  # list of references to check duplicates

        for rec in self:
            try:
                root = etree.fromstring(rec.data)
                Store = root.findtext('Store') or ''
                Company = (root.findtext('Company') or '').strip()
                SalesDate = root.findtext('SalesDate') or ''
                ItemCode = root.findtext('ItemCode') or ''
                Quantity = float(root.findtext('Quantity') or 0)
                GrossAmount = float(root.findtext('GrossAmount') or 0)
                DiscItem = float(root.findtext('DiscItem') or 0)
                ref = rec.name or ''

                parsed[rec.id] = {
                    'rec': rec,
                    'root': root,
                    'Store': Store,
                    'Company': Company,
                    'SalesDate': SalesDate,
                    'ItemCode': ItemCode,
                    'Quantity': Quantity,
                    'GrossAmount': GrossAmount,
                    'DiscItem': DiscItem,
                    'reference': ref,
                }

                if Store:
                    store_codes.add(Store)
                if ItemCode:
                    item_codes.add(ItemCode)
                if ref:
                    references.append(ref)

            except Exception as e:
                local_dt = fields.Datetime.context_timestamp(
                    rec, _dt.now()).replace(microsecond=0, tzinfo=None)
                failed_updates.append(
                    (rec.id, f"<b>Executed at {local_dt}:</b><br>Failed to parse inbound XML: {_escape_html(e)}"))
                failed_count += 1

        # -----------------------
        # Step B: resolve allowed companies and preload company-scoped masters
        # -----------------------
        allowed_companies = self.env.companies
        allowed_company_ids = allowed_companies.ids
        allowed_company_by_id = {
            company.id: company for company in allowed_companies
        }
        companies_by_registry = {}
        for company in allowed_companies:
            registry = (company.company_registry or '').strip().upper()
            if registry:
                companies_by_registry.setdefault(registry, []).append(company)

        # Resolve company before acquiring locks. A missing Company remains
        # backward compatible and uses the active company; an explicit unknown
        # Company must fail instead of leaking into another allowed company.
        order_lock_keys = []
        for info in parsed.values():
            company_code = info['Company'].upper()
            company_matches = companies_by_registry.get(company_code, [])
            if company_code:
                company = company_matches[0] if len(company_matches) == 1 else False
            else:
                company = self.env.company
            info['company'] = company
            info['company_ambiguous'] = len(company_matches) > 1
            if company and info['reference']:
                lock_key = (
                    f"snk.pi.inbound:ORDER:{company.id}:"
                    f"{info['reference']}"
                )
                order_lock_keys.append(lock_key)
                info['lock_key'] = lock_key
            else:
                info['lock_key'] = False

        # Non-blocking bulk locks close the cross-worker duplicate race without
        # making unrelated ORDER rows wait behind a contested reference.
        acquired_order_keys = self._try_lock_inbound_business_keys(
            order_lock_keys
        )

        product_candidates = {}
        if item_codes:
            products = self.env['product.product'].search([
                ('default_code', 'in', list(item_codes)),
                ('company_id', 'in', [False] + allowed_company_ids),
            ])
            for product in products:
                key = (product.company_id.id or False, product.default_code)
                product_candidates.setdefault(key, []).append(product)

        site_candidates = {}
        if store_codes:
            sites = self.env['snk.site'].search([
                ('snk_code', 'in', list(store_codes)),
                ('company_id', 'in', [False] + allowed_company_ids),
            ])
            for site in sites:
                site_company_id = (
                    site.company_id.id or site.warehouse_id.company_id.id or False
                )
                site_candidates.setdefault(
                    (site_company_id, site.snk_code), []
                ).append(site)

        warehouse_candidates = {}
        if store_codes:
            warehouses = self.env['stock.warehouse'].search([
                ('code', 'in', list(store_codes)),
                ('company_id', 'in', allowed_company_ids),
            ])
            for warehouse in warehouses:
                warehouse_candidates.setdefault(
                    (warehouse.company_id.id, warehouse.code), []
                ).append(warehouse)

        existing_orders_by_key = {}
        if references and allowed_company_ids:
            existing_orders = self.env[RefModel].search([
                ('reference', 'in', references),
                ('company_id', 'in', allowed_company_ids),
            ])
            for order in existing_orders:
                existing_orders_by_key.setdefault(
                    (order.reference, order.company_id.id), order
                )
        existing_ref_keys = set(existing_orders_by_key)
        created_refs = set()

        # -----------------------
        # Step C: build all_values
        # -----------------------
        for rec_id, info in parsed.items():
            rec = info['rec']
            Store = info['Store']
            SalesDate = info['SalesDate']
            ItemCode = info['ItemCode']
            Quantity = info['Quantity']
            GrossAmount = info['GrossAmount']
            DiscItem = info['DiscItem']
            reference = info['reference']
            company = info['company']

            local_dt = fields.Datetime.context_timestamp(
                rec, _dt.now()).replace(microsecond=0, tzinfo=None)

            if (
                info['lock_key']
                and info['lock_key'] not in acquired_order_keys
            ):
                deferred_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>ORDER processing "
                    "deferred because another worker is processing the same "
                    "company/reference. State remains pending.",
                ))
                continue

            company_id = company.id if company else False
            matching_sites = site_candidates.get((company_id, Store), [])
            matching_warehouses = warehouse_candidates.get(
                (company_id, Store), []
            )
            company_products = product_candidates.get(
                (company_id, ItemCode), []
            )
            shared_products = product_candidates.get((False, ItemCode), [])
            matching_products = company_products or shared_products
            site = matching_sites[0] if len(matching_sites) == 1 else False
            warehouse = (
                matching_warehouses[0]
                if len(matching_warehouses) == 1 else False
            )
            Product = (
                matching_products[0] if len(matching_products) == 1 else False
            )
            company_label = (
                info['Company']
                or (company.display_name if company else '-')
            )

            # Validate essential fields - per field untuk error message yang spesifik
            errors = []
            if not Store:        errors.append(f"Store kosong")
            if info['company_ambiguous']:
                errors.append(
                    f"Company ambigu untuk registry: {info['Company']}"
                )
            elif info['Company'] and not company:
                errors.append(
                    f"Company tidak ditemukan/tidak diizinkan: {info['Company']}"
                )
            if not SalesDate:    errors.append(f"SalesDate kosong")
            if not ItemCode:     errors.append(f"ItemCode kosong")
            if not reference:    errors.append(f"Reference kosong")
            if Quantity == 0:    errors.append(f"Quantity = 0")
            if len(matching_sites) > 1:
                errors.append(
                    f"Site ambigu untuk company={company_label}, "
                    f"store={Store}"
                )
            elif not site:
                errors.append(
                    f"Site tidak ditemukan untuk company="
                    f"{company_label}: {Store}"
                )
            if site and site.snk_state != 'open':
                errors.append(f"Site belum dibuka, saat ini statusnya {site.snk_state}: {Store}")
            if len(matching_warehouses) > 1:
                errors.append(
                    f"Warehouse ambigu untuk company="
                    f"{company_label}, code={Store}"
                )
            elif not warehouse:
                errors.append(
                    f"Warehouse tidak ditemukan untuk company="
                    f"{company_label}: {Store}"
                )
            if site and warehouse and site.warehouse_id != warehouse:
                errors.append(
                    f"Site {Store} terhubung ke warehouse "
                    f"{site.warehouse_id.display_name}, bukan {warehouse.display_name}"
                )
            if len(matching_products) > 1:
                errors.append(
                    f"Product ambigu untuk company="
                    f"{company_label}, code={ItemCode}"
                )
            elif not Product:
                errors.append(
                    f"Product tidak ditemukan (atau inactive): {ItemCode}"
                )

            if errors:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>"
                    + "<br>".join(f"&bull; {_escape_html(error)}" for error in errors)
                ))
                failed_count += 1
                continue


            reference_key = (reference, company.id)
            if reference_key in existing_ref_keys or reference_key in created_refs:
                existing_order = existing_orders_by_key.get(reference_key)
                duplicate_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>"
                    f"Duplicate reference {_escape_html(reference)} untuk "
                    f"company {_escape_html(company.display_name)}",
                    existing_order.name if existing_order else False,
                    existing_order.id if existing_order else False,
                ))
                continue

            # -----------------------------
            # Determine PickingType
            # -----------------------------
            if Quantity > 0:
                PickingType = warehouse.snk_pos_out_id
            elif Quantity < 0:
                PickingType = warehouse.snk_pos_in_id
            else:  # Quantity = 0
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Quantity = 0, cannot process."
                ))
                failed_count += 1
                continue

            if not PickingType:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Warehouse "
                    f"{_escape_html(warehouse.code)} tidak punya Picking Type "
                    f"untuk Qty={_escape_html(Quantity)}."
                ))
                failed_count += 1
                continue

            if PickingType.company_id and PickingType.company_id != company:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Operation Type "
                    f"{_escape_html(PickingType.display_name)} berasal dari "
                    f"company {_escape_html(PickingType.company_id.display_name)}, "
                    f"bukan {_escape_html(company.display_name)}."
                ))
                failed_count += 1
                continue

            # -----------------------------
            # Calculate price_unit & discount
            # -----------------------------
            # Price per unit sesuai tanda Quantity
            price_unit = (GrossAmount / Quantity) if Quantity != 0 else 0

            # Discount amount per unit (bisa negatif)
            Disc = (DiscItem / Quantity) if Quantity != 0 else 0

            # Discount (bisa negatif jika DiscItem negatif)
            Discount = (DiscItem / GrossAmount *
                        100) if GrossAmount != 0 else 0

            vals = {
                'snk_site_id': site.id,
                'customer_id': site.customer_id.id or False,
                'partner_id': site.partner_id.id or False,
                'warehouse_id': warehouse.id,
                'price_unit': price_unit,
                'date_order': _dt.combine(_dt.strptime(SalesDate, "%Y-%m-%d").date(), _time_of_day(14)),
                'picking_type_id': PickingType.id,
                'promo_code': info['root'].findtext('PromoCode') or '',
                'discount_amount': Disc,
                'discount': Discount,
                'product_id': Product.id,
                'product_uom_qty': Quantity,
                'reference': reference,
                'company_id': company.id,
            }

            all_values.append(vals)
            created_mapping.append(rec_id)
            company_mapping.append(company.id)
            created_refs.add(reference_key)

        # -----------------------
        # Step D: atomic batch create + fallback per-row
        # -----------------------
        values_by_company = {}
        for vals, rec_id, company_id in zip(
            all_values, created_mapping, company_mapping
        ):
            values_by_company.setdefault(company_id, []).append((vals, rec_id))

        batch_count = 0
        for company_id in sorted(values_by_company):
            company = allowed_company_by_id[company_id]
            company_values = values_by_company[company_id]
            order_model = self.env[RefModel].with_company(company).with_context(
                from_pos_inbound=True
            )

            for index in range(0, len(company_values), batch_size):
                batch_count += 1
                chunk_pairs = company_values[index:index + batch_size]
                source_chunk = [pair[0] for pair in chunk_pairs]
                chunk_rec_ids = [pair[1] for pair in chunk_pairs]
                batch_start = _time.time()
                try:
                    batch_results = []
                    with self.env.cr.savepoint():
                        # Model create chains may mutate their input dictionaries
                        # (for example by assigning generated names). Keep the
                        # source templates pristine for a possible fallback.
                        orders = order_model.create(deepcopy(source_chunk))
                        orders.action_confirm()
                        _ensure_orders_processed(orders)
                        batch_results = [
                            _mark_inbound_done(rec_id, order)
                            for order, rec_id in zip(orders, chunk_rec_ids)
                        ]

                    # Python containers are not transactional. Only record
                    # success after the savepoint exits without an exception.
                    successful_results.extend(batch_results)
                    batch_elapsed = _time.time() - batch_start
                    _logger.info(
                        "Batch %d: processed %d orders atomically for company "
                        "%s in %.2f sec",
                        batch_count,
                        len(orders),
                        company.display_name,
                        batch_elapsed,
                    )

                except Exception as error:
                    _logger.exception(
                        "Batch %d for company %s failed and was rolled back: "
                        "%s. Falling back to per-row processing.",
                        batch_count,
                        company.display_name,
                        error,
                    )

                    for source_vals, rec_id in chunk_pairs:
                        try:
                            row_result = None
                            with self.env.cr.savepoint():
                                order = order_model.create(
                                    deepcopy(source_vals)
                                )
                                order.action_confirm()
                                _ensure_orders_processed(order)
                                row_result = _mark_inbound_done(rec_id, order)

                            successful_results.append(row_result)
                            _logger.info(
                                "ORDER fallback succeeded: batch=%d, "
                                "inbound_id=%s, order=%s, company_id=%s",
                                batch_count,
                                rec_id,
                                order.name,
                                company.id,
                            )
                        except Exception as row_error:
                            local_dt = fields.Datetime.context_timestamp(
                                self.browse(rec_id), fields.Datetime.now()
                            ).replace(microsecond=0, tzinfo=None)
                            failed_updates.append((
                                rec_id,
                                f"<b>Executed at {local_dt}:</b><br>"
                                "POS Order processing failed and all business "
                                "changes were rolled back: "
                                f"{_escape_html(row_error)}",
                            ))
                            failed_count += 1

        # -----------------------
        # Step E: persist non-success outcomes in the outer transaction
        # -----------------------
        for rec_id, msg in deferred_updates:
            # Deliberately keep the inbound pending. A later cron/request can
            # retry after the competing transaction releases its advisory lock.
            self.browse(rec_id).exists().write({
                'log_info': msg,
            })

        for rec_id, msg, output, ref_id in duplicate_updates:
            self.browse(rec_id).exists().write({
                'state': 'duplicate',
                'log_info': msg,
                'output': output,
                'ref_model': RefModel if ref_id else False,
                'ref_id': ref_id or False,
            })

        for rec_id, msg in failed_updates:
            self.browse(rec_id).exists().write({
                'state': 'failed',
                'log_info': msg,
                'output': False,
                'ref_model': False,
                'ref_id': False,
            })

        # -----------------------
        # Final log summary
        # -----------------------
        total_elapsed = _time.time() - start_total
        final_dt = fields.Datetime.context_timestamp(
            self, _dt.now()).replace(microsecond=0, tzinfo=None)
        _logger.info(
            "Executed at %s: Process order completed: %d POS Orders created, failed=%d, duplicates=%d, elapsed=%.2f sec",
            final_dt, len(successful_results), failed_count, len(
                duplicate_updates), total_elapsed
        )
        if deferred_updates:
            _logger.info(
                "ORDER processing deferred %d contested inbound records; "
                "they remain pending for the next run.",
                len(deferred_updates),
            )

        return True

    def process_TO(self, batch_size=50):
        """
        Optimized process_TO (batch architecture, matching process_order):
        - single-pass XML parse
        - preload products/sites/warehouses/operation types
        - batch duplicate detection
        - per-batch create + validate with fallback
        - ORM traceability updates for inbound outcomes
        - per-batch and total timing logs

        Transaction invariant:
        done picking + inbound state=done are persisted together. This method
        intentionally does not commit or fully rollback the cursor; the outer
        cron/request owns the final transaction boundary.
        """
        start_total = _time.time()
        RefModel = 'stock.picking'

        successful_results = []  # recorded only after atomic savepoint success
        failed_updates = []      # list of tuples (rec_id, log_info, output_name, ref_id)
        duplicate_updates = []   # list of tuples (rec_id, log_info, output_name, ref_id)
        failed_count = 0

        def _ensure_pickings_done(pickings):
            """Do not treat a wizard-returning validate call as success."""
            pickings.invalidate_recordset(['state'])
            incomplete_pickings = pickings.filtered(
                lambda picking: picking.state != 'done'
            )
            if not incomplete_pickings:
                return

            details = ", ".join(
                f"{picking.display_name or picking.id}: {picking.state}"
                for picking in incomplete_pickings
            )
            raise ValidationError(_(
                "Transfer processing incomplete; every picking must be done: "
                "%(details)s",
                details=details,
            ))

        def _mark_inbound_done(rec_id, picking, fallback=False):
            """Write TI-style traceability in the picking savepoint."""
            inbound = self.browse(rec_id).exists()
            if not inbound:
                raise ValidationError(_(
                    "Inbound record %(rec_id)s no longer exists.",
                    rec_id=rec_id,
                ))

            local_dt = fields.Datetime.context_timestamp(
                inbound, fields.Datetime.now()
            ).replace(microsecond=0, tzinfo=None)
            result_label = (
                "Data Integrated (fallback)" if fallback
                else "Data Integrated"
            )
            inbound.write({
                'state': 'done',
                'log_info': (
                    f"<b>Executed at {local_dt}:</b><br><ul>"
                    f"<li>{result_label}: {picking.name}</li></ul>"
                ),
                'ref_id': picking.id,
                'ref_model': RefModel,
                'output': picking.name,
            })
            return rec_id, picking.id, picking.name

        # -----------------------
        # Step A: single-pass XML parse → collect all unique codes
        # -----------------------
        parsed = {}              # rec.id → parsed fields dict
        source_store_codes = set()
        dest_store_codes = set()
        all_item_codes = set()
        all_doc_nums = []        # for duplicate check

        for rec in self:
            try:
                root = etree.fromstring(rec.data)
                DocNum = root.xpath('//DocNum/text()')[0]
                DocDate = root.xpath('//DocDate/text()')[0]
                SourceStore = root.xpath('//SourceStore/text()')[0]
                DestinationStore = root.xpath('//DestinationStore/text()')[0]
                Remarks = root.xpath('//Remarks/text()')[0]
                Type = root.xpath('//DocType/text()')[0]
                TotalLine = int(root.xpath('//TotalLine/text()')[0])

                # Collect item codes from all rows
                row_items = []
                for i in range(1, TotalLine + 1):
                    RowPath = '//Row[%s]/' % i
                    ItemCode = root.xpath(RowPath + 'ItemCode/text()')[0]
                    QtyItemCode = root.xpath(RowPath + 'QtyItemCode/text()')[0]
                    row_items.append({'ItemCode': ItemCode, 'Qty': QtyItemCode})
                    all_item_codes.add(ItemCode)

                parsed[rec.id] = {
                    'rec': rec,
                    'DocNum': DocNum,
                    'DocDate': _dt.strptime(DocDate, "%Y-%m-%d").date(),
                    'SourceStore': SourceStore,
                    'DestinationStore': DestinationStore,
                    'Remarks': Remarks,
                    'Type': Type,
                    'row_items': row_items,
                }

                source_store_codes.add(SourceStore)
                dest_store_codes.add(DestinationStore)
                all_doc_nums.append(DocNum)

            except Exception as e:
                local_dt = fields.Datetime.context_timestamp(
                    rec, _dt.now()).replace(microsecond=0, tzinfo=None)
                failed_updates.append((
                    rec.id,
                    f"<b>Executed at {local_dt}:</b><br>Failed to parse inbound XML: {str(e)}",
                    None, None
                ))
                failed_count += 1

        # -----------------------
        # Step B: bulk pre-fetch all lookups
        # -----------------------
        all_store_codes = source_store_codes | dest_store_codes

        # Sites: keyed by snk_code
        sites = {s.snk_code: s for s in self.env['snk.site'].search(
            [('snk_code', 'in', list(all_store_codes))])} if all_store_codes else {}

        # Warehouses: keyed by code
        warehouses = {w.code: w for w in self.env['stock.warehouse'].search(
            [('code', 'in', list(source_store_codes))])} if source_store_codes else {}

        # Products: keyed by default_code
        products = {p.default_code: p for p in self.env['product.product'].search(
            [('default_code', 'in', list(all_item_codes))])} if all_item_codes else {}

        # Operation Types: pre-fetch non-MRP outgoing types for source stores.
        # MRP always originates from Odoo and must not be selected by inbound TO.
        # snk_is_mutation is intentionally not a lookup discriminator: the selected
        # Operation Type defines whether validation must create a receipt.
        # Cache key: (warehouse_id, site_id, mutation_code)
        all_op_types = self.env['stock.picking.type'].search([
            ('code', '=', 'outgoing'),
            ('snk_is_mrp', '=', False),
        ]) if source_store_codes else self.env['stock.picking.type']
        op_type_cache = {}
        for ot in all_op_types:
            key = (ot.warehouse_id.id, ot.site_id.id, ot.snk_mutation_code)
            op_type_cache.setdefault(key, []).append(ot)

        # Duplicate check: batch query existing pickings by snk_pos_ref + picking_type_id
        # Matching legacy: hanya duplicate kalau DocNum DAN picking_type SAMA
        # Keep the matching picking metadata, not only the duplicate key. The
        # inbound `ref_id` field stores an Odoo database ID (Integer), while
        # `snk_pos_ref`/DocNum is an alphanumeric business reference.
        existing_picks_by_key = {}
        if all_doc_nums:
            existing_picks = self.env[RefModel].search_read(
                [('snk_pos_ref', 'in', all_doc_nums)],
                ['snk_pos_ref', 'picking_type_id', 'name']
            )
            for picking_data in existing_picks:
                if not picking_data.get('picking_type_id'):
                    continue
                duplicate_key = (
                    picking_data['snk_pos_ref'],
                    picking_data['picking_type_id'][0],
                )
                # Duplicate detection only needs one matching transfer. Keep
                # the first deterministic ORM result if historical data already
                # contains more than one picking for the same business key.
                existing_picks_by_key.setdefault(duplicate_key, picking_data)
        created_doc_keys = set()  # set of (snk_pos_ref, picking_type_id)

        # -----------------------
        # Step C: build picking values from parsed data
        # -----------------------
        all_picking_values = []   # list of dicts to create
        picking_mapping = []      # parallel list: rec_id for each picking

        for rec_id, info in parsed.items():
            rec = info['rec']
            local_dt = fields.Datetime.context_timestamp(
                rec, _dt.now()).replace(microsecond=0, tzinfo=None)

            DocNum = info['DocNum']
            SourceStore = info['SourceStore']
            DestinationStore = info['DestinationStore']
            Type = info['Type']

            # Resolve lookups from cache
            sourcesite = sites.get(SourceStore)
            destinationsite = sites.get(DestinationStore)
            warehouse = warehouses.get(SourceStore)

            # Validate essential lookups - per field untuk error message yang spesifik
            errors = []
            if not sourcesite:      errors.append(f"Source Site tidak ditemukan: {SourceStore}")
            if sourcesite and sourcesite.snk_state != 'open':
                errors.append(f"Source Site belum dibuka, saat ini statusnya {sourcesite.snk_state}: {SourceStore}")
            if not destinationsite: errors.append(f"Destination Site tidak ditemukan: {DestinationStore}")
            if destinationsite and destinationsite.snk_state != 'open':
                errors.append(f"Destination Site belum dibuka, saat ini statusnya {destinationsite.snk_state}: {DestinationStore}")
            if not warehouse:       errors.append(f"Warehouse tidak ditemukan: {SourceStore}")

            if errors:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>" + "<br>".join(f"• {e}" for e in errors),
                    None, None
                ))
                failed_count += 1
                continue

            # Resolve operation type from cache
            op_key = (warehouse.id, sourcesite.id, Type)
            operation_type_candidates = op_type_cache.get(op_key, [])
            if not operation_type_candidates:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>• Operation Type outgoing tidak ditemukan untuk: warehouse={warehouse.code}, site={sourcesite.snk_code}, mutation_code={Type}",
                    None, None
                ))
                failed_count += 1
                continue

            if len(operation_type_candidates) > 1:
                conflicting_operations = ", ".join(
                    f"{operation.display_name} (ID: {operation.id})"
                    for operation in operation_type_candidates
                )
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Konfigurasi Operation Type ambigu:<br>• Ditemukan lebih dari satu outgoing non-MRP Operation Type untuk warehouse={warehouse.code}, site={sourcesite.snk_code}, mutation_code={Type}: {conflicting_operations}",
                    None, None
                ))
                failed_count += 1
                continue

            OperationTypeOut = operation_type_candidates[0]

            # Duplicate check (matching legacy: DocNum + picking_type_id harus sama)
            dup_key = (DocNum, OperationTypeOut.id)
            existing_picking = existing_picks_by_key.get(dup_key)
            if existing_picking or dup_key in created_doc_keys:
                # An existing DB record can be linked through its integer ID.
                # A same-batch duplicate has no picking ID yet, so retain only
                # its business reference in `output` and leave `ref_id` empty.
                duplicate_output = (
                    existing_picking['name'] if existing_picking else DocNum
                )
                duplicate_ref_id = (
                    existing_picking['id'] if existing_picking else False
                )
                duplicate_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Found existing delivery with POS Reference: {DocNum}. Duplicate detected.",
                    duplicate_output, duplicate_ref_id
                ))
                continue

            # Build move lines from pre-fetched products
            data_lines = []
            missing_products = []
            for item in info['row_items']:
                Product = products.get(item['ItemCode'])
                if not Product:
                    missing_products.append(item['ItemCode'])
                    continue
                data_lines.append((0, 0, {
                    'product_id': Product.id,
                    'product_uom_qty': item['Qty'],
                    'quantity': item['Qty'],
                    'location_id': OperationTypeOut.default_location_src_id.id,
                    'location_dest_id': OperationTypeOut.default_location_dest_id.id,
                    'name': Product.name,
                }))

            if missing_products:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>• Product tidak ditemukan (atau inactive): {', '.join(missing_products)}",
                    None, None
                ))
                failed_count += 1
                continue

            if not data_lines:
                failed_updates.append((
                    rec_id,
                    f"<b>Executed at {local_dt}:</b><br>No valid line items for DocNum={DocNum}",
                    None, None
                ))
                failed_count += 1
                continue

            values = {
                'snk_site_id': sourcesite.id,
                'warehouse_address_id': sourcesite.partner_id.id,
                'snk_site_address_id': destinationsite.id,
                'partner_id': destinationsite.partner_id.id,
                'picking_type_code': 'outgoing',
                'picking_type_id': OperationTypeOut.id,
                'location_id': OperationTypeOut.default_location_src_id.id,
                'location_dest_id': OperationTypeOut.default_location_dest_id.id,
                'scheduled_date': _dt.combine(info['DocDate'], _time_of_day(14)),
                'snk_create_date': _dt.combine(info['DocDate'], _time_of_day(14)),
                'snk_is_pos': True,
                'snk_pos_ref': DocNum,
                'origin': info['Remarks'],
                'snk_inbound_id': rec_id,
                'move_ids': data_lines,
            }

            all_picking_values.append(values)
            picking_mapping.append(rec_id)
            created_doc_keys.add(dup_key)


        # -----------------------
        # Step D: batch create + validate with per-row fallback
        # -----------------------
        batch_count = 0
        for i in range(0, len(all_picking_values), batch_size):
            batch_count += 1
            source_chunk = all_picking_values[i:i + batch_size]
            chunk_rec_ids = picking_mapping[i:i + batch_size]
            batch_start = _time.time()

            try:
                batch_results = []
                with self.env.cr.savepoint():
                    # Odoo stock.picking.create() assigns ``name`` and pops
                    # ``scheduled_date`` from each input dict. Never let those
                    # mutations leak into the per-row fallback templates.
                    pickings = self.env[RefModel].create(
                        deepcopy(source_chunk)
                    )
                    for picking in pickings:
                        picking.analytic_ids = [
                            (6, 0, picking.move_ids.mapped(
                                "product_id.analytic_id"
                            ).ids)
                        ]
                    pickings.button_validate()
                    _ensure_pickings_done(pickings)
                    batch_results = [
                        _mark_inbound_done(rec_id, picking)
                        for picking, rec_id in zip(pickings, chunk_rec_ids)
                    ]

                # Python containers are not transactional. Record success only
                # after the savepoint exits without an exception.
                successful_results.extend(batch_results)

                batch_elapsed = _time.time() - batch_start
                _logger.info("TO Batch %d: created+validated %d pickings in %.2f sec",
                             batch_count, len(pickings), batch_elapsed)

            except Exception as e:
                _logger.exception(
                    "TO Batch %d processing failed and was rolled back: %s. "
                    "Falling back to per-row.",
                    batch_count,
                    e,
                )

                # Per-row fallback
                for source_vals, rec_id in zip(
                    source_chunk, chunk_rec_ids
                ):
                    try:
                        row_result = None
                        with self.env.cr.savepoint():
                            # A fresh dict forces stock.picking.create() to draw
                            # a fresh sequence number after the batch savepoint
                            # (and its No-gap increment) has rolled back.
                            picking = self.env[RefModel].create(
                                deepcopy(source_vals)
                            )
                            picking.analytic_ids = [
                                (6, 0, picking.move_ids.mapped(
                                    "product_id.analytic_id"
                                ).ids)
                            ]
                            picking.button_validate()
                            _ensure_pickings_done(picking)
                            row_result = _mark_inbound_done(
                                rec_id, picking, fallback=True
                            )

                        successful_results.append(row_result)
                        _logger.info(
                            "TO fallback succeeded: batch=%d, inbound_id=%s, "
                            "picking=%s, picking_type_id=%s, sequence_id=%s",
                            batch_count,
                            rec_id,
                            picking.name,
                            picking.picking_type_id.id,
                            picking.picking_type_id.sequence_id.id,
                        )
                    except Exception as ex2:
                        local_dt = fields.Datetime.context_timestamp(
                            self.browse(rec_id), fields.Datetime.now()
                        ).replace(microsecond=0, tzinfo=None)
                        failed_updates.append((
                            rec_id,
                            f"<b>Executed at {local_dt}:</b><br>"
                            "TO processing failed and all picking changes were "
                            f"rolled back: {Markup.escape(str(ex2))}",
                            None, None
                        ))
                        failed_count += 1

        # -----------------------
        # Step E: persist non-success outcomes in the outer transaction
        # -----------------------
        for rec_id, msg, output, ref_id in duplicate_updates:
            self.browse(rec_id).exists().write({
                'state': 'duplicate',
                'log_info': msg,
                'output': output,
                'ref_id': ref_id or False,
                'ref_model': RefModel if ref_id else False,
            })

        for rec_id, msg, output, ref_id in failed_updates:
            self.browse(rec_id).exists().write({
                'state': 'failed',
                'log_info': msg,
                'output': output,
                'ref_id': ref_id or False,
                'ref_model': RefModel if ref_id else False,
            })

        # -----------------------
        # Step F: final log summary
        # -----------------------
        total_elapsed = _time.time() - start_total
        final_dt = fields.Datetime.context_timestamp(
            self, _dt.now()).replace(microsecond=0, tzinfo=None)
        _logger.info(
            "Executed at %s: Process TO completed: %d pickings created, failed=%d, duplicates=%d, elapsed=%.2f sec",
            final_dt, len(successful_results), failed_count,
            len(duplicate_updates), total_elapsed
        )

        return True

    # ---------------------------------------------------------------
    # LEGACY: Arsip fungsi lama process_TO (per-record, non-batch)
    # ---------------------------------------------------------------
    # def _process_TO_legacy(self):
    #     # self.ensure_one()
    #
    #     RefId = 0
    #     RefModel = 'stock.picking'
    #     picking_type_code = 'outgoing'
    #
    #     try:
    #         root = etree.fromstring(self.data)
    #
    #         DocNum = root.xpath('//DocNum/text()')[0]
    #         DocDate = _dt.strptime(
    #             str(root.xpath('//DocDate/text()')[0]), "%Y-%m-%d").date()
    #         SourceStore = root.xpath('//SourceStore/text()')[0]
    #         SourceLocation = root.xpath('//SourceLocation/text()')[0]
    #         DestinationStore = root.xpath('//DestinationStore/text()')[0]
    #         DestinationLocation = root.xpath('//DestinationLocation/text()')[0]
    #         Remarks = root.xpath('//Remarks/text()')[0]
    #         Type = root.xpath('//DocType/text()')[0]
    #
    #         sourcesite = self.env['snk.site'].search(
    #             [('snk_code', '=', SourceStore)], limit=1)
    #         destinationsite = self.env['snk.site'].search(
    #             [('snk_code', '=', DestinationStore)], limit=1)
    #         SourceWarehouse = self.env['stock.warehouse'].search(
    #             [('code', '=', SourceStore)], limit=1)
    #         OperationTypeOut = self.env['stock.picking.type'].search([
    #             ('code', '=', 'outgoing'),
    #             ('site_id', '=', sourcesite.id),
    #             ('warehouse_id', '=', SourceWarehouse.id),
    #             ('snk_is_mutation', "=", True),
    #             ('snk_is_mrp', "=", False),
    #             ('snk_mutation_code', "=", Type)
    #         ], limit=1)
    #
    #         TO_Find = self.env[RefModel].search([('picking_type_id', '=', OperationTypeOut.id), ('snk_pos_ref', '=', DocNum)], limit=1)
    #         local_dt = fields.Datetime.context_timestamp(self, _dt.now()).replace(microsecond=0, tzinfo=None)
    #         if TO_Find:
    #             log_info = f"<b>Executed at {local_dt}:</b><br><ul><li>Found existing delivery ID: {TO_Find.name}, POS Document: {DocNum}</ul></li>"
    #             self.write({'state': 'duplicate', 'log_info': log_info})
    #         else:
    #             TotalLine = root.xpath('//TotalLine/text()')[0]
    #             data_lines = []
    #             for i in range(1, int(TotalLine) + 1):
    #                 RowPath = '//Row[%s]/' % i
    #                 ItemCode = root.xpath(RowPath + 'ItemCode/text()')[0]
    #                 QtyItemCode = root.xpath(RowPath + 'QtyItemCode/text()')[0]
    #                 Product = self.env['product.product'].search([('default_code', '=', ItemCode)], limit=1)
    #                 data_lines.extend([(0, 0, {
    #                     'product_id': Product.id,
    #                     'product_uom_qty': QtyItemCode,
    #                     'location_id': OperationTypeOut.default_location_src_id.id,
    #                     'location_dest_id': OperationTypeOut.default_location_dest_id.id,
    #                     'name': Product.name,
    #                 })])
    #             if len(data_lines):
    #                 values = {
    #                     'snk_site_id': sourcesite.id,
    #                     'warehouse_address_id': sourcesite.partner_id.id,
    #                     'snk_site_address_id': destinationsite.id,
    #                     'partner_id': destinationsite.partner_id.id,
    #                     'picking_type_code': picking_type_code,
    #                     'picking_type_id': OperationTypeOut.id,
    #                     'location_id': OperationTypeOut.default_location_src_id.id,
    #                     'location_dest_id': OperationTypeOut.default_location_dest_id.id,
    #                     'scheduled_date': _dt.combine(DocDate, _time_of_day(14)),
    #                     'snk_create_date': _dt.combine(DocDate, _time_of_day(14)),
    #                     'snk_is_pos': True,
    #                     'snk_pos_ref': DocNum,
    #                     'origin': Remarks,
    #                     'snk_inbound_id': self.id,
    #                     'move_ids': data_lines
    #                 }
    #                 TO = self.env[RefModel].create(values)
    #                 self.output = TO.name
    #                 TO.analytic_ids = [(6, 0, TO.move_ids.mapped("product_id.analytic_id").ids)]
    #                 TO.button_validate()
    #                 RefId = TO.id
    #                 log_info = f"<b>Executed at {local_dt}:</b><br><ul><li>Data Integrated</li></ul>"
    #                 self.write({'state': 'done', 'log_info': log_info,
    #                             'ref_model': RefModel})
    def process_TI(self, batch_size=50):
        """
        Optimized process_TI (batch architecture, matching process_TO):
        - single-pass XML parse
        - preload products/sites/warehouses/operation types
        - batch duplicate detection (existing receipts by snk_pos_ref)
        - separate new-create from existing-update
        - per-batch execution for updates and creates
        - ORM traceability updates for inbound outcomes

        Transaction invariant:
        updated and done receipt + inbound state=done are persisted together.
        This method intentionally does not commit or fully rollback the cursor;
        the outer cron/request owns the final transaction boundary.
        """
        start_total = _time.time()
        RefModel = 'stock.picking'

        successful_results = []  # recorded only after atomic savepoint success
        failed_updates = []      # tuples (rec_id, log_info, output_name, ref_id)
        duplicate_updates = []   # tuples (rec_id, log_info, output_name, ref_id)
        failed_count = 0
        processed_refs = set()    # Reference yang sudah berhasil divalidasi pada run ini

        def _mark_inbound_done(rec_id, picking):
            """Write receipt traceability inside the receipt savepoint."""
            inbound = self.browse(rec_id).exists()
            if not inbound:
                raise ValidationError(_(
                    "Inbound record %(rec_id)s no longer exists.",
                    rec_id=rec_id,
                ))

            local_dt = fields.Datetime.context_timestamp(
                inbound, fields.Datetime.now()
            ).replace(microsecond=0, tzinfo=None)
            inbound.write({
                'state': 'done',
                'log_info': (
                    f"<b>Executed at {local_dt}:</b><br><ul>"
                    f"<li>Found existing receipt ID: {picking.name}. "
                    "Updated &amp; Processed.</li></ul>"
                ),
                'ref_id': picking.id,
                'ref_model': RefModel,
                'output': picking.name,
            })
            return rec_id, picking.id, picking.name

        # -----------------------
        # Step A: single-pass XML parse
        # -----------------------
        parsed = {}              # rec_id -> parsed field dict
        source_store_codes = set()
        dest_store_codes = set()
        all_item_codes = set()
        all_references = []      # field name 'Reference' in XML, matches 'snk_pos_ref' in Odoo

        for rec in self:
            try:
                root = etree.fromstring(rec.data)
                # Validation: Ensure all required header fields exist
                required_header = ['DocDate', 'SourceStore', 'DestinationStore', 'Reference', 'DocType', 'TotalLine']
                missing_h = [f for f in required_header if not root.xpath(f'//{f}/text()')]
                if missing_h:
                    raise ValueError(f"Field XML berikut kosong atau tidak ditemukan: {', '.join(missing_h)}")

                DocDate = root.xpath('//DocDate/text()')[0]
                SourceStore = root.xpath('//SourceStore/text()')[0]
                DestinationStore = root.xpath('//DestinationStore/text()')[0]
                Remarks = root.xpath('//Remarks/text()')[0] if root.xpath('//Remarks/text()') else ''
                Reference = root.xpath('//Reference/text()')[0]
                if root.xpath('//DocType/text()')[0] == 'TI':
                    # Sementara karena POS belum ada DocType yang flexible seperti ODOO
                    # DocType digunakan untuk mapping opeartion type mutasi Delivery -> Transit -> Receipt
                    # TI = TO karena TI adalah mutasi dari Transit ke Receipt
                    # Kecuali TO dan AH karena TO awal mutasi dan AH adjustment yang tidak melalui transit
                    # Jadi incoming transfer dengan kode TI harus di force menjadi TO
                    # TODO: Nanti kalau sudah ada DocType yang flexible bisa di remove
                    # Pandabluu~
                    Type = 'TO'
                else:
                    Type = root.xpath('//DocType/text()')[0]
                TotalLine = int(root.xpath('//TotalLine/text()')[0])
                _logger.info(f"Type: {Type}")
                
                row_items = []
                for i in range(1, TotalLine + 1):
                    RowPath = '//Row[%s]/' % i
                    
                    # Validation: Ensure row fields exist
                    if not root.xpath(RowPath + 'ItemCode/text()') or not root.xpath(RowPath + 'QtyItemCode/text()'):
                        raise ValueError(f"Baris ke-{i} memiliki ItemCode atau QtyItemCode yang kosong")

                    ItemCode = root.xpath(RowPath + 'ItemCode/text()')[0]
                    Qty = root.xpath(RowPath + 'QtyItemCode/text()')[0]
                    row_items.append({'ItemCode': ItemCode, 'Qty': Qty})
                    all_item_codes.add(ItemCode)

                parsed[rec.id] = {
                    'rec': rec,
                    'DocDate': _dt.strptime(DocDate, "%Y-%m-%d").date(),
                    'SourceStore': SourceStore,
                    'DestinationStore': DestinationStore,
                    'Remarks': Remarks,
                    'Reference': Reference,
                    'Type': Type,
                    'row_items': row_items,
                }
                source_store_codes.add(SourceStore)
                dest_store_codes.add(DestinationStore)
                all_references.append(Reference)

            except Exception as e:
                local_dt = fields.Datetime.context_timestamp(
                    rec, _dt.now()).replace(microsecond=0, tzinfo=None)
                failed_updates.append((
                    rec.id,
                    f"<b>Executed at {local_dt}:</b><br>Failed to parse inbound XML: {str(e)}",
                    None, None
                ))
                failed_count += 1

        # -----------------------
        # Step B: Bulk Pre-fetch
        # -----------------------
        all_store_codes = source_store_codes | dest_store_codes
        sites = {s.snk_code: s for s in self.env['snk.site'].search(
            [('snk_code', 'in', list(all_store_codes))])} if all_store_codes else {}

        products = {p.default_code: p for p in self.env['product.product'].search(
            [('default_code', 'in', list(all_item_codes))])} if all_item_codes else {}

        # Operation Types: incoming for destinationsite
        all_op_types = self.env['stock.picking.type'].search([
            ('code', '=', 'incoming'),
        ])
        op_type_cache = {}
        for ot in all_op_types:
            key = (ot.site_id.id, ot.warehouse_id.id, ot.snk_mutation_code)
            op_type_cache[key] = ot

        # Existing Pickings (by Reference)
        existing_pickings = {}
        if all_references:
            picks = self.env[RefModel].search([
                ('picking_type_code', '=', 'incoming'),
                ('snk_pos_ref', 'in', all_references)
            ])
            for p in picks:
                existing_pickings[p.snk_pos_ref] = p

        # -----------------------
        # Step C & D: Logic Build & Execution
        # -----------------------
        # We process in batches of 50
        all_rec_ids = list(parsed.keys())

        for i in range(0, len(all_rec_ids), batch_size):
            batch_rec_ids = all_rec_ids[i:i + batch_size]
            batch_start = _time.time()

            update_batch_data = []      # list of dicts: {rec_id, info, picking}

            for rid in batch_rec_ids:
                info = parsed[rid]
                local_dt = fields.Datetime.context_timestamp(
                    info['rec'], _dt.now()).replace(microsecond=0, tzinfo=None)

                existing_pick = existing_pickings.get(info['Reference'])
                if not existing_pick:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>Reference tidak ditemukan di Receipt: {info['Reference']}",
                        None, None
                    ))
                    failed_count += 1
                    continue

                # Receipt yang sudah selesai adalah duplicate secara bisnis, bukan
                # processing failure. Cek ini dilakukan sebelum validasi master data
                # karena receipt tidak akan disentuh lagi.
                if existing_pick.state == 'done':
                    duplicate_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Duplicate inbound: receipt {existing_pick.name} untuk Reference {info['Reference']} sudah berstatus done.",
                        existing_pick.name,
                        existing_pick.id
                    ))
                    continue

                if existing_pick.state == 'cancel':
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Receipt {existing_pick.name} untuk Reference {info['Reference']} berstatus cancel dan tidak dapat diproses.",
                        existing_pick.name,
                        existing_pick.id
                    ))
                    failed_count += 1
                    continue

                sourcesite = sites.get(info['SourceStore'])
                destinationsite = sites.get(info['DestinationStore'])
                
                # Validation
                errors = []
                if not sourcesite:      errors.append(f"Source Site tidak ditemukan: {info['SourceStore']}")
                if sourcesite and sourcesite.snk_state != 'open':
                    errors.append(f"Source Site belum dibuka, saat ini statusnya {sourcesite.snk_state}: {info['SourceStore']}")
                if not destinationsite: errors.append(f"Destination Site tidak ditemukan: {info['DestinationStore']}")
                if destinationsite and destinationsite.snk_state != 'open':
                    errors.append(f"Destination Site belum dibuka, saat ini statusnya {destinationsite.snk_state}: {info['DestinationStore']}")
                
                if errors:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>" + "<br>".join(f"• {e}" for e in errors),
                        None, None
                    ))
                    failed_count += 1
                    continue

                # Operation Type
                op_key = (destinationsite.id, destinationsite.warehouse_id.id, info['Type'])
                OperationTypeIn = op_type_cache.get(op_key)
                if not OperationTypeIn:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>• Operation Type incoming tidak ditemukan untuk: site={info['DestinationStore']}, warehouse={destinationsite.warehouse_id.code}, mutation_code={info['Type']}",
                        None, None
                    ))
                    failed_count += 1
                    continue

                # Build Lines
                line_data = []
                missing_products = []
                for item in info['row_items']:
                    prod = products.get(item['ItemCode'])
                    if not prod:
                        missing_products.append(item['ItemCode'])
                        continue
                    line_data.append({
                        'product_id': prod.id,
                        'product_uom_qty': item['Qty'],
                        'quantity': item['Qty'],
                        'location_id': OperationTypeIn.default_location_src_id.id,
                        'location_dest_id': OperationTypeIn.default_location_dest_id.id,
                        'name': prod.name,
                    })
                
                if missing_products:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Data tidak valid:<br>• Product tidak ditemukan (atau inactive): {', '.join(missing_products)}",
                        None, None
                    ))
                    failed_count += 1
                    continue

                if not line_data:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>No valid line items for Reference={info['Reference']}",
                        None, None
                    ))
                    failed_count += 1
                    continue

                update_batch_data.append({
                    'rid': rid,
                    'info': info,
                    'picking': existing_pick,
                    'line_data': line_data
                })

            # Execution Batch Update (Existing)
            for upd in update_batch_data:
                rid = upd['rid']
                p = upd['picking']
                info = upd['info']
                ldat = upd['line_data']
                local_dt = fields.Datetime.context_timestamp(
                    info['rec'], fields.Datetime.now()
                ).replace(microsecond=0, tzinfo=None)

                # A reference is successful only after receipt validation and
                # its inbound write have both completed.
                if info['Reference'] in processed_refs:
                    duplicate_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Duplicate inbound: "
                        f"receipt {p.name} untuk Reference {info['Reference']} "
                        "sudah diproses pada run ini.",
                        p.name,
                        p.id
                    ))
                    continue

                try:
                    locked_outcome = None
                    success_result = None
                    with self.env.cr.savepoint():
                        # This parameterized row lock prevents concurrent workers
                        # from validating the same receipt simultaneously.
                        self.env.cr.execute(
                            "SELECT state FROM stock_picking "
                            "WHERE id = %s FOR UPDATE",
                            [p.id]
                        )
                        locked_row = self.env.cr.fetchone()
                        if not locked_row:
                            raise ValidationError(_(
                                "Receipt for Reference %(reference)s no longer "
                                "exists.",
                                reference=info['Reference'],
                            ))

                        locked_state = locked_row[0]
                        p.invalidate_recordset()

                        if locked_state == 'done':
                            locked_outcome = (
                                'duplicate',
                                f"<b>Executed at {local_dt}:</b><br>Duplicate "
                                f"inbound: receipt {p.name} untuk Reference "
                                f"{info['Reference']} sudah berstatus done.",
                                p.name,
                                p.id,
                            )
                        elif locked_state == 'cancel':
                            locked_outcome = (
                                'failed',
                                f"<b>Executed at {local_dt}:</b><br>Receipt "
                                f"{p.name} untuk Reference {info['Reference']} "
                                "berstatus cancel dan tidak dapat diproses.",
                                p.name,
                                p.id,
                            )
                        else:
                            p.update({
                                'scheduled_date': _dt.combine(
                                    info['DocDate'], _time_of_day(14)
                                ),
                                'snk_create_date': _dt.combine(
                                    info['DocDate'], _time_of_day(14)
                                ),
                                'snk_is_pos': True,
                                'snk_inbound_id': rid
                            })
                            for line in p.move_ids:
                                match = next((
                                    values for values in ldat
                                    if values['product_id']
                                    == line.product_id.id
                                ), None)
                                if match:
                                    line.update({
                                        'quantity': match['product_uom_qty']
                                    })

                            p.move_ids.filtered(
                                lambda move: move.quantity and not move.picked
                            ).picked = True
                            p.with_context(
                                skip_backorder=True
                            ).button_validate()
                            p.invalidate_recordset(['state'])
                            if p.state != 'done':
                                raise ValidationError(_(
                                    "Receipt %(picking)s was not validated; "
                                    "final state: %(state)s.",
                                    picking=p.display_name,
                                    state=p.state,
                                ))

                            success_result = _mark_inbound_done(rid, p)

                    if locked_outcome:
                        outcome_type, msg, output, ref_id = locked_outcome
                        if outcome_type == 'duplicate':
                            duplicate_updates.append((
                                rid, msg, output, ref_id
                            ))
                        else:
                            failed_updates.append((rid, msg, output, ref_id))
                            failed_count += 1
                        continue

                    # Python containers are not transactional. Update them only
                    # after the savepoint has exited successfully.
                    successful_results.append(success_result)
                    processed_refs.add(info['Reference'])
                except Exception as ex3:
                    failed_updates.append((
                        rid,
                        f"<b>Executed at {local_dt}:</b><br>Receipt processing "
                        "failed and all receipt changes were rolled back: "
                        f"{Markup.escape(str(ex3))}",
                        p.name,
                        p.id,
                    ))
                    failed_count += 1

            batch_elapsed = _time.time() - batch_start
            _logger.info("TI Batch chunk finished in %.2f sec", batch_elapsed)

        # -----------------------
        # Step E: persist non-success outcomes in the outer transaction
        # -----------------------
        for rec_id, log, output, ref_id in duplicate_updates:
            self.browse(rec_id).exists().write({
                'state': 'duplicate',
                'log_info': log,
                'output': output,
                'ref_id': ref_id,
                'ref_model': RefModel,
            })

        for rec_id, msg, output, ref_id in failed_updates:
            self.browse(rec_id).exists().write({
                'state': 'failed',
                'log_info': msg,
                'output': output,
                'ref_id': ref_id,
                'ref_model': RefModel if ref_id else False,
            })

        total_elapsed = _time.time() - start_total
        final_dt = fields.Datetime.context_timestamp(self, _dt.now()).replace(microsecond=0, tzinfo=None)
        _logger.info(
            "Executed at %s: Process TI completed: %d processed, failed=%d, duplicates=%d, elapsed=%.2f sec",
            final_dt, len(successful_results), failed_count,
            len(duplicate_updates), total_elapsed
        )

        return True

    def action_direct_process(self):
        _logger.info(">>> Starting direct process for inbound records...")
        for record in self:
            if record.state != 'pending':
                raise UserError("Record is not in pending state. Please process pending records first.")

            if record.ref_data == 'TO':
                record.process_TO()
            elif record.ref_data == 'TI':
                record.process_TI()
            elif record.ref_data == 'SALES':
                record.process_sales()
            elif record.ref_data == 'ORDER':
                record.process_order()

    def action_direct_cancel(self):
        for record in self:
            if record.state != 'pending':
                continue
            record.write({'state': 'cancel'})

    def action_direct_retry(self):
        for record in self:
            _logger.info(">>> Retrying inbound ID: %s, current state: %s", record.id, record.state)
            if record.state != 'failed' and record.state != 'cancel' and record.state != 'duplicate':
                continue
            record.write({'state': 'pending'})
