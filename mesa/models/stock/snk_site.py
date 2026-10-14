from xml.dom import minidom

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError, UserError
import xml.etree.ElementTree as ET
from datetime import datetime
import logging
_logger = logging.getLogger(__name__)


class SnkSite(models.Model):
    _name = "snk.site"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _description = "SNK Site"
    _rec_name = 'snk_code'
    _order = "snk_code asc"

    snk_state = fields.Selection(
        [("new","New"),
         ("open","Opened"),
         ("close","Closed"),
        ],
        'Status',
        default='new',
        copy=False,
        tracking=True)
    snk_code = fields.Char(string='Code', required=True, tracking=True ,help="Unique code for the site")
    snk_name = fields.Char(string='Name' , required=True,tracking=True ,help="Name of the site")
    industry_id = fields.Many2one('res.partner.industry', tracking=True, string='Industry')
    company_id = fields.Many2one('res.company', tracking=True, string='Company', default=lambda self: self.env.company)
    customer_id = fields.Many2one('res.partner', tracking=True, string='Invoice Address', help='Company which invoice/bill is addressed to.')
    partner_id = fields.Many2one('res.partner', tracking=True, string='Delivery Address', help='Company which delivery is addressed to.')
    snk_lead_time = fields.Float(string='Delivery Lead Time', tracking=True)
    full_name = fields.Char(string='Site', compute='_compute_full_name', store=True)

    # operations Group
    warehouse_id = fields.Many2one('stock.warehouse', tracking=True, string='Warehouse', required=True)
    snk_analytic_id = fields.Many2one('account.analytic.account', tracking=True, string='Brand', domain=[('snk_brand', '=', True)])
    snk_grade_id = fields.Many2one('snk.site.grade', tracking=True, string='Grade')
    snk_tag_ids = fields.Many2many('snk.site.tag', string='Tags', tracking=True)
    snk_pos_status = fields.Selection([("inactive","Inactive"),("active","Active"),("gudang","Gudang")], string='POS Status', default='inactive', tracking=True, help="Status of the site in POS system")
    active = fields.Boolean(string="Active", default=True, tracking=True)
    snk_reason_id = fields.Many2one('snk.site.history',string='Close Reason',compute='_compute_close_reason',readonly=True)  
    contact_ids = fields.Many2many('res.partner',
        'snk_site_contact_rel', # nama tabel relasi custom
        'snk_site_id',             # kolom referensi ke snk.site
        'res_partner_id',
        string='Contacts'
    )
    history_ids = fields.One2many('snk.site.history', 'site_id', string='Histories')  # Ditambahkan: One2many ke snk.site.history
    note = fields.Text('Notes', tracking=True)
    snk_is_synch = fields.Boolean("Is Synchronize")
    delay_lead_time = fields.Integer(string='Delay Lead Time', tracking=True, help="Additional lead time for deliveries to this site in days.")
    expected_arrival_time = fields.Integer(string='Expected Arrival Time', tracking=True, help="Expected arrival time for deliveries to this site in days.")

    # Constraints           
    @api.constrains('snk_code')
    def _check_unique_code(self):
        for rec in self:
            if rec.snk_code and self.search_count([
                ('id', '!=', rec.id),
                ('snk_code', '=', rec.snk_code)
            ]):
                raise ValidationError(
                    f"Site code '{rec.snk_code}' already exists. Please use another code."
                )

    # Computed Fields
    @api.depends('snk_name','snk_analytic_id')
    def _compute_full_name(self):
        for rec in self:
            if rec.snk_name and rec.snk_analytic_id:
                rec.full_name = f"{rec.snk_name} - {rec.snk_analytic_id.name}"
            elif rec.snk_name and not rec.snk_analytic_id:
                rec.full_name = rec.snk_name
            else:
                rec.full_name = ""

    @api.depends('snk_state')
    def _compute_close_reason(self):
        for rec in self:
            if rec.snk_state == 'close':
                last_close = self.env['snk.site.history'].search(
                    [('site_id', '=', rec.id), ('action', '=', 'closed')],
                    order='create_date desc',
                    limit=1
                )
                rec.snk_reason_id = last_close.id if last_close else False
            else:
                rec.snk_reason_id = False

    # Actions
    @api.model
    def create(self, vals):
        record = super(SnkSite, self).create(vals)
        self.env['snk.site.history'].sudo().create({
            'site_id': record.id,   # pakai record
            'user': self.env.user.id,
            'action': 'created'
        })
        return record

    def write(self, vals):
        if 'snk_is_synch' not in vals:
            vals['snk_is_synch'] = False

        result = super(SnkSite, self).write(vals)

        return result
      

    def action_open(self):
        invalid_sites = self.filtered(lambda s: s.snk_state != 'new')
        if invalid_sites:
            raise UserError(_(
                "The following sites cannot be opened because "
                "their status is not 'New': %s")
                % ", ".join(invalid_sites.mapped('snk_code')))
        else:    
            for rec in self:
                if rec.snk_state == 'new':
                    rec.write({'snk_state': 'open'})
                    rec.env['snk.site.history'].sudo().create({
                        'site_id': rec.id,
                        'user': self.env.user.id,
                        'action': 'opened'
                    })
                else:
                    raise UserError(_("Only sites that is new can be opened."))

    def snk_action_xml_site(self):
        for rec in self:
            if rec.snk_is_synch:
                raise UserError(_(f"The site {rec.snk_name} is already synchronized."))
            rec.generate_xml()

    def _get_pos_customer_location(self):
        customer_location = self.env.ref('stock.stock_location_customers', raise_if_not_found=False)
        if not customer_location:
            customer_location = self.env['stock.location'].search([('usage', '=', 'customer')], limit=1)
        if not customer_location:
            raise UserError(_("Customer location was not found."))
        return customer_location

    def _get_existing_pos_picking_type(self, warehouse, field_name, code, sequence_code):
        picking_type = warehouse[field_name]
        if picking_type:
            return picking_type
        picking_type = self.env['stock.picking.type'].with_context(active_test=False).search([
            ('warehouse_id', '=', warehouse.id),
            ('code', '=', code),
            ('sequence_code', '=', sequence_code),
        ], limit=1)
        if picking_type:
            warehouse.write({field_name: picking_type.id})
        return picking_type

    def _ensure_pos_picking_type(self, warehouse, field_name, values, force_values=None):
        picking_type = self._get_existing_pos_picking_type(
            warehouse,
            field_name,
            values['code'],
            values['sequence_code'],
        )
        if picking_type:
            update_vals = {'active': True}
            if not picking_type.site_id:
                update_vals['site_id'] = self.id
            if force_values:
                update_vals.update(force_values)
            picking_type.write(update_vals)
            return picking_type

        if force_values:
            values.update(force_values)
        picking_type = self.env['stock.picking.type'].create(values)
        if force_values:
            picking_type.write(force_values)
        warehouse.write({field_name: picking_type.id})
        return picking_type

    def action_activate_pos(self):
        customer_location = self._get_pos_customer_location()
        max_sequence = self.env['stock.picking.type'].search_read(
            [('sequence', '!=', False)],
            ['sequence'],
            limit=1,
            order='sequence desc',
        )
        max_sequence = max_sequence and max_sequence[0]['sequence'] or 0

        for rec in self:
            warehouse = rec.warehouse_id
            if not warehouse:
                raise UserError(_("Site %s does not have a warehouse.") % rec.display_name)
            if not warehouse.lot_stock_id:
                raise UserError(_("Warehouse %s does not have a stock location.") % warehouse.display_name)

            site_code = rec.snk_code.replace(" ", "").upper()
            max_sequence += 1
            pos_out = rec._ensure_pos_picking_type(warehouse, 'snk_pos_out_id', {
                'name': _('POS OUT'),
                'code': 'outgoing',
                'use_existing_lots': False,
                'sequence': max_sequence,
                'sequence_code': 'POSOUT',
                'company_id': warehouse.company_id.id,
                'default_location_src_id': warehouse.lot_stock_id.id,
                'default_location_dest_id': customer_location.id,
                'barcode': site_code + 'POSOUT',
                'warehouse_id': warehouse.id,
                'site_id': rec.id,
            })

            max_sequence += 1
            pos_in = rec._ensure_pos_picking_type(warehouse, 'snk_pos_in_id', {
                'name': _('POS IN'),
                'code': 'incoming',
                'use_existing_lots': False,
                'sequence': max_sequence,
                'sequence_code': 'POSIN',
                'company_id': warehouse.company_id.id,
                'default_location_src_id': customer_location.id,
                'default_location_dest_id': warehouse.lot_stock_id.id,
                'barcode': site_code + 'POSIN',
                'warehouse_id': warehouse.id,
                'site_id': rec.id,
            }, {
                'default_location_src_id': customer_location.id,
                'default_location_dest_id': warehouse.lot_stock_id.id,
            })
            pos_out.write({'return_picking_type_id': pos_in.id})
            pos_in.write({'return_picking_type_id': pos_out.id})
            rec.write({'snk_pos_status': 'active'})

    def action_deactivate_pos(self):
        self.write({'snk_pos_status': 'inactive'})

    def action_mark_as_gudang(self):
        self.write({'snk_pos_status': 'gudang'})
    
    def action_close(self):
        invalid_sites = self.filtered(lambda s: s.snk_state != 'open')
        if invalid_sites:
            raise UserError(_(
                "The following sites cannot be closed because " \
                "their status is not 'Open': %s")
                % ", ".join(invalid_sites.mapped('snk_code')))
        return {
            'name': 'Close Site',
            'type': 'ir.actions.act_window',
            'res_model': 'snk.site.close.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'default_site_ids': self.ids,
                'default_action': 'closed',
            },
        }

    def action_reopen(self):
        invalid_sites = self.filtered(lambda s: s.snk_state != 'close')
        if invalid_sites:
            raise UserError(_(
                "The following sites cannot be re-opened because "
                "their status is not 'Closed': %s")
                % ", ".join(invalid_sites.mapped('snk_code')))
        else:
            for rec in self:
                if rec.snk_state == 'close':
                    rec.write({'snk_state': 'open'})
                    rec.env['snk.site.history'].sudo().create({
                        'site_id': rec.id,
                        'user': self.env.user.id,
                        'action': 'reopened'
                    })
                else:
                    raise UserError(_("Only sites that has been closed can be re-opened."))

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        if name:
            records = self.search([
                '|',
                ('snk_code', operator, name),
                ('snk_name', operator, name)
            ] + args, limit=limit)
        else:
            records = self.search(args, limit=limit)

        # simpan keyword pencarian di context supaya name_get bisa bedain
        return records.with_context(search_term=name).name_get()

    def name_get(self):
        """ 
        Saat dropdown muncul:
        - Kalau user search pakai code → tampil code.
        - Kalau user search pakai name → tampil name.
        Setelah pilih, tetap tampil code (karena _rec_name = snk_code).
        """
        result = []
        search_term = self.env.context.get('search_term')
        for rec in self:
            if search_term and search_term.lower() in (rec.snk_name or '').lower():
                # kalau keyword cocok ke nama → tampilkan nama
                result.append((rec.id, rec.snk_name))
            else:
                # default → tampilkan code
                result.append((rec.id, rec.snk_code))
        return result

    def outbound_process(self, brand=False):
        ref_model = self._name
        ref_data = "store"
        if brand:
            domain = [('snk_is_synch', '=', False),('snk_analytic_id.name', '=', brand)]
        else:
            domain = [('snk_is_synch', '=', False)]
        self.env[ref_model].search(domain).generate_xml()

    def generate_xml(self):
        OutboundData = self.env['snk.pi.outbound']
        ref_data = "store"
        for record in self:
            root = ET.Element("Store")
            now = datetime.now()
            data_name = 'STORE_%s' % now.strftime('%Y%m%d-%H%M%S-') + f'{int(now.microsecond / 1000):03d}'
            contact_person = record.contact_ids.filtered(
                lambda p: p.function == 'PIC'
            )[:1]
            _logger.info("Contact person (PIC) for Site %s is %s", record.snk_code, contact_person.name if contact_person else "Not Found")

            ET.SubElement(root, "Code").text = record.snk_code.upper() if record.snk_code else ""
            ET.SubElement(root, "Name").text = record.snk_name.upper() if record.snk_name else ""
            ET.SubElement(root, "Company").text = record.company_id.company_registry.upper() if record.company_id.company_registry else ""
            ET.SubElement(root, "MainStore").text = "MAIN"
            ET.SubElement(root, "Type").text = record.snk_pos_status
            ET.SubElement(root, "Tax").text = "NOTAX"
            ET.SubElement(root, "TaxNo").text = "1"
            ET.SubElement(root, "TaxDate").text = record.create_date.strftime('%Y-%m-%d')
            ET.SubElement(root, "Street1").text = record.partner_id.street if record.partner_id.street else ""
            ET.SubElement(root, "Street2").text = record.partner_id.street2 if record.partner_id.street2 else ""
            ET.SubElement(root, "City").text = record.partner_id.city_id.name if record.partner_id.city_id else ""
            ET.SubElement(root, "Postal").text = record.partner_id.zip if record.partner_id.zip else ""
            ET.SubElement(root, "Country").text = record.partner_id.country_id.name if record.partner_id.country_id else ""
            ET.SubElement(root, "Phone").text = record.partner_id.phone if record.partner_id.phone else ""
            ET.SubElement(root, "ContactName").text = contact_person.name if contact_person else ""
            ET.SubElement(root, "Fax").text = record.partner_id.snk_fax if record.partner_id.snk_fax else ""
            ET.SubElement(root, "Email").text = record.partner_id.email if record.partner_id.email else ""
            ET.SubElement(root, "CloseSales").text = "9999-12-31"
            ET.SubElement(root, "CloseStore").text = "9999-12-31"

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
                "brand": record.snk_analytic_id.name if record.snk_analytic_id else "",
            }
            ET.dump(root)

            OutboundData.FindOrCreate(mevals)

            record.write({'snk_is_synch': True})
           

    
