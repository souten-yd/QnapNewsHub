import { useEffect, useRef, useState } from "react";
import type { EChartsOption } from "./echarts";
import { chartColors, useIsDark } from "./theme";

/** ECharts を遅延読み込みして描く。親の幅に合わせて作り直さず resize だけ。表示の説明（aria-label）を必ず付ける。 */
export function Chart({ option, height, label, testId }: { option: EChartsOption; height: number; label: string; testId?: string }) {
  const el = useRef<HTMLDivElement>(null);
  const inst = useRef<{ setOption: (o: EChartsOption, notMerge?: boolean) => void; resize: () => void; dispose: () => void } | null>(null);
  const [ready, setReady] = useState(false);
  const dark = useIsDark();
  const latest = useRef({ option, dark });
  latest.current = { option, dark };

  const paint = () => {
    const c = chartColors(latest.current.dark);
    inst.current?.setOption({
      backgroundColor: c.bg, textStyle: { color: c.text, fontSize: 12 }, animation: !matchMedia("(prefers-reduced-motion: reduce)").matches,
      tooltip: { confine: true, backgroundColor: c.tip, borderColor: c.axis, textStyle: { color: c.text, fontSize: 12 } },
      ...latest.current.option,
    }, true);
  };
  useEffect(() => {
    let dead = false;
    let ro: ResizeObserver | null = null;
    void import("./echarts").then((m) => {
      if (dead || !el.current) return;
      inst.current = m.default.init(el.current, undefined, { renderer: "canvas" });
      paint();
      setReady(true);
      ro = new ResizeObserver(() => inst.current?.resize());
      ro.observe(el.current);
    });
    return () => { dead = true; ro?.disconnect(); inst.current?.dispose(); inst.current = null; setReady(false); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => { if (inst.current) paint(); }, [option, dark, ready]); // eslint-disable-line react-hooks/exhaustive-deps
  return <div ref={el} role="img" aria-label={label} data-testid={testId} data-ready={ready} style={{ width: "100%", height, minWidth: 0 }} />;
}
