/* Minimal QR encoder — byte mode, ECC level L, versions 1-5 (single-block,
   so no interleaving needed). Enough for ~78 chars, which covers our
   "imicobo://pair?h=<host>&c=<code>" payload with room to spare.

   Self-contained on purpose: iMicobo must work on a router with no internet,
   so we can't pull a QR library from a CDN. */
const QR = (() => {

  // ---- GF(256) tables, primitive polynomial 0x11d ----
  const EXP = new Uint8Array(512), LOG = new Uint8Array(256);
  (() => { let x = 1;
    for (let i = 0; i < 255; i++) { EXP[i] = x; LOG[x] = i; x <<= 1; if (x & 0x100) x ^= 0x11d; }
    for (let i = 255; i < 512; i++) EXP[i] = EXP[i - 255];
  })();
  const mul = (a, b) => (a === 0 || b === 0) ? 0 : EXP[LOG[a] + LOG[b]];

  // total codewords / data codewords per version at ECC level L (all 1 block)
  const CAP = { 1:[26,19], 2:[44,34], 3:[70,55], 4:[100,80], 5:[134,108] };
  const ALIGN = { 2:18, 3:22, 4:26, 5:30 };   // single alignment centre

  function genPoly(n) {
    let g = [1];
    for (let i = 0; i < n; i++) {
      const next = new Array(g.length + 1).fill(0);
      for (let j = 0; j < g.length; j++) {
        next[j]     ^= mul(g[j], 1);
        next[j + 1] ^= mul(g[j], EXP[i]);
      }
      g = next;
    }
    return g;
  }

  function ecc(data, n) {
    const g = genPoly(n);
    const rem = data.concat(new Array(n).fill(0));
    for (let i = 0; i < data.length; i++) {
      const c = rem[i];
      if (!c) continue;
      for (let j = 1; j <= n; j++) rem[i + j] ^= mul(g[j], c);
    }
    return rem.slice(data.length);
  }

  function encode(text) {
    const bytes = Array.from(new TextEncoder().encode(text));

    let version = 0;
    for (let v = 1; v <= 5; v++) {
      const dataCw = CAP[v][1];
      if (bytes.length + 2 <= dataCw) { version = v; break; }  // +2 ≈ header
    }
    if (!version) throw new Error("payload too long for QR v1-5");

    const [total, dataCw] = CAP[version];
    const eccCw = total - dataCw;

    // --- bitstream: mode(0100) + length(8) + data + terminator + padding ---
    const bits = [];
    const push = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1); };
    push(0b0100, 4);
    push(bytes.length, 8);
    bytes.forEach(b => push(b, 8));
    for (let i = 0; i < 4 && bits.length < dataCw * 8; i++) bits.push(0);
    while (bits.length % 8) bits.push(0);

    const codewords = [];
    for (let i = 0; i < bits.length; i += 8) {
      let b = 0; for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
      codewords.push(b);
    }
    let pad = 0;
    while (codewords.length < dataCw) codewords.push(pad++ % 2 === 0 ? 0xEC : 0x11);

    const all = codewords.concat(ecc(codewords, eccCw));

    // --- matrix ---
    const size = 17 + 4 * version;
    const m = Array.from({ length: size }, () => new Array(size).fill(null));
    const res = Array.from({ length: size }, () => new Array(size).fill(false));
    const set = (r, c, v) => { if (r >= 0 && r < size && c >= 0 && c < size) { m[r][c] = v; res[r][c] = true; } };

    const finder = (r, c) => {
      for (let i = -1; i <= 7; i++) for (let j = -1; j <= 7; j++) {
        const on = i >= 0 && i <= 6 && j >= 0 && j <= 6 &&
          (i === 0 || i === 6 || j === 0 || j === 6 || (i >= 2 && i <= 4 && j >= 2 && j <= 4));
        set(r + i, c + j, on ? 1 : 0);
      }
    };
    finder(0, 0); finder(size - 7, 0); finder(0, size - 7);

    for (let i = 8; i < size - 8; i++) {
      const v = i % 2 === 0 ? 1 : 0;
      set(6, i, v); set(i, 6, v);
    }

    if (version >= 2) {
      const k = ALIGN[version];
      for (let i = -2; i <= 2; i++) for (let j = -2; j <= 2; j++)
        set(k + i, k + j, Math.max(Math.abs(i), Math.abs(j)) !== 1 ? 1 : 0);
    }

    set(4 * version + 9, 8, 1);                       // dark module

    for (let i = 0; i <= 8; i++) { res[8][i] = true; res[i][8] = true; }
    for (let i = 0; i < 8; i++) { res[8][size - 1 - i] = true; res[size - 1 - i][8] = true; }

    // --- data placement (zigzag) ---
    const dataBits = [];
    all.forEach(b => { for (let i = 7; i >= 0; i--) dataBits.push((b >> i) & 1); });
    let idx = 0, up = true;
    for (let col = size - 1; col > 0; col -= 2) {
      if (col === 6) col--;
      for (let i = 0; i < size; i++) {
        const row = up ? size - 1 - i : i;
        for (let k = 0; k < 2; k++) {
          const c = col - k;
          if (!res[row][c]) m[row][c] = idx < dataBits.length ? dataBits[idx++] : 0;
        }
      }
      up = !up;
    }

    // --- masking ---
    const MASKS = [
      (i, j) => (i + j) % 2 === 0,
      (i, j) => i % 2 === 0,
      (i, j) => j % 3 === 0,
      (i, j) => (i + j) % 3 === 0,
      (i, j) => (Math.floor(i / 2) + Math.floor(j / 3)) % 2 === 0,
      (i, j) => (i * j) % 2 + (i * j) % 3 === 0,
      (i, j) => ((i * j) % 2 + (i * j) % 3) % 2 === 0,
      (i, j) => ((i + j) % 2 + (i * j) % 3) % 2 === 0,
    ];

    const penalty = (g) => {
      let p = 0;
      const runs = (get) => {
        for (let a = 0; a < size; a++) {
          let run = 1;
          for (let b = 1; b < size; b++) {
            if (get(a, b) === get(a, b - 1)) run++;
            else { if (run >= 5) p += 3 + (run - 5); run = 1; }
          }
          if (run >= 5) p += 3 + (run - 5);
        }
      };
      runs((a, b) => g[a][b]); runs((a, b) => g[b][a]);
      for (let i = 0; i < size - 1; i++) for (let j = 0; j < size - 1; j++) {
        const v = g[i][j];
        if (v === g[i][j + 1] && v === g[i + 1][j] && v === g[i + 1][j + 1]) p += 3;
      }
      let dark = 0;
      for (let i = 0; i < size; i++) for (let j = 0; j < size; j++) dark += g[i][j];
      const pct = dark * 100 / (size * size);
      p += 10 * Math.floor(Math.abs(pct - 50) / 5);
      return p;
    };

    let best = null, bestP = Infinity, bestMask = 0;
    for (let mk = 0; mk < 8; mk++) {
      const g = m.map(r => r.slice());
      for (let i = 0; i < size; i++) for (let j = 0; j < size; j++)
        if (!res[i][j] && MASKS[mk](i, j)) g[i][j] ^= 1;

      // format info: ECC L = 01, then mask; BCH(15,5) + XOR 0x5412
      const fdata = (0b01 << 3) | mk;
      let d = fdata << 10;
      for (let i = 14; i >= 10; i--) if ((d >> i) & 1) d ^= 0x537 << (i - 10);
      const fmt = ((fdata << 10) | (d & 0x3ff)) ^ 0x5412;

      for (let i = 0; i < 15; i++) {
        const bit = (fmt >> (14 - i)) & 1;      // format info is MSB-first
        if (i < 6)       g[8][i] = bit;
        else if (i === 6) g[8][7] = bit;
        else if (i === 7) g[8][8] = bit;
        else if (i === 8) g[7][8] = bit;
        else              g[14 - i][8] = bit;

        if (i < 7)  g[size - 1 - i][8] = bit;      // bits 0-6 → rows size-1 … size-7
        else        g[8][size - 15 + i] = bit;     // bits 7-14 → cols size-8 … size-1
      }

      const p = penalty(g);
      if (p < bestP) { bestP = p; best = g; bestMask = mk; }
    }
    return best;
  }

  /** Render to an SVG string. */
  function svg(text, px = 220, quiet = 4) {
    const m = encode(text);
    const size = m.length;
    const dim = size + quiet * 2;
    let path = "";
    for (let i = 0; i < size; i++)
      for (let j = 0; j < size; j++)
        if (m[i][j]) path += `M${j + quiet} ${i + quiet}h1v1h-1z`;
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 ${dim} ${dim}" shape-rendering="crispEdges">`
         + `<rect width="${dim}" height="${dim}" fill="#fff"/>`
         + `<path d="${path}" fill="#000"/></svg>`;
  }

  return { svg, encode };
})();
