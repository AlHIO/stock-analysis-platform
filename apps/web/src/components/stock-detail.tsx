"use client";

import { useEffect, useMemo, useState, type FormEvent } from "react";
import Link from "next/link";
import { ApiError, getBars, getLatest } from "../lib/market-api";
import type {
  BarsResponse,
  LatestResponse,
  Timeframe,
} from "../lib/market-types";
import {
  changeDirection,
  dateLabel,
  decimal,
  marketLabel,
  shares,
  timestamp,
} from "../lib/format";
import {
  alignedHistoryRange,
  presetHistoryRange,
  recommendedHistoryTimeframe,
  validateHistoryRange,
  type HistoryPreset,
  type HistoryRange,
} from "../lib/history-range";
import { analyzeHistoryGaps } from "../lib/history-gaps";
import { IndicatorWorkspace } from "./indicator-workspace";
import { StockSearch } from "./stock-search";
import { ChipDashboard } from "./chip-dashboard";

type Load<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "error"; error: unknown };
const timeframes: { value: Timeframe; label: string }[] = [
  { value: "1d", label: "日 K" },
  { value: "1w", label: "週 K" },
  { value: "1M", label: "月 K" },
];
const presets: { value: HistoryPreset; label: string }[] = [
  { value: "1y", label: "最近 1 年" },
  { value: "5y", label: "最近 5 年" },
  { value: "10y", label: "最近 10 年" },
];
type HistorySelection = {
  id: string;
  preset: HistoryPreset | null;
  range: HistoryRange;
};

function selectedRange(
  data: LatestResponse,
  id: string,
  selection: HistorySelection | null,
) {
  const current = selection?.id === id ? selection : null;
  return current?.preset === null
    ? current.range
    : presetHistoryRange(
        data.history.available_from,
        data.history.available_to,
        current?.preset ?? "1y",
      );
}

function Failure({ error, retry }: { error: unknown; retry: () => void }) {
  const unavailable = !(error instanceof ApiError) || error.status >= 500;
  const missing = error instanceof ApiError && error.status === 404;
  return (
    <div role="alert" className="empty-state error-state">
      <span className="state-icon" aria-hidden="true">
        {missing ? "?" : "!"}
      </span>
      <h2>
        {missing
          ? "找不到這檔股票"
          : unavailable
            ? "行情 API 暫時無法連線"
            : "無法載入行情"}
      </h2>
      <p>
        {missing
          ? "證券可能不存在，或目前不是股票類型。請重新搜尋。"
          : "請稍後重試。已保存的資料不會因查詢失敗而變更。"}
      </p>
      {!missing && (
        <button className="secondary-button" onClick={retry}>
          重新載入
        </button>
      )}
      {missing && (
        <Link className="secondary-button" href="/">
          返回搜尋
        </Link>
      )}
    </div>
  );
}

