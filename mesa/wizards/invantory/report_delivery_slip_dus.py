from odoo import models, api
import re
import logging
from collections import defaultdict

_logger = logging.getLogger(__name__)

class ReportDeliverySlipDus(models.AbstractModel):
    _name = 'report.mesa.report_delivery_slip_dus_template'
    _description = 'Delivery Slip Dus Report'

    @api.model
    def _get_report_values(self, docids, data=None):
        _logger.info("ReportDeliverySlipDus called. Docids: %s", docids)

        if not docids:
            docids = (
                data.get('doc_ids') if data and data.get('doc_ids')
                else self.env.context.get('active_ids', [])
            )

        docs = self.env['stock.picking'].browse(docids).sorted('name')

        PAGE_LIMIT = 20

        # =========================
        # GROUP BY DESTINATION SITE
        # =========================
        site_groups = defaultdict(list)

        for picking in docs:
            site = picking.snk_site_address_id.id if picking.snk_site_address_id else 0
            site_groups[site].append(picking)

        grouped_pages = []

        for site_id, pickings in site_groups.items():

            pages = []
            for i in range(0, len(pickings), PAGE_LIMIT):
                pages.append(pickings[i:i + PAGE_LIMIT])

            konter_codes_group = sorted(set(
                p.snk_site_address_id.snk_code
                for p in pickings
                if p.snk_site_address_id and p.snk_site_address_id.snk_code
            ))

            grouped_pages.append({
                'site_id': site_id,
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

        # konter_codes = sorted(set(
        #     d.snk_site_address_id.snk_code
        #     for d in docs
        #     if d.snk_site_address_id and d.snk_site_address_id.snk_code
        # ))

        return {
            'doc_ids': docids,
            'docs': docs,
            'grouped_pages': grouped_pages,
            'get_no_dus': get_no_dus,
            # 'konter_codes': konter_codes,
            'data': data,
        }