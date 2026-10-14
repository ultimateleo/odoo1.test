from odoo import _, api, fields, models
import xml.etree.ElementTree as ET
from datetime import datetime


class ResPartner(models.Model):
    _inherit = "res.partner"

    snk_is_synch = fields.Boolean("Is Synchronize")


    @api.model_create_multi
    def create(self, vals):
        result = super(ResPartner, self).create(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     result.generate_xml()

        return result

    def write(self, vals):
        if 'snk_is_synch' not in vals:
            vals['snk_is_synch'] = False

        result = super(ResPartner, self).write(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     self.generate_xml()

        return result

    def outbound_opening_balance(self):
        ref_model = 'res.partner'
        ref_data = "store"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_xml()

    # def outbound_process(self):
    #     ref_model = 'res.partner'
    #     ref_data = "store"
    #     domain = [('snk_is_synch', '=', False)]
    #     self.env[ref_model].search(domain).generate_xml()


    def generate_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "store"
        for record in self:
            root = ET.Element("Store")
            now = datetime.now()
            data_name = 'STORE_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

            ET.SubElement(root, "Code").text = record.company_registry.upper() if record.company_registry else ""
            ET.SubElement(root, "Name").text = record.name.upper() if record.name else ""
            ET.SubElement(root, "Company").text = record.company_id.company_registry.upper() if record.company_id.company_registry else ""
            ET.SubElement(root, "MainStore").text = "MAIN"
            ET.SubElement(root, "Type").text = record.snk_status
            ET.SubElement(root, "Tax").text = "NOTAX"
            ET.SubElement(root, "TaxNo").text = "1"
            ET.SubElement(root, "TaxDate").text = record.create_date.strftime('%Y-%m-%d')
            ET.SubElement(root, "Street1").text = record.street if record.street else ""
            ET.SubElement(root, "Street2").text = record.street2 if record.street2 else ""
            ET.SubElement(root, "City").text = record.city_id.name if record.city_id else ""
            ET.SubElement(root, "Postal").text = record.zip if record.zip else ""
            ET.SubElement(root, "Country").text = record.country_id.name if record.country_id else ""
            ET.SubElement(root, "Phone").text = record.phone if record.phone else ""
            ET.SubElement(root, "ContactName").text = record.followup_responsible_id.name if record.followup_responsible_id else "BLANK"
            ET.SubElement(root, "Fax").text = record.snk_fax if record.snk_fax else ""
            ET.SubElement(root, "Email").text = record.email if record.email else ""
            ET.SubElement(root, "CloseSales").text = "9999-12-31"
            ET.SubElement(root, "CloseStore").text = "9999-12-31"

            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper() if record.create_uid.name else ""
            ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper() if record.write_uid.name else ""

            xmlstr = ET.tostring(root).decode()
            mevals = {"name": data_name, "ref_data": ref_data, "ref_model": record._name, "ref_id": record.id,
                      "data": xmlstr}
            ET.dump(root)

            OutboundData.FindOrCreate(mevals)

            record.write({'snk_is_synch': True})

    def snk_action_xml(self):
        self.generate_xml()
