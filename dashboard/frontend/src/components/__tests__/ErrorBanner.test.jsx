import { render, screen } from "@testing-library/react";
import { ErrorBanner } from "../ErrorBanner.jsx";

describe("ErrorBanner", () => {
  it("renders the error message", () => {
    render(<ErrorBanner message="API returned 503" />);
    expect(screen.getByText(/API returned 503/)).toBeInTheDocument();
  });

  it("renders the fallback text", () => {
    render(<ErrorBanner message="timeout" />);
    expect(screen.getByText(/showing last known data/)).toBeInTheDocument();
  });
});
