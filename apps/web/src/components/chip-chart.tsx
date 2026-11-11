"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { IChartApi, ISeriesApi, LogicalRange, MouseEventParams, Time } from "lightweight-charts";
import { chartTimeKey, displayPrice } from "../lib/chart-data";
import { toChipChartData, type ChipSeries } from "../lib/chip-chart";
import styles from "./chip-chart.module.css";

export type { ChipDatum, ChipSeries } from "../lib/chip-chart";

type Library = typeof import("lightweight-charts");
type Renderer = { chart: IChartApi; library: Library; series: ISeriesApi<"Line" | "Histogram">[]; fitted: boolean; extent: string };

function loadViewport(key: string): LogicalRange | null {
  try {
    const saved: unknown = JSON.parse(sessionStorage.getItem(key) ?? "null");
    if (saved && typeof saved === "object" && "from" in saved && "to" in saved &&
        typeof saved.from === "number" && typeof saved.to === "number" &&
        Number.isFinite(saved.from) && Number.isFinite(saved.to) && saved.from < saved.to &&
        Math.abs(saved.from) < 100000 && Math.abs(saved.to) < 100000) {
      return saved as LogicalRange;
    }
  } catch { /* Storage may be unavailable; the live chart still keeps its range. */ }
  return null;
}

