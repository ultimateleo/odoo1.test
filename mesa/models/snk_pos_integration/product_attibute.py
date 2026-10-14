from xml.dom import minidom

from odoo import _, api, fields, models
import xml.etree.ElementTree as ET
from datetime import datetime


class ProductAttribute(models.Model):
    _inherit = "product.attribute"

    snk_is_synch = fields.Boolean("Is Synchronize")


    @api.model_create_multi
    def create(self, vals):
        result = super(ProductAttribute, self).create(vals)
        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     result.generate_xml()

        return result

    def write(self, vals):
        if 'snk_is_synch' not in vals:
            vals['snk_is_synch'] = False

        result = super(ProductAttribute, self).write(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     self.generate_xml()

        return result

    def outbound_opening_balance(self):
        ref_model = 'product.attribute'
        ref_data = "product_attribute"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_xml()

    def outbound_process(self):
        ref_model = 'product.attribute'
        ref_data = "product_attribute"
        domain = [('snk_is_synch', '=', False)]
        self.env[ref_model].search(domain).generate_xml()

    def generate_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "product_attribute"
        for record in self:
            attribute_name = record.name.upper()
            for value in record.value_ids:
                root = ET.Element("Characteristic")
                now = datetime.now()
                data_name = attribute_name + '_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

                # ET.SubElement(root, "Config").text =attribute_name +"_FG"
                ET.SubElement(root, "Config").text =attribute_name
                ET.SubElement(root, "Code").text = value.name.upper() if value.name else ""
                ET.SubElement(root, "Description").text = value.snk_description.upper() if value.snk_description else ""


                ET.SubElement(root, "Deleted").text = "false"
                ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper() if record.create_uid.name else ""
                ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper() if record.write_uid.name else ""

                rough_string = ET.tostring(root, 'utf-8')
                reparsed = minidom.parseString(rough_string)
                xmlstr = reparsed.toprettyxml(indent="  ")
                xmlstr = "\n".join(xmlstr.split("\n")[1:])  # remove XML declaration

                mevals = {
                    "name": data_name,
                    "ref_data": ref_data,
                    "ref_model": record._name,
                    "ref_id": record.id,
                    "data": xmlstr,
                    "origin": record.display_name,
                    # "brand": record.analytic_id.name if record.analytic_id else "",
                }
                ET.dump(root)

                OutboundData.FindOrCreate(mevals)
                record.write({'snk_is_synch':True})


    def snk_action_xml_attribute(self):
        self.generate_xml()