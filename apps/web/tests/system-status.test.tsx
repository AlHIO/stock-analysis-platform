import React from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { SystemStatus } from "../src/components/system-status";
afterEach(cleanup);
it("shows all connected services", () => {
  render(<SystemStatus health={{ status: "ok", dependencies: { postgres: "ok", redis: "ok" } }} />);
  expect(screen.getAllByText("已連線")).toHaveLength(3);
});
it("shows the offline state", () => {
  render(<SystemStatus health={null} />);
  expect(screen.getByText("無法連線")).toBeInTheDocument();
  expect(screen.getAllByText("尚未就緒")).toHaveLength(2);
});
