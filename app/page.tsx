'use client';

import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';

const BASE       = 'https://macka.agtc.app';
const STREAM_HI  = process.env.NEXT_PUBLIC_STREAM_URL    ?? `${BASE}/stream`;
const STREAM_LO  = process.env.NEXT_PUBLIC_STREAM_LO_URL ?? `${BASE}/stream-lo`;
const NOW_URL    = process.env.NEXT_PUBLIC_NOW_URL        ?? `${BASE}/now`;
const COVER_URL  = process.env.NEXT_PUBLIC_COVER_URL      ?? `${BASE}/cover`;

const T = {
  dark: {
    bg:      [3, 7, 15] as const,
    glowMid: (b: number) => `rgb(10,${Math.round((0.08+b*0.18)*255)},${Math.round((0.18+b*0.28)*255)})`,
  },
  light: {
    bg:      [238, 245, 255] as const,
    glowMid: (b: number) => `rgb(180,${Math.round(205+b*30)},${Math.round(240+b*15)})`,
  },
};

export default function MackaPage() {
  const canvasRef  = useRef<HTMLCanvasElement>(null);
  const playerRef  = useRef<HTMLAudioElement | null>(null);
  const ofscr1     = useRef<HTMLCanvasElement | null>(null);
  const ofscr2     = useRef<HTMLCanvasElement | null>(null);
  const analyser   = useRef<AnalyserNode | null>(null);
  const dataFreq   = useRef<Uint8Array<ArrayBuffer> | null>(null);
  const bassSmooth   = useRef(0);
  const beatDisplay  = useRef(0);
  const ripplesRef   = useRef<{ x: number; y: number; t: number }[]>([]);
  const audioCtx   = useRef<AudioContext | null>(null);
  const pollRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const frameRef   = useRef(0);

  const onRef    = useRef(false);
  const lightRef = useRef(false);
  const isLoRef  = useRef(false);

  const userPaused     = useRef(true);                               // false only while the user wants audio (after pressing play)
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const lastTime       = useRef(0);                                  // for the liveness watchdog

  // canvas text bounds → used to position the play button and track info
  const textBoundsRef = useRef({ top: 0, bottom: 0, set: false });
  const [textBounds, setTextBounds] = useState({ top: 0, bottom: 0 });

  const [on,       setOn]       = useState(false);
  const [isLight,  setIsLight]  = useState(() => {
    if (typeof window === 'undefined') return false;
    return localStorage.getItem('macka-theme') === 'light';
  });
  const [isLo,     setIsLo]     = useState(false);
  const [track,    setTrack]    = useState('—');
  const [status,   setStatus]   = useState('macka station');
  const [live,     setLive]     = useState(false);
  const [hasCover, setHasCover] = useState(false);
  const [coverKey, setCoverKey] = useState(0);
  const [mindful,  setMindful]  = useState(false);
  const trackRef = useRef<HTMLAnchorElement>(null);
  const [isMarquee, setIsMarquee] = useState(false);
  const [marqueeOffset, setMarqueeOffset] = useState('0px');

  useLayoutEffect(() => {
    const el = trackRef.current;
    if (!el) return;
    const overflow = el.scrollWidth - el.parentElement!.clientWidth;
    setIsMarquee(overflow > 0);
    setMarqueeOffset(overflow > 0 ? `-${overflow}px` : '0px');
  }, [track]);

  useEffect(() => { isLoRef.current = isLo; }, [isLo]);
  useEffect(() => { onRef.current   = on;   }, [on]);
  useEffect(() => {
    lightRef.current = isLight;
    document.body.classList.toggle('light', isLight);
    const bg = T[isLight ? 'light' : 'dark'].bg;
    document.body.style.background = `rgb(${bg[0]},${bg[1]},${bg[2]})`;
    localStorage.setItem('macka-theme', isLight ? 'light' : 'dark');
  }, [isLight]);

  // animation loop — runs once on mount
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;

    function resize() {
      canvas!.width  = window.innerWidth;
      canvas!.height = window.innerHeight;
      ofscr1.current = null;
      ofscr2.current = null;
      textBoundsRef.current.set = false;
    }
    resize();
    window.addEventListener('resize', resize);

    function drawFrame() {
      frameRef.current = requestAnimationFrame(drawFrame);
      const W = canvas!.width, H = canvas!.height;
      const cx = W / 2, cy = H / 2;
      const R      = Math.min(W, H) * 0.28;
      const mobile  = W < 640;
      const on      = onRef.current;
      const isLight = lightRef.current;
      const t = T[isLight ? 'light' : 'dark'];

      if (analyser.current && dataFreq.current) {
        analyser.current.getByteFrequencyData(dataFreq.current);
        const bass = dataFreq.current.slice(0, 6).reduce((a, b) => a + b, 0) / 6 / 255;
        bassSmooth.current += (bass - bassSmooth.current) * 0.15;
      }

      const bg = t.bg;
      ctx.fillStyle = `rgb(${bg[0]},${bg[1]},${bg[2]})`;
      ctx.fillRect(0, 0, W, H);

      const glow = ctx.createRadialGradient(cx, cy, R * 0.3, cx, cy, R * 2.2);
      glow.addColorStop(0, t.glowMid(0.35 + bassSmooth.current * 0.65));
      glow.addColorStop(1, `rgb(${bg[0]},${bg[1]},${bg[2]})`);
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);

      {
        const now2 = performance.now();
        const bLH = 3, bGap = 2, step = 7;
        for (let y = 0; y < H; y += bLH + bGap) {
          const prog = y / H;
          const hue = (isLight ? 340 : 320) + Math.sin(now2 * 0.00012 + prog * Math.PI * 2) * 40;
          const lum = isLight ? 55 : 62;
          const a   = (isLight ? 0.18 : 0.22) + bassSmooth.current * 0.12;
          ctx.beginPath();
          for (let x = 0; x <= W + step; x += step) {
            const xp = x / W;
            const wm = mobile ? 0.4 : 1;
            const wy = Math.sin(now2 * 0.00022 + xp * 6    + prog * 5.5) * 6 * wm
                     + Math.sin(now2 * 0.00055 + xp * 13.5 + prog * 9 + 2) * 3 * wm
                     + bassSmooth.current * Math.sin(now2 * 0.00090 + xp * 22 + prog * 14) * 5 * wm;
            x === 0 ? ctx.moveTo(x, y + wy) : ctx.lineTo(x, y + wy);
          }
          ctx.strokeStyle = `hsla(${hue},72%,${lum}%,${a})`;
          ctx.lineWidth = bLH;
          ctx.stroke();
        }
      }

      const targetBeat = on ? bassSmooth.current : 0;
      beatDisplay.current += (targetBeat - beatDisplay.current) * 0.025;
      const beat = beatDisplay.current;
      const fsize = mobile
        ? Math.min(W * 0.38, H * 0.18, 180)
        : Math.min(W * 0.22, H * 0.28, 260);
      const numBins = analyser.current ? dataFreq.current!.length : 128;
      const now   = performance.now();
      const lineH = 3, lineGap = 2;
      const FONT  = `900 ${fsize}px Impact, 'Arial Black', sans-serif`;

      if (!ofscr1.current || ofscr1.current.width !== W || ofscr1.current.height !== H) {
        ofscr1.current = document.createElement('canvas');
        ofscr1.current.width = W; ofscr1.current.height = H;
      }
      if (!ofscr2.current || ofscr2.current.width !== W || ofscr2.current.height !== H) {
        ofscr2.current = document.createElement('canvas');
        ofscr2.current.width = W; ofscr2.current.height = H;
      }

      const ofx  = ofscr1.current.getContext('2d')!;
      ofx.clearRect(0, 0, W, H);
      ofx.font = FONT;
      ofx.textAlign = 'center';
      ofx.textBaseline = 'alphabetic';

      const m1 = ofx.measureText('Macka');
      const m2 = ofx.measureText('Funk');
      const interLine  = fsize * 0.1;
      const blockH     = m1.actualBoundingBoxAscent + m1.actualBoundingBoxDescent
                       + interLine
                       + m2.actualBoundingBoxAscent + m2.actualBoundingBoxDescent;
      const y1         = cy - blockH / 2 + m1.actualBoundingBoxAscent;
      const y2         = y1 + m1.actualBoundingBoxDescent + interLine + m2.actualBoundingBoxAscent;
      const textTop    = cy - blockH / 2;
      const textBottom = y2 + m2.actualBoundingBoxDescent;
      const textH      = textBottom - textTop;

      // expose text bounds once (and on resize) so React can position UI elements
      if (!textBoundsRef.current.set || Math.abs(textTop - textBoundsRef.current.top) > 4) {
        textBoundsRef.current = { top: textTop, bottom: textBottom, set: true };
        setTextBounds({ top: textTop, bottom: textBottom });
      }

      const lb     = isLight ? 30 : 68;
      const bright = 4 + beat * 14;
      const grad   = ofx.createLinearGradient(0, textTop, 0, textBottom);
      grad.addColorStop(0,   `hsl(205,88%,${lb + 8 + bright}%)`);
      grad.addColorStop(0.5, `hsl(215,92%,${lb + bright}%)`);
      grad.addColorStop(1,   `hsl(228,85%,${lb - 8 + bright}%)`);
      ofx.fillStyle = grad;
      ofx.fillText('Macka', cx, y1);
      ofx.fillText('Funk',  cx, y2);

      const ofx2 = ofscr2.current.getContext('2d')!;
      ofx2.clearRect(0, 0, W, H);
      for (let y = textTop; y < textBottom; y += lineH + lineGap) {
        const prog    = (y - textTop) / textH;
        const binIdx  = Math.min(Math.floor(prog * numBins * 0.85), numBins - 1);
        const freqVal = (analyser.current && dataFreq.current)
          ? dataFreq.current[binIdx] / 255 : 0;
        const swayAmp = mobile ? 4 : 12;
        const dispAmp = mobile ? 14 : 40;
        const sway = Math.sin(now * 0.0005 + prog * Math.PI * 5) * swayAmp;
        const dx   = Math.round(sway + freqVal * dispAmp * beat);
        ofx2.drawImage(ofscr1.current!, 0, y, W, lineH, dx, y, W, lineH);
      }
      ctx.drawImage(ofscr2.current!, 0, 0);

    }

    drawFrame();
    return () => {
      cancelAnimationFrame(frameRef.current);
      window.removeEventListener('resize', resize);
    };
  }, []);

  const setupAudio = useCallback(() => {
    if (audioCtx.current || !playerRef.current) return;
    const ctx = new AudioContext();
    const node = ctx.createAnalyser();
    node.fftSize = 512;
    node.smoothingTimeConstant = 0.8;
    const src = ctx.createMediaElementSource(playerRef.current);
    src.connect(node);
    node.connect(ctx.destination);
    audioCtx.current = ctx;
    analyser.current = node;
    dataFreq.current = new Uint8Array(node.frequencyBinCount) as Uint8Array<ArrayBuffer>;
  }, []);

  const pollNow = useCallback(() => {
    fetch(NOW_URL)
      .then(r => r.json())
      .then((d: { track?: string; has_cover?: boolean; mindful?: boolean }) => {
        const newTrack = d.track ?? '—';
        setTrack(prev => {
          if (prev !== newTrack) setCoverKey(k => k + 1);
          return newTrack;
        });
        setHasCover(!!d.has_cover);
        setMindful(!!d.mindful);
        setLive(true);
      })
      .catch(() => {});
  }, []);

  // Re-attach to the live stream after the connection drops (deploy / network).
  // A process restart kills every open connection — the only way playback resumes
  // without re-tapping play is for the client to reconnect itself. Fresh src →
  // jumps to live; also resume the AudioContext (it suspends when the tab is
  // backgrounded — otherwise the element "plays" but stays silent → looks paused).
  const reconnect = useCallback((delay = 1200) => {
    if (userPaused.current) return;                // only when the user wants audio
    if (reconnectTimer.current) return;            // one attempt in flight
    setOn(true);
    setStatus('reconnecting…');
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      const el = playerRef.current;
      if (!el || userPaused.current) return;
      if (audioCtx.current?.state === 'suspended') audioCtx.current.resume().catch(() => {});
      el.src = isLoRef.current ? STREAM_LO : STREAM_HI;
      el.load();
      el.play().catch(() => {});
    }, delay);
  }, []);

  const toggle = useCallback(() => {
    const el = playerRef.current;
    if (!el) return;
    // Only treat a tap as "pause" when audio is genuinely playing. If the button
    // shows pause but the stream is actually stuck/silent (dropped, suspended),
    // a single tap should RESTART it — never the old pause-then-play dance.
    const reallyPlaying = !el.paused && !el.error && el.readyState >= 2;
    if (onRef.current && reallyPlaying) {
      userPaused.current = true;
      if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
      setOn(false);
      el.pause();
      el.src = '';
      el.load();
    } else {
      userPaused.current = false;
      setOn(true);
      setupAudio();
      if (audioCtx.current?.state === 'suspended') audioCtx.current.resume().catch(() => {});
      el.src = isLoRef.current ? STREAM_LO : STREAM_HI;
      el.load();
      el.play().catch(() => {});
      setStatus('connecting…');
    }
  }, [setupAudio]);

  // Spacebar toggles play/pause (ignored while a button/link/field is focused so
  // it doesn't double-fire or hijack their own space handling).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code !== 'Space' && e.key !== ' ') return;
      const t = e.target as HTMLElement | null;
      const tag = t?.tagName;
      if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'TEXTAREA' || t?.isContentEditable) return;
      e.preventDefault();
      if (!e.repeat) toggle();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [toggle]);

  useEffect(() => {
    const el = document.createElement('audio');
    el.preload = 'none';
    el.crossOrigin = 'anonymous';
    playerRef.current = el;

    // The button reflects user INTENT (set by toggle), not the element's moment-
    // to-moment state — so a deploy drop or an OS pause never leaves it lying.
    el.addEventListener('playing', () => {
      setStatus('live');
      if (reconnectTimer.current) { clearTimeout(reconnectTimer.current); reconnectTimer.current = null; }
    });
    // ended / error / stalled / waiting all mean "no audio is flowing". While the
    // user still wants to listen, reconnect ourselves — no re-tap, ever.
    el.addEventListener('ended',   () => { if (!userPaused.current) reconnect(700); });
    el.addEventListener('error',   () => { if (!userPaused.current) reconnect(1500); });
    el.addEventListener('stalled', () => { setStatus('buffering…'); if (!userPaused.current) reconnect(6000); });
    el.addEventListener('waiting', () => { setStatus('buffering…'); if (!userPaused.current) reconnect(6000); });

    // start polling /now immediately — track name shows before pressing play
    pollNow();
    pollRef.current = setInterval(pollNow, 5000);

    return () => {
      el.pause();
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [pollNow]);

  // Liveness watchdog: if we think we're playing but currentTime stops advancing
  // (and we're not OS-paused), the connection silently died — reconnect. Belt-and-
  // suspenders behind the ended/error/stalled handlers for browsers that just go
  // quiet without firing an event.
  useEffect(() => {
    const iv = setInterval(() => {
      const el = playerRef.current;
      if (!el || userPaused.current || el.paused) return;
      if (el.currentTime === lastTime.current) reconnect(0);   // frozen while "playing" → re-attach
      lastTime.current = el.currentTime;
    }, 4000);
    return () => clearInterval(iv);
  }, [reconnect]);

  // Returning to the tab/app: if the user wants audio but it isn't actually
  // flowing (backgrounding suspended the context or dropped the connection),
  // wake the context and re-attach — so coming back resumes on its own.
  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState !== 'visible' || userPaused.current) return;
      if (audioCtx.current?.state === 'suspended') audioCtx.current.resume().catch(() => {});
      const el = playerRef.current;
      if (!el || el.paused || el.readyState < 2) reconnect(0);
    };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, [reconnect]);

  const infoTop = textBounds.bottom > 0
    ? Math.min(textBounds.bottom + 20, (typeof window !== 'undefined' ? window.innerHeight : 800) - 60)
    : undefined;

  return (
    <>
      <canvas ref={canvasRef} />
      <div id="ui">

        {/* top-left: quality */}
        <div id="top-left">
          <button
            className="ctrl-btn"
            style={{ fontSize: '.7rem', letterSpacing: '.2em' }}
            title="hi / lo quality"
            onClick={() => {
              const next = !isLoRef.current;
              setIsLo(next);
              if (onRef.current && playerRef.current) {
                playerRef.current.src = next ? STREAM_LO : STREAM_HI;
                playerRef.current.play().catch(() => {});
              }
            }}
          >
            {isLo ? 'lo' : 'hi'}
          </button>
        </div>

        {/* top-right: theme */}
        <div id="top-right">
          <button
            className="ctrl-btn theme-btn"
            title="light / dark"
            onClick={() => setIsLight(v => !v)}
          >
            {isLight ? '●' : '○'}
          </button>
        </div>

        {/* invisible hit-area over the canvas text */}
        {textBounds.bottom > 0 && (
          <div
            onClick={(e) => {
              toggle();
              ripplesRef.current.push({ x: e.clientX, y: e.clientY, t: performance.now() });
            }}
            style={{
              position: 'absolute',
              left: '10%', width: '80%',
              top: textBounds.top,
              height: textBounds.bottom - textBounds.top,
              cursor: 'pointer',
              pointerEvents: 'all',
              outline: 'none',
              WebkitTapHighlightColor: 'transparent',
              userSelect: 'none',
            }}
          />
        )}

        {/* play — centered at top */}
        <button id="play-btn" className="ctrl-btn" onClick={toggle}>
          {on ? 'pause' : 'play'}
        </button>

        {/* track info — centered at bottom */}
        <div id="bottom-center">
          {hasCover && !mindful && (
            <img
              key={coverKey}
              src={`${COVER_URL}?t=${coverKey}`}
              alt=""
              id="cover-art"
            />
          )}
          {mindful ? (
            <div id="track-wrap">
              <span id="track" className="intermission">{track}</span>
            </div>
          ) : track && track !== '—' && (
            <div id="track-wrap">
              <a
                ref={trackRef}
                id="track"
                className={[live ? 'live' : '', isMarquee ? 'marquee' : ''].join(' ').trim()}
                href={`https://music.youtube.com/search?q=${encodeURIComponent(track)}`}
                target="_blank"
                rel="noopener noreferrer"
                style={{ pointerEvents: 'all', '--marquee-dur': `${Math.max(8, track.length * 0.22)}s` } as React.CSSProperties}
              >
                {isMarquee ? <><span>{track}</span><span className="sep"/><span>{track}</span><span className="sep"/></> : track}
              </a>
            </div>
          )}
        </div>

      </div>
    </>
  );
}
