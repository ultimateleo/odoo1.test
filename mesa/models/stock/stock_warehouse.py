from odoo import models, api, fields, _
from odoo.osv import expression


class Warehouse(models.Model):
    _inherit = 'stock.warehouse'


    snk_status = fields.Selection(related='partner_id.snk_status', string="POS Status", store=True)
    snk_pos_in_id = fields.Many2one('stock.picking.type', 'POS In', copy=False)
    snk_pos_out_id = fields.Many2one('stock.picking.type', 'POS Out', copy=False)
    minimum_picking = fields.Float('Minimum Picking')


    def snk_active_or_create_pos_picking_type(self):
        name = self.name
        code = self.code
        input_loc, output_loc = self._get_input_output_locations(self.reception_steps, self.delivery_steps)
        IrSequenceSudo = self.env['ir.sequence'].sudo()
        PickingType = self.env['stock.picking.type']

        # suit for each warehouse: reception, internal, pick, pack, ship
        max_sequence = self.env['stock.picking.type'].search_read([('sequence', '!=', False)], ['sequence'], limit=1,
                                                                  order='sequence desc')
        max_sequence = max_sequence and max_sequence[0]['sequence'] or 0

        create_data, max_sequence = self._get_picking_type_create_values(max_sequence)

        sequence_data = {
            'snk_pos_in_id': {
                'name': _('SNK %(name)s Sequence POS in', name=name),
                'prefix': code + '/POS/IN/', 'padding': 5,
                'company_id': self.company_id.id,
            },
            'snk_pos_out_id': {
                'name': _('SNK %(name)s Sequence POS out', name=name),
                'prefix': code + '/POS/OUT/', 'padding': 5,
                'company_id': self.company_id.id,
            },
        }

        if self.snk_pos_in_id:
            self.snk_pos_in_id.write({'active': True})
        else:
            sequence = IrSequenceSudo.create(sequence_data['snk_pos_in_id'])

            values = {
                'name': _('POS IN'),
                'code': 'incoming',
                'use_existing_lots': False,
                'sequence': max_sequence + 1,
                'sequence_code': 'POSIN',
                'company_id': self.company_id.id,
                'default_location_dest_id': input_loc.id,
                'barcode': self.code.replace(" ", "").upper() + "POSIN",
                'sequence_id': sequence.id,
                'warehouse_id': self.id
            }
            self.write({
                'snk_pos_in_id' : PickingType.create(values).id
            })

        if self.snk_pos_out_id:
            self.snk_pos_out_id.write({'active': True})
        else:
            sequence = IrSequenceSudo.create(sequence_data['snk_pos_out_id'])

            values = {
                'name': _('POS OUT'),
                'code': 'outgoing',
                'use_existing_lots': False,
                'sequence': max_sequence + 1,
                'sequence_code': 'POSOUT',
                'company_id': self.company_id.id,
                'default_location_src_id': input_loc.id,
                'barcode': self.code.replace(" ", "").upper() + "POSOUT",
                'sequence_id': sequence.id,
                'warehouse_id': self.id
            }
            self.write({
                'snk_pos_out_id': PickingType.create(values).id
            })


    def snk_inactive_pos_picking_type(self):
        if self.snk_pos_in_id:
            self.snk_pos_in_id.write({'active': False})

        if self.snk_pos_out_id:
            self.snk_pos_out_id.write({'active': False})

    @api.model
    def name_search(self, name='', args=None, operator='ilike', limit=100):
        args = args or []
        domain = args.copy()
        positive_operators = ['=', 'ilike', '=ilike', 'like', '=like']

        if operator in positive_operators:
            if name:
                warehouse = self.search_fetch(expression.AND([domain, [('name', operator, name)]]),
                                                   ['display_name'], limit=limit) \
                                 or self.search_fetch(expression.AND([domain, [('code', operator, name)]]),
                                                      ['display_name'], limit=limit)
                if warehouse:
                    return [(p.id, p.display_name) for p in warehouse.sudo()]

        return super().name_search(name=name, args=args, operator=operator, limit=limit)
