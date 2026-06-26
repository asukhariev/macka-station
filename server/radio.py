#!/usr/bin/env python3
"""
Macka Station Radio v3
- PostgreSQL smart shuffle: every track plays once per cycle before repeating
- Album art endpoint /cover  (ffmpeg extract, in-memory cache)
- /now returns track title + has_cover flag
- Dual bitrate: /stream (320kbps) / /stream-lo (128kbps)
- 4s acrossfade between tracks
- ffprobe for accurate bitrate/duration
"""
import os, time, threading, subprocess, json, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

import psycopg2
import psycopg2.pool

AUDIO_DIR    = os.getenv('AUDIO_DIR',    '/srv/macka/audio')
AUDIO_LO_DIR = os.getenv('AUDIO_LO_DIR', '/srv/macka/audio_lo')
DATABASE_URL  = os.getenv('DATABASE_URL', 'postgresql://macka:mackapass@localhost/macka')
PORT         = int(os.getenv('PORT', 8765))
CHUNK        = 32768
BURST_SECS   = 8
CROSSFADE    = 4.0

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
    """
    Phase 1 (fast, synchronous): insert all filenames so shuffle can start immediately.
    Phase 2 (slow, background): enrich title/artist/has_cover via ffprobe.
    """
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
    print(f'sync_tracks phase1: {len(files)} files indexed', flush=True)

def enrich_tracks():
    """Background: populate title/artist/has_cover for un-enriched rows."""
    while True:
        conn = db()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT id, filename FROM tracks WHERE title IS NULL LIMIT 20")
                rows = cur.fetchall()
        finally:
            db_release(conn)

        if not rows:
            print('enrich_tracks: all done', flush=True)
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
        time.sleep(0.05)  # avoid hammering disk

def next_track_filename():
    """Return next filename using cycle-based smart shuffle."""
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
                # All tracks played — start new cycle
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

def make_crossfade(path_a, path_b, bitrate_kbps=320):
    try:
        r = subprocess.run(
            ['ffmpeg', '-y',
             '-sseof', f'-{CROSSFADE}', '-i', path_a,
             '-t', str(CROSSFADE), '-i', path_b,
             '-filter_complex', f'[0][1]acrossfade=d={CROSSFADE}:c1=tri:c2=tri',
             '-b:a', f'{bitrate_kbps}k', '-f', 'mp3', 'pipe:1'],
            capture_output=True, timeout=30
        )
        return r.stdout if len(r.stdout) > 2000 else None
    except Exception:
        return None

def lo_path(hi_path):
    name = os.path.basename(hi_path)
    lo   = os.path.join(AUDIO_LO_DIR, name)
    return lo if os.path.exists(lo) else hi_path

# ---------- station ----------------------------------------------------------

class Station:
    def __init__(self):
        self._lock      = threading.Lock()
        self._filename  = None
        self._path      = None
        self._dur       = None
        self._bitrate   = 320_000
        self._started   = time.monotonic()
        self.current    = '—'
        self.has_cover  = False

    def _switch(self, filename):
        path = os.path.join(AUDIO_DIR, filename)
        dur, br = ffprobe_info(path)
        # Cover check: use cached DB value if available
        cover_data = extract_cover(path)
        with self._lock:
            self._filename  = filename
            self._path      = path
            self._dur       = dur
            self._bitrate   = br
            self._started   = time.monotonic()
            self.has_cover  = bool(cover_data)
            conn = db()
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT title, artist FROM tracks WHERE filename=%s", (filename,))
                    row = cur.fetchone()
                title  = (row[0] if row else '') or ''
                artist = (row[1] if row else '') or ''
                self.current = f'{artist} – {title}' if artist and title else (title or os.path.splitext(filename)[0])
            finally:
                db_release(conn)
        print(f'NOW  {self.current}  cover={self.has_cover}  [{br//1000}kbps]', flush=True)

    def snapshot(self):
        with self._lock:
            elapsed = time.monotonic() - self._started
            return dict(
                filename=self._filename,
                path=self._path,
                bitrate=self._bitrate,
                dur=self._dur,
                offset=max(0, int(elapsed * self._bitrate / 8) - CHUNK),
                elapsed=elapsed,
            )

    def run(self):
        while True:
            filename = next_track_filename()
            self._switch(filename)
            # Wait until near end of track
            while True:
                time.sleep(1)
                with self._lock:
                    if not self._dur:
                        break
                    elapsed = time.monotonic() - self._started
                    if elapsed >= self._dur - CROSSFADE - 0.5:
                        break

station = Station()

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
            body = json.dumps({
                'track':     station.current,
                'has_cover': station.has_cover,
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
            data = extract_cover(snap['path']) if snap['path'] else None
            if data:
                self.send_response(200)
                # Detect JPEG vs PNG by magic bytes
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
            snap      = station.snapshot()
            hi_path   = snap['path']
            serve_path = lo_path(hi_path) if lo else hi_path
            bitrate   = snap['bitrate']
            dur       = snap['dur']
            offset    = snap['offset']
            cf_cache: dict = {}

            while True:
                bytes_sec = bitrate / 8
                burst_b   = int(BURST_SECS * bytes_sec)
                cf_byte   = int((dur - CROSSFADE) * bytes_sec) if dur else os.path.getsize(serve_path)
                sent      = 0
                deadline  = time.monotonic()
                cf_bytes  = None

                with open(serve_path, 'rb') as f:
                    f.seek(offset)
                    while True:
                        pos   = f.tell()
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break

                        if pos >= cf_byte and cf_bytes is None:
                            # Pre-generate crossfade with next track
                            fn_next   = next_track_filename()
                            nxt_hi    = os.path.join(AUDIO_DIR, fn_next)
                            nxt_serve = lo_path(nxt_hi) if lo else nxt_hi
                            key = (serve_path, nxt_serve)
                            if key not in cf_cache:
                                cf_cache[key] = make_crossfade(
                                    serve_path, nxt_serve,
                                    bitrate_kbps=128 if lo else 320
                                )
                            cf_bytes  = cf_cache[key]
                            next_file = fn_next
                            chunk     = chunk[:max(0, cf_byte - pos)]
                            if not chunk:
                                break

                        self.wfile.write(chunk)
                        sent += len(chunk)
                        if sent > burst_b:
                            deadline += len(chunk) / bytes_sec
                            lag = deadline - time.monotonic()
                            if lag > 0.005:
                                time.sleep(lag)

                if cf_bytes:
                    self.wfile.write(cf_bytes)

                # Switch station to next track (already picked above)
                hi_path    = os.path.join(AUDIO_DIR, next_file)
                serve_path = lo_path(hi_path) if lo else hi_path
                dur, bitrate = ffprobe_info(serve_path)
                offset     = int(CROSSFADE * bitrate / 8) if cf_bytes else 0
                cf_bytes   = None
                station._switch(next_file)

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    print('Macka Station v3  initialising…', flush=True)
    init_db()
    sync_tracks()                                                    # fast: filenames only
    threading.Thread(target=enrich_tracks, daemon=True).start()    # slow: metadata in bg
    threading.Thread(target=station.run,   daemon=True).start()
    time.sleep(1)
    tracks_count = len([f for f in os.listdir(AUDIO_DIR)
                        if f.lower().endswith(('.mp3', '.flac'))])
    print(f'Macka Station v3  port={PORT}  tracks={tracks_count}', flush=True)
    Server(('0.0.0.0', PORT), Handler).serve_forever()
