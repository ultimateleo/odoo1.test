from odoo import models

class ReportDeliverySTO(models.AbstractModel):
    _name = 'report.mesa.report_delivery_sto_template'
    _description = 'Report Stock Transfer Order'
    
    def _get_report_values(self, docids, data=None):
        """
        Similar to ReportDeliverySlipPrice but for STO template.
        """
        # Reuse the logic from the existing abstract model if possible, 
        # but here we implement it for this specific template name.
        
        if not data:
            pickings = self.env['stock.picking'].browse(docids)
            processed_pickings = []
            for picking in pickings:
                moves_with_data = []
                for move in picking.move_ids_without_package:
                    quantity = getattr(move, 'quantity', False) or getattr(move, 'quantity_done', False) or move.product_uom_qty
                    if quantity <= 0:
                        continue
                        
                    moves_with_data.append({
                        'move': move,
                        'product': move.product_id,
                        'quantity': quantity,
                        'uom': move.product_uom,
                        'price': move.product_id.lst_price,
                        'subtotal': quantity * move.product_id.lst_price,
                        'has_grade': hasattr(move.product_id, 'snk_grade_id'),
                        'grade_name': getattr(getattr(move.product_id, 'snk_grade_id', None), 'snk_name', ''),
                    })
                
                processed_pickings.append({
                    'picking': picking,
                    'moves_with_data': moves_with_data,
                    'total_amount': sum(m['subtotal'] for m in moves_with_data),
                })
            
            return {
                'doc_ids': docids,
                'doc_model': 'stock.picking',
                'docs': pickings,
                'processed_pickings': processed_pickings,
            }
            
        # If data from wizard
        processed_pickings_with_objects = []
        for picking_data in data.get('processed_pickings', []):
            picking = self.env['stock.picking'].browse(picking_data['picking_id'])
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
                    'has_grade': hasattr(product, 'snk_grade_id'),
                    'grade_name': getattr(getattr(product, 'snk_grade_id', None), 'snk_name', ''),
                })
            
            processed_pickings_with_objects.append({
                'picking': picking,
                'moves_with_data': moves_with_objects,
                'total_amount': picking_data['total_amount'],
            })
        
        return {
            'doc_ids': docids,
            'doc_model': 'stock.picking',
            'docs': self.env['stock.picking'].browse(docids),
            'processed_pickings': processed_pickings_with_objects,
            'include_price': data.get('include_price', True),
            'slip_format': data.get('slip_format', 'sto'),
        }
