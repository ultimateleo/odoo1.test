# -*- coding: utf-8 -*-
from odoo import api, fields, models, _


class SnkProductGrade(models.Model):
    _name = "snk.product.grade"
    _description = "Product Grade"
    _rec_name = 'display_name'

    snk_name = fields.Char('Name', required=True)
    display_name = fields.Char(store=True, compute="_compute_display_name")
    snk_description = fields.Char('Description')
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ('uniq_name', 'unique(snk_name)', 'You cannot have the same grade.'),
    ]

    @api.depends('snk_name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.snk_name