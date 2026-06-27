#!/usr/bin/env python3
"""
Macka Station Radio v4
- ONE encoder per bitrate → shared in-memory ring buffer → fan-out to all listeners.
  Crossfade / ffprobe / disk reads / pacing happen ONCE, not once-per-listener.
  Listener cost is now just a socket write, so the ceiling is bandwidth, not CPU.
- PostgreSQL cycle-based smart shuffle (every track plays once per cycle)
- /cover endpoint — streams embedded album art from ID3 tags
- /now returns track title + has_cover flag
- Dual bitrate: /stream (320kbps) / /stream-lo (128kbps)
- 4s acrossfade between tracks
- Zero-downtime deploys via SIGUSR1 → mindfulness interlude
"""
import os, time, threading, subprocess, json, hashlib, signal, sys, socket, random
from collections import deque
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

AUDIO_DIR       = os.getenv('AUDIO_DIR',       '/srv/macka/audio')
AUDIO_LO_DIR    = os.getenv('AUDIO_LO_DIR',    '/srv/macka/audio_lo')
TRANSITIONS_DIR = os.getenv('TRANSITIONS_DIR', '/srv/macka/sounds/transitions')
MINDFULNESS     = os.getenv('MINDFULNESS',     '/srv/macka/sounds/mindfulness.mp3')
DATABASE_URL    = os.getenv('DATABASE_URL',    'postgresql://macka:mackapass@localhost/macka')
PORT            = int(os.getenv('PORT', 8765))
MINDFULNESS_SECS = int(os.getenv('MINDFULNESS_SECS', '20'))
CHUNK           = 32768
BURST_SECS      = 8
CROSSFADE       = 4.0
DEPLOY_TRIGGER  = '/tmp/macka_deploy'

import psycopg2
import psycopg2.pool

# ---------- systemd watchdog -------------------------------------------------
# The master encoder pings WATCHDOG=1 while it's producing audio. If it ever
# wedges (the failure that drained listeners to silence), the pings stop and
# systemd auto-restarts the service — self-healing, no human in the loop.

def _sd_notify(state: str):
    addr = os.environ.get('NOTIFY_SOCKET')
    if not addr:
        return
    if addr[0] == '@':
        addr = '\0' + addr[1:]   # abstract namespace socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(addr)
            s.sendall(state.encode())
    except OSError:
        pass

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
             '-ar', '44100',   # force 44.1kHz so a stray 48k file can't change the
                               # stream's sample rate mid-MP3 and break mobile decoders
             '-b:a', f'{bitrate_kbps}k', '-f', 'mp3', 'pipe:1'],
            capture_output=True, timeout=12
        )
        return r.stdout if len(r.stdout) > 2000 else None
    except Exception:
        return None

# ---------- intermission transitions -----------------------------------------
# During any intermission (deploy / mindfulness) we play random lo-fi cuts from
# the transitions pool, crossfading between them for as long as it lasts.

def list_transitions():
    try:
        return [os.path.join(TRANSITIONS_DIR, f) for f in sorted(os.listdir(TRANSITIONS_DIR))
                if f.lower().endswith(('.mp3', '.flac', '.ogg', '.m4a'))]
    except OSError:
        return []

def random_transition(exclude=None):
    pool = list_transitions()
    if not pool:
        return MINDFULNESS                       # legacy single-file fallback
    choices = [p for p in pool if p != exclude] or pool
    return random.choice(choices)

def is_transition(path):
    return bool(path) and (path == MINDFULNESS or path.startswith(TRANSITIONS_DIR))

def lo_path(hi_path):
    if is_transition(hi_path):
        return hi_path                           # transitions have no lo variant
    name = os.path.basename(hi_path)
    lo   = os.path.join(AUDIO_LO_DIR, name)
    return lo if os.path.exists(lo) else hi_path

# ---------- shared playlist --------------------------------------------------

class Playlist:
    """
    Single ordered sequence of upcoming track paths, shared by both encoders so
    /stream and /stream-lo always walk the SAME tracks in the SAME order.
    Each encoder holds its own integer cursor; entries are produced on demand.
    """
    def __init__(self):
        self._paths: list[str] = []   # paths[seq - base]
        self._base = 0
        self._lock = threading.Lock()

    def at(self, seq: int) -> str:
        with self._lock:
            while seq >= self._base + len(self._paths):
                fn = next_track_filename()
                self._paths.append(os.path.join(AUDIO_DIR, fn))
            # bound memory; encoders stay within ~1 of each other so trimming
            # 200 behind the leader never strands a live cursor
            if len(self._paths) > 300:
                cut = 200
                self._paths = self._paths[cut:]
                self._base += cut
            return self._paths[seq - self._base]

