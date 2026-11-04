"use client";

import {
  useEffect,
  useMemo,
  useState,
  type FormEvent,
  type ReactNode,
} from "react";
import type { Bar, Timeframe } from "../lib/market-types";
import { getIndicator } from "../lib/technical-api";
import {
  INDICATOR_NAMES,
  type IndicatorName,
  type IndicatorResponse,
} from "../lib/technical-types";
import { StockChart } from "./stock-chart";
import { decimal } from "../lib/format";
import styles from "./indicator-workspace.module.css";

type Parameters = Record<IndicatorName, Record<string, number>>;
const DEFAULTS: Parameters = {
  sma: { period: 20 },
  ema: { period: 20 },
  rsi: { period: 14 },
  macd: { fast_period: 12, slow_period: 26, signal_period: 9 },
  volume_sma: { period: 20 },
};
const LABELS: Record<IndicatorName, string> = {
  sma: "SMA",
  ema: "EMA",
  rsi: "RSI",
  macd: "MACD",
  volume_sma: "成交量均線",
};
const FIELD_LABELS: Record<string, string> = {
  period: "期數",
  fast_period: "快線期數",
  slow_period: "慢線期數",
  signal_period: "訊號期數",
};
const VALUE_LABELS: Record<string, string> = {
  sma: "SMA",
  ema: "EMA",
  rsi: "RSI",
  macd: "MACD",
  signal: "Signal",
  histogram: "Histogram",
  volume_sma: "均量",
};
type Entry = {
  name: IndicatorName;
  status: "loading" | "ready" | "error";
  data?: IndicatorResponse;
};

