from odoo import models, api
import re
import logging
from collections import defaultdict

_logger = logging.getLogger(__name__)

class ReportDeliverySlipDusV2(models.AbstractModel):
    _name = 'report.mesa.report_delivery_slip_dus_v2_template'
    _description = 'Delivery Slip Dus Report V2'

    @api.model
    def _get_report_values(self, docids, data=None):
        _logger.info("ReportDeliverySlipDusV2 called. Docids: %s", docids)

        if not docids:
            docids = (
                data.get('doc_ids') if data and data.get('doc_ids')
                else self.env.context.get('active_ids', [])
            )

        docs = self.env['stock.picking'].browse(docids).sorted('name')

        PAGE_LIMIT = 20

        # =========================
        # GROUP BY DESTINATION ADDRESS
        # =========================
        site_groups = defaultdict(list)

        for picking in docs:
            # Group by Address (partner_id.id) instead of Site ID
            address_id = picking.snk_site_address_id.partner_id.id if picking.snk_site_address_id and picking.snk_site_address_id.partner_id else 0
            site_groups[address_id].append(picking)

        grouped_pages = []

        for address_id, pickings in site_groups.items():

            pages = []
            for i in range(0, len(pickings), PAGE_LIMIT):
                pages.append(pickings[i:i + PAGE_LIMIT])

            konter_codes_group = sorted(set(
                p.snk_site_address_id.snk_code
                for p in pickings
                if p.snk_site_address_id and p.snk_site_address_id.snk_code
            ))

            grouped_pages.append({
                'address_id': address_id,
                'pages': pages,
                'first_doc': pickings[0],
                'konter_codes': konter_codes_group,
            })

        # =========================
        # HELPER NO DUS
        # =========================
        def get_no_dus(picking):
            if not picking or not picking.name:
                return ''

            matches = re.findall(r'\d+', picking.name)
            num = matches[-1] if matches else ''

            prefix = ''
            if picking.analytic_ids:
                brand_name = picking.analytic_ids[0].name
                if brand_name:
                    prefix = "".join(w[0].upper() for w in brand_name.split() if w) + "-"

            return f"{prefix}{num}"

        def get_total_qty(picking):
            if not picking:
                return 0.0
            
            total = 0.0
            for move in picking.move_ids_without_package:
                if picking.state == 'draft':
                    total += move.product_uom_qty or 0.0
                else:
                    quantity = getattr(move, 'quantity', False) or getattr(move, 'qty_done', False) or 0.0
                    total += quantity
            return total

        return {
            'doc_ids': docids,
            'docs': docs,
            'grouped_pages': grouped_pages,
            'get_no_dus': get_no_dus,
            'get_total_qty': get_total_qty,
            'data': data,
        }
