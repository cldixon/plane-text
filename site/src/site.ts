/** Site-wide content and metadata. */
export const site = {
  title: "Plane-text",
  description:
    "An open-source corpus of aviation maintenance text. Regulations, airworthiness directives and other public documents, gathered, cleaned and published as datasets for research and development.",
  tagline: "An open corpus of aviation maintenance text, built only from public sources.",
} as const;

/** A link badge under the title: a muted key and its value. With no href it
 * shows as not yet available. */
export type Badge = { key: string; value: string; href?: string };

export const badges: Badge[] = [
  { key: "Code", value: "GitHub", href: "https://github.com/cldixon/plane-text" },
  { key: "Data", value: "Download" },
  { key: "Hub", value: "Hugging Face" },
  { key: "Author", value: "cldixon.com", href: "https://cldixon.com" },
];