export function IndicatorWorkspace({
  instrumentId,
  bars,
  timeframe,
  range,
  placeholder,
}: {
  instrumentId: string;
  bars: Bar[] | null;
  timeframe: Timeframe;
  range?: { from: string; to: string };
  placeholder?: ReactNode;
}) {
  const [enabled, setEnabled] = useState<Record<IndicatorName, boolean>>({
    sma: false,
    ema: false,
    rsi: false,
    macd: false,
    volume_sma: false,
  });
  const [parameters, setParameters] = useState<Parameters>(DEFAULTS);
  const [draft, setDraft] = useState<
    Record<IndicatorName, Record<string, string>>
  >(
    () =>
      Object.fromEntries(
        INDICATOR_NAMES.map((name) => [
          name,
          Object.fromEntries(
            Object.entries(DEFAULTS[name]).map(([key, value]) => [
              key,
              String(value),
            ]),
          ),
        ]),
      ) as Record<IndicatorName, Record<string, string>>,
  );
  const [validation, setValidation] = useState("");
  const [retry, setRetry] = useState(0);
  const [result, setResult] = useState<{
    key: string;
    bars: Bar[] | null;
    entries: Entry[];
  }>({
    key: "",
    bars: null,
    entries: [],
  });
  const selected = INDICATOR_NAMES.filter((name) => enabled[name]);
  const canQuery = bars !== null && bars.length > 0 && range !== undefined;
  const key = JSON.stringify({
    instrumentId,
    timeframe,
    range,
    selected: selected.map((name) => [name, parameters[name]]),
    canQuery,
  });

  useEffect(() => {
    const controller = new AbortController();
    if (!canQuery || !range || !selected.length) {
      setResult({ key, bars, entries: [] });
      return () => controller.abort();
    }
    setResult({
      key,
      bars,
      entries: selected.map((name) => ({ name, status: "loading" })),
    });
    for (const name of selected) {
      getIndicator(
        instrumentId,
        timeframe,
        name,
        parameters[name],
        range,
        controller.signal,
      )
        .then((data) => {
          if (!controller.signal.aborted)
            setResult((current) =>
              current.key !== key
                ? current
                : {
                    ...current,
                    entries: current.entries.map((entry) =>
                      entry.name === name
                        ? { name, status: "ready", data }
                        : entry,
                    ),
                  },
            );
        })
        .catch(() => {
          if (!controller.signal.aborted)
            setResult((current) =>
              current.key !== key
                ? current
                : {
                    ...current,
                    entries: current.entries.map((entry) =>
                      entry.name === name ? { name, status: "error" } : entry,
                    ),
                  },
            );
        });
    }
    return () => controller.abort();
    // The serialized key includes all request inputs; draft text never fetches.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, retry, bars]);

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = {} as Parameters;
    for (const name of INDICATOR_NAMES) {
      next[name] = {};
      for (const [field, value] of Object.entries(draft[name])) {
        if (!/^\d+$/.test(value) || Number(value) < 1 || Number(value) > 250) {
          setValidation("所有期數必須為 1–250 的整數。");
          return;
        }
        next[name][field] = Number(value);
      }
    }
    if (next.macd.fast_period >= next.macd.slow_period) {
      setValidation("MACD 快線期數必須小於慢線期數。");
      return;
    }
    setValidation("");
    setParameters(next);
  }

  const entries =
    result.key === key && result.bars === bars ? result.entries : [];
  const responses = useMemo(
    () =>
      result.key === key && result.bars === bars
        ? result.entries.flatMap((entry) =>
            entry.status === "ready" && entry.data ? [entry.data] : [],
          )
        : [],
    [result, key, bars],
  );
  return (
    <>
      <section className={styles.controls} aria-label="技術指標設定">
        <div className={styles.heading}>
          <span>技術指標</span>
          <small>未還原價格 · 期數以 K 棒計</small>
        </div>
        <div className={styles.toggles}>
          {INDICATOR_NAMES.map((name) => (
            <label key={name} className={enabled[name] ? styles.active : ""}>
              <input
                type="checkbox"
                checked={enabled[name]}
                onChange={(event) =>
                  setEnabled((current) => ({
                    ...current,
                    [name]: event.target.checked,
                  }))
                }
              />
              {LABELS[name]}
              {name !== "macd" && <small>{parameters[name].period}</small>}
            </label>
          ))}
        </div>
        <details className={styles.settings}>
          <summary>指標參數</summary>
          <form onSubmit={apply}>
            <div className={styles.fields}>
              {INDICATOR_NAMES.map((name) => (
                <fieldset key={name}>
                  <legend>{LABELS[name]}</legend>
                  {Object.entries(draft[name]).map(([field, value]) => (
                    <label key={field}>
                      {FIELD_LABELS[field]}
                      <input
                        aria-label={`${LABELS[name]} ${FIELD_LABELS[field]}`}
                        type="number"
                        min="1"
                        max="250"
                        step="1"
                        required
                        value={value}
                        onChange={(event) => {
                          setValidation("");
                          setDraft((current) => ({
                            ...current,
                            [name]: {
                              ...current[name],
                              [field]: event.target.value,
                            },
                          }));
                        }}
                      />
                    </label>
                  ))}
                </fieldset>
              ))}
            </div>
            {validation && (
              <p role="alert" className={styles.error}>
                {validation}
              </p>
            )}
            <div className={styles.formFooter}>
              <span>
                EMA 以 SMA 初始化；RSI 採 Wilder 平滑；MACD 柱值為 MACD −
                Signal。
              </span>
              <button type="submit">套用參數</button>
            </div>
          </form>
        </details>
        {!selected.length && (
          <p className={styles.hint}>
            選擇指標以顯示計算結果。資料不足時保留暖機空值，不補造曲線。
          </p>
        )}
        {selected.length > 0 && bars !== null && bars.length === 0 && (
          <p className={styles.hint}>目前沒有行情，無法計算指標。</p>
        )}
        {entries.map((entry) =>
          entry.status === "loading" ? (
            <p key={entry.name} role="status" className={styles.hint}>
              正在計算 {LABELS[entry.name]}…
            </p>
          ) : entry.status === "error" ? (
            <div key={entry.name} role="alert" className={styles.error}>
              {LABELS[entry.name]} 暫時無法載入，價格圖仍可使用。
              <button type="button" onClick={() => setRetry((n) => n + 1)}>
                重試指標
              </button>
            </div>
          ) : entry.data ? (
            <div key={entry.name} className={styles.result}>
              <strong>
                {LABELS[entry.name]}{" "}
                <small>v{entry.data.algorithm_version}</small>
              </strong>
              <span>
                {entry.data.points.at(-1)?.status === "ready"
                  ? Object.entries(entry.data.points.at(-1)!.values)
                      .map(
                        ([name, value]) =>
                          `${VALUE_LABELS[name] ?? name} ${decimal(value)}${entry.name === "volume_sma" ? " 股" : ""}`,
                      )
                      .join(" · ")
                  : entry.data.points.at(-1)?.status === "missing_input"
                    ? "遇到已知缺值，計算已重置"
                    : `暖機中：需要 ${entry.data.required_samples} 根連續有效 K 棒`}
              </span>
              <small>
                前置序列 {entry.data.calculation.prehistory_count} 期 ·
                歷史完整度未驗證
                {entry.data.points.at(-1)?.is_provisional
                  ? " · 期間尚未完成"
                  : ""}
              </small>
            </div>
          ) : null,
        )}
      </section>
      {bars === null ? (
        placeholder
      ) : bars.length ? (
        <StockChart bars={bars} timeframe={timeframe} indicators={responses} />
      ) : (
        <div role="status" className="empty-state">
          <span className="state-icon" aria-hidden="true">
            ∅
          </span>
          <h3>此範圍沒有行情資料</h3>
          <p>不補零、不以前收價填補非交易日，也不產生示範 K 線。</p>
        </div>
      )}
    </>
  );
}
