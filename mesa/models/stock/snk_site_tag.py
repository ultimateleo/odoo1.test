from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError



class SnkSiteTag(models.Model):
    _name = "snk.site.tag"
    _description = "SNK Site Tag"
    _rec_name = 'snk_name'

    snk_name = fields.Char(string='Tag')
    parent_id = fields.Many2one('snk.site.tag', string='Parent Tag')
    
    def unlink(self):
        for record in self:
            site_count = self.env['snk.site'].search_count([
                ('snk_tag_ids', 'in', record.id)
            ])
            if site_count > 0:
                raise ValidationError(
                    _("Cannot delete tag '%s' because it is used in %s site(s).") % (record.snk_name, site_count)
                )
        return super(SnkSiteTag, self).unlink()