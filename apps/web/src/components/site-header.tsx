import Link from "next/link";

export function SiteHeader() {
  return (
    <header className="site-header">
      <Link className="brand" href="/" aria-label="Stock Analysis 首頁">
        <span className="brand-mark" aria-hidden="true">
          <i />
          <i />
          <i />
        </span>
        <span>
          Stock<span className="brand-light"> Analysis</span>
          <small>台灣股票研究平台</small>
        </span>
      </Link>
      <div className="header-caption">
        <span className="status-dot" />
        <span className="header-data-label">已收錄日行情</span>
        <span className="header-divider" />
        <span>非即時報價</span>
      </div>
    </header>
  );
}
