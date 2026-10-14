from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError



class SnkSiteHistory(models.Model):
    _name = "snk.site.history"
    _description = "Site Histories"
    _order = "datetime desc"

    name = fields.Char("History", compute="_compute_name", store=True)
    site_id = fields.Many2one("snk.site","Site", store=True)
    user = fields.Many2one("res.users", string="User")
    action = fields.Selection([
        ("created", "Created"),
        ("opened", "Opened"),
        ("reopened", "Reopened"),
        ("closed", "Closed"),
    ], string="Action", required=True)
    datetime = fields.Datetime(
        "Timestamp",
        default=lambda
        self: fields.Datetime.context_timestamp(self, fields.Datetime.now()).replace(tzinfo=None)
    )

    # Actions
    @api.model_create_multi
    def create(self, vals_list):
        records = super(SnkSiteHistory, self).create(vals_list)
        for record in records:
            record._compute_name()
        return records

    # Computed Fields
    @api.depends("site_id", "user", "action", "datetime")
    def _compute_name(self):
        for rec in self:
            if not rec.name:
                rec.name = f"{rec.site_id.snk_code if rec.site_id else ''} is {rec.action} by {rec.user.name if rec.user else ''} on {rec.datetime.strftime('%Y-%m-%d %H:%M:%S') if rec.datetime else ''}"
            else:
                rec.name = f"{rec.site_id.snk_code if rec.site_id else ''} {rec.name} and {rec.action} by {rec.user.name if rec.user else ''} on {rec.datetime.strftime('%Y-%m-%d %H:%M:%S')  if rec.datetime else ''}"
                

    # def unlink(self):
    #     for record in self:
    #         if record.state == 'close':
    #             raise ValidationError(_("Cannot delete a history record with state 'Close'."))
    #     return super().unlink()



    # def unlink(self):
    #     for record in self:
    #         site_count = self.env['snk.site'].search_count([('grade_id', '=', record.id)])
    #         if site_count > 0:
    #             raise ValidationError(
    #                 _("Cannot delete grade '%s' because it is used in %s site(s).") % (record.snk_name, site_count)
    #             )
    #     return super().unlink()