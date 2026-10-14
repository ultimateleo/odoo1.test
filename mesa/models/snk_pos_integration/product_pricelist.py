from xml.dom import minidom

from odoo import _, api, fields, models
import xml.etree.ElementTree as ET
from datetime import datetime

from odoo.exceptions import UserError


class Pricelist(models.Model):
    _inherit = "product.pricelist"

    snk_is_synch = fields.Boolean("Is Synchronize")

    @api.model_create_multi
    def create(self, vals):
        result = super(Pricelist, self).create(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     result.generate_xml()

        return result

    def write(self, vals):
        if 'snk_is_synch' not in vals:
            vals['snk_is_synch'] = False

        result = super(Pricelist, self).write(vals)

        # Config = self.env['ir.config_parameter'].sudo()
        # active_integration = Config.get_param('mesa.snk_active_integration')
        #
        # if active_integration:
        #     self.generate_xml()
        #     self.generate_xml("2")

        return result

    def outbound_opening_balance(self):
        ref_model = 'product.pricelist'
        ref_data = "price"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_xml("0")
        self.env[ref_model].search(domain).generate_xml("2")

    def outbound_process(self, brand=False):
        ref_model = self._name
        ref_data = "price"
        if brand:
            domain = [('snk_is_synch', '=', False),('analytic_id.name', '=', brand)]
        else:
            domain = [('snk_is_synch', '=', False)]
        self.env[ref_model].search(domain).generate_xml("0")
        self.env[ref_model].search(domain).generate_xml("2")


    def generate_xml(self, type = "0"):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "price"
        for record in self:
            list_range_date = []
            fixed_item = record.item_ids.filtered(lambda x: x.compute_price == 'fixed')
            if not len(fixed_item):
                continue

            for item in fixed_item:
                range_date = {'date_start': item.date_start, 'date_end': item.date_end}
                if range_date not in list_range_date:
                    list_range_date.append(range_date)


            for drange in list_range_date:
                date_start = drange['date_start']
                date_end = drange['date_end']
                root = ET.Element("Price")
                now = datetime.now()
                data_name = 'PRICE_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'
                # Convert ke timezone user
                user_tz_start = fields.Datetime.context_timestamp(self, date_start) if date_start else False
                user_tz_end = fields.Datetime.context_timestamp(self, date_end) if date_end else False

                ET.SubElement(root, "DocNum").text = record.name.upper() + "_" + type if record.name else ""
                ET.SubElement(root, "Store").text = "ALL"
                ET.SubElement(root, "Location").text = "L1"
                ET.SubElement(root, "StartDate").text = user_tz_start.strftime('%Y-%m-%d') if user_tz_start else ""
                ET.SubElement(root, "EndDate").text = user_tz_end.strftime('%Y-%m-%d') if user_tz_end else ""
                ET.SubElement(root, "Type").text = type
                ET.SubElement(root, "Deleted").text = "false"
                lines = ET.SubElement(root, "Lines")

                line_no = 0
                for item in fixed_item.filtered(lambda x: x.date_start == date_start and x.date_end == date_end):
                    line_no += 1
                    item_type = "ITEMCODE"

                    item_code = item.product_id.default_code if item.product_id else ""

                    if not item.product_id:
                        item_type = "GENERIC"
                        item_code = item.product_tmpl_id.default_code if item.product_tmpl_id else ""

                    row = ET.SubElement(lines, "Row")
                    ET.SubElement(row, "LineNo").text = str(line_no)
                    ET.SubElement(row, "ItemType").text = item_type
                    ET.SubElement(row, "Code").text = item_code
                    ET.SubElement(row, "ValueType").text = "0"
                    ET.SubElement(row, "PriceLine").text = str(int(item.fixed_price))
                    ET.SubElement(row, "Currency").text = item.currency_id.name

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

            record.write({'snk_is_synch':True})


class PricelistItem(models.Model):
    _inherit = "product.pricelist.item"

    snk_is_synch = fields.Boolean("Is Synchronize")

    def write(self, vals):
        if 'snk_is_synch' not in vals:
            vals['snk_is_synch'] = False

        result = super(PricelistItem, self).write(vals)

        return result

    def outbound_opening_balance(self):
        ref_model = 'product.pricelist'
        ref_data = "price"
        domain = [('ref_data', '=', ref_data),('ref_model', '=',ref_model)]
        ids = [rec.ref_id for rec in self.env['snk.pi.outbound'].search(domain)]
        domain = [('id', 'not in', ids)]
        self.env[ref_model].search(domain).generate_xml("0")
        self.env[ref_model].search(domain).generate_xml("2")

    def outbound_process(self, brand=False):
        ref_model = self._name
        ref_data = "price"
        if brand:
            domain = [('snk_is_synch', '=', False), ('analytic_id.name', '=', brand)]
        else:
            domain = [('snk_is_synch', '=', False)]
        self.env[ref_model].search(domain).generate_xml("0")
        self.env[ref_model].search(domain).generate_xml("2")


    def generate_xml(self, type = "0"):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "price"
        for item in self:
            if item.compute_price == 'fixed':
                date_start = item.date_start
                date_end = item.date_end
                # Convert ke timezone user
                user_tz_start = fields.Datetime.context_timestamp(self, date_start) if date_start else False
                user_tz_end = fields.Datetime.context_timestamp(self, date_end) if date_end else False
                root = ET.Element("Price")
                now = datetime.now()
                data_name = 'PRICE_' + f'{item.id}-' + '%s' % now.strftime('%Y%m%d-') + f'{int(now.microsecond / 1000):03d}'
                docnum = (
                    f"{item.pricelist_id.name.upper()}{type}"
                    f"{now.strftime('%Y%m%d-')}{int(now.microsecond/1000):03d}"
                    if item.pricelist_id and item.pricelist_id.name
                    else ""
                )

                ET.SubElement(root, "DocNum").text = docnum if docnum else data_name
                ET.SubElement(root, "Store").text = "ALL"
                ET.SubElement(root, "Location").text = "L1"
                ET.SubElement(root, "StartDate").text = user_tz_start.strftime('%Y-%m-%d') if user_tz_start else ""
                ET.SubElement(root, "EndDate").text = user_tz_end.strftime('%Y-%m-%d') if user_tz_end else "9999-12-31"
                ET.SubElement(root, "Type").text = type
                ET.SubElement(root, "Deleted").text = "false"
                lines = ET.SubElement(root, "Lines")

                line_no = 1
                item_type = "ITEMCODE"

                item_code = item.product_id.default_code if item.product_id else ""

                if not item.product_id:
                    item_type = "GENERIC"
                    item_code = item.product_tmpl_id.default_code if item.product_tmpl_id else ""

                row = ET.SubElement(lines, "Row")
                ET.SubElement(row, "LineNo").text = str(line_no)
                ET.SubElement(row, "ItemType").text = item_type
                ET.SubElement(row, "Code").text = item_code
                ET.SubElement(row, "ValueType").text = "0"
                ET.SubElement(row, "PriceLine").text = str(int(item.fixed_price))
                ET.SubElement(row, "Currency").text = item.currency_id.name

                ET.SubElement(root, "CreateTime").text = item.create_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "CreateBy").text = item.create_uid.name.upper() if item.create_uid.name else ""
                ET.SubElement(root, "UpdateTime").text = item.write_date.strftime('%Y-%m-%dT%H:%M:%S')
                ET.SubElement(root, "UpdateBy").text = item.write_uid.name.upper() if item.write_uid.name else ""

                rough_string = ET.tostring(root, 'utf-8')
                reparsed = minidom.parseString(rough_string)
                xmlstr = reparsed.toprettyxml(indent="  ")
                xmlstr = "\n".join(xmlstr.split("\n")[1:])  # remove XML declaration

                mevals = {
                    "name": data_name,
                    "ref_data": ref_data,
                    "ref_model": item._name,
                    "ref_id": item.id,
                    "data": xmlstr,
                    "origin": item.name,
                    "brand": item.analytic_id.name if item.analytic_id else "",
                }
                ET.dump(root)

                OutboundData.FindOrCreate(mevals)

                item.write({'snk_is_synch':True})

    def snk_action_xml_site(self):
        for rec in self:
            if rec.snk_is_synch:
                raise UserError(_(f"The pricelist {rec.name} is already synchronized."))
            rec.generate_xml("0")
            rec.generate_xml("2")