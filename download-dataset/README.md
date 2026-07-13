# supernova-step-download-dataset

Step image for SuperNova training pipelines. Resolves a `repo://` dataset URI via
Data Repository and pulls the dataset artifact from Harbor (ORAS) into the job
workspace.

## Workspace Contract

This step conforms to the [Job Step Workspace
specification](https://github.com/DamitDev/training-platform-project/blob/master/docs/specs/job-step-workspace.md)
(S-021).

- **Consumes:** `DATASET_URI` env var, `DATA_REPOSITORY_URL`, Harbor credentials
- **Produces:** Dataset files in `DATASET_OUTPUT_DIR`
- **Side effect:** Atomically updates `/workspace/config/job.json` →
  `steps.download_dataset`

## Required Environment Variables

| Variable               | Example                                        | Description                                             |
| ---------------------- | ---------------------------------------------- | ------------------------------------------------------- |
| `DATASET_URI`          | `repo://iris-tickets:2026-03`                  | Source URI of the dataset                               |
| `DATASET_OUTPUT_DIR`   | `/workspace/data/tickets-dataset`              | Target directory (must be under `/workspace/data/`)     |
| `DATA_REPOSITORY_URL`  | `http://data-repository:8000`                  | Data Repository API base URL                            |
| `HARBOR_URL`           | `https://imgrepo.damit.hu`                     | Harbor registry URL                                     |
| `HARBOR_USERNAME`      | `robot_supernova+aiops`                        | Harbor robot account                                    |
| `HARBOR_PASSWORD`      | `...`                                          | Harbor robot password                                   |
| `HARBOR_OPERATION_TIMEOUT_SECONDS` | `300`                               | Optional maximum duration for Harbor login and pull     |

## Building

```bash
docker build -f download-dataset/Dockerfile -t supernova-step-download-dataset .
```

The image installs `certs/damit-cloud-root-ca.crt` into its system trust store,
so Data Repository and Harbor endpoints under `*.damit.cloud` are verified
without disabling TLS validation.

## Local Smoke Test

This test pulls a real dataset artifact (requires Harbor access and a Data
Repository with the artifact registered).

```bash
# 1. Build the image
docker build -f download-dataset/Dockerfile -t supernova-step-download-dataset .

# 2. Create a temporary workspace for testing
TEST_WS=$(mktemp -d)
mkdir -p "$TEST_WS"/{data,config}
echo '{"job_id":"smoke-test","name":"smoke","steps":{}}' > "$TEST_WS"/config/job.json

# 3. Run the step
docker run --rm \
  -v "$TEST_WS/data:/workspace/data" \
  -v "$TEST_WS/config:/workspace/config" \
  -e DATASET_URI="repo://iris-tickets:2026-03" \
  -e DATASET_OUTPUT_DIR="/workspace/data/tickets-dataset" \
  -e DATA_REPOSITORY_URL \
  -e HARBOR_URL \
  -e HARBOR_USERNAME \
  -e HARBOR_PASSWORD \
  supernova-step-download-dataset

# 4. Verify
echo "Exit code: $?"
ls -la "$TEST_WS"/data/tickets-dataset/
cat "$TEST_WS"/config/job.json | python3 -m json.tool

# 5. Cleanup
rm -rf "$TEST_WS"
```

## Error Handling

| Failure                            | Exit Code | Message                                          |
| ---------------------------------- | --------- | ------------------------------------------------ |
| Missing `DATASET_URI`              | 1         | `DATASET_URI is required`                        |
| Missing `DATASET_OUTPUT_DIR`       | 1         | `DATASET_OUTPUT_DIR is required`                 |
| Path outside `/workspace/data/`    | 1         | `DATASET_OUTPUT_DIR (...) must be under ...`     |
| Data Repository unreachable        | 1         | `Failed to reach Data Repository at ...`         |
| Artifact not found (404)           | 1         | `Artifact not found in Data Repository: ...`     |
| Missing resolve metadata           | 1         | `Data Repository response missing required field(s)` |
| Harbor auth failure (401)          | 1         | `ORAS pull failed for ...`                       |
| Harbor login/pull timeout          | 1         | `ORAS operation timed out after ...`             |
| ORAS pull failure                  | 1         | `ORAS pull failed for ...`                       |
| Empty ORAS pull                    | 1         | `ORAS pull returned no files for ...`            |
| Existing output directory          | 1         | `DATASET_OUTPUT_DIR already exists and will not be overwritten` |
| `job.json` not found               | 1         | `job.json not found at ...`                      |
| Invalid `job.json` shared state    | 1         | `job.json ... is not valid JSON` or `non-object steps field` |
| Success                            | 0         | `job.json updated successfully`                  |

The artifact is pulled into a temporary sibling directory and moved into
`DATASET_OUTPUT_DIR` only after a non-empty transfer succeeds. This prevents
failed pulls from leaving a partial dataset in the requested destination. If
the subsequent `job.json` update fails, the completed dataset is removed so
downstream steps cannot consume an unrecorded artifact.
