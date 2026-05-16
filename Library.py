#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║           TUI Music Player               ║
║        — Versi Final —                   ║
╚══════════════════════════════════════════╝

Cara install:
    pip install pygame mutagen

Struktur folder:
    proyek/
    ├── library.py        ← file ini
    ├── library.db        ← database otomatis dibuat
    ├── player.log        ← log error otomatis dibuat
    └── music/            ← taruh lagu di sini
        ├── lagu1.mp3
        ├── lagu2.flac
        └── subfolder/
            └── lagu3.ogg

Cara jalankan:
    python3 library.py

Kontrol:
    ↑ / ↓      navigasi daftar lagu
    ENTER       putar lagu yang dipilih
    SPACE       pause / resume
    n / p       lagu berikutnya / sebelumnya
    + / -       volume naik / turun
    /           mulai mengetik pencarian
    ESC         keluar mode pencarian
    1 / 2       ganti tab (Semua / Cari)
    r           scan ulang folder music/
    q           keluar
"""

# ─────────────────────────────────────────
#  Redirect stderr ke log file — 2 LEVEL
#  Harus dilakukan SEBELUM import lain
#
#  Level 1 → sys.stderr : tangkap error Python
#  Level 2 → os.dup2()  : tangkap error library C
#             (libmpg123, libFLAC, dll) yang
#             menulis langsung ke fd 2 sistem,
#             melewati sys.stderr Python.
#             Inilah penyebab pesan:
#             [src/libmpg123/id3.c:process_comment()]
#             muncul di layar dan merusak tampilan.
# ─────────────────────────────────────────
import sys, os
from pathlib import Path

_BASE = Path(__file__).parent.resolve()
_LOG  = open(_BASE / "player.log", "a", buffering=1)

# Level 1: Python stderr
sys.stderr = _LOG

# Level 2: OS file descriptor — tangkap output C library
os.dup2(_LOG.fileno(), 2)

# ─────────────────────────────────────────
#  Import standar
# ─────────────────────────────────────────
import curses
import sqlite3
import time
import threading
import warnings

# ─────────────────────────────────────────
#  Import library audio & metadata
# ─────────────────────────────────────────
try:
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

try:
    import vlc
    # Test apakah VLC benar-benar bisa dipakai
    _vlc_inst = vlc.Instance("--no-xlib", "--quiet", "--no-video")
    VLC_OK    = True
except Exception:
    VLC_OK = False

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from mutagen import File as MutagenFile
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

# ─────────────────────────────────────────
#  Konfigurasi path & format
# ─────────────────────────────────────────
MUSIC_DIR     = _BASE / "music"
DB_PATH       = _BASE / "library.db"
AUDIO_FORMATS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac"}

# Konstanta layout layar
_TOP = 2   # baris terpakai di atas  (tabbar + separator)
_BOT = 4   # baris terpakai di bawah (separator + nowplaying + status + help)


# ══════════════════════════════════════════
#  SETUP FOLDER MUSIC/
# ══════════════════════════════════════════
def setup_music_folder():
    """Buat folder music/ dan README jika belum ada."""
    MUSIC_DIR.mkdir(exist_ok=True)
    readme = MUSIC_DIR / "README.txt"
    if not readme.exists():
        readme.write_text(
            "Taruh file musik kamu di folder ini.\n"
            "Format yang didukung: mp3, flac, ogg, wav, m4a, aac\n"
            "Subfolder juga terbaca otomatis.\n\n"
            "Setelah menambah lagu, tekan  r  di player untuk refresh.\n",
            encoding="utf-8"
        )


# ══════════════════════════════════════════
#  KELAS: Library  (Database)
# ══════════════════════════════════════════
class Library:
    """
    Kelola koleksi lagu dengan SQLite.
    - Scan folder music/ secara otomatis
    - Simpan metadata agar tidak perlu scan ulang tiap buka
    - Cari lagu berdasarkan judul, artis, atau album
    """

    def __init__(self):
        self.conn  = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._init_db()

        # Status scan (dipakai progress bar di TUI)
        self.scanning   = False
        self.scan_done  = 0
        self.scan_total = 0

    def _init_db(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS songs (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    path     TEXT UNIQUE NOT NULL,
                    title    TEXT NOT NULL DEFAULT '',
                    artist   TEXT NOT NULL DEFAULT '',
                    album    TEXT NOT NULL DEFAULT '',
                    duration REAL NOT NULL DEFAULT 0
                )
            """)
            self.conn.commit()

    # ── Scan folder music/ ────────────────
    def scan(self, on_done=None):
        """Scan folder music/ di background (tidak memblokir TUI)."""
        t = threading.Thread(
            target=self._scan_worker, args=(on_done,), daemon=True
        )
        t.start()

    def _scan_worker(self, on_done):
        self.scanning  = True
        self.scan_done = 0

        # Kumpulkan semua file audio
        all_files = sorted(
            f for f in MUSIC_DIR.rglob("*")
            if f.suffix.lower() in AUDIO_FORMATS
        )
        self.scan_total = len(all_files)

        # Path yang sudah ada di DB — lewati agar tidak proses ulang
        with self._lock:
            existing = {
                r[0] for r in
                self.conn.execute("SELECT path FROM songs").fetchall()
            }

        batch = []
        for f in all_files:
            self.scan_done += 1
            if str(f) in existing:
                continue
            batch.append(self._read_meta(f))
            if len(batch) >= 30:
                self._insert(batch)
                batch.clear()

        if batch:
            self._insert(batch)

        self.scanning = False
        if on_done:
            on_done(len(all_files))

    def _insert(self, songs: list):
        with self._lock:
            self.conn.executemany(
                "INSERT OR IGNORE INTO songs "
                "(path,title,artist,album,duration) "
                "VALUES (:path,:title,:artist,:album,:duration)",
                songs
            )
            self.conn.commit()

    def _read_meta(self, filepath: Path) -> dict:
        """Baca metadata dari file audio. Fallback ke nama file jika gagal."""
        title  = filepath.stem
        artist = ""
        album  = ""
        dur    = 0.0

        if MUTAGEN_OK:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    audio = MutagenFile(str(filepath))

                if audio:
                    def _tag(keys):
                        for k in keys:
                            if k in audio:
                                v = audio[k]
                                return str(
                                    v[0] if isinstance(v, list) else v
                                ).strip()
                        return ""

                    title  = _tag(["title","TIT2","\xa9nam"]) or title
                    artist = _tag(["artist","TPE1","\xa9ART"])
                    album  = _tag(["album","TALB","\xa9alb"])
                    dur    = getattr(
                        getattr(audio, "info", None), "length", 0.0
                    )
            except Exception as e:
                print(f"[meta] {filepath.name}: {e}", file=sys.__stderr__)

        return {
            "path"    : str(filepath),
            "title"   : title[:200],
            "artist"  : artist[:200],
            "album"   : album[:200],
            "duration": float(dur),
        }

    # ── Query ─────────────────────────────
    def all_songs(self) -> list:
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM songs ORDER BY artist,album,title"
            ).fetchall()]

    def search(self, q: str) -> list:
        if not q.strip():
            return self.all_songs()
        p = f"%{q.strip()}%"
        with self._lock:
            return [dict(r) for r in self.conn.execute(
                "SELECT * FROM songs "
                "WHERE title LIKE ? OR artist LIKE ? OR album LIKE ? "
                "ORDER BY artist,album,title",
                (p, p, p)
            ).fetchall()]

    def count(self) -> int:
        with self._lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM songs"
            ).fetchone()[0]

    def close(self):
        with self._lock:
            self.conn.close()


