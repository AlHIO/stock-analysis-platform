"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import type { MouseEventParams, Time } from "lightweight-charts";
import type { Bar, Timeframe } from "../lib/market-types";
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

export function StockChart({
  bars,
  timeframe,
}: {
  bars: Bar[];
  timeframe: Timeframe;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [hovered, setHovered] = useState<Bar | null>(null);
  const [chartError, setChartError] = useState(false);
  const [ready, setReady] = useState(false);
  const converted = useMemo(() => {
    try {
      return toChartData(bars);
    } catch {
      return null;
    }
  }, [bars]);
  const selected =
    hovered ?? (converted ? [...converted.barsByDate.values()].at(-1) : null);

  useEffect(() => {
    const element = container.current;
    if (!element || !converted || !converted.candles.length) return;
    let disposed = false;
    let disposeChart: (() => void) | undefined;
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
        disposeChart = () => {
          observer?.disconnect();
          if (onCrosshair) chart.unsubscribeCrosshairMove(onCrosshair);
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
        const volume = chart.addSeries(
          library.HistogramSeries,
          {
            priceFormat: { type: "volume" },
            priceLineVisible: false,
            lastValueVisible: false,
          },
          1,
        );
        chart.panes()[0].setStretchFactor(4);
        chart.panes()[1].setStretchFactor(1);
        candles.setData(converted.candles);
        volume.setData(converted.volume);
        chart.timeScale().fitContent();
        onCrosshair = (event: MouseEventParams<Time>) => {
          if (
            !event.time ||
            !event.point ||
            event.point.x < 0 ||
            event.point.y < 0 ||
            event.point.x > element.clientWidth ||
            event.point.y > element.clientHeight
          ) {
            setHovered(null);
            return;
          }
          setHovered(
            converted.barsByDate.get(chartTimeKey(event.time)) ?? null,
          );
        };
        chart.subscribeCrosshairMove(onCrosshair);
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
  }, [converted, timeframe]);

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
      aria-label={`${PERIOD_LABELS[timeframe]}與成交量圖`}
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
      <div className={styles.stage}>
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
