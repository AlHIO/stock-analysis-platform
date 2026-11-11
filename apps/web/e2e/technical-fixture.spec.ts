/**
 * Explicitly synthetic browser-only fixture. Requests are intercepted before the
 * proxy; no PostgreSQL writes, provider calls or production fixtures are used.
 * Golden values are fixed independently from the frontend rendering code.
 */
import { expect, test } from "@playwright/test";
import type {
  Bar,
  BarsResponse,
  LatestResponse,
} from "../src/lib/market-types";
import type {
  IndicatorName,
  IndicatorResponse,
} from "../src/lib/technical-types";
import {
  barFixture,
  instrumentFixture,
  latestFixture,
} from "../tests/fixtures/market-api";
import { indicatorFixture } from "../tests/fixtures/technical";
import {
  institutionalHistoryFixture,
  institutionalMetricsFixture,
} from "../tests/fixtures/chip";

const instrument = {
  ...instrumentFixture,
  symbol: "SYNTHETIC",
  name: "合成測試股票（非真實行情）",
  market: {
    ...instrumentFixture.market,
    code: "TEST",
    name: "合成測試市場（非真實）",
  },
};
const dates = [
  "2026-01-01",
  "2026-01-02",
  "2026-01-03",
  "2026-01-04",
  "2026-01-05",
  "2026-01-06",
];
const prices = ["1", "2", "3", "6", "5", "10"];
const volumes = [100, 200, 300, 600, 500, 1000];
const source = {
  ...barFixture.sources[0],
  code: "SYNTHETIC_TEST",
  name: "合成測試來源（非真實行情）",
};
const bars: Bar[] = dates.map((trade_date, index) => ({
  ...barFixture,
  trade_date,
  period_start: trade_date,
  period_end: trade_date,
  first_trade_date: trade_date,
  last_trade_date: trade_date,
  open: prices[index],
  high: prices[index],
  low: prices[index],
  close: prices[index],
  volume_shares: volumes[index],
  turnover: null,
  trade_count: null,
  sources: [source],
  calendar_missing_dates: [trade_date],
  last_ingested_at: "2026-01-06T12:00:00Z",
}));
const latest: LatestResponse = {
  ...latestFixture,
  instrument,
  bar: bars.at(-1)!,
  freshness: {
    ...latestFixture.freshness,
    as_of: "2026-01-06T12:00:00Z",
    market_local_date: dates[5],
    latest_trade_date: dates[5],
    last_ingested_at: "2026-01-06T12:00:00Z",
    age_calendar_days: 0,
  },
  history: {
    available_from: dates[0],
    available_to: dates[5],
    completeness: "not_verified",
  },
};
const goldens: Record<IndicatorName, Record<string, (string | null)[]>> = {
  sma: {
    sma: [
      null,
      null,
      "2.000000000000",
      "3.666666666667",
      "4.666666666667",
      "7.000000000000",
    ],
  },
  ema: {
    ema: [
      null,
      null,
      "2.000000000000",
      "4.000000000000",
      "4.500000000000",
      "7.250000000000",
    ],
  },
  rsi: {
    rsi: [
      null,
      null,
      null,
      "100.000000000000",
      "76.923076923077",
      "91.549295774648",
    ],
  },
  volume_sma: {
    volume_sma: [
      null,
      null,
      "200.000000000000",
      "366.666666666667",
      "466.666666666667",
      "700.000000000000",
    ],
  },
  macd: {
    macd: [
      null,
      null,
      "0.500000000000",
      "0.833333333333",
      "0.444444444444",
      "1.064814814815",
    ],
    signal: [
      null,
      null,
      null,
      "0.666666666667",
      "0.518518518519",
      "0.882716049383",
    ],
    histogram: [
      null,
      null,
      null,
      "0.166666666667",
      "-0.074074074074",
      "0.182098765432",
    ],
  },
};

