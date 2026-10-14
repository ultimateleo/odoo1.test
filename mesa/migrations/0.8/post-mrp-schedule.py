"""Backfill konfigurasi MRP schedule untuk aturan effective schedule.

Mulai versi 0.8, jadwal MRP dihitung sebagai:

    effective_schedule = Schedule Day terakhir yang <= next_schedule
    next_schedule_baru = effective_schedule + (Schedule Week x 7 hari)

Dua field itu sekarang wajib (ada @api.constrains), sementara data lama boleh
punya salah satunya kosong karena dulu 'day' dan 'week' adalah dua mode yang
saling meniadakan. Migration ini mengisi yang kosong dengan nilai yang membuat
jadwal existing TIDAK bergeser sama sekali:

- day kosong   -> weekday dari next_schedule yang sekarang, dibaca di timezone
                  company. Mode lama (week terisi, day kosong) memang selalu
                  mendarat di weekday yang sama dengan next_schedule.
- week <= 0    -> 1. Mode lama (day terisi, week kosong) artinya mingguan.
- active NULL  -> TRUE, untuk baris yang sudah ada sebelum kolom ditambahkan.

Idempotent: hanya menyentuh baris yang datanya masih kosong.
"""

import logging

from odoo import api, SUPERUSER_ID

_logger = logging.getLogger(__name__)

# Map weekday() Python (0=Senin ... 6=Minggu) ke Selection day (0=Minggu ... 6=Sabtu).
PY_WEEKDAY_TO_SELECTION = {0: '1', 1: '2', 2: '3', 3: '4', 4: '5', 5: '6', 6: '0'}


def migrate(cr, version):
    if not version:
        return  # fresh install, tidak ada data lama

    # Kolom 'active' baru ditambahkan di versi ini. Odoo mengisi default saat
    # membuat kolom, tapi dipastikan lagi di sini supaya aman kalau kolomnya
    # sudah pernah dibuat manual tanpa default.
    cr.execute("""
        UPDATE snk_stock_schedule
        SET active = TRUE
        WHERE active IS NULL
    """)
    if cr.rowcount:
        _logger.info("[MIGRATION] %s schedule di-set active = TRUE", cr.rowcount)

    # Schedule Week kosong/0 -> 1 (mingguan), setara mode 'by day' yang lama.
    cr.execute("""
        UPDATE snk_stock_schedule
        SET week = 1
        WHERE week IS NULL OR week <= 0
    """)
    if cr.rowcount:
        _logger.info("[MIGRATION] %s schedule di-set Schedule Week = 1", cr.rowcount)

    # Schedule Day kosong -> weekday next_schedule di timezone company.
    # Lewat ORM supaya memakai helper timezone yang sama dengan runtime, bukan
    # menerjemahkan ulang aturan tz di SQL.
    env = api.Environment(cr, SUPERUSER_ID, {})
    Schedule = env['snk.stock.schedule']

    schedules = Schedule.with_context(active_test=False).search([
        ('day', '=', False),
        ('next_schedule', '!=', False),
    ])

    # Kelompokkan dulu per nilai hari, lalu satu write per kelompok (maksimal 7
    # write), bukan satu write per schedule.
    ids_by_day = {}
    for schedule in schedules:
        local_dt = schedule._to_local(schedule.next_schedule)
        day_value = PY_WEEKDAY_TO_SELECTION[local_dt.weekday()]
        ids_by_day.setdefault(day_value, []).append(schedule.id)

    for day_value, schedule_ids in ids_by_day.items():
        Schedule.browse(schedule_ids).write({'day': day_value})
        _logger.info(
            "[MIGRATION] %s schedule di-backfill Schedule Day = %s",
            len(schedule_ids), day_value)

    # Sisa yang tidak bisa di-backfill: tidak punya next_schedule sama sekali,
    # jadi tidak ada acuan hari. Dibiarkan kosong - cron akan men-skip-nya dengan
    # warning, dan user mengisi manual dari Distribution Schedule.
    leftover = Schedule.with_context(active_test=False).search_count([('day', '=', False)])
    if leftover:
        _logger.warning(
            "[MIGRATION] %s schedule masih tanpa Schedule Day (tidak punya "
            "next_schedule sebagai acuan) - perlu diisi manual", leftover)
