# Copyright 2017 ForgeFlow S.L.
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).

import xml.etree.ElementTree as ET

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError
from xml.dom import minidom
from datetime import datetime


class ProductAttributeValue(models.Model):
    _name = 'product.attribute.value'
    _inherit = ['product.attribute.value', 'mail.thread', 'mail.activity.mixin']

    name = fields.Char(tracking=True)
    snk_description = fields.Char('Description')
    snk_is_synch = fields.Boolean("Is Synchronize")

    # _sql_constraints = [('uniq_product_attribute_value', 'unique(name, attribute_id)', 'Value already exists.')]


    def _message_log(self, body='', subject=False, message_type='notification', **kwargs):
        for rec in self:
            rec.attribute_id._message_log(body=f"{body}", subject=subject, message_type=message_type, **kwargs)

    def create(self, vals_list):
        for vals in vals_list:
            attribute_value_exists = self.env['product.attribute.value'].search_count([
                ('name', '=', vals.get("name")),
                ('attribute_id', '=', vals.get("attribute_id"))
            ])

            if attribute_value_exists > 0:
                raise ValidationError("Value already exists.")

        return super(ProductAttributeValue, self).create(vals_list)

    def write(self, vals):
        for record in self:
            
            attribute_value_exists = self.env['product.attribute.value'].search_count([
                ('name', '=', vals.get("name")),
                ('attribute_id', '=', record.attribute_id.id)
            ])

            if attribute_value_exists > 0:
                raise ValidationError("Value already exists.")

            if 'snk_is_synch' not in vals:
                vals['snk_is_synch'] = False

        return super(ProductAttributeValue, self).write(vals)

    def outbound_opening_balance(self):
        ref_model = 'product.attribute.value'
        ref_data = "product_attribute"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_xml()

    def outbound_process(self):
        ref_model = 'product.attribute.value'
        ref_data = "product_attribute"
        domain = [('snk_is_synch', '=', False)]
        self.env[ref_model].search(domain).generate_xml()

    def generate_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "product_attribute"
        for value in self:
            attribute_name = value.attribute_id.name.upper()
            root = ET.Element("Characteristic")
            now = datetime.now()
            data_name = attribute_name + '_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

            # ET.SubElement(root, "Config").text =attribute_name +"_FG"
            ET.SubElement(root, "Config").text =attribute_name
            ET.SubElement(root, "Code").text = value.name.upper() if value.name else ""
            ET.SubElement(root, "Description").text = value.snk_description.upper() if value.snk_description else ""


            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = value.create_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "CreateBy").text = value.create_uid.name.upper() if value.create_uid.name else ""
            ET.SubElement(root, "UpdateTime").text = value.write_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "UpdateBy").text = value.write_uid.name.upper() if value.write_uid.name else ""

            rough_string = ET.tostring(root, 'utf-8')
            reparsed = minidom.parseString(rough_string)
            xmlstr = reparsed.toprettyxml(indent="  ")
            xmlstr = "\n".join(xmlstr.split("\n")[1:])  # remove XML declaration

            mevals = {
                "name": data_name,
                "ref_data": ref_data,
                "ref_model": value._name,
                "ref_id": value.id,
                "data": xmlstr,
                "origin": value.display_name,
                # "brand": record.analytic_id.name if record.analytic_id else "",
            }
            ET.dump(root)

            OutboundData.FindOrCreate(mevals)
            value.write({'snk_is_synch':True})

    def snk_action_xml_attribute_value(self):
        self.generate_xml()