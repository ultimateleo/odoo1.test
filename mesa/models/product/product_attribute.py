# Copyright 2017 ForgeFlow S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

from odoo import _, api, fields, models


class ProductAttribute(models.Model):
    _name = 'product.attribute'
    _inherit = ['product.attribute', 'mail.thread', 'mail.activity.mixin']