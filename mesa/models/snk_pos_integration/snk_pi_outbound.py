# -*- coding: utf-8 -*-
from odoo import api, Command, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from markupsafe import Markup
from datetime import datetime

import os
import paramiko
import logging

_logger = logging.getLogger(__name__)

MODEL_TYPE = [
    ('none', ''),
    ('brand', 'Brand'),
    ('product_category', 'Product Categorie'),
    ('product_attribute', 'Product Attribute'),
    ('product', 'Product'),
    ('barcode', 'Barcode'),
    ('store', 'Store'),
    ('price', 'Price'),
    ('transfer', 'Transfer'),
]


class SnkPiOutbound(models.Model):
    _name = "snk.pi.outbound"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _description = "POS Integration Outbound"
    _order = 'id desc'

    active = fields.Boolean(default=True, tracking=90)
    name = fields.Char("Ref", tracking=10)
    origin = fields.Char("Origin Data", tracking=50)
    brand = fields.Char("Brand", tracking=60)
    data = fields.Text(string="Data XML")
    ref_data = fields.Selection(MODEL_TYPE, "Ref Data", tracking=20)
    ref_model = fields.Char("Ref Model", tracking=30)
    ref_id = fields.Integer("Ref ID", tracking=40)
    state = fields.Selection([
        ('pending', 'Pending'),
        ('done', 'Done'),
        ('failed', 'Failed'),
        ('duplicated', 'Duplicated'),
        ('cancel', 'Canceled'),
    ], string='Status Integration', default='pending')
    log_info = fields.Html(string="Log Info")
    resend_count = fields.Integer(
        string="Resend Count", readonly=True, default=0, tracking=70
    )
    last_resend_at = fields.Datetime(
        string="Last Resend At", readonly=True, tracking=80
    )
    last_resend_uid = fields.Many2one(
        'res.users',
        string="Last Resend By",
        readonly=True,
        tracking=85,
    )
    last_resend_reason = fields.Text(
        string="Last Resend Reason",
        readonly=True,
        tracking=75,
    )

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

    def FindOrCreate(self, vals):
        name = vals['name']
        ref_data = vals['ref_data']
        ref_model = vals['ref_model']
        ref_id = vals['ref_id']
        # Search dan create harus memakai environment yang sama. Method ini dipanggil
        # setelah validasi picking, sehingga sudo dipertahankan dari behavior existing
        # agar user operasional tidak membutuhkan create access ke integration queue.
        # Outbound generation can be high-volume. Avoid a generic creation
        # message/follower per queue row while retaining later field tracking.
        OutboundModel = self.env['snk.pi.outbound'].sudo().with_context(
            mail_create_nolog=True,
            mail_create_nosubscribe=True,
        )
        OutboundData = OutboundModel.search(
            [
                ('name', '=', name),
                ('ref_data', '=', ref_data),
                ('ref_model', '=', ref_model),
                ('ref_id', '=', ref_id),
            ], limit=1)
        if not OutboundData:
            OutboundData = OutboundModel.create(vals)
        return OutboundData

    def action_opening_balance(self):
        self.env['product.category'].outbound_opening_balance()
        self.env['res.partner'].outbound_opening_balance()
        self.env['product.pricelist'].outbound_opening_balance()
        self.env['product.attribute'].outbound_opening_balance()
        self.env['product.product'].outbound_opening_balance()

    def action_process(self, type = False, brand = False):
        if type:
            if type == 'product_category' or type == 1:
                self.env['product.category'].outbound_process()
            elif type == 'product_attribute' or type == 2:
                self.env['product.attribute'].outbound_process()
            elif type == 'product' or type == 3:
                self.env['product.product'].outbound_process(brand)
            elif type =='price' or type == 4:
                self.env['product.pricelist.item'].outbound_process(brand)
            elif type == 'store' or type == 5:
                self.env['snk.site'].outbound_process(brand)
        else:
            self.env['product.category'].outbound_process()
            self.env['product.attribute'].outbound_process()
            self.env['product.product'].outbound_process(brand)
            self.env['product.pricelist.item'].outbound_process(brand)
            self.env['snk.site'].outbound_process(brand)



    def action_synchronize(self, ref_data=False):
        Config = self.env['ir.config_parameter'].sudo()

        try:
            sftp_host = Config.get_param('mesa.snk_sftp_host')
            sftp_username = Config.get_param('mesa.snk_sftp_username')
            sftp_password = Config.get_param('mesa.snk_sftp_password')
            sftp_port = int(Config.get_param('mesa.snk_sftp_port'))
            sftp_path_source = Config.get_param('mesa.snk_sftp_path_local_outbound')
            sftp_path_outbound = Config.get_param('mesa.snk_sftp_path_outbound')

            transport = paramiko.Transport((sftp_host, sftp_port))
            transport.connect(username=sftp_username, password=sftp_password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            source_path = sftp_path_source
            remote_path = sftp_path_outbound

            os.makedirs(source_path, exist_ok=True)
            sftp.chdir(remote_path)

            domain = [('state', '=', 'pending')]
            if ref_data:
                domain.append(('ref_data', '=', ref_data))

            OutboundData = self.search(domain)
            for record in OutboundData:
                # Background Check: If origin already exists in 'done' state, mark as duplicated
                is_duplicate = self.search_count([
                    ('origin', '=', record.origin),
                    ('state', '=', 'done'),
                    ('id', '!=', record.id)
                ])
                if is_duplicate:
                    log_time = fields.Datetime.context_timestamp(record, fields.Datetime.now()).strftime('%Y/%m/%d %H:%M:%S')
                    log_info = Markup('<p><b>Skipped (Duplicated)</b> : %s</p>\n') % log_time
                    log_info += Markup('<ul><li>Data already exists in "Done" state.</li></ul>')
                    record.write({'state': 'duplicated', 'log_info': log_info})
                    continue

                record._process_to_sftp(sftp, source_path)

            sftp.close()
            transport.close()
        except Exception as e:
            raise UserError(e)

    def _process_to_sftp(self, sftp, source_path, is_resend=False, resend_reason=False):
        """ Core logic to process a record and send it to SFTP. """
        self.ensure_one()
        file_name = _("%(filename)s.xml" % {'filename': self.name})
        full_file_path = os.path.join(source_path, file_name)
        try:
            with open(full_file_path, "w") as file:
                file.write(self.data)
            sftp.put(full_file_path, file_name)
            if os.path.exists(full_file_path):
                os.remove(full_file_path)

            log_time = fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime('%Y/%m/%d %H:%M:%S')
            if is_resend:
                log_info = Markup(self.log_info or '')
                log_info += Markup('<p><b>Resent to SFTP</b> : %s</p>\n') % log_time
                log_info += Markup(
                    '<ul>'
                    '<li>User: %s</li>'
                    '<li>Reason: %s</li>'
                    '<li>Filename: %s</li>'
                    '<li>Result: Success</li>'
                    '</ul>'
                ) % (
                    self.env.user.display_name,
                    resend_reason,
                    file_name,
                )
                self.write({
                    'state': 'done',
                    'log_info': log_info,
                    'resend_count': self.resend_count + 1,
                    'last_resend_reason': resend_reason,
                    'last_resend_at': fields.Datetime.now(),
                    'last_resend_uid': self.env.user.id,
                })
            else:
                log_info = Markup(self.log_info or '')
                log_info += Markup('<p><b>Executed</b> : %s</p>\n') % log_time
                log_info += Markup('<ul><li>Data Integrated</li></ul>')
                self.write({'state': 'done', 'log_info': log_info})
            return True
        except Exception as e:
            if os.path.exists(full_file_path):
                os.remove(full_file_path)
            log_time = fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime('%Y/%m/%d %H:%M:%S')
            if is_resend:
                log_info = Markup(self.log_info or '')
                log_info += Markup('<p><b>Resend Exception</b> : %s</p>\n') % log_time
                log_info += Markup(
                    '<ul>'
                    '<li>User: %s</li>'
                    '<li>Reason: %s</li>'
                    '<li>Filename: %s</li>'
                    '<li>Result: Failure</li>'
                    '<li>Error: %s</li>'
                    '</ul>'
                ) % (
                    self.env.user.display_name,
                    resend_reason,
                    file_name,
                    str(e),
                )
                self.write({
                    'state': 'failed',
                    'log_info': log_info,
                    'resend_count': self.resend_count + 1,
                    'last_resend_reason': resend_reason,
                    'last_resend_at': fields.Datetime.now(),
                    'last_resend_uid': self.env.user.id,
                })
            else:
                log_info = Markup(self.log_info or '')
                log_info += Markup('<p><b>Exception</b> : %s</p>\n') % log_time
                log_info += Markup('<ul><li>%s</li></ul>', ) % _('`%(error)s`', error=str(e))
                self.write({'state': 'failed', 'log_info': log_info})
            return False

    def action_open_resend_wizard(self):
        """Open the explicit resend confirmation for completed outbound records."""
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise AccessError(_("Only Inventory Managers can resend outbound files."))
        if not self:
            raise UserError(_("Select at least one outbound record to resend."))

        invalid_records = self.filtered(lambda record: record.state != 'done')
        if invalid_records:
            invalid_names = ", ".join(invalid_records.mapped('display_name'))
            raise UserError(_(
                "Resend to SFTP only accepts records in Done state. "
                "Remove these records from the selection: %(records)s",
                records=invalid_names,
            ))

        return {
            'name': _('Confirm Resend to SFTP'),
            'type': 'ir.actions.act_window',
            'res_model': 'snk.pi.outbound.resend.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_outbound_ids': [Command.set(self.ids)],
            },
        }

    def action_confirm_resend(self, reason):
        """Resend the exact existing payload and filename after user confirmation."""
        if not self.env.user.has_group('stock.group_stock_manager'):
            raise AccessError(_("Only Inventory Managers can resend outbound files."))
        if not self:
            raise UserError(_("No outbound records were selected for resend."))

        reason = (reason or '').strip()
        if not reason:
            raise ValidationError(_("A resend reason is required."))

        self.invalidate_recordset(['state'])
        invalid_records = self.filtered(lambda record: record.state != 'done')
        if invalid_records:
            invalid_names = ", ".join(invalid_records.mapped('display_name'))
            raise UserError(_(
                "Resend was stopped because these records are no longer Done: "
                "%(records)s",
                records=invalid_names,
            ))

        Config = self.env['ir.config_parameter'].sudo()
        transport = False
        sftp = False
        try:
            sftp_host = Config.get_param('mesa.snk_sftp_host')
            sftp_username = Config.get_param('mesa.snk_sftp_username')
            sftp_password = Config.get_param('mesa.snk_sftp_password')
            sftp_port = int(Config.get_param('mesa.snk_sftp_port'))
            source_path = Config.get_param('mesa.snk_sftp_path_local_outbound')
            remote_path = Config.get_param('mesa.snk_sftp_path_outbound')

            transport = paramiko.Transport((sftp_host, sftp_port))
            transport.connect(username=sftp_username, password=sftp_password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            os.makedirs(source_path, exist_ok=True)
            sftp.chdir(remote_path)

            success_count = 0
            failed_count = 0
            skipped_count = 0
            for record in self:
                # Re-read once more immediately before the external side effect.
                record.invalidate_recordset(['state'])
                if record.state != 'done':
                    skipped_count += 1
                    continue

                if record._process_to_sftp(
                    sftp,
                    source_path,
                    is_resend=True,
                    resend_reason=reason,
                ):
                    success_count += 1
                else:
                    failed_count += 1
        except Exception as e:
            raise UserError(_("Unable to start outbound resend: %(error)s", error=str(e)))
        finally:
            if sftp:
                try:
                    sftp.close()
                except Exception:
                    _logger.exception("Failed to close SFTP client after outbound resend.")
            if transport:
                try:
                    transport.close()
                except Exception:
                    _logger.exception("Failed to close SFTP transport after outbound resend.")

        notification_type = 'success' if not failed_count and not skipped_count else 'warning'
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Outbound Resend Completed'),
                'message': _(
                    "Success: %(success)s, Failed: %(failed)s, Skipped: %(skipped)s.",
                    success=success_count,
                    failed=failed_count,
                    skipped=skipped_count,
                ),
                'type': notification_type,
                'sticky': bool(failed_count or skipped_count),
                'next': {
                    'type': 'ir.actions.act_window_close',
                },
            },
        }


    def action_direct_process(self, bypass_confirm=False):
        pending_records = self.filtered(lambda record: record.state == 'pending')

        # Stop on the highest-priority non-pending state. A mixed selection must
        # never silently skip records and continue sending the pending subset.
        state_guards = [
            (
                'done',
                _('Outbound Already Done'),
                _(
                    "%(count)s selected outbound record(s) are already Done. "
                    "Deselect them before running Process.",
                    count=len(self.filtered(lambda record: record.state == 'done')),
                ),
                'info',
            ),
            (
                'failed',
                _('Failed Outbound Selected'),
                _(
                    "%(count)s selected outbound record(s) are Failed. "
                    "Run Retry first; Process only accepts Pending records.",
                    count=len(self.filtered(lambda record: record.state == 'failed')),
                ),
                'warning',
            ),
            (
                'duplicated',
                _('Duplicated Outbound Selected'),
                _(
                    "%(count)s selected outbound record(s) are Duplicated. "
                    "Run Retry first; Process only accepts Pending records.",
                    count=len(self.filtered(lambda record: record.state == 'duplicated')),
                ),
                'warning',
            ),
            (
                'cancel',
                _('Canceled Outbound Selected'),
                _(
                    "%(count)s selected outbound record(s) are Canceled. "
                    "Run Retry first; Process only accepts Pending records.",
                    count=len(self.filtered(lambda record: record.state == 'cancel')),
                ),
                'warning',
            ),
        ]

        for state, title, message, notification_type in state_guards:
            if any(record.state == state for record in self):
                return {
                    'type': 'ir.actions.client',
                    'tag': 'display_notification',
                    'params': {
                        'title': title,
                        'message': message,
                        'type': notification_type,
                        'sticky': False,
                    },
                }

        if not pending_records:
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': _('Nothing to Process'),
                    'message': _('No Pending outbound records were selected.'),
                    'type': 'warning',
                    'sticky': False,
                },
            }

        # 1. Manual Check: If not bypassing, check for duplicate origins
        if not bypass_confirm:
            # Check for records that are ALREADY done in DB
            duplicates = self.env['snk.pi.outbound'].search([
                ('origin', 'in', pending_records.mapped('origin')),
                ('state', '=', 'done'),
            ])

            # Check for duplicates WITHIN the selection
            origins = [origin for origin in pending_records.mapped('origin') if origin]
            internal_duplicates = [o for o in set(origins) if origins.count(o) > 1]

            if duplicates or internal_duplicates:
                duplicate_origins = list(set(duplicates.mapped('origin')) | set(internal_duplicates))
                return {
                    'name': _('Confirmation: Duplicates Found'),
                    'type': 'ir.actions.act_window',
                    'res_model': 'snk.pi.outbound.confirm.wizard',
                    'view_mode': 'form',
                    'target': 'new',
                    'context': {
                        'default_outbound_ids': [(6, 0, pending_records.ids)],
                        'default_duplicate_info': _("The following origins have a successful integration (DONE) or multiple pending entries:\n\n%s") % "\n".join(duplicate_origins),
                    }
                }

        # 2. Proceed with integration
        Config = self.env['ir.config_parameter'].sudo()
        try:
            sftp_host = Config.get_param('mesa.snk_sftp_host')
            sftp_username = Config.get_param('mesa.snk_sftp_username')
            sftp_password = Config.get_param('mesa.snk_sftp_password')
            sftp_port = int(Config.get_param('mesa.snk_sftp_port'))
            sftp_path_source = Config.get_param('mesa.snk_sftp_path_local_outbound')
            sftp_path_outbound = Config.get_param('mesa.snk_sftp_path_outbound')

            transport = paramiko.Transport((sftp_host, sftp_port))
            transport.connect(username=sftp_username, password=sftp_password)
            sftp = paramiko.SFTPClient.from_transport(transport)

            source_path = sftp_path_source
            remote_path = sftp_path_outbound

            os.makedirs(source_path, exist_ok=True)
            sftp.chdir(remote_path)

            for record in pending_records:
                # State may change while the confirmation wizard is open or while
                # the SFTP connection is being established. Re-read it before send
                # so a concurrently completed record is never uploaded again.
                record.invalidate_recordset(['state'])
                if record.state != 'pending':
                    continue

                # Check for duplicates again inside the loop to catch ones just processed in this batch
                if not bypass_confirm:
                    is_duplicate = self.search_count([
                        ('origin', '=', record.origin),
                        ('state', '=', 'done'),
                        ('id', '!=', record.id)
                    ])
                    if is_duplicate:
                        log_time = fields.Datetime.context_timestamp(record, fields.Datetime.now()).strftime('%Y/%m/%d %H:%M:%S')
                        log_info = Markup('<p><b>Skipped (Duplicated)</b> : %s</p>\n') % log_time
                        log_info += Markup('<ul><li>Data already exists in "Done" state.</li></ul>')
                        record.write({'state': 'duplicated', 'log_info': log_info})
                        continue

                record._process_to_sftp(sftp, source_path)

            sftp.close()
            transport.close()
        except Exception as e:
            raise UserError(e)

    def action_direct_cancel(self):
        for record in self:
            if record.state not in ['pending']:
                continue
            record.write({'state': 'cancel'})

    def action_direct_retry(self):
        for record in self:
            if record.state not in ['failed', 'cancel', 'duplicated']:
                continue
            record.write({'state': 'pending'})
