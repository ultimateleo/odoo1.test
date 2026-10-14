from odoo import models, api

class IrActionsReport(models.Model):
    _inherit = 'ir.actions.report'
    
    @api.model
    def _get_report_values(self, docids, data=None):
        # Check if this is the pricelist report
        if self.report_name == 'mesa.report_pricelist_mesa':
            if data and data.get('pricelist_id'):
                # Data from JavaScript/wizard
                pricelist_id = data.get('pricelist_id')
                active_ids = data.get('active_ids', [])
                pricelist = self.env['product.pricelist'].browse(pricelist_id)
                
                return {
                    'doc_ids': [pricelist_id],
                    'doc_model': 'product.pricelist',
                    'docs': pricelist,
                    'data': data,
                    'active_ids': active_ids,
                }
            else:
                # Direct access via URL or standard report generation
                return {
                    'doc_ids': docids,
                    'doc_model': 'product.pricelist', 
                    'docs': self.env['product.pricelist'].browse(docids),
                    'data': data or {},  # Ensure data is always available
                }
        
        # For other reports, use the default behavior
        return super()._get_report_values(docids, data)