/**
 * Starts DuckDB-WASM in a Web Worker and exposes parquet files on this site as
 * views. DuckDB's wasm and worker scripts come from jsDelivr; only the small JS
 * loader is bundled with the site, and it is imported on demand.
 */
import type { AsyncDuckDB, AsyncDuckDBConnection } from "@duckdb/duckdb-wasm";

export type Table = { name: string; src: string };

export type Session = { db: AsyncDuckDB; conn: AsyncDuckDBConnection };

export async function openDuckDB(tables: Table[]): Promise<Session> {
  const duckdb = await import("@duckdb/duckdb-wasm");
  const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
  if (!bundle.mainWorker) throw new Error("no worker script in the selected DuckDB bundle");

  // Workers must be same-origin, so start one that imports the CDN script.
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts(${JSON.stringify(bundle.mainWorker)});`], { type: "text/javascript" }),
  );
  const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(workerUrl));
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  // Read files with Range requests only. Left at its defaults, this build
  // downloads the whole file in one GET.
  await db.open({
    filesystem: { reliableHeadRequests: true, allowFullHTTPReads: false, forceFullHTTPReads: false },
  });

  const conn = await db.connect();
  for (const table of tables) {
    // Read the file through DuckDB-WASM's own HTTP reader (Range requests).
    const fileName = `${table.name}.parquet`;
    await db.registerFileURL(fileName, new URL(table.src, location.href).href, duckdb.DuckDBDataProtocol.HTTP, false);
    await conn.query(`CREATE VIEW ${table.name} AS SELECT * FROM read_parquet('${fileName}')`);
  }
  return { db, conn };
}

/** Renders an Arrow value as text. Dates arrive as epoch milliseconds. */
export function formatValue(value: unknown, type: string): string {
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
    return `[${Array.from(value.toArray() as ArrayLike<unknown>, (v) => formatValue(v, "")).join(", ")}]`;
  }
  // Structs and maps.
  if ("toJSON" in value && typeof value.toJSON === "function") {
    return JSON.stringify(value.toJSON(), (_, v: unknown) => (typeof v === "bigint" ? v.toString() : v));
  }
  return String(value);
}

/** DuckDB's names for the Arrow types a query can return. */
export function typeName(arrowType: string): string {
  const names: Record<string, string> = {
    Utf8: "varchar",
    LargeUtf8: "varchar",
    Int8: "tinyint",
    Int16: "smallint",
    Int32: "integer",
    Int64: "bigint",
    Uint64: "ubigint",
    Float32: "float",
    Float64: "double",
    Bool: "boolean",
  };
  if (arrowType in names) return names[arrowType] ?? arrowType;
  if (arrowType.startsWith("Date")) return "date";
  if (arrowType.startsWith("Timestamp")) return "timestamp";
  if (arrowType.startsWith("List")) return "list";
  if (arrowType.startsWith("Struct")) return "struct";
  if (arrowType.startsWith("Decimal")) return "decimal";
  return arrowType.toLowerCase();
}

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
