from xml.dom import minidom

from odoo import _, api, fields, models
import xml.etree.ElementTree as ET
from datetime import datetime


class ProductProduct(models.Model):
    _inherit = "product.product"

    snk_is_synch_product = fields.Boolean("Is Synchronize Product")
    snk_is_synch_barcode = fields.Boolean("Is Synchronize Barcode")


    @api.model_create_multi
    def create(self, vals):
        result = super(ProductProduct, self).create(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     result.generate_product_xml()
        #     result.generate_barcode_xml()

        return result

    def write(self, vals):

        if 'snk_is_synch_product' not in vals:
            vals['snk_is_synch_product'] = False

        if 'snk_is_synch_barcode' not in vals:
            vals['snk_is_synch_barcode'] = False

        result = super(ProductProduct, self).write(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     self.generate_product_xml()
        #     self.generate_barcode_xml()

        return result

    def outbound_opening_balance(self):
        ref_model = 'product.product'
        ref_data = "product"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_product_xml()

        ref_data = "barcode"
        domain = [('ref_data', '=', ref_data), ('ref_model', '=', ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_barcode_xml()

    def outbound_process(self, brand=False):
        ref_model = 'product.product'
        ref_data = "price"
        if brand:
            domain_product = [('snk_is_synch_product', '=', False),('analytic_id.name', '=', brand)]
            domain_barcode = [('snk_is_synch_barcode', '=', False),('analytic_id.name', '=', brand)]
        else:
            domain_product = [('snk_is_synch_product', '=', False)]
            domain_barcode = [('snk_is_synch_barcode', '=', False)]
        self.env[ref_model].search(domain_product).generate_product_xml()
        self.env[ref_model].search(domain_barcode).generate_barcode_xml()


    def generate_product_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "product"
        for record in self:
            if not record.default_code:
                continue

            root = ET.Element("Article")
            now = datetime.now()
            data_name = 'ARTICLE_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

            ET.SubElement(root, "ItemCode").text = record.default_code
            item_type = 'Variant'
            variant_1 = ""
            variant_2 = ""
            if len(record.product_template_variant_value_ids) == 2:
                variant_1 = record.product_template_variant_value_ids[0].name.upper() if record.product_template_variant_value_ids[0] else ""
                variant_2 = record.product_template_variant_value_ids[1].name.upper() if record.product_template_variant_value_ids[1] else ""
                ET.SubElement(root, "Description").text = "{}, {}, {}".format(record.name.upper(), variant_1, variant_2)
            elif len(record.product_template_variant_value_ids) == 1:
                variant_1 = record.product_template_variant_value_ids[0].name.upper() if record.product_template_variant_value_ids[0] else ""
                ET.SubElement(root, "Description").text = "{}, {}".format(record.name.upper(), variant_1)
            else :
                ET.SubElement(root, "Description").text = record.name.upper() if record.name else ""
                item_type = 'Single'

            ET.SubElement(root, "MC").text = record.categ_id.display_name.replace(" ","").replace("/","").upper()
            ET.SubElement(root, "Type").text = item_type
            ET.SubElement(root, "Conversion").text = "1"

            if item_type == 'Variant':
                ET.SubElement(root, "Generic").text = record.product_tmpl_id.default_code

            ET.SubElement(root, "StockType").text = "1"
            ET.SubElement(root, "StockUOM").text = record.uom_id.name if record.uom_id else ""
            ET.SubElement(root, "IdentType").text = "0"
            ET.SubElement(root, "PrefVendor").text = record.company_id.company_registry.upper() if record.company_id.company_registry else ""
            ET.SubElement(root, "OpenPrice").text = "0"

            if item_type == 'Variant':
                ET.SubElement(root, "Row").text = variant_1
                ET.SubElement(root, "Col").text = variant_2

            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S') if record.create_date else ""
            ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper() if record.create_uid.name else ""
            ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S') if record.write_date else ""
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
                "brand": record.analytic_id.name if record.analytic_id else "",
            }
            ET.dump(root)

            OutboundData.FindOrCreate(mevals)
            record.write({'snk_is_synch_product':True})


    def generate_barcode_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "barcode"
        for record in self:
            if not record.default_code:
                continue

            if not record.barcode:
                continue

            root = ET.Element("ArticleSales")
            now = datetime.now()
            data_name = 'BARCODE_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'

            ET.SubElement(root, "EAN").text = record.barcode
            ET.SubElement(root, "ItemCode").text = record.default_code
            ET.SubElement(root, "SalesUOM").text = record.uom_id.name if record.uom_id else ""
            ET.SubElement(root, "UOMFactor").text = "1"
            ET.SubElement(root, "Currency").text = record.company_currency_id.name
            ET.SubElement(root, "NormalPrice").text = "1"
            ET.SubElement(root, "PromoPrice").text = "0"
            ET.SubElement(root, "OriginalPrice").text = "1"
            ET.SubElement(root, "Tax").text = "NOTAX"
            ET.SubElement(root, "Regular").text = "true"
            ET.SubElement(root, "LocalPurchase").text = "false"

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
                "brand": record.analytic_id.name if record.analytic_id else "",
            }
            ET.dump(root)

            OutboundData.FindOrCreate(mevals)
            record.write({'snk_is_synch_barcode':True})

    def snk_action_xml_product(self):
        self.generate_product_xml()


    def snk_action_xml_barcode(self):
        self.generate_barcode_xml()
