from odoo import models

class ReportDeliverySlipPrice(models.AbstractModel):
    _name = 'report.mesa.report_delivery_slip_template'
    _description = 'Report Delivery Slip with Price'
    
    def _get_report_values(self, docids, data=None):
        """
        Override method untuk menyiapkan data untuk report.
        Data sudah diproses oleh wizard dan dikirim via parameter 'data'.
        """
        import logging
        _logger = logging.getLogger(__name__)
        
        # Jika data tidak ada (direct print), siapkan data default
        if not data:
            # Get picking records
            pickings = self.env['stock.picking'].browse(docids)
            
            # Process setiap picking
            processed_pickings = []
            for picking in pickings:
                moves_with_data = []
                for move in picking.move_ids_without_package:
                    # Try different field names for done quantity (varies by Odoo version)
                    quantity = getattr(move, 'quantity', False) or getattr(move, 'quantity_done', False) or move.product_uom_qty
                    if quantity <= 0:
                        continue
                        
                    moves_with_data.append({
                        'move': move,
                        'product': move.product_id,
                        'quantity': quantity,
                        'uom': move.product_uom,
                        'price': 0.0,
                        'subtotal': 0.0,
                    })
                
                processed_pickings.append({
                    'picking': picking,
                    'moves_with_data': moves_with_data,
                    'total_amount': 0.0,
                })
            
            return {
                'doc_ids': docids,
                'doc_model': 'stock.picking',
                'docs': pickings,
                'processed_pickings': processed_pickings,
                'include_price': False,
                'slip_format': 'with_price',
                'pricelist': False,
            }
        
        # DEBUG: Log data yang diterima
        _logger.info("=== ABSTRACT MODEL RECEIVED DATA ===")
        _logger.info("Data type: %s", type(data))
        _logger.info("Data keys: %s", data.keys() if isinstance(data, dict) else 'NOT A DICT')
        
        # Jika data ada (dari wizard), rebuild objects dari IDs
        processed_pickings_with_objects = []
        for picking_data in data.get('processed_pickings', []):
            _logger.info("Processing picking_data keys: %s", picking_data.keys())
            
            # Rebuild picking object dari ID
            picking = self.env['stock.picking'].browse(picking_data['picking_id'])
            
            # Rebuild moves dengan objects
            moves_with_objects = []
            for move_data in picking_data['moves_with_data']:
                move = self.env['stock.move'].browse(move_data['move_id'])
                product = self.env['product.product'].browse(move_data['product_id'])
                uom = self.env['uom.uom'].browse(move_data['uom_id'])
                
                moves_with_objects.append({
                    'move': move,
                    'product': product,
                    'quantity': move_data['quantity'],
                    'uom': uom,
                    'price': move_data['price'],
                    'subtotal': move_data['subtotal'],
                })
            
            processed_pickings_with_objects.append({
                'picking': picking,
                'moves_with_data': moves_with_objects,
                'total_amount': picking_data['total_amount'],
            })
        
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
            'slip_format': data.get('slip_format', 'with_price'),
            'pricelist': pricelist,
        }
