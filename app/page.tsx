'use client';

import { useEffect, useRef, useState, useCallback } from 'react';

const STREAM_HI  = process.env.NEXT_PUBLIC_STREAM_URL    ?? '/stream';
const STREAM_LO  = process.env.NEXT_PUBLIC_STREAM_LO_URL ?? '/stream-lo';
const NOW_URL    = process.env.NEXT_PUBLIC_NOW_URL        ?? '/now';

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
  const bassSmooth = useRef(0);
  const audioCtx   = useRef<AudioContext | null>(null);
  const pollRef    = useRef<ReturnType<typeof setInterval> | null>(null);
  const frameRef   = useRef(0);

  // animation-loop reads these refs (avoids stale closures)
  const onRef      = useRef(false);
  const lightRef   = useRef(false);

  const [on,      setOn]      = useState(false);
  const [isLight, setIsLight] = useState(false);
  const [isLo,    setIsLo]    = useState(false);
  const [track,   setTrack]   = useState('—');
  const [status,  setStatus]  = useState('macka station');
  const [live,    setLive]    = useState(false);

  const isLoRef = useRef(false);
  useEffect(() => { isLoRef.current = isLo; }, [isLo]);

  // keep refs in sync
  useEffect(() => { onRef.current = on; }, [on]);
  useEffect(() => {
    lightRef.current = isLight;
    document.body.classList.toggle('light', isLight);
    const bg = T[isLight ? 'light' : 'dark'].bg;
    document.body.style.background = `rgb(${bg[0]},${bg[1]},${bg[2]})`;
  }, [isLight]);

  // animation loop — runs once on mount
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d')!;

    function resize() {
      canvas!.width  = window.innerWidth;
      canvas!.height = window.innerHeight;
      ofscr1.current = null; // invalidate offscreen caches
      ofscr2.current = null;
    }
    resize();
    window.addEventListener('resize', resize);

    function drawFrame() {
      frameRef.current = requestAnimationFrame(drawFrame);
      const W = canvas!.width, H = canvas!.height;
      const cx = W / 2, cy = H / 2;
      const R  = Math.min(W, H) * 0.28;
      const on      = onRef.current;
      const isLight = lightRef.current;
      const t = T[isLight ? 'light' : 'dark'];

      // update bass
      if (analyser.current && dataFreq.current) {
        analyser.current.getByteFrequencyData(dataFreq.current);
        const bass = dataFreq.current.slice(0, 6).reduce((a, b) => a + b, 0) / 6 / 255;
        bassSmooth.current += (bass - bassSmooth.current) * 0.15;
      }

      // background
      const bg = t.bg;
      ctx.fillStyle = `rgb(${bg[0]},${bg[1]},${bg[2]})`;
      ctx.fillRect(0, 0, W, H);

      // center glow — always present (base 0.35)
      const glow = ctx.createRadialGradient(cx, cy, R * 0.3, cx, cy, R * 2.2);
      glow.addColorStop(0, t.glowMid(0.35 + bassSmooth.current * 0.65));
      glow.addColorStop(1, `rgb(${bg[0]},${bg[1]},${bg[2]})`);
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, W, H);

      // psychedelic background scanlines
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
            const wy = Math.sin(now2 * 0.00022 + xp * 6    + prog * 5.5) * 6
                     + Math.sin(now2 * 0.00055 + xp * 13.5 + prog * 9 + 2) * 3
                     + bassSmooth.current * Math.sin(now2 * 0.00090 + xp * 22 + prog * 14) * 5;
            x === 0 ? ctx.moveTo(x, y + wy) : ctx.lineTo(x, y + wy);
          }
          ctx.strokeStyle = `hsla(${hue},72%,${lum}%,${a})`;
          ctx.lineWidth = bLH;
          ctx.stroke();
        }
      }

      // scanline equalizer text
      const beat  = on ? bassSmooth.current : 0;
      const fsize = Math.min(W * 0.22, H * 0.28, 260);
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

      // pixel-perfect centering via measureText
      const m1 = ofx.measureText('Macka');
      const m2 = ofx.measureText('Funk');
      const interLine = fsize * 0.1;
      const blockH = m1.actualBoundingBoxAscent  + m1.actualBoundingBoxDescent
                   + interLine
                   + m2.actualBoundingBoxAscent  + m2.actualBoundingBoxDescent;
      const y1       = cy - blockH / 2 + m1.actualBoundingBoxAscent;
      const y2       = y1 + m1.actualBoundingBoxDescent + interLine + m2.actualBoundingBoxAscent;
      const textTop  = cy - blockH / 2;
      const textBottom = y2 + m2.actualBoundingBoxDescent;
      const textH    = textBottom - textTop;

      const lb     = isLight ? 30 : 68;
      const bright = 4 + beat * 14;
      const grad   = ofx.createLinearGradient(0, textTop, 0, textBottom);
      grad.addColorStop(0,   `hsl(205,88%,${lb + 8 + bright}%)`);
      grad.addColorStop(0.5, `hsl(215,92%,${lb + bright}%)`);
      grad.addColorStop(1,   `hsl(228,85%,${lb - 8 + bright}%)`);
      ofx.fillStyle = grad;
      ofx.fillText('Macka', cx, y1);
      ofx.fillText('Funk',  cx, y2);

      // strips with per-strip displacement
      const ofx2 = ofscr2.current.getContext('2d')!;
      ofx2.clearRect(0, 0, W, H);
      for (let y = textTop; y < textBottom; y += lineH + lineGap) {
        const prog   = (y - textTop) / textH;
        const binIdx = Math.min(Math.floor(prog * numBins * 0.85), numBins - 1);
        const freqVal = (analyser.current && dataFreq.current)
          ? dataFreq.current[binIdx] / 255 : 0;
        const sway = Math.sin(now * 0.0005 + prog * Math.PI * 5) * 12;
        const dx   = Math.round(sway + freqVal * 40 * beat);
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
      .then((d: { track?: string }) => {
        setTrack(d.track ?? '—');
        setLive(true);
      })
      .catch(() => {});
  }, []);

  const toggle = useCallback(() => {
    if (!playerRef.current) return;
    if (onRef.current) {
      setOn(false);
      playerRef.current.pause();
      playerRef.current.src = '';
      playerRef.current.load();
      if (pollRef.current) clearInterval(pollRef.current);
      setTrack('—');
      setLive(false);
      setStatus('macka station');
    } else {
      setOn(true);
      setupAudio();
      if (audioCtx.current?.state === 'suspended') audioCtx.current.resume();
      const url = isLoRef.current ? STREAM_LO : STREAM_HI;
      playerRef.current.src = url;
      playerRef.current.play().catch(() => {});
      setStatus('connecting...');
      pollNow();
      pollRef.current = setInterval(pollNow, 5000);
    }
  }, [setupAudio, pollNow]);

  // audio element event listeners
  useEffect(() => {
    const el = document.createElement('audio');
    el.preload = 'none';
    el.crossOrigin = 'anonymous';
    playerRef.current = el;

    el.addEventListener('playing', () => setStatus('live'));
    el.addEventListener('stalled', () => setStatus('buffering...'));
    el.addEventListener('error',   () => {
      if (!onRef.current) return;
      setStatus('reconnecting...');
      setTimeout(() => {
        if (!onRef.current || !playerRef.current) return;
        playerRef.current.src = isLoRef.current ? STREAM_LO : STREAM_HI;
        playerRef.current.load();
        playerRef.current.play().catch(() => {});
      }, 2000);
    });

    return () => {
      el.pause();
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, []);

  return (
    <>
      <canvas ref={canvasRef} />
      <div id="ui">
        <div id="top">
          <button className="ctrl-btn" onClick={toggle}>
            {on ? 'pause' : 'play'}
          </button>
          <div style={{ display: 'flex', gap: '1.5rem', alignItems: 'center' }}>
            <button
              className="ctrl-btn"
              onClick={() => {
                const next = !isLoRef.current;
                setIsLo(next);
                if (onRef.current && playerRef.current) {
                  playerRef.current.src = next ? STREAM_LO : STREAM_HI;
                  playerRef.current.play().catch(() => {});
                }
              }}
              title="hi / lo quality"
              style={{ fontSize: '.7rem', letterSpacing: '.2em' }}
            >
              {isLo ? 'lo' : 'hi'}
            </button>
            <button
              className="ctrl-btn theme-btn"
              onClick={() => setIsLight(v => !v)}
              title="light / dark"
            >
              {isLight ? '●' : '○'}
            </button>
          </div>
        </div>
        <div id="bottom">
          <div id="track" className={live ? 'live' : ''}>{track}</div>
          <div id="status">{status}</div>
        </div>
      </div>
    </>
  );
}