export function ChipChart({ chartId, title, unit, series, frequency }: {
  chartId: string;
  title: string;
  unit: string;
  series: readonly ChipSeries[];
  frequency: "1d" | "1w";
}) {
  const container = useRef<HTMLDivElement>(null);
  const renderer = useRef<Renderer | null>(null);
  const converted = useMemo(() => {
    try { return toChipChartData(series); } catch { return null; }
  }, [series]);
  const dataRef = useRef(converted);
  dataRef.current = converted;
  const [ready, setReady] = useState(false);
  const [failed, setFailed] = useState(false);
  const [hovered, setHovered] = useState<string | null>(null);
  const hasValues = converted?.some((item) => item.segments.length > 0) ?? false;
  const dates = converted?.flatMap((item) => [...item.exactByDate.keys()]).sort() ?? [];
  const latestDate = dates.at(-1) ?? null;
  const earliestDate = dates[0] ?? null;
  const extent = `${earliestDate ?? ""}/${latestDate ?? ""}`;
  const cacheKey = `stockapp:chip-viewport:${chartId}:${frequency}:${extent}`;
  const cacheRef = useRef(cacheKey);
  cacheRef.current = cacheKey;
  const selectedDate = hovered ?? latestDate;

  useEffect(() => {
    const element = container.current;
    if (!element || !hasValues || !converted) return;
    let cancelled = false;
    let destroy: (() => void) | undefined;
    setReady(false);
    setFailed(false);
    void import("lightweight-charts").then((library) => {
      if (cancelled) return;
      const chart = library.createChart(element, {
        width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight),
        layout: { background: { type: library.ColorType.Solid, color: "#111923" }, textColor: "#8c9bad", fontSize: 11, attributionLogo: true },
        grid: { vertLines: { color: "#1d2938" }, horzLines: { color: "#1d2938" } },
        crosshair: { mode: library.CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#283547", scaleMargins: { top: .12, bottom: .1 } },
        timeScale: { borderColor: "#283547", timeVisible: false, rightOffset: 2, barSpacing: 14 },
        handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
        handleScale: { mouseWheel: true, pinch: true, axisPressedMouseMove: true },
        localization: { locale: "zh-TW", dateFormat: "yyyy/MM/dd" },
      });
      const instance: Renderer = { chart, library, series: [], fitted: false, extent: "" };
      renderer.current = instance;
      const onRange = (range: LogicalRange | null) => {
        if (!range) return;
        try { sessionStorage.setItem(cacheRef.current, JSON.stringify(range)); } catch { /* Optional persistence. */ }
      };
      const onHover = (event: MouseEventParams<Time>) => {
        const time = event.time !== undefined ? chartTimeKey(event.time) : "";
        const inside = event.point && event.point.x >= 0 && event.point.y >= 0 &&
          event.point.x <= element.clientWidth && event.point.y <= element.clientHeight;
        setHovered(inside && dataRef.current?.some((item) => item.exactByDate.has(time)) ? time : null);
      };
      chart.subscribeCrosshairMove(onHover);
      chart.timeScale().subscribeVisibleLogicalRangeChange(onRange);
      const resize = new ResizeObserver((entries) => {
        const size = entries[0]?.contentRect;
        // Hidden visited tabs have zero dimensions. Keep the live range until
        // ResizeObserver reports the tab's real size again.
        if (size && size.width > 0 && size.height > 0) chart.resize(size.width, size.height);
      });
      resize.observe(element);
      destroy = () => {
        onRange(chart.timeScale().getVisibleLogicalRange());
        resize.disconnect();
        chart.unsubscribeCrosshairMove(onHover);
        chart.timeScale().unsubscribeVisibleLogicalRangeChange(onRange);
        chart.remove();
        if (renderer.current === instance) renderer.current = null;
      };
      setReady(true);
    }).catch(() => { if (!cancelled) setFailed(true); });
    return () => { cancelled = true; destroy?.(); };
    // Data changes update the existing instance below. They do not reset its
    // zoom or pan. A genuinely empty/error data set releases its chart.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartId, frequency, hasValues]);

  useEffect(() => {
    const instance = renderer.current;
    if (!ready || !instance || !converted) return;
    try {
      const { chart, library } = instance;
      const unchangedExtent = instance.extent === extent;
      const cachedRange = loadViewport(cacheKey);
      const previousRange = instance.fitted && unchangedExtent ? chart.timeScale().getVisibleLogicalRange() : cachedRange;
      const previousTime = instance.fitted && !unchangedExtent ? chart.timeScale().getVisibleRange() : null;
      for (const old of instance.series) chart.removeSeries(old);
      instance.series = [];
      for (const item of converted) {
        if (item.source.kind === "histogram") {
          const histogram = chart.addSeries(library.HistogramSeries, { title: item.source.label,
            color: item.source.color, priceLineVisible: false, lastValueVisible: false,
            priceFormat: { type: "price", precision: item.precision, minMove: 10 ** -item.precision } });
          histogram.setData(item.data);
          instance.series.push(histogram);
        } else {
          for (const segment of item.segments) {
            const line = chart.addSeries(library.LineSeries, { title: item.source.label,
              color: item.source.color, lineWidth: 2, pointMarkersVisible: frequency === "1w" || segment.length === 1,
              priceLineVisible: false, lastValueVisible: false,
              priceFormat: { type: "price", precision: item.precision, minMove: 10 ** -item.precision } });
            line.setData(segment);
            instance.series.push(line);
          }
          // A whitespace-only companion retains explicit missing dates on the
          // shared time axis without connecting the separated valid segments.
          const gaps = item.data.filter((point) => !("value" in point));
          if (gaps.length) {
            const whitespace = chart.addSeries(library.LineSeries, { color: item.source.color,
              priceLineVisible: false, lastValueVisible: false });
            whitespace.setData(gaps);
            instance.series.push(whitespace);
          }
        }
      }
      if (previousRange) chart.timeScale().setVisibleLogicalRange(previousRange);
      else if (previousTime && earliestDate && latestDate) {
        const from = chartTimeKey(previousTime.from);
        const to = chartTimeKey(previousTime.to);
        const overlapFrom = from > earliestDate ? from : earliestDate;
        const overlapTo = to < latestDate ? to : latestDate;
        if (overlapFrom < overlapTo) chart.timeScale().setVisibleRange({ from: overlapFrom, to: overlapTo });
        else chart.timeScale().fitContent();
      } else if (!instance.fitted || !unchangedExtent) chart.timeScale().fitContent();
      instance.fitted = true;
      instance.extent = extent;
      setFailed(false);
    } catch { setFailed(true); }
  }, [converted, ready, cacheKey, frequency, extent, earliestDate, latestDate]);

  const frequencyLabel = frequency === "1w" ? "每週統計" : "每日資料";
  return (
    <figure className={styles.figure} data-testid={`chip-chart-${chartId}`}>
      <div className={styles.heading}><h4>{title}</h4><span>{frequencyLabel} · 單位：{unit}</span></div>
      {!converted ? <div className={styles.empty} role="alert">籌碼數值或日期無法安全繪圖，請重新載入。</div>
        : !hasValues ? <div className={styles.empty}>此區間沒有可繪製的籌碼資料；缺值不補零。</div>
        : <>
          <div className={styles.legend} aria-live="off">
            <time>{selectedDate}</time>
            {converted.map((item) => {
              const datum = selectedDate ? item.exactByDate.get(selectedDate) : undefined;
              return <span key={item.source.id} style={{ color: item.source.color }}>{item.source.label}
                <strong>{datum?.value != null ? displayPrice(datum.value) : "—"}</strong>
              </span>;
            })}
          </div>
          <div className={styles.stage}>
            <div ref={container} className={styles.canvas} role="img" aria-label={`${title}，${frequencyLabel}，單位${unit}`} data-chart-ready={ready && !failed ? "true" : "false"} />
            {(!ready || failed) && <div className={styles.overlay} role={failed ? "alert" : "status"}>{failed ? "圖表暫時無法顯示；摘要與數字標籤仍保留原始精確數值。" : "正在建立籌碼圖表…"}</div>}
            {hovered && <div className={styles.tooltip} role="tooltip"><time>{hovered}</time>
              {converted.map((item) => {
                const datum = item.exactByDate.get(hovered);
                return <span key={item.source.id}>{item.source.label}：{datum?.value != null ? displayPrice(datum.value) : datum ? "缺值" : "此日無觀察"} {datum?.value != null ? unit : ""}</span>;
              })}
            </div>}
          </div>
        </>}
      <figcaption className={styles.caption}>{frequency === "1w" ? "僅使用來源真正的週統計日期，不建立每日持股點。" : "只繪製已保存的來源資料，不補缺日或缺值。"} 可拖曳、縮放及使用十字游標；圖形為顯示近似值，數字標籤保留 API 精度。</figcaption>
    </figure>
  );
}
