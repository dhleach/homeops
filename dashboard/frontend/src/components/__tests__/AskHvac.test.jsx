import { render, screen, fireEvent } from "@testing-library/react";
import { vi, beforeEach } from "vitest";
import { AskHvac } from "../AskHvac.jsx";

// ---------------------------------------------------------------------------
// Mock useAsk hook
// ---------------------------------------------------------------------------

const mockAsk = vi.fn();
const mockReset = vi.fn();

let mockState = { answer: null, loading: false, error: null };

vi.mock("../../hooks/useAsk.js", () => ({
  useAsk: () => ({
    ask: mockAsk,
    reset: mockReset,
    answer: mockState.answer,
    loading: mockState.loading,
    error: mockState.error,
  }),
}));

beforeEach(() => {
  mockAsk.mockReset();
  mockReset.mockReset();
  mockState = { answer: null, loading: false, error: null };
});

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("AskHvac", () => {
  it("renders header and subtitle", () => {
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    expect(screen.getByText("Ask HomeOps")).toBeInTheDocument();
    expect(screen.getByText(/Powered by Gemini/)).toBeInTheDocument();
  });

  it("renders all 3 suggested question chips", () => {
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    expect(screen.getByText("Is my HVAC behaving normally?")).toBeInTheDocument();
    expect(screen.getByText("Which floor ran the most today?")).toBeInTheDocument();
    expect(screen.getByText("Is floor 2 running too long?")).toBeInTheDocument();
  });

  it("clicking a chip submits immediately", () => {
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    const chip = screen.getByText("Is my HVAC behaving normally?");
    fireEvent.click(chip);
    expect(mockAsk).toHaveBeenCalledWith("Is my HVAC behaving normally?");
  });

  it("shows spinner and loading text while loading", () => {
    mockState = { answer: null, loading: true, error: null };
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    expect(screen.getByText("Analyzing your HVAC data…")).toBeInTheDocument();
  });

  it("renders answer text when answer is provided", () => {
    mockState = { answer: "Floor 2 ran 1h 12m today — within normal range.", loading: false, error: null };
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    expect(screen.getByText("Floor 2 ran 1h 12m today — within normal range.")).toBeInTheDocument();
  });

  it("renders error text when error is provided", () => {
    mockState = { answer: null, loading: false, error: "GEMINI_API_KEY not configured" };
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    expect(screen.getByText("GEMINI_API_KEY not configured")).toBeInTheDocument();
  });

  it("Ask button is disabled while loading", () => {
    mockState = { answer: null, loading: true, error: null };
    render(<AskHvac apiUrl="https://api.homeops.now" />);
    const btn = screen.getByRole("button", { name: /…/ });
    expect(btn).toBeDisabled();
  });
});
