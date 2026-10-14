from odoo import api, fields, models, _
from odoo.exceptions import UserError
import paramiko


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    snk_pos_integration_enabled = fields.Boolean("POS Integration", config_parameter='mesa.group_snk_pos_integration', default=False)

    snk_active_integration = fields.Boolean("Active Integration", config_parameter='mesa.snk_active_integration')
    snk_sftp_host = fields.Char(string="Host", config_parameter='mesa.snk_sftp_host')
    snk_sftp_username = fields.Char("Username", config_parameter='mesa.snk_sftp_username')
    snk_sftp_password = fields.Char("Password", config_parameter='mesa.snk_sftp_password')
    snk_sftp_port = fields.Char("Port", config_parameter='mesa.snk_sftp_port', default='22')
    snk_sftp_path_local_outbound = fields.Char("Outbound Local", config_parameter='mesa.snk_sftp_path_local_outbound')
    snk_sftp_path_outbound = fields.Char("Outbound", config_parameter='mesa.snk_sftp_path_outbound')
    snk_sftp_path_local_inbound = fields.Char("Inbound Local", config_parameter='mesa.snk_sftp_path_local_inbound')
    snk_sftp_path_inbound = fields.Char("Inbound", config_parameter='mesa.snk_sftp_path_inbound')
    snk_sftp_path_inbound_archive = fields.Char("Inbound Archive", config_parameter='mesa.snk_sftp_path_inbound_archive')

    snk_sale_has_voucher = fields.Boolean("Has Voucher", config_parameter='mesa.snk_sale_has_voucher',
                                               default=False)
    snk_sale_voucher_product_id = fields.Many2one(
        comodel_name='product.product',
        string="Voucher Product",
        config_parameter='mesa.snk_sale_voucher_product_id',
        change_default=True, ondelete='restrict', check_company=True, index='btree_not_null',
        domain="[('sale_ok', '=', True)]")
    group_snk_order_staging = fields.Boolean("Order Staging", config_parameter='mesa.group_snk_order_staging',
                                               default=False,
                                               implied_group='mesa.group_snk_order_staging')
    snk_default_shipping = fields.Boolean("Default Shipping", config_parameter='mesa.snk_default_shipping', default=False)
    snk_default_shipping_id = fields.Many2one("delivery.carrier", string="Default Shipping",
                                              config_parameter="mesa.snk_default_shipping_id", change_default=False,
                                              ondelete='restrict', check_company=True, index='btree_not_null',)
    snk_default_shipping_product_id = fields.Many2one("product.product", string="Default Shipping Product",
                                              config_parameter="mesa.snk_default_shipping_product_id", change_default=False,
                                              ondelete='restrict', check_company=True, index='btree_not_null',)

    snk_is_mutation = fields.Boolean(string="Is Mutation",
        config_parameter='mesa.snk_is_mutation', default=False, help="Distribute your goods through transit location.",
    )

    def action_test_connection_sftp(self):
        Config = self.env['ir.config_parameter'].sudo()

        try:
            sftp_host = Config.get_param('mesa.snk_sftp_host')
            sftp_username = Config.get_param('mesa.snk_sftp_username')
            sftp_password = Config.get_param('mesa.snk_sftp_password')
            sftp_port = int(Config.get_param('mesa.snk_sftp_port'))

            transport = paramiko.Transport((sftp_host, sftp_port))
            transport.connect(username=sftp_username, password=sftp_password)
            return {
                'type': 'ir.actions.client',
                'tag': 'display_notification',
                'params': {
                    'title': 'Success!',
                    'message': 'SFTP server connected.',
                    'type': 'success',  # 'success', 'warning', 'danger', 'info'
                    'sticky': False,  # Set to True to keep the toast visible
                }
            }
        except Exception as e:
            raise UserError(e)