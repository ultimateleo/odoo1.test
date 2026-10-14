/** @odoo-module **/
import { ProductPricelistReport } from "@product/js/pricelist_report/product_pricelist_report";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";

function sendCustomNotification(type, message) {
  return {
    type: "ir.actions.client",
    tag: "display_notification",
    params: {
      type: type,
      message: message,
    },
  };
}

// Menggunakan patch saja - lebih sederhana dan aman
patch(ProductPricelistReport.prototype, {
  async onClickPrint() {
    if (this.noProducts) {
      this.action.doAction(
        sendCustomNotification(
          "warning",
          _t("Silakan pilih beberapa produk terlebih dahulu.")
        )
      );
      return;
    }
    const selectedFormat = document.getElementById("formats").value;
    if (selectedFormat === "pdf_mesa") {
      // Custom export untuk Mesa
      this.export_pdf_mesa();
    } else if (selectedFormat === "pdf") {
      this.export_pdf();
    } else {
      await this.export_pricelist_csv_xlsx(selectedFormat);
    }
  },

  export_pdf_mesa() {
    this.action.doAction({
      type: "ir.actions.report",
      report_type: "qweb-pdf",
      report_name: "mesa.report_pricelist_mesa",
      report_file: "mesa.report_pricelist_mesa",
      data: this.reportParams,
    });
  },
});
