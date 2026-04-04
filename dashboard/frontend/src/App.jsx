import { useTemps } from "./hooks/useTemps.js";
import { TempCard } from "./components/TempCard.jsx";
import { OutdoorCard } from "./components/OutdoorCard.jsx";
import { LiveIndicator } from "./components/LiveIndicator.jsx";
import { ErrorBanner } from "./components/ErrorBanner.jsx";

const GRAFANA_BASE = import.meta.env.VITE_GRAFANA_URL ?? "https://api.homeops.now/grafana";
const GRAFANA_URL = GRAFANA_BASE;

const DASHBOARDS = [
  { uid: "homeops-temps",       title: "Floor Temperatures",              description: "Live readings — all floors + outdoor" },
  { uid: "homeops-zones",       title: "Zone Runtimes + Furnace Status",  description: "Call activity and today's runtime per floor" },
  { uid: "homeops-correlation", title: "Outdoor Temp Correlation",        description: "How cold weather drives heating demand" },
  { uid: "homeops-daily",       title: "Daily Summary + Anomalies",       description: "Session history and floor-2 long-call events" },
];
const ZONE_ORDER = ["floor_1", "floor_2", "floor_3"];

export default function App() {
  const { data, loading, error, lastUpdated, refresh } = useTemps();

  return (
    <div className="flex min-h-screen flex-col bg-surface">
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
                    hvac_action: data[`${zone}_call`] ? "heating" : (data.furnace_active ? "idle" : "idle"),
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

        {/* Grafana dashboards */}
        <section className="mt-16 border-t border-border pt-12">
          <div className="mb-8 flex items-center justify-between">
            <div>
              <h2 className="text-xl font-semibold text-white">Live Dashboards</h2>
              <p className="mt-1 text-sm text-slate-400">Powered by Prometheus + Grafana on AWS EC2</p>
            </div>
            <a
              href={`${GRAFANA_BASE}/dashboards`}
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
                  style={{ height: "300px" }}
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
              body="A Pi 4 running Home Assistant reads thermostat and outdoor sensor data every 30 seconds via a custom Python event pipeline."
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
          </div>
        </section>
      </main>

      {/* ── Footer ─────────────────────────────────────────────────────── */}
      <footer className="border-t border-border px-6 py-6">
        <div className="mx-auto flex max-w-5xl flex-col items-center justify-between gap-3 text-xs text-slate-600 sm:flex-row">
          <span>Built with React · Tailwind · FastAPI · Prometheus · Terraform</span>
          <div className="flex items-center gap-4">
            <a
              href="https://github.com/dhleach/homeops"
              target="_blank"
              rel="noopener noreferrer"
              className="transition-colors hover:text-slate-400"
            >
              GitHub →
            </a>
          </div>
        </div>
      </footer>
    </div>
  );
}

function FeatureTile({ icon, title, body }) {
  return (
    <div className="flex flex-col gap-3">
      <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-border bg-card text-xl">
        {icon}
      </div>
      <h3 className="font-semibold text-white">{title}</h3>
      <p className="text-sm leading-relaxed text-slate-400">{body}</p>
    </div>
  );
}
