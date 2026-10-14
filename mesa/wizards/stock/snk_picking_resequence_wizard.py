import re
import logging
from datetime import datetime

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

_logger = logging.getLogger(__name__)

class SnkPickingResequenceV2Wizard(models.TransientModel):
    _name = "snk.picking.resequence.v2.wizard"
    _description = "Wizard Confirm Resequence V2"

    def action_confirm(self):
        active_ids = self.env.context.get('active_ids', [])
        if active_ids:
            pickings = self.env['stock.picking'].browse(active_ids)
            pickings.regenerate_sequence()
