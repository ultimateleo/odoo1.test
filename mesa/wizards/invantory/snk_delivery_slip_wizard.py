import io
import base64
from odoo import models, fields, api
from odoo.exceptions import UserError

class DeliverySlipWizard(models.TransientModel):
    _name = "delivery.slip.wizard"
    _description = "Delivery Slip Wizard"

    slip_format = fields.Selection([
        ('mutasi', 'Mutasi'),
        ('normal', 'Delivery Slip'),
        ('with_price', 'Delivery Slip with Price'),
        ('with_price_v2', 'Delivery Slip with Price v2'),
        ('with_dus', 'Delivery Slip with Dus'),
        ('with_dus_v2', 'Delivery Slip with Dus v2'),
        ('factory', 'Delivery Slip Factory'),
        ('sto', 'Stock Transfer Order'),
        ('sto_v2', 'Stock Transfer Order v2'),
    ], string="Format Slip", default='mutasi', required=True)

    pricelist_id = fields.Many2one(
        'product.pricelist',
        string="Pricelist",
        help="Pricelist to use for pricing (if applicable)"
    )

    def _get_price_from_pricelist(self, product, pricelist, quantity=1.0):
        """Get product price from pricelist"""
        if not pricelist:
            return 0.0
        
        price = pricelist._get_product_price(
            product=product,
            quantity=quantity,
            uom=product.uom_id,
        )
        return price

    def _get_valid_prices_map(self, product_ids=None, tmpl_ids=None):
        """Build dictionary of product prices directly from product_pricelist_item via SQL to speed up extraction"""
        if not self.pricelist_id:
            return {}
            
        today = fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
        
        # Base query parameters
        params = [today, today, self.pricelist_id.id]
        
        # Filter condition for product and template IDs to optimize SQL speed
        filter_clause = ""
        if product_ids or tmpl_ids:
            p_ids = tuple(product_ids) if product_ids else (0,)
            t_ids = tuple(tmpl_ids) if tmpl_ids else (0,)
            filter_clause = "AND (pli.product_id IN %s OR pli.product_tmpl_id IN %s)"
            params.extend([p_ids, t_ids])
            
        query = f"""
            WITH ranked_items AS (
                SELECT
                    pli.id,
                    pli.fixed_price,
                    pli.date_start,
                    pli.date_end,
                    pli.product_id,
                    pli.product_tmpl_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(pli.product_id, 0), COALESCE(pli.product_tmpl_id, 0)
                        ORDER BY pli.date_start DESC NULLS LAST, pli.id DESC
                    ) AS rn
                FROM product_pricelist_item pli
                WHERE (pli.date_start IS NULL OR pli.date_start <= %s)
                AND (pli.date_end IS NULL OR pli.date_end >= %s)
                AND pli.pricelist_id = %s
                {filter_clause}
            ),
            valid_pricelist_items AS (
                SELECT product_id, product_tmpl_id, fixed_price
                FROM ranked_items
                WHERE rn = 1
            )
            SELECT product_id, product_tmpl_id, fixed_price FROM valid_pricelist_items
        """
        self.env.cr.execute(query, params)
        result = self.env.cr.dictfetchall()
        
        price_map = {
            'product_id': {},
            'product_tmpl_id': {}
        }
        for row in result:
            if row['product_id']:
                price_map['product_id'][row['product_id']] = row['fixed_price']
            if row['product_tmpl_id']:
                price_map['product_tmpl_id'][row['product_tmpl_id']] = row['fixed_price']
                
        return price_map

    def _process_picking_data(self, picking, price_map=None):
        """Process picking data with price calculation"""
        moves_with_data = []
        total_amount = 0.0
        
        # Tentukan apakah perlu include price berdasarkan format
        include_price = self.slip_format in ['with_price', 'with_price_v2', 'mutasi', 'sto', 'sto_v2']
        
        for move in picking.move_ids_without_package:
            # Jika status masih draft, ambil dari snk_original_demand atau demand dasar
            # Force demand quantity for STO V2 (always) or other formats if still in draft
            if self.slip_format == 'sto_v2' or picking.state == 'draft':
                quantity = move.product_uom_qty or 0.0
            else:
                # Match logic tanpa wizard: strictly gunakan quantity (done) tanpa fallback ke demand
                quantity = getattr(move, 'quantity', False) or getattr(move, 'quantity_done', False) or 0.0
            
            # Match logic tanpa wizard: lewati move yang tidak memiliki kuantitas
            if not quantity or quantity <= 0:
                continue
            
            # Get price from pricelist if needed
            price = 0.0
            if include_price and self.pricelist_id:
                if price_map is not None:
                    # check product_id specific rule
                    if move.product_id.id in price_map.get('product_id', {}):
                        price = price_map['product_id'][move.product_id.id]
                    # check product template rule
                    elif move.product_id.product_tmpl_id.id in price_map.get('product_tmpl_id', {}):
                        price = price_map['product_tmpl_id'][move.product_id.product_tmpl_id.id]
                    else:
                        # fallback old method if neither matched in map
                        price = self._get_price_from_pricelist(
                            product=move.product_id,
                            pricelist=self.pricelist_id,
                            quantity=quantity
                        )
                else: 
                    # fallback old method
                    price = self._get_price_from_pricelist(
                        product=move.product_id,
                        pricelist=self.pricelist_id,
                        quantity=quantity
                    )
            
            # Calculate subtotal
            subtotal = quantity * price
            total_amount += subtotal
            
            # PENTING: Kirim ID saja, bukan object recordset
            # Object akan di-rebuild di abstract model
            moves_with_data.append({
                'move_id': move.id,
                'product_id': move.product_id.id,
                'quantity': quantity,
                'uom_id': move.product_uom.id,
                'price': price,
                'subtotal': subtotal,
            })
        
        return {
            'picking_id': picking.id,  # Kirim ID saja
            'moves_with_data': moves_with_data,
            'total_amount': total_amount,
        }

    def action_print_report(self):
        """Print delivery slip report based on selected format"""
        # Validasi context: Support multiple records
        picking_ids = self.env.context.get('active_ids')
        if not picking_ids:
             # Fallback to active_id if active_ids is empty
             picking_ids = [self.env.context.get('active_id')] if self.env.context.get('active_id') else []
        
        if not picking_ids:
            raise UserError('No delivery order selected. Please try again.')
        
        # Get picking records
        pickings = self.env['stock.picking'].browse(picking_ids)
        
        if not pickings.exists():
            raise UserError('The selected delivery order(s) do not exist.')
        
        # Tentukan apakah perlu include price berdasarkan format
        include_price = self.slip_format in ['with_price', 'with_price_v2', 'mutasi', 'sto', 'sto_v2']
        
        # [PERFORMANCE OPTIMIZATION] PREFETCH DATA
        # Menghindari N+1 Query ketika mengakses moves dan products
        pickings.mapped('move_ids_without_package.product_id.product_tmpl_id')
        pickings.mapped('move_ids_without_package.product_uom')
        
        # Jika format butuh harga dan pricelist belum ada, set dulu dari picking pertama
        if include_price and not self.pricelist_id and pickings:
            current_pricelist = pickings[0].partner_id.property_product_pricelist or \
                               self.env['product.pricelist'].search([
                                   ('currency_id', '=', pickings[0].company_id.currency_id.id)
                                ], limit=1)
            self.pricelist_id = current_pricelist

        # Ambil semua data harga valid dari database jika report membutuhkan price
        price_map = {}
        if include_price and self.pricelist_id:
            # Dapatkan ID produk unik untuk limitasi query
            moves = pickings.mapped('move_ids_without_package')
            unique_product_ids = moves.mapped('product_id.id')
            unique_tmpl_ids = moves.mapped('product_id.product_tmpl_id.id')
            price_map = self._get_valid_prices_map(product_ids=unique_product_ids, tmpl_ids=unique_tmpl_ids)

        # Process data for ALL selected pickings
        data_list = []
        for picking in pickings:
            # Process picking data
            processed_data = self._process_picking_data(picking, price_map=price_map)
            data_list.append(processed_data)
        
        # Siapkan data untuk report
        data = {
            'include_price': include_price,
            'slip_format': self.slip_format,
            'pricelist_id': self.pricelist_id.id if self.pricelist_id else False,
            'processed_pickings': data_list, 
        }

        # Mapping format ke report ACTION (bukan template!)
        report_mapping = {
            'mutasi': 'mesa.action_report_delivery_mutasi',
            'normal': 'mesa.action_report_delivery_slip',
            'with_price': 'mesa.action_report_delivery_slip_price',
            'with_price_v2': 'mesa.action_report_delivery_slip_price_v2',
            'with_dus': 'mesa.action_report_delivery_slip_dus',
            'with_dus_v2': 'mesa.action_report_delivery_slip_dus_v2',
            'factory': 'mesa.action_report_delivery_slip_factory',
            'sto': 'mesa.action_report_delivery_sto',
            'sto_v2': 'mesa.action_report_delivery_sto_v2',
        }

        # Get report reference
        report_ref = report_mapping.get(self.slip_format, 'mesa.report_delivery_slip')
        
        try:
            report = self.env.ref(report_ref)
        except ValueError:
            raise UserError(
                'Report "%s" not found. Please check your module configuration.' % report_ref
            )
        
        # Gunakan jalur cepat (standard rendering) untuk performa tinggi, 
        # kecuali format 'mutasi' yang butuh penomoran halaman mandiri per dokumen.
        if self.slip_format == 'mutasi' and len(pickings) > 1:
            return self._render_merged_pdf(report, report_ref, pickings, data_list, data)
        
        return report.report_action(pickings.ids, data=data)

    def _render_merged_pdf(self, report, report_ref, pickings, data_list, base_data):
        """
        Render setiap picking sebagai PDF terpisah lalu merge.
        Tujuan: supaya page/topage counter independent per dokumen.
        """
        try:
            from pypdf import PdfWriter, PdfReader
        except ImportError:
            from PyPDF2 import PdfWriter, PdfReader

        processed_map = {p['picking_id']: p for p in data_list}
        writer = PdfWriter()

        for picking in pickings:
            single_data = dict(base_data)
            single_data['processed_pickings'] = [processed_map[picking.id]]

            pdf_content, _ = report._render_qweb_pdf(
                report_ref, [picking.id], data=single_data
            )
            reader = PdfReader(io.BytesIO(pdf_content))
            for page in reader.pages:
                writer.add_page(page)

        output = io.BytesIO()
        writer.write(output)
        
        # Ambil label murni dari selection field tanpa fallback string
        slip_label = dict(self._fields['slip_format'].selection).get(self.slip_format)
        timestamp = fields.Datetime.context_timestamp(self, fields.Datetime.now()).strftime('%Y-%m-%d %H:%M:%S')
        
        names = f'{slip_label} - {timestamp}'
        
        attachment = self.env['ir.attachment'].create({
            'name': names + '.pdf',
            'datas': base64.b64encode(output.getvalue()),
            'res_model': self._name,
            'res_id': self.id,
            'type': 'binary',
            'mimetype': 'application/pdf',
        })

        return {
            'type': 'ir.actions.act_url',
            'url': f'/web/content/{attachment.id}?download=true',
            'target': 'self',
        }