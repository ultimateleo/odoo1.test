from xml.dom import minidom

from odoo import _, api, fields, models
import xml.etree.ElementTree as ET
from datetime import datetime
from odoo.exceptions import UserError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class Picking(models.Model):
    _inherit = "stock.picking"

    snk_is_pos = fields.Boolean(default=False)

    snk_inbound_id = fields.Many2one(
        'snk.pi.inbound',
        string="Inbound Id",
        copy=False
    )

    snk_outbound_id = fields.Many2one(
        'snk.pi.outbound',
        string="Outbound Id",
        copy=False
    )
    enable_mutation = fields.Boolean('Enable Mutation',
        # compute='_compute_enable_mutation',
        store=False, readonly=True,
        default=lambda self: self._get_default_enable_mutation())

    def _get_default_enable_mutation(self):
        config = self.env['ir.config_parameter'].sudo().get_param('mesa.snk_is_mutation')
        # _logger.info(">>>>> Param value: %s", config)
        return str(config).strip().lower() in ['true', '1', 'yes']

    def _find_existing_transfer_outbound(self):
        """Resolve one existing transfer outbound without relying on origin."""
        self.ensure_one()
        Outbound = self.env['snk.pi.outbound']

        linked_outbound = self.snk_outbound_id.exists()
        if linked_outbound:
            expected_reference = (
                linked_outbound.ref_model == self._name
                and linked_outbound.ref_id == self.id
                and linked_outbound.ref_data == 'transfer'
            )
            if not expected_reference:
                raise ValidationError(_(
                    "Outbound %(outbound)s linked to picking %(picking)s does not "
                    "reference this picking. Resolve the link before generating XML.",
                    outbound=linked_outbound.display_name,
                    picking=self.display_name,
                ))
            return linked_outbound

        legacy_outbounds = Outbound.search([
            ('ref_model', '=', self._name),
            ('ref_id', '=', self.id),
            ('ref_data', '=', 'transfer'),
        ], order='id desc', limit=2)

        if len(legacy_outbounds) > 1:
            outbound_info = ", ".join(
                "%s (ID %s)" % (outbound.display_name, outbound.id)
                for outbound in legacy_outbounds
            )
            raise ValidationError(_(
                "Multiple outbound records reference picking %(picking)s: "
                "%(outbounds)s. XML generation was stopped to prevent another "
                "duplicate outbound.",
                picking=self.display_name,
                outbounds=outbound_info,
            ))

        if legacy_outbounds:
            outbound = legacy_outbounds
            link_values = {'snk_outbound_id': outbound.id}
            if not self.snk_pos_ref and outbound.name:
                link_values['snk_pos_ref'] = outbound.name
            self.write(link_values)
            _logger.info(
                ">>>>> Relink picking %s ke legacy outbound %s (ID %s).",
                self.display_name,
                outbound.display_name,
                outbound.id,
            )
            return outbound

        return Outbound.browse()

    def _raise_if_existing_transfer_outbounds(self):
        """Stop XML generation and list every picking with an existing outbound."""
        conflicts = []
        for picking in self:
            outbound = picking._find_existing_transfer_outbound()
            if not outbound:
                continue

            next_action = {
                'pending': 'Process the existing outbound.',
                'failed': 'Retry the existing outbound, then run Process.',
                'duplicated': 'Retry the existing outbound, then run Process.',
                'cancel': 'Retry the existing outbound, then run Process.',
                'done': 'Use Resend to SFTP if POS still needs the file.',
            }.get(
                outbound.state,
                'Review the existing outbound before continuing.',
            )
            conflicts.append(_(
                "- %(picking)s: %(outbound)s "
                "(ID %(outbound_id)s, state %(state)s). %(next_action)s",
                picking=picking.display_name,
                outbound=outbound.display_name,
                outbound_id=outbound.id,
                state=outbound.state,
                next_action=next_action,
            ))

        if conflicts:
            raise UserError(_(
                "Outbound records already exist for these pickings:\n\n%(conflicts)s",
                conflicts="\n".join(conflicts),
            ))

    # @api.depends_context('uid')
    # def _compute_enable_mutation(self):
    #     config = self.env['ir.config_parameter'].sudo().get_param('mesa.snk_is_mutation')
    #     is_enable = str(config).strip().lower() in ['true', '1', 'yes']
    #     for rec in self:
    #         rec.enable_mutation = is_enable

    # def button_validate(self):
    #     res = super().button_validate()

    #     Config = self.env['ir.config_parameter'].sudo()
    #     active_integration = Config.get_param('mesa.snk_active_integration')

    #     # _logger.info(">>>>> active_integration: %s", active_integration)
    #     if active_integration:
    #         for record in self:
    #             if record.picking_type_id.code == "incoming":
    #                 if record.snk_site_id.snk_pos_status == 'active' and not record.snk_is_pos: ## ubah pos status dari snk_site_id
    #                     _logger.info(">>>>> Validasi by pos status dari Site berjalan.")
    #                     raise ValidationError(
    #                         "You cannot validate receipt for active POS. Please contact the responsible partner to validate this receipt.")

    #     return res

    # Tidak dipakai lagi karena sudah di-handle di stock_picking.py pada stock module
    def generate_receipt(self):
        for record in self:
            DestinationWarehouse = self.env['stock.warehouse'].search([('partner_id', '=', record.partner_id.id)], limit=1)

            OperationTypeIn = self.env['stock.picking.type'].search(
                [('code', '=', 'incoming'), ('warehouse_id', '=', DestinationWarehouse.id)], limit=1)

            data_lines = []
            for item in record.move_ids_without_package:
                data_lines.extend([(0, 0, {
                    'product_id': item.product_id.id,
                    'snk_grade_id': item.snk_grade_id.id or False,
                    'product_uom_qty': item.product_uom_qty,
                    'location_id': OperationTypeIn.default_location_src_id.id,
                    'location_dest_id': OperationTypeIn.default_location_dest_id.id,
                    'name': item.product_id.name,
                })])

            if len(data_lines):
                values = {
                    'picking_type_id': OperationTypeIn.id,
                    'location_id': OperationTypeIn.default_location_src_id.id,
                    'location_dest_id': OperationTypeIn.default_location_dest_id.id,
                    'scheduled_date': record.scheduled_date,
                    'snk_pos_ref': record.name,
                    'origin': record.origin,
                    'move_ids': data_lines
                }
                TO_R = self.env['stock.picking'].create(values)
                TO_R.action_confirm()
                TO_R.move_ids.update({'quantity': 0})

    def generate_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "transfer"

        self._raise_if_existing_transfer_outbounds()
        for record in self:
            DocType = ""
            if record.picking_type_id.snk_is_integration:
                DocType = record.picking_type_id.snk_mutation_code if record.picking_type_id.snk_mutation_code else ""
            # elif record.picking_type_id.code == "internal":
            #     DocType = "AH"
            destination = ''
            if record.snk_site_address_id:
                destination = record.snk_site_address_id.snk_code.upper()
            elif not record.snk_site_address_id and record.snk_site_id:
                destination = record.snk_site_id.snk_code.upper()
            else:
                destination = ""

            root = ET.Element("MoveHis")
            now = datetime.now()
            data_name = 'TRANSFER_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'
            if record.write({'snk_pos_ref': data_name}):
                _logger.info(">>>>> Outbound transfer terbentuk: %s", data_name)

            TotalQuantity = 0
            TotalPrcQuantity = 0
            TotalLine = 0
            for item in record.move_line_ids_without_package:
                qty = item.quantity
                TotalQuantity += qty
                TotalPrcQuantity += qty
                TotalLine += 1

            ET.SubElement(root, "DocNum").text = record.name.upper() if record.name else ""
            ET.SubElement(root, "DocDate").text = record.date_done.strftime('%Y-%m-%d') if record.date_done else datetime.now().strftime('%Y-%m-%d')
            ET.SubElement(root, "DocDraftDate").text = record.scheduled_date.strftime('%Y-%m-%d') if record.scheduled_date else datetime.now().strftime('%Y-%m-%d')
            ET.SubElement(root, "DocType").text = DocType.upper()
            ET.SubElement(root, "Pos").text = "01"
            ET.SubElement(root, "Remarks").text = record.origin.upper() if record.origin else ""
            ET.SubElement(root, "PosType").text = "Fixed"
            ET.SubElement(root, "Status").text = "NEW"
            ET.SubElement(root, "TotalQuantity").text = str(TotalQuantity)
            # ET.SubElement(root, "TotalPrcQuantity").text = str(TotalPrcQuantity)
            ET.SubElement(root, "TotalPrcQuantity").text = "0.00"
            ET.SubElement(root, "TotalLine").text = str(TotalLine)
            # ET.SubElement(root, "SourceStore").text = record.partner_id.company_registry.upper() if record.partner_id.company_registry else ""
            ET.SubElement(root, "SourceStore").text = record.snk_site_id.snk_code.upper() if record.snk_site_id else ""
            ET.SubElement(root, "SourceLocation").text = "L1"
            # ET.SubElement(root, "DestinationStore").text = record.location_dest_id.warehouse_id.code.upper() if record.location_dest_id.warehouse_id.code else ""
            ET.SubElement(root, "DestinationStore").text = destination
            ET.SubElement(root, "DestinationLocation").text = "L1"


            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper() if record.create_uid.name else ""
            ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper() if record.write_uid.name else ""

            lines = ET.SubElement(root, "Lines")
            line_no = 0
            for item in record.move_line_ids_without_package:
                line_no += 1
                row = ET.SubElement(lines, "Row")
                ET.SubElement(row, "LineNo").text = str(line_no)
                ET.SubElement(row, "BaseLine").text = "0"
                ET.SubElement(row, "ItemCode").text = item.product_id.default_code
                ET.SubElement(row, "EAN").text = item.product_id.barcode
                ET.SubElement(row, "QtyItemCode").text = str(item.quantity)
                ET.SubElement(row, "QtyEAN").text = str(item.quantity)
                ET.SubElement(row, "Reason")
                ET.SubElement(row, "Currency").text = "IDR"
                ET.SubElement(row, "Rate").text = "1.0"
                ET.SubElement(row, "Price").text = "0.00"
                ET.SubElement(row, "DisplayPrice").text = "0.00"
                # ET.SubElement(row, "QtyProc").text = str(item.quantity)
                ET.SubElement(row, "QtyProc").text = "0.00"

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

            outbound = OutboundData.FindOrCreate(mevals)
            record.write({'snk_outbound_id': outbound.id})

    def generate_xml_adjustment(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "transfer"

        self._raise_if_existing_transfer_outbounds()
        for record in self:
            DocType = ""
            if record.picking_type_id.snk_is_integration:
                DocType = record.picking_type_id.snk_mutation_code if record.picking_type_id.snk_mutation_code else ""
            # elif record.picking_type_id.code == "internal":
            #     DocType = "AH"
            destination = ''
            if record.snk_site_address_id:
                destination = record.snk_site_address_id.snk_code.upper()
            elif not record.snk_site_address_id and record.snk_site_id:
                destination = record.snk_site_id.snk_code.upper()
            else:
                destination = ""

            root = ET.Element("MoveHis")
            now = datetime.now()
            data_name = 'TRANSFER_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'
            if record.write({'snk_pos_ref': data_name}):
                _logger.info(">>>>> Outbound transfer terbentuk: %s", data_name)

            TotalQuantity = 0
            TotalPrcQuantity = 0
            TotalLine = 0
            for item in record.move_line_ids_without_package:
                qty = item.quantity
                if qty:
                    TotalQuantity += qty
                    TotalPrcQuantity += qty
                    TotalLine += 1

            if record.picking_type_code == 'outgoing':
                TotalQuantity = TotalQuantity * -1
            else:
                TotalQuantity = TotalQuantity

            ET.SubElement(root, "DocNum").text = record.name.upper() if record.name else ""
            ET.SubElement(root, "DocDate").text = record.date_done.strftime('%Y-%m-%d') if record.date_done else datetime.now().strftime('%Y-%m-%d')
            ET.SubElement(root, "DocDraftDate").text = record.scheduled_date.strftime('%Y-%m-%d') if record.scheduled_date else datetime.now().strftime('%Y-%m-%d')
            ET.SubElement(root, "DocType").text = DocType.upper()
            ET.SubElement(root, "Pos").text = "01"
            ET.SubElement(root, "Remarks").text = record.origin.upper() if record.origin else ""
            ET.SubElement(root, "PosType").text = "Fixed"
            ET.SubElement(root, "Status").text = "NEW"
            ET.SubElement(root, "TotalQuantity").text = str(TotalQuantity)
            # ET.SubElement(root, "TotalPrcQuantity").text = str(TotalPrcQuantity)
            ET.SubElement(root, "TotalPrcQuantity").text = "0.00"
            ET.SubElement(root, "TotalLine").text = str(TotalLine)
            # ET.SubElement(root, "SourceStore").text = record.partner_id.company_registry.upper() if record.partner_id.company_registry else ""
            ET.SubElement(root, "SourceStore").text = record.snk_site_id.snk_code.upper() if record.snk_site_id else ""
            ET.SubElement(root, "SourceLocation").text = "L1"
            # ET.SubElement(root, "DestinationStore").text = record.location_dest_id.warehouse_id.code.upper() if record.location_dest_id.warehouse_id.code else ""
            ET.SubElement(root, "DestinationStore").text = destination
            ET.SubElement(root, "DestinationLocation").text = "L1"


            ET.SubElement(root, "Deleted").text = "false"
            ET.SubElement(root, "CreateTime").text = record.create_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "CreateBy").text = record.create_uid.name.upper() if record.create_uid.name else ""
            ET.SubElement(root, "UpdateTime").text = record.write_date.strftime('%Y-%m-%dT%H:%M:%S')
            ET.SubElement(root, "UpdateBy").text = record.write_uid.name.upper() if record.write_uid.name else ""

            lines = ET.SubElement(root, "Lines")
            line_no = 0
            for item in record.move_line_ids_without_package:
                line_no += 1
                # QtyItemCode = item.product_uom_qty * (-1 if record.picking_type_code == 'outgoing' else 1)
                # QtyEAN = item.product_uom_qty * (-1 if record.picking_type_code == 'outgoing' else 1)
                # _logger.info(">>>>> QtyItemCode: %s for picking type: %s", QtyItemCode, record.picking_type_code)
                # _logger.info(">>>>> QtyEAN: %s for picking type: %s", QtyEAN, record.picking_type_code)
                row = ET.SubElement(lines, "Row")
                ET.SubElement(row, "LineNo").text = str(line_no)
                ET.SubElement(row, "BaseLine").text = "0"
                ET.SubElement(row, "ItemCode").text = item.product_id.default_code
                ET.SubElement(row, "EAN").text = item.product_id.barcode
                ET.SubElement(row, "QtyItemCode").text = str(item.quantity * (-1 if record.picking_type_code == 'outgoing' else 1))
                ET.SubElement(row, "QtyEAN").text = str(item.quantity * (-1 if record.picking_type_code == 'outgoing' else 1))
                ET.SubElement(row, "Reason")
                ET.SubElement(row, "Currency").text = "IDR"
                ET.SubElement(row, "Rate").text = "1.0"
                ET.SubElement(row, "Price").text = "0.00"
                ET.SubElement(row, "DisplayPrice").text = "0.00"
                # ET.SubElement(row, "QtyProc").text = str(item.quantity)
                ET.SubElement(row, "QtyProc").text = "0.00"

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

            outbound = OutboundData.FindOrCreate(mevals)
            record.write({'snk_outbound_id': outbound.id})
