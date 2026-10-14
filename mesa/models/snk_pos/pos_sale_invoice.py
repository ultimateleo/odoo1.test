import logging
import time as t
from datetime import date, datetime

from odoo import models, fields, _, api
# from odoo.addons.queue_job.job import job
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)


class SnkPosSale(models.Model):
    _name = "snk.pos.sale"
    _inherit = "snk.pos.sale"

    invoice_in_queue = fields.Boolean(
        string="Invoice in Queue",
        default=False,
        help="Indicates that the invoice is currently being processed in the queue."
    )

    def _cancel_invoice_flag(self):
        self.write({
            'invoice_in_queue': False,
            'invoice_status': 'no',
        })
    def _reset_invoice_flag(self):
        self.write({
            'invoice_in_queue': False,
            'account_move_id': False,
            'invoice_status': 'no',
        })

    # --- === UI BUTTON === ---
    def action_create_invoice_queue(self):
        """Button handler yang enqueue job ke background queue.
        UI hanya enqueue — semua pembuatan invoice dilakukan di job background.
        """
        sale = self.browse(self.id).exists()
        if not sale:
            return
        self.ensure_one()
        if not self.invoice_in_queue:
            user_id = self.env.user.id
            today = fields.Date.context_today(self)
            # hanya enqueue, jangan buat invoice di UI
            job = self.with_delay(
                priority=10,
                channel="invoice_queue",
                description=f"Proses invoice {self.name}",
            ).action_create_invoice_job(self.id, user_id, today, batch_size=1000)

            # set flag UI supaya tombol langsung hilang
            self.write({'invoice_in_queue': True, 'invoice_status': 'to invoice'})
            
            _logger.info("[ENQUEUE] Create invoice job queued for sale %s (job UUID: %s)",
                        sale.display_name, getattr(job, "uuid", None))
            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'title': 'Invoice Creation Started',
                    'message': 'Invoice creation for "%s" has been queued in background. You can continue working.' % self.display_name,
                    'type': 'success',
                    'sticky': False,
                }
            )
        else:
            _logger.exception(">>> Invoice already in queue.")
            raise UserError(f"Invoice for {self.display_name} has been queued in background. Just enjoy your coffee.")
        # return {
        #     'type': 'ir.actions.client',
        #     'tag': 'reload',   # <--- form reload langsung
        # }
    
    # --- === BACKGROUND JOB === ---
    # @job
    def action_create_invoice_job(self, sale_id, user_id, today, batch_size=5000):
        """Background job: buat invoice (account.move) dan move lines secara batch."""
        # Ambil sale record (gunakan sale untuk semua referensi)
        sale = self.browse(sale_id).exists()
        if not sale:
            _logger.warning("Job called but sale %s not found", sale_id)
            return

        # Pastikan operate dengan sudo untuk menghindari permission issue pada worker
        sale = sale.sudo()
        target_user = self.env['res.users'].browse(user_id)
        target_partner = target_user.partner_id

        # Log only after we have the real sale record
        _logger.info(">>> JOB START for sale %s (id=%s)", sale.display_name, sale.id)

        # Validasi
        if not sale.customer_id or not sale.journal_id:
            # Kirim notif error ke user dan raise agar queue_job bisa menandai gagal
            msg = "Customer and Journal must be set before creating the invoice."
            _logger.error(msg + " Sale: %s", sale.display_name)
            # optional: notify user via bus
            try:
                self.env['bus.bus']._sendone(target_partner.id, 'simple_notification', {
                    'title': 'Invoice Creation Failed',
                    'message': msg,
                    'type': 'danger',
                })
            except Exception:
                _logger.exception("Failed sending failure notification")
            raise UserError(msg)

        if not sale.order_lines:
            msg = "No order lines available to create the invoice."
            _logger.error(msg + " Sale: %s", sale.display_name)
            try:
                self.env['bus.bus']._sendone(target_partner.id, 'simple_notification', {
                    'title': 'Invoice Creation Failed',
                    'message': msg,
                    'type': 'warning',
                })
            except Exception:
                _logger.exception("Failed sending failure notification")
            raise UserError(msg)

        start_time = t.perf_counter()

        # Create the account.move (invoice) here in the job
        move_vals = {
            'move_type': 'out_invoice',
            'journal_id': sale.journal_id.id,
            'partner_id': sale.customer_id.id,
            'invoice_date': sale.end_date or today,
            'invoice_date_due': sale.end_date or today,
            'date': today,
            'invoice_origin': sale.name or False,
        }
        # _logger.info("Invocie date %s", move_vals['invoice_date'])
        # _logger.info("Invocie date %s", today)
        move_obj = self.env['account.move'].sudo()
        move = move_obj.create(move_vals)
        _logger.info("[JOB] Created invoice move id=%s (name=%s) for sale %s", move.id, move.name, sale.display_name)

        # Link invoice to sale (done in job)
        try:
            sale.write({'account_move_id': move.id})
            # sale.write({'account_move_ids': (6,0,move.id)})
            move.write({'pos_sale_id': sale.id})
        except Exception:
            _logger.exception("Failed to link account.move to sale %s", sale.display_name)

        # Prefetch fields for performance (read on order_lines)
        sale.order_lines.read([
            'product_id', 'product_uom_qty', 'description',
            'product_uom', 'price_unit', 'tax_id',
            'price_subtotal', 'price_total',
        ])

        move_line_obj = self.env['account.move.line'].sudo()
        lines_to_create = []
        total_created = 0

        try:
            for idx, line in enumerate(sale.order_lines, start=1):
                batch_start_time = t.perf_counter()
                qty = float(line.product_uom_qty or 0.0)
                net_price_unit = float(line.price_unit or 0.0)
                discount = float(line.discount or 0.0) # tambahin ini vin
                if line.discount_amount:
                    discount = 0.0
                    discount_amount = line.discount_amount
                    net_price_unit = line.price_unit - discount_amount

                vals = {
                    'move_id': move.id,
                    'product_id': line.product_id.id or False,
                    'quantity': qty,
                    'name': line.description or (line.product_id.display_name if line.product_id else ''),
                    'product_uom_id': line.product_uom.id if line.product_uom else False,
                    'price_unit': net_price_unit,
                    'discount': discount,
                    # many2many field on move line depends on version; use 'tax_ids' or 'tax_id' accordingly
                    'tax_ids': [(6, 0, line.tax_id.ids)] if line.tax_id else [(6, 0, [])],
                }
                lines_to_create.append(vals)

                if idx % int(batch_size) == 0:
                    move_line_obj.create(lines_to_create)
                    total_created += len(lines_to_create)
                    elapsed = t.perf_counter() - batch_start_time
                    _logger.info("[JOB BATCH] Created %d lines (elapsed %.2fs)", total_created, elapsed)
                    lines_to_create.clear()
                    # batch_start_time = t.perf_counter()

            # last remainder
            if lines_to_create:
                inv = move_line_obj.create(lines_to_create)
                total_created += len(lines_to_create)
                elapsed = t.perf_counter() - batch_start_time
                if inv:
                    _logger.info("[JOB FINAL BATCH] Created %d lines (elapsed %.2fs)", len(lines_to_create), elapsed)
                    self.write({
                        'invoice_in_queue': False
                    })

            total_time = t.perf_counter() - start_time
            _logger.info("[JOB COMPLETE] Sale %s → Invoice %s (%d lines in %.2fs)",
                         sale.display_name, move.name, total_created, total_time)

            # Recompute invoice totals / post process if needed
            try:
                move._recompute_dynamic_lines()  # if your version has such method; otherwise call button_compute_taxes or _onchange methods as needed
            except Exception:
                # ignore if not present
                pass

            # Send success notification to user who triggered
            try:
                # url = f"/web#id={move.id}&model=account.move&view_type=form"
                self.env['bus.bus']._sendone(
                    target_partner.id,
                    'simple_notification',
                    {
                        'title': 'Invoice Created',
                        'message':
                            f'Invoice {move.name} has been successfully created for {sale.display_name}.',
                        #  'message':
                        #     f"Invoice <a href='{url}' target='_blank'>{move.name}</a> "
                        #     f"has been successfully created for {sale.display_name}.",   
                        'type': 'success',
                        'sticky': True
                    }
                )
                _logger.info("Success sending notification to %s", target_partner.id)
            except Exception:
                _logger.exception("Failed sending success notification")

        except Exception as exc:
            _logger.exception("Exception while processing invoice creation job for sale %s", sale.display_name)
            # send failure notice
            try:
                self.write({
                    'invoice_in_queue': False,
                })
                self.env['bus.bus']._sendone(target_partner.id, 'simple_notification', {
                    'title': 'Invoice Creation Failed',
                    'message': f'Failed to create invoice for {sale.display_name}: {exc}',
                    'type': 'danger',
                })
            except Exception:
                _logger.exception("Failed sending failure notification on exception")
            # Re-raise to let queue_job mark job as failed and apply retries if configured
            raise

        return True


    def test_notification(self):
        target_user = self.env.user
        target_partner = target_user.partner_id
        url = f"/web#id={target_partner.id}&model=res.partner&view_type=form"
        try:
            self.env['bus.bus']._sendone(
                self.env.user.partner_id,
                'simple_notification',
                {
                    'title': 'Invoice Created',
                    'message':
                        f"Invoice <a href='{url}' target='_blank'> {target_user.name} </a> "
                        f"has been successfully created for {target_partner.name}.",
                    'type': 'success',
                    'sticky': True,
                }
            )
            # return {
            #     'type': 'ir.actions.client',
            #     'tag': 'display_notification',
            #     'params':
            #         {
            #             'title': 'Invoice Created',
            #             'message':
            #                 f"Invoice <a href='{url}' target='_blank'> {target_user.name} </a> "
            #                 f"has been successfully created for {target_partner.name}.",
            #             'type': 'success',
            #             'sticky': True,
            #             'html': True
            #         }
            # }
            _logger.info("Success send")
        except:
            _logger.exception("Gagal send")

