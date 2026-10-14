# -*- coding: utf-8 -*-
from odoo import api, fields, models, _
from odoo.exceptions import ValidationError

class SnkPiOutboundConfirmWizard(models.TransientModel):
    _name = 'snk.pi.outbound.confirm.wizard'
    _description = 'Confirm Duplicate Outbound'

    outbound_ids = fields.Many2many('snk.pi.outbound', string='Outbound Records')
    duplicate_info = fields.Text(string='Duplicate Info', readonly=True)

    def action_confirm_process(self):
        """ Continue processing despite duplicates. """
        return self.outbound_ids.action_direct_process(bypass_confirm=True)


class SnkPiOutboundResendWizard(models.TransientModel):
    _name = 'snk.pi.outbound.resend.wizard'
    _description = 'Confirm Outbound Resend'

    outbound_ids = fields.Many2many(
        'snk.pi.outbound',
        string='Outbound Records',
        required=True,
    )
    reason = fields.Text(string='Resend Reason', required=True)

    def action_confirm_resend(self):
        """Resend only after a reason and explicit user confirmation."""
        self.ensure_one()
        reason = (self.reason or '').strip()
        if not reason:
            raise ValidationError(_("A resend reason is required."))
        return self.outbound_ids.action_confirm_resend(reason)
