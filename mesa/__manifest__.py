# -*- coding: utf-8 -*-
{
    'name': "MESA",
    'version': '0.9',

    'summary': """
        Odoo 18 Enterprise Edition custom module for ERP of PT MEGARIAMAS SENTOSA.
    """,

    'description': """
        Odoo 18 EE custom module for ERP of PT MEGARIAMAS SENTOSA
        ========================================

        This module is made for Odoo 18 Enterprise Edition and customized based on PT MEGARIAMAS SENTOSA requests and needs.
    """,

    'category': 'Accounting/Accounting',
    'author': "Sinerka",
    'website': "https://www.sinerka.id/",
    'company': 'Sinerka',
    'maintainer': 'Sinerka',
    'depends': [
        'base', 'mail', 'account', 'sale', 'base_tier_validation','queue_job', 'contacts', 'purchase_requisition', 'purchase', 'product', 'stock', 'delivery',
        'l10n_id_efaktur', 'accountant', 'project', 'sale_project', 'stock_delivery','bus'
    ],

    'data': [
        # data
        'data/mrp_data.xml',
        'data/mrp_cron.xml',
        'data/purchase_report.xml',

        # security
        'security/mesa_security.xml',
        'security/ir.model.access.csv',
        'security/record_rules.xml',
        'security/ir_rule_pos_report.xml',

        # wizards
        'wizards/stock/snk_inventory_delivery_bill_views.xml',
        'wizards/purchase/snk_purchase_multi_product_views.xml',
        'wizards/stock/product_stock_multi_select_wizard.xml',
        'wizards/sale/snk_sale_order_filter_wizard.xml',
        'wizards/sale/snk_staging_sale_wizard.xml',
        'wizards/stock/snk_site_close_wizard.xml',
        'wizards/stock/snk_picking_resequence_wizard.xml',
        'wizards/product/product_label_layout_views.xml',
        'wizards/invantory/snk_delivery_slip_wizard_view.xml',
        'wizards/invantory/snk_label_summary_wizard_view.xml',
        'wizards/pos_integration/snk_pi_outbound_wizard_views.xml',
        'views/report/sales/report_sales_wizard.xml',
        'views/report/sales/report_sales_menu.xml',
        'views/report/pos/report_pos_wizard.xml',
        'views/report/purchase/report_po_monitoring.xml',
        'views/report/inventory/report_inventory_reserve_wizard.xml',
        'views/report/inventory/report_inventory_reserve_view.xml',
        'views/report/inventory/report_inventory_transfer_wizard.xml',
        'views/report/inventory/report_inventory_transfer_view.xml',
        # 'views/report/inventory/report_inventory_movement_analysis_wizard.xml',
        # 'views/report/inventory/report_inventory_movement_analysis_view.xml',
        'views/report/inventory/report_stock_card_wizard.xml',
        'views/report/inventory/report_stock_card_view.xml',
        'views/report/inventory/report_stock_balance_site_gudang_wizard.xml',
        'views/report/inventory/report_stock_balance_site_gudang_view.xml',
        'views/report/inventory/report_stock_periode_view.xml',
        'views/report/inventory/report_monitoring_sto_wizard.xml',
        'views/report/inventory/report_monitoring_sto_view.xml',
        'views/report/inventory/report_sto_summary_wizard.xml',
        'views/report/inventory/report_sto_summary_view.xml',
        'views/report/inventory/report_site.xml',
        'views/report/menu/snk_menu.xml',

        # reports
        'reports/product_pricelist_report_templates.xml',
        'reports/product_product_template.xml',
        'reports/purchase_requisition_template.xml',
        'reports/purchase_requisition_report.xml',
        'reports/sale_order_template.xml',
        'reports/sale_order_report.xml',
        'reports/purchase_order_report_templates.xml',
        'reports/snk_printout_vendor_bill.xml',
        'reports/snk_printout_PO_Subcont.xml',
        'reports/snk_printout_invoice.xml',
        'reports/snk_printout_payment.xml',
        'reports/snk_printout_delivery_mutasi.xml',
        'reports/snk_printout_delivery_slip.xml',
        'reports/snk_printout_delivery_slip_price.xml',
        'reports/snk_printout_delivery_slip_price_v2.xml',
        'reports/snk_printout_delivery_slip_dus.xml',
        'reports/snk_printout_delivery_slip_dus_v2.xml',
        'reports/snk_printout_delivery_slip_factory.xml',
        'reports/snk_printout_delivery_sto.xml',
        'reports/snk_printout_delivery_sto_v2.xml',
        # # 'reports/snk_printout_PO_Factory.xml',

        # views
        'views/config/res_config_settings.xml',
        'views/base/ir_actions.xml',
        'views/base/res_user.xml',
        'views/product/product_attribute_value_views.xml',
        'views/product/product_attribute_views.xml',
        'views/product/product_category_views.xml',
        'views/product/snk_product_grade_view.xml',
        'views/product/product_template_views.xml',
        'views/product/product_views.xml',
        'views/product/product_cron_views.xml',
        'views/product/product_pricelist_item_views.xml',
        'views/stock/snk_stock_site_view.xml',
        'views/stock/delivery_price_rule_views.xml',
        'views/stock/delivery_carrier_views.xml',
        'views/stock/delivery_views.xml',
        'views/stock/stock_move_line_views.xml',
        'views/stock/stock_move_views.xml',
        'views/stock/stock_picking_views.xml',
        'views/stock/stock_picking_type.xml',
        'views/stock/stock_warehouse_views.xml',
        'views/stock/stock_package_type_views.xml',
        'views/stock/stock_quant_views.xml',
        'views/contact/res_country_city_views.xml',
        'views/contact/res_partner_views.xml',
        'views/account/analytic_account_views.xml',
        'views/account/account_move_view.xml',
        'views/purchase/snk_purchase_type_views.xml',
        'views/purchase/purchase_requisition_view.xml',
        'views/purchase/purchase_order_view.xml',
        'views/sale/sale_order_view.xml',
        'views/sale/snk_sale_menu.xml',
        'views/sale/snk_staging_sale_view.xml',
        'views/snk_pos/snk_pos_menu_views.xml',
        'views/snk_pos/snk_pos_order_views.xml',
        'views/snk_pos/snk_pos_sale_views.xml',
        'views/snk_pos_integration/snk_pi_menu_views.xml',
        'views/snk_pos_integration/snk_pi_outbound_views.xml',
        'views/snk_pos_integration/snk_pi_inbound_views.xml',
        'views/mrp/snk_stock_transfer_order_view.xml',
        'views/mrp/snk_stock_transfer_rule.xml',
        'views/mrp/snk_order_point_views.xml',
        'views/mrp/snk_stock_schedule_view.xml',
        'views/mrp/snk_stock_schedule_log_view.xml',
        'views/contact/snk_district.xml',
        'views/stock/snk_site_grade.xml',
        'views/stock/snk_site_tag.xml',
        'views/stock/snk_site_history.xml',
        'views/stock/snk_site.xml',


        # menus
    ],
    # css
    'assets': {
        'web.assets_backend': [
            "mesa/static/src/js/pricelist_report/product_pricelist_report.js",
            "mesa/static/src/js/pricelist_report/product_pricelist_report.xml",
            # "mesa/static/src/js/pos/pos_notification.js",
            # ### komen karena coba pakai simple notification dulu. Update tetap belum jalan ketika dipanggil dari job.
        ],
    },



    'license': 'AGPL-3',
    'installable': True,
    'auto_install': False,
    'application': True,
}
