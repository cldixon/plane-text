/** Site-wide content and metadata. */
export const site = {
  title: "Plane-text",
  description:
    "An open-source corpus of aviation maintenance text. Regulations, airworthiness directives and other public documents, gathered, cleaned and published as datasets for research and development.",
  tagline: "An open corpus of aviation maintenance text, built only from public sources.",
} as const;

/** A link in the masthead's link row. With no href it shows as not yet available. */
export type ProjectLink = { label: string; href?: string; download?: boolean };

export const links: ProjectLink[] = [
  // The latest release: the Worker redirects /download to its zip.
  { label: "Download", href: "/download", download: true },
  { label: "GitHub", href: "https://github.com/cldixon/plane-text" },
  { label: "Hugging Face" },
  { label: "cldixon.com", href: "https://cldixon.com" },
];
