/** Coordinate chart interaction only; dates never create observations or values. */
export type ChartDateRange = { from: string; to: string };
export type ChartSyncPeer = {
  cursor(date: string | null): void;
  range(value: ChartDateRange): void;
};
export type ChartSyncConnection = {
  publishCursor(date: string | null): void;
  publishRange(value: ChartDateRange | null): void;
  dispose(): void;
};

type Group = { peers: Set<ChartSyncPeer>; cursor?: string | null; range?: ChartDateRange; dispatching: boolean };
const groups = new Map<string, Group>();

function validDate(value: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const date = new Date(`${value}T00:00:00Z`);
  return Number.isFinite(date.getTime()) && date.toISOString().slice(0, 10) === value;
}

/** One live group per instrument; the final disconnect releases its state. */
export function connectChartSync(groupId: string | undefined, peer: ChartSyncPeer): ChartSyncConnection {
  if (!groupId) return { publishCursor() {}, publishRange() {}, dispose() {} };
  let group = groups.get(groupId);
  if (!group) {
    group = { peers: new Set(), dispatching: false };
    groups.set(groupId, group);
  }
  const current = group;
  current.peers.add(peer);
  let disposed = false;
  const notify = (callback: (entry: ChartSyncPeer) => void) => {
    current.dispatching = true;
    try {
      for (const target of [...current.peers]) if (target !== peer) {
        try { callback(target); } catch { /* One disposed renderer cannot break its peers. */ }
      }
    } finally { current.dispatching = false; }
  };
  // A newly enabled pane joins the current date window instead of refitting
  // the existing charts. Values still come solely from its own exact dates.
  try { if (current.range) peer.range({ ...current.range }); } catch { /* Empty renderer; future interaction can retry. */ }
  try { if (current.cursor !== undefined) peer.cursor(current.cursor); } catch { /* No exact point is a valid empty state. */ }
  return {
    publishCursor(date) {
      if (disposed || current.dispatching || date !== null && !validDate(date) || current.cursor === date) return;
      current.cursor = date;
      notify(entry => entry.cursor(date));
    },
    publishRange(value) {
      if (disposed || current.dispatching || !value || !validDate(value.from) || !validDate(value.to) || value.from >= value.to) return;
      if (current.range?.from === value.from && current.range.to === value.to) return;
      current.range = { ...value };
      notify(entry => entry.range({ ...value }));
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      current.peers.delete(peer);
      if (!current.peers.size && groups.get(groupId) === current) groups.delete(groupId);
    },
  };
}

/** Clamp calendar-time views only. Logical indexes differ across frequencies. */
export function overlappingChartRange(value: ChartDateRange, first: string, last: string): ChartDateRange | null {
  const from = value.from > first ? value.from : first;
  const to = value.to < last ? value.to : last;
  return from < to ? { from, to } : null;
}
