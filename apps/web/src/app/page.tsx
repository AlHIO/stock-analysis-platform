import { SystemStatus } from "../components/system-status";
import { getReadiness } from "../lib/health";
import { StockSearch } from "../components/stock-search";
export const dynamic = "force-dynamic";
export default async function Home() {
  const health = await getReadiness(
    process.env.BACKEND_URL ?? "http://localhost:8000",
  );
  return (
    <main className="page-container home-page">
      <section className="home-hero">
        <div className="hero-copy">
          <p className="eyebrow">TAIWAN EQUITY RESEARCH</p>
          <h1>
            從一檔股票，
            <br />
            看見<span>價格的脈絡。</span>
          </h1>
          <p className="hero-description">
            搜尋台灣上市與上櫃股票，檢視已收錄的 K
            線與成交量。每一筆行情，都保留來源與資料日期。
          </p>
          <div className="hero-tags">
            <span>日 / 週 / 月 K 線</span>
            <span>官方來源匯入</span>
            <span>成交量以股計</span>
          </div>
        </div>
        <div className="search-card">
          <div className="search-card-heading">
            <span className="small-label">FIND AN INSTRUMENT</span>
            <span className="search-card-number">01 / SEARCH</span>
          </div>
          <h2>尋找你的研究標的</h2>
          <p>輸入股票代號或公司名稱</p>
          <StockSearch />
          <div className="search-card-footnote">
            <span className="status-dot" />
            直接查詢已收錄資料，不觸發外部行情抓取
          </div>
        </div>
      </section>
      <section className="reading-guide" aria-label="使用說明">
        <div>
          <span className="guide-number">01</span>
          <h2>搜尋股票</h2>
          <p>依代號、名稱與市場找到標的，上市與上櫃清楚區分。</p>
        </div>
        <div>
          <span className="guide-number">02</span>
          <h2>閱讀價格與成交量</h2>
          <p>切換日、週、月 K 線，拖曳與縮放檢視已保存的行情。</p>
        </div>
        <div>
          <span className="guide-number">03</span>
          <h2>確認資料邊界</h2>
          <p>留意所屬交易日、來源與完整度，不將歷史快照當成即時報價。</p>
        </div>
      </section>
      <details className="system-details">
        <summary>
          <span
            className={`status-dot ${health?.status === "ok" ? "" : "status-warning"}`}
          />
          系統服務狀態
          <span>
            {health?.status === "ok"
              ? "所有基礎服務已連線"
              : "部分服務尚未就緒"}
          </span>
        </summary>
        <SystemStatus health={health} />
        <p className="state-note">狀態於頁面載入時檢查，重新整理可更新結果。</p>
      </details>
    </main>
  );
}
