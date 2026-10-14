from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
import logging
_logger = logging.getLogger(__name__)


class ResPartner(models.Model):
    _inherit = "stock.picking.type"

    site_id = fields.Many2one('snk.site', "Site", copy=False, store=True, index=True)
    snk_is_mutation = fields.Boolean("Is Mutation",
        default=False,
        store=True,
        copy=False,
        index=True,
        help="Indicates the operation type is used for mutation through transit location.")
    snk_is_integration = fields.Boolean("Is Integration",
        default=False,
        store=True,
        copy=False,
        index=True,
        help="Indicates the operation type is used for POS Integration.")
    snk_mutation_code = fields.Char("Mutation Code", copy=False, index=True)
    snk_is_mrp = fields.Boolean("Is MRP",
        default=False,
        store=True,
        copy=False,
        index=True,
        help="Indicates the operation type is used mutation for MRP.")
    
    enable_mutation = fields.Boolean('Enable Mutation',
        store=False, readonly=True,
        default=lambda self: self._get_default_enable_mutation())

    def _get_default_enable_mutation(self):
        config = self.env['ir.config_parameter'].sudo().get_param('mesa.snk_is_mutation')
        return str(config).strip().lower() in ['true', '1', 'yes']

    # --- Helper check unique flagging ---
    @api.constrains('code', 'site_id', 'warehouse_id', 'snk_is_mrp', 'snk_is_mutation', 'snk_mutation_code')            
    def _check_unique_site_flagging(self):
        for rec in self:
            if not rec.code or not rec.site_id or not rec.warehouse_id:
                continue

            # hanya cek jika mutation aktif dan ada mutation_code
            if rec.snk_is_mutation and rec.snk_mutation_code:
                _logger.info(f"Checking unique mutation code for record ID {rec.id} with mutation code '{rec.snk_mutation_code}'")
                domain = [
                    ('id', '!=', rec.id),
                    ('code', '=', rec.code),
                    ('site_id', '=', rec.site_id.id),
                    ('warehouse_id', '=', rec.warehouse_id.id),
                    ('snk_is_mrp', '=', rec.snk_is_mrp),
                    ('snk_mutation_code', '=', rec.snk_mutation_code),
                ]
                _logger.info(f"Domain for mutation code check: {domain}")

                if self.search_count(domain):
                    raise ValidationError(
                        f"Configuration already exists for:\n"
                        f"- Type: {rec.code}\n"
                        f"- Site: {rec.site_id.display_name}\n"
                        f"- Warehouse: {rec.warehouse_id.display_name}\n"
                        f"- MRP: {'Yes' if rec.snk_is_mrp else 'No'}\n"
                        f"- Mutation Code: {rec.snk_mutation_code}"
                    )
            # cek unik untuk MRP tanpa mutation code
            elif rec.snk_is_mrp:
                _logger.info(f"Checking unique MRP config for record ID {rec.id}")
                domain = [
                    ('id', '!=', rec.id),
                    ('code', '=', rec.code),
                    ('site_id', '=', rec.site_id.id),
                    ('warehouse_id', '=', rec.warehouse_id.id),
                    ('snk_is_mrp', '=', True),
                ]
                _logger.info(f"Domain for MRP check: {domain}")

                if self.search_count(domain):
                    raise ValidationError(
                        f"Only one MRP config is allowed per Type, Site, and Warehouse.\n"
                        f"Existing config:\n"
                        f"- Type: {rec.code}\n"
                        f"- Site: {rec.site_id.display_name}\n"
                        f"- Warehouse: {rec.warehouse_id.display_name()}"
                    )