from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError



class SnkSiteGrade(models.Model):
    _name = "snk.site.grade"
    _description = "SNK Site Grade"
    _rec_name = 'snk_name'

    snk_name = fields.Char(string='Grade')
    snk_description = fields.Text(string='Description')

    _sql_constraints = [
        ('unique_grade_name', 'unique(snk_name)', 'Grade name must be unique!')
    ]



    # site itu modelnya yang mana pak na? 
    def unlink(self):
        for record in self:
            site_count = self.env['snk.site'].search_count([('snk_grade_id', '=', record.id)])
            if site_count > 0:
                raise ValidationError(
                    _("Cannot delete grade '%s' because it is used in %s site(s).") % (record.snk_name, site_count)
                )
        return super().unlink()

   