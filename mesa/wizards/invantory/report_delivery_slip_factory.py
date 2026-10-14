from odoo import models, api
import logging

_logger = logging.getLogger(__name__)

class ReportDeliverySlipFactory(models.AbstractModel):
    _name = 'report.mesa.report_delivery_slip_factory_template'
    _description = 'Delivery Slip Factory Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        _logger.info("ReportDeliverySlipFactory called with docids: %s, data keys: %s", docids, data.keys() if data else 'None')
        
        # Ensure we have docids
        if not docids and data and data.get('doc_ids'):
            docids = data.get('doc_ids')
        
        if not docids:
             # Try to find from context as fallback
             docids = self.env.context.get('active_ids', [])

        docs = self.env['stock.picking'].browse(docids)
        
        return {
            'doc_ids': docids,
            'docs': docs,
            'data': data,
        }
