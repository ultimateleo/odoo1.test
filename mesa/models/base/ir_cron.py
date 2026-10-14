# -*- coding: utf-8 -*-
from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)

class IrCron(models.Model):
    _inherit = 'ir.cron'

    # snk_process_type = fields.Selection([('transfer_in', 'Transfer In'),
    #                                      ('transfer_out', 'Transfer Out'),
    #                                      ('pos_order', 'POS Order'),
    #                                      ('pos_sales', 'POS Sales')], string="Process Type", default=False)

    is_snk_pi_inbound = fields.Boolean(
        string="Is SNK PI Inbound",
        compute="_compute_is_snk_pi_inbound",
        store=False
    )

    @api.depends("model_id")
    def _compute_is_snk_pi_inbound(self):
        for rec in self:
            rec.is_snk_pi_inbound = rec.model_id.model == "snk.pi.inbound"

    def method_direct_trigger(self):
        """
        Override cron entry point.
        - Kalau cron sudah punya code action_process(...) → biarkan Odoo eksekusi langsung.
        - Kalau tidak, baru pakai fallback logic.
        """
        for cron in self:
            if cron.model_id.model != "snk.pi.inbound":
                return super().method_direct_trigger()

            # 🚨 cek apakah cron.code berisi pemanggilan langsung
            if cron.code and "action_process" in cron.code:
                _logger.debug("Bypass fallback, jalankan code langsung: %s", cron.code)
                return super().method_direct_trigger()

            # --- fallback logic ---
            mapping = {
                "transfer_in": "TI",
                "transfer_out": "TO",
                "pos_sales": "SALES",
                "pos_order": "ORDER",
            }

            process_type = getattr(cron, "process_type", None) or False
            workers = getattr(cron, "download_workers", None) or 5

            try:
                ref_type = mapping.get(process_type, process_type) if process_type else False
                pending_records = (
                    self.env["snk.pi.inbound"].search(
                        [("state", "=", "pending"), ("ref_data", "=", ref_type)]
                    )
                    if process_type
                    else self.env["snk.pi.inbound"].search([("state", "=", "pending")])
                )

                if process_type == "pos_order":
                    if pending_records:
                        self.env["snk.pi.inbound"].action_process(process_type="pos_order")
                    else:
                        _logger.info("No pending POS Order, fallback to POS Sales")
                        self.env["snk.pi.inbound"].action_process(process_type="pos_sales")

                elif process_type == "pos_sales":
                    if pending_records:
                        self.env["snk.pi.inbound"].action_process(process_type="pos_sales")
                    else:
                        _logger.info("No pending POS Sales, fallback to Synchronize SFTP")
                        self.env["snk.pi.inbound"].synchronize_sftp(download_workers=workers)

                elif process_type:
                    self.env["snk.pi.inbound"].action_process(process_type=process_type)

                else:
                    self.env["snk.pi.inbound"].synchronize_sftp(download_workers=workers)
                    _logger.warning(">>>>>> masuk nih bos")
            except Exception as e:
                _logger.exception(
                    "Error executing cron for process_type=%s: %s", process_type, e
                )





# -*- coding: utf-8 -*-
# from odoo import models, fields, api, SUPERUSER_ID
# import odoo
# from datetime import timedelta
# import pytz
# import logging

# _logger = logging.getLogger(__name__)


# Cron tanpa atur timezone server
# class IrCron(models.Model):
#     _inherit = 'ir.cron'

#     snk_process_type = fields.Selection([
#         ('transfer_in', 'Transfer In'),
#         ('transfer_out', 'Transfer Out'),
#         ('pos_order', 'POS Order'),
#         ('pos_sales', 'POS Sales')
#     ], string="Process Type", default=False)

#     is_snk_pi_inbound = fields.Boolean(
#         string="Is SNK PI Inbound",
#         compute="_compute_is_snk_pi_inbound",
#         store=False
#     )

#     # 🔹 field helper: menampilkan nextcall sesuai timezone scheduler user
#     nextcall_user = fields.Datetime(
#         string="Next Execution (User TZ)",
#         compute="_compute_nextcall_user",
#         inverse="_inverse_nextcall_user",
#         store=False
#     )

