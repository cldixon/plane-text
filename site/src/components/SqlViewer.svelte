<script lang="ts">
  /**
   * A small SQL console: DuckDB-WASM runs in a Web Worker in the browser and
   * reads one parquet file over HTTP Range requests, exposed as a view.
   * DuckDB's wasm and worker scripts come from jsDelivr; only the small JS
   * loader is bundled with the site, and it is loaded after the page renders.
   */
  import { onMount, untrack } from "svelte";
  import type { AsyncDuckDB, AsyncDuckDBConnection } from "@duckdb/duckdb-wasm";

  interface Props {
    /** Path of the parquet file on this site, e.g. /data/…/file.parquet. */
    src: string;
    /** Name of the view the file is exposed as. */
    view: string;
    /** The query the editor starts with; it runs as soon as DuckDB is ready. */
    initialQuery: string;
    /** Queries offered as buttons above the editor; picking one runs it. */
    examples?: { label: string; query: string }[];
  }

  let { src, view, initialQuery, examples = [] }: Props = $props();

  /** Rows kept for display; the rest are counted, not stored. */
  const MAX_ROWS = 100;
  /** Characters shown in a cell before the text is cut off. */
  const MAX_CELL_CHARS = 120;
  /** Characters kept in a cell's tooltip. */
  const MAX_TITLE_CHARS = 2000;

  type Result = { columns: string[]; rows: string[][]; rowCount: number; ms: number };

  // The editor owns the text after the first render.
  let query = $state(untrack(() => initialQuery));
  let status = $state<"loading" | "ready" | "running">("loading");
  let error = $state<string | null>(null);
  let result = $state<Result | null>(null);

  const summary = $derived.by(() => {
    if (status === "loading") return error ? "" : "Loading DuckDB…";
    if (status === "running") return "Running…";
    if (error || !result) return "Ctrl+Enter or ⌘+Enter to run";
    const { rowCount, rows, ms } = result;
    const shown = rowCount > rows.length ? `, showing the first ${rows.length}` : "";
    return `${rowCount.toLocaleString()} ${rowCount === 1 ? "row" : "rows"} in ${Math.round(ms).toLocaleString()} ms${shown}`;
  });

  let db: AsyncDuckDB | undefined;
  let conn: AsyncDuckDBConnection | undefined;

  onMount(() => {
    init().catch((e: unknown) => {
      error = `Could not start DuckDB: ${message(e)}`;
    });
    return () => {
      void conn?.close();
      void db?.terminate();
    };
  });

  async function init(): Promise<void> {
    const duckdb = await import("@duckdb/duckdb-wasm");
    const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
    if (!bundle.mainWorker) throw new Error("no worker script in the selected DuckDB bundle");

    // Workers must be same-origin, so start one that imports the CDN script.
    const workerUrl = URL.createObjectURL(
      new Blob([`importScripts(${JSON.stringify(bundle.mainWorker)});`], { type: "text/javascript" }),
    );
    db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(workerUrl));
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    URL.revokeObjectURL(workerUrl);
    // Read files with Range requests only. Left at its defaults, this build
    // downloads the whole file in one GET.
    await db.open({
      filesystem: { reliableHeadRequests: true, allowFullHTTPReads: false, forceFullHTTPReads: false },
    });

    // Read the file through DuckDB-WASM's own HTTP reader (Range requests).
    const fileName = `${view}.parquet`;
    await db.registerFileURL(fileName, new URL(src, location.href).href, duckdb.DuckDBDataProtocol.HTTP, false);
    conn = await db.connect();
    await conn.query(`CREATE VIEW ${view} AS SELECT * FROM read_parquet('${fileName}')`);
    status = "ready";
    await run();
  }

  function pick(example: string): void {
    query = example;
    void run();
  }

  async function run(): Promise<void> {
    if (!conn || status !== "ready") return;
    status = "running";
    error = null;
    const started = performance.now();
    try {
      const reader = await conn.send(query);
      const rows: string[][] = [];
      let columns: string[] = [];
      let rowCount = 0;
      for await (const batch of reader) {
        columns = batch.schema.fields.map((f) => f.name);
        const types = batch.schema.fields.map((f) => String(f.type));
        for (let i = 0; i < batch.numRows && rows.length < MAX_ROWS; i++) {
          const row = batch.get(i)?.toArray() ?? [];
          rows.push(columns.map((_, c) => format(row[c], types[c] ?? "")));
        }
        rowCount += batch.numRows;
      }
      if (columns.length === 0) columns = reader.schema?.fields.map((f) => f.name) ?? [];
      result = { columns, rows, rowCount, ms: performance.now() - started };
    } catch (e) {
      error = message(e);
      result = null;
    } finally {
      status = "ready";
    }
  }

  function onKeydown(event: KeyboardEvent): void {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void run();
    }
  }

  /** Renders an Arrow value as text. Dates arrive as epoch milliseconds. */
  function format(value: unknown, type: string): string {
    if (value === null || value === undefined) return "NULL";
    if (typeof value === "number" && type.startsWith("Date")) {
      return new Date(value).toISOString().slice(0, 10);
    }
    if (typeof value === "number" && type.startsWith("Timestamp")) {
      return new Date(value).toISOString().replace("T", " ").replace(".000Z", "Z");
    }
    if (value instanceof Uint8Array) return `<${value.length} bytes>`;
    // HUGEINT and DECIMAL arrive as typed-array wrappers that print as numbers.
    if (typeof value !== "object" || ArrayBuffer.isView(value)) return String(value);
    // Lists arrive as Arrow vectors.
    if ("toArray" in value && typeof value.toArray === "function") {
      return `[${Array.from(value.toArray() as ArrayLike<unknown>, (v) => format(v, "")).join(", ")}]`;
    }
    // Structs and maps.
    if ("toJSON" in value && typeof value.toJSON === "function") {
      return JSON.stringify(value.toJSON(), (_, v: unknown) => (typeof v === "bigint" ? v.toString() : v));
    }
    return String(value);
  }

  function truncate(text: string, max: number): string {
    return text.length > max ? `${text.slice(0, max - 1)}…` : text;
  }

  function message(e: unknown): string {
    return e instanceof Error ? e.message : String(e);
  }
