<script lang="ts">
  /**
   * The query console: a Query button that floats in the bottom right corner
   * of the page and opens the SQL shell in a modal over most of it.
   *
   * - The shell, and DuckDB-WASM with it, load on the first open only; after
   *   that the shell stays alive while closed, so reopening keeps its history.
   * - Opens with the button, Cmd+K (Ctrl+K elsewhere), or a link to #shell;
   *   while open the address reads #shell. Closes with Escape, the close
   *   button, a click outside it, or Cmd+K again.
   * - A native <dialog>: focus stays inside while open, and the page behind
   *   is inert and does not scroll.
   *
   * Its look comes from the --console-* tokens in design/tokens.css.
   */
  import { onMount, type Component } from "svelte";
  import type { Table } from "../lib/duckdb";

  interface Props {
    tables: (Table & { rows: string })[];
    soon?: string[];
    sample?: string;
    placeholder?: string;
  }

  let props: Props = $props();

  const HASH = "#shell";

  let dialog: HTMLDialogElement | undefined = $state();
  let Shell: Component<Props, { focus: () => void }> | undefined = $state();
  let shell: { focus: () => void } | undefined = $state();
  let loadError = $state("");
  let shortcut = $state("Ctrl K");

  async function open(): Promise<void> {
    if (!dialog || dialog.open) return;
    dialog.showModal();
    if (location.hash !== HASH) history.replaceState(history.state, "", HASH);
    if (!Shell) {
      try {
        Shell = (await import("./Shell.svelte")).default;
      } catch {
        loadError = "The shell could not be loaded. Check your connection and try again.";
      }
    } else {
      shell?.focus();
    }
  }

  function close(): void {
    dialog?.close();
  }

  /** On close, by any route (Escape included): take #shell off the address. */
  function onClose(): void {
    if (location.hash === HASH) history.replaceState(history.state, "", location.pathname + location.search);
  }

  /** A click on the backdrop lands on the dialog itself, outside its frame. */
  function onDialogClick(event: MouseEvent): void {
    if (event.target === dialog) close();
  }

  onMount(() => {
    if (/Mac|iPhone|iPad/.test(navigator.platform)) shortcut = "⌘K";
    const onKey = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() === "k" && (event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey) {
        event.preventDefault();
        if (dialog?.open) close();
        else void open();
      }
    };
    const onHash = () => {
      if (location.hash === HASH) void open();
    };
    document.addEventListener("keydown", onKey);
    window.addEventListener("hashchange", onHash);
    onHash();
    return () => {
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("hashchange", onHash);
    };
  });
</script>

<button type="button" class="launcher" onclick={open} aria-haspopup="dialog" title="Query the corpus with SQL ({shortcut})">
  <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
    <path d="m7 11 2-2-2-2" />
    <path d="M11 13h4" />
    <rect width="18" height="18" x="3" y="3" rx="2" ry="2" />
  </svg>
  <span>Query</span>
  <kbd>{shortcut}</kbd>
</button>

<dialog bind:this={dialog} class="console" aria-labelledby="console-title" onclose={onClose} onclick={onDialogClick}>
  <div class="frame">
    <header>
      <h2 id="console-title">Query <span>SQL over the corpus, in your browser</span></h2>
      <button type="button" class="close" onclick={close} aria-label="Close the query console">
        <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
          <path d="M18 6 6 18" />
          <path d="m6 6 12 12" />
        </svg>
      </button>
    </header>
    <div class="body">
      {#if Shell}
        <Shell bind:this={shell} {...props} />
      {:else if loadError}
        <p class="note error">{loadError}</p>
      {:else}
        <p class="note">Loading the shell…</p>
      {/if}
    </div>
  </div>
</dialog>

<style>
  svg {
    fill: none;
    stroke: currentColor;
    stroke-width: 1.75;
    stroke-linecap: round;
    stroke-linejoin: round;
  }

  /* The floating button, clear of the page edge and of phone safe areas. */
  .launcher {
    position: fixed;
    right: max(var(--space-4), env(safe-area-inset-right));
    bottom: max(var(--space-4), env(safe-area-inset-bottom));
    z-index: 10;
    display: inline-flex;
    align-items: center;
    gap: var(--space-2);
    min-height: var(--target-min);
    padding: 0 var(--space-4);
    font-family: var(--console-font-label);
    font-size: var(--font-size-sm);
    color: var(--console-text);
    background: var(--console-background);
    border: var(--border-width) solid var(--console-border);
    border-radius: 999px;
    box-shadow: var(--console-shadow);
    cursor: pointer;
    transition: border-color var(--transition-fast);
  }

  .launcher:hover,
  .launcher:focus-visible {
    border-color: var(--console-accent);
  }

  kbd {
    font-family: var(--console-font);
    font-size: 0.75em;
    color: var(--console-text-muted);
  }

  /* No keyboard hint where there is likely no keyboard. */
  @media (hover: none) {
    kbd {
      display: none;
    }
  }

  /* The modal: most of the page, with the page still showing around it. */
  .console {
    width: min(72rem, 92vw);
    height: min(48rem, 86vh);
    max-width: none;
    max-height: none;
    padding: 0;
    color: var(--console-text);
    background: var(--console-background);
    border: var(--border-width) solid var(--console-border);
    border-radius: var(--console-radius);
    box-shadow: var(--console-shadow);
  }

  .console::backdrop {
    background: var(--console-backdrop);
  }

  /* Nearly the whole screen on phones. */
  @media (max-width: 40rem) {
    .console {
      width: 100vw;
      height: 100dvh;
      border: 0;
      border-radius: 0;
    }
  }

  .frame {
    display: flex;
    flex-direction: column;
    height: 100%;
  }

  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: var(--space-3);
    padding: var(--space-2) var(--space-2) var(--space-2) var(--space-3);
    border-bottom: var(--border-width) solid var(--console-border);
  }

  h2 {
    margin: 0;
    font-family: var(--console-font-label);
    font-size: var(--font-size-sm);
    font-weight: var(--font-weight-normal);
  }

  h2 span {
    margin-left: var(--space-2);
    color: var(--console-text-muted);
  }

  @media (max-width: 40rem) {
    h2 span {
      display: none;
    }
  }

  .close {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: var(--target-min);
    height: var(--target-min);
    padding: 0;
    color: var(--console-text-muted);
    background: transparent;
    border: 0;
    border-radius: var(--console-radius);
    cursor: pointer;
  }

  .close:hover,
  .close:focus-visible {
    color: var(--console-text);
  }

  .body {
    flex: 1;
    min-height: 0;
  }

  .note {
    margin: 0;
    padding: var(--space-3);
    font-family: var(--console-font);
    font-size: var(--font-size-sm);
    color: var(--console-text-muted);
  }

  .error {
    color: var(--console-error);
  }

  /* The page behind does not scroll while the console is open. */
  :global(html:has(dialog.console[open])) {
    overflow: hidden;
  }
</style>
