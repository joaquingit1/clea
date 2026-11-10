import { useEffect, useId, useState } from "react";
import { BLUE, MARK, WORD, WORD_RIGHT } from "../brand";
import { cx } from "../lib";

/** What the mark is doing, from the PDF's "In conversation" row: idle, listening (the waves come in toward the dot) and
 *  speaking (they leave it). "intro" draws it once, from the dot outward. */
export type MarkState = "idle" | "speaking" | "listening" | "intro";

/** Clea's mark, "the channel": a dot and two bands opened on the right by a channel one dot high. One ink: it takes the
 *  text color. Each piece is its own path so the waves can move. */
export function Mark({ size = 20, state = "idle", busy, className }: { size?: number; state?: MarkState; busy?: boolean; className?: string }) {
  const s = useSettled(busy ? "speaking" : state);
  return (
    <svg width={size} height={size} viewBox="-7 -7 14 14" fill="currentColor" className={cx("clea-mark shrink-0", className)} data-state={s} aria-hidden>
      <path className="clea-mark__outer" d={MARK.outer} />
      <path className="clea-mark__inner" d={MARK.inner} />
      <path className="clea-mark__dot" d={MARK.dot} />
    </svg>
  );
}

/** The mark and the drawn wordmark, as in the PDF's lockup. */
export function Lockup({ height = 18, state, busy, className }: { height?: number; state?: MarkState; busy?: boolean; className?: string }) {
  const s = useSettled(busy ? "speaking" : state ?? "idle");
  const w = WORD_RIGHT + 6.5;
  return (
    <svg height={height} width={(height * w) / 13} viewBox={`-6.5 -6.5 ${w} 13`} fill="currentColor" className={cx("clea-mark shrink-0", className)} data-state={s} aria-label="Clea">
      <path className="clea-mark__outer" d={MARK.outer} />
      <path className="clea-mark__inner" d={MARK.inner} />
      <path className="clea-mark__dot" d={MARK.dot} />
      <g className="clea-word">
        {Object.values(WORD).map((d) => (
          <path key={d} fillRule="evenodd" d={d} />
        ))}
      </g>
    </svg>
  );
}

/** The app icon as the UI draws it: the blue tile lit from the top left with a shaded lower edge and a glass rim, the
 *  white mark on a soft shadow, the dot glowing. A band of light crosses it now and then; while Clea works the glow
 *  breathes and the waves leave the dot. */
export function AppIcon({ size = 56, busy, intro, className }: { size?: number; busy?: boolean; intro?: boolean; className?: string }) {
  const id = useId().replace(/:/g, "");
  const settled = useSettled(busy ? "speaking" : intro ? "intro" : "idle");
  const u = 2.9; // the mark is 13u of a 64-wide tile, about 59%, like the Dock icon
  return (
    <span className={cx("clea-icon relative inline-block shrink-0", busy && "is-busy", className)} style={{ width: size, height: size }}>
      <span className="clea-icon__halo" aria-hidden />
      <svg width={size} height={size} viewBox="0 0 64 64" className="relative block" aria-label="Clea">
        <defs>
          <linearGradient id={`${id}b`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#4A5DFF" />
            <stop offset=".55" stopColor={BLUE} />
            <stop offset="1" stopColor="#1424C4" />
          </linearGradient>
          <radialGradient id={`${id}k`} cx=".22" cy="-.05" r="1">
            <stop offset="0" stopColor="#fff" stopOpacity=".42" />
            <stop offset=".45" stopColor="#fff" stopOpacity=".08" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </radialGradient>
          <radialGradient id={`${id}s`} cx=".78" cy="1.08" r=".75">
            <stop offset="0" stopColor="#050B5C" stopOpacity=".55" />
            <stop offset="1" stopColor="#050B5C" stopOpacity="0" />
          </radialGradient>
          <radialGradient id={`${id}g`} cx="32" cy="32" r="19" gradientUnits="userSpaceOnUse">
            <stop offset="0" stopColor="#B9C3FF" stopOpacity=".8" />
            <stop offset=".35" stopColor="#7D8CFF" stopOpacity=".3" />
            <stop offset="1" stopColor="#7D8CFF" stopOpacity="0" />
          </radialGradient>
          <linearGradient id={`${id}r`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#fff" stopOpacity=".6" />
            <stop offset=".3" stopColor="#fff" stopOpacity=".14" />
            <stop offset="1" stopColor="#fff" stopOpacity=".06" />
          </linearGradient>
          <linearGradient id={`${id}i`} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#fff" />
            <stop offset="1" stopColor="#DCE1FF" />
          </linearGradient>
          <linearGradient id={`${id}w`} x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stopColor="#fff" stopOpacity="0" />
            <stop offset=".5" stopColor="#fff" stopOpacity=".38" />
            <stop offset="1" stopColor="#fff" stopOpacity="0" />
          </linearGradient>
          <filter id={`${id}m`} x="-30%" y="-30%" width="160%" height="170%">
            <feDropShadow dx="0" dy=".7" stdDeviation=".8" floodColor="#0A1394" floodOpacity=".55" />
          </filter>
          <clipPath id={`${id}c`}>
            <path d={TILE} />
          </clipPath>
        </defs>
        <g clipPath={`url(#${id}c)`}>
          <rect width="64" height="64" fill={`url(#${id}b)`} />
          <rect width="64" height="64" fill={`url(#${id}s)`} />
          <rect className="clea-icon__glow" width="64" height="64" fill={`url(#${id}g)`} />
          <rect width="64" height="64" fill={`url(#${id}k)`} />
          <g transform={`translate(${32 + 0.09 * u} 32) scale(${u})`} fill={`url(#${id}i)`} filter={`url(#${id}m)`} className="clea-mark" data-state={settled}>
            <path className="clea-mark__outer" d={MARK.outer} />
            <path className="clea-mark__inner" d={MARK.inner} />
            <path className="clea-mark__dot" d={MARK.dot} />
          </g>
          <rect className="clea-icon__sheen" x="-40" y="-8" width="34" height="80" fill={`url(#${id}w)`} transform="rotate(18 32 32)" />
        </g>
        <path d={TILE} fill="none" stroke={`url(#${id}r)`} strokeWidth=".6" />
      </svg>
    </span>
  );
}

/** Apple's tile, a superellipse (n = 5), on a 64 grid with a 0.3 inset for the rim. */
const TILE = (() => {
  const pts: string[] = [];
  const n = 5, a = 31.7, steps = 96;
  for (let i = 0; i < steps; i++) {
    const t = (i / steps) * 2 * Math.PI, c = Math.cos(t), s = Math.sin(t);
    pts.push(`${(32 + a * Math.sign(c) * Math.abs(c) ** (2 / n)).toFixed(2)} ${(32 + a * Math.sign(s) * Math.abs(s) ** (2 / n)).toFixed(2)}`);
  }
  return `M${pts.join("L")}Z`;
})();

/** "intro" draws the mark once and then hands over to idle, so the mark keeps breathing after it arrives. */
function useSettled(s: MarkState): MarkState {
  const [done, setDone] = useState(false);
  useEffect(() => {
    if (s !== "intro" || done) return;
    const h = setTimeout(() => setDone(true), 1400);
    return () => clearTimeout(h);
  }, [s, done]);
  return s === "intro" && done ? "idle" : s;
}