# ══════════════════════════════════════════
#  KELAS: Player  (Audio Engine)
# ══════════════════════════════════════════
class Player:
    """
    Audio engine dengan fallback otomatis:
      1. Pygame  → coba pertama (ringan, cepat)
      2. VLC     → fallback jika pygame gagal (support semua format)

    Property engine_ menunjukkan engine mana yang aktif saat ini.
    """

    ENGINE_NONE   = "none"
    ENGINE_PYGAME = "pygame"
    ENGINE_VLC    = "vlc"

    def __init__(self):
        self.playing = False
        self.paused  = False
        self.volume  = 0.7
        self.error   = ""
        self._engine = self.ENGINE_NONE   # engine yang sedang aktif
        self._vlc_mp = None               # VLC MediaPlayer instance

        # Inisialisasi pygame
        if PYGAME_OK:
            try:
                pygame.mixer.init(44100, -16, 2, 2048)
                pygame.mixer.music.set_volume(self.volume)
            except Exception as e:
                print(f"[pygame init] {e}", file=sys.__stderr__)

        # Inisialisasi VLC (instance tunggal, hemat resource)
        if VLC_OK:
            try:
                self._vlc_inst = vlc.Instance("--no-xlib", "--quiet", "--no-video")
                self._vlc_mp   = self._vlc_inst.media_player_new()
                self._vlc_mp.audio_set_volume(int(self.volume * 100))
            except Exception as e:
                print(f"[vlc init] {e}", file=sys.__stderr__)
                self._vlc_mp = None

    @property
    def engine_label(self) -> str:
        """Label engine aktif untuk ditampilkan di UI."""
        return {"pygame": "PG", "vlc": "VLC", "none": "--"}[self._engine]

    # ══════════════════════════════════════
    #  PLAY — coba pygame dulu, fallback VLC
    # ══════════════════════════════════════
    def play(self, path: str) -> bool:
        self.error   = ""
        self._engine = self.ENGINE_NONE
        p = Path(path)

        # Validasi file
        if not p.exists():
            self.error = f"File tidak ada: {p.name[:40]}"
            return False
        if p.stat().st_size == 0:
            self.error = f"File kosong: {p.name[:40]}"
            return False

        # ── Coba 1: pygame music loader ───────────────────────────
        if PYGAME_OK:
            try:
                pygame.mixer.music.stop()
                pygame.mixer.music.load(str(p))
                pygame.mixer.music.play()
                self.playing = True
                self.paused  = False
                self._engine = self.ENGINE_PYGAME
                self._stop_vlc()   # pastikan VLC tidak ikut jalan
                return True
            except Exception as e:
                print(f"[pygame] {p.name}: {e}", file=sys.__stderr__)

        # ── Coba 2: VLC fallback ──────────────────────────────────
        if VLC_OK and self._vlc_mp:
            try:
                self._stop_pygame()
                media = self._vlc_inst.media_new(str(p))
                self._vlc_mp.set_media(media)
                self._vlc_mp.audio_set_volume(int(self.volume * 100))
                self._vlc_mp.play()
                # Tunggu sebentar sampai VLC benar-benar mulai
                time.sleep(0.15)
                state = self._vlc_mp.get_state()
                if state not in (vlc.State.Error, vlc.State.Ended):
                    self.playing = True
                    self.paused  = False
                    self._engine = self.ENGINE_VLC
                    return True
                else:
                    self._vlc_mp.stop()
            except Exception as e:
                print(f"[vlc] {p.name}: {e}", file=sys.__stderr__)

        # ── Gagal semua ────────────────────────────────────────────
        self.error   = f"Tidak bisa diputar: {p.name[:35]}"
        self.playing = False
        return False

    # ── Internal stop helpers ─────────────
    def _stop_pygame(self):
        if PYGAME_OK:
            try:
                pygame.mixer.music.stop()
            except Exception:
                pass

    def _stop_vlc(self):
        if VLC_OK and self._vlc_mp:
            try:
                self._vlc_mp.stop()
            except Exception:
                pass

    # ══════════════════════════════════════
    #  KONTROL PLAYBACK
    # ══════════════════════════════════════
    def toggle_pause(self):
        if not self.playing:
            return
        if self._engine == self.ENGINE_PYGAME and PYGAME_OK:
            if self.paused:
                pygame.mixer.music.unpause()
            else:
                pygame.mixer.music.pause()
            self.paused = not self.paused

        elif self._engine == self.ENGINE_VLC and self._vlc_mp:
            self._vlc_mp.pause()   # VLC toggle otomatis
            self.paused = not self.paused

    def stop(self):
        self._stop_pygame()
        self._stop_vlc()
        self.playing = False
        self.paused  = False
        self._engine = self.ENGINE_NONE

    def finished(self) -> bool:
        if not self.playing or self.paused:
            return False
        if self._engine == self.ENGINE_PYGAME and PYGAME_OK:
            return not pygame.mixer.music.get_busy()
        if self._engine == self.ENGINE_VLC and self._vlc_mp:
            state = self._vlc_mp.get_state()
            return state in (vlc.State.Ended, vlc.State.Stopped)
        return False

    # ══════════════════════════════════════
    #  VOLUME
    # ══════════════════════════════════════
    def _apply_volume(self):
        if PYGAME_OK:
            try:
                pygame.mixer.music.set_volume(self.volume)
            except Exception:
                pass
        if VLC_OK and self._vlc_mp:
            try:
                self._vlc_mp.audio_set_volume(int(self.volume * 100))
            except Exception:
                pass

    def vol_up(self):
        self.volume = min(1.0, self.volume + 0.05)
        self._apply_volume()

    def vol_down(self):
        self.volume = max(0.0, self.volume - 0.05)
        self._apply_volume()

    # ══════════════════════════════════════
    #  POSISI
    # ══════════════════════════════════════
    def position(self) -> float:
        if not self.playing:
            return 0.0
        if self._engine == self.ENGINE_PYGAME and PYGAME_OK:
            ms = pygame.mixer.music.get_pos()
            return ms / 1000.0 if ms >= 0 else 0.0
        if self._engine == self.ENGINE_VLC and self._vlc_mp:
            ms = self._vlc_mp.get_time()
            return ms / 1000.0 if ms >= 0 else 0.0
        return 0.0

    def cleanup(self):
        self._stop_vlc()
        if VLC_OK and self._vlc_mp:
            self._vlc_mp.release()
        if PYGAME_OK:
            try:
                pygame.mixer.quit()
            except Exception:
                pass


