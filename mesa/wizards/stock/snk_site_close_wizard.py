from odoo import models, fields, api


class SnkSiteCloseWizard(models.TransientModel):
    _name = 'snk.site.close.wizard'
    _description = 'Close Site Wizard'

    name = fields.Char(string='Reason', required=True)
    site_ids = fields.Many2many('snk.site', string='Sites', required=True)
    action = fields.Selection([
        ("created", "Created"),
        ("opened", "Opened"),
        ("reopened", "Reopened"),
        ("closed", "Closed"),
    ], string="Action", default='closed')
    datetime = fields.Datetime(
        "Timestamp",
        default=lambda
        self: fields.Datetime.context_timestamp(
            self, fields.Datetime.now()).replace(tzinfo=None)
    )

    def action_confirm(self):
        for rec in self.site_ids:
            rec.write({'snk_state': 'close'})
            self.env['snk.site.history'].sudo().create({
                'name': self.name,
                'site_id': rec.id,
                'user': self.env.user.id,
                'action': self.action
            })
        return {'type': 'ir.actions.act_window_close'}
