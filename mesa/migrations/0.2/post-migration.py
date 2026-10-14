from odoo import api, SUPERUSER_ID

def migrate(cr, version):
    """
    Post-migration 0.2
    Membuat index untuk optimasi process_sales / pos_order
    """

    # Index untuk kolom name → cek duplicate cepat
    cr.execute("""
        CREATE INDEX IF NOT EXISTS snk_pi_inbound_name_idx
            ON snk_pi_inbound (name);
    """)

    # Index gabungan ref_data + state → filter pending / done cepat
    cr.execute("""
        CREATE INDEX IF NOT EXISTS snk_pi_inbound_refdata_state_idx
            ON snk_pi_inbound (ref_data, state);
    """)

    # Opsional: log info di console (bisa dihapus)
    print("✅ snk_pi_inbound indexes created (name, ref_data+state)")