playlist = Playlist()

# ---------- shared broadcast buffer ------------------------------------------

class Broadcast:
    """
    One ring buffer per bitrate. The encoder pushes encoded chunks; every
    listener tails the same buffer. New listeners get the recent backlog (burst)
    for an instant start, then follow live. Listeners that fall behind the
    window resync to live (a radio drop, never a stall on the producer).
    """
    def __init__(self, burst_chunks: int):
        self._cond = threading.Condition()
        self._buf: deque[bytes] = deque(maxlen=burst_chunks)
        self._seq = 0   # total chunks ever pushed

    def push(self, data: bytes):
        if not data:
            return
        with self._cond:
            self._buf.append(data)
            self._seq += 1
            self._cond.notify_all()

    def stream(self):
        """Generator: yield burst backlog, then live chunks forever."""
        with self._cond:
            backlog = list(self._buf)
            want = self._seq            # next chunk index after the backlog
        for c in backlog:
            yield c
        while True:
            with self._cond:
                while self._seq <= want:
                    self._cond.wait()
                oldest = self._seq - len(self._buf)
                if want < oldest:       # fell behind the window → skip to live
                    want = oldest
                new = list(self._buf)[want - oldest:]
                want = self._seq
            for c in new:
                yield c

# ~20s of burst backlog per bitrate (chunks = bitrate / CHUNK) — generous cushion
# so a slow boundary crossfade can't drain a listener to silence before the
# continuous deadline flushes the catch-up and refills it
broadcast_hi = Broadcast(burst_chunks=24)
broadcast_lo = Broadcast(burst_chunks=12)

# ---------- station metadata + mindfulness -----------------------------------

class Station:
    """Holds 'what's playing' for /now + /cover, and the mindfulness flag."""
    def __init__(self):
        self._lock              = threading.Lock()
        self.current            = '—'
        self.has_cover          = False
        self._cur_path          = None
        self._mindfulness       = False
        self._mindfulness_until = 0.0

    def note_now(self, path: str):
        """Called (off-thread) by the master encoder when a new track starts.
        The ffprobe/ffmpeg probes run OUTSIDE the lock — otherwise they'd hold it
        for ~1.5s and stall the encoder's per-chunk is_mindfulness() check."""
        if is_transition(path):
            cur, cover = '· · ·', False
        else:
            title, artist = probe_tags(path)
            cur = (f'{artist} – {title}' if artist and title
                   else (title or os.path.splitext(os.path.basename(path))[0]))
            cover = bool(extract_cover(path))
        with self._lock:
            self._cur_path = path
            self.current   = cur
            self.has_cover = cover
        label = f'{cur}  «{os.path.basename(path)}»' if is_transition(path) else cur
        print(f'NOW  {label}', flush=True)

    def cur_path(self):
        with self._lock:
            return self._cur_path

    # ── mindfulness ───────────────────────────────────────────────────────────

    def enter_mindfulness(self, secs=MINDFULNESS_SECS):
        with self._lock:
            self._mindfulness       = True
            self._mindfulness_until = time.monotonic() + secs
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

station = Station()

# ---------- encoder (one per bitrate) ----------------------------------------

