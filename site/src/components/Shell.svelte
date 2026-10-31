<script lang="ts">
  /**
   * A small SQL shell over the corpus, in the manner of the DuckDB CLI: type a
   * statement at the prompt, end it with ";" and press Enter. Results print
   * as box-drawn tables. Up and Down walk the history. Dot commands: .tables,
   * .help, .clear.
   */
  import { onMount, tick } from "svelte";
  import { errorMessage, formatValue, openDuckDB, typeName, type Session, type Table } from "../lib/duckdb";

  interface Props {
    /** Tables the shell can query, each a parquet file on this site. */
    tables: (Table & { rows: string })[];
    /** Datasets listed in the table picker as not queryable yet. */
    soon?: string[];
  }

  let { tables, soon = [] }: Props = $props();

  /** Rows printed per result; the rest are counted. */
  const MAX_ROWS = 40;
  /** Characters shown in a cell before the text is cut off. */
  const MAX_CELL = 40;

  type Line = { text: string; muted?: boolean };
  type Entry = { kind: "note" | "input" | "error" | "result"; lines: Line[] };

  let entries = $state<Entry[]>([{ kind: "note", lines: [{ text: "Starting DuckDB…", muted: true }] }]);
  let status = $state<"loading" | "ready" | "running">("loading");
  let input = $state("");
  let picked = $state(tables[0]?.name ?? "");
  let screen: HTMLDivElement | undefined = $state();
  let field: HTMLTextAreaElement | undefined = $state();

  const history: string[] = [];
  let historyIndex = 0;
  let session: Session | undefined;

  const pickedRows = $derived(tables.find((t) => t.name === picked)?.rows ?? "");

  onMount(() => {
    openDuckDB(tables)
      .then((s) => {
        session = s;
        entries = [
          {
            kind: "note",
            lines: [
              { text: "Plane-text shell, on DuckDB-WASM", muted: true },
              { text: 'Enter ".help" for commands.', muted: true },
            ],
          },
        ];
        status = "ready";
        field?.focus();
      })
      .catch((e: unknown) => {
        entries = [{ kind: "error", lines: [{ text: `Could not start DuckDB: ${errorMessage(e)}` }] }];
      });
    return () => {
      void session?.conn.close();
      void session?.db.terminate();
    };
  });

  /** A statement is complete when it ends with ";" or is a dot command. */
  function complete(text: string): boolean {
    const trimmed = text.trim();
    return trimmed.startsWith(".") || trimmed.endsWith(";");
  }

  function onKeydown(event: KeyboardEvent): void {
    const el = event.currentTarget as HTMLTextAreaElement;
    if (event.key === "Enter" && !event.shiftKey && complete(input)) {
      event.preventDefault();
      void submit();
    } else if (event.key === "ArrowUp" && !el.value.slice(0, el.selectionStart).includes("\n") && historyIndex > 0) {
      event.preventDefault();
      historyIndex -= 1;
      input = history[historyIndex] ?? "";
    } else if (event.key === "ArrowDown" && !el.value.slice(el.selectionEnd).includes("\n") && historyIndex < history.length) {
      event.preventDefault();
      historyIndex += 1;
      input = history[historyIndex] ?? "";
    }
  }

  async function submit(): Promise<void> {
    if (status !== "ready" || !session) return;
    const text = input.trim();
    input = "";
    if (!text) return;
    if (history.at(-1) !== text) history.push(text);
    historyIndex = history.length;
    entries.push({ kind: "input", lines: text.split("\n").map((line) => ({ text: line })) });

    const command = text.replace(/;$/, "").trim();
    if (command === ".clear") {
      entries = [];
    } else if (command === ".help") {
      entries.push({
        kind: "note",
        lines: [
          { text: ".tables   list the tables" },
          { text: ".clear    clear the screen" },
          { text: ".help     show this help" },
          { text: "End SQL with ; and press Enter. Shift+Enter adds a line; Up and Down walk the history.", muted: true },
        ],
      });
    } else {
      await query(command === ".tables" ? "SHOW TABLES" : text);
    }
    await scrollDown();
  }

  async function query(sql: string): Promise<void> {
    if (!session) return;
    status = "running";
    const started = performance.now();
    try {
      const reader = await session.conn.send(sql);
      let columns: string[] = [];
      let types: string[] = [];
      const rows: string[][] = [];
      let total = 0;
      for await (const batch of reader) {
        columns = batch.schema.fields.map((f) => f.name);
        types = batch.schema.fields.map((f) => String(f.type));
        for (let i = 0; i < batch.numRows && rows.length < MAX_ROWS; i++) {
          const row = batch.get(i)?.toArray() ?? [];
          rows.push(columns.map((_, c) => formatValue(row[c], types[c] ?? "")));
        }
        total += batch.numRows;
      }
      const ms = Math.round(performance.now() - started);
      if (columns.length === 0) {
        entries.push({ kind: "note", lines: [{ text: `OK (${ms} ms)`, muted: true }] });
      } else {
        entries.push({ kind: "result", lines: box(columns, types, rows, total, ms) });
      }
    } catch (e) {
      entries.push({ kind: "error", lines: [{ text: errorMessage(e) }] });
    } finally {
      status = "ready";
    }
  }

  /** Draws a result as a DuckDB-style box table, with types under the names. */
  function box(columns: string[], types: string[], rows: string[][], total: number, ms: number): Line[] {
    const clip = (s: string) => (s.length > MAX_CELL ? `${s.slice(0, MAX_CELL - 1)}…` : s);
    const names = columns.map(clip);
    const kinds = types.map(typeName);
    const cells = rows.map((row) => row.map(clip));
    const widths = names.map((name, c) => Math.max(name.length, kinds[c]?.length ?? 0, ...cells.map((r) => r[c]?.length ?? 0)));
    const numeric = kinds.map((k) => /int|double|float|decimal/.test(k));
    const center = (s: string, w: number) => {
      const left = Math.floor((w - s.length) / 2);
      return " ".repeat(left) + s + " ".repeat(w - s.length - left);
    };
    const rule = (l: string, m: string, r: string) => l + widths.map((w) => "─".repeat(w + 2)).join(m) + r;
    const line = (values: string[], align: (v: string, c: number) => string) =>
      "│" + values.map((v, c) => ` ${align(v, c)} `).join("│") + "│";

    const out: Line[] = [
      { text: rule("┌", "┬", "┐") },
      { text: line(names, (v, c) => center(v, widths[c] ?? 0)) },
      { text: line(kinds, (v, c) => center(v, widths[c] ?? 0)), muted: true },
      { text: rule("├", "┼", "┤") },
      ...cells.map((row) => ({
        text: line(row, (v, c) => (numeric[c] ? v.padStart(widths[c] ?? 0) : v.padEnd(widths[c] ?? 0))),
      })),
      { text: rule("└", "┴", "┘") },
    ];
    const shown = total > rows.length ? `, ${rows.length} shown` : "";
    out.push({ text: `${total.toLocaleString("en-US")} ${total === 1 ? "row" : "rows"}${shown} (${ms} ms)`, muted: true });
    return out;
  }

  async function scrollDown(): Promise<void> {
    await tick();
    screen?.scrollTo({ top: screen.scrollHeight });
    field?.focus();
  }