# ══════════════════════════════════════════
#  KELAS: TUI  (Tampilan Terminal)
# ══════════════════════════════════════════
class TUI:
    """
    Antarmuka terminal dengan 2 tab:
      [1] Semua  — seluruh lagu di database
      [2] Cari   — pencarian real-time
    """

    REFRESH    = 300   # ms antar refresh layar
    TAB_ALL    = 0
    TAB_SEARCH = 1

    def __init__(self, scr, lib: Library, player: Player):
        self.scr    = scr
        self.lib    = lib
        self.player = player

        self.tab = self.TAB_ALL

        # Daftar lagu per tab
        self.all_songs = []
        self.results   = []
        self.query     = ""
        self.typing    = False  # True = mode mengetik pencarian

        # Navigasi
        self.sel    = 0
        self.scroll = 0

        # Antrian putar (lagu yang aktif saat ini)
        self.queue = []
        self.q_idx = 0

        # Pesan notifikasi sementara
        self._msg   = ""
        self._msg_t = 0.0

        self._init_colors()
        curses.curs_set(0)
        self.scr.timeout(self.REFRESH)
        self._reload()

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,   -1)                   # header / separator
        curses.init_pair(2, curses.COLOR_GREEN,  -1)                   # lagu aktif
        curses.init_pair(3, curses.COLOR_BLACK,  curses.COLOR_WHITE)   # baris terpilih
        curses.init_pair(4, curses.COLOR_YELLOW, -1)                   # status bar
        curses.init_pair(5, curses.COLOR_RED,    -1)                   # error / kosong
        curses.init_pair(6, curses.COLOR_WHITE,  curses.COLOR_BLUE)    # tab aktif

    def _reload(self):
        self.all_songs = self.lib.all_songs()
        self.results   = self.lib.search(self.query)

    def notify(self, msg: str, secs: float = 3.0):
        self._msg   = msg
        self._msg_t = time.time() + secs

    # ── List yang aktif sesuai tab ────────
    @property
    def _list(self):
        return self.all_songs if self.tab == self.TAB_ALL else self.results

    # ── Hitung area list yang aman ────────
    def _area(self, h: int, extra_top: int = 0) -> int:
        """
        Hitung tinggi area daftar lagu agar tidak pernah
        menimpa baris bawah (separator, now playing, status, help).
        extra_top = baris tambahan di atas list (mis. header kolom, kotak cari)
        """
        used = _TOP + extra_top + _BOT
        return max(1, h - used)

    # ══════════════════════════════════════
    #  LOOP UTAMA
    # ══════════════════════════════════════
    def run(self):
        # Scan saat pertama buka
        def _done(total):
            self._reload()
            self.notify(f"Scan selesai — {total} file ditemukan di music/")

        self.lib.scan(on_done=_done)

        while True:
            self._draw()
            key = self.scr.getch()

            # Tidak ada input — cek auto-next & reload scan
            if key == -1:
                if self.player.finished() and self.queue:
                    self._next()
                if self.lib.scanning:
                    self._reload()
                continue

            # Tampilkan error player ke notif bar
            if self.player.error:
                self.notify(f"⚠ {self.player.error}", 4)
                self.player.error = ""

            # ── Mode mengetik pencarian ────────────────────────────────
            if self.typing:
                if key == 27:                              # ESC — batal mengetik
                    self.typing = False
                    curses.curs_set(0)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.query = self.query[:-1]
                    self._do_search()
                elif 32 <= key <= 126:
                    self.query += chr(key)
                    self._do_search()
                elif key == curses.KEY_DOWN: self._move(+1)
                elif key == curses.KEY_UP:   self._move(-1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    self._play_sel()
                continue

            # ── Mode normal ────────────────────────────────────────────
            if   key == ord("q"):                           break
            elif key == ord("1"):
                self.tab = self.TAB_ALL
                self.sel = 0; self.scroll = 0
            elif key == ord("2"):
                self.tab = self.TAB_SEARCH
                self.sel = 0; self.scroll = 0
            elif key == ord("/"):
                self.tab    = self.TAB_SEARCH
                self.typing = True
                curses.curs_set(1)
            elif key == curses.KEY_DOWN:                    self._move(+1)
            elif key == curses.KEY_UP:                      self._move(-1)
            elif key in (curses.KEY_ENTER, 10, 13):         self._play_sel()
            elif key == ord(" "):                           self.player.toggle_pause()
            elif key == ord("n"):                           self._next()
            elif key == ord("p"):                           self._prev()
            elif key == ord("+"):                           self.player.vol_up()
            elif key == ord("-"):                           self.player.vol_down()
            elif key == ord("r"):
                self._reload()
                self.lib.scan(on_done=_done)
                self.notify("Scan ulang folder music/ ...")

    # ── Helpers navigasi ──────────────────
    def _do_search(self):
        self.results = self.lib.search(self.query)
        self.sel = 0; self.scroll = 0

    def _move(self, d: int):
        lst = self._list
        if not lst:
            return
        h, _ = self.scr.getmaxyx()
        extra = 1 if self.tab == self.TAB_ALL else 3
        area  = self._area(h, extra)

        self.sel = (self.sel + d) % len(lst)

        if self.sel < self.scroll:
            self.scroll = self.sel
        elif self.sel >= self.scroll + area:
            self.scroll = self.sel - area + 1
        self.scroll = max(0, self.scroll)

    def _play_sel(self):
        lst = self._list
        if not lst or self.sel >= len(lst):
            return
        self.queue = list(lst)
        self.q_idx = self.sel
        self.player.play(lst[self.sel]["path"])

    def _next(self):
        if not self.queue:
            return
        self.q_idx = (self.q_idx + 1) % len(self.queue)
        self.player.play(self.queue[self.q_idx]["path"])

    def _prev(self):
        if not self.queue:
            return
        self.q_idx = (self.q_idx - 1) % len(self.queue)
        self.player.play(self.queue[self.q_idx]["path"])

    # ══════════════════════════════════════
    #  GAMBAR LAYAR
    # ══════════════════════════════════════
    def _draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()

        if h < 8 or w < 30:
            self._put(0, 0, "Terminal terlalu kecil — perbesar window!")
            self.scr.refresh()
            return

        # ── Atas ──────────────────────────────────────────
        self._draw_tabbar(w)
        self._line(1, w)

        # ── Konten tab ────────────────────────────────────
        if self.tab == self.TAB_ALL:
            self._draw_all(h, w)
        else:
            self._draw_search(h, w)

        # ── Bawah (selalu digambar terakhir agar tidak tertimpa) ──
        sep = h - _BOT
        self._line(sep, w)
        self._draw_now_playing(sep + 1, w)
        self._draw_status(sep + 2, w)
        self._draw_help(sep + 3, w)

        self.scr.refresh()

    # ── Tab bar ───────────────────────────
    def _draw_tabbar(self, w):
        tabs = [
            (self.TAB_ALL,    f" [1] Semua ({self.lib.count()}) "),
            (self.TAB_SEARCH, " [2] Cari "),
        ]
        x = 1
        for tid, label in tabs:
            attr = (curses.color_pair(6) | curses.A_BOLD
                    if self.tab == tid else curses.color_pair(1))
            self._put(0, x, label, attr)
            x += len(label) + 1

        hint = f" music/ → {MUSIC_DIR.name} "
        self._put(0, max(x + 2, w - len(hint) - 1), hint, curses.A_DIM)

    # ── Tab: Semua ────────────────────────
    def _draw_all(self, h, w):
        area     = self._area(h, extra_top=1)   # 1 = baris header kolom
        safe_bot = h - _BOT - 1                 # baris terakhir yang boleh dipakai
        songs    = self.all_songs

        if not songs:
            if self.lib.scanning:
                self._put(3, 2, "Sedang scan folder music/ ...", 4)
            else:
                self._put(3, 2, f"Folder music/ kosong.", 5)
                self._put(4, 2, f"Tambahkan file mp3/flac/ogg ke: {MUSIC_DIR}", 4)
            return

        # Header kolom
        hdr = f"  {'#':>4}  {'Judul':<30}  {'Artis':<22}  Dur"
        self._put(2, 0, hdr[:w - 1], 1)

        for i in range(area):
            y   = 3 + i
            idx = i + self.scroll
            if y > safe_bot or idx >= len(songs):
                break
            self._draw_row(y, songs[idx], idx, w)

    # ── Tab: Cari ─────────────────────────
    def _draw_search(self, h, w):
        cur    = "█" if self.typing else " "
        prompt = f" Cari: {self.query}{cur}"
        self._put(2, 0, prompt[:w - 1], curses.color_pair(4) | curses.A_BOLD)
        self._line_dim(3, min(len(prompt) + 2, w - 1))

        area     = self._area(h, extra_top=3)   # prompt + garis + label hasil
        safe_bot = h - _BOT - 1
        songs    = self.results

        if not songs:
            msg = (f"Tidak ada hasil untuk '{self.query}'"
                   if self.query else "Tekan  /  lalu ketik untuk mencari.")
            self._put(5, 2, msg, 5 if self.query else 4)
            return

        self._put(4, 0, f"  {len(songs)} hasil ditemukan", 1)

        for i in range(area):
            y   = 5 + i
            idx = i + self.scroll
            if y > safe_bot or idx >= len(songs):
                break
            self._draw_row(y, songs[idx], idx, w)

    # ── Satu baris lagu ───────────────────
    def _draw_row(self, y, song, idx, w):
        is_sel  = (idx == self.sel)
        is_play = (
            self.player.playing
            and bool(self.queue)
            and 0 <= self.q_idx < len(self.queue)
            and self.queue[self.q_idx]["path"] == song["path"]
        )

        marker = "▶" if is_play else " "
        title  = (song["title"]  or Path(song["path"]).stem)[:30]
        artist = (song["artist"] or "—")[:22]
        dur    = self._fmt(song["duration"])

        row = f" {marker}{idx+1:>4}  {title:<30}  {artist:<22}  {dur}"

        # Pad ke lebar penuh — karakter lama ikut tertimpa
        row = row[:w - 1].ljust(w - 1)

        if is_sel:    attr = curses.color_pair(3)
        elif is_play: attr = curses.color_pair(2) | curses.A_BOLD
        else:         attr = curses.A_NORMAL

        self._put(y, 0, row, attr)

    # ── Now Playing bar ───────────────────
    def _draw_now_playing(self, y, w):
        if not self.player.playing or not self.queue:
            self._put(y, 0, " ■  Tidak ada lagu".ljust(w - 1), curses.A_DIM)
            return

        song  = self.queue[self.q_idx]
        state = "⏸" if self.player.paused else "▶"
        title = (song["title"] or Path(song["path"]).stem)[:28]
        artist= (song["artist"] or "")[:18]
        pos   = self._fmt(self.player.position())
        tot   = self._fmt(song["duration"])
        pbar  = self._pbar(song["duration"], 16)

        # Bagian kanan: waktu + progress (selalu tampil)
        right = f"  {pos}/{tot} {pbar} "
        # Bagian kiri: status + judul + artis
        left  = f" {state}  {title}"
        if artist:
            left += f"  —  {artist}"

        # Gabungkan tanpa overflow
        max_left = max(0, w - len(right) - 1)
        line     = left[:max_left].ljust(max_left) + right
        line     = line[:w - 1].ljust(w - 1)

        self._put(y, 0, line, curses.color_pair(2) | curses.A_BOLD)

    # ── Status bar ────────────────────────
    def _draw_status(self, y, w):
        vol     = int(self.player.volume * 100)
        vol_bar = "█" * (vol // 10) + "░" * (10 - vol // 10)

        # Bagian kanan: volume (selalu tampil)
        right = f"  Vol [{vol_bar}] {vol}% "

        # Bagian kiri: notif / scan progress / jumlah lagu
        if self.lib.scanning:
            done = self.lib.scan_done
            tot  = max(1, self.lib.scan_total)
            pct  = int(done / tot * 100)
            bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
            left = f" Scan [{bar}] {pct}%"
        elif self._msg and time.time() < self._msg_t:
            left = f" {self._msg}"
        else:
            warn = ""
            if not PYGAME_OK:  warn += " ⚠pygame"
            if not MUTAGEN_OK: warn += " ⚠mutagen"
            if not VLC_OK:     warn += " ⚠vlc"
            eng  = self.player.engine_label
            left = f" {self.lib.count()} lagu di music/  [{eng}]{warn}"

        max_left = max(0, w - len(right) - 1)
        line     = left[:max_left].ljust(max_left) + right
        self._put(y, 0, line[:w - 1].ljust(w - 1), 4)

    # ── Help bar ──────────────────────────
    def _draw_help(self, y, w):
        txt = " 1/2:Tab  /:Cari  ENTER:Putar  SPC:Pause  n/p:Skip  +/-:Vol  r:Refresh  q:Keluar"
        # Pad penuh — hindari karakter sisa di baris paling bawah
        self._put(y, 0, txt[:w - 1].ljust(w - 1), curses.A_DIM)

    # ══════════════════════════════════════
    #  UTILITAS GAMBAR
    # ══════════════════════════════════════
    def _put(self, y, x, text, color=curses.A_NORMAL):
        """Tulis teks ke layar dengan aman. Bersihkan sisa baris sesudahnya."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            attr = curses.color_pair(color) if isinstance(color, int) and color > 0 else color
            self.scr.addstr(y, x, str(text), attr)
            # Bersihkan sisa karakter di baris ini (kecuali baris paling bawah)
            if y < h - 1:
                self.scr.clrtoeol()
        except curses.error:
            pass

    def _line(self, y, w):
        """Gambar garis pemisah horizontal."""
        self._put(y, 0, "─" * (w - 1), 1)

    def _line_dim(self, y, length):
        """Gambar garis pemisah pendek (untuk bawah kotak cari)."""
        self._put(y, 0, "─" * length, 4)

    def _pbar(self, total: float, width: int) -> str:
        """Progress bar sederhana."""
        if total <= 0 or not self.player.playing:
            return "░" * width
        pct    = min(1.0, self.player.position() / total)
        filled = int(pct * width)
        return "█" * filled + "░" * (width - filled)

    @staticmethod
    def _fmt(s: float) -> str:
        """Format detik → mm:ss."""
        s = max(0, int(s))
        return f"{s // 60:02d}:{s % 60:02d}"


# ══════════════════════════════════════════
#  TITIK MASUK
# ══════════════════════════════════════════
def main(stdscr):
    setup_music_folder()
    lib    = Library()
    player = Player()
    try:
        TUI(stdscr, lib, player).run()
    finally:
        player.stop()
        player.cleanup()
        lib.close()
        # Kembalikan fd 2 ke stderr asli terminal
        os.dup2(sys.__stderr__.fileno(), 2)
        sys.stderr = sys.__stderr__


if __name__ == "__main__":
    curses.wrapper(main)