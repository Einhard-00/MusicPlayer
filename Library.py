#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║     FASE 1 — Library & Pencarian Lagu   ║
║     (versi fixed — semua bug diperbaiki) ║
╚══════════════════════════════════════════╝

FIX LOG:
  [FIX-1] stderr diarahkan ke file log agar tidak merusak tampilan curses
          (bug: pesan mutagen "comment():584 error" muncul di layar)
  [FIX-2] Perhitungan area list diperbaiki dengan safe_list_area()
          (bug: status bar / volume hilang di terminal kecil atau scroll bawah)
  [FIX-3] play() sekarang menampilkan pesan error yang jelas ke status bar
          (bug: lagu tidak bisa diputar tanpa pesan apapun)
  [FIX-4] Fallback pygame.mixer.Sound untuk format yang gagal di music loader
  [FIX-5] Validasi file sebelum diputar (exists, readable, ukuran > 0)

Cara jalankan:
    python3 library.py
    python3 library.py /path/ke/folder/musik

Log error tersimpan di:
    ~/.config/tui-player/error.log
"""

# ─────────────────────────────────────────────────────────────
#  [FIX-1] Arahkan stderr ke file log SEBELUM import apapun
#  Ini mencegah pesan error mutagen / library lain merusak
#  tampilan curses di terminal
# ─────────────────────────────────────────────────────────────
import sys
import os

_LOG_DIR  = os.path.join(os.path.expanduser("~"), ".config", "tui-player")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_FILE = open(os.path.join(_LOG_DIR, "error.log"), "a", buffering=1)
sys.stderr = _LOG_FILE

# ─────────────────────────────────────────
#  Import standar
# ─────────────────────────────────────────
import curses
import sqlite3
import time
import threading
import warnings
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────
#  Import library audio & metadata
# ─────────────────────────────────────────
try:
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        from mutagen import File as MutagenFile
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False

# Format audio yang didukung
SUPPORTED_FORMATS = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac"}

# Konstanta layout — berapa baris yang selalu ada di atas/bawah
TOP_ROWS    = 2   # tab_bar + separator
BOTTOM_ROWS = 3   # separator + status_bar + help_bar


# ══════════════════════════════════════════
#  KELAS: MusicLibrary
# ══════════════════════════════════════════
class MusicLibrary:
    """Database SQLite untuk koleksi lagu."""

    DB_DIR  = Path.home() / ".config" / "tui-player"
    DB_PATH = DB_DIR / "library.db"

    def __init__(self):
        self.DB_DIR.mkdir(parents=True, exist_ok=True)
        self.conn     = sqlite3.connect(str(self.DB_PATH), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._db_lock = threading.Lock()
        self._create_tables()

        self.scan_total    = 0
        self.scan_progress = 0
        self.scan_running  = False

    def _create_tables(self):
        with self._db_lock:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS songs (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    path       TEXT    UNIQUE NOT NULL,
                    title      TEXT    NOT NULL DEFAULT 'Unknown Title',
                    artist     TEXT    NOT NULL DEFAULT 'Unknown Artist',
                    album      TEXT    NOT NULL DEFAULT 'Unknown Album',
                    duration   REAL    NOT NULL DEFAULT 0.0,
                    file_size  INTEGER NOT NULL DEFAULT 0,
                    added_at   TEXT    NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_title  ON songs(title);
                CREATE INDEX IF NOT EXISTS idx_artist ON songs(artist);
                CREATE INDEX IF NOT EXISTS idx_album  ON songs(album);
            """)
            self.conn.commit()

    # ── Scan folder ───────────────────────
    def scan_folder(self, folder: str, callback=None):
        path = Path(folder).resolve()
        if not path.exists():
            return
        t = threading.Thread(
            target=self._scan_worker,
            args=(str(path), callback),
            daemon=True
        )
        t.start()
        return t

    def _scan_worker(self, folder: str, callback=None):
        self.scan_running  = True
        self.scan_progress = 0

        all_files = [
            str(f) for f in Path(folder).rglob("*")
            if f.suffix.lower() in SUPPORTED_FORMATS
        ]
        self.scan_total = len(all_files)

        with self._db_lock:
            existing = {
                row[0] for row in
                self.conn.execute("SELECT path FROM songs").fetchall()
            }

        new_songs = []
        for filepath in all_files:
            self.scan_progress += 1
            if filepath in existing:
                if callback:
                    callback(self.scan_progress, self.scan_total,
                             filepath, skipped=True)
                continue

            meta = self._read_metadata(filepath)
            new_songs.append(meta)
            if callback:
                callback(self.scan_progress, self.scan_total,
                         filepath, skipped=False)

            if len(new_songs) >= 50:
                self._bulk_insert(new_songs)
                new_songs = []

        if new_songs:
            self._bulk_insert(new_songs)

        self.scan_running = False

    def _bulk_insert(self, songs: list):
        now = datetime.now().isoformat()
        with self._db_lock:
            self.conn.executemany(
                """INSERT OR IGNORE INTO songs
                   (path,title,artist,album,duration,file_size,added_at)
                   VALUES (:path,:title,:artist,:album,:duration,:file_size,:added_at)""",
                [{**s, "added_at": now} for s in songs]
            )
            self.conn.commit()

    def _read_metadata(self, filepath: str) -> dict:
        """Baca metadata dengan aman — semua exception ditangkap."""
        title    = Path(filepath).stem
        artist   = "Unknown Artist"
        album    = "Unknown Album"
        duration = 0.0
        size     = 0

        try:
            size = os.path.getsize(filepath)
        except OSError:
            pass

        if MUTAGEN_OK:
            try:
                # [FIX-1] suppress warnings mutagen agar tidak bocor ke stderr
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    audio = MutagenFile(filepath)

                if audio is not None:
                    for key in ("title", "TIT2", "\xa9nam", "TITLE"):
                        if key in audio:
                            v = audio[key]
                            t = str(v[0] if isinstance(v, list) else v).strip()
                            if t:
                                title = t
                            break
                    for key in ("artist", "TPE1", "\xa9ART", "ARTIST"):
                        if key in audio:
                            v = audio[key]
                            a = str(v[0] if isinstance(v, list) else v).strip()
                            if a:
                                artist = a
                            break
                    for key in ("album", "TALB", "\xa9alb", "ALBUM"):
                        if key in audio:
                            v = audio[key]
                            al = str(v[0] if isinstance(v, list) else v).strip()
                            if al:
                                album = al
                            break
                    if hasattr(audio, "info") and hasattr(audio.info, "length"):
                        duration = float(audio.info.length)
            except Exception as e:
                print(f"[metadata error] {filepath}: {e}", file=_LOG_FILE)

        return {
            "path"     : filepath,
            "title"    : (title  or Path(filepath).stem)[:200],
            "artist"   : (artist or "Unknown Artist")[:200],
            "album"    : (album  or "Unknown Album")[:200],
            "duration" : duration,
            "file_size": size,
        }

    # ── Query database ────────────────────
    def search(self, query: str, limit: int = 300) -> list:
        if not query.strip():
            return self.get_all(limit)
        p = f"%{query.strip()}%"
        with self._db_lock:
            rows = self.conn.execute(
                """SELECT id,path,title,artist,album,duration FROM songs
                   WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
                   ORDER BY artist,album,title LIMIT ?""",
                (p, p, p, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_all(self, limit: int = 500) -> list:
        with self._db_lock:
            rows = self.conn.execute(
                """SELECT id,path,title,artist,album,duration FROM songs
                   ORDER BY artist,album,title LIMIT ?""",
                (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def total_songs(self) -> int:
        with self._db_lock:
            return self.conn.execute(
                "SELECT COUNT(*) FROM songs"
            ).fetchone()[0]

    def close(self):
        with self._db_lock:
            self.conn.close()


# ══════════════════════════════════════════
#  KELAS: AudioEngine
# ══════════════════════════════════════════
class AudioEngine:
    def __init__(self):
        self.is_playing   = False
        self.is_paused    = False
        self.current_file = None
        self.volume       = 0.7
        self.last_error   = ""    # [FIX-3]

        if PYGAME_OK:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
            pygame.mixer.music.set_volume(self.volume)

    def play(self, filepath: str) -> bool:
        """
        [FIX-3][FIX-4][FIX-5] Putar file audio dengan validasi lengkap.
        Mengembalikan True jika berhasil, False jika gagal.
        Pesan error tersimpan di self.last_error.
        """
        self.last_error = ""
        path = Path(filepath)

        # [FIX-5] Validasi file
        if not path.exists():
            self.last_error = f"File tidak ada: {path.name[:40]}"
            return False
        if path.stat().st_size == 0:
            self.last_error = f"File kosong: {path.name[:40]}"
            return False
        if path.suffix.lower() not in SUPPORTED_FORMATS:
            self.last_error = f"Format tidak didukung: {path.suffix}"
            return False
        if not PYGAME_OK:
            self.last_error = "pygame belum terinstall"
            return False

        # Coba dengan music loader (streaming, hemat RAM)
        try:
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
            self.current_file = str(path)
            self.is_playing   = True
            self.is_paused    = False
            return True
        except Exception as e:
            print(f"[play/music] {path}: {e}", file=_LOG_FILE)

        # [FIX-4] Fallback ke Sound loader (cocok untuk WAV tertentu)
        try:
            sound = pygame.mixer.Sound(str(path))
            sound.set_volume(self.volume)
            pygame.mixer.stop()
            sound.play()
            self.current_file = str(path)
            self.is_playing   = True
            self.is_paused    = False
            return True
        except Exception as e2:
            self.last_error = f"Tidak bisa diputar: {path.name[:35]}"
            print(f"[play/sound] {path}: {e2}", file=_LOG_FILE)
            self.is_playing = False
            return False

    def toggle_pause(self):
        if not PYGAME_OK or not self.is_playing:
            return
        if self.is_paused:
            pygame.mixer.music.unpause()
            self.is_paused = False
        else:
            pygame.mixer.music.pause()
            self.is_paused = True

    def stop(self):
        if PYGAME_OK:
            pygame.mixer.music.stop()
        self.is_playing = False
        self.is_paused  = False

    def is_song_finished(self) -> bool:
        if not PYGAME_OK or not self.is_playing or self.is_paused:
            return False
        return not pygame.mixer.music.get_busy()

    def volume_up(self):
        self.volume = min(1.0, self.volume + 0.05)
        if PYGAME_OK:
            pygame.mixer.music.set_volume(self.volume)

    def volume_down(self):
        self.volume = max(0.0, self.volume - 0.05)
        if PYGAME_OK:
            pygame.mixer.music.set_volume(self.volume)

    def get_position(self) -> float:
        if not PYGAME_OK or not self.is_playing:
            return 0.0
        pos = pygame.mixer.music.get_pos()
        return pos / 1000.0 if pos >= 0 else 0.0

    def cleanup(self):
        if PYGAME_OK:
            pygame.mixer.quit()


# ══════════════════════════════════════════
#  KELAS: LibraryTUI
# ══════════════════════════════════════════
class LibraryTUI:
    REFRESH_MS  = 300
    TAB_LIBRARY = 0
    TAB_SEARCH  = 1
    TAB_PLAYING = 2

    def __init__(self, stdscr, library: MusicLibrary,
                 engine: AudioEngine, music_folder: str):
        self.screen       = stdscr
        self.library      = library
        self.engine       = engine
        self.music_folder = music_folder
        self.current_tab  = self.TAB_LIBRARY

        self.lib_tracks  = []
        self.lib_sel     = 0
        self.lib_scroll  = 0

        self.search_query  = ""
        self.search_tracks = []
        self.search_sel    = 0
        self.search_scroll = 0
        self.search_mode   = False

        self.active_tracks = []
        self.active_idx    = 0

        self._status_msg   = ""
        self._status_timer = 0.0

        self._setup_colors()
        curses.curs_set(0)
        self.screen.timeout(self.REFRESH_MS)

        self._reload_library()
        self._start_scan()

    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN,   -1)
        curses.init_pair(2, curses.COLOR_GREEN,  -1)
        curses.init_pair(3, curses.COLOR_BLACK,  curses.COLOR_WHITE)
        curses.init_pair(4, curses.COLOR_YELLOW, -1)
        curses.init_pair(5, curses.COLOR_RED,    -1)
        curses.init_pair(6, curses.COLOR_WHITE,  curses.COLOR_BLUE)

    def _reload_library(self):
        self.lib_tracks    = self.library.get_all()
        self.search_tracks = self.library.search(self.search_query)

    def _start_scan(self):
        def on_progress(progress, total, filepath, skipped=False):
            if not skipped:
                pct  = int(progress / total * 100) if total else 0
                name = Path(filepath).name[:25]
                self._status_msg   = f"Scan {pct}% — {name}"
                self._status_timer = time.time() + 1.0
            if progress % 50 == 0:
                self._reload_library()

        def after_scan():
            time.sleep(0.3)
            self._reload_library()
            n = self.library.total_songs()
            self._status_msg   = f"Scan selesai — {n} lagu ditemukan"
            self._status_timer = time.time() + 4.0

        self.library.scan_folder(self.music_folder, callback=on_progress)
        threading.Thread(target=after_scan, daemon=True).start()

    # ── [FIX-2] Hitung area list yang aman ──
    def _safe_area(self, h: int, extra_top: int = 0) -> int:
        """
        Hitung jumlah baris yang boleh dipakai untuk list lagu.
        Selalu menyisakan ruang untuk baris bawah (separator+status+help).
        extra_top = baris tambahan di atas list (misal: header kolom, prompt search)
        """
        used = TOP_ROWS + extra_top + 1 + BOTTOM_ROWS  # +1 separator bawah
        return max(1, h - used)

    # ══════════════════════════════════════
    #  LOOP UTAMA
    # ══════════════════════════════════════
    def run(self):
        while True:
            self._draw()
            key = self.screen.getch()

            # Tidak ada input — cek auto-next
            if key == -1:
                if self.engine.is_song_finished() and self.active_tracks:
                    self._next_song()
                continue

            # ── Mode mengetik (search) ─────────────────────────────────
            if self.search_mode:
                if key == 27:
                    self.search_mode = False
                    curses.curs_set(0)
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    self.search_query = self.search_query[:-1]
                    self._do_search()
                elif 32 <= key <= 126:
                    self.search_query += chr(key)
                    self._do_search()
                elif key == curses.KEY_DOWN:
                    self._move(+1)
                elif key == curses.KEY_UP:
                    self._move(-1)
                elif key in (curses.KEY_ENTER, 10, 13):
                    self._play_selected()
                continue

            # ── Mode normal ────────────────────────────────────────────
            if   key == ord("q"):               break
            elif key == ord("1"):               self.current_tab = self.TAB_LIBRARY
            elif key == ord("2"):               self.current_tab = self.TAB_SEARCH
            elif key == ord("3"):               self.current_tab = self.TAB_PLAYING
            elif key == ord("/"):
                self.current_tab = self.TAB_SEARCH
                self.search_mode = True
                curses.curs_set(1)
            elif key == curses.KEY_DOWN:        self._move(+1)
            elif key == curses.KEY_UP:          self._move(-1)
            elif key in (curses.KEY_ENTER,10,13): self._play_selected()
            elif key == ord(" "):               self.engine.toggle_pause()
            elif key == ord("n"):               self._next_song()
            elif key == ord("p"):               self._prev_song()
            elif key == ord("+"):               self.engine.volume_up()
            elif key == ord("-"):               self.engine.volume_down()
            elif key == ord("r"):               self._start_scan()

            # [FIX-3] Tampilkan error audio ke status bar
            if self.engine.last_error:
                self._status_msg   = f"ERR {self.engine.last_error}"
                self._status_timer = time.time() + 4.0
                self.engine.last_error = ""

    # ── Helpers navigasi ──────────────────
    def _do_search(self):
        self.search_tracks = self.library.search(self.search_query)
        self.search_sel    = 0
        self.search_scroll = 0

    def _move(self, d: int):
        if self.current_tab == self.TAB_LIBRARY:
            tracks, sel, scroll = self.lib_tracks, self.lib_sel, self.lib_scroll
        else:
            tracks, sel, scroll = (self.search_tracks,
                                   self.search_sel, self.search_scroll)
        if not tracks:
            return

        sel = (sel + d) % len(tracks)
        h, _ = self.screen.getmaxyx()

        extra = 1 if self.current_tab == self.TAB_LIBRARY else 3
        area  = self._safe_area(h, extra_top=extra)

        if sel < scroll:
            scroll = sel
        elif sel >= scroll + area:
            scroll = sel - area + 1
        scroll = max(0, scroll)

        if self.current_tab == self.TAB_LIBRARY:
            self.lib_sel, self.lib_scroll = sel, scroll
        else:
            self.search_sel, self.search_scroll = sel, scroll

    def _play_selected(self):
        if self.current_tab == self.TAB_LIBRARY:
            tracks, sel = self.lib_tracks, self.lib_sel
        else:
            tracks, sel = self.search_tracks, self.search_sel

        if not tracks or sel >= len(tracks):
            return

        self.active_tracks = list(tracks)
        self.active_idx    = sel
        ok = self.engine.play(tracks[sel]["path"])
        if ok:
            self.current_tab = self.TAB_PLAYING

    def _next_song(self):
        if not self.active_tracks:
            return
        self.active_idx = (self.active_idx + 1) % len(self.active_tracks)
        self.engine.play(self.active_tracks[self.active_idx]["path"])

    def _prev_song(self):
        if not self.active_tracks:
            return
        self.active_idx = (self.active_idx - 1) % len(self.active_tracks)
        self.engine.play(self.active_tracks[self.active_idx]["path"])

    # ══════════════════════════════════════
    #  GAMBAR LAYAR
    # ══════════════════════════════════════
    def _draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()

        if h < 8 or w < 30:
            self._safe_addstr(0, 0, "Terminal terlalu kecil! Perbesar window.")
            self.screen.refresh()
            return

        # ── Atas ──────────────────────────
        self._draw_tab_bar(w)
        self._safe_addstr(1, 0, "─" * (w - 1), curses.color_pair(1))

        # ── Konten tab ────────────────────
        if   self.current_tab == self.TAB_LIBRARY: self._draw_library(h, w)
        elif self.current_tab == self.TAB_SEARCH:  self._draw_search(h, w)
        elif self.current_tab == self.TAB_PLAYING: self._draw_now_playing(h, w)

        # ── [FIX-2] Bawah — selalu digambar terakhir agar tidak tertimpa ──
        self._safe_addstr(h - BOTTOM_ROWS - 1, 0,
                          "─" * (w - 1), curses.color_pair(1))
        self._draw_status_bar(h, w)
        self._draw_help_bar(h, w)

        self.screen.refresh()

    def _draw_tab_bar(self, w):
        tabs = [
            (self.TAB_LIBRARY, f" [1] Library ({len(self.lib_tracks)}) "),
            (self.TAB_SEARCH,  " [2] Search "),
            (self.TAB_PLAYING, " [3] Now Playing "),
        ]
        x = 1
        for tid, label in tabs:
            attr = (curses.color_pair(6) | curses.A_BOLD
                    if self.current_tab == tid
                    else curses.color_pair(1))
            self._safe_addstr(0, x, label, attr)
            x += len(label) + 1

    # ── Tab Library ───────────────────────
    def _draw_library(self, h, w):
        area   = self._safe_area(h, extra_top=1)   # 1 = header kolom
        tracks = self.lib_tracks
        sel    = self.lib_sel
        scroll = self.lib_scroll

        if not tracks:
            self._safe_addstr(3, 2, "Library kosong — sedang scan...",
                              curses.color_pair(4))
            return

        hdr = f"  {'#':>4}  {'Artis':<18}  {'Judul':<26}  {'Album':<16}  Dur"
        self._safe_addstr(2, 0, hdr[:w - 1], curses.color_pair(1))

        # Batas bawah yang AMAN untuk menggambar baris list
        safe_bottom = h - BOTTOM_ROWS - 2

        for i in range(area):
            row_y = 3 + i
            tidx  = i + scroll
            if row_y > safe_bottom or tidx >= len(tracks):
                break
            self._draw_track_row(row_y, tracks[tidx], tidx, sel, w)

    # ── Tab Search ────────────────────────
    def _draw_search(self, h, w):
        cursor = "█" if self.search_mode else " "
        prompt = f" Cari: {self.search_query}{cursor}"
        self._safe_addstr(2, 0, prompt[:w - 1],
                          curses.color_pair(4) | curses.A_BOLD)
        self._safe_addstr(3, 0, "─" * min(len(prompt) + 2, w - 1),
                          curses.color_pair(4))

        tracks = self.search_tracks
        sel    = self.search_sel
        scroll = self.search_scroll
        area   = self._safe_area(h, extra_top=3)  # prompt + sep + label

        if not tracks and self.search_query:
            self._safe_addstr(5, 2,
                              f"Tidak ada hasil untuk '{self.search_query}'",
                              curses.color_pair(5))
            return
        if not tracks:
            self._safe_addstr(5, 2,
                              "Tekan  /  lalu ketik untuk mencari.",
                              curses.color_pair(4))
            return

        self._safe_addstr(4, 0, f"  {len(tracks)} hasil", curses.color_pair(1))

        safe_bottom = h - BOTTOM_ROWS - 2
        for i in range(area):
            row_y = 5 + i
            tidx  = i + scroll
            if row_y > safe_bottom or tidx >= len(tracks):
                break
            self._draw_track_row(row_y, tracks[tidx], tidx, sel, w)

    # ── Baris lagu ────────────────────────
    def _draw_track_row(self, y, track, idx, sel_idx, w):
        is_sel  = (idx == sel_idx)
        is_play = (
            self.engine.is_playing
            and self.active_tracks
            and 0 <= self.active_idx < len(self.active_tracks)
            and self.active_tracks[self.active_idx].get("path") == track["path"]
        )

        marker = "▶" if is_play else " "
        row = (
            f" {marker}{idx+1:>4}  "
            f"{track['artist'][:18]:<18}  "
            f"{track['title'][:26]:<26}  "
            f"{track['album'][:16]:<16}  "
            f"{self._fmt_dur(track['duration'])}"
        )
        row = row[:w - 1]

        if is_sel:
            attr = curses.color_pair(3)
        elif is_play:
            attr = curses.color_pair(2) | curses.A_BOLD
        else:
            attr = curses.A_NORMAL

        self._safe_addstr(y, 0, row.ljust(w - 1), attr)

    # ── Tab Now Playing ───────────────────
    def _draw_now_playing(self, h, w):
        content_top = 2
        content_bot = h - BOTTOM_ROWS - 2

        if not self.engine.is_playing or not self.active_tracks:
            mid = (content_top + content_bot) // 2
            msg = "Tidak ada lagu yang sedang diputar"
            self._safe_addstr(mid, max(0, w // 2 - len(msg) // 2),
                              msg, curses.color_pair(5))
            return

        track = self.active_tracks[self.active_idx]
        state = "PAUSED" if self.engine.is_paused else "NOW PLAYING"
        pos   = self._fmt_dur(self.engine.get_position())
        total = self._fmt_dur(track["duration"])
        pbar  = self._progress_bar(track["duration"], w)
        pinfo = f"Lagu {self.active_idx + 1} dari {len(self.active_tracks)}"

        lines = [
            (f"[ {state} ]",       curses.color_pair(2) | curses.A_BOLD),
            ("",                   curses.A_NORMAL),
            (track["title"],       curses.A_BOLD),
            (track["artist"],      curses.color_pair(1)),
            (track["album"],       curses.A_DIM),
            ("",                   curses.A_NORMAL),
            (f"{pos}  /  {total}", curses.color_pair(4)),
            (pbar,                 curses.color_pair(2)),
            ("",                   curses.A_NORMAL),
            (pinfo,                curses.A_DIM),
        ]

        mid_y = (content_top + content_bot) // 2
        start = max(content_top, mid_y - len(lines) // 2)
        for i, (text, attr) in enumerate(lines):
            y = start + i
            if y > content_bot:
                break
            x = max(0, w // 2 - len(text) // 2)
            self._safe_addstr(y, x, text[:w - 2], attr)

    # ── Status bar & help ─────────────────
    def _draw_status_bar(self, h, w):
        vol_pct = int(self.engine.volume * 100)
        vol_bar = "█" * (vol_pct // 10) + "░" * (10 - vol_pct // 10)
        total   = self.library.total_songs()

        if self.library.scan_running:
            prog = self.library.scan_progress
            maxt = max(1, self.library.scan_total)
            pct  = int(prog / maxt * 100)
            bar  = "█" * (pct // 5) + "░" * (20 - pct // 5)
            left = f" [{bar}] {pct}% ({prog}/{maxt})"
        elif self._status_msg and time.time() < self._status_timer:
            left = f" {self._status_msg}"
        else:
            left = f" {total} lagu"

        right = f"  Vol [{vol_bar}] {vol_pct}% "
        pad   = max(0, w - len(right) - 1)
        line  = left[:pad].ljust(pad) + right
        self._safe_addstr(h - 2, 0, line[:w - 1], curses.color_pair(4))

    def _draw_help_bar(self, h, w):
        txt = "  1-3:Tab  /:Cari  ENTER:Putar  SPACE:Pause  n/p:Skip  +/-:Vol  r:Scan  q:Keluar"
        self._safe_addstr(h - 1, 0, txt[:w - 1], curses.A_DIM)

    # ── Utilities ─────────────────────────
    def _progress_bar(self, total_sec: float, w: int) -> str:
        if total_sec <= 0 or not self.engine.is_playing:
            return ""
        pct    = min(1.0, self.engine.get_position() / total_sec)
        bar_w  = min(44, max(10, w - 8))
        filled = int(pct * bar_w)
        return "█" * filled + "░" * (bar_w - filled)

    @staticmethod
    def _fmt_dur(seconds: float) -> str:
        s = max(0, int(seconds))
        return f"{s // 60:02d}:{s % 60:02d}"

    def _safe_addstr(self, y, x, text, attr=curses.A_NORMAL):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass


# ══════════════════════════════════════════
#  TITIK MASUK
# ══════════════════════════════════════════
def main(stdscr):
    folder  = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Music")
    library = MusicLibrary()
    engine  = AudioEngine()
    try:
        tui = LibraryTUI(stdscr, library, engine, folder)
        tui.run()
    finally:
        engine.stop()
        engine.cleanup()
        library.close()
        sys.stderr = sys.__stderr__   # kembalikan stderr ke normal


if __name__ == "__main__":
    curses.wrapper(main)