#     @api.depends("model_id")
#     def _compute_is_snk_pi_inbound(self):
#         for rec in self:
#             rec.is_snk_pi_inbound = rec.model_id.model == "snk.pi.inbound"

#     # 🔹 tampilkan nextcall sesuai timezone user_id
#     def _compute_nextcall_user(self):
#         for cron in self:
#             if not cron.nextcall:
#                 cron.nextcall_user = False
#                 continue
#             user_tz = cron.user_id.tz or 'UTC'
#             tz = pytz.timezone(user_tz)
#             utc_dt = fields.Datetime.from_string(cron.nextcall)
#             local_dt = pytz.UTC.localize(utc_dt).astimezone(tz)
#             cron.nextcall_user = fields.Datetime.to_string(local_dt)

#     # 🔹 kalau user update nextcall_user → convert ke UTC untuk disimpan
#     def _inverse_nextcall_user(self):
#         for cron in self:
#             if not cron.nextcall_user:
#                 continue
#             user_tz = cron.user_id.tz or 'UTC'
#             tz = pytz.timezone(user_tz)
#             local_dt = fields.Datetime.from_string(cron.nextcall_user)
#             if local_dt.tzinfo is None:
#                 local_dt = tz.localize(local_dt)
#             utc_dt = local_dt.astimezone(pytz.UTC).replace(tzinfo=None)
#             cron.nextcall = fields.Datetime.to_string(utc_dt)

#     # 🔹 override method_direct_trigger biar bisa logging sesuai user timezone
#     def method_direct_trigger(self):
#         for cron in self:
#             user_tz = cron.user_id.tz or 'UTC'
#             tz = pytz.timezone(user_tz)

#             utc_now = fields.Datetime.now()
#             local_now = pytz.UTC.localize(utc_now).astimezone(tz)

#             _logger.info(
#                 "[CRON %s] Run at %s %s (user=%s, tz=%s)",
#                 cron.name,
#                 local_now.strftime("%Y-%m-%d %H:%M:%S"),
#                 user_tz,
#                 cron.user_id.name,
#                 user_tz,
#             )

#             if cron.model_id.model == "snk.pi.inbound" and cron.snk_process_type:
#                 process_type = cron.snk_process_type
#                 self.env["snk.pi.inbound"].action_process(process_type=process_type)
#             else:
#                 super().method_direct_trigger()

#     # 🔹 optional: override _process_job untuk hitung nextcall sesuai user tz
#     @classmethod
#     def _process_job(cls, db, cron_cr, job):
#         # Jangan pakai api.Environment.manage() lagi
#         with cron_cr.savepoint():
#             env = api.Environment(cron_cr, SUPERUSER_ID, {})
#             cron = env[cls._name].browse(job["cron_id"])

#             if not cron:
#                 return super()._process_job(db, cron_cr, job)

#             # jalankan default Odoo
#             res = super()._process_job(db, cron_cr, job)

#             # update nextcall sesuai timezone user kalau ada
#             if cron.user_id.tz:
#                 try:
#                     user_tz = pytz.timezone(cron.user_id.tz)
#                     lastcall_utc = fields.Datetime.now()
#                     cron.lastcall = fields.Datetime.to_string(lastcall_utc)

#                     local_lastcall = pytz.UTC.localize(lastcall_utc).astimezone(user_tz)

#                     if cron.interval_type == "minutes":
#                         local_nextcall = local_lastcall + timedelta(minutes=cron.interval_number)
#                     elif cron.interval_type == "hours":
#                         local_nextcall = local_lastcall + timedelta(hours=cron.interval_number)
#                     elif cron.interval_type == "days":
#                         local_nextcall = local_lastcall + timedelta(days=cron.interval_number)
#                     elif cron.interval_type == "weeks":
#                         local_nextcall = local_lastcall + timedelta(weeks=cron.interval_number)
#                     else:
#                         local_nextcall = local_lastcall

#                     nextcall_utc = local_nextcall.astimezone(pytz.UTC).replace(tzinfo=None)
#                     cron.nextcall = fields.Datetime.to_string(nextcall_utc)

#                 except Exception as e:
#                     _logger.warning("Gagal update nextcall untuk cron %s: %s", cron.name, e)

#             return res



