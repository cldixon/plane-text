/**
 * Runs plane_text pipelines in a Cloudflare Container, which writes to R2.
 *
 *   POST /runs/<pipeline>[?snapshot=YYYY-MM-DD]   start a run (202), or 409 if one is running;
 *                                                 the snapshot defaults to today (UTC)
 *   GET  /runs/<pipeline>                         the latest runs, newest first
 *   DELETE /runs/<pipeline>                       stop the current run (its container is destroyed)
 *
 * Both need `Authorization: Bearer <RUN_TOKEN>`. Each pipeline has its own
 * Durable Object, so it runs at most once at a time; the object starts the
 * container with the pipeline's command on the pipeline's instance type, waits
 * for it to exit, and records the run.
 *
 * `Env` is generated from cloudflare.config.ts by `cf workers types` into .cloudflare/types/.
 */

import { DurableObject } from "cloudflare:workers";

type Step = {
	/** Run as `python -m plane_text.<module> <args> --upload --snapshot <date>`. */
	module: string;
	args?: string[];
};

type Pipeline = {
	/** Run in order in one container; a later step may read what an earlier one built. */
	steps: Step[];
	instance: "standard-1" | "standard-2" | "standard-3" | "standard-4";
	/** A run still going after this long is stopped and recorded as failed. */
	maxHours: number;
};

/**
 * Pipelines that can be started, by URL name. PDF pipelines OCR with one Tesseract
 * per vCPU. Advisory circulars are left out: re-reading 2.5 GB of PDFs and OCRing
 * ~7,000 scanned pages on every run needs state that outlives the container.
 */
const PIPELINES: Record<string, Pipeline> = {
	"ecfr-title14": { steps: [{ module: "ecfr" }], instance: "standard-1", maxHours: 1 },
	"faa-ads": { steps: [{ module: "faa_ads" }], instance: "standard-1", maxHours: 1 },
	"faa-proposed-rules": { steps: [{ module: "faa_proposed_rules" }], instance: "standard-1", maxHours: 2 },
	// Processed SDRs decode codes with the processed lookups, so the lookups are built first.
	// Decoding 1.77 million reports peaks near 8 GiB, too close to standard-3's limit.
	"faa-sdrs": {
		steps: [{ module: "faa_sdrs_lookups" }, { module: "faa_sdrs" }],
		instance: "standard-4",
		maxHours: 1,
	},
	// The three NTSB datasets share one download; narratives and findings decode with the lookups.
	ntsb: {
		steps: [{ module: "ntsb_lookups" }, { module: "ntsb" }, { module: "ntsb_findings" }],
		instance: "standard-3",
		maxHours: 1,
	},
	"faa-saibs": {
		steps: [{ module: "faa_guidance", args: ["SAIB", "--workers", "4"] }],
		instance: "standard-4",
		maxHours: 1,
	},
	"faa-maintenance-alerts": {
		steps: [{ module: "faa_guidance", args: ["ALERTS", "--workers", "4"] }],
		instance: "standard-4",
		maxHours: 1,
	},
};

/**
 * The container's command: each step in turn, stopping at the first that fails.
 * Every step gets the same snapshot, so a run that crosses midnight UTC stays one version.
 */
function command(pipeline: Pipeline, snapshot: string): string[] {
	const steps = pipeline.steps.map((step) =>
		["python", "-m", `plane_text.${step.module}`, ...(step.args ?? []), "--upload", "--snapshot", snapshot].join(
			" ",
		),
	);
	return steps.length === 1 ? steps[0]!.split(" ") : ["sh", "-c", `set -e; ${steps.join("; ")}`];
}

const CHECK_EVERY_MS = 60 * 1000;
/**
 * How long the container may outlive an idle Durable Object. Without it, Cloudflare
 * stops the container soon after the object goes idle, which a pending `monitor()`
 * only postpones for 15 minutes. The minute alarm keeps waking the object, so this
 * only has to cover the gaps between alarms.
 */
const INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000;
const HISTORY = 20;

type Run = {
	id: string;
	pipeline: string;
	command: string[];
	instance: string;
	snapshot: string;
	state: "running" | "succeeded" | "failed" | "cancelled";
	startedAt: string;
	finishedAt?: string;
	error?: string;
};

export class PipelineRun extends DurableObject<Env> {
	/** The run whose container exit this instance is waiting on. Lost if the object restarts. */
	private watching: string | undefined;