test("synthetic fixture renders nonempty SMA EMA RSI MACD and volume average", async ({
  page,
}, testInfo) => {
  const errors: string[] = [];
  const external: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (/twse\.com|tpex\.org/.test(request.url())) external.push(request.url());
  });
  await page.route(
    `**/api/market/stocks/${instrument.id}/**`,
    async (route) => {
      const url = new URL(route.request().url());
      // The stock page also mounts the chip panel. Keep this numerical indicator
      // test offline and explicitly empty instead of passing synthetic IDs to DB.
      if (url.pathname.endsWith("/institutional")) {
        const fixture = institutionalHistoryFixture();
        return route.fulfill({ json: {
          ...fixture, instrument, items: [], count: 0,
          from: url.searchParams.get("from"), to: url.searchParams.get("to"),
          coverage: { ...fixture.coverage, stored_from: null, stored_to: null, stored_report_count: 0 },
          freshness: { ...fixture.freshness, latest_trade_date: null, latest_as_of_date: null, assessment: "no_data" },
        } });
      }
      if (url.pathname.endsWith("/institutional/metrics")) {
        const fixture = institutionalMetricsFixture(url.searchParams.get("category") ?? "foreign_ex_dealers");
        return route.fulfill({ json: {
          ...fixture, instrument, count: 0, daily: null,
          from: url.searchParams.get("from"), to: url.searchParams.get("to"),
          cumulative: fixture.cumulative.map((point) => ({ ...point, value: null, status: "missing_data", input_refs: [], session_dates: [] })),
          streak: { ...fixture.streak, buy_count: null, sell_count: null, buy_lower_bound: 0, sell_lower_bound: 0, status: "missing_data", input_refs: [], session_dates: [] },
          coverage: { ...fixture.coverage, stored_from: null, stored_to: null, stored_report_count: 0 },
          freshness: { ...fixture.freshness, latest_trade_date: null, latest_as_of_date: null, assessment: "no_data" },
        } });
      }
      if (url.pathname.endsWith("/latest"))
        return route.fulfill({ json: latest });
      if (url.pathname.endsWith("/bars")) {
        expect(url.searchParams.get("timeframe")).toBe("1d");
        const body: BarsResponse = {
          ...latest,
          timeframe: "1d",
          from: url.searchParams.get("from")!,
          to: url.searchParams.get("to")!,
          bars,
          count: bars.length,
          query_limit: 1000,
        };
        return route.fulfill({ json: body });
      }
      if (url.pathname.endsWith("/indicators")) {
        const name = url.searchParams.get("indicator") as IndicatorName;
        expect(name in goldens).toBeTruthy();
        expect(url.searchParams.get("timeframe")).toBe("1d");
        const parameters = JSON.parse(url.searchParams.get("parameters")!);
        expect(parameters).toEqual(
          name === "macd"
            ? { fast_period: 2, slow_period: 3, signal_period: 2 }
            : { period: 3 },
        );
        const required = name === "macd" || name === "rsi" ? 4 : 3;
        const body: IndicatorResponse = indicatorFixture(name, {
          instrument,
          freshness: latest.freshness,
          history: latest.history,
          parameters,
          from: url.searchParams.get("from")!,
          to: url.searchParams.get("to")!,
          count: dates.length,
          required_samples: required,
          points: dates.map((trade_date, index) => ({
            trade_date,
            values: Object.fromEntries(
              Object.entries(goldens[name]).map(([key, values]) => [
                key,
                values[index],
              ]),
            ),
            status: index + 1 < required ? "warmup" : "ready",
            is_provisional: false,
          })),
          calculation: {
            seed_policy: "full_available_history",
            history_from: dates[0],
            input_count: dates.length,
            prehistory_count: 0,
            missing_policy: "reset_on_explicit_missing",
            calendar_policy: "observed_sessions_with_known_gaps",
            history_completeness: "not_verified",
          },
        });
        return route.fulfill({ json: body });
      }
      throw new Error(`Unexpected synthetic route: ${url.pathname}`);
    },
  );
  await page.goto(`/stocks/${instrument.id}`);
  await expect(
    page.getByRole("heading", {
      name: new RegExp("合成測試股票（非真實行情）"),
    }),
  ).toBeVisible();
  await expect(page.getByLabel("日 K與成交量圖")).toBeVisible();
  await page.getByText("指標參數", { exact: true }).click();
  for (const label of ["SMA 期數", "EMA 期數", "RSI 期數", "成交量均線 期數"])
    await page.getByLabel(label, { exact: true }).fill("3");
  await page.getByLabel("MACD 快線期數", { exact: true }).fill("2");
  await page.getByLabel("MACD 慢線期數", { exact: true }).fill("3");
  await page.getByLabel("MACD 訊號期數", { exact: true }).fill("2");
  await page.getByRole("button", { name: "套用參數", exact: true }).click();
  for (const name of [/^SMA/, /^EMA/, /^RSI/, /^MACD/, /^成交量均線/])
    await page.getByRole("checkbox", { name }).check();
  const legend = page.getByLabel("已顯示技術指標");
  for (const exact of [
    "SMA(3)7",
    "EMA(3)7.25",
    "RSI(3)91.549295774648",
    "VOL MA(3)700",
    "MACD 1.064814814815",
    "SIGNAL 0.882716049383",
    "HISTOGRAM 0.182098765432",
  ])
    await expect(legend).toContainText(exact);
  await expect(page.getByText("載入圖表…", { exact: true })).toHaveCount(0);
  expect(
    await page.getByLabel("日 K與成交量圖").locator("canvas").count(),
  ).toBeGreaterThanOrEqual(8);
  // Next.js owns a separate, empty route-announcer alert outside <main>.
  await expect(page.getByRole("main").getByRole("alert")).toHaveCount(0);
  await expect(page.locator('[data-subpanes="2"]')).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("synthetic-indicators.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
  expect(external).toEqual([]);
});
