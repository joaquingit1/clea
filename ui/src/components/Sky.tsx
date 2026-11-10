import { useEffect, useRef, useState } from "react";

/** The main panel's sky, built the way Craft builds its onboarding background (CraftCloudyBackgroundView, read from
 *  Craft.app's arm64 code, 2026-10-23): a vertical gradient, a star field that only shows in the dark, and two halftone
 *  clouds drifting right at 18 px/s that wrap round when they leave. The clouds are Clea's own, drawn here once as a dot
 *  grid over a soft silhouette (Craft ships a bitmap; the dots also echo the mark's dot). The star field is Craft's
 *  algorithm as is: 340 stars from a day-seeded mulberry32, twinkle, a bright band sweeping across, a shooting star every
 *  5 to 15 s. Hidden (case and PR screens) it fades out and stops drawing. */
export function Sky({ hidden }: { hidden: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  const stars = useRef<HTMLCanvasElement>(null);
  const [w, setW] = useState(0);
  const [h, setH] = useState(0);
  const dark = useDark();
  const [clouds, setClouds] = useState<[string, string] | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const ro = new ResizeObserver(([e]) => {
      setW(Math.round(e.contentRect.width));
      setH(Math.round(e.contentRect.height));
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // the two cloud bitmaps, drawn once per theme (two different shapes; Craft repeats one)
  useEffect(() => {
    let alive = true;
    const run = () => alive && setClouds([drawCloud(7, dark), drawCloud(23, dark)]);
    const idle = (window as unknown as { requestIdleCallback?: (f: () => void) => number }).requestIdleCallback;
    if (idle) idle(run);
    else setTimeout(run, 0);
    return () => {
      alive = false;
    };
  }, [dark]);

  useStarField(stars, dark && !hidden, w, h);

  // Craft's layout: scale clamp(width / 720, 0.8, 1.5); cloud 1 two thirds off the left edge at 33% of the height, cloud 2
  // flush right at 66%; both move right and come back in from the left
  const k = Math.min(Math.max(w / 720, 0.8), 1.5);
  const cw = CLOUD.w * k, ch = CLOUD.h * k;
  const run = w + cw; // a full lap: from just off the left edge to just off the right one
  const lap = run / SPEED;
  const at = (x: number) => -((x + cw) / run) * lap; // a negative delay starts the lap at x
  return (
    <div ref={box} aria-hidden className="sky pointer-events-none absolute inset-0 overflow-hidden" data-hidden={hidden || undefined}>
      <div className="sky__gradient absolute inset-0" />
      <canvas ref={stars} className="sky__stars absolute inset-0 h-full w-full" />
      {clouds && w
        ? [
            { src: clouds[0], x: -cw * 0.66, y: h * 0.33 - ch / 2 },
            { src: clouds[1], x: w - cw, y: h * 0.66 - ch / 2 },
          ].map((c, i) => (
            <img
              key={i}
              src={c.src}
              alt=""
              className="sky__cloud absolute top-0 left-0"
              style={{
                width: cw,
                height: ch,
                top: Math.round(c.y),
                left: Math.round(-cw),
                ["--lap" as string]: `${run}px`,
                animationDuration: `${lap}s`,
                animationDelay: `${at(c.x)}s`,
              }}
            />
          ))
        : null}
    </div>
  );
}

const SPEED = 18; // px/s: Craft adds 0.3 pt per display-link frame, 18 pt/s at 60 Hz
const CLOUD = { w: 386, h: 198.5 }; // Craft's cloud size in points

function useDark(): boolean {
  const read = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  };
  const [d, setD] = useState(read);
  useEffect(() => {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const on = () => setD(read());
    mq.addEventListener("change", on);
    const mo = new MutationObserver(on);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    return () => {
      mq.removeEventListener("change", on);
      mo.disconnect();
    };
  }, []);
  return d;
}

function mulberry32(seed: number) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** A halftone cumulus: a flat row of puffs with a crown of bigger ones (each puff a soft disc, the silhouette mostly their
 *  union so it stays bumpy), a little value noise on the rim. The body is white lit from the top left and cool in the
 *  shade; a dot grid runs through it and is all that is left toward the edges, the way Craft's cloud feathers out.
 *  Returns a PNG data URL at up to 1.5x Craft's size. */
function drawCloud(seed: number, dark: boolean): string {
  const rnd = mulberry32(seed);
  const scale = Math.min(window.devicePixelRatio || 1, 2) * 1.5;
  const W = Math.round(CLOUD.w * scale), H = Math.round(CLOUD.h * scale);
  const c = document.createElement("canvas");
  c.width = W;
  c.height = H;
  const g = c.getContext("2d")!;
  const base = 0.7;
  const P: [number, number, number][] = [];
  for (let i = 0; i < 6; i++) P.push([0.14 + (i / 5) * 0.72 + (rnd() - 0.5) * 0.03, base - 0.02 + (rnd() - 0.5) * 0.04, 0.075 + rnd() * 0.03]);
  for (let i = 0; i < 4; i++) {
    const t = (i + 0.5) / 4, crown = Math.sin(t * Math.PI);
    P.push([0.2 + t * 0.6 + (rnd() - 0.5) * 0.05, base - 0.16 - crown * (0.1 + rnd() * 0.08), 0.1 + crown * (0.05 + rnd() * 0.035)]);
  }
  P.push([0.5 + (rnd() - 0.5) * 0.1, base - 0.34 - rnd() * 0.06, 0.11 + rnd() * 0.03]);
  const noise = valueNoise(rnd);
  const asp = H / W;
  const field = (x: number, y: number) => {
    let m = 0, sum = 0;
    for (const [bx, by, br] of P) {
      const dx = x - bx, dy = (y - by) * asp;
      const v = Math.exp(-(dx * dx + dy * dy) / (br * br));
      m = Math.max(m, v);
      sum += v;
    }
    let f = 0.75 * m + (0.25 * Math.min(sum, 1.6)) / 1.6;
    if (y > base + 0.06) f *= Math.max(0, 1 - (y - base - 0.06) * 9); // a flat underside
    return f + (noise(x * 14, y * 8) - 0.5) * 0.08;
  };
  const sm = (a: number, b: number, v: number) => {
    const t = Math.min(Math.max((v - a) / (b - a), 0), 1);
    return t * t * (3 - 2 * t);
  };
  const pitch = 3.5 * scale; // Craft's grid: a dot every 3.5 pt
  const img = g.createImageData(W, H);
  const d = img.data;
  for (let y = 0; y < H; y++) {
    for (let x = 0; x < W; x++) {
      const f = field(x / W, y / H);
      if (f < 0.08) continue;
      const fill = sm(0.42, 0.8, f), halo = sm(0.12, 0.5, f);
      const gx = (x % pitch) - pitch / 2, gy = (y % pitch) - pitch / 2;
      const r = pitch * (0.18 + 0.14 * halo);
      const dot = 1 - sm(r - 0.8, r + 0.8, Math.hypot(gx, gy));
      const i = (y * W + x) * 4;
      if (dark) {
        // white dots and a faint white body; the layer is soft-light blended at 0.3 like Craft's
        d[i] = d[i + 1] = d[i + 2] = 255;
        d[i + 3] = 255 * Math.min(1, fill * 0.3 + dot * halo * 0.55);
        continue;
      }
      const light = Math.min(1, Math.max(0, 1.15 - (y / H) * 1.1 - (x / W - 0.3) * 0.25));
      const k = dot * (0.16 + 0.2 * (1 - fill)); // dots darker than the body under them, clearer at the rim
      d[i] = 252 - 46 * (1 - light) - k * 120;
      d[i + 1] = 253 - 36 * (1 - light) - k * 95;
      d[i + 2] = 255 - 12 * (1 - light) - k * 40;
      d[i + 3] = 255 * Math.min(1, fill * 0.92 + dot * halo * 0.6);
    }
  }
  g.putImageData(img, 0, 0);
  return c.toDataURL("image/png");
}

/** Smooth 2D value noise on a 16x16 lattice. */
function valueNoise(rnd: () => number) {
  const N = 16, v = Array.from({ length: N * N }, rnd);
  const at = (x: number, y: number) => v[(((y % N) + N) % N) * N + (((x % N) + N) % N)];
  const s = (t: number) => t * t * (3 - 2 * t);
  return (x: number, y: number) => {
    const xi = Math.floor(x), yi = Math.floor(y), xf = s(x - xi), yf = s(y - yi);
    const a = at(xi, yi), b = at(xi + 1, yi), c = at(xi, yi + 1), e = at(xi + 1, yi + 1);
    return a + (b - a) * xf + (c - a) * yf + (a - b - c + e) * xf * yf;
  };
}

interface Star { x: number; y: number; r: number; speed: number; phase: number; base: number; dx: number; dy: number }
interface Shooting { x: number; y: number; angle: number; speed: number; length: number; life: number; max: number }

/** Craft's star field, constant for constant, drawn on a canvas while the sky shows in the dark. */
function useStarField(ref: React.RefObject<HTMLCanvasElement | null>, on: boolean, w: number, h: number) {
  useEffect(() => {
    const cv = ref.current;
    if (!cv || !on || !w || !h) {
      cv?.getContext("2d")?.clearRect(0, 0, cv.width, cv.height);
      return;
    }
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    cv.width = w * dpr;
    cv.height = h * dpr;
    const g = cv.getContext("2d")!;
    g.scale(dpr, dpr);
    const rnd = mulberry32(Math.floor((Date.now() / 1000 - 978307200) / 86400)); // the sky changes once a day
    const stars: Star[] = Array.from({ length: 340 }, (_, i) => ({
      x: rnd(),
      y: rnd(),
      r: i % 2 === 0 ? 2.1 : 2.6,
      speed: 0.4 + rnd() * 1.4,
      phase: rnd() * 2 * Math.PI,
      base: 0.1 + rnd() * 0.2,
      dx: -0.0003 + rnd() * 0.0006,
      dy: -0.00015 + rnd() * 0.0003,
    }));
    const shooting: Shooting[] = [];
    let next = rnd() * 6 + 4;
    let elapsed = 0;
    let last: number | null = null;
    let raf = 0;
    const area = (w * h) / 504000;
    const count = Math.max(1, Math.min(area >= 0.1 ? Math.floor(Math.min(area ** 1.25, 1) * 340) : 34, 340));
    const density = area >= 0.45 ? Math.min(area, 1) : 0.45;
    const wrap = (v: number) => v - Math.floor(v);
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    const frame = (now: number) => {
      raf = still ? 0 : requestAnimationFrame(frame);
      if (document.documentElement.classList.contains("away")) {
        last = null; // Clea in the background: hold the sky
        return;
      }
      const dt = last == null ? 0 : Math.min((now - last) / 1000, 0.1);
      last = now;
      elapsed += dt;
      for (const s of stars) {
        s.x = wrap(s.x + dt * s.dx);
        s.y = wrap(s.y + dt * s.dy);
      }
      next -= dt;
      if (next <= 0) {
        const max = 1.2 + rnd() * 1.3;
        shooting.push({ x: 0.1 + rnd() * 0.8, y: 0.05 + rnd() * 0.35, angle: 0.1 * Math.PI + rnd() * 0.3 * Math.PI, speed: 0.15 + rnd() * 0.2, length: 0.06 + rnd() * 0.06, life: max, max });
        next = rnd() * 10 + 5;
      }
      for (const s of shooting) {
        s.x += Math.cos(s.angle) * s.speed * dt;
        s.y += Math.sin(s.angle) * s.speed * dt;
        s.life -= dt;
      }
      for (let i = shooting.length - 1; i >= 0; i--) if (shooting[i].life <= 0) shooting.splice(i, 1);

      g.clearRect(0, 0, w, h);
      g.globalCompositeOperation = "screen";
      const band = (Math.sin(elapsed * 0.15) + 1) * 0.5;
      for (let i = 0; i < count; i++) {
        const s = stars[i];
        const tw = Math.sin(s.speed * elapsed + s.phase) * 0.5 + 0.5;
        let a = density * (Math.max(0, 1 - Math.abs(s.x - band) * 2.5) * 0.3 + s.base * (tw * 0.7 + 0.3));
        if (s.y > 0.65 && s.x < 0.35) a *= 0.15; // dim behind the lower-left cloud
        const d = s.r * (tw * 0.2 + 0.35) * 2;
        g.fillStyle = `rgba(255,255,255,${Math.min(a, 1)})`;
        g.beginPath();
        g.arc(w * s.x, h * s.y, d / 2, 0, 2 * Math.PI);
        g.fill();
      }
      for (const s of shooting) {
        const t = 1 - s.life / s.max;
        const env = Math.max(t < 0.2 ? t / 0.2 : (1 - t) / 0.8, 0);
        const hx = w * s.x, hy = h * s.y;
        const tx = hx - w * Math.cos(s.angle) * s.length, ty = hy - h * Math.sin(s.angle) * s.length;
        const lg = g.createLinearGradient(tx, ty, hx, hy);
        lg.addColorStop(0, "rgba(255,255,255,0)");
        lg.addColorStop(1, `rgba(255,255,255,${env * 0.8})`);
        g.strokeStyle = lg;
        g.lineWidth = 1.5;
        g.beginPath();
        g.moveTo(tx, ty);
        g.lineTo(hx, hy);
        g.stroke();
      }
    };
    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, [ref, on, w, h]);
}
