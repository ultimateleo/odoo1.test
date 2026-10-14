from odoo import models, fields, api, _
from odoo.exceptions import ValidationError
from datetime import timedelta
import logging
import pytz

_logger = logging.getLogger(__name__)

# Timezone acuan penentuan hari jadwal kalau company belum punya tz sendiri.
SCHEDULE_TZ_FALLBACK = 'Asia/Jakarta'

# Map Selection day (0=Minggu ... 6=Sabtu) ke weekday() Python (0=Senin ... 6=Minggu).
SELECTION_TO_PY_WEEKDAY = {'0': 6, '1': 0, '2': 1, '3': 2, '4': 3, '5': 4, '6': 5}

# Effective schedule paling jauh 6 hari SEBELUM next_schedule (snap mundur ke
# Schedule Day). Karena due sekarang dibatasi pada TANGGAL cron berjalan,
# effective yang jatuh hari ini hanya mungkin berasal dari next_schedule di
# rentang [hari ini 00:00, hari ini + 7 hari). Dipakai sebagai prefilter di SQL
# supaya index next_schedule tetap terpakai dan kita tidak perlu menarik seluruh
# tabel ke Python cuma untuk mengecek due.
DUE_PREFILTER_DAYS = 7


class SnkStockSchedule(models.Model):
    _name = 'snk.stock.schedule'
    _description = 'SNK Stock Schedule'

    # name = fields.Char(string='Name', required=True)
    active = fields.Boolean(
        string='Active', default=True,
        help='Uncheck untuk menonaktifkan schedule tanpa menghapus datanya.')
    site_id = fields.Many2one('snk.site', string='Site', required=True, help="Site")
    site_name = fields.Char(string='Site Name' , related='site_id.snk_name', store=True)
    warehouse_id = fields.Many2one('stock.warehouse', string='Warehouse', related='site_id.warehouse_id', store=True)
    day = fields.Selection([
        ('0', 'Minggu'),
        ('1', 'Senin'),
        ('2', 'Selasa'),
        ('3', 'Rabu'),
        ('4', 'Kamis'),
        ('5', 'Jumat'),
        ('6', 'Sabtu'),
    ], string='Schedule Day', help='Day of checking stock for replenishment.')
    week = fields.Integer(string='Schedule Week', help='Number of week of checking stock for replenishment.')
    sto_day = fields.Integer(string='To Do STO', help='Number of day to doing transfer from scheduled MRP.')
    # last_schedule menyimpan EFFECTIVE schedule periode sebelumnya - tanggal dan
    # jam yang benar-benar dieksekusi setelah snap ke Schedule Day - bukan salinan
    # next_schedule lama yang cuma acuan minggu. Disimpan UTC seperti Datetime
    # lainnya, ditampilkan Odoo dalam tz user.
    last_schedule = fields.Datetime(
        string='Last Schedule',
        help='Effective schedule periode terakhir yang berhasil dijalankan '
             '(sudah mengikuti Schedule Day, bukan acuan minggu).')
    next_schedule = fields.Datetime(
        string='Next Schedule',
        help='Acuan minggu jadwal berikutnya. Hari eksekusi sebenarnya '
             'ditentukan Schedule Day (lihat Effective Schedule).')

    log_ids = fields.One2many(
        'snk.stock.schedule.log', 'schedule_id', string='Execution Log', readonly=True)

    # ============================================================================
    # ADAPTIVE SCHEDULING FIELDS - For Queue-Based Processing
    # ============================================================================
    processing_status = fields.Selection([
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ], default='pending', string='Processing Status',
       help='Current processing status for queue-based scheduling')

    last_processed_date = fields.Date(
        'Last Processed Date',
        help='Last date this site was successfully processed'
    )

    processing_priority = fields.Integer(
        'Priority',
        default=100,
        help='Processing priority (lower number = higher priority). Used for sorting in queue.'
    )

    processing_attempt = fields.Integer(
        'Attempt Count',
        default=0,
        help='Number of processing attempts (increments on each try, resets on success)'
    )

    processing_error = fields.Text(
        'Last Error',
        help='Last error message if processing failed'
    )

    processing_duration = fields.Float(
        'Last Processing Duration (seconds)',
        help='Duration of last successful processing in seconds'
    )

    processing_last_run = fields.Datetime(
        'Last Run Time',
        help='Timestamp of last processing attempt (success or fail)'
    )

    # ============================================================================
    # INDEXING FOR PERFORMANCE
    # ============================================================================
    def init(self):
        """Create PostgreSQL indexes for adaptive scheduling queries"""
        _logger.debug("Creating indexes for snk.stock.schedule (adaptive scheduling)...")

        # Index for main queue query: next_schedule + processing status
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS idx_snk_stock_schedule_queue_lookup
            ON snk_stock_schedule USING btree(next_schedule, processing_status, last_processed_date, processing_priority)
            WHERE next_schedule IS NOT NULL AND site_id IS NOT NULL
        """)
        _logger.debug("✓ Created composite index: idx_snk_stock_schedule_queue_lookup")

        # Index for failed sites query
        self.env.cr.execute("""
            CREATE INDEX IF NOT EXISTS idx_snk_stock_schedule_failed
            ON snk_stock_schedule USING btree(processing_status, processing_attempt)
            WHERE processing_status = 'failed'
        """)
        _logger.debug("✓ Created index: idx_snk_stock_schedule_failed")

        _logger.debug("All adaptive scheduling indexes created successfully!")

    # ============================================================================
    # CONSTRAINTS
    # ============================================================================
    @api.constrains('day', 'week')
    def _check_schedule_config(self):
        """Schedule Day dan Schedule Week adalah dua parameter dari satu rumus.

        Tanpa 'day', effective schedule tidak bisa ditentukan sehingga jadwal
        ikut ke hari apa pun yang kebetulan tersimpan di next_schedule. Tanpa
        'week' > 0, next_schedule tidak pernah maju dan periode yang sama akan
        diproses berulang-ulang.
        """
        for schedule in self:
            label = schedule.site_id.snk_name or schedule.site_id.snk_code or schedule.id
            if not schedule.day:
                raise ValidationError(_(
                    "Schedule Day wajib diisi (site: %s).", label))
            if not schedule.week or schedule.week <= 0:
                raise ValidationError(_(
                    "Schedule Week harus lebih besar dari 0 (site: %s).", label))

    # ============================================================================
    # HELPER METHODS
    # ============================================================================
    @api.model
    def reset_daily_processing_status(self):
        """
        Reset processing status for new day
        Called by daily cron at midnight

        UPDATED LOGIC:
        - Only reset schedules whose next_schedule has passed (ready to be processed again)
        - This works with auto-updated next_schedule from Fixed Batch processor
        """
        today = self._today_local()
        now = fields.Datetime.now()

        # Reset schedules that:
        # 1. Were processed before today OR
        # 2. Have next_schedule that has already passed (ready for processing)
        # 3. Are in completed/failed/processing status
        schedules = self.search([
            '|',
            ('last_processed_date', '<', today),
            ('next_schedule', '<=', now),
            ('processing_status', 'in', ['completed', 'failed', 'processing']),
        ])

        if schedules:
            schedules.write({
                'processing_status': 'pending',
                'processing_attempt': 0,
            })
            _logger.debug("[MRP SCHEDULE] Reset %s schedule untuk pemrosesan hari baru", len(schedules))
        else:
            _logger.debug("[MRP SCHEDULE] Tidak ada schedule yang perlu di-reset")

        return True

    @api.model
    def get_stuck_processing_schedules(self):
        """
        Find schedules stuck in 'processing' state for > 1 hour
        These might have crashed and need to be reset
        """
        one_hour_ago = fields.Datetime.now() - timedelta(hours=1)

        stuck_schedules = self.search([
            ('processing_status', '=', 'processing'),
            ('processing_last_run', '<', one_hour_ago),
        ])

        if stuck_schedules:
            _logger.warning(
                "[MRP SCHEDULE] %s schedule nyangkut di status processing, di-reset ke pending",
                len(stuck_schedules))
            stuck_schedules.write({
                'processing_status': 'pending',
            })

        return stuck_schedules

    # ============================================================================
    # TIMEZONE HELPER - acuan penentuan hari jadwal
    # ============================================================================
    def _schedule_tz(self):
        """Timezone acuan untuk menentukan hari jadwal.

        Sengaja memakai timezone company, BUKAN timezone user yang menjalankan
        cron. Hari jadwal itu properti kalender perusahaan, bukan preferensi
        tampilan per user: kalau ikut user, dua user dengan tz berbeda bisa
        menghasilkan next_schedule berbeda untuk site yang sama, dan cron yang
        jalan tanpa tz akan diam-diam jatuh kembali ke UTC.
        """
        return pytz.timezone(self.env.company.partner_id.tz or SCHEDULE_TZ_FALLBACK)

    def _to_local(self, dt_utc):
        """Datetime naive UTC (nilai ORM) -> datetime naive waktu lokal."""
        return pytz.UTC.localize(dt_utc).astimezone(self._schedule_tz()).replace(tzinfo=None)

    def _to_utc(self, dt_local):
        """Kebalikan _to_local: datetime naive lokal -> naive UTC untuk disimpan ORM."""
        return self._schedule_tz().localize(dt_local).astimezone(pytz.UTC).replace(tzinfo=None)

    @api.model
    def _add_calendar_days(self, dt_utc, days):
        """Maju `days` HARI KALENDER dari dt_utc, lalu lompati weekend.

        Urutannya penting: HITUNG DULU sampai selesai, baru geser kalau tanggal
        JATUHNYA kena Sabtu/Minggu. Weekend ikut dihitung sebagai hari; tidak
        ada pengecekan weekend di tengah perhitungan, dan tanggal awal tidak
        pernah digeser lebih dulu:

            Rabu 9 Sep + 7 = Rabu 16 Sep     (bukan weekend, tidak digeser)
            Selasa 8 Sep + 4 = Sabtu 12 Sep  -> digeser ke Senin 14 Sep
            Selasa 8 Sep + 5 = Minggu 13 Sep -> digeser ke Senin 14 Sep
            Sabtu 19 Sep + 2 = Senin 21 Sep  (awal di weekend TIDAK digeser dulu;
                                              kalau digeser dulu jadi Rabu 23 - salah)

        Karena itu pemanggil yang merangkai beberapa jeda (PR date -> STO Date
        -> Scheduled Date DO) harus menjumlahkan jedanya dan menghitung dari
        tanggal MENTAH, bukan memanggil ini berulang atas hasil yang sudah
        digeser. Menggeser di tengah rantai lalu menghitung dari hasil geseran
        bukan hitung hari kalender lagi - rantainya melar.

        Beda dengan perhitungan hari kerja yang tidak menghitung Sabtu/Minggu
        sama sekali: di sana setiap weekend yang dilewati menambah jeda 2 hari
        sehingga makin besar `days` makin melar. Di sini jeda tetap `days`,
        paling banyak bertambah 2 hari kalau jatuhnya di weekend.

        Weekday dievaluasi di waktu LOKAL company, bukan atas nilai UTC. ORM
        menyimpan Datetime dalam UTC dan weekday() atas nilai UTC pernah membaca
        Jumat 18:00 UTC (= Sabtu 01:00 WIB) sebagai Jumat. Pola yang sama sudah
        dipakai untuk ETA receipt di stock_picking._compute_next_pickings().

        Jam pada dt_utc dipertahankan - yang digeser hanya tanggalnya.

        Args:
            dt_utc (datetime): naive UTC dari ORM. Falsy dikembalikan apa adanya.
            days (int): jumlah hari kalender. Boleh 0 - weekend tetap dilompati.

        Returns:
            datetime: naive UTC siap disimpan ORM.
        """
        if not dt_utc:
            return dt_utc

        local_dt = self._to_local(dt_utc) + timedelta(days=days or 0)
        # weekday(): 0=Senin ... 4=Jumat, 5=Sabtu, 6=Minggu.
        while local_dt.weekday() >= 5:
            local_dt += timedelta(days=1)

        return self._to_utc(local_dt)

    @api.model
    def _today_local(self):
        """Tanggal hari ini menurut timezone company (WIB), bukan timezone user.

        fields.Date.context_today() mengikuti tz user yang sedang berjalan. Cron
        MRP berjalan sebagai base.user_root yang tz-nya umumnya kosong, sehingga
        hasilnya diam-diam jatuh kembali ke UTC: antara 00:00-07:00 WIB tanggal
        yang didapat mundur satu hari. Akibatnya last_processed_date, tanggal
        Transfer Order, dan reset harian tercatat di hari yang salah.

        Sama alasannya dengan _schedule_tz(): kalender MRP itu properti
        perusahaan, bukan preferensi tampilan per user.
        """
        return self._to_local(fields.Datetime.now()).date()

    @api.model
    def _local_day_start_utc(self, running_dt=None, offset_days=0):
        """Awal hari (00:00:00) waktu lokal company, dikembalikan sebagai naive UTC.

        Dipakai untuk membangun batas window due di level SQL. Batas harus
        dihitung di waktu LOKAL lalu dikonversi, bukan langsung memotong nilai
        UTC - alasannya sama dengan _today_local(): 00:00 WIB itu 17:00 UTC hari
        sebelumnya, jadi memotong di UTC menggeser window tujuh jam.

        Args:
            running_dt (datetime): naive UTC acuan. Default fields.Datetime.now().
            offset_days (int): geser N hari dari hari acuan.

        Returns:
            datetime: naive UTC.
        """
        running_local = self._to_local(running_dt or fields.Datetime.now())
        start_local = running_local.replace(
            hour=0, minute=0, second=0, microsecond=0) + timedelta(days=offset_days)
        return self._to_utc(start_local)

    # ============================================================================
    # EFFECTIVE SCHEDULE - tanggal sebenarnya suatu periode harus jalan
    # ============================================================================
    def _get_effective_schedule_local(self):
        """Effective Schedule dalam waktu lokal (naive).

        Effective Schedule = Schedule Day TERAKHIR yang <= next_schedule.
        next_schedule hanya acuan minggu; yang menentukan HARI adalah Schedule Day.

            next_schedule : Senin, 24 Agustus 2026
            Schedule Day  : Jumat
            effective     : Jumat, 21 Agustus 2026   (mundur 3 hari)

        Snap ini hanya berdampak sekali. Setelah satu periode sukses,
        next_schedule selalu mendarat tepat di Schedule Day sehingga snap
        berikutnya jadi no-op dan polanya stabil.

        Jam pada next_schedule dipertahankan apa adanya - yang digeser hanya
        tanggalnya.

        Returns:
            datetime naive lokal, atau False kalau konfigurasi belum lengkap.
        """
        self.ensure_one()
        if not self.next_schedule or not self.day:
            return False

        target_py_day = SELECTION_TO_PY_WEEKDAY.get(self.day)
        if target_py_day is None:
            return False

        local_dt = self._to_local(self.next_schedule)
        # Selisih mundur 0..6 hari ke Schedule Day terdekat sebelum/pada next_schedule.
        days_back = (local_dt.weekday() - target_py_day) % 7
        return local_dt - timedelta(days=days_back)

    def _get_effective_schedule(self):
        """Sama dengan _get_effective_schedule_local, tapi dalam naive UTC.

        Dipakai untuk perbandingan dengan fields.Datetime.now() dan domain ORM,
        yang keduanya bekerja di UTC.
        """
        local_dt = self._get_effective_schedule_local()
        return self._to_utc(local_dt) if local_dt else False

    def _is_due(self, running_dt=None):
        """Schedule due HANYA kalau effective schedule jatuh pada TANGGAL cron berjalan.

        Window-nya satu hari penuh waktu lokal company: 00:00:00-23:59:59. Jadi
        jam pada next_schedule tidak menentukan kapan periode dieksekusi - yang
        dibandingkan tanggalnya. Schedule berjam 20:00 tetap jalan di cron
        pertama hari itu.

            cron jalan 8 Sep 2026
            effective  8 Sep 2026 (jam berapa pun) -> due
            effective  4 Sep 2026 (terlewat)       -> TIDAK due
            effective 11 Sep 2026                  -> belum due

        Perilaku sebelumnya `now >= effective` terbuka ke belakang: semua periode
        yang terlewat ikut terangkut pada hari cron akhirnya berjalan, sehingga
        satu run bisa memproses periode milik minggu-minggu sebelumnya. Periode
        terlewat sekarang tidak dikejar; yang menjaga schedule-nya tidak mati
        permanen adalah _rollforward_overdue().

        Args:
            running_dt: datetime naive UTC. Default fields.Datetime.now().
        """
        self.ensure_one()
        effective_local = self._get_effective_schedule_local()
        if not effective_local:
            return False
        running_local = self._to_local(running_dt or fields.Datetime.now())
        return effective_local.date() == running_local.date()

    def _is_overdue(self, running_dt=None):
        """True kalau effective schedule sudah lewat tanpa pernah dijalankan.

        Kebalikan dari _is_due() untuk sisi belakang window: periode yang
        tanggalnya sudah lewat. Dengan window harian, record seperti ini tidak
        akan pernah due lagi karena next_schedule cuma maju saat periodenya
        sukses diproses.

        Args:
            running_dt: datetime naive UTC. Default fields.Datetime.now().
        """
        self.ensure_one()
        effective_local = self._get_effective_schedule_local()
        if not effective_local:
            return False
        running_local = self._to_local(running_dt or fields.Datetime.now())
        return effective_local.date() < running_local.date()

    def _is_period_done(self, effective_date):
        """True kalau periode (schedule + effective_date) sudah pernah SUKSES.

        Ini pengaman anti eksekusi ganda yang sebenarnya - status processing
        saja tidak cukup karena bisa di-reset cron lain atau manual dari UI.
        """
        self.ensure_one()
        return bool(self.env['snk.stock.schedule.log'].search_count([
            ('schedule_id', '=', self.id),
            ('effective_date', '=', effective_date),
            ('state', '=', 'success'),
        ]))

    @api.model
    def _get_due_schedules(self, site_ids=None, statuses=None, limit=None, running_dt=None):
        """Kumpulan schedule yang jatuh tempo HARI INI, terurut prioritas.

        Due-check tidak bisa sepenuhnya dilakukan di SQL karena effective
        schedule butuh konversi timezone per record. Yang dilakukan di SQL
        adalah prefilter yang terbukti aman: effective schedule paling jauh
        6 hari sebelum next_schedule, jadi effective yang jatuh hari ini hanya
        mungkin berasal dari next_schedule di rentang [hari ini 00:00,
        hari ini + 7 hari). Sisanya disaring di Python atas himpunan kecil,
        bukan atas seluruh tabel.

        Batas BAWAH window itu yang baru: sebelumnya prefilter cuma punya batas
        atas karena periode terlewat memang ikut diproses. Sekarang next_schedule
        yang sudah lewat tidak perlu ditarik sama sekali - urusan periode
        terlewat ada di _rollforward_overdue().

        limit diterapkan SETELAH penyaringan due - kalau dipasang di search(),
        batch bisa terisi penuh oleh schedule yang ternyata belum jatuh tempo.
        """
        running_dt = running_dt or fields.Datetime.now()

        domain = [
            ('next_schedule', '!=', False),
            ('next_schedule', '>=', self._local_day_start_utc(running_dt)),
            ('next_schedule', '<', self._local_day_start_utc(
                running_dt, offset_days=DUE_PREFILTER_DAYS)),
            ('site_id', '!=', False),
        ]
        if site_ids:
            domain.append(('site_id', 'in', site_ids))
        if statuses:
            domain.append(('processing_status', 'in', statuses))

        candidates = self.search(
            domain, order='processing_priority asc, processing_attempt asc, id asc')
        due = candidates.filtered(lambda s: s._is_due(running_dt))

        return due[:limit] if limit else due

    # ============================================================================
    # PERIODE TERLEWAT - konsekuensi window due harian
    # ============================================================================
    @api.model
    def _get_overdue_schedules(self, site_ids=None, running_dt=None):
        """Schedule yang effective schedule-nya sudah lewat tanpa pernah dijalankan.

        Prefilter SQL memakai batas atas yang sama dengan _get_due_schedules()
        tapi tanpa batas bawah: next_schedule yang masih di rentang minggu ini
        pun bisa punya effective di masa lalu kalau snap mundur ke Schedule Day
        melompati hari ini (mis. next_schedule Sabtu dengan Schedule Day Minggu
        -> effective mundur 6 hari).
        """
        running_dt = running_dt or fields.Datetime.now()

        domain = [
            ('next_schedule', '!=', False),
            ('next_schedule', '<', self._local_day_start_utc(
                running_dt, offset_days=DUE_PREFILTER_DAYS)),
            ('site_id', '!=', False),
            ('day', '!=', False),
        ]
        if site_ids:
            domain.append(('site_id', 'in', site_ids))

        candidates = self.search(domain, order='id asc')

        return candidates.filtered(lambda s: s._is_overdue(running_dt))

    @api.model
    def _rollforward_overdue(self, site_ids=None, running_dt=None):
        """Majukan next_schedule schedule yang periodenya sudah terlewat.

        Ini pasangan wajib dari window due harian. next_schedule hanya maju
        kalau periodenya BERHASIL diproses, jadi begitu satu periode lewat tanpa
        dijalankan (cron mati, server maintenance, site baru diisi tanggal lama)
        record-nya jatuh permanen di luar window dan tidak akan pernah due lagi.

        Yang dilakukan di sini BUKAN menjalankan periode yang terlewat - itu
        justru yang dilarang - tapi memajukan acuannya ke periode terdekat yang
        belum lewat, kelipatan `week` dari effective schedule lama supaya pola
        jadwal tetap mendarat di Schedule Day yang sama.

            effective 21 Agu, week 3, cron baru jalan 30 Sep
            -> 11 Sep, 2 Okt: dimajukan ke 2 Okt (2 periode dilewati)

        Jumlah periode dihitung langsung dengan pembagian, bukan loop bertambah
        satu-satu - schedule yang tertinggal setahun tidak boleh jadi 17 iterasi
        per record.

        Returns:
            int: jumlah schedule yang dimajukan.
        """
        running_dt = running_dt or fields.Datetime.now()
        today = self._to_local(running_dt).date()

        overdue = self._get_overdue_schedules(site_ids=site_ids, running_dt=running_dt)
        if not overdue:
            _logger.debug("[MRP SCHEDULE] Tidak ada periode terlewat yang perlu dimajukan")
            return 0

        rolled = 0
        skipped_info = []
        for schedule in overdue:
            if not schedule.week or schedule.week <= 0:
                # Sudah dijaga _check_schedule_config, tapi data lama bisa lolos.
                # Tanpa guard ini pembagian di bawah jadi ZeroDivisionError.
                continue

            effective_local = schedule._get_effective_schedule_local()
            period_days = schedule.week * 7
            behind_days = (today - effective_local.date()).days
            # Pembulatan ke atas: maju sesedikit mungkin sampai tidak lagi di masa lalu.
            periods = -(-behind_days // period_days)
            next_local = effective_local + timedelta(days=periods * period_days)

            schedule.write({
                'next_schedule': schedule._to_utc(next_local),
                'processing_status': 'pending',
                'processing_attempt': 0,
                'processing_error': _(
                    "Periode %(missed)s terlewat (cron tidak berjalan pada tanggalnya). "
                    "Jadwal dimajukan ke %(next)s, %(count)s periode dilewati.",
                    missed=effective_local.date(), next=next_local.date(), count=periods),
            })
            rolled += 1
            if len(skipped_info) < 5:
                skipped_info.append("%s (%s -> %s)" % (
                    schedule.site_id.snk_name or schedule.id,
                    effective_local.date(), next_local.date()))

        if rolled:
            _logger.warning(
                "[MRP SCHEDULE] %s schedule punya periode terlewat dan dimajukan "
                "ke periode berikutnya (tidak dijalankan susulan): %s%s",
                rolled, ', '.join(skipped_info), '...' if rolled > 5 else '')

        return rolled

    def calculate_next_schedule(self):
        """Jadwal berikutnya = Effective Schedule + (week x 7 hari).

        Dihitung dari effective schedule LAMA, bukan dari tanggal cron benar-benar
        berjalan. Kalau cron telat beberapa jam dalam hari yang sama, pola jadwal
        tidak ikut bergeser:

            effective 21 Agustus + 3 minggu = 11 September     (benar)
            running   25 Agustus + 3 minggu = 15 September     (salah)

        Sejak due dibatasi pada hari berjalan, selisih running vs effective tidak
        pernah lebih dari satu hari - tapi perhitungan tetap bertumpu pada
        effective schedule supaya sumber kebenaran jadwal cuma satu.

        TIDAK ADA lagi penyesuaian hari kerja. Hari jadwal sekarang ditentukan
        penuh oleh Schedule Day, jadi filter weekend lama justru menggeser jadwal
        yang sudah benar - user memilih Jumat, filter memindahkannya ke Senin.
        Kalau user memang memilih Sabtu, jadwalnya memang Sabtu.

        Yang TETAP dipertahankan adalah perhitungan di waktu LOKAL. ORM menyimpan
        Datetime dalam UTC sementara user melihatnya dalam WIB; weekday() yang
        dievaluasi langsung atas nilai UTC pernah membaca Jumat 18:00 UTC
        (= Sabtu 01:00 WIB) sebagai Jumat dan mendaratkan 148 schedule di Sabtu.

        Returns:
            datetime: naive UTC siap disimpan ORM, atau False kalau konfigurasi
            schedule belum lengkap.
        """
        self.ensure_one()

        if not self.next_schedule:
            _logger.warning(
                "[MRP SCHEDULE] Schedule %s tidak punya next_schedule, "
                "tidak bisa hitung jadwal berikutnya", self.id)
            return False

        if not self.day:
            _logger.warning(
                "[MRP SCHEDULE] Schedule %s belum punya Schedule Day, "
                "tidak bisa hitung effective schedule", self.id)
            return False

        if not self.week or self.week <= 0:
            _logger.warning(
                "[MRP SCHEDULE] Schedule %s punya Schedule Week %s (harus > 0), "
                "jadwal berikutnya tidak bisa dihitung", self.id, self.week)
            return False

        effective_local = self._get_effective_schedule_local()
        next_local = effective_local + timedelta(weeks=self.week)

        _logger.debug(
            "[MRP SCHEDULE] Schedule %s: effective %s + %s minggu -> %s (%s)",
            self.id, effective_local, self.week, next_local,
            self._get_day_name(self.day))

        return self._to_utc(next_local)

    def _get_day_name(self, day):
        """Helper to get day name for logging"""
        day_names = {
            '0': 'Minggu',
            '1': 'Senin',
            '2': 'Selasa',
            '3': 'Rabu',
            '4': 'Kamis',
            '5': 'Jumat',
            '6': 'Sabtu',
        }
        return day_names.get(day, 'Unknown')
