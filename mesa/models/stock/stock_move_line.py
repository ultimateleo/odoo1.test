from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class StockMoveLine(models.Model):
    _inherit = "stock.move.line"

    # Writable for imports; readonly belongs in the transaction views.
    snk_grade_id = fields.Many2one(
        "snk.product.grade", string="Grade", index=True, copy=True,
        ondelete='restrict',
    )
    analytic_id = fields.Many2one("account.analytic.account", string="Brand", store=True,
                                  domain=[('snk_brand', '=', True)], related='product_id.analytic_id')
    event_ids = fields.Many2many("project.project", string="Events")
    snk_material_code = fields.Char("Material Code", related="product_id.snk_material_code")
    snk_notes = fields.Char("Notes")

    # @api.constrains('origin') ### dikomen karena sudah di-handle di stock.move, kalau gak jadi error index.
    # def constrains_origin(self):
    #     for rec in self:
    #         if not rec.origin:
    #             rec.origin = rec.move_id.origin

    @api.model
    def _snk_grade_from_parent(self, move, product_id, company_id):
        if move.product_id.id != product_id or move.company_id.id != company_id:
            raise ValidationError(_("The detail and its stock move must have the same product and company."))
        return move.snk_grade_id.id or False

    @api.onchange('product_id', 'move_id')
    def _onchange_snk_grade(self):
        for line in self:
            line.snk_grade_id = (
                line.move_id.snk_grade_id if line.move_id
                else line.product_id.snk_grade_id
            )

    @api.model_create_multi
    def create(self, vals_list):
        # Core needs these defaults before its quant/company preprocessing.
        defaults = self.default_get(['product_id', 'move_id', 'picking_id', 'company_id', 'quant_id'])
        vals_list = [dict(defaults, **vals) for vals in vals_list]
        moves = self.env['stock.move'].browse({v['move_id'] for v in vals_list if v.get('move_id')})
        move_by_id = {move.id: move for move in moves}
        for vals in vals_list:
            if vals.get('move_id') and not vals.get('quant_id'):
                vals.setdefault('product_id', move_by_id[vals['move_id']].product_id.id)
        return super().create(vals_list)

    @api.model
    def _prepare_create_values(self, vals_list):
        # Stock core has already resolved quant -> product. This batch hook runs
        # before INSERT and before core attaches a detail or creates its move.
        vals_list = super()._prepare_create_values(vals_list)
        moves = self.env['stock.move'].browse({v['move_id'] for v in vals_list if v.get('move_id')})
        pickings = self.env['stock.picking'].browse({v['picking_id'] for v in vals_list if v.get('picking_id')})
        move_by_id = {move.id: move for move in moves}
        picking_by_id = {picking.id: picking for picking in pickings}
        fallback_vals = []
        for vals in vals_list:
            move = move_by_id.get(vals.get('move_id'))
            picking = picking_by_id.get(vals.get('picking_id'))
            if move:
                grade_id = self._snk_grade_from_parent(
                    move, vals.get('product_id'), vals.get('company_id') or move.company_id.id,
                )
                if 'snk_grade_id' in vals and (vals['snk_grade_id'] or False) != grade_id:
                    raise ValidationError(_("Import grade corrections on the stock move, not on an individual detail."))
                vals['snk_grade_id'] = grade_id
            elif 'snk_grade_id' not in vals:
                candidates = picking.move_ids.filtered(
                    lambda candidate: candidate.product_id.id == vals.get('product_id')
                ) if picking and picking.state != 'done' else self.env['stock.move']
                grades = {candidate.snk_grade_id.id or False for candidate in candidates}
                if len(grades) > 1:
                    raise ValidationError(_("Several moves have different grades for this product. Select the stock move explicitly."))
                if grades:
                    vals['snk_grade_id'] = grades.pop()
                else:
                    fallback_vals.append(vals)
        # Import overrides and parent snapshots do not need a master grade read.
        products = self.env['product.product'].browse({
            vals['product_id'] for vals in fallback_vals if vals.get('product_id')
        })
        grade_by_product = {product.id: product.snk_grade_id.id for product in products}
        for vals in fallback_vals:
            vals['snk_grade_id'] = grade_by_product.get(vals.get('product_id')) or False
        self.env['stock.move']._check_snk_grade_access([v['snk_grade_id'] for v in vals_list])
        return vals_list

    def _write_snk_quant_values(self, vals):
        # Let the complete stock/stock_account chain receive quant_id unchanged.
        # Reuse the resulting product, rather than resolving the quant twice.
        before = {
            line.id: (line.product_id.id, line.move_id.id, line.state)
            for line in self
        }
        if 'snk_grade_id' in vals:
            self.env['stock.move']._check_snk_grade_access([vals['snk_grade_id']])
        with self.env.cr.savepoint():
            result = super(StockMoveLine, self).write(dict(vals))
            updates = {}
            fallback_lines = self.browse()
            for line in self:
                old_product_id, old_move_id, old_state = before[line.id]
                product_changed = line.product_id.id != old_product_id
                # Core checks product_id before resolving quant_id. Preserve the
                # draft-only guard also when only the quant changes the product.
                if product_changed and vals.get('state', old_state) != 'draft':
                    raise UserError(_("Changing the product is only allowed in 'Draft' state."))
                if line.move_id:
                    grade_id = self._snk_grade_from_parent(
                        line.move_id, line.product_id.id, line.company_id.id,
                    )
                    if 'snk_grade_id' in vals and (vals['snk_grade_id'] or False) != grade_id:
                        raise ValidationError(_("Import grade corrections on the stock move, not on an individual detail."))
                    if product_changed or line.move_id.id != old_move_id:
                        updates.setdefault(grade_id, self.browse())
                        updates[grade_id] |= line
                elif product_changed and 'snk_grade_id' not in vals:
                    fallback_lines |= line
            # Prefetch only products that really require a new master snapshot.
            grades = {product.id: product.snk_grade_id.id or False
                      for product in fallback_lines.product_id}
            for line in fallback_lines:
                grade_id = grades[line.product_id.id]
                updates.setdefault(grade_id, self.browse())
                updates[grade_id] |= line
            for grade_id, lines in updates.items():
                changed = lines.filtered(lambda line: line.snk_grade_id.id != grade_id)
                if changed:
                    changed.write({'snk_grade_id': grade_id})
            return result

    def write(self, vals):
        if vals.get('quant_id'):
            return self._write_snk_quant_values(dict(vals))
        if not {'snk_grade_id', 'move_id', 'product_id', 'quant_id', 'company_id'}.intersection(vals):
            return super().write(vals)
        vals = dict(vals)
        if 'snk_grade_id' in vals:
            self.env['stock.move']._check_snk_grade_access([vals['snk_grade_id']])
        groups = {}
        unchanged_grade = object()
        new_move = self.env['stock.move'].browse(vals.get('move_id'))
        product = self.env['product.product'].browse(vals.get('product_id'))
        for line in self:
            move = new_move if 'move_id' in vals else line.move_id
            product_id = vals.get('product_id', line.product_id.id)
            grade_id = vals.get('snk_grade_id', unchanged_grade)
            if move:
                parent_grade = self._snk_grade_from_parent(
                    move, product_id, vals.get('company_id', line.company_id.id),
                )
                if grade_id is not unchanged_grade and (grade_id or False) != parent_grade:
                    raise ValidationError(_("Import grade corrections on the stock move, not on an individual detail."))
                if move != line.move_id or product_id != line.product_id.id:
                    grade_id = parent_grade
            elif product_id != line.product_id.id and grade_id is unchanged_grade:
                grade_id = product.snk_grade_id.id or False
            groups.setdefault(grade_id, self.browse())
            groups[grade_id] |= line
        with self.env.cr.savepoint():
            for grade_id, lines in groups.items():
                line_vals = dict(vals)
                if grade_id is not unchanged_grade:
                    line_vals['snk_grade_id'] = grade_id
                super(StockMoveLine, lines).write(line_vals)
        return True

    def _get_linkable_moves(self):
        # Core chooses a same-product move; also require the same snapshot.
        return [move for move in super()._get_linkable_moves()
                if move.snk_grade_id == self.snk_grade_id]

    def _prepare_stock_move_vals(self):
        vals = super()._prepare_stock_move_vals()
        vals['snk_grade_id'] = self.snk_grade_id.id or False
        return vals

    def update_scheduled_date(self):
        # Filter hanya baris yang scheduled_date-nya berbeda dengan picking-nya
        records_to_process = self.filtered(lambda r: r.picking_id.scheduled_date != r.scheduled_date)
        
        # Ambil move_id dari baris yang tersaring
        moves = records_to_process.mapped('move_id')
        
        if moves:
            # Panggil method update yang sudah menggunakan SQL di stock.move
            moves.update_scheduled_date()
            
        # Invalidate cache untuk move line juga agar tampilan sinkron
        self.invalidate_recordset(['scheduled_date'])
                
            
