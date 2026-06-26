#!/usr/bin/env python3
"""
Macka Station Radio v3
- PostgreSQL cycle-based smart shuffle (every track plays once per cycle)
- /cover endpoint — streams embedded album art from ID3 tags
- /now returns track title + has_cover flag
- Dual bitrate: /stream (320kbps) / /stream-lo (128kbps)
- 4s acrossfade between tracks
- Zero-downtime deploys via SIGUSR1 → mindfulness interlude
"""
import os, time, threading, subprocess, json, hashlib, signal, sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

AUDIO_DIR       = os.getenv('AUDIO_DIR',       '/srv/macka/audio')
AUDIO_LO_DIR    = os.getenv('AUDIO_LO_DIR',    '/srv/macka/audio_lo')
MINDFULNESS     = os.getenv('MINDFULNESS',     '/srv/macka/sounds/mindfulness.mp3')
DATABASE_URL    = os.getenv('DATABASE_URL',    'postgresql://macka:mackapass@localhost/macka')
PORT            = int(os.getenv('PORT', 8765))
MINDFULNESS_SECS = int(os.getenv('MINDFULNESS_SECS', '30'))
CHUNK           = 32768
BURST_SECS      = 8
CROSSFADE       = 4.0
DEPLOY_TRIGGER  = '/tmp/macka_deploy'

import psycopg2
import psycopg2.pool

# ---------- database ---------------------------------------------------------

pool = psycopg2.pool.ThreadedConnectionPool(1, 10, DATABASE_URL)

def db():
    return pool.getconn()

def db_release(conn):
    pool.putconn(conn)