</script>

<div class="shell">
  <div class="toolbar">
    <label for="shell-table">Table</label>
    <select id="shell-table" bind:value={picked}>
      {#each tables as table}
        <option value={table.name}>{table.name}</option>
      {/each}
      {#each soon as name}
        <option disabled>{name} · soon</option>
      {/each}
    </select>
    <span class="muted">{pickedRows} rows</span>
  </div>

  <div class="screen" bind:this={screen} role="log" aria-live="polite" aria-label="Shell output">
    {#each entries as entry}
      <div class={entry.kind}>
        {#each entry.lines as line, i}
          <div class:muted={line.muted}>
            {#if entry.kind === "input"}<span class="prompt">{i === 0 ? "plane-text >" : "           ‣"}</span>
            {/if}{line.text}
          </div>
        {/each}
      </div>
    {/each}
    <div class="prompt-line">
      <label class="prompt" for="shell-input">plane-text &gt;</label>
      <textarea
        id="shell-input"
        bind:this={field}
        bind:value={input}
        onkeydown={onKeydown}
        rows={Math.max(1, input.split("\n").length)}
        disabled={status === "loading"}
        spellcheck="false"
        autocapitalize="off"
        autocomplete="off"
      ></textarea>
    </div>
  </div>
</div>

<style>
  .shell {
    border: var(--border-strong);
    border-radius: var(--radius);
    font-family: var(--font-family-terminal);
    font-size: var(--font-size-sm);
  }

  .shell:focus-within {
    border-color: var(--color-text);
  }

  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
    padding: var(--space-2) var(--space-3);
    border-bottom: var(--border);
    font-family: var(--font-family-mono);
  }

  .toolbar label,
  .muted,
  .prompt {
    color: var(--color-text-muted);
  }

  select {
    width: 8rem;
    min-height: 2rem;
    padding: 0 var(--space-2);
    font: inherit;
    color: var(--color-text);
    background: var(--color-background);
    border: var(--border-strong);
    border-radius: var(--radius);
  }

  .screen {
    height: var(--shell-height);
    overflow: auto;
    padding: var(--space-3);
    background: var(--color-surface);
    line-height: 1.55;
  }

  .screen > div > div {
    white-space: pre;
  }

  .error {
    color: var(--color-error);
    white-space: pre-wrap;
  }

  .input,
  .note,
  .result,
  .error {
    margin-bottom: var(--space-2);
  }

  .prompt {
    margin-right: 1ch;
  }

  .prompt-line {
    display: flex;
    align-items: flex-start;
  }

  textarea {
    flex: 1;
    min-width: 0;
    padding: 0;
    font: inherit;
    line-height: inherit;
    color: var(--color-text);
    background: transparent;
    border: 0;
    outline: none;
    resize: none;
    caret-color: var(--color-text);
  }
</style>
