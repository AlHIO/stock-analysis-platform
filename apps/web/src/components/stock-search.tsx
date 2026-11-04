"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ApiError, searchInstruments } from "../lib/market-api";
import type { Instrument } from "../lib/market-types";

const markets = [
  { code: "", label: "全部市場" },
  { code: "TWSE", label: "上市" },
  { code: "TPEX", label: "上櫃" },
];

type SearchState = "idle" | "loading" | "success" | "error";

export function StockSearch({ compact = false }: { compact?: boolean }) {
  const router = useRouter();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const [query, setQuery] = useState("");
  const [market, setMarket] = useState("");
  const [items, setItems] = useState<Instrument[]>([]);
  const [state, setState] = useState<SearchState>("idle");
  const [error, setError] = useState("");
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [composing, setComposing] = useState(false);
  const [retry, setRetry] = useState(0);
  const [page, setPage] = useState(1);
  const [pagination, setPagination] = useState({ total: 0, totalPages: 0 });

  function clearSuggestions() {
    setItems([]);
    setActiveIndex(-1);
    setState("loading");
  }

  function movePage(next: number) {
    clearSuggestions();
    setPage(next);
    inputRef.current?.focus();
  }

  useEffect(() => {
    const value = query.trim();
    setItems([]);
    setActiveIndex(-1);
    if (!value || composing) {
      setState("idle");
      return;
    }
    const controller = new AbortController();
    let cancelled = false;
    setState("loading");
    setError("");
    const timer = setTimeout(async () => {
      try {
        const result = await searchInstruments(
          value,
          market,
          controller.signal,
          page,
        );
        if (cancelled) return;
        setItems(
          result.items.filter((item) => item.instrument_type === "stock"),
        );
        setPagination({ total: result.total, totalPages: result.total_pages });
        setState("success");
      } catch (cause) {
        if (cancelled) return;
        setError(
          cause instanceof ApiError && cause.status < 500
            ? "搜尋未完成，請確認輸入後再試一次。"
            : "行情 API 暫時無法連線，請稍後重試。",
        );
        setState("error");
      }
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [query, market, page, composing, retry]);

  const expanded = open && Boolean(query.trim()) && !composing;

  function select(index: number) {
    const item = items[index];
    if (!item) return;
    setOpen(false);
    router.push(`/stocks/${item.id}`);
  }

  return (
    <div
      className={`stock-search${compact ? " is-compact" : ""}`}
      role="search"
      aria-label="股票搜尋"
    >
      <div className="market-tabs" role="group" aria-label="市場別篩選">
        {markets.map((item) => (
          <button
            key={item.code}
            type="button"
            aria-pressed={market === item.code}
            onClick={() => {
              if (market !== item.code || page !== 1) clearSuggestions();
              setMarket(item.code);
              setPage(1);
              setOpen(true);
              inputRef.current?.focus();
            }}
          >
            {item.label}
          </button>
        ))}
      </div>
      <div className="search-field">
        <svg
          viewBox="0 0 24 24"
          width="21"
          height="21"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.7"
          aria-hidden="true"
        >
          <circle cx="10.8" cy="10.8" r="6.6" />
          <path d="m16 16 4.5 4.5" />
        </svg>
        <label className="sr-only" htmlFor={`${listId}-input`}>
          股票代號或名稱
        </label>
        <input
          ref={inputRef}
          id={`${listId}-input`}
          className="search-input"
          type="text"
          role="combobox"
          aria-autocomplete="list"
          aria-expanded={expanded}
          aria-controls={expanded ? listId : undefined}
          aria-activedescendant={
            expanded && activeIndex >= 0
              ? `${listId}-${activeIndex}`
              : undefined
          }
          autoComplete="off"
          spellCheck={false}
          maxLength={100}
          value={query}
          placeholder="輸入股票代號或名稱，例如 2330、台積電"
          onChange={(event) => {
            clearSuggestions();
            setQuery(event.target.value);
            setPage(1);
            setOpen(true);
          }}
          onFocus={() => setOpen(true)}
          onBlur={(event) => {
            if (
              !event.currentTarget.parentElement?.parentElement?.contains(
                event.relatedTarget as Node | null,
              )
            )
              setOpen(false);
          }}
          onCompositionStart={() => setComposing(true)}
          onCompositionEnd={() => setComposing(false)}
          onKeyDown={(event) => {
            if (composing || event.nativeEvent.isComposing) return;
            if (event.key === "Escape") {
              setOpen(false);
              setActiveIndex(-1);
            }
            if (
              event.key === "ArrowDown" &&
              state === "success" &&
              items.length
            ) {
              event.preventDefault();
              setOpen(true);
              setActiveIndex((index) => (index + 1) % items.length);
            }
            if (
              event.key === "ArrowUp" &&
              state === "success" &&
              items.length
            ) {
              event.preventDefault();
              setOpen(true);
              setActiveIndex((index) =>
                index <= 0 ? items.length - 1 : index - 1,
              );
            }
            if (
              event.key === "Enter" &&
              expanded &&
              state === "success" &&
              items.length
            ) {
              event.preventDefault();
              select(activeIndex < 0 ? 0 : activeIndex);
            }
          }}
        />
        <span className="search-shortcut" aria-hidden="true">
          <svg
            viewBox="0 0 16 16"
            width="13"
            height="13"
            fill="none"
            stroke="currentColor"
          >
            <path d="M12 3v6H4m3-3L4 9l3 3" />
          </svg>
        </span>
      </div>
      {expanded && (
        <div className="search-results">
          {state === "loading" && (
            <p className="state-note" role="status">
              搜尋中…
            </p>
          )}
          {state === "error" && (
            <div className="state-note" role="alert">
              <p>{error}</p>
              <button
                type="button"
                onClick={() => {
                  clearSuggestions();
                  setRetry((value) => value + 1);
                }}
              >
                重新搜尋
              </button>
            </div>
          )}
          {state === "success" && !items.length && (
            <p className="state-note" role="status">
              {pagination.total > 0
                ? "本頁沒有股票。搜尋結果包含其他證券，請切換頁面或縮小搜尋條件。"
                : "找不到符合條件的股票，請試試其他代號、名稱或市場。"}
            </p>
          )}
          <ul id={listId} role="listbox" aria-label="股票搜尋建議">
            {items.map((item, index) => (
              <li key={item.id} role="presentation">
                <Link
                  id={`${listId}-${index}`}
                  href={`/stocks/${item.id}`}
                  role="option"
                  tabIndex={-1}
                  aria-selected={activeIndex === index}
                  className={`search-option${activeIndex === index ? " is-active" : ""}`}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseMove={() => setActiveIndex(index)}
                  onClick={() => setOpen(false)}
                >
                  <span className="search-symbol">{item.symbol}</span>
                  <span className="search-name">{item.name}</span>
                  <span className="search-market">
                    {item.market.code === "TWSE"
                      ? "上市"
                      : item.market.code === "TPEX"
                        ? "上櫃"
                        : item.market.code}
                  </span>
                  <span aria-hidden="true">↗</span>
                </Link>
              </li>
            ))}
          </ul>
          {state === "success" && pagination.total > 0 && (
            <div className="search-footer">
              <p className="m-0">
                本頁 {items.length} 檔股票 · 證券結果共 {pagination.total}{" "}
                筆，股票以外類型不顯示
              </p>
              {items.length > 0 && (
                <p className="mt-1 mb-0">↑ ↓ 選擇，Enter 開啟</p>
              )}
              {pagination.totalPages > 1 && (
                <nav
                  className="mt-3 flex items-center justify-between gap-2"
                  aria-label="搜尋結果分頁"
                >
                  <button
                    type="button"
                    className="rounded border border-slate-600 px-2 py-1 text-slate-300 disabled:opacity-30"
                    disabled={page <= 1}
                    onClick={() => movePage(page - 1)}
                  >
                    上一頁
                  </button>
                  <span>
                    第 {page} / {pagination.totalPages} 頁
                  </span>
                  <button
                    type="button"
                    className="rounded border border-slate-600 px-2 py-1 text-slate-300 disabled:opacity-30"
                    disabled={page >= Math.min(pagination.totalPages, 1000)}
                    onClick={() => movePage(page + 1)}
                  >
                    下一頁
                  </button>
                </nav>
              )}
              {pagination.totalPages > 1000 && (
                <p className="mt-2 mb-0">
                  最多可瀏覽前 1000 頁，請縮小搜尋條件。
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
