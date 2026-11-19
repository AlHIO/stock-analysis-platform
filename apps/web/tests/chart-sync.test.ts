/** SYNTHETIC date interaction only; this registry never calculates prices. */
import { describe, expect, it, vi } from "vitest";
import { connectChartSync, overlappingChartRange } from "../src/lib/chart-sync";

describe("chart date synchronization", () => {
  it("shares actual dates and time windows without echoing to the sender", () => {
    const a = { cursor: vi.fn(), range: vi.fn() };
    const b = { cursor: vi.fn(), range: vi.fn() };
    const first = connectChartSync("SYNTHETIC-1", a);
    const second = connectChartSync("SYNTHETIC-1", b);
    first.publishCursor("2026-01-05");
    first.publishRange({ from: "2026-01-02", to: "2026-01-09" });
    expect(a.cursor).not.toHaveBeenCalled();
    expect(b.cursor).toHaveBeenCalledWith("2026-01-05");
    expect(b.range).toHaveBeenCalledWith({ from: "2026-01-02", to: "2026-01-09" });
    first.publishCursor(null);
    expect(b.cursor).toHaveBeenLastCalledWith(null);
    first.dispose(); second.dispose();
  });

  it("prevents recursive programmatic events and deduplicates the same date", () => {
    const a = { cursor: vi.fn(), range: vi.fn() };
    const first = connectChartSync("SYNTHETIC-loop", a);
    let second = connectChartSync("SYNTHETIC-loop", {
      cursor: value => first.publishCursor(value),
      range: value => first.publishRange(value),
    });
    first.publishCursor("2026-01-05");
    first.publishCursor("2026-01-05");
    expect(a.cursor).not.toHaveBeenCalled();
    second.dispose(); first.dispose();
  });

  it("replays the user's current window to newly enabled panes, and frees a closed group", () => {
    const first = connectChartSync("SYNTHETIC-new-pane", { cursor() {}, range() {} });
    first.publishRange({ from: "2026-01-02", to: "2026-01-09" });
    first.publishCursor("2026-01-05");
    const peer = { cursor: vi.fn(), range: vi.fn() };
    const second = connectChartSync("SYNTHETIC-new-pane", peer);
    expect(peer.range).toHaveBeenCalledOnce();
    expect(peer.cursor).toHaveBeenCalledWith("2026-01-05");
    first.dispose(); second.dispose();
    const third = connectChartSync("SYNTHETIC-new-pane", { cursor: peer.cursor, range: peer.range });
    expect(peer.range).toHaveBeenCalledOnce();
    third.dispose();
  });

  it("isolates instruments, invalid dates and disabled synchronization", () => {
    const peer = { cursor: vi.fn(), range: vi.fn() };
    const first = connectChartSync("SYNTHETIC-other-1", { cursor() {}, range() {} });
    const second = connectChartSync("SYNTHETIC-other-2", peer);
    const disabled = connectChartSync(undefined, peer);
    first.publishCursor("2026-02-30"); first.publishCursor("2026-01-05");
    first.publishRange({ from: "2026-01-09", to: "2026-01-02" });
    disabled.publishCursor("2026-01-02");
    expect(peer.cursor).not.toHaveBeenCalled();
    expect(peer.range).not.toHaveBeenCalled();
    first.dispose(); second.dispose(); disabled.dispose();
  });

  it("clamps time windows without inventing a logical index or weekly data", () => {
    expect(overlappingChartRange({ from: "2026-01-02", to: "2026-01-20" }, "2026-01-09", "2026-01-16"))
      .toEqual({ from: "2026-01-09", to: "2026-01-16" });
    expect(overlappingChartRange({ from: "2026-01-02", to: "2026-01-05" }, "2026-01-09", "2026-01-16")).toBeNull();
  });
});