</script>

<div class="viewer">
  {#if examples.length > 0}
    <div class="bar">
      <span class="muted">Try:</span>
      {#each examples as example}
        <button type="button" onclick={() => pick(example.query)} disabled={status !== "ready"}>{example.label}</button>
      {/each}
    </div>
  {/if}

  <label for="sql">SQL</label>
  <textarea
    id="sql"
    bind:value={query}
    onkeydown={onKeydown}
    rows="6"
    spellcheck="false"
    autocapitalize="off"
    autocomplete="off"
  ></textarea>

  <div class="bar">
    <button type="button" onclick={run} disabled={status !== "ready"}>Run</button>
    <span class="muted" aria-live="polite">{summary}</span>
  </div>

  {#if error}
    <pre class="error" role="alert">{error}</pre>
  {/if}

  {#if result && !error}
    <div class="scroll">
      <table>
        <thead>
          <tr>
            {#each result.columns as column}
              <th scope="col">{column}</th>
            {/each}
          </tr>
        </thead>
        <tbody>
          {#each result.rows as row}
            <tr>
              {#each row as cell}
                <td class:null={cell === "NULL"} title={cell.length > MAX_CELL_CHARS ? truncate(cell, MAX_TITLE_CHARS) : undefined}
                  >{truncate(cell, MAX_CELL_CHARS)}</td
                >
              {/each}
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .viewer {
    display: flex;
    flex-direction: column;
    gap: var(--space-2);
  }

  textarea {
    width: 100%;
    padding: var(--space-2);
    font-family: var(--font-family-mono);
    font-size: var(--font-size-sm);
    border: var(--border);
    resize: vertical;
  }

  .bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: var(--space-2) var(--space-3);
  }

  .muted,
  .null {
    color: var(--color-text-muted);
  }

  .error {
    margin: 0;
    padding: var(--space-2);
    color: var(--color-error);
    border: var(--border-width) solid var(--color-error);
    white-space: pre-wrap;
  }

  .scroll {
    max-height: var(--results-max-height);
    overflow: auto;
    border: var(--border);
  }

  table {
    border-collapse: collapse;
    font-size: var(--font-size-sm);
  }

  th,
  td {
    max-width: var(--cell-max-width);
    padding: var(--space-1) var(--space-2);
    border-bottom: var(--border);
    text-align: left;
    vertical-align: top;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  th {
    position: sticky;
    top: 0;
    background: var(--color-background);
    font-family: var(--font-family-mono);
  }
</style>
