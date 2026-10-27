/**
 * Runs plane_text pipelines in a Cloudflare Container, which writes to R2.
 *
 *   POST /runs/<pipeline>[?snapshot=YYYY-MM-DD]   start a run (202), or 409 if one is running
 *   GET  /runs/<pipeline>                         the latest runs, newest first
 *
 * Both need `Authorization: Bearer <RUN_TOKEN>`. Each pipeline has its own
 * Durable Object, so it runs at most once at a time; the object starts the
 * container with the pipeline's command on the pipeline's instance type, waits
 * for it to exit, and records the run.
 *
 * `Env` is generated from cloudflare.config.ts by `cf workers types` into .cloudflare/types/.
 */

import { DurableObject } from "cloudflare:workers";

type Pipeline = {
	/** Run as `python -m plane_text.<module> <args> --upload [--snapshot <date>]`. */
	module: string;
	args?: string[];
	instance: "standard-1" | "standard-2" | "standard-3" | "standard-4";
	/** A run still going after this long is stopped and recorded as failed. */
	maxHours: number;
};

/** Pipelines that can be started, by URL name. PDF pipelines OCR with one Tesseract per vCPU. */
const PIPELINES: Record<string, Pipeline> = {
	"faa-ads": { module: "faa_ads", instance: "standard-1", maxHours: 1 },
	"faa-saibs": { module: "faa_guidance", args: ["SAIB", "--workers", "4"], instance: "standard-4", maxHours: 2 },
	"faa-maintenance-alerts": {
		module: "faa_guidance",
		args: ["ALERTS", "--workers", "4"],
		instance: "standard-4",
		maxHours: 2,
	},
	"faa-advisory-circulars": {
		module: "faa_guidance",
		args: ["AC", "--workers", "4"],
		instance: "standard-4",
		maxHours: 8,
	},
};

const CHECK_EVERY_MS = 60 * 1000;
const HISTORY = 20;

type Run = {
	id: string;
	pipeline: string;
	command: string[];
	instance: string;
	snapshot: string | null;
	state: "running" | "succeeded" | "failed";
	startedAt: string;
	finishedAt?: string;
	error?: string;
};

export class PipelineRun extends DurableObject<Env> {
	async start(pipeline: string, snapshot: string | null): Promise<{ started: boolean; run: Run }> {
		const container = this.ctx.container!;
		const runs = await this.runs();
		if (runs[0]?.state === "running" && container.running) return { started: false, run: runs[0] };

		const spec = PIPELINES[pipeline]!;
		const command = ["python", "-m", `plane_text.${spec.module}`, ...(spec.args ?? []), "--upload"];
		if (snapshot) command.push("--snapshot", snapshot);
		const run: Run = {
			id: crypto.randomUUID(),
			pipeline,
			command,
			instance: spec.instance,
			snapshot,
			state: "running",
			startedAt: new Date().toISOString(),
		};
		container.start({
			image: container.images.pipelines,
			instance: spec.instance,
			enableInternet: true,
			entrypoint: command,
			env: {
				DRS_API_KEY: this.env.DRS_API_KEY,
				S3_API_ENDPOINT: this.env.S3_API_ENDPOINT,
				AWS_ACCESS_KEY_ID: this.env.AWS_ACCESS_KEY_ID,
				AWS_SECRET_ACCESS_KEY: this.env.AWS_SECRET_ACCESS_KEY,
			},
		});
		await this.save([run, ...runs]);
		this.ctx.waitUntil(this.watch(run.id));
		await this.ctx.storage.setAlarm(Date.now() + CHECK_EVERY_MS);
		return { started: true, run };
	}

	async history(): Promise<Run[]> {
		return this.runs();
	}

	/** Records the run's outcome when the container exits: a clean exit succeeds, anything else fails. */
	private async watch(id: string): Promise<void> {
		try {
			await this.ctx.container!.monitor();
			await this.finish(id, "succeeded");
		} catch (error) {
			await this.finish(id, "failed", String(error));
		}
	}

	/** A backstop for `watch`, in case the object was evicted while the container ran; also enforces `maxHours`. */
	async alarm(): Promise<void> {
		const run = (await this.runs())[0];
		if (run?.state !== "running") return;
		const container = this.ctx.container!;
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
		if (request.method !== "POST") return json({ error: "method not allowed" }, 405);
		const snapshot = new URL(request.url).searchParams.get("snapshot");
		if (snapshot !== null && !/^\d{4}-\d{2}-\d{2}$/.test(snapshot)) {
			return json({ error: "snapshot must be YYYY-MM-DD" }, 400);
		}
		const { started, run } = await runner.start(pipeline, snapshot);
		return json(run, started ? 202 : 409);
	},
} satisfies ExportedHandler<Env>;
