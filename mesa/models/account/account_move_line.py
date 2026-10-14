from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    snk_description = fields.Text(string="Description")
    snk_material_code = fields.Char(string="Material Code")
    # Imports may override the snapshot; readonly belongs in the view.
    # Copies/reversals take a fresh master snapshot when their lines are created.
    snk_grade_id = fields.Many2one(
        "snk.product.grade", string="Grade", index=True,
        copy=False, ondelete='restrict',
    )

    @api.model
    def _check_snk_grade_access(self, grade_ids):
        grades = self.env['snk.product.grade'].browse(set(grade_ids) - {False, None})
        if grades:
            grades.check_access('read')
            if len(grades.exists()) != len(grades):
                raise ValidationError(_("The imported grade does not exist."))

    @api.model
    def _prepare_create_values(self, vals_list):
        # Reuse final/default products after core preparation, before INSERT.
        vals_list = super()._prepare_create_values(vals_list)
        fallback_vals = [vals for vals in vals_list if 'snk_grade_id' not in vals]
        products = self.env['product.product'].browse({
            vals['product_id'] for vals in fallback_vals if vals.get('product_id')
        })
        grade_by_product = {product.id: product.snk_grade_id.id for product in products}
        for vals in fallback_vals:
            vals['snk_grade_id'] = grade_by_product.get(vals.get('product_id')) or False
        self._check_snk_grade_access([vals['snk_grade_id'] for vals in vals_list])
        return vals_list

    def write(self, vals):
        if 'snk_grade_id' in vals:
            self._check_snk_grade_access([vals['snk_grade_id']])
            return super().write(vals)
        if 'product_id' not in vals:
            return super().write(vals)

        changed = self.filtered(lambda line: line.product_id.id != vals['product_id'])
        if not changed:
            return super().write(vals)

        product = self.env['product.product'].browse(vals['product_id'])
        grade_id = product.snk_grade_id.id or False
        self._check_snk_grade_access([grade_id])
        unchanged = self - changed
        # A mixed batch must preserve same-product history. Keep both writes
        # atomic and retain all accounting checks in the normal super chain.
        with self.env.cr.savepoint():
            if unchanged:
                super(AccountMoveLine, unchanged).write(dict(vals))
            return super(AccountMoveLine, changed).write(dict(vals, snk_grade_id=grade_id))
