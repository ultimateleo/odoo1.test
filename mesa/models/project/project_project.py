from odoo import _, api, fields, models


class Project(models.Model):
    _inherit = "project.project"

    snk_event = fields.Boolean("is Event", related='account_id.snk_event')