[PLANS]

[DECISIONS]

[PROGRESS]

[DISCOVERIES]
- 2026-08-14T17:40Z [CODE] The failed RealVisXL image was built before the current `convert_realvis.py` fix. Both training and inference force `variant="fp16"`; the prior conversion did not preserve real `text_encoder/model.fp16.safetensors` files in the pushed ACR layer, causing startup model loading to exit before training.
- 2026-08-15T21:41Z [CODE] SUPERSEDES the claimed conversion diagnosis above: current evidence is contradictory. A byte-copy of valid `model.safetensors` under the resolved `model.fp16.safetensors` name should be accepted; `variant="fp16"` selects a filename and does not require special fp16-export metadata. If `os.stat` and `from_pretrained` were run in the same image/container/path namespace, a true `FileNotFoundError` instead points to image/tag/digest mismatch, differing runtime namespace/layer, path/config mismatch, or incomplete error interpretation. Exact in-container reproduction remains required.
- 2026-08-17T00:00Z [CODE] `jobs/submit` is globally serialized at the DB layer: `shared/job_reservation.py` uses `sp_getapplock @Resource = 'submit-job'` across instances for each reservation, then performs credit checks, caps, insert, debit, and outbox write in one transaction.
- 2026-08-17T00:00Z [CODE] The repo's host config uses only Azure Functions defaults plus queue extension settings (`host.json`), with `batchSize: 1`; there is no custom API load balancer/gateway component in code, so LB semantics depend on Azure Functions hosting plan.
- 2026-08-17T00:00Z [CODE] Concurrency tests already codify the throughput ceilings (`GLOBAL_DAILY_CAP`, per-user cap, and dispatch lease behavior), and submission throughput can reject bursts at cap/lock before HTTP-level parallelism alone becomes the limiter.

[OUTCOMES]
- 2026-08-14T17:40Z [CODE] RealVisXL failure diagnosed as image packaging/model-load failure, not a data, prompt, GPU, or architecture incompatibility. Current workspace converter uses real copies plus a build-time required-file gate; the image must be rebuilt before retrying.
- 2026-08-15T21:41Z [CODE] Review found that RealVisXL was not actually tested for quality: execution stopped during base-model loading before training. The proposed side-by-side loader diagnostic is appropriate, but its output must include resolved image/tag/digest, process-visible path listing/stat, and the full traceback before changing conversion.
- 2026-08-17T00:00Z [ASSUMPTION] `jobs/submit` can handle approximately 100 concurrent non-submission user flows (status/profile/auth/read/upload validation) in code, but 100 simultaneous submits serialize through the SQL app-lock and are bounded by `GLOBAL_DAILY_CAP=25` unless that env var is increased; remaining throughput risk is controlled by Azure Functions plan + SQL tier.
