/** Site-wide content and metadata. */
export const site = {
  title: "Plane-text",
  description:
    "An open-source corpus of aviation maintenance text. Regulations, airworthiness directives and other public documents, gathered, cleaned and published as datasets for research and development.",
} as const;

export type Link = { label: string; href: string };

/**
 * Where else the project lives. Add Hugging Face or Igloo here once the data is
 * published there; the page shows whatever is listed.
 */
export const links: Link[] = [
  { label: "GitHub", href: "https://github.com/cldixon/plane-text" },
  { label: "cldixon.com", href: "https://cldixon.com" },
];

export type Dataset = {
  name: string;
  title: string;
  source: string;
  /** What one row is, and how many there are in the latest snapshot. */
  size: string;
  /** Anything a reader should know before using it. */
  note?: string;
};

/** The published datasets, in the order they were added. */
export const datasets: Dataset[] = [
  {
    name: "ecfr-title14",
    title: "14 CFR, Aeronautics and Space",
    source: "eCFR",
    size: "6,357 sections and appendices",
  },
  {
    name: "faa-ads",
    title: "Airworthiness directives",
    source: "FAA Dynamic Regulatory System",
    size: "20,184 final rules, 1946 onward",
  },
  {
    name: "faa-proposed-rules",
    title: "Proposed rules",
    source: "FAA Dynamic Regulatory System, Federal Register",
    size: "8,290 proposed ADs and 548 rulemaking notices",
  },
  {
    name: "faa-saibs",
    title: "Special Airworthiness Information Bulletins",
    source: "FAA Dynamic Regulatory System",
    size: "1,339 bulletins",
  },
  {
    name: "faa-advisory-circulars",
    title: "Advisory circulars",
    source: "FAA Dynamic Regulatory System",
    size: "1,686 circulars",
    note: "Some scanned pages still await OCR.",
  },
  {
    name: "faa-maintenance-alerts",
    title: "Aviation Maintenance Alerts",
    source: "FAA Dynamic Regulatory System",
    size: "109 monthly issues, 2004–2012",
  },
  {
    name: "faa-sdrs",
    title: "Service Difficulty Reports",
    source: "FAA SDRS",
    size: "1.77 million reports, 1995 onward",
  },
  {
    name: "ntsb-narratives",
    title: "Accident and incident narratives",
    source: "NTSB aviation databases",
    size: "90,108 narratives, 1982 onward",
  },
  {
    name: "ntsb-findings",
    title: "Accident findings and event sequences",
    source: "NTSB aviation databases",
    size: "Findings, events and causes for the same accidents",
  },
];