export function StockDetail({ id }: { id: string }) {
  const [latest, setLatest] = useState<Load<LatestResponse>>({
    status: "loading",
  });
  const [bars, setBars] = useState<
    Load<BarsResponse> | { status: "range-error" }
  >({ status: "loading" });
  const [timeframe, setTimeframe] = useState<Timeframe>("1d");
  const [refresh, setRefresh] = useState(0);
  const [chartRefresh, setChartRefresh] = useState(0);
  const [selection, setSelection] = useState<HistorySelection | null>(null);
  const [dateDraft, setDateDraft] = useState<HistoryRange>({
    from: "",
    to: "",
  });
  const [rangeError, setRangeError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLatest({ status: "loading" });
    getLatest(id, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setLatest({ status: "ready", data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setLatest({ status: "error", error });
      });
    return () => controller.abort();
  }, [id, refresh]);

  useEffect(() => {
    if (latest.status !== "ready" || latest.data.instrument.id !== id) return;
    setDateDraft(
      selectedRange(latest.data, id, selection) ?? { from: "", to: "" },
    );
    setRangeError(null);
  }, [id, latest, selection]);

  useEffect(() => {
    if (latest.status !== "ready" || latest.data.instrument.id !== id) return;
    const controller = new AbortController();
    setBars({ status: "loading" });
    const requested = selectedRange(latest.data, id, selection);
    if (
      requested &&
      timeframe === "1d" &&
      recommendedHistoryTimeframe(requested) === "1w"
    ) {
      setTimeframe("1w");
      return () => controller.abort();
    }
    const range = alignedHistoryRange(requested, timeframe);
    if (requested && !range) {
      setBars({ status: "range-error" });
      return () => controller.abort();
    }
    getBars(id, timeframe, controller.signal, range)
      .then((data) => {
        if (!controller.signal.aborted) setBars({ status: "ready", data });
      })
      .catch((error) => {
        if (!controller.signal.aborted) setBars({ status: "error", error });
      });
    return () => controller.abort();
  }, [id, timeframe, latest, selection, chartRefresh]);

  const data =
    latest.status === "ready" && latest.data.instrument.id === id
      ? latest.data
      : null;
  const bar = data?.bar;
  const freshness = data?.freshness;
  const older =
    freshness?.age_calendar_days != null && freshness.age_calendar_days > 0;
  const future = freshness?.assessment === "future_dated";
  const change = bar?.change;
  const direction = changeDirection(change);
  const validChart =
    bars.status === "ready" && bars.data.timeframe === timeframe;
  const observedHistory = useMemo(() => {
    if (bars.status !== "ready" || bars.data.timeframe !== timeframe)
      return { gaps: [], recentFrom: null, invalid: false };
    try {
      return { ...analyzeHistoryGaps(bars.data.bars), invalid: false };
    } catch {
      return { gaps: [], recentFrom: null, invalid: true };
    }
  }, [bars, timeframe]);
  const canRenderChart = validChart && !observedHistory.invalid;
  const requestedRange = data ? selectedRange(data, id, selection) : undefined;
  const wideRange =
    !!requestedRange && recommendedHistoryTimeframe(requestedRange) === "1w";
  const alignmentError =
    requestedRange && !alignedHistoryRange(requestedRange, timeframe)
      ? "週／月 K 對齊後超過可查詢範圍，請縮短日期區間。"
      : null;
  const currentPreset = selection?.id === id ? selection.preset : "1y";

  function chooseTimeframe(range: HistoryRange) {
    setTimeframe((current) =>
      current === "1M" ? "1M" : recommendedHistoryTimeframe(range),
    );
  }

  function selectPreset(preset: HistoryPreset) {
    if (!data) return;
    const range = presetHistoryRange(
      data.history.available_from,
      data.history.available_to,
      preset,
    );
    if (!range) return;
    setSelection({ id, preset, range });
    chooseTimeframe(range);
  }

  function applyDates(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const error = validateHistoryRange(dateDraft);
    if (error) {
      setRangeError(error);
      return;
    }
    if (data && dateDraft.to > data.freshness.market_local_date) {
      setRangeError("結束日期不可晚於市場當地日期。");
      return;
    }
    setRangeError(null);
    setSelection({ id, preset: null, range: { ...dateDraft } });
    chooseTimeframe(dateDraft);
  }

  function showRecentSegment() {
    if (bars.status !== "ready" || !observedHistory.recentFrom) return;
    setSelection({
      id,
      preset: null,
      range: {
        from: observedHistory.recentFrom,
        to: bars.data.to,
      },
    });
  }

  return (
    <main className="page-container detail-page">
      <div className="detail-navigation">
        <Link href="/" className="back-link">
          ← 股票搜尋
        </Link>
        <StockSearch compact />
      </div>
      {latest.status === "loading" && (
        <div role="status" className="detail-skeleton">
          <div className="skeleton skeleton-title" />
          <div className="skeleton skeleton-price" />
          <p>正在讀取已收錄行情…</p>
        </div>
      )}
      {latest.status === "error" && (
        <Failure error={latest.error} retry={() => setRefresh((n) => n + 1)} />
      )}
      {data && (
        <>
          <div className="instrument-heading">
            <div>
              <div className="eyebrow">
                STOCK OVERVIEW{" "}
                <span className="market-badge">
                  {marketLabel(data.instrument.market.code)} ·{" "}
                  {data.instrument.market.code}
                </span>
              </div>
              <h1>
                {data.instrument.name}
                <span className="instrument-symbol">
                  {data.instrument.symbol}
                </span>
              </h1>
            </div>
            <span className="snapshot-badge">日行情快照 · 非即時</span>
          </div>

          {(older || future) && (
            <div role="status" className="freshness-notice">
              <span aria-hidden="true">
                <svg
                  viewBox="0 0 24 24"
                  width="21"
                  height="21"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.4"
                >
                  <circle cx="12" cy="12" r="9" />
                  <path d="M12 6v6l4 2" />
                </svg>
              </span>
              <div>
                <strong>
                  {future
                    ? "資料日期超出市場本地日期"
                    : "目前顯示較早日期的已收錄資料"}
                </strong>
                <p>
                  {future
                    ? "請確認來源日期，暫不視為目前行情。"
                    : `最新交易日為 ${dateLabel(freshness.latest_trade_date)}，距市場本地日期 ${freshness.age_calendar_days} 個日曆日。此差距不代表遺漏交易日；尚未驗證交易日曆與來源是否已更新。`}
                </p>
              </div>
            </div>
          )}

          <section className="quote-grid" aria-label="最新日行情">
            <div className="quote-primary">
              <span className="metric-label">
                最新已收錄收盤價{" "}
                <small>{data.instrument.market.currency}</small>
              </span>
              <div className={`closing-price ${direction}`}>
                {decimal(bar?.close)}
              </div>
              <span className="quote-date">
                所屬交易日 {dateLabel(bar?.trade_date)}
              </span>
            </div>
            <div className="metric">
              <span className="metric-label">漲跌 / 漲跌幅</span>
              <div className={`metric-value ${direction}`}>
                {decimal(change)}
                <span className="metric-unit">
                  {" "}
                  / {decimal(bar?.change_percent)}
                  {bar?.change_percent != null ? "%" : ""}
                </span>
              </div>
              <p>缺少可核實官方參考價時不計算</p>
            </div>
            <div className="metric">
              <span className="metric-label">成交量</span>
              <div className="metric-value">
                {shares(bar?.volume_shares)}
                <span className="metric-unit"> 股</span>
              </div>
              <p>以股為單位，非張數</p>
            </div>
            <div className="metric">
              <span className="metric-label">資料匯入時間</span>
              <div className="metric-value timestamp-value">
                {timestamp(
                  bar?.last_ingested_at,
                  data.instrument.market.timezone,
                )}
              </div>
              <p>{data.instrument.market.timezone} · 非報價時間</p>
            </div>
          </section>

          {!bar && (
            <div className="no-latest" role="status">
              這檔股票尚無已收錄日行情。搜尋結果中的證券資料不代表已有行情。
            </div>
          )}

          <section id="stock-price-chart" className="chart-panel" aria-label="歷史 K 線">
            <div className="chart-toolbar">
              <div>
                <h2>
                  價格走勢<span>未還原權息</span>
                </h2>
                <p>僅顯示資料庫已保存的行情</p>
              </div>
              <div
                className="timeframe-tabs"
                role="group"
                aria-label="K 線週期"
              >
                {timeframes.map((item) => (
                  <button
                    key={item.value}
                    aria-pressed={timeframe === item.value}
                    disabled={item.value === "1d" && wideRange}
                    aria-describedby={
                      wideRange ? "history-daily-limit" : undefined
                    }
                    onClick={() => setTimeframe(item.value)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="history-range-controls">
              <div className="history-range-topline">
                <div
                  role="group"
                  aria-label="歷史資料範圍"
                  className="history-range-presets"
                >
                  {presets.map((preset) => (
                    <button
                      key={preset.value}
                      type="button"
                      aria-pressed={currentPreset === preset.value}
                      disabled={!data.history.available_to}
                      onClick={() => selectPreset(preset.value)}
                    >
                      {preset.label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  className="history-refresh"
                  onClick={() => setRefresh((n) => n + 1)}
                >
                  重新讀取資料
                </button>
              </div>
              <form
                aria-label="自訂歷史日期"
                noValidate
                className="history-date-form"
                onSubmit={applyDates}
              >
                <label>
                  開始日期
                  <input
                    type="date"
                    value={dateDraft.from}
                    aria-invalid={rangeError ? true : undefined}
                    onChange={(event) => {
                      setDateDraft((current) => ({
                        ...current,
                        from: event.target.value,
                      }));
                      setRangeError(null);
                    }}
                  />
                </label>
                <label>
                  結束日期
                  <input
                    type="date"
                    value={dateDraft.to}
                    max={data.freshness.market_local_date}
                    aria-invalid={rangeError ? true : undefined}
                    onChange={(event) => {
                      setDateDraft((current) => ({
                        ...current,
                        to: event.target.value,
                      }));
                      setRangeError(null);
                    }}
                  />
                </label>
                <button type="submit" disabled={!data.history.available_to}>
                  套用日期
                </button>
              </form>
              <p className="history-range-coverage">
                最早／最新已收錄日期 {dateLabel(data.history.available_from)} —{" "}
                {dateLabel(data.history.available_to)}。
                起訖之間不一定已收錄完整；重新讀取可查看新入庫資料。
              </p>
              {wideRange && (
                <p className="history-range-hint" id="history-daily-limit">
                  日 K 每次最多顯示 1,000 根；長區間請使用週 K／月
                  K，或縮短日期範圍。
                </p>
              )}
              {(rangeError || alignmentError) && (
                <p role="alert" className="history-range-error">
                  {rangeError || alignmentError}
                </p>
              )}
            </div>
            {observedHistory.gaps.length > 0 && (
              <aside
                role="status"
                aria-label="歷史資料日期間隔"
                className="history-gap-notice"
              >
                <strong>這段歷史資料尚未連續收錄</strong>
                <p>
                  以下相鄰已收錄行情的觀察日期間隔超過 90
                  個日曆日，兩者之間目前沒有可顯示的 K 棒：
                </p>
                <ul>
                  {observedHistory.gaps.slice(0, 3).map((gap) => (
                    <li key={`${gap.previousDate}-${gap.nextDate}`}>
                      {dateLabel(gap.previousDate)} → {dateLabel(gap.nextDate)}
                    </li>
                  ))}
                </ul>
                {observedHistory.gaps.length > 3 && (
                  <p>另有 {observedHistory.gaps.length - 3} 段日期間隔。</p>
                )}
                <p>
                  圖表按已收錄 K
                  棒排列，沒有行情的期間會壓縮顯示。最早與最新日期不代表中間資料完整，也不能據此判定哪些日期應有交易。技術指標只依已收錄行情計算，缺漏也可能影響暖機與數值。
                </p>
                <button type="button" onClick={showRecentSegment}>
                  只看最近已收錄區段
                </button>
              </aside>
            )}
            {observedHistory.invalid && (
              <p role="alert" className="history-gap-notice">
                行情日期資訊不一致，暫時無法安全顯示圖表。請重新讀取資料。
              </p>
            )}
            <IndicatorWorkspace
              instrumentId={id}
              timeframe={timeframe}
              bars={canRenderChart ? bars.data.bars : null}
              range={
                canRenderChart
                  ? { from: bars.data.from, to: bars.data.to }
                  : undefined
              }
              placeholder={
                observedHistory.invalid ? (
                  <div className="empty-state">
                    <p>請重新讀取資料後再查看圖表。</p>
                  </div>
                ) : bars.status === "range-error" ? (
                  <div className="empty-state">
                    <p>請調整日期範圍後重新查詢。</p>
                  </div>
                ) : bars.status === "error" ? (
                  <Failure
                    error={bars.error}
                    retry={() => setChartRefresh((n) => n + 1)}
                  />
                ) : !validChart ? (
                  <div role="status" className="chart-loading">
                    <span className="loading-spinner" />
                    正在載入
                    {timeframes.find((item) => item.value === timeframe)?.label}
                    …
                  </div>
                ) : null
              }
            />
            {validChart && (
              <>
                <div className="chart-data-note">
                  <span>
                    查詢範圍 {dateLabel(bars.data.from)} —{" "}
                    {dateLabel(bars.data.to)}
                  </span>
                  <span>已收錄 {bars.data.count} 根 K 棒 · 成交量單位：股</span>
                </div>
                {bars.data.bars.length > 0 && (
                  <div className="completeness-note">
                    <span className="small-label">資料覆蓋</span>
                    <p>
                      {bars.data.bars.some((item) => !item.is_complete)
                        ? "包含尚未完成或完整度未確認的 K 棒。"
                        : "此查詢期間的 K 棒已通過交易日曆核對。"}
                      目前全部歷史完整性未驗證；沒有資料的日期不補值。週 K、月 K
                      由後端按市場交易日聚合。
                    </p>
                  </div>
                )}
              </>
            )}
          </section>

          <ChipDashboard key={id} id={id} marketDate={data.freshness.market_local_date} currency={data.instrument.market.currency}/>

          <section className="provenance-panel" aria-label="資料來源與限制">
            <div>
              <span className="small-label">DATA PROVENANCE</span>
              <h2>知道資料從哪裡來</h2>
            </div>
            <dl>
              <div>
                <dt>最早／最新已收錄日期</dt>
                <dd>
                  {dateLabel(data.history.available_from)} —{" "}
                  {dateLabel(data.history.available_to)}
                </dd>
              </div>
              <div>
                <dt>最新日行情來源</dt>
                <dd>
                  {bar?.sources.length
                    ? bar.sources
                        .map((source) => `${source.name} (${source.code})`)
                        .join("、")
                    : "尚無來源"}
                </dd>
              </div>
              <div>
                <dt>資料時效評估</dt>
                <dd>
                  {timestamp(
                    data.freshness.as_of,
                    data.instrument.market.timezone,
                  )}{" "}
                  · 尚未驗證應發布交易日
                </dd>
              </div>
            </dl>
            <p className="provenance-disclaimer">
              以上為已收錄的未還原日行情，非即時報價。歷史資料可能不完整；除權息、停牌與缺漏不應僅憑圖形判定。漲跌與漲跌幅僅在
              API 提供可核實基準時呈現。
            </p>
          </section>
        </>
      )}
    </main>
  );
}
