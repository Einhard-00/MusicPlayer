#!/usr/bin/env python3
"""
╔══════════════════════════════════════════╗
║        TUI Music Player - Skeleton       ║
║   Dibuat untuk belajar step by step      ║
╚══════════════════════════════════════════╝

Cara install dependensi:
    pip install pygame mutagen

Cara jalankan:
    python3 music_player.py
    python3 music_player.py /path/ke/folder/musik

Kontrol keyboard:
    ↑ / ↓     → navigasi playlist
    ENTER      → putar lagu yang dipilih
    SPACE      → pause / resume
    n          → lagu berikutnya
    p          → lagu sebelumnya
    q          → keluar
    + / -      → volume naik / turun
"""

import curses
import os
import sys
import time
import threading
from pathlib import Path

# ─────────────────────────────────────────
#  Coba import library audio dan metadata
# ─────────────────────────────────────────
try:
    import pygame
    PYGAME_OK = True
except ImportError:
    PYGAME_OK = False

try:
    from mutagen import File as MutagenFile
    MUTAGEN_OK = True
except ImportError:
    MUTAGEN_OK = False


# ══════════════════════════════════════════
#  KELAS UTAMA: AudioEngine
#  Bertanggung jawab memutar audio
# ══════════════════════════════════════════
class AudioEngine:
    """Mesin audio — semua urusan putar/pause/stop ada di sini."""

    def __init__(self):
        self.is_playing   = False
        self.is_paused    = False
        self.current_file = None
        self.volume       = 0.7          # 0.0 s/d 1.0

        if PYGAME_OK:
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=512)
            pygame.mixer.music.set_volume(self.volume)

    # ── Putar file musik ──────────────────
    def play(self, filepath: str):
        if not PYGAME_OK:
            return
        try:
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.play()
            self.current_file = filepath
            self.is_playing   = True
            self.is_paused    = False
        except Exception as e:
            # TODO: tampilkan error ke UI
            pass

    # ── Pause / Resume ────────────────────
    def toggle_pause(self):
        if not PYGAME_OK or not self.is_playing:
            return
        if self.is_paused:
            pygame.mixer.music.unpause()
            self.is_paused = False
        else:
            pygame.mixer.music.pause()
            self.is_paused = True

    # ── Stop ──────────────────────────────
    def stop(self):
        if not PYGAME_OK:
            return
        pygame.mixer.music.stop()
        self.is_playing   = False
        self.is_paused    = False
        self.current_file = None

    # ── Cek apakah lagu sudah selesai ─────
    def is_song_finished(self) -> bool:
        if not PYGAME_OK or not self.is_playing or self.is_paused:
            return False
        return not pygame.mixer.music.get_busy()

    # ── Volume naik ───────────────────────
    def volume_up(self):
        self.volume = min(1.0, self.volume + 0.05)
        if PYGAME_OK:
            pygame.mixer.music.set_volume(self.volume)

    # ── Volume turun ──────────────────────
    def volume_down(self):
        self.volume = max(0.0, self.volume - 0.05)
        if PYGAME_OK:
            pygame.mixer.music.set_volume(self.volume)

    # ── Posisi saat ini (detik) ───────────
    def get_position(self) -> float:
        if not PYGAME_OK or not self.is_playing:
            return 0.0
        return pygame.mixer.music.get_pos() / 1000.0   # ms → detik

    def cleanup(self):
        if PYGAME_OK:
            pygame.mixer.quit()


# ══════════════════════════════════════════
#  KELAS: Playlist
#  Menyimpan dan mengelola daftar lagu
# ══════════════════════════════════════════
class Playlist:
    """Daftar lagu — navigasi, shuffle, repeat bisa ditambah di sini."""

    # Format audio yang didukung
    SUPPORTED = {".mp3", ".flac", ".ogg", ".wav", ".m4a", ".aac"}

    def __init__(self):
        self.tracks       = []    # list dari dict: {path, title, artist, duration}
        self.current_idx  = 0

    # ── Scan folder untuk file musik ──────
    def load_folder(self, folder: str):
        folder_path = Path(folder)
        if not folder_path.exists():
            return

        self.tracks = []
        # Jelajahi semua file dalam folder (termasuk subfolder)
        for filepath in sorted(folder_path.rglob("*")):
            if filepath.suffix.lower() in self.SUPPORTED:
                meta = self._read_metadata(str(filepath))
                self.tracks.append(meta)

        self.current_idx = 0

    # ── Baca metadata (judul, artis, durasi) ──
    def _read_metadata(self, filepath: str) -> dict:
        title    = Path(filepath).stem          # default: nama file
        artist   = "Unknown Artist"
        duration = 0.0

        if MUTAGEN_OK:
            try:
                audio = MutagenFile(filepath)
                if audio is not None:
                    # Coba baca tag title
                    for key in ("title", "TIT2", "\xa9nam"):
                        if key in audio:
                            val = audio[key]
                            title = str(val[0]) if isinstance(val, list) else str(val)
                            break
                    # Coba baca tag artist
                    for key in ("artist", "TPE1", "\xa9ART"):
                        if key in audio:
                            val = audio[key]
                            artist = str(val[0]) if isinstance(val, list) else str(val)
                            break
                    # Durasi
                    if hasattr(audio, "info") and hasattr(audio.info, "length"):
                        duration = audio.info.length
            except Exception:
                pass

        return {
            "path"    : filepath,
            "title"   : title,
            "artist"  : artist,
            "duration": duration,
        }

    # ── Navigasi ──────────────────────────
    def current_track(self):
        if not self.tracks:
            return None
        return self.tracks[self.current_idx]

    def next_track(self):
        if not self.tracks:
            return None
        self.current_idx = (self.current_idx + 1) % len(self.tracks)
        return self.current_track()

    def prev_track(self):
        if not self.tracks:
            return None
        self.current_idx = (self.current_idx - 1) % len(self.tracks)
        return self.current_track()

    def select(self, idx: int):
        if 0 <= idx < len(self.tracks):
            self.current_idx = idx
        return self.current_track()

    def __len__(self):
        return len(self.tracks)


