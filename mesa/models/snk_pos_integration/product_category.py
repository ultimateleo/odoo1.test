from xml.dom import minidom

from odoo import api, fields, models, _
import xml.etree.ElementTree as ET
from datetime import datetime


class ProductCategory(models.Model):
    _inherit = "product.category"

    snk_is_synch_brand = fields.Boolean("Is Synchronize")
    snk_is_synch_categories = fields.Boolean("Is Synchronize")


    @api.model_create_multi
    def create(self, vals):
        result = super(ProductCategory, self).create(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     result.generate_brand_xml()
        #     result.generate_categories_xml()

        return result


    def write(self, vals):
        if 'snk_is_synch_brand' not in vals:
            vals['snk_is_synch_brand'] = False

        if 'snk_is_synch_categories' not in vals:
            vals['snk_is_synch_categories'] = False


        result = super(ProductCategory, self).write(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     self.generate_brand_xml()
        #     self.generate_categories_xml()

        return result

    def outbound_opening_balance(self):
        ref_model = 'product.category'
        ref_data = "brand"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_brand_xml()

        ref_data = "product_category"
        domain = [('ref_data', '=', ref_data), ('ref_model', '=', ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_categories_xml()

    def outbound_process(self):
        ref_model = 'product.category'
        ref_data = "brand"
        domain = [('snk_is_synch_brand', '=', False)]
        self.env[ref_model].search(domain).generate_brand_xml()
        domain = [('snk_is_synch_categories', '=', False)]
        self.env[ref_model].search(domain).generate_categories_xml()

    def generate_brand_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "brand"
        for record in self:
            root = ET.Element("MCH")
            now = datetime.now()
            data_name = 'GROUP_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'
            config = "BRAND"
            if record.parent_id:
                config = "GROUP"
                if record.parent_id.parent_id:
                    config = "SUBGROUP"

            ET.SubElement(root, "Config").text = config
            ET.SubElement(root, "Code").text = record.name.upper()
            ET.SubElement(root, "Description").text = record.snk_description.upper() if record.snk_description else ""
            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper()
            ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper()

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
            record.write({'snk_is_synch_brand':True, 'snk_is_synch_categories':record.snk_is_synch_categories})

    def generate_categories_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "product_category"
        for record in self:
            root = ET.Element("MCMaster")
            now = datetime.now()
            data_name = 'MC_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

            ET.SubElement(root, "MCCode").text = record.display_name.replace(" ","").replace("/","").upper()
            ET.SubElement(root, "Description").text = record.snk_description.upper() if record.snk_description else ""

            sub = record.display_name.replace(" ","").split("/")
            MCH1 = ""
            MCH2 = ""
            MCH3 = ""
            if len(sub) == 3:
                MCH1 = sub[2] or ""
                MCH2 = sub[1] or ""
                MCH3 = sub[0] or ""
            # elif len(sub) == 2:
            #     MCH2 = sub[1] or ""
            #     MCH3 = sub[0] or ""
            # else:
            #     MCH3 = sub[0] or record.display_name


                ET.SubElement(root, "MCH1").text = MCH1.upper()
                ET.SubElement(root, "MCH2").text = MCH2.upper()
                ET.SubElement(root, "MCH3").text = MCH3.upper()
                ET.SubElement(root, "Deleted").text = "false"
                ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper()
                ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper()

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
            record.write({'snk_is_synch_brand':record.snk_is_synch_brand, 'snk_is_synch_categories':True})

    def snk_action_xml_brand(self):
        self.generate_brand_xml()

    def snk_action_xml_categories(self):
        self.generate_categories_xml()
