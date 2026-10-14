# -*- coding: utf-8 -*-
from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning
import re
import logging
from odoo.tools.float_utils import float_compare

_logger = logging.getLogger(__name__)

DAYS = [
    ('0','Minggu'),
    ('1','Senin'),
    ('2','Selasa'),
    ('3','Rabu'),
    ('4','Kamis'),
    ('5','Jumat'),
    ('6','Sabtu')
]

class SnkStockRule(models.Model):
    _name = 'snk.stock.rule'
    _description = "SNK Stock Rule"

    site_id = fields.Many2one('snk.site', string='Site', required=True, help="Site")
    partner_id = fields.Many2one('res.partner', related='site_id.partner_id', string='Delivery Address', required=True)
    product_id = fields.Many2one("product.product", string="Product")
    analytic_id = fields.Many2one("account.analytic.account", string="Brand", related="product_id.analytic_id", store=True)
    # werehouse_id = fields.Many2one("stock.warehouse", string="Warehouse", related="site_id.warehouse_id", store=True)
    warehouse_ids = fields.Many2many("stock.warehouse", compute="_compute_warehouse_ids", string="Warehouse")
    location_ids = fields.Many2many("stock.location", string="Location",compute="_compute_location_id", store=True, readonly=False, precompute=True,ondelete="cascade", required=True, check_company=True)
    min_stock = fields.Float("Min Stock")
    max_stock = fields.Float("Max Stock")
    days = fields.Selection(DAYS, string="Days", index=True,copy=False)
    company_id = fields.Many2one('res.company', 'Company', required=True, index=True,default=lambda self: self.env.company)
    active = fields.Boolean("Active", default=True)
    grade_id = fields.Many2one("snk.product.grade", string="Grade", related="product_id.snk_grade_id", store=True)
    route_ids = fields.Many2many("stock.route", string="Route")
    available_qty = fields.Float(string="On Hand", compute="_compute_available_qty", readonly=True)
    available_route_ids = fields.Many2many("stock.route", string="Available", compute="_compute_avail_route")
    reorder_point = fields.Float(string="Reorder Points")
    target_point = fields.Float(string="Target Order Points")
    

    # ubah type field atau tambahkan field
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', related='site_id.warehouse_id', store=True, readonly=False,)
    location_id = fields.Many2one('stock.location', string='Location', related='site_id.warehouse_id.lot_stock_id', store=True, readonly=False, precompute=True,)
    to_order = fields.Float(string="To Order", compute="_compute_to_order", store=True)
    source_warehouse_id = fields.Many2one('stock.warehouse',string='Source Warehouse', related='source_site_id.warehouse_id', store=True, readonly=False)
    source_site_id = fields.Many2one('snk.site', string='Source Site')
    template_reference = fields.Char(
        string="Template Reference",
        related='product_id.product_tmpl_id.default_code',
        store=True,
    )
    variant_reference = fields.Char(
        string="Variant Reference",
        related='product_id.default_code',
        store=True,
    )

    @api.depends("site_id.warehouse_id")
    def _compute_warehouse_ids(self):
        for rec in self:
            rec.warehouse_ids = rec.site_id.warehouse_id if rec.site_id and rec.site_id.warehouse_id else False


    @api.depends('warehouse_ids','company_id')
    def _compute_location_id(self):
        for rule in self:
            location_ids = False
            warehouse_ids = rule.warehouse_ids
            if warehouse_ids:
                location_ids = []
                for wh in warehouse_ids:
                    location_ids.append(wh.lot_stock_id.id)
            rule.location_ids = location_ids
    


    @api.constrains('warehouse_ids')
    def _check_warehouse_routes(self):
        for rec in self:
            for warehouse in rec.warehouse_ids:
                route_exist = self.env['stock.route'].search_count([
                    ('supplied_wh_id', '=', warehouse.id)
                ])
                if not route_exist:
                    raise ValidationError(_(
                        "Warehouse %s tidak memiliki route supply."
                    ) % warehouse.display_name)

    @api.constrains('product_id', 'days', 'site_id')
    def _check_unique_product_days_site(self):
        for rec in self:
            if not rec.product_id or not rec.site_id:
                continue
            domain = [
                ('id', '!=', rec.id),
                ('product_id', '=', rec.product_id.id),
                ('days', '=', rec.days),
                ('site_id', '=', rec.site_id.id),
            ]
            duplicate = self.search(domain, limit=1)
            if duplicate:
                days_label = dict(DAYS).get(rec.days, rec.days)
                raise ValidationError(_(
                    "Rule dengan kombinasi produk '%s', hari '%s', dan site '%s' sudah ada."
                ) % (rec.product_id.display_name, days_label, rec.site_id.snk_name))

    @api.depends("warehouse_ids")
    def _compute_avail_route(self):
        for rec in self:
            if not rec.warehouse_ids:
                rec.available_route_ids = [(6, 0, [])]
                continue

            routes = rec.warehouse_ids._get_all_routes()
            filtered_routes = routes.filtered(lambda r: r.supplied_wh_id.id in rec.warehouse_ids.ids)
            rec.available_route_ids = [(6, 0, filtered_routes.ids)]

    @api.depends('location_id', 'product_id')
    def _compute_available_qty(self):
        """
        Compute on-hand quantity for this stock rule.
        On Hand = Free To Use + Incoming, so stock already in transit counts toward availabilty and doesn't trgger extra reorder.
        """
        for rec in self:
            if not rec.location_id or not rec.product_id:
                rec.available_qty = 0.0
                continue

            try:
                # Context 'location' auto-resolves child_of hierarchy via ORM
                product = rec.product_id.with_context(location=rec.location_id.id)
                rec.available_qty = product.free_qty + product.incoming_qty
            except Exception as e:
                _logger.warning(
                    "Failed to compute available_qty for rule %s (product=%s, location=%s): %s",
                    rec.id, rec.product_id.id, rec.location_id.id, str(e)
                )
                rec.available_qty = 0.0

    @api.depends('days', 'product_id')
    def _compute_display_name(self):
        for rec in self:
            days_label = dict(self._fields['days'].selection).get(rec.days, '')
            if not rec.product_id:
                rec.display_name = days_label

            rec.display_name = f"{days_label} - {rec.product_id.name}"


    @api.depends('min_stock', 'available_qty', 'max_stock')
    def _compute_to_order(self):
        precision = self.env['decimal.precision'].precision_get(
            'Product Unit of Measure')
        for rec in self:
            if float_compare(rec.available_qty, rec.min_stock, precision_digits=precision) <= 0:
                rec.to_order = max(rec.max_stock - rec.available_qty, 0)
            else:
                rec.to_order = 0.0