# ══════════════════════════════════════════
#  KELAS: TUI (Terminal User Interface)
#  Semua urusan tampilan terminal ada di sini
# ══════════════════════════════════════════
class TUI:
    """Antarmuka terminal — digambar ulang setiap refresh."""

    REFRESH_MS = 500    # refresh setiap 500ms

    def __init__(self, stdscr, playlist: Playlist, engine: AudioEngine):
        self.screen   = stdscr
        self.playlist = playlist
        self.engine   = engine

        # Indeks baris yang sedang di-scroll di panel playlist
        self.scroll_offset = 0
        self.selected_idx  = 0

        self._setup_colors()
        curses.curs_set(0)                         # sembunyikan kursor
        self.screen.timeout(self.REFRESH_MS)       # non-blocking getch

    # ── Setup warna ───────────────────────
    def _setup_colors(self):
        curses.start_color()
        curses.use_default_colors()
        # (pair_id, foreground, background)
        curses.init_pair(1, curses.COLOR_CYAN,    -1)   # judul header
        curses.init_pair(2, curses.COLOR_GREEN,   -1)   # lagu aktif
        curses.init_pair(3, curses.COLOR_BLACK,   curses.COLOR_WHITE)  # selected
        curses.init_pair(4, curses.COLOR_YELLOW,  -1)   # status bar
        curses.init_pair(5, curses.COLOR_RED,     -1)   # warning

    # ── Loop utama ────────────────────────
    def run(self):
        while True:
            self._draw()
            key = self.screen.getch()

            # ── Keluar ────────────────────
            if key == ord("q"):
                break

            # ── Navigasi playlist ─────────
            elif key == curses.KEY_DOWN:
                self._move_selection(+1)

            elif key == curses.KEY_UP:
                self._move_selection(-1)

            # ── Pilih & putar lagu ────────
            elif key == curses.KEY_ENTER or key in (10, 13):
                track = self.playlist.select(self.selected_idx)
                if track:
                    self.engine.play(track["path"])

            # ── Pause / Resume ────────────
            elif key == ord(" "):
                self.engine.toggle_pause()

            # ── Lagu berikutnya ───────────
            elif key == ord("n"):
                track = self.playlist.next_track()
                self.selected_idx = self.playlist.current_idx
                if track:
                    self.engine.play(track["path"])

            # ── Lagu sebelumnya ───────────
            elif key == ord("p"):
                track = self.playlist.prev_track()
                self.selected_idx = self.playlist.current_idx
                if track:
                    self.engine.play(track["path"])

            # ── Volume ────────────────────
            elif key == ord("+"):
                self.engine.volume_up()

            elif key == ord("-"):
                self.engine.volume_down()

            # ── Auto-next jika lagu selesai ───
            if self.engine.is_song_finished():
                track = self.playlist.next_track()
                self.selected_idx = self.playlist.current_idx
                if track:
                    self.engine.play(track["path"])

    # ── Geser pilihan ─────────────────────
    def _move_selection(self, direction: int):
        total = len(self.playlist)
        if total == 0:
            return
        self.selected_idx = (self.selected_idx + direction) % total
        # Scroll agar item terpilih selalu terlihat
        h, _ = self.screen.getmaxyx()
        list_area = h - 6    # ruang untuk header + status bar
        if self.selected_idx < self.scroll_offset:
            self.scroll_offset = self.selected_idx
        elif self.selected_idx >= self.scroll_offset + list_area:
            self.scroll_offset = self.selected_idx - list_area + 1

    # ══════════════════════════════════════
    #  GAMBAR LAYAR
    # ══════════════════════════════════════
    def _draw(self):
        self.screen.erase()
        h, w = self.screen.getmaxyx()

        self._draw_header(w)
        self._draw_playlist(h, w)
        self._draw_now_playing(h, w)
        self._draw_status_bar(h, w)
        self._draw_help_bar(h, w)

        self.screen.refresh()

    # ── Header ────────────────────────────
    def _draw_header(self, w):
        title = "♪  TUI Music Player  ♪"
        self._safe_addstr(
            0, max(0, (w - len(title)) // 2),
            title,
            curses.color_pair(1) | curses.A_BOLD
        )
        self._safe_addstr(1, 0, "─" * w, curses.color_pair(1))

    # ── Daftar lagu ───────────────────────
    def _draw_playlist(self, h, w):
        list_area = h - 6
        tracks    = self.playlist.tracks

        if not tracks:
            msg = "Tidak ada lagu ditemukan. Tambahkan file musik ke folder."
            self._safe_addstr(3, 2, msg, curses.color_pair(5))
            return

        for i in range(list_area):
            track_idx = i + self.scroll_offset
            if track_idx >= len(tracks):
                break

            track   = tracks[track_idx]
            is_sel  = (track_idx == self.selected_idx)
            is_play = (track_idx == self.playlist.current_idx
                       and self.engine.is_playing)

            # Format baris: [►] No. Artis — Judul  (durasi)
            marker   = "►" if is_play else " "
            num      = f"{track_idx + 1:>3}."
            artist   = track["artist"][:15]
            title    = track["title"][:25]
            dur      = self._fmt_duration(track["duration"])
            row_text = f" {marker} {num} {artist:<15} — {title:<25}  {dur}"
            row_text = row_text[:w - 1]   # potong jika terlalu panjang

            attr = curses.A_NORMAL
            if is_sel:
                attr = curses.color_pair(3)
            elif is_play:
                attr = curses.color_pair(2) | curses.A_BOLD

            self._safe_addstr(2 + i, 0, row_text.ljust(w - 1), attr)

    # ── Info lagu yang sedang diputar ─────
    def _draw_now_playing(self, h, w):
        track = self.playlist.current_track()
        sep_y = h - 4
        self._safe_addstr(sep_y, 0, "─" * w, curses.color_pair(1))

        if track and self.engine.is_playing:
            state  = "⏸ PAUSED" if self.engine.is_paused else "▶ NOW PLAYING"
            info   = f"{state}  {track['artist']} — {track['title']}"
            pos    = self._fmt_duration(self.engine.get_position())
            total  = self._fmt_duration(track["duration"])
            timing = f"  [{pos} / {total}]"
            line   = (info + timing)[:w - 1]
            self._safe_addstr(sep_y + 1, 1, line,
                              curses.color_pair(2) | curses.A_BOLD)
        else:
            self._safe_addstr(sep_y + 1, 1, "■ STOPPED",
                              curses.color_pair(4))

    # ── Status bar (volume, jumlah lagu) ──
    def _draw_status_bar(self, h, w):
        vol_pct  = int(self.engine.volume * 100)
        vol_bar  = "█" * (vol_pct // 10) + "░" * (10 - vol_pct // 10)
        total    = len(self.playlist)
        status   = f" Vol [{vol_bar}] {vol_pct}%   Lagu: {total}"
        if not PYGAME_OK:
            status += "  ⚠ pygame tidak terinstall!"
        if not MUTAGEN_OK:
            status += "  ⚠ mutagen tidak terinstall!"
        self._safe_addstr(h - 2, 0, status[:w - 1], curses.color_pair(4))

    # ── Bar shortcut keyboard ─────────────
    def _draw_help_bar(self, h, w):
        keys = "  ENTER:Putar  SPACE:Pause  n:Next  p:Prev  +/-:Volume  q:Keluar"
        self._safe_addstr(h - 1, 0, keys[:w - 1], curses.A_DIM)

    # ── Format detik → mm:ss ──────────────
    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        return f"{s // 60:02d}:{s % 60:02d}"

    # ── Gambar teks dengan aman (hindari crash di tepi layar) ──
    def _safe_addstr(self, y, x, text, attr=curses.A_NORMAL):
        h, w = self.screen.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:
            self.screen.addstr(y, x, text, attr)
        except curses.error:
            pass


# ══════════════════════════════════════════
#  TITIK MASUK PROGRAM
# ══════════════════════════════════════════
def main(stdscr):
    # Ambil folder musik dari argumen, atau gunakan folder saat ini
    music_folder = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Music")

    playlist = Playlist()
    playlist.load_folder(music_folder)

    engine = AudioEngine()

    try:
        tui = TUI(stdscr, playlist, engine)
        tui.run()
    finally:
        engine.stop()
        engine.cleanup()


if __name__ == "__main__":
    curses.wrapper(main)