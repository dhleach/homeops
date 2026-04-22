import { useTemps } from "./hooks/useTemps.js";
import { TempCard } from "./components/TempCard.jsx";
import { OutdoorCard } from "./components/OutdoorCard.jsx";
import { LiveIndicator } from "./components/LiveIndicator.jsx";
import { ErrorBanner } from "./components/ErrorBanner.jsx";
import { AskHvac } from "./components/AskHvac.jsx";

const GRAFANA_BASE = import.meta.env.VITE_GRAFANA_URL ?? "https://api.homeops.now/grafana";
const GRAFANA_URL = `${GRAFANA_BASE}/d/homeops-temps`;
const API_URL = import.meta.env.VITE_API_URL ?? "https://api.homeops.now";

const DASHBOARDS = [
  { uid: "homeops-temps",       title: "Floor Temperatures",              description: "Live readings — all floors + outdoor" },
  { uid: "homeops-zones",       title: "Zone Runtimes + Furnace Status",  description: "Call activity and today's runtime per floor" },
  { uid: "homeops-correlation", title: "Outdoor Temp Correlation",        description: "How cold weather drives heating demand" },
  { uid: "homeops-daily",       title: "Daily Summary + Anomalies",       description: "Session history and floor-2 long-call events" },
];
const ZONE_ORDER = ["floor_3", "floor_2", "floor_1"];

