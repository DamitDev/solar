# supernova-step-download-model

Step image for SuperNova training pipelines. Resolves a `repo://` model URI via
Data Repository and pulls the model artifact from Harbor (ORAS) into the job
workspace.

## Workspace Contract

This step conforms to the [Job Step Workspace
specification](https://github.com/DamitDev/training-platform-project/blob/master/docs/specs/job-step-workspace.md)
(S-021).

- **Consumes:** `MODEL_URI` env var, `DATA_REPOSITORY_URL`, Harbor credentials
- **Produces:** Model files in `MODEL_OUTPUT_DIR`
- **Side effect:** Atomically updates `/workspace/config/job.json` →
  `steps.download_model`

## Required Environment Variables

| Variable               | Example                                        | Description                                             |
| ---------------------- | ---------------------------------------------- | ------------------------------------------------------- |
| `MODEL_URI`            | `repo://IRIS-BERT-base:v1`                     | Source URI of the base model                            |
| `MODEL_OUTPUT_DIR`     | `/workspace/models/IRIS-BERT-base`             | Target directory (must be under `/workspace/models/`)   |
| `DATA_REPOSITORY_URL`  | `http://data-repository:8000`                  | Data Repository API base URL                            |
| `HARBOR_URL`           | `https://imgrepo.damit.hu`                     | Harbor registry URL                                     |
| `HARBOR_USERNAME`      | `robot_supernova+aiops`                        | Harbor robot account                                    |
| `HARBOR_PASSWORD`      | `...`                                          | Harbor robot password                                   |
| `HARBOR_OPERATION_TIMEOUT_SECONDS` | `300`                               | Optional maximum duration for Harbor login and pull     |

## Building

```bash
docker build -t supernova-step-download-model download-model/
```

## Local Smoke Test

This test pulls a real model artifact (requires Harbor access and a Data
Repository with the artifact registered).

```bash
# 1. Build the image
docker build -t supernova-step-download-model download-model/

# 2. Create a temporary workspace for testing
TEST_WS=$(mktemp -d)
mkdir -p "$TEST_WS"/{models,config}
echo '{"job_id":"smoke-test","name":"smoke","steps":{}}' > "$TEST_WS"/config/job.json

# 3. Run the step
docker run --rm \
  -v "$TEST_WS/models:/workspace/models" \
  -v "$TEST_WS/config:/workspace/config" \
  -e MODEL_URI="repo://iris-osl:v3" \
  -e MODEL_OUTPUT_DIR="/workspace/models/iris-osl" \
  -e DATA_REPOSITORY_URL \
  -e HARBOR_URL \
  -e HARBOR_USERNAME \
  -e HARBOR_PASSWORD \
  supernova-step-download-model

# 4. Verify
echo "Exit code: $?"
ls -la "$TEST_WS"/models/iris-osl/
cat "$TEST_WS"/config/job.json | python3 -m json.tool

# 5. Cleanup
rm -rf "$TEST_WS"
```

## Error Handling

| Failure                            | Exit Code | Message                                          |
| ---------------------------------- | --------- | ------------------------------------------------ |
| Missing `MODEL_URI`                | 1         | `MODEL_URI is required`                          |
| Missing `MODEL_OUTPUT_DIR`         | 1         | `MODEL_OUTPUT_DIR is required`                   |
| Path outside `/workspace/models/`  | 1         | `MODEL_OUTPUT_DIR (...) must be under ...`       |
| Data Repository unreachable        | 1         | `Failed to reach Data Repository at ...`         |
| Artifact not found (404)           | 1         | `Artifact not found in Data Repository: ...`     |
| Missing resolve metadata           | 1         | `Data Repository response missing required field(s)` |
| Harbor auth failure (401)          | 1         | `ORAS pull failed for ...`                       |
| Harbor login/pull timeout          | 1         | `ORAS operation timed out after ...`             |
| ORAS pull failure                  | 1         | `ORAS pull failed for ...`                       |
| Existing output directory          | 1         | `MODEL_OUTPUT_DIR already exists and will not be overwritten` |
| `job.json` not found               | 1         | `job.json not found at ...`                      |
| Success                            | 0         | `job.json updated successfully`                  |

The artifact is pulled into a temporary sibling directory and moved into
`MODEL_OUTPUT_DIR` only after a non-empty transfer succeeds. This prevents
failed pulls from leaving a partial model in the requested destination.

## Future Enhancements

- **Digest verification:** After pulling the artifact, the step could verify the
  downloaded files' checksum against the `checksum`/`digest` returned by the
  Data Repository resolve endpoint. Currently, the digest is passed through to
  `job.json` for audit trail but not verified.
