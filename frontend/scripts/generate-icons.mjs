// SPDX-License-Identifier: AGPL-3.0-or-later
/**
 * Generates the PWA icons into public/ as PNG files with no dependencies:
 * a rounded accent square carrying three rising bars (the "growth" mark).
 * Run `npm run gen:icons` and commit the result; the output is deterministic.
 *
 *   icon-192.png, icon-512.png   manifest "any" icons
 *   icon-maskable-512.png        full-bleed, mark inside the 80% safe zone
 *   apple-touch-icon.png (180)   iOS home screen (full-bleed; iOS rounds it)
 *   favicon-32.png               browser tab
 */
import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { deflateSync } from "node:zlib";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "public");
const ACCENT = [0x1f, 0x5f, 0xbf];
const WHITE = [0xff, 0xff, 0xff];
const SUPERSAMPLE = 4;

/** Coverage (0..1) at unit-square point (x, y) and the colour there, or null for transparent. */
function shade(x, y, { rounded, markScale }) {
  if (rounded) {
    const r = 0.22;
    const cx = Math.min(Math.max(x, r), 1 - r);
    const cy = Math.min(Math.max(y, r), 1 - r);
    if ((x - cx) ** 2 + (y - cy) ** 2 > r * r) {
      return null;
    }
  }
  // Mark box centred, scaled; bars rise left to right on a common baseline.
  const box = 0.5 * markScale;
  const left = 0.5 - box / 2;
  const base = 0.5 + box / 2;
  const barWidth = box / 5;
  const heights = [0.4, 0.7, 1];
  for (let index = 0; index < 3; index += 1) {
    const x0 = left + index * 2 * barWidth;
    const height = box * (heights[index] ?? 1);
    if (x >= x0 && x < x0 + barWidth && y <= base && y >= base - height) {
      return WHITE;
    }
  }
  return ACCENT;
}

function render(size, options) {
  const pixels = Buffer.alloc(size * size * 4);
  const samples = SUPERSAMPLE * SUPERSAMPLE;
  for (let py = 0; py < size; py += 1) {
    for (let px = 0; px < size; px += 1) {
      let r = 0;
      let g = 0;
      let b = 0;
      let a = 0;
      for (let sy = 0; sy < SUPERSAMPLE; sy += 1) {
        for (let sx = 0; sx < SUPERSAMPLE; sx += 1) {
          const colour = shade((px + (sx + 0.5) / SUPERSAMPLE) / size, (py + (sy + 0.5) / SUPERSAMPLE) / size, options);
          if (colour !== null) {
            r += colour[0];
            g += colour[1];
            b += colour[2];
            a += 1;
          }
        }
      }
      const offset = (py * size + px) * 4;
      if (a > 0) {
        pixels[offset] = Math.round(r / a);
        pixels[offset + 1] = Math.round(g / a);
        pixels[offset + 2] = Math.round(b / a);
      }
      pixels[offset + 3] = Math.round((255 * a) / samples);
    }
  }
  return pixels;
}

const CRC_TABLE = Array.from({ length: 256 }, (_, n) => {
  let c = n;
  for (let k = 0; k < 8; k += 1) {
    c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
  }
  return c >>> 0;
});

function crc32(buffer) {
  let c = 0xffffffff;
  for (const byte of buffer) {
    c = (CRC_TABLE[(c ^ byte) & 0xff] ?? 0) ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function chunk(type, data) {
  const body = Buffer.concat([Buffer.from(type, "ascii"), data]);
  const out = Buffer.alloc(body.length + 8);
  out.writeUInt32BE(data.length, 0);
  body.copy(out, 4);
  out.writeUInt32BE(crc32(body), body.length + 4);
  return out;
}

function encodePng(size, pixels) {
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header[8] = 8; // bit depth
  header[9] = 6; // RGBA
  const stride = size * 4;
  const raw = Buffer.alloc((stride + 1) * size);
  for (let row = 0; row < size; row += 1) {
    raw[row * (stride + 1)] = 0; // filter: none
    pixels.copy(raw, row * (stride + 1) + 1, row * stride, (row + 1) * stride);
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

const ICONS = [
  ["icon-192.png", 192, { rounded: true, markScale: 1 }],
  ["icon-512.png", 512, { rounded: true, markScale: 1 }],
  ["icon-maskable-512.png", 512, { rounded: false, markScale: 0.8 }],
  ["apple-touch-icon.png", 180, { rounded: false, markScale: 0.9 }],
  ["favicon-32.png", 32, { rounded: true, markScale: 1.1 }],
];

mkdirSync(OUT, { recursive: true });
for (const [name, size, options] of ICONS) {
  writeFileSync(join(OUT, name), encodePng(size, render(size, options)));
  console.log(`wrote public/${name} (${String(size)}x${String(size)})`);
}
