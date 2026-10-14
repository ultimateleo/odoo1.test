import json
import logging

from werkzeug.urls import url_parse

from odoo import http
from odoo.http import content_disposition, request
from odoo.tools.safe_eval import safe_eval, time

from odoo.addons.web.controllers.report import ReportController

_logger = logging.getLogger(__name__)


class MesaReportController(ReportController):

    @http.route()
    def report_download(self, data, context=None, token=None, readonly=True):
        """
        Override report_download untuk evaluate print_report_name
        pada report yang dipanggil dari wizard (dengan data parameter).

        Masalah: Ketika report dipanggil dari wizard dengan data,
        docids tidak masuk ke URL path. Controller Odoo hanya evaluate
        print_report_name jika docids ada di URL. Akibatnya filename
        selalu fallback ke report.name (misal 'Form Mutasi').

        Solusi: Setelah super() selesai, cek apakah report punya
        print_report_name dan active_ids tersedia di context.
        Jika ya, evaluate print_report_name dan update filename.
        """
        response = super().report_download(
            data, context=context, token=token, readonly=readonly
        )

        # Coba override filename jika report punya print_report_name
        try:
            requestcontent = json.loads(data)
            url, type_ = requestcontent[0], requestcontent[1]

            if type_ not in ('qweb-pdf', 'qweb-text'):
                return response

            extension = 'pdf' if type_ == 'qweb-pdf' else 'txt'
            pattern = '/report/pdf/' if type_ == 'qweb-pdf' else '/report/text/'
            reportname = url.split(pattern)[1].split('?')[0]

            # Hanya handle case wizard (TIDAK ada docids di URL)
            if '/' in reportname:
                return response

            report = request.env['ir.actions.report']._get_report_from_name(
                reportname
            )
            if not report or not report.print_report_name:
                return response

            # Ambil active_ids dari context
            ctx = {}
            if context:
                ctx = json.loads(context) if isinstance(context, str) else context

            # Juga cek context dari URL params
            url_data = url_parse(url).decode_query(cls=dict)
            if url_data.get('context'):
                url_ctx = json.loads(url_data['context'])
                ctx.update(url_ctx)

            active_ids = ctx.get('active_ids', [])
            if not active_ids:
                return response

            # Evaluate print_report_name untuk record pertama
            obj = request.env[report.model].browse(active_ids[:1])
            if not obj.exists():
                return response

            report_name = safe_eval(
                report.print_report_name,
                {'object': obj, 'time': time}
            )
            filename = "%s.%s" % (report_name, extension)

            # Update Content-Disposition header
            response.headers['Content-Disposition'] = content_disposition(
                filename
            )

        except Exception:
            # Jika ada error, tetap return response asli tanpa modifikasi
            _logger.debug(
                "Could not evaluate print_report_name for wizard report",
                exc_info=True
            )

        return response
