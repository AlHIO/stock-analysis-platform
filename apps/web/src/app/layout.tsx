import type { Metadata } from "next";
import "./globals.css";
import { SiteHeader } from "../components/site-header";
export const metadata: Metadata = {
  title: "Stock Analysis · 台灣股票研究",
  description:
    "搜尋上市與上櫃股票，檢視已收錄日、週、月 K 線與成交量。非即時報價。",
};
export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-Hant">
      <body>
        <a className="skip-link" href="#main-content">
          跳至主要內容
        </a>
        <SiteHeader />
        <div id="main-content">{children}</div>
        <footer className="site-footer">
          <span>Stock Analysis Platform</span>
          <span>已收錄行情 · 非即時報價 · 歷史完整度未驗證</span>
        </footer>
      </body>
    </html>
  );
}
