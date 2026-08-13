import { useId } from 'react';
import { NORD } from './chartTheme';

interface Props {
  values: number[];
  color?: string;
  height?: number;
}

/**
 * Deliberately plain SVG: one of these sits on every endpoint card, so a full
 * recharts instance per card would cost far more than the trend is worth.
 */
export function Sparkline({ values, color = NORD.cyan, height = 22 }: Props) {
  const gradientId = useId();

  if (values.length < 2) return <div style={{ height }} />;

  const peak = Math.max(...values, 1);
  const width = 100;
  const step = width / (values.length - 1);
  const y = (v: number) => height - (v / peak) * (height - 2) - 1;
  const line = values.map((v, i) => `${(i * step).toFixed(2)},${y(v).toFixed(2)}`).join(' ');

  return (
    <svg
      width="100%"
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      preserveAspectRatio="none"
      aria-hidden
      className="block"
    >
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity={0.35} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      <polygon points={`0,${height} ${line} ${width},${height}`} fill={`url(#${gradientId})`} />
      <polyline
        points={line}
        fill="none"
        stroke={color}
        strokeWidth={1.25}
        strokeLinejoin="round"
        vectorEffect="non-scaling-stroke"
      />
    </svg>
  );
}