export default function App() {
  const { data, loading, error, lastUpdated, refresh } = useTemps();

  return (
    <div className="flex min-h-screen flex-col bg-surface">
      {/* ── Maintenance Banner ── */}
      <div className="bg-amber-500/15 border-b border-amber-500/30 px-6 py-2.5 text-center text-sm text-amber-300">
        <span className="mr-2">🔧</span>
        Scheduled maintenance in progress — live data will resume shortly.
        <span className="ml-2 text-amber-400/60">(4/22/26, 11:00 AM – 3:00 PM ET)</span>
      </div>
      {/* ── Header ─────────────────────────────────────────────────────── */}
      <header className="border-b border-border px-6 py-5">
        <div className="mx-auto flex max-w-5xl items-center justify-between">
          <div className="flex items-center gap-3">
            <span className="text-2xl">🏠</span>
            <div>
              <h1 className="text-lg font-semibold tracking-tight text-white">
                HomeOps
              </h1>
              <p className="text-xs text-slate-500">
                Live HVAC monitoring · Pittsburgh, PA
              </p>
            </div>
          </div>

          <a
            href={GRAFANA_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-2 rounded-xl border border-blue-500/40 bg-blue-500/10 px-4 py-2 text-sm font-medium text-blue-400 transition-colors hover:bg-blue-500/20 hover:text-blue-300"
          >
            View Full Dashboard
            <svg
              xmlns="http://www.w3.org/2000/svg"
              viewBox="0 0 20 20"
              fill="currentColor"
              className="h-4 w-4"
            >
              <path
                fillRule="evenodd"
                d="M5.22 14.78a.75.75 0 0 0 1.06 0l7.22-7.22v5.69a.75.75 0 0 0 1.5 0v-7.5a.75.75 0 0 0-.75-.75h-7.5a.75.75 0 0 0 0 1.5h5.69l-7.22 7.22a.75.75 0 0 0 0 1.06Z"
                clipRule="evenodd"
              />
            </svg>
          </a>
        </div>
      </header>

      {/* ── Main ───────────────────────────────────────────────────────── */}
      <main className="mx-auto w-full max-w-5xl flex-1 px-6 py-10">
        {/* Hero text */}
        <div className="mb-10 text-center">
          <h2 className="text-3xl font-bold tracking-tight text-white sm:text-4xl">
            What's the temperature right now?
          </h2>
          <p className="mt-3 text-slate-400">
            Real sensor data from a 3-zone HVAC system, updated every 30 seconds.
          </p>
        </div>

        {/* Error banner */}
        {error && !loading && <div className="mb-6"><ErrorBanner message={error} /></div>}

        {/* Loading skeleton */}
        {loading && (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
            {[...Array(4)].map((_, i) => (
              <div
                key={i}
                className="h-44 animate-pulse rounded-2xl border border-border bg-card"
              />
            ))}
          </div>
        )}

        {/* Temp cards */}
        {!loading && (
          <>
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {ZONE_ORDER.map((zone) => (
                <TempCard
                  key={zone}
                  zone={zone}
                  data={data ? {
                    current_temp_f: data[zone] ?? null,
                    hvac_action: data[`${zone}_call`] ? "heating" : "idle",
                    setpoint_f: data[`${zone}_setpoint`] ?? null,
                  } : null}
                />
              ))}
              <OutdoorCard
                temp={data?.outdoor ?? null}
                lastUpdated={data?.last_updated}
              />
            </div>

            {/* Live indicator */}
            <div className="mt-5">
              <LiveIndicator lastUpdated={lastUpdated} onRefresh={refresh} />
            </div>
          </>
        )}

        {/* AI Diagnostic */}
        <section className="mt-16 border-t border-border pt-12">
          <AskHvac apiUrl={API_URL} />
        </section>

        {/* Grafana dashboards */}
        <section className="mt-16 border-t border-border pt-12">
          <div className="mb-8 flex items-center justify-between">
            <div>
              <h2 className="text-xl font-semibold text-white">Live Dashboards</h2>
              <p className="mt-1 text-sm text-slate-400">Powered by Prometheus + Grafana on AWS EC2</p>
            </div>
            <a
              href={`${GRAFANA_BASE}/d/homeops-temps`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-xs text-slate-500 transition-colors hover:text-slate-300"
            >
              Open in Grafana →
            </a>
          </div>

          <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
            {DASHBOARDS.map(({ uid, title, description }) => (
              <div key={uid} className="overflow-hidden rounded-2xl border border-border bg-card">
                <div className="border-b border-border px-4 py-3 flex items-center justify-between">
                  <div>
                    <h3 className="text-sm font-medium text-white">{title}</h3>
                    <p className="text-xs text-slate-500">{description}</p>
                  </div>
                  <a
                    href={`${GRAFANA_BASE}/d/${uid}`}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-xs text-blue-400 hover:text-blue-300 transition-colors ml-4 shrink-0"
                  >
                    Full view ↗
                  </a>
                </div>
                <iframe
                  src={`${GRAFANA_BASE}/d/${uid}?kiosk&theme=dark&refresh=30s`}
                  title={title}
                  className="w-full border-0"
                  style={{ height: "450px" }}
                  loading="lazy"
                />
              </div>
            ))}
          </div>
        </section>

        {/* About section */}
        <section className="mt-20 border-t border-border pt-12">
          <div className="grid grid-cols-1 gap-8 sm:grid-cols-3">
            <FeatureTile
              icon="🥧"
              title="Raspberry Pi"
              body="A Pi 5 running Home Assistant reads thermostat and outdoor sensor data every 30 seconds via a custom Python event pipeline."
            />
            <FeatureTile
              icon="📊"
              title="Prometheus + Grafana"
              body="Events flow into Prometheus on AWS EC2. Grafana dashboards surface floor runtimes, duty cycles, outdoor temp correlation, and anomalies."
            />
            <FeatureTile
              icon="🔥"
              title="Overheating prevention"
              body="Floor 2's 3-vent constraint causes the furnace to overheat if calls run too long. The pipeline detects, alerts, and logs every long-call event."
            />
            <FeatureTile
              icon="⚡"
              title="REST API"
              body="Live sensor data is served as JSON by a FastAPI backend on AWS EC2, querying Prometheus for the latest floor temperatures, setpoints, and zone call states."
              link={{ href: `${API_URL}/api/current-temps`, label: "View live JSON →" }}
            />
          </div>
        </section>
      </main>

      {/* ── Builder section ────────────────────────────────────────────── */}
      <section className="border-t border-border px-6 py-16">
        <div className="mx-auto max-w-5xl">
          <div className="grid grid-cols-1 gap-12 sm:grid-cols-2">
            {/* Bio */}
            <div className="flex flex-col gap-4">
              <h2 className="text-xl font-semibold text-white">About the builder</h2>
              <p className="text-sm leading-relaxed text-slate-400">
                I'm Derek Leach — a DevOps/Release Management Engineer based in Pittsburgh, PA. I built
                HomeOps to solve a real problem: my three-zone HVAC system was triggering furnace
                overheating faults because Floor 2 only has 3 vents. The solution turned into a full
                event-driven monitoring pipeline.
              </p>
              <p className="text-sm leading-relaxed text-slate-400">
                Before software I spent 9 years as a drilling engineer at Shell Oil, where I set the
                Pennsylvania state record for footage drilled in 24 hours and designed process
                improvements that saved $15M/year. I made the switch to engineering in 2019 and have
                been building ever since.
              </p>
              <div className="mt-2 flex items-center gap-4">
                <a
                  href="https://github.com/dhleach/homeops"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:border-slate-500 hover:text-white"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4">
                    <path fillRule="evenodd" d="M12 2C6.477 2 2 6.484 2 12.017c0 4.425 2.865 8.18 6.839 9.504.5.092.682-.217.682-.483 0-.237-.008-.868-.013-1.703-2.782.605-3.369-1.343-3.369-1.343-.454-1.158-1.11-1.466-1.11-1.466-.908-.62.069-.608.069-.608 1.003.07 1.531 1.032 1.531 1.032.892 1.53 2.341 1.088 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.113-4.555-4.951 0-1.093.39-1.988 1.029-2.688-.103-.253-.446-1.272.098-2.65 0 0 .84-.27 2.75 1.026A9.564 9.564 0 0 1 12 6.844a9.59 9.59 0 0 1 2.504.337c1.909-1.296 2.747-1.027 2.747-1.027.546 1.379.202 2.398.1 2.651.64.7 1.028 1.595 1.028 2.688 0 3.848-2.339 4.695-4.566 4.942.359.31.678.921.678 1.856 0 1.338-.012 2.419-.012 2.747 0 .268.18.58.688.482A10.02 10.02 0 0 0 22 12.017C22 6.484 17.522 2 12 2z" clipRule="evenodd" />
                  </svg>
                  View source
                </a>
                <a
                  href="https://linkedin.com/in/derekleach"
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-2 rounded-lg border border-border bg-card px-4 py-2 text-sm font-medium text-slate-300 transition-colors hover:border-slate-500 hover:text-white"
                >
                  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4">
                    <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
                  </svg>
                  LinkedIn
                </a>
              </div>
            </div>

            {/* Tech stack */}
            <div className="flex flex-col gap-4">
              <h2 className="text-xl font-semibold text-white">Tech stack</h2>
              <div className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm text-slate-400">
                {[
                  ["Edge", "Raspberry Pi 5 · Home Assistant"],
                  ["Pipeline", "Python · JSONL event stream"],
                  ["Metrics", "Prometheus · custom exporters"],
                  ["Dashboards", "Grafana · 4 provisioned dashboards"],
                  ["API", "FastAPI · Pydantic · httpx"],
                  ["Frontend", "React · Vite · Tailwind CSS"],
                  ["Infra", "AWS EC2 · S3 · CloudFront · Route53"],
                  ["IaC", "Terraform · Docker Compose"],
                  ["CI/CD", "GitHub Actions · Nginx · Certbot"],
                  ["AI", "Gemini 2.0 Flash · live diagnostic Q&A"],
                ].map(([label, value]) => (
                  <div key={label}>
                    <span className="font-medium text-slate-300">{label}</span>
                    <span className="block text-slate-500">{value}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      <footer className="border-t border-border px-6 py-6">
        <div className="mx-auto flex max-w-5xl flex-col items-center justify-between gap-3 text-xs text-slate-400 sm:flex-row">
          <span>Built with React · Tailwind · FastAPI · Prometheus · Terraform</span>
          <div className="flex items-center gap-4">
            <a
              href={`${API_URL}/api/current-temps`}
              target="_blank"
              rel="noopener noreferrer"
              className="transition-colors hover:text-slate-200"
            >
              API →
            </a>
            <a
              href="https://github.com/dhleach/homeops"
              target="_blank"
              rel="noopener noreferrer"
              className="transition-colors hover:text-slate-200"
            >
              GitHub →
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}

function FeatureTile({ icon, title, body, link }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border bg-card text-xl">
        {icon}
      </div>
      <h3 className="font-semibold text-white">{title}</h3>
      <p className="text-sm leading-relaxed text-slate-400">{body}</p>
      {link && (
        <a
          href={link.href}
          target="_blank"
          rel="noopener noreferrer"
          className="text-sm text-blue-400 transition-colors hover:text-blue-300"
        >
          {link.label}
        </a>
      )}
    </div>
  );
}
