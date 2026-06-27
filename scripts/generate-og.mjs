import sharp from 'sharp';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.join(__dirname, '../public/og.png');

const W = 1200, H = 630;

// Build wave-line paths: horizontal stripes with sine displacement
function waveLines() {
  const lines = [];
  const lineH = 3, lineGap = 2;
  for (let y = 0; y < H; y += lineH + lineGap) {
    const prog = y / H;
    const hue  = Math.round(320 + Math.sin(prog * Math.PI * 2) * 40);
    const a    = (0.18 + Math.sin(prog * Math.PI * 6) * 0.06).toFixed(3);
    const pts  = [];
    for (let x = 0; x <= W; x += 10) {
      const wy = Math.sin(x / W * 6 + prog * 5.5) * 5
               + Math.sin(x / W * 13.5 + prog * 9) * 2.5;
      pts.push(`${x === 0 ? 'M' : 'L'}${x},${(y + wy).toFixed(2)}`);
    }
    lines.push(`<path d="${pts.join(' ')}" stroke="hsla(${hue},72%,62%,${a})" stroke-width="${lineH}" fill="none"/>`);
  }
  return lines.join('\n');
}

// Build scan-line text slices: slice the text path into horizontal strips
function textSlices() {
  const fsize   = 280;
  const textTop = H / 2 - fsize * 0.95;
  const lineH   = 3, lineGap = 2;
  const slices  = [];
  for (let y = textTop; y < textTop + fsize * 2; y += lineH + lineGap) {
    const prog = (y - textTop) / (fsize * 2);
    const sway = Math.sin(prog * Math.PI * 5) * 8;
    slices.push(
      `<use href="#txt" clip-path="url(#clip_${slices.length})" transform="translate(${sway.toFixed(2)},0)"/>`,
    );
  }

  const clipDefs = [];
  let   idx = 0;
  for (let y = textTop; y < textTop + fsize * 2; y += lineH + lineGap) {
    clipDefs.push(
      `<clipPath id="clip_${idx++}"><rect x="-10" y="${y.toFixed(2)}" width="${W + 20}" height="${lineH}"/></clipPath>`
    );
  }

  return { slices: slices.join('\n'), clipDefs: clipDefs.join('\n') };
}

const { slices, clipDefs } = textSlices();
const fsize = 280;
const textTop = H / 2 - fsize * 0.95;
const y1 = textTop + fsize * 0.82;
const y2 = textTop + fsize * 1.82;

const svg = `<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">
<defs>
  <!-- background gradient -->
  <radialGradient id="bg" cx="50%" cy="50%" r="70%">
    <stop offset="0%"   stop-color="rgb(10,30,90)" stop-opacity="0.95"/>
    <stop offset="100%" stop-color="rgb(3,7,15)"   stop-opacity="0"/>
  </radialGradient>
  <!-- text gradient -->
  <linearGradient id="tg" x1="0" y1="${y1 - fsize * 0.8}" x2="0" y2="${y2}" gradientUnits="userSpaceOnUse">
    <stop offset="0%"   stop-color="#b8dcfc"/>
    <stop offset="50%"  stop-color="#6eaaee"/>
    <stop offset="100%" stop-color="#3a6ad4"/>
  </linearGradient>
  ${clipDefs}
</defs>

<!-- base dark fill -->
<rect width="${W}" height="${H}" fill="#03070f"/>
<!-- glow -->
<rect width="${W}" height="${H}" fill="url(#bg)"/>

<!-- wave lines -->
${waveLines()}

<!-- ghost text for clipping reference -->
<g id="txt" font-family="Impact, Arial Black" font-weight="900" font-size="${fsize}" text-anchor="middle" fill="url(#tg)">
  <text x="${W / 2}" y="${y1.toFixed(2)}">Macka</text>
  <text x="${W / 2}" y="${y2.toFixed(2)}">Funk</text>
</g>

<!-- scan-line sliced text -->
${slices}

<!-- label -->
<text x="${W / 2}" y="${H - 36}" font-family="'Courier New', monospace" font-weight="700" font-size="20"
      text-anchor="middle" fill="rgba(168,212,248,0.45)" letter-spacing="4">macka.agtc.app</text>
</svg>`;

fs.mkdirSync(path.dirname(OUT), { recursive: true });
await sharp(Buffer.from(svg)).png().toFile(OUT);
console.log('✓ og.png generated →', OUT);