class Encoder(threading.Thread):
    """
    Reads the shared playlist at real-time pace for one bitrate, generates the
    crossfade ONCE, and pushes chunks to its Broadcast buffer. Exactly two of
    these run for the whole station regardless of how many people are listening.
    """
    def __init__(self, broadcast: Broadcast, lo: bool, master: bool):
        super().__init__(daemon=True)
        self.broadcast = broadcast
        self.lo        = lo
        self.master    = master   # the hi encoder owns /now metadata + watchdog
        self.seq       = 0        # playlist cursor
        self._last_ping = 0.0     # systemd watchdog throttle

    def _next(self, _cur):
        if station.is_mindfulness():
            return random_transition(exclude=_cur)   # random lo-fi cut, no repeat
        p = playlist.at(self.seq)
        self.seq += 1
        return p

    def _announce(self, path):
        """Update /now metadata off the hot path — probe_tags/extract_cover spawn
        ffprobe/ffmpeg and must never stall the chunk pump."""
        if self.master:
            threading.Thread(target=station.note_now, args=(path,), daemon=True).start()

    def run(self):
        path   = self._next(None)
        offset = 0
        self._announce(path)
        # ONE continuous pacing clock for the encoder's whole life — never reset
        # per track. A stall (crossfade/ffprobe) leaves the deadline in the past,
        # so the next chunks flush back-to-back and refill every listener's buffer
        # instead of all of them slowly draining to silence at each boundary.
        deadline = time.monotonic()

        while True:
            serve = lo_path(path) if (self.lo and not is_transition(path)) else path
            dur, bitrate = ffprobe_info(serve)
            bytes_sec = (bitrate or (128_000 if self.lo else 320_000)) / 8
            cf_byte   = int((dur - CROSSFADE) * bytes_sec) if dur else os.path.getsize(serve)
            cf_bytes  = None
            nxt       = None
            emergency = False

            try:
                with open(serve, 'rb') as f:
                    f.seek(offset)
                    while True:
                        pos   = f.tell()
                        chunk = f.read(CHUNK)
                        if not chunk:
                            break

                        # Emergency crossfade: intermission flipped on mid-track
                        if not is_transition(serve) and station.is_mindfulness() and cf_bytes is None:
                            tr       = random_transition(exclude=serve)
                            pos_sec  = max(0.0, (pos - offset) / bytes_sec)
                            cf_bytes = make_crossfade(
                                serve, tr,
                                bitrate_kbps=128 if self.lo else 320,
                                offset_a_sec=pos_sec if dur and pos_sec < dur - CROSSFADE else None,
                            )
                            nxt       = tr
                            emergency = True
                            break

                        # Normal crossfade at end of track
                        if pos >= cf_byte and cf_bytes is None:
                            nxt       = self._next(path)
                            nxt_serve = lo_path(nxt) if (self.lo and not is_transition(nxt)) else nxt
                            cf_bytes  = make_crossfade(
                                serve, nxt_serve, bitrate_kbps=128 if self.lo else 320)
                            chunk     = chunk[:max(0, cf_byte - pos)]
                            if not chunk:
                                break

                        self.broadcast.push(chunk)
                        # pace at real time so the buffer stays "live"
                        deadline += len(chunk) / bytes_sec
                        now = time.monotonic()
                        # heartbeat: prove the encoder is still producing audio
                        if self.master and now - self._last_ping > 5:
                            _sd_notify('WATCHDOG=1')
                            self._last_ping = now
                        lag = deadline - now
                        if lag > 0.005:
                            time.sleep(lag)
                        elif lag < -BURST_SECS:
                            # fell too far behind (e.g. a slow crossfade) — flush at
                            # most BURST_SECS fast to refill buffers, then resync so
                            # we never dump more than the buffer can hold at once
                            deadline = now - BURST_SECS
            except (FileNotFoundError, OSError) as e:
                print(f'encoder {"lo" if self.lo else "hi"}: skip {serve}: {e}', flush=True)

            if cf_bytes:
                self.broadcast.push(cf_bytes)
                deadline += CROSSFADE   # the crossfade is ~CROSSFADE s of audio

            path = nxt or self._next(path)
            # after a real crossfade we already emitted the next track's first 4s
            offset = int(CROSSFADE * bytes_sec) if (cf_bytes and not emergency) else 0
            self._announce(path)

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
            cur  = station.cur_path()
            data = extract_cover(cur) if cur and not is_transition(cur) else None
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
        """A listener is now just a tap on the shared buffer — no work per client."""
        self.send_response(200)
        self.send_header('Content-Type', 'audio/mpeg')
        self.send_header('Cache-Control', 'no-cache, no-store')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('icy-name', 'Macka Station')
        self.end_headers()

        bc = broadcast_lo if lo else broadcast_hi
        try:
            for chunk in bc.stream():
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    print('Macka Station v4  initialising…', flush=True)
    init_db()
    sync_tracks()
    threading.Thread(target=enrich_tracks, daemon=True).start()

    # Deploy trigger: start in mindfulness, fade to music after MINDFULNESS_SECS
    if os.path.exists(DEPLOY_TRIGGER):
        os.remove(DEPLOY_TRIGGER)
        station.enter_mindfulness()
        print(f'Deploy start: mindfulness for {MINDFULNESS_SECS}s', flush=True)

    # Two encoders for the whole station — hi is the metadata master
    Encoder(broadcast_hi, lo=False, master=True).start()
    Encoder(broadcast_lo, lo=True,  master=False).start()

    time.sleep(0.5)
    tracks_count = len([f for f in os.listdir(AUDIO_DIR)
                        if f.lower().endswith(('.mp3', '.flac'))])
    print(f'Macka Station v5  port={PORT}  tracks={tracks_count}', flush=True)
    _sd_notify('READY=1')   # tell systemd we're up (Type=notify + watchdog)
    Server(('0.0.0.0', PORT), Handler).serve_forever()
