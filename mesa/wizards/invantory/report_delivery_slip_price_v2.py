from odoo import models
from odoo.exceptions import UserError
import logging
_logger = logging.getLogger(__name__)

class ReportDeliverySlipPriceV2(models.AbstractModel):
    _name = 'report.mesa.report_delivery_slip_price_v2_template'
    _description = 'Report Delivery Slip with Price V2'
    
    def _get_report_values(self, docids, data=None):
        """
        Override method untuk menyiapkan data untuk report.
        Data sudah diproses oleh wizard dan dikirim via parameter 'data'.
        """
        
        # Jika data tidak ada (direct print), siapkan data default
        if not data or not data.get('processed_pickings'):
            # Get picking records
            pickings = self.env['stock.picking'].browse(docids)
            
            # Process setiap picking
            processed_pickings = []
            empty_pickings = []
            for picking in pickings:
                moves_with_data = []

                # Gunakan stock.move untuk semua status agar konsisten mendapatkan data demand & done
                lines = picking.move_ids_without_package

                for rec in lines:
                    # Logic: Draft = Demand, Else = Done/Quantity
                    if picking.state == 'draft':
                        quantity = rec.product_uom_qty or 0.0
                    else:
                        quantity = getattr(rec, 'quantity', 0.0) or getattr(rec, 'qty_done', 0.0)

                    if not quantity or quantity <= 0:
                        continue

                    moves_with_data.append({
                        'move': rec,
                        'product': rec.product_id,
                        'grade_name': rec.snk_grade_id.snk_name if rec.snk_grade_id else '',
                        'quantity': quantity,
                        'uom': rec.product_uom,
                        'price': 0.0,
                        'subtotal': 0.0,
                    })
                
                if not moves_with_data:
                    empty_pickings.append(picking.name)
                    continue

                # Sort by product default_code
                moves_with_data.sort(key=lambda x: x['product'].default_code or '')
                
                processed_pickings.append({
                    'picking': picking,
                    'moves_with_data': moves_with_data,
                    'total_amount': 0.0,
                })
            
            if empty_pickings:
                raise UserError("These picking has no lines:\n" + "\n".join(empty_pickings))

            return {
                'doc_ids': docids,
                'doc_model': 'stock.picking',
                'docs': pickings,
                'processed_pickings': processed_pickings,
                'include_price': False,
                'slip_format': 'with_price_v2',
                'pricelist': False,
            }
        
        # Jika data ada (dari wizard), rebuild objects dari IDs
        processed_pickings_with_objects = []
        try:
            pic_data = data.get('processed_pickings', [])
            
            # 1. Collect all IDs for bulk pre-fetching
            all_picking_ids = [x['picking_id'] for x in pic_data if 'picking_id' in x]
            all_move_ids = []
            all_product_ids = []
            all_uom_ids = []
            for p in pic_data:
                for m in p.get('moves_with_data', []):
                    if m.get('move_id'): all_move_ids.append(m['move_id'])
                    if m.get('product_id'): all_product_ids.append(m['product_id'])
                    if m.get('uom_id'): all_uom_ids.append(m['uom_id'])
            
            # 2. Bulk Browse to fill cache
            pickings_map = {p.id: p for p in self.env['stock.picking'].browse(all_picking_ids)}
            moves_map = {m.id: m for m in self.env['stock.move'].browse(all_move_ids)}
            products_map = {p.id: p for p in self.env['product.product'].browse(all_product_ids)}
            uoms_map = {u.id: u for u in self.env['uom.uom'].browse(all_uom_ids)}
            
            # 3. Main Processing Loop
            empty_pickings = []
            for picking_data in pic_data:
                picking = pickings_map.get(picking_data['picking_id'])
                if not picking:
                    continue
                
                moves_with_objects = []
                for move_data in picking_data['moves_with_data']:
                    move = moves_map.get(move_data['move_id'])
                    product = products_map.get(move_data['product_id'])
                    uom = uoms_map.get(move_data['uom_id'])
                    
                    if not move or not product or move_data['quantity'] <= 0:
                        continue
                    
                    moves_with_objects.append({
                        'move': move,
                        'product': product,
                        'grade_name': move.snk_grade_id.snk_name if move.snk_grade_id else '',
                        'quantity': move_data['quantity'],
                        'uom': uom,
                        'price': move_data['price'],
                        'subtotal': move_data['subtotal'],
                    })
                
                if not moves_with_objects:
                    empty_pickings.append(picking.name)
                    continue

                # Sort by product default_code
                moves_with_objects.sort(key=lambda x: x['product'].default_code or '')
                
                processed_pickings_with_objects.append({
                    'picking': picking,
                    'moves_with_data': moves_with_objects,
                    'total_amount': picking_data['total_amount'],
                })

            if empty_pickings:
                raise UserError("These picking has no lines:\n" + "\n".join(empty_pickings))

        except KeyError as e:
            _logger.error(f"Structure Error in Wizard Data: Missing key {e}")
            raise UserError(f"Data report tidak valid (missing key: {e}). Silakan coba proses ulang.")
        except Exception as e:
             if isinstance(e, UserError):
                 raise e
             _logger.error(f"Unexpected error in Price V2 report: {e}")
             raise UserError(f"Terjadi kesalahan saat memproses data: {e}")
        
        # Rebuild pricelist object dari ID
        pricelist = False
        if data.get('pricelist_id'):
            pricelist = self.env['product.pricelist'].browse(data['pricelist_id'])
        
        return {
            'doc_ids': docids,
            'doc_model': 'stock.picking',
            'docs': self.env['stock.picking'].browse(docids),
            'processed_pickings': processed_pickings_with_objects,
            'include_price': data.get('include_price', False),
            'slip_format': data.get('slip_format', 'with_price_v2'),
            'pricelist': pricelist,
        }
