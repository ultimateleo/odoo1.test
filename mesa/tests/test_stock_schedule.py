"""Test perhitungan jadwal MRP (effective schedule + next schedule).

Semua skenario di plan_mrp_scheduling.md §10 diuji di sini, plus dua kasus yang
pernah jadi bug production: hari dibaca dari nilai UTC, dan jadwal digeser
filter weekend.

Kalkulasi tanggal diuji lewat record in-memory (.new()) supaya tidak butuh
fixture snk.site/stock.warehouse - yang diuji murni rumus tanggalnya.
"""

from datetime import datetime

import pytz

from odoo import fields
from odoo.exceptions import ValidationError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install', 'mesa_mrp')
class TestStockScheduleCalculation(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Timezone company adalah acuan penentuan hari jadwal. Dipatok eksplisit
        # supaya hasil test tidak ikut berubah kalau setup company berbeda.
        cls.env.company.partner_id.tz = 'Asia/Jakarta'
        cls.Schedule = cls.env['snk.stock.schedule']

    def _schedule(self, next_schedule_local, day, week=3):
        """Schedule in-memory dengan next_schedule diberikan dalam WIB.

        next_schedule disimpan ORM sebagai UTC, jadi input WIB dikonversi dulu -
        persis seperti yang terjadi saat user mengisi form.
        """
        schedule = self.Schedule.new({'day': day, 'week': week})
        schedule.next_schedule = schedule._to_utc(next_schedule_local)
        return schedule

    def _next_local(self, schedule):
        """calculate_next_schedule() dikembalikan dalam UTC; dibaca lagi di WIB."""
        next_utc = schedule.calculate_next_schedule()
        return schedule._to_local(next_utc) if next_utc else False

    # ------------------------------------------------------------------
    # Effective schedule - snap MUNDUR ke Schedule Day
    # ------------------------------------------------------------------
    def test_effective_schedule_snap_mundur(self):
        """Senin 24 Agu + Schedule Day Jumat -> Jumat 21 Agu (mundur 3 hari)."""
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5')

        effective = schedule._get_effective_schedule_local()

        self.assertEqual(effective.date().isoformat(), '2026-08-21')
        self.assertEqual(effective.weekday(), 4, "harus mendarat di Jumat")

    def test_effective_schedule_sudah_di_schedule_day(self):
        """Kalau next_schedule sudah di Schedule Day, snap tidak menggeser apa pun."""
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5')

        effective = schedule._get_effective_schedule_local()

        self.assertEqual(effective.date().isoformat(), '2026-08-21')

    def test_effective_schedule_mempertahankan_jam(self):
        """Yang digeser hanya tanggalnya, jam next_schedule dipertahankan."""
        schedule = self._schedule(datetime(2026, 8, 24, 17, 30), day='5')

        effective = schedule._get_effective_schedule_local()

        self.assertEqual(effective.hour, 17)
        self.assertEqual(effective.minute, 30)

    def test_effective_schedule_tanpa_day(self):
        """Tanpa Schedule Day, effective schedule tidak bisa ditentukan."""
        schedule = self.Schedule.new({'week': 3})
        schedule.next_schedule = schedule._to_utc(datetime(2026, 8, 24, 8, 0))

        self.assertFalse(schedule._get_effective_schedule_local())
        self.assertFalse(schedule.calculate_next_schedule())

    # ------------------------------------------------------------------
    # Plan §10 Test 1 & 2 - tepat waktu dan terlambat menghasilkan jadwal sama
    # ------------------------------------------------------------------
    def test_next_schedule_tepat_waktu(self):
        """Effective 21 Agu + 3 minggu = 11 September."""
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5', week=3)

        self.assertEqual(self._next_local(schedule).date().isoformat(), '2026-09-11')

    def test_next_schedule_dihitung_dari_effective_bukan_running(self):
        """Jadwal berikutnya dihitung dari effective schedule, bukan tanggal cron.

            benar: 21 Agu + 3 minggu = 11 Sep
            salah: 25 Agu + 3 minggu = 15 Sep
        """
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5', week=3)

        self.assertEqual(self._next_local(schedule).date().isoformat(), '2026-09-11')

    # ------------------------------------------------------------------
    # Window due harian - hanya periode yang jatuh di HARI cron berjalan
    # ------------------------------------------------------------------
    def test_overdue_tidak_lagi_diproses(self):
        """Periode yang tanggalnya sudah lewat tidak dikejar.

        Perilaku lama (`now >= effective`) mengangkut semua periode terlewat
        pada hari cron akhirnya berjalan. Sekarang effective 21 Agu tidak lagi
        due saat cron jalan 25 Agu.
        """
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5', week=3)
        running_dt = schedule._to_utc(datetime(2026, 8, 25, 6, 0))

        self.assertFalse(schedule._is_due(running_dt))
        self.assertTrue(schedule._is_overdue(running_dt))

    def test_due_sepanjang_hari_apapun_jamnya(self):
        """Window-nya 00:00:00-23:59:59, bukan jam pada next_schedule.

        Effective 21 Agu 17:30. Cron jam 00:30 pagi maupun 23:50 malam di
        tanggal yang sama sama-sama due.
        """
        schedule = self._schedule(datetime(2026, 8, 21, 17, 30), day='5', week=3)

        for jam, menit in [(0, 30), (17, 30), (23, 50)]:
            running_dt = schedule._to_utc(datetime(2026, 8, 21, jam, menit))
            self.assertTrue(
                schedule._is_due(running_dt),
                "harus due pada %02d:%02d di tanggal effective" % (jam, menit))

    def test_belum_due_sehari_sebelum_dan_sesudah(self):
        """Batas window tepat di pergantian hari lokal."""
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5', week=3)

        sehari_sebelum = schedule._to_utc(datetime(2026, 8, 20, 23, 59, 59))
        sehari_sesudah = schedule._to_utc(datetime(2026, 8, 22, 0, 0, 1))

        self.assertFalse(schedule._is_due(sehari_sebelum))
        self.assertFalse(schedule._is_overdue(sehari_sebelum), "besok bukan terlewat")
        self.assertFalse(schedule._is_due(sehari_sesudah))
        self.assertTrue(schedule._is_overdue(sehari_sesudah))

    def test_window_dihitung_di_waktu_lokal(self):
        """01:00 WIB 21 Agu = 18:00 UTC 20 Agu, dan harus tetap dibaca 21 Agu.

        Window yang dipotong di UTC akan menganggap cron jam 01:00 WIB masih
        hari sebelumnya dan melewatkan seluruh jadwal pagi.
        """
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5', week=3)
        running_dt = schedule._to_utc(datetime(2026, 8, 21, 1, 0))

        self.assertEqual(running_dt.date().isoformat(), '2026-08-20', "di UTC masih 20 Agu")
        self.assertTrue(schedule._is_due(running_dt))

    # ------------------------------------------------------------------
    # Plan §10 Test 3 - belum jatuh tempo
    # ------------------------------------------------------------------
    def test_belum_jatuh_tempo(self):
        """Running date sebelum effective schedule -> tidak due."""
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5', week=3)
        running_dt = schedule._to_utc(datetime(2026, 8, 20, 23, 0))

        self.assertFalse(schedule._is_due(running_dt))

    def test_due_tepat_di_effective_schedule(self):
        """Running date sama dengan effective schedule -> due."""
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5', week=3)
        running_dt = schedule._to_utc(datetime(2026, 8, 21, 8, 0))

        self.assertTrue(schedule._is_due(running_dt))

    # ------------------------------------------------------------------
    # Plan §10 Test 6 - konsistensi pola lintas periode
    # ------------------------------------------------------------------
    def test_pola_tiga_mingguan_konsisten(self):
        """21 Agu -> 11 Sep -> 2 Okt -> 23 Okt -> 13 Nov, semuanya Jumat."""
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5', week=3)

        hasil = []
        for _ in range(5):
            next_local = self._next_local(schedule)
            hasil.append(next_local.date().isoformat())
            self.assertEqual(next_local.weekday(), 4, "pola harus tetap di Jumat")
            schedule.next_schedule = schedule._to_utc(next_local)

        self.assertEqual(
            hasil,
            ['2026-09-11', '2026-10-02', '2026-10-23', '2026-11-13', '2026-12-04'])

    def test_interval_tiga_minggu_tepat_21_hari(self):
        """Interval dihitung dalam hari kalender, bukan hari kerja."""
        schedule = self._schedule(datetime(2026, 8, 21, 8, 0), day='5', week=3)

        selisih = self._next_local(schedule) - datetime(2026, 8, 21, 8, 0)

        self.assertEqual(selisih.days, 21)

    # ------------------------------------------------------------------
    # Filter weekend sudah dilepas
    # ------------------------------------------------------------------
    def test_schedule_day_sabtu_tetap_sabtu(self):
        """User memilih Sabtu, jadwalnya memang Sabtu - tidak digeser ke Senin.

        Perilaku lama menggeser Sabtu/Minggu ke hari kerja berikutnya, yang
        justru merusak jadwal yang sudah benar.
        """
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='6', week=1)

        effective = schedule._get_effective_schedule_local()
        next_local = self._next_local(schedule)

        self.assertEqual(effective.date().isoformat(), '2026-08-22')
        self.assertEqual(effective.weekday(), 5, "Sabtu tidak digeser")
        self.assertEqual(next_local.date().isoformat(), '2026-08-29')
        self.assertEqual(next_local.weekday(), 5)

    def test_schedule_day_minggu_tetap_minggu(self):
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='0', week=1)

        effective = schedule._get_effective_schedule_local()

        self.assertEqual(effective.date().isoformat(), '2026-08-23')
        self.assertEqual(effective.weekday(), 6, "Minggu tidak digeser")

    # ------------------------------------------------------------------
    # Timezone - bug yang mendaratkan 148 schedule di Sabtu
    # ------------------------------------------------------------------
    def test_hari_dibaca_di_waktu_lokal_bukan_utc(self):
        """Jumat 01:00 WIB = Kamis 18:00 UTC, dan harus tetap dibaca Jumat.

        Perhitungan yang membaca weekday() langsung dari nilai UTC akan
        menganggap ini Kamis dan menggeser jadwal satu hari.
        """
        schedule = self._schedule(datetime(2026, 8, 21, 1, 0), day='5', week=3)

        # next_schedule tersimpan di UTC sebagai hari Kamis
        self.assertEqual(schedule.next_schedule.weekday(), 3)

        effective = schedule._get_effective_schedule_local()
        self.assertEqual(effective.date().isoformat(), '2026-08-21')
        self.assertEqual(effective.weekday(), 4)

        next_local = self._next_local(schedule)
        self.assertEqual(next_local.date().isoformat(), '2026-09-11')
        self.assertEqual(next_local.hour, 1, "jam lokal dipertahankan")

    def test_effective_schedule_utc_untuk_last_schedule(self):
        """Nilai yang disimpan ke last_schedule adalah effective schedule (UTC).

        last_schedule mencatat periode yang BENAR-BENAR dijalankan. Untuk
        next_schedule Senin 24 Agu 08:00 WIB dengan Schedule Day Jumat, yang
        dieksekusi adalah Jumat 21 Agu 08:00 WIB = 21 Agu 01:00 UTC. Menyimpan
        next_schedule mentah membuat Last Schedule menunjuk hari dan jam yang
        tidak pernah dieksekusi.
        """
        schedule = self._schedule(datetime(2026, 8, 24, 8, 0), day='5', week=3)

        effective_utc = schedule._get_effective_schedule()

        self.assertEqual(effective_utc, datetime(2026, 8, 21, 1, 0))
        self.assertEqual(
            schedule._to_local(effective_utc), datetime(2026, 8, 21, 8, 0))
        self.assertNotEqual(
            effective_utc, schedule.next_schedule,
            "last_schedule tidak boleh sama dengan next_schedule mentah")

    def test_today_local_tidak_ikut_timezone_user(self):
        """_today_local() harus mengikuti tz company, bukan tz user cron.

        Cron MRP jalan sebagai base.user_root yang tz-nya umumnya kosong.
        fields.Date.context_today() dalam kondisi itu jatuh ke UTC, sehingga
        antara 00:00-07:00 WIB tanggalnya mundur sehari.
        """
        expected = pytz.UTC.localize(fields.Datetime.now()).astimezone(
            pytz.timezone('Asia/Jakarta')).date()

        self.env.user.tz = 'Pacific/Kiritimati'   # UTC+14
        self.assertEqual(self.Schedule._today_local(), expected)

        self.env.user.tz = False                  # persis kondisi user_root
        self.assertEqual(self.Schedule._today_local(), expected)


    # ------------------------------------------------------------------
    # Hari kalender + lompat weekend - PR date, STO Date, Scheduled Date DO
    # ------------------------------------------------------------------
    def test_add_calendar_days_awal_weekend_tidak_digeser_dulu(self):
        """Hitung dulu dari tanggal mentah, geser hanya tanggal jatuhnya.

        Sabtu 19 Sep + 2 = Senin 21 Sep. Kalau tanggal awalnya digeser dulu
        ke Senin 21 baru dihitung, hasilnya Rabu 23 - rantai melar 2 hari dan
        bukan hitung hari kalender lagi.
        """
        sabtu = self.Schedule._to_utc(datetime(2026, 9, 19, 8, 0))

        lokal = self.Schedule._to_local(self.Schedule._add_calendar_days(sabtu, 2))

        self.assertEqual(lokal.date().isoformat(), '2026-09-21')
        self.assertNotEqual(lokal.date().isoformat(), '2026-09-23',
                            "awal weekend tidak boleh digeser sebelum dihitung")

    def test_rantai_dihitung_dari_tanggal_mentah(self):
        """PR Sabtu 19 Sep, sto_day 2, lead 7: STO Senin 21, DO 19 + 9 = Senin 28.

        Ini cara action_confirm() menghitung Scheduled Date DO: jeda dijumlah
        lalu dihitung sekali dari PR date mentah. Kalau PR date digeser dulu ke
        Senin 21 baru dihitung, STO jadi Rabu 23 dan DO Rabu 30 - melar 2 hari.
        """
        pr_sabtu = self.Schedule._to_utc(datetime(2026, 9, 19, 0, 0))

        sto = self.Schedule._to_local(self.Schedule._add_calendar_days(pr_sabtu, 2))
        do = self.Schedule._to_local(self.Schedule._add_calendar_days(pr_sabtu, 2 + 7))

        self.assertEqual(sto.date().isoformat(), '2026-09-21')
        self.assertEqual(do.date().isoformat(), '2026-09-28')

    def test_rantai_dari_mentah_beda_dengan_dari_hasil_geseran(self):
        """Menghitung dari hasil geseran memberi tanggal lain kalau lead bukan kelipatan 7.

        PR Sabtu 19 Sep, sto_day 1, lead 5:
            dari mentah  : 19 + 1 + 5 = Jumat 25
            dari geseran : STO 19 + 1 = Minggu 20 -> Senin 21; 21 + 5 = Sabtu 26 -> Senin 28
        Selisih 3 hari. Yang benar dari mentah - itu sebabnya action_confirm()
        tidak boleh memanggil helper atas sto_date yang sudah digeser.
        """
        pr_sabtu = self.Schedule._to_utc(datetime(2026, 9, 19, 0, 0))

        mentah = self.Schedule._to_local(self.Schedule._add_calendar_days(pr_sabtu, 1 + 5))
        sto_digeser = self.Schedule._add_calendar_days(pr_sabtu, 1)
        dari_geseran = self.Schedule._to_local(self.Schedule._add_calendar_days(sto_digeser, 5))

        self.assertEqual(mentah.date().isoformat(), '2026-09-25')
        self.assertEqual(dari_geseran.date().isoformat(), '2026-09-28')
        self.assertNotEqual(mentah.date(), dari_geseran.date())

    def test_add_calendar_days_tidak_menghitung_hari_kerja(self):
        """Weekend IKUT dihitung - jeda tetap N hari kalender.

        Rabu 9 Sep + 7 hari kalender = Rabu 16 Sep. Perhitungan hari kerja
        lama mendaratkannya di Jumat 18 Sep karena Sabtu/Minggu tidak dihitung.
        """
        hasil = self.Schedule._add_calendar_days(
            self.Schedule._to_utc(datetime(2026, 9, 9, 8, 0)), 7)

        lokal = self.Schedule._to_local(hasil)
        self.assertEqual(lokal.date().isoformat(), '2026-09-16')
        self.assertEqual((lokal.date() - date(2026, 9, 9)).days, 7)

    def test_add_calendar_days_mendarat_weekend_digeser_ke_senin(self):
        """Selasa 8 Sep + 4 = Sabtu 12, + 5 = Minggu 13; dua-duanya jadi Senin 14."""
        base = self.Schedule._to_utc(datetime(2026, 9, 8, 8, 0))

        for tambah in (4, 5):
            lokal = self.Schedule._to_local(self.Schedule._add_calendar_days(base, tambah))
            self.assertEqual(
                lokal.date().isoformat(), '2026-09-14',
                "+%s hari harus mendarat di Senin 14 Sep" % tambah)

    def test_add_calendar_days_nol_hari_tetap_lompat_weekend(self):
        """days=0 tetap menggeser tanggal yang kebetulan jatuh di weekend."""
        sabtu = self.Schedule._to_utc(datetime(2026, 9, 12, 8, 0))

        lokal = self.Schedule._to_local(self.Schedule._add_calendar_days(sabtu, 0))

        self.assertEqual(lokal.date().isoformat(), '2026-09-14')

    def test_add_calendar_days_mempertahankan_jam(self):
        """Yang digeser hanya tanggalnya, jam dipertahankan."""
        base = self.Schedule._to_utc(datetime(2026, 9, 8, 17, 30))

        lokal = self.Schedule._to_local(self.Schedule._add_calendar_days(base, 4))

        self.assertEqual(lokal.hour, 17)
        self.assertEqual(lokal.minute, 30)

    def test_add_calendar_days_weekday_dibaca_di_waktu_lokal(self):
        """Sabtu 01:00 WIB = Jumat 18:00 UTC, dan harus tetap dibaca Sabtu.

        Perhitungan yang membaca weekday() langsung dari nilai UTC akan
        menganggap ini Jumat dan membiarkan tanggalnya mendarat di weekend.
        """
        sabtu_dini_hari = self.Schedule._to_utc(datetime(2026, 9, 12, 1, 0))
        self.assertEqual(sabtu_dini_hari.weekday(), 4, "di UTC masih Jumat")

        lokal = self.Schedule._to_local(
            self.Schedule._add_calendar_days(sabtu_dini_hari, 0))

        self.assertEqual(lokal.date().isoformat(), '2026-09-14')

    def test_add_calendar_days_falsy_dikembalikan_apa_adanya(self):
        self.assertFalse(self.Schedule._add_calendar_days(False, 7))


