/**
 * What the corpus holds, for the Overview's data plate and charts and the
 * shell. Counts and year spans are those of the latest snapshot, as
 * reported by the pipelines; a null means not counted yet.
 */

export const snapshot = "2026-10-23";

export type Dataset = {
  name: string;
  title: string;
  /** Rows in the processed table, or null if not counted yet. */
  rows: number | null;
  /** Whether `rows` is rounded (the SDR count is). */
  approximate?: boolean;
  /** First and last year covered, when known. */
  span?: [number, number];
};

/** The processed datasets, in the order they were added. */
export const datasets: Dataset[] = [
  { name: "ecfr-title14", title: "14 CFR, Aeronautics and Space", rows: 6357 },
  { name: "faa-ads", title: "Airworthiness directives", rows: 20184, span: [1946, 2026] },
  { name: "faa-proposed-rules", title: "Proposed rules", rows: 8838 },
  { name: "faa-saibs", title: "Special Airworthiness Information Bulletins", rows: 1339 },
  { name: "faa-advisory-circulars", title: "Advisory circulars", rows: 1686 },
  { name: "faa-maintenance-alerts", title: "Aviation Maintenance Alerts", rows: 109, span: [2004, 2012] },
  { name: "faa-sdrs", title: "Service Difficulty Reports", rows: 1_770_000, approximate: true, span: [1995, 2026] },
  { name: "ntsb-narratives", title: "Accident and incident narratives", rows: 90108, span: [1982, 2026] },
  { name: "ntsb-findings", title: "Accident findings and event sequences", rows: null },
];

/** Tables the shell can query today; the rest are listed as coming. */
export const shellTables = [
  { name: "ads", dataset: "faa-ads", src: `/data/processed/faa-ads/v${snapshot}/ad-final-rules.parquet` },
];

/** Run when the shell starts, so it opens on real data. */
export const shellSample = `SELECT ad_number, effective_date, subject
FROM ads
ORDER BY effective_date DESC
LIMIT 5;`;

/** Shown in the empty prompt. */
export const shellPlaceholder = "SELECT count(*) FROM ads;";

export function formatCount(n: number, approximate = false): string {
  if (approximate) return `~${(n / 1e6).toFixed(2)}M`;
  return n.toLocaleString("en-US");
}

const counted = datasets.filter((d) => d.rows !== null);
const records = counted.reduce((sum, d) => sum + (d.rows ?? 0), 0);
const spans = datasets.flatMap((d) => d.span ?? []);

/** The data plate's fields. Tokens are not counted yet. */
export const plate: { key: string; value: string }[] = [
  { key: "Snapshot", value: snapshot },
  { key: "Datasets", value: String(datasets.length) },
  { key: "Records", value: `~${(records / 1e6).toFixed(1)}M` },
  { key: "Tokens", value: "—" },
  { key: "Sources", value: "FAA · eCFR · NTSB" },
  { key: "Span", value: `${Math.min(...spans)}–${Math.max(...spans)}` },
  { key: "Format", value: "Parquet" },
  { key: "License", value: "CC BY 4.0" },
];
