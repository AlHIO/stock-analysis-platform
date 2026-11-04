import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Stock Analysis Platform", description: "台灣股票分析平台 · Phase 0" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-Hant"><body>{children}</body></html>;
}
