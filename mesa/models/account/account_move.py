# -*- coding: utf-8 -*-
from odoo import api, fields, models, tools, _
from odoo.exceptions import UserError, ValidationError, RedirectWarning
import re
from num2words import num2words
from PyPDF2 import PdfMerger
import io
import base64

class AccountMove(models.Model):
    _inherit = 'account.move'

    snk_po_number = fields.Char("PO Number")
    snk_is_not_efaktur = fields.Boolean(
        "Is Not Use Efaktur", related='partner_id.snk_is_not_efaktur')
    snk_picking_ids = fields.One2many(
        'stock.picking', 'snk_bill_id', string='Picking Reference')
    snk_pr_id = fields.Char("PR Number")

    @api.onchange('l10n_id_tax_number')
    def _onchange_l10n_id_tax_number(self):
        for record in self:
            if record.l10n_id_tax_number and record.move_type not in self.get_purchase_types():
                # bypass validation in customer invoice
                if record.move_type == 'out_invoice':
                    continue
                # end
                raise UserError(
                    _("You can only change the number manually for a Vendor Bills and Credit Notes"))

    @api.constrains('l10n_id_tax_number')
    def _constrains_l10n_id_tax_number(self):
        for rec in self:
            if rec.partner_id.snk_is_not_efaktur:
                continue

            super()._constrains_l10n_id_tax_number()


    def _print_vendor_bill_in_chunks(self, batch_size=800):
        """
        Render vendor bill PDF dalam batch untuk menghindari timeout pada invoice dengan banyak line items.

        Kenapa butuh chunking?
        - Vendor bill dengan ribuan line items (misalnya 3000+ lines) akan menyebabkan QWebException
        - Rendering semua lines sekaligus membebani memory dan menyebabkan timeout
        - Dengan chunking, kita pecah menjadi batch kecil (800 lines per batch), lalu gabungkan PDF-nya

        Args:
            batch_size (int): Jumlah lines per batch. Aman antara 600-1200 lines.
                              Default 800 untuk balance antara performa dan keamanan.

        Returns:
            bytes: PDF content yang sudah digabungkan dari semua batch

        Flow:
            1. Ambil semua invoice_line_ids
            2. Loop setiap batch (0-800, 800-1600, dst)
            3. Render PDF untuk batch tersebut dengan passing line_ids_override ke template
            4. Gabungkan semua PDF menggunakan PdfMerger
            5. Return PDF final
        """
        all_lines = self.invoice_line_ids
        pdf_parts = []

        # Loop untuk setiap batch lines (misalnya 3000 lines = 4 batch @ 800 lines)
        for i in range(0, len(all_lines), batch_size):
            # Ambil subset lines untuk batch ini
            # Contoh: batch 1 = lines[0:800], batch 2 = lines[800:1600], dst
            batch_lines = all_lines[i:i+batch_size]

            # Render PDF untuk batch ini saja
            # 'line_ids_override' akan digunakan di template untuk override invoice_line_ids
            report = self.env['ir.actions.report']._get_report('mesa.report_vendor_bill')
            # _render_qweb_pdf(report_ref, res_ids=None, data=None)
            # report_ref bisa berupa report object atau report name string
            pdf_data, _ = report._render_qweb_pdf('mesa.report_vendor_bill', res_ids=self.ids, data={
                'line_ids_override': batch_lines.ids
            })
            pdf_parts.append(pdf_data)

        # Gabungkan semua PDF batch menjadi satu file PDF
        merger = PdfMerger()
        for pdf_bytes in pdf_parts:
            merger.append(io.BytesIO(pdf_bytes))

        # Write hasil merge ke buffer
        final_buf = io.BytesIO()
        merger.write(final_buf)
        merger.close()

        return final_buf.getvalue()

    def action_print_vendor_bill(self):
        """
        Action untuk print vendor bill dengan chunking mechanism.
        Dipanggil dari button/menu di UI.

        Returns:
            dict: Action untuk download PDF
        """
        # Generate PDF dengan chunking
        pdf_data = self._print_vendor_bill_in_chunks(batch_size=800)

        # Simpan PDF sebagai attachment agar bisa di-download user
        attachment = self.env['ir.attachment'].create({
            'name': 'Vendor Bill - %s.pdf' % self.name,
            'type': 'binary',
            'datas': base64.b64encode(pdf_data),
            'res_model': self._name,
            'res_id': self.id,
            'mimetype': 'application/pdf'
        })

        # Return action untuk download PDF di tab baru
        return {
            'type': 'ir.actions.act_url',
            'url': '/web/content/%s?download=true' % attachment.id,
            'target': 'new',
        }

class AccountPayment(models.Model):
    _inherit = "account.payment"

    def amount_to_text_id(self, amount):
        try:
            return num2words(amount, lang='id').capitalize() + " rupiah"
        except:
            return ""