	async start(pipeline: string, snapshot: string | null): Promise<{ started: boolean; run: Run }> {
		const container = this.ctx.container!;
		const runs = await this.runs();
		if (runs[0]?.state === "running" && container.running) return { started: false, run: runs[0] };

		const spec = PIPELINES[pipeline]!;
		const date = snapshot ?? new Date().toISOString().slice(0, 10);
		const run: Run = {
			id: crypto.randomUUID(),
			pipeline,
			command: command(spec, date),
			instance: spec.instance,
			snapshot: date,
			state: "running",
			startedAt: new Date().toISOString(),
		};
		container.start({
			image: container.images.pipelines,
			instance: spec.instance,
			enableInternet: true,
			entrypoint: run.command,
			env: {
				DRS_API_KEY: this.env.DRS_API_KEY,
				S3_API_ENDPOINT: this.env.S3_API_ENDPOINT,
				AWS_ACCESS_KEY_ID: this.env.AWS_ACCESS_KEY_ID,
				AWS_SECRET_ACCESS_KEY: this.env.AWS_SECRET_ACCESS_KEY,
			},
		});
		await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
		await this.save([run, ...runs]);
		this.ctx.waitUntil(this.watch(run.id));
		await this.ctx.storage.setAlarm(Date.now() + CHECK_EVERY_MS);
		return { started: true, run };
	}

	/** Stops the current run, if any: recorded as cancelled, then its container destroyed. */
	async cancel(): Promise<Run | undefined> {
		const run = (await this.runs())[0];
		if (run?.state !== "running") return undefined;
		await this.finish(run.id, "cancelled");
		const container = this.ctx.container!;
		if (container.running) await container.destroy(new Error("run cancelled"));
		return (await this.runs())[0];
	}

	async history(): Promise<Run[]> {
		return this.runs();
	}

	/** Records the run's outcome when the container exits: a clean exit succeeds, anything else fails. */
	private async watch(id: string): Promise<void> {
		this.watching = id;
		try {
			await this.ctx.container!.monitor();
			await this.finish(id, "succeeded");
		} catch (error) {
			await this.finish(id, "failed", String(error));
		}
	}

	/**
	 * Every minute while a run is going: re-attaches `watch` if the object restarted
	 * since the run began (which drops the wait on the container), records a
	 * container gone without a recorded exit, and enforces `maxHours`.
	 */
	async alarm(): Promise<void> {
		const run = (await this.runs())[0];
		if (run?.state !== "running") return;
		const container = this.ctx.container!;
		if (container.running) {
			await container.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
			if (this.watching !== run.id) this.ctx.waitUntil(this.watch(run.id));
		}
		if (!container.running) {
			await this.finish(run.id, "failed", "container stopped without a recorded exit");
		} else if (Date.now() - Date.parse(run.startedAt) > PIPELINES[run.pipeline]!.maxHours * 3600_000) {
			await container.destroy(new Error("run exceeded its time limit"));
			await this.finish(run.id, "failed", `stopped after ${PIPELINES[run.pipeline]!.maxHours} hours`);
		} else {
			await this.ctx.storage.setAlarm(Date.now() + CHECK_EVERY_MS);
		}
	}

	private async finish(id: string, state: Run["state"], error?: string): Promise<void> {
		const runs = await this.runs();
		const run = runs.find((r) => r.id === id);
		if (!run || run.state !== "running") return;
		Object.assign(run, { state, finishedAt: new Date().toISOString() }, error ? { error } : {});
		await this.save(runs);
	}

	private async runs(): Promise<Run[]> {
		return (await this.ctx.storage.get<Run[]>("runs")) ?? [];
	}

	private async save(runs: Run[]): Promise<void> {
		await this.ctx.storage.put("runs", runs.slice(0, HISTORY));
	}
}

function json(body: unknown, status = 200): Response {
	return Response.json(body, { status });
}

/** Constant-time comparison of the bearer token. */
async function authorized(request: Request, token: string): Promise<boolean> {
	const given = request.headers.get("Authorization")?.replace(/^Bearer /, "") ?? "";
	const [a, b] = await Promise.all(
		[given, token].map(async (t) => new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(t)))),
	);
	return crypto.subtle.timingSafeEqual(a!, b!);
}

export default {
	async fetch(request, env, ctx): Promise<Response> {
		const match = /^\/runs\/([a-z0-9-]+)$/.exec(new URL(request.url).pathname);
		if (!match) return json({ error: "not found" }, 404);
		if (!(await authorized(request, env.RUN_TOKEN))) return json({ error: "unauthorized" }, 401);
		const pipeline = match[1]!;
		if (!(pipeline in PIPELINES)) return json({ error: `unknown pipeline: ${pipeline}` }, 404);
		// Typed by hand: the generated `Exports` type does not know this class is a Durable Object.
		const runs = ctx.exports.PipelineRun as unknown as DurableObjectNamespace<PipelineRun>;
		const runner = runs.getByName(pipeline);

		if (request.method === "GET") return json(await runner.history());
		if (request.method === "DELETE") {
			const run = await runner.cancel();
			return run ? json(run) : json({ error: "no run in progress" }, 409);
		}
		if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
		const snapshot = new URL(request.url).searchParams.get("snapshot");
		if (snapshot !== null && !/^\d{4}-\d{2}-\d{2}$/.test(snapshot)) {
			return json({ error: "snapshot must be YYYY-MM-DD" }, 400);
		}
		const { started, run } = await runner.start(pipeline, snapshot);
		return json(run, started ? 202 : 409);
	},
} satisfies ExportedHandler<Env>;
