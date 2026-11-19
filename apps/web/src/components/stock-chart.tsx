"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { LogicalRange, MouseEventParams, Time } from "lightweight-charts";
import type { Bar, Timeframe } from "../lib/market-types";
import type { IndicatorResponse } from "../lib/technical-types";
import { toIndicatorChartData } from "../lib/indicator-chart";
import { connectChartSync, overlappingChartRange, type ChartDateRange, type ChartSyncConnection } from "../lib/chart-sync";
import {
  chartPricePrecision,
  chartTimeKey,
  displayPrice,
  DOWN_COLOR,
  toChartData,
  UP_COLOR,
} from "../lib/chart-data";
import styles from "./stock-chart.module.css";

const PERIOD_LABELS: Record<Timeframe, string> = {
  "1d": "日 K",
  "1w": "週 K",
  "1M": "月 K",
};
const COMPLETENESS_LABELS = {
  complete: "期間資料完整",
  incomplete: "期間資料尚未完整",
  unknown: "期間完整度未驗證",
};
const NO_INDICATORS: IndicatorResponse[] = [];

export function StockChart({
  bars,
  timeframe,
  indicators = NO_INDICATORS,
  syncGroup,
  showVolume = true,
}: {
  bars: Bar[];
  timeframe: Timeframe;
  indicators?: IndicatorResponse[];
  syncGroup?: string;
  showVolume?: boolean;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState<Bar | null>(null);
  const [chartError, setChartError] = useState(false);
  const [ready, setReady] = useState(false);
  const [sharedDate, setSharedDate] = useState<string | null>(null);
  const viewport = useRef<{ key: string; logical: LogicalRange | null; time: ChartDateRange | null } | null>(null);
  const converted = useMemo(() => {
    try {
      return toChartData(bars);
    } catch {
      return null;
    }
  }, [bars]);
  const indicatorData = useMemo(() => {
    try {
      return {
        data: toIndicatorChartData(indicators, bars, timeframe),
        error: false,
      };
    } catch {
      return { data: [], error: true };
    }
  }, [indicators, bars, timeframe]);
  const subpanes = indicatorData.data.filter(
    (item) =>
      item.source.indicator === "rsi" || item.source.indicator === "macd",
  ).length;
  const selected = sharedDate
    ? converted?.barsByDate.get(sharedDate) ?? null
    : hovered ?? (converted ? [...converted.barsByDate.values()].at(-1) : null);

  useEffect(() => {
    const element = container.current;
    if (!element || !converted || !converted.candles.length) return;
    let disposed = false;
    let disposeChart: (() => void) | undefined;
    setSharedDate(null);
    setHovered(null);
    setChartError(false);
    setReady(false);
    void import("lightweight-charts")
      .then((library) => {
        if (disposed) return;
        const chart = library.createChart(element, {
          width: Math.max(1, element.clientWidth),
          height: Math.max(1, element.clientHeight),
          layout: {
            background: { type: library.ColorType.Solid, color: "#111a25" },
            textColor: "#92a2b7",
            fontFamily: "Arial, sans-serif",
            fontSize: 11,
            attributionLogo: true,
          },
          grid: {
            vertLines: { color: "#1d2938" },
            horzLines: { color: "#1d2938" },
          },
          crosshair: {
            mode: library.CrosshairMode.Normal,
            vertLine: { color: "#71839c", labelBackgroundColor: "#334155" },
            horzLine: { color: "#71839c", labelBackgroundColor: "#334155" },
          },
          rightPriceScale: {
            borderColor: "#283547",
            scaleMargins: { top: 0.12, bottom: 0.08 },
          },
          timeScale: {
            borderColor: "#283547",
            timeVisible: false,
            rightOffset: 3,
            barSpacing: 12,
            maxBarSpacing: 24,
          },
          handleScroll: {
            mouseWheel: true,
            pressedMouseMove: true,
            horzTouchDrag: true,
            vertTouchDrag: false,
          },
          handleScale: {
            mouseWheel: true,
            pinch: true,
            axisPressedMouseMove: true,
          },
          localization: { locale: "zh-TW", dateFormat: "yyyy/MM/dd" },
        });
        let observer: ResizeObserver | undefined;
        let onCrosshair: ((event: MouseEventParams<Time>) => void) | undefined;
        let onTimeRange: (() => void) | undefined;
        let sync: ChartSyncConnection | undefined;
        let applyingSync = false;
        let ignoreCursor: string | null | undefined;
        let lastAppliedRange = "";
        const firstDate = converted.candles[0].time as string;
        const lastDate = converted.candles.at(-1)!.time as string;
        const viewportKey = `${syncGroup ?? "stock"}/${timeframe}/${firstDate}/${lastDate}/${bars.length}`;
        disposeChart = () => {
          try {
            const range = chart.timeScale().getVisibleRange();
            viewport.current = { key: viewportKey, logical: chart.timeScale().getVisibleLogicalRange(),
              time: range ? { from: chartTimeKey(range.from), to: chartTimeKey(range.to) } : null };
          } catch { /* A failed renderer has no reusable viewport. */ }
          sync?.dispose();
          observer?.disconnect();
          if (onCrosshair) chart.unsubscribeCrosshairMove(onCrosshair);
          if (onTimeRange) chart.timeScale().unsubscribeVisibleTimeRangeChange(onTimeRange);
          chart.remove();
        };
        const precision = chartPricePrecision(bars);
        const candles = chart.addSeries(library.CandlestickSeries, {
          upColor: UP_COLOR,
          downColor: DOWN_COLOR,
          borderUpColor: UP_COLOR,
          borderDownColor: DOWN_COLOR,
          wickUpColor: UP_COLOR,
          wickDownColor: DOWN_COLOR,
          priceFormat: { type: "price", precision, minMove: 10 ** -precision },
        });
        const hasVolumePane = showVolume || indicatorData.data.some(item => item.source.indicator === "volume_sma");
        const volume = showVolume ? chart.addSeries(
          library.HistogramSeries,
          {
            priceFormat: { type: "volume" },
            priceLineVisible: false,
            lastValueVisible: false,
          },
          1,
        ) : undefined;
        chart.panes()[0].setStretchFactor(4);
        candles.setData(converted.candles);
        volume?.setData(converted.volume);
        let nextPane = hasVolumePane ? 2 : 1;
        // Separate line series prevent the library bridging whitespace across a
        // missing-input reset. Histogram gaps need no connecting segment.
        for (const indicator of indicatorData.data) {
          const name = indicator.source.indicator;
          const paneIndex =
            name === "sma" || name === "ema"
              ? 0
              : name === "volume_sma"
                ? 1
                : nextPane++;
          let firstLine: ReturnType<typeof chart.addSeries<"Line">> | undefined;
          for (const component of indicator.components) {
            if (component.histogram) {
              chart
                .addSeries(
                  library.HistogramSeries,
                  {
                    title: "MACD HIST",
                    priceLineVisible: false,
                    lastValueVisible: false,
                  },
                  paneIndex,
                )
                .setData(component.data);
              continue;
            }
            const segments = component.segments.length
              ? component.segments
              : [[]];
            for (const segment of segments) {
              const line = chart.addSeries(
                library.LineSeries,
                {
                  title: indicator.label,
                  color: component.color,
                  lineWidth: 2,
                  priceLineVisible: false,
                  lastValueVisible: false,
                  pointMarkersVisible: segment.length === 1,
                  autoscaleInfoProvider:
                    name === "rsi"
                      ? () => ({ priceRange: { minValue: 0, maxValue: 100 } })
                      : undefined,
                  priceFormat:
                    name === "volume_sma"
                      ? { type: "volume" }
                      : {
                          type: "price",
                          precision: component.precision,
                          minMove: 10 ** -component.precision,
                        },
                },
                paneIndex,
              );
              line.setData(segment);
              firstLine ??= line;
            }
          }
          if (
            name === "rsi" &&
            firstLine &&
            indicator.components.some((item) => item.segments.length)
          ) {
            for (const level of [30, 70])
              firstLine.createPriceLine({
                price: level,
                color: "#8d739455",
                lineWidth: 1,
                lineStyle: library.LineStyle.Dashed,
                axisLabelVisible: true,
                title: String(level),
              });
          }
          if (paneIndex > 0) chart.panes()[paneIndex].setStretchFactor(1.5);
        }
        if (hasVolumePane) chart.panes()[1].setStretchFactor(1);
        const saved = viewport.current;
        if (saved?.key === viewportKey && saved.logical) chart.timeScale().setVisibleLogicalRange(saved.logical);
        else {
          const overlap = saved?.time ? overlappingChartRange(saved.time, firstDate, lastDate) : null;
          if (overlap) chart.timeScale().setVisibleRange(overlap);
          else chart.timeScale().fitContent();
        }
        onCrosshair = (event: MouseEventParams<Time>) => {
          if (applyingSync) return;
          const date = event.time ? chartTimeKey(event.time) : null;
          if (ignoreCursor !== undefined && ignoreCursor === date) { ignoreCursor = undefined; return; }
          ignoreCursor = undefined;
          setSharedDate(null);
          if (
            !event.time ||
            !event.point ||
            event.point.x < 0 ||
            event.point.y < 0 ||
            event.point.x > element.clientWidth ||
            event.point.y > element.clientHeight
          ) {
            setHovered(null);
            sync?.publishCursor(null);
            return;
          }
          setHovered(
            converted.barsByDate.get(chartTimeKey(event.time)) ?? null,
          );
          sync?.publishCursor(date);
        };
        chart.subscribeCrosshairMove(onCrosshair);
        if (syncGroup) {
          sync = connectChartSync(syncGroup, {
            cursor(date) {
              applyingSync = true;
              setSharedDate(date);
              const bar = date ? converted.barsByDate.get(date) : undefined;
              setHovered(bar ?? null);
              try {
                ignoreCursor = bar ? date : null;
                if (bar) chart.setCrosshairPosition(Number(bar.close), date!, candles);
                else chart.clearCrosshairPosition();
              } finally { applyingSync = false; }
            },
            range(value) {
              const overlap = overlappingChartRange(value, firstDate, lastDate);
              if (!overlap) return;
              applyingSync = true;
              try {
                chart.timeScale().setVisibleRange(overlap);
                const visible = chart.timeScale().getVisibleRange();
                lastAppliedRange = visible ? `${chartTimeKey(visible.from)}/${chartTimeKey(visible.to)}` : "";
              } catch { /* A hidden chart can join again after its next resize. */ }
              finally { applyingSync = false; }
            },
          });
          onTimeRange = () => {
            if (applyingSync || disposed || !element.clientWidth || !element.clientHeight) return;
            const visible = chart.timeScale().getVisibleRange();
            if (!visible) return;
            const value = { from: chartTimeKey(visible.from), to: chartTimeKey(visible.to) };
            if (`${value.from}/${value.to}` === lastAppliedRange) return;
            lastAppliedRange = "";
            sync?.publishRange(value);
          };
          chart.timeScale().subscribeVisibleTimeRangeChange(onTimeRange);
        }
        observer = new ResizeObserver((entries) => {
          if (disposed) return;
          const rect = entries[0]?.contentRect;
          if (rect && rect.width > 0 && rect.height > 0)
            chart.resize(Math.floor(rect.width), Math.floor(rect.height));
        });
        observer.observe(element);
        setReady(true);
      })
      .catch(() => {
        disposeChart?.();
        disposeChart = undefined;
        if (!disposed) setChartError(true);
      });
    return () => {
      disposed = true;
      disposeChart?.();
    };
  }, [converted, timeframe, indicatorData, syncGroup, showVolume]);

  if (!bars.length)
    return (
      <div className={styles.empty} role="status">
        此查詢區間沒有已收錄行情，不產生 K 線。
      </div>
    );
  if (!converted)
    return (
      <div className={styles.empty} role="alert">
        行情資料無法安全繪製，請稍後重試。
      </div>
    );

  return (
    <figure
      className={styles.figure}
      aria-label={`${PERIOD_LABELS[timeframe]}${showVolume ? "與成交量" : "價格"}圖`}
    >
      {selected && (
        <div className={styles.legend} aria-label="圖表選取行情">
          <span className={styles.legendDate}>{selected.trade_date}</span>
          <span>
            開 <b>{displayPrice(selected.open)}</b>
          </span>
          <span>
            高 <b>{displayPrice(selected.high)}</b>
          </span>
          <span>
            低 <b>{displayPrice(selected.low)}</b>
          </span>
          <span>
            收 <b>{displayPrice(selected.close)}</b>
          </span>
          <span>
            量 <b>{selected.volume_shares.toLocaleString("zh-TW")}</b> 股
          </span>
        </div>
      )}
      {sharedDate && !selected && <div className={styles.legend} aria-label="圖表選取行情">{sharedDate} · 此日期無{PERIOD_LABELS[timeframe]}觀察，不沿用其他日期。</div>}
      {indicatorData.error && (
        <p className={styles.indicatorWarning} role="alert">
          部分指標數值無法安全繪圖；價格與成交量仍可檢視。
        </p>
      )}
      {!!indicatorData.data.length && (
        <div className={styles.indicatorLegend} aria-label="已顯示技術指標">
          {indicatorData.data.map((indicator) => {
            const point = selected
              ? indicator.pointsByDate.get(selected.trade_date)
              : undefined;
            return (
              <span key={indicator.source.indicator}>
                <strong style={{ color: indicator.components[0].color }}>
                  {indicator.label}
                </strong>
                {indicator.components.map((component) => (
                  <span key={component.key}>
                    {indicator.source.indicator === "macd"
                      ? `${component.label} `
                      : ""}
                    {displayPrice(point?.values[component.key] ?? "")}
                  </span>
                ))}
                {point?.status !== "ready" && (
                  <em>
                    {point?.status === "missing_input"
                      ? "輸入缺值"
                      : "暖機／資料不足"}
                  </em>
                )}
                {point?.is_provisional && <em>暫定</em>}
              </span>
            );
          })}
        </div>
      )}
      <div className={styles.stage} data-subpanes={subpanes}>
        <div ref={container} className={styles.canvas} aria-hidden="true" />
        {!ready && !chartError && (
          <div className={styles.overlay} role="status">
            載入圖表…
          </div>
        )}
        {chartError && (
          <div className={styles.overlay} role="alert">
            圖表暫時無法開啟，請重新整理。上方仍顯示已取得的行情數值。
          </div>
        )}
        {hovered && (
          <div className={styles.tooltip} role="status" aria-label="十字線行情">
            <strong>
              {hovered.first_trade_date}{" "}
              {hovered.last_trade_date !== hovered.first_trade_date
                ? `— ${hovered.last_trade_date}`
                : ""}
            </strong>
            <span>
              開 {displayPrice(hovered.open)} · 收 {displayPrice(hovered.close)}
            </span>
            <span>
              高 {displayPrice(hovered.high)} · 低 {displayPrice(hovered.low)}
            </span>
            <span>
              成交量 {hovered.volume_shares.toLocaleString("zh-TW")} 股
            </span>
            <span>
              {PERIOD_LABELS[timeframe]} · {hovered.observed_session_count}{" "}
              個已收錄交易日
            </span>
            <span>{COMPLETENESS_LABELS[hovered.completeness]}</span>
            <span>{hovered.period_closed ? "期間已結束" : "期間尚未結束"}</span>
            {indicatorData.data.map((indicator) => {
              const point = indicator.pointsByDate.get(hovered.trade_date);
              return (
                <span
                  key={indicator.source.indicator}
                  className={styles.tooltipIndicator}
                >
                  <strong>{indicator.label}</strong>{" "}
                  {indicator.components
                    .map(
                      (component) =>
                        `${component.label} ${displayPrice(point?.values[component.key] ?? "")}`,
                    )
                    .join(" · ")}
                  {point?.status !== "ready" ? " · 暖機／資料不足" : ""}
                </span>
              );
            })}
          </div>
        )}
      </div>
      <figcaption className={styles.caption}>
        <span>
          {bars.length} 根已收錄 K 棒 · 成交量單位：股 · 紅：收≥開／綠：收&lt;開
        </span>
        <span>拖曳平移，滾輪或雙指縮放，移動十字線查看數值。</span>
      </figcaption>
      <div className={styles.notes}>
        <span>
          依市場交易日顯示；無資料日期不補值。週／月 K
          由後端聚合，時間軸標示最後已收錄交易日。
        </span>
        <a href="https://www.tradingview.com/" target="_blank" rel="noreferrer">
          TradingView Lightweight Charts™ · Copyright (с) 2025 TradingView,
          Inc. ↗
        </a>
      </div>
    </figure>
  );
}