@tagged('post_install', '-at_install', 'mesa_mrp')
class TestStockScheduleRecord(TransactionCase):
    """Test yang butuh record sungguhan: constraint dan anti-duplikasi periode."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.company.partner_id.tz = 'Asia/Jakarta'
        warehouse = cls.env['stock.warehouse'].search(
            [('company_id', '=', cls.env.company.id)], limit=1)
        cls.site = cls.env['snk.site'].create({
            'snk_code': 'TEST-MRP-SCHED',
            'snk_name': 'Test Site MRP Schedule',
            'warehouse_id': warehouse.id,
        })

    def _create_schedule(self, **vals):
        base = {
            'site_id': self.site.id,
            'day': '5',
            'week': 3,
            'next_schedule': datetime(2026, 8, 24, 1, 0),  # 08:00 WIB
        }
        base.update(vals)
        return self.env['snk.stock.schedule'].create(base)

    def test_schedule_day_wajib_diisi(self):
        with self.assertRaises(ValidationError):
            self._create_schedule(day=False)

    def test_schedule_week_harus_lebih_dari_nol(self):
        with self.assertRaises(ValidationError):
            self._create_schedule(week=0)

    def test_periode_sukses_tidak_diproses_ulang(self):
        """Plan §10 Test 5 - log sukses mengunci periode."""
        schedule = self._create_schedule()
        effective_date = schedule._get_effective_schedule_local().date()

        self.assertFalse(schedule._is_period_done(effective_date))

        self.env['snk.stock.schedule.log'].create({
            'schedule_id': schedule.id,
            'effective_date': effective_date,
            'state': 'success',
        })

        self.assertTrue(schedule._is_period_done(effective_date))

    def test_percobaan_gagal_boleh_berulang(self):
        """Unique index hanya berlaku untuk state='success', retry tetap tercatat."""
        schedule = self._create_schedule()
        effective_date = schedule._get_effective_schedule_local().date()
        Log = self.env['snk.stock.schedule.log']

        for _ in range(3):
            Log.create({
                'schedule_id': schedule.id,
                'effective_date': effective_date,
                'state': 'failed',
                'error': 'boom',
            })

        self.assertEqual(len(schedule.log_ids), 3)
        self.assertFalse(schedule._is_period_done(effective_date))

    def test_due_schedules_menangkap_effective_yang_lebih_awal(self):
        """Schedule due walaupun next_schedule mentahnya masih di masa depan.

        Ini konsekuensi snap mundur: next_schedule 24 Agustus, tapi effective
        schedule-nya 21 Agustus. Prefilter SQL - yang sekarang punya batas bawah
        di awal hari berjalan - harus tetap menangkapnya, kalau tidak periode
        ini terlewat diam-diam.
        """
        schedule = self._create_schedule()
        running_dt = datetime(2026, 8, 21, 1, 0)  # 21 Agu 08:00 WIB = effective

        due = self.env['snk.stock.schedule']._get_due_schedules(
            site_ids=[self.site.id], running_dt=running_dt)

        self.assertIn(schedule, due)

    def test_due_schedules_tidak_menarik_periode_terlewat(self):
        """Effective 21 Agu tidak ikut terangkut saat cron jalan 25 Agu."""
        schedule = self._create_schedule()
        running_dt = datetime(2026, 8, 25, 1, 0)

        due = self.env['snk.stock.schedule']._get_due_schedules(
            site_ids=[self.site.id], running_dt=running_dt)

        self.assertNotIn(schedule, due)

    # ------------------------------------------------------------------
    # Roll-forward periode terlewat
    # ------------------------------------------------------------------
    def test_rollforward_memajukan_ke_periode_terdekat(self):
        """Effective 21 Agu, week 3, cron baru jalan 30 Sep -> maju ke 2 Okt.

        Periode 11 Sep dilewati (tidak dijalankan susulan), tapi polanya tetap
        mendarat di Jumat dan schedule-nya hidup lagi.
        """
        schedule = self._create_schedule()
        running_dt = datetime(2026, 9, 30, 1, 0)   # 30 Sep 08:00 WIB

        rolled = self.env['snk.stock.schedule']._rollforward_overdue(
            site_ids=[self.site.id], running_dt=running_dt)

        next_local = schedule._to_local(schedule.next_schedule)
        self.assertEqual(rolled, 1)
        self.assertEqual(next_local.date().isoformat(), '2026-10-02')
        self.assertEqual(next_local.weekday(), 4, "pola harus tetap di Jumat")
        self.assertEqual(next_local.hour, 8, "jam jadwal dipertahankan")
        self.assertEqual(schedule.processing_status, 'pending')

    def test_rollforward_hasilnya_due_di_tanggal_barunya(self):
        """Setelah dimajukan, schedule harus benar-benar due di tanggal barunya."""
        schedule = self._create_schedule()
        self.env['snk.stock.schedule']._rollforward_overdue(
            site_ids=[self.site.id], running_dt=datetime(2026, 9, 30, 1, 0))

        running_dt = schedule._to_utc(datetime(2026, 10, 2, 6, 0))

        self.assertTrue(schedule._is_due(running_dt))

    def test_rollforward_tidak_menyentuh_yang_belum_lewat(self):
        """Schedule yang periodenya hari ini atau di depan tidak boleh digeser."""
        schedule = self._create_schedule()
        next_sebelum = schedule.next_schedule

        rolled = self.env['snk.stock.schedule']._rollforward_overdue(
            site_ids=[self.site.id], running_dt=datetime(2026, 8, 21, 1, 0))  # tepat di effective

        self.assertEqual(rolled, 0)
        self.assertEqual(schedule.next_schedule, next_sebelum)

    def test_rollforward_idempotent(self):
        """Dijalankan dua kali berturut-turut, yang kedua tidak menggeser apa pun."""
        schedule = self._create_schedule()
        Schedule = self.env['snk.stock.schedule']
        running_dt = datetime(2026, 9, 30, 1, 0)

        Schedule._rollforward_overdue(site_ids=[self.site.id], running_dt=running_dt)
        next_setelah_run_pertama = schedule.next_schedule

        self.assertEqual(
            Schedule._rollforward_overdue(
                site_ids=[self.site.id], running_dt=running_dt), 0)
        self.assertEqual(schedule.next_schedule, next_setelah_run_pertama)
