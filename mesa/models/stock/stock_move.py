from odoo import api, fields, models, _
from odoo.exceptions import AccessError, ValidationError
import logging

_logger = logging.getLogger(__name__)


class StockMove(models.Model):
    _inherit = "stock.move"

    # Writable for imports; readonly belongs in the transaction views.
    snk_grade_id = fields.Many2one(
        "snk.product.grade", string="Grade", index=True, copy=True,
        ondelete='restrict',
    )
    analytic_id = fields.Many2one("account.analytic.account", string="Brand", store=True,
                                  domain=[('snk_brand', '=', True)], related='product_id.analytic_id')
    event_ids = fields.Many2many("project.project", copy=True, string="Events", store=True, compute='_compute_event_ids')
    snk_material_code = fields.Char("Material Code", related="product_id.snk_material_code")
    incoming_qty = fields.Float('Incoming', compute='_compute_incoming_qty')
    snk_original_demand = fields.Float(
        string="Original Demand",
        help="Simpan nilai mutlak demand pertama kali (qty_demand) agar tidak terpengaruh re-calculation Odoo saat partial/backorder.",
        readonly=True,
        copy=False
    )

    @api.model
    def _check_snk_grade_access(self, grade_ids):
        grades = self.env['snk.product.grade'].browse(set(grade_ids) - {False, None})
        grades.check_access('read')
        if len(grades.exists()) != len(grades):
            raise ValidationError(_("The imported grade does not exist."))

    @api.onchange('product_id')
    def _onchange_product_id_snk_grade(self):
        for move in self:
            move.snk_grade_id = move.product_id.snk_grade_id

    def _sync_snk_grade_to_move_lines(self):
        # Only explicit corrections / actual product changes call this method.
        # Do not repair historical differences during unrelated writes.
        for grade, moves in self.grouped('snk_grade_id').items():
            lines = moves.move_line_ids.filtered(lambda line: line.snk_grade_id != grade)
            if lines:
                lines.write({'snk_grade_id': grade.id or False})

    def write(self, vals):
        if 'product_id' in vals and 'snk_grade_id' not in vals:
            changed = self.filtered(lambda move: move.product_id.id != vals['product_id'])
            if changed:
                product = self.env['product.product'].browse(vals['product_id'])
                # The same batch may contain moves whose product did not change.
                with self.env.cr.savepoint():
                    (self - changed).write(dict(vals))
                    changed.write(dict(vals, snk_grade_id=product.snk_grade_id.id or False))
                return True
        if 'snk_grade_id' in vals:
            self.check_access('write')
            lines = self.move_line_ids
            # Security: One2many reads may hide lines through record rules.
            # Elevate ONLY a count over already-authorized moves, never a write
            # or a data read. Refuse the correction if any detail is invisible.
            total_lines = self.env['stock.move.line'].sudo().search_count([
                ('move_id', 'in', self.ids),
            ])
            if total_lines != len(lines):
                raise AccessError(_("You must have access to every detail before correcting this move's grade."))
            lines.check_access('write')
            self._check_snk_grade_access([vals['snk_grade_id']])
            # Import handles batches in savepoints too; keep this correction
            # atomic even when another caller catches the exception itself.
            with self.env.cr.savepoint():
                result = self._write_snk_move_values(dict(vals))
                self._sync_snk_grade_to_move_lines()
            return result
        return self._write_snk_move_values(dict(vals))

    def _write_snk_move_values(self, vals):
        result = super(StockMove, self).write(vals)
        for move in self:
            if not move.origin and move.picking_id.origin:
                move.origin = move.picking_id.origin
                
            # Update snk_original_demand ketika demand diupdate saat status masih draft
            if 'product_uom_qty' in vals and move.state == 'draft':
                move.snk_original_demand = vals.get('product_uom_qty')
                
        return result

    @api.depends('product_id', 'location_id', 'company_id')
    def _compute_incoming_qty(self):
        self.incoming_qty = 0.0
        moves = self.filtered(lambda move: move.product_id and move.location_id)
        if not moves:
            return

        # A picking normally has one source location. Resolve its warehouse once
        # for the whole batch instead of repeating the same search per row.
        moves_by_scope = {}
        for move in moves:
            key = (move.company_id, move.location_id)
            moves_by_scope.setdefault(key, self.browse())
            moves_by_scope[key] |= move

        moves_by_warehouse = {}
        Warehouse = self.env['stock.warehouse']
        for (company, location), scoped_moves in moves_by_scope.items():
            warehouse = Warehouse.search([
                ('company_id', '=', company.id),
                ('lot_stock_id', 'child_of', location.id),
            ], limit=1)
            if warehouse:
                key = (company, warehouse)
                moves_by_warehouse.setdefault(key, self.browse())
                moves_by_warehouse[key] |= scoped_moves

        # Aggregate all products in a warehouse with one query. _read_group()
        # keeps record rules and avoids loading every matching move in Python.
        for (company, warehouse), scoped_moves in moves_by_warehouse.items():
            incoming_by_product = {
                product.id: quantity
                for product, quantity in self.env['stock.move']._read_group([
                    ('company_id', '=', company.id),
                    ('product_id', 'in', scoped_moves.product_id.ids),
                    ('state', 'in', ['confirmed', 'waiting', 'assigned']),
                    ('picking_type_id.code', '=', 'incoming'),
                    ('location_dest_id', 'child_of', warehouse.lot_stock_id.id),
                ], ['product_id'], ['product_uom_qty:sum'])
            }
            for move in scoped_moves:
                move.incoming_qty = incoming_by_product.get(move.product_id.id, 0.0)

    @api.depends('sale_line_id')
    def _compute_event_ids(self):
        for rec in self:
            if rec.event_ids:
                continue

            if rec.sale_line_id:
                rec.event_ids = rec.sale_line_id.event_ids
            else:
                rec.event_ids = False

    def _prepare_move_line_vals(self, quantity=None, reserved_quant=None):
        self.ensure_one()
        move_vals = super()._prepare_move_line_vals(quantity, reserved_quant)
        move_vals['snk_grade_id'] = self.snk_grade_id.id or False
        if self.event_ids:
            move_vals.update({
                'event_ids': self.event_ids.ids,
            })
        return move_vals

    def action_add_from_catalog(self):
        return super().action_add_from_catalog()

    def action_add_from_catalog_picking(self):
        order = self.env['stock.picking'].browse(self.env.context.get('order_id'))
        return order.with_context(child_field='move_ids_without_package').action_add_from_catalog()

    @api.model_create_multi
    def create(self, vals_list):
        # Resolve context/default products as well as explicitly supplied ones.
        defaults = self.default_get([
            'product_id', 'sale_line_id', 'origin_returned_move_id',
            'company_id', 'picking_id',
        ])
        vals_list = [dict(defaults, **vals) for vals in vals_list]
        products = self.env['product.product'].browse({
            vals['product_id'] for vals in vals_list if vals.get('product_id')
        })
        grade_by_product = {product.id: product.snk_grade_id.id for product in products}
        returns = self.browse({
            vals['origin_returned_move_id'] for vals in vals_list
            if vals.get('origin_returned_move_id')
        })
        sales = self.env['sale.order.line'].browse({
            vals['sale_line_id'] for vals in vals_list if vals.get('sale_line_id')
        })
        return_by_id = {move.id: move for move in returns}
        sale_by_id = {line.id: line for line in sales}
        for vals in vals_list:
            source = return_by_id.get(vals.get('origin_returned_move_id'))
            if not source or source.product_id.id != vals.get('product_id'):
                source = sale_by_id.get(vals.get('sale_line_id'))
            matching_source = source and source.product_id.id == vals.get('product_id')
            if matching_source and source.company_id.id != vals.get('company_id'):
                raise ValidationError(_("The grade source must belong to the move's company."))
            if 'snk_grade_id' in vals:
                continue  # An explicit False is a valid snapshot, too.
            if matching_source:
                vals['snk_grade_id'] = source.snk_grade_id.id or False
            else:
                vals['snk_grade_id'] = grade_by_product.get(vals.get('product_id')) or False
        self._check_snk_grade_access([vals['snk_grade_id'] for vals in vals_list])
        for vals in vals_list:
            # Auto-generate name from product if not provided (useful for imports)
            if not vals.get('name') and vals.get('product_id'):
                product = self.env['product.product'].browse(vals['product_id'])
                vals['name'] = product.display_name or product.name or '/'
                
            # Save original demand safely
            if 'product_uom_qty' in vals:
                vals['snk_original_demand'] = vals['product_uom_qty']
            
            picking = self.env['stock.picking'].browse(vals.get('picking_id')) if vals.get('picking_id') else False
            if picking and picking.origin:
                vals['origin'] = picking.origin
                _logger.debug(f"Picking origin {picking.origin} in picking")
                _logger.debug(f"Picking origin {vals.get('origin')} in move values")

            # Preserve picking's scheduled_date: jangan biarkan move baru pakai default now()
            # karena _compute_scheduled_date mengambil min(move_ids.date) Ã¢â€ â€™ bisa reset ke hari ini
            # if picking and picking.scheduled_date:
            #     vals['date'] = picking.scheduled_date
            #     _logger.info(f"Picking scheduled_date {picking.scheduled_date} in picking")
            #     _logger.info(f"Picking scheduled_date {vals.get('date')} in move values")
            # if picking and picking.date_deadline:
            #     vals['date_deadline'] = picking.date_deadline
            #     _logger.info(f"Picking date_deadline {picking.date_deadline} in picking")
            #     _logger.info(f"Picking date_deadline {vals.get('date_deadline')} in move values")

        return super(StockMove, self).create(vals_list)

    def copy_data(self, default=None):
        defaults = dict(default or {})
        if 'product_id' in defaults and 'snk_grade_id' not in defaults:
            # Kit explosion can copy a move to a different component product.
            # Ordinary copies, returns and backorders retain the source grade.
            vals_list = []
            for move in self:
                move_defaults = dict(defaults)
                if move.product_id.id != defaults['product_id']:
                    product = self.env['product.product'].browse(defaults['product_id'])
                    move_defaults['snk_grade_id'] = product.snk_grade_id.id or False
                vals_list.extend(super(StockMove, move).copy_data(move_defaults))
            return vals_list
        return super().copy_data(defaults)

    def _prepare_procurement_values(self):
        values = super()._prepare_procurement_values()
        values['snk_grade_id'] = self.snk_grade_id.id or False
        values['_snk_grade_product_id'] = self.product_id.id
        return values

    @api.model
    def _prepare_merge_moves_distinct_fields(self):
        return super()._prepare_merge_moves_distinct_fields() + ['snk_grade_id']

    def update_scheduled_date(self):
        if not self:
            return
        # SQL Update massal (Gunakan koma, bukan AND untuk memisahkan kolom)
        query = """
            UPDATE stock_move sm
            SET date = sp.scheduled_date,
                date_deadline = sp.date_deadline
            FROM stock_picking sp
            WHERE sm.picking_id = sp.id
            AND sm.id IN %s
        """
        self.env.cr.execute(query, (tuple(self.ids),))
        self.invalidate_recordset(['date', 'date_deadline'])

    