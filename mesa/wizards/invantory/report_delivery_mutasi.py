from odoo import models

class ReportDeliveryMutasi(models.AbstractModel):
    _name = 'report.mesa.report_delivery_mutasi_template'
    _description = 'Report Delivery Mutasi'
    
    def _safe_get_site_info(self, site, location=None):
        """Safely get site info from site record or location"""
        site_info = {
            'has_site': False,
            'name': location.name if location else '',
            'complete_name': location.complete_name if location else '',
            'site_code': '',
            'site_name': '',
            'street': '',
            'street2': '',
            'city': '',
            'pos_status': '',
        }
        
        # Priority 1: Site record passed directly
        # Priority 2: Site from location if site record is empty
        if not site and location and hasattr(location, 'snk_site_id'):
            site = location.snk_site_id
            
        if site:
            # Get address parts from site's partner
            partner = site.partner_id
            street = partner.street or ''
            street2 = partner.street2 or ''
            city = (partner.city_id.name if hasattr(partner, 'city_id') and partner.city_id else partner.city) or ''
            address = ", ".join([p for p in [street, street2, city] if p])
            
            # Combined site info as requested: Code + Name + Address
            # Format: [CODE] NAME, ADDRESS
            full_site_name = f"{site.snk_code or ''} {site.snk_name or ''} {address}".strip()
            
            # Get selection label for POS Status
            pos_status = site.snk_pos_status
            if pos_status:
                pos_status = dict(site._fields['snk_pos_status']._description_selection(self.env)).get(pos_status, pos_status)
            else:
                pos_status = '-'

            site_info.update({
                'has_site': True,
                'site_code': site.snk_code or '',
                'site_name': full_site_name,
                'pos_status': pos_status,
                'street': street,
                'street2': street2,
                'city': city,
            })
        
        return site_info

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
                        'has_grade': hasattr(move.product_id, 'snk_grade_id'),
                        'grade_name': getattr(getattr(move.product_id, 'snk_grade_id', None), 'snk_name', ''),
                    })
                
                # Sort moves by product display name
                moves_with_data.sort(key=lambda m: m['product'].display_name or '')
                
                processed_pickings.append({
                    'picking': picking,
                    'moves_with_data': moves_with_data,
                    'total_amount': 0.0,
                    'from_site': self._safe_get_site_info(picking.snk_site_id, picking.location_id),
                    'to_site': self._safe_get_site_info(picking.snk_site_address_id, picking.location_dest_id),
                })
            
            return {
                'doc_ids': docids,
                'doc_model': 'stock.picking',
                'docs': pickings,
                'processed_pickings': processed_pickings,
                'include_price': False,
                'slip_format': 'mutasi',
                'pricelist': False,
            }
        
        # DEBUG: Log data yang diterima
        _logger.info("=== ABSTRACT MODEL RECEIVED DATA ===")
        _logger.info("Data type: %s", type(data))
        _logger.info("Data keys: %s", data.keys() if isinstance(data, dict) else 'NOT A DICT')
        _logger.info("Processed pickings: %s", data.get('processed_pickings', 'KEY NOT FOUND'))
        
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
                    'has_grade': hasattr(product, 'snk_grade_id'),
                    'grade_name': getattr(getattr(product, 'snk_grade_id', None), 'snk_name', ''),
                })
            
            # Sort moves by product display name
            moves_with_objects.sort(key=lambda m: m['product'].display_name or '')
            
            processed_pickings_with_objects.append({
                'picking': picking,
                'moves_with_data': moves_with_objects,
                'total_amount': picking_data['total_amount'],
                'from_site': self._safe_get_site_info(picking.snk_site_id, picking.location_id),
                'to_site': self._safe_get_site_info(picking.snk_site_address_id, picking.location_dest_id),
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
            'slip_format': data.get('slip_format', 'mutasi'),
            'pricelist': pricelist,
        }