def init_db():
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tracks (
                    id           SERIAL PRIMARY KEY,
                    filename     TEXT UNIQUE NOT NULL,
                    title        TEXT,
                    artist       TEXT,
                    duration_sec FLOAT,
                    has_cover    BOOLEAN DEFAULT FALSE,
                    cycle_played INTEGER DEFAULT -1,
                    total_plays  INTEGER DEFAULT 0,
                    added_at     TIMESTAMPTZ DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS radio_state (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO radio_state (key, value) VALUES ('cycle', '0')
                    ON CONFLICT (key) DO NOTHING;
            """)
            conn.commit()
    finally:
        db_release(conn)

def sync_tracks():
    """Phase 1 (fast): insert filenames so shuffle can start immediately."""
    files = [f for f in os.listdir(AUDIO_DIR)
             if f.lower().endswith(('.mp3', '.flac', '.ogg', '.m4a'))]
    conn = db()
    try:
        with conn.cursor() as cur:
            for f in files:
                cur.execute(
                    "INSERT INTO tracks (filename) VALUES (%s) ON CONFLICT (filename) DO NOTHING",
                    (f,)
                )
            conn.commit()
    finally:
        db_release(conn)
    print(f'sync_tracks: {len(files)} files indexed', flush=True)

def enrich_tracks():
    """Phase 2 (slow, background): populate title/artist/has_cover via ffprobe."""
    while True:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, filename FROM tracks WHERE title IS NULL LIMIT 20")
                rows = cur.fetchall()
        finally:
            db_release(conn)
        if not rows:
            print('enrich_tracks: complete', flush=True)
            break
        for row_id, filename in rows:
            path = os.path.join(AUDIO_DIR, filename)
            title, artist = probe_tags(path)
            cover = bool(extract_cover(path))
            c = db()
            try:
                with c.cursor() as cur:
                    cur.execute(
                        "UPDATE tracks SET title=%s, artist=%s, has_cover=%s WHERE id=%s",
                        (title, artist, cover, row_id)
                    )
                    c.commit()
            finally:
                db_release(c)
        time.sleep(0.05)

def next_track_filename():
    """Cycle-based smart shuffle: all tracks play once before any repeats."""
    conn = db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM radio_state WHERE key='cycle'")
            cycle = int(cur.fetchone()[0])

            cur.execute(
                "SELECT filename FROM tracks WHERE cycle_played < %s ORDER BY RANDOM() LIMIT 1",
                (cycle,)
            )
            row = cur.fetchone()

            if not row:
                cycle += 1
                cur.execute("UPDATE radio_state SET value=%s WHERE key='cycle'", (str(cycle),))
                cur.execute("SELECT filename FROM tracks ORDER BY RANDOM() LIMIT 1")
                row = cur.fetchone()

            filename = row[0]
            cur.execute(
                "UPDATE tracks SET cycle_played=%s, total_plays=total_plays+1 WHERE filename=%s",
                (cycle, filename)
            )
            conn.commit()
            return filename
    finally:
        db_release(conn)

# ---------- audio helpers ----------------------------------------------------

def ffprobe_info(path):
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', path],
            stderr=subprocess.DEVNULL, timeout=5
        )
        fmt = json.loads(out)['format']
        return float(fmt['duration']), int(fmt['bit_rate'])
    except Exception:
        return None, 320_000

def probe_tags(path):
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json',
             '-show_entries', 'format_tags=title,artist', path],
            stderr=subprocess.DEVNULL, timeout=5
        )
        tags = json.loads(out).get('format', {}).get('tags', {})
        artist = tags.get('artist') or tags.get('ARTIST', '')
        title  = tags.get('title')  or tags.get('TITLE', '')
        return (title or os.path.splitext(os.path.basename(path))[0]), (artist or '')
    except Exception:
        return os.path.splitext(os.path.basename(path))[0], ''

_cover_cache: dict[str, bytes | None] = {}

def extract_cover(path: str) -> bytes | None:
    key = hashlib.md5(path.encode()).hexdigest()
    if key in _cover_cache:
        return _cover_cache[key]
    try:
        r = subprocess.run(
            ['ffmpeg', '-y', '-i', path, '-an', '-vcodec', 'copy', '-f', 'image2', 'pipe:1'],
            capture_output=True, timeout=5
        )
        data = r.stdout if len(r.stdout) > 512 else None
    except Exception:
        data = None
    _cover_cache[key] = data
    return data

def make_crossfade(path_a, path_b, bitrate_kbps=320, offset_a_sec=None):
    """
    4s crossfade between path_a (from offset_a_sec or near end) and start of path_b.
    offset_a_sec: if given, start from this point in path_a instead of -4s from EOF.
    """
    if offset_a_sec is not None:
        input_a = ['-ss', str(offset_a_sec), '-t', str(CROSSFADE), '-i', path_a]
    else:
        input_a = ['-sseof', f'-{CROSSFADE}', '-i', path_a]
    try:
        r = subprocess.run(
            ['ffmpeg', '-y'] + input_a +
            ['-t', str(CROSSFADE), '-i', path_b,
             '-filter_complex', f'[0][1]acrossfade=d={CROSSFADE}:c1=tri:c2=tri',
             '-b:a', f'{bitrate_kbps}k', '-f', 'mp3', 'pipe:1'],
            capture_output=True, timeout=30
        )
        return r.stdout if len(r.stdout) > 2000 else None
    except Exception:
        return None

def lo_path(hi_path):
    if hi_path == MINDFULNESS:
        return hi_path
    name = os.path.basename(hi_path)
    lo   = os.path.join(AUDIO_LO_DIR, name)
    return lo if os.path.exists(lo) else hi_path

# ---------- station ----------------------------------------------------------

class Station:
    """
    Shared broadcast state — all listeners hear the same track at the same position.
    """
    def __init__(self):
        self._lock              = threading.Lock()
        self._path              = None      # current hi-res track path
        self._dur               = None
        self._bitrate           = 320_000
        self._started           = time.monotonic()
        self.current            = '—'
        self.has_cover          = False
        self._mindfulness       = False
        self._mindfulness_until = 0.0

    # ── shared broadcast ──────────────────────────────────────────────────────

    def _switch(self, path: str):
        """Must be called with lock held."""
        dur, br = ffprobe_info(path)
        self._path     = path
        self._dur      = dur
        self._bitrate  = br
        self._started  = time.monotonic()
        # Update display info
        if path == MINDFULNESS:
            self.current   = '· · ·'
            self.has_cover = False
        else:
            title, artist = probe_tags(path)
            self.current   = f'{artist} – {title}' if artist and title else (title or os.path.splitext(os.path.basename(path))[0])
            self.has_cover = bool(extract_cover(path))
        print(f'NOW  {self.current}  [{br//1000}kbps]', flush=True)

    def snapshot(self):
        with self._lock:
            elapsed = time.monotonic() - self._started
            return dict(
                path     = self._path,
                bitrate  = self._bitrate,
                dur      = self._dur,
                offset   = max(0, int(elapsed * self._bitrate / 8) - CHUNK),
                elapsed  = elapsed,
            )

    def advance(self, from_path: str):
        """Move to next track if we're still on from_path."""
        with self._lock:
            if self._path != from_path:
                return
            if self._mindfulness and time.monotonic() < self._mindfulness_until:
                next_path = MINDFULNESS
            else:
                self._mindfulness = False
                fn = next_track_filename()
                next_path = os.path.join(AUDIO_DIR, fn)
            self._switch(next_path)

    def run(self):
        """Advance tracks based on timing; also drives /now updates."""
        # Pick first track
        with self._lock:
            if self._mindfulness and time.monotonic() < self._mindfulness_until:
                self._switch(MINDFULNESS)
            else:
                fn = next_track_filename()
                self._switch(os.path.join(AUDIO_DIR, fn))

        while True:
            time.sleep(1)
            with self._lock:
                # Tick mindfulness timeout
                if self._mindfulness and time.monotonic() >= self._mindfulness_until:
                    self._mindfulness = False

                if not self._path or not self._dur:
                    continue
                elapsed = time.monotonic() - self._started
                if elapsed >= self._dur - 0.5:
                    if self._mindfulness and time.monotonic() < self._mindfulness_until:
                        self._switch(MINDFULNESS)
                    else:
                        self._mindfulness = False
                        fn = next_track_filename()
                        self._switch(os.path.join(AUDIO_DIR, fn))

    # ── mindfulness ───────────────────────────────────────────────────────────

    def enter_mindfulness(self, secs=MINDFULNESS_SECS):
        with self._lock:
            self._mindfulness       = True
            self._mindfulness_until = time.monotonic() + secs
            # Force early crossfade by pretending the track ends soon
            if self._dur:
                elapsed = time.monotonic() - self._started
                self._dur = elapsed + CROSSFADE + 0.5
        print(f'→ mindfulness mode ({secs}s)', flush=True)

    def exit_mindfulness(self):
        with self._lock:
            self._mindfulness = False
        print('← exiting mindfulness early', flush=True)

    def is_mindfulness(self) -> bool:
        with self._lock:
            if not self._mindfulness:
                return False
            if time.monotonic() >= self._mindfulness_until:
                self._mindfulness = False
                return False
            return True

    def lo_serve_path(self, lo: bool) -> str:
        with self._lock:
            path = self._path
        if not path:
            return path
        if path == MINDFULNESS or not lo:
            return path
        return lo_path(path)

station = Station()

# ---------- POSIX signals ----------------------------------------------------

def _handle_sigusr1(sig, frame):
    """SIGUSR1: enter mindfulness (graceful deploy start)."""
    station.enter_mindfulness()

def _handle_sigusr2(sig, frame):
    """SIGUSR2: exit mindfulness early."""
    station.exit_mindfulness()

def _handle_sigterm(sig, frame):
    """SIGTERM: enter mindfulness briefly, then exit (gives listeners time to reconnect)."""
    station.enter_mindfulness(secs=8)
    time.sleep(10)
    sys.exit(0)

signal.signal(signal.SIGUSR1, _handle_sigusr1)
signal.signal(signal.SIGUSR2, _handle_sigusr2)
signal.signal(signal.SIGTERM, _handle_sigterm)

# ---------- HTTP handler -----------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass

    def do_HEAD(self):
        if self.path.split('?')[0] in ('/now', '/cover', '/stream', '/stream-lo'):
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]

        if path == '/now':
            is_m = station.is_mindfulness()
            body = json.dumps({
                'track':     '· · ·' if is_m else station.current,
                'has_cover': False if is_m else station.has_cover,
                'mindful':   is_m,
            }).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()
            self.wfile.write(body)

        elif path == '/cover':
            snap = station.snapshot()
            data = extract_cover(snap['path']) if snap['path'] and snap['path'] != MINDFULNESS else None
            if data:
                self.send_response(200)
                mime = 'image/png' if data[:4] == b'\x89PNG' else 'image/jpeg'
                self.send_header('Content-Type', mime)
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        elif path in ('/stream', '/stream-lo'):
            self._serve(lo=(path == '/stream-lo'))

        else:
            self.send_response(404)
            self.end_headers()

    def _serve(self, lo=False):
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('icy-name', 'Macka Station')
        self.end_headers()

        try:
            snap       = station.snapshot()
            hi_path    = snap['path']
            serve_path = station.lo_serve_path(lo)
            bitrate    = snap['bitrate']
            dur        = snap['dur']
            offset     = snap['offset']
            cf_cache: dict = {}

            while True:
                bytes_sec = (bitrate or 320_000) / 8
                burst_b   = int(BURST_SECS * bytes_sec)
                cf_byte   = int((dur - CROSSFADE) * bytes_sec) if dur else os.path.getsize(serve_path)
                sent      = 0
                deadline  = time.monotonic()
                cf_bytes  = None
                nxt_hi    = None
                emergency = False

                with open(serve_path, 'rb') as f:
                    f.seek(offset)
                    while True:
                        pos   = f.tell()
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break

                        # Emergency crossfade: mindfulness activated mid-track
                        if serve_path != MINDFULNESS and station.is_mindfulness() and cf_bytes is None:
                            station.advance(hi_path)   # advance to mindfulness
                            pos_sec   = max(0.0, (pos - offset) / bytes_sec)
                            cf_bytes  = make_crossfade(
                                serve_path, MINDFULNESS,
                                bitrate_kbps=128 if lo else 320,
                                offset_a_sec=pos_sec if dur and pos_sec < dur - CROSSFADE else None,
                            )
                            nxt_hi    = MINDFULNESS
                            emergency = True
                            chunk     = b''
                            break

                        # Crossfade at end of track: advance station first, then
                        # snapshot gives us the NEW track as crossfade target
                        if pos >= cf_byte and cf_bytes is None:
                            station.advance(hi_path)   # ← advance before snapshot
                            snap2   = station.snapshot()
                            nxt_hi  = snap2['path']
                            nxt_srv = station.lo_serve_path(lo)
                            key     = (serve_path, nxt_srv)
                            if key not in cf_cache:
                                cf_cache[key] = make_crossfade(
                                    serve_path, nxt_srv,
                                    bitrate_kbps=128 if lo else 320,
                                )
                            cf_bytes = cf_cache[key]
                            chunk    = chunk[:max(0, cf_byte - pos)]
                            if not chunk:
                                break

                        if chunk:
                            self.wfile.write(chunk)
                            sent += len(chunk)
                            if sent > burst_b:
                                deadline += len(chunk) / bytes_sec
                                lag = deadline - time.monotonic()
                                if lag > 0.005:
                                    time.sleep(lag)

                if cf_bytes:
                    self.wfile.write(cf_bytes)

                # Track already advanced above; just update local vars
                hi_path    = nxt_hi or station.snapshot()['path']
                serve_path = station.lo_serve_path(lo)
                dur, bitrate = ffprobe_info(serve_path)
                offset     = int(CROSSFADE * (bitrate / 8)) if cf_bytes and not emergency else 0

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    print('Macka Station v3  initialising…', flush=True)
    init_db()
    sync_tracks()
    threading.Thread(target=enrich_tracks, daemon=True).start()
    threading.Thread(target=station.run,   daemon=True).start()

    # Deploy trigger: start in mindfulness, fade to music after MINDFULNESS_SECS
    if os.path.exists(DEPLOY_TRIGGER):
        os.remove(DEPLOY_TRIGGER)
        station.enter_mindfulness()
        print(f'Deploy start: mindfulness for {MINDFULNESS_SECS}s', flush=True)

    time.sleep(0.5)
    tracks_count = len([f for f in os.listdir(AUDIO_DIR)
                        if f.lower().endswith(('.mp3', '.flac'))])
    print(f'Macka Station v3  port={PORT}  tracks={tracks_count}', flush=True)
    Server(('0.0.0.0', PORT), Handler).serve_forever()
