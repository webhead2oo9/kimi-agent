import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { LaunchScreen } from "./LaunchScreen";

it("guides failed SDK launches to reopen the Activity without reloading the iframe", () => {
  render(<LaunchScreen state="failed" message="Discord did not connect" />);
  expect(screen.getByText(/close and reopen this Activity/i)).toBeVisible();
  expect(screen.queryByRole("button", { name: /try again|reconnect/i })).not.toBeInTheDocument();
});
