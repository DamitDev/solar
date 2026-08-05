// Type definitions for Solar API

export type InstanceStatus = 'stopped' | 'starting' | 'running' | 'failed' | 'stopping';

export type HostStatus = 'online' | 'offline' | 'error';

/**
 * Drain lifecycle of a host (S-043). Independent of HostStatus: a draining
 * host is still online and still serving. Null/absent means normal operation.
 */
export type DrainState = 'draining' | 'drained';

export type BackendType =
  'llamacpp' | 'huggingface_causal' | 'huggingface_classification' | 'huggingface_embedding' | 'huggingface_vision';

export interface MemoryInfo {
  used_gb: number;
  total_gb: number;
  available_gb?: number;
  percent: number;
  memory_type: string;
}

// Base config interface with common fields
// Note: api_key is NOT part of instance config - instances always use host API key
export interface BaseInstanceConfig {
  backend_type: BackendType;
  alias: string;
  host: string;
  port?: number;
  /** HuggingFace download filters (allow_patterns) used when pulling the model */
  file_filters?: string[] | null;
  // Ownership marker (spec deployment-intent.md §5.1): set to "intent" + intent_id
  // for reconciler-managed instances; absent for manual instances.
  managed_by?: string | null;
  intent_id?: string | null;
}

// llama.cpp specific config
export interface LlamaCppConfig extends BaseInstanceConfig {
  backend_type: 'llamacpp';
  model: string;
  /** Filename, relative path or glob selecting the GGUF inside the pulled model directory */
  model_file?: string | null;
  /** Path to multimodal projector GGUF (llama-server --mmproj) */
  mmproj?: string;
  /** Whether to GPU-offload the multimodal projector (default: true) */
  mmproj_offload?: boolean;
  threads: number;
  n_gpu_layers: number;
  temp: number;
  top_p: number;
  top_k: number;
  min_p: number;
  ctx_size: number;
  chat_template_file?: string;
  chat_template_kwargs?: string;
  reasoning?: 'on' | 'off' | 'auto';
  reasoning_budget?: number;
  spec_type?: 'draft-mtp';
  spec_draft_n_max?: number;
  cache_type_k?: string;
  cache_type_v?: string;
  rope_scaling?: string;
  rope_scale?: number;
  yarn_orig_ctx?: number;
  special?: boolean;
  ot?: string;
  model_type?: 'llm' | 'embedding' | 'reranker';
  pooling?: 'none' | 'mean' | 'cls' | 'last' | 'rank';
}

// HuggingFace Causal LM config
export interface HuggingFaceCausalConfig extends BaseInstanceConfig {
  backend_type: 'huggingface_causal';
  model_id: string;
  device: string;
  dtype: string;
  max_length: number;
  trust_remote_code?: boolean;
  use_flash_attention?: boolean;
}

// HuggingFace Classification config
export interface HuggingFaceClassificationConfig extends BaseInstanceConfig {
  backend_type: 'huggingface_classification';
  model_id: string;
  device: string;
  dtype: string;
  max_length: number;
  labels?: string[];
  trust_remote_code?: boolean;
}

// HuggingFace Embedding config
export interface HuggingFaceEmbeddingConfig extends BaseInstanceConfig {
  backend_type: 'huggingface_embedding';
  model_id: string;
  device: string;
  dtype: string;
  max_length: number;
  normalize_embeddings?: boolean;
  trust_remote_code?: boolean;
}

// Union type for all config types
export type InstanceConfig =
  LlamaCppConfig | HuggingFaceCausalConfig | HuggingFaceClassificationConfig | HuggingFaceEmbeddingConfig;

// Helper to check backend type
export function isLlamaCppConfig(config: InstanceConfig): config is LlamaCppConfig {
  return config.backend_type === 'llamacpp' || !('backend_type' in config) || config.backend_type === undefined;
}

export function isHuggingFaceCausalConfig(config: InstanceConfig): config is HuggingFaceCausalConfig {
  return config.backend_type === 'huggingface_causal';
}

export function isHuggingFaceClassificationConfig(config: InstanceConfig): config is HuggingFaceClassificationConfig {
  return config.backend_type === 'huggingface_classification';
}

export function isHuggingFaceEmbeddingConfig(config: InstanceConfig): config is HuggingFaceEmbeddingConfig {
  return config.backend_type === 'huggingface_embedding';
}

export function getBackendType(config: InstanceConfig): BackendType {
  if ('backend_type' in config && config.backend_type) {
    return config.backend_type;
  }
  // Legacy configs without backend_type are llamacpp
  return 'llamacpp';
}

export function getBackendLabel(backendType: BackendType): string {
  switch (backendType) {
    case 'llamacpp':
      return 'llama.cpp';
    case 'huggingface_causal':
      return 'HF Causal';
    case 'huggingface_classification':
      return 'HF Classifier';
    case 'huggingface_embedding':
      return 'HF Embedding';
    default:
      return backendType;
  }
}

export function getBackendColor(backendType: BackendType): string {
  switch (backendType) {
    case 'llamacpp':
      return 'bg-nord-10 text-nord-6'; // Blue
    case 'huggingface_causal':
      return 'bg-nord-14 text-nord-0'; // Green
    case 'huggingface_classification':
      return 'bg-nord-13 text-nord-0'; // Yellow
    case 'huggingface_embedding':
      return 'bg-nord-15 text-nord-6'; // Purple
    default:
      return 'bg-nord-3 text-nord-4';
  }
}

// Model category type for routing/display purposes
export type ModelCategory = 'generation' | 'embedding' | 'classification' | 'reranker';

// Get the effective model category considering llama.cpp model_type
export function getModelCategory(config: InstanceConfig): ModelCategory {
  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return 'embedding';
      case 'reranker':
        return 'reranker';
      default:
        return 'generation';
    }
  }
  if (isHuggingFaceClassificationConfig(config)) {
    return 'classification';
  }
  if (isHuggingFaceEmbeddingConfig(config)) {
    return 'embedding';
  }
  return 'generation';
}

// Get display label for the full model type (including llama.cpp mode)
export function getFullModelLabel(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return 'llama.cpp Embedding';
      case 'reranker':
        return 'llama.cpp Reranker';
      default:
        return 'llama.cpp';
    }
  }
  return getBackendLabel(getBackendType(config));
}

// Get color class for the full model type (including llama.cpp mode)
export function getFullModelColor(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return 'bg-nord-15 text-nord-6'; // Purple (same as HF Embedding)
      case 'reranker':
        return 'bg-nord-12 text-nord-6'; // Orange for reranker
      default:
        return 'bg-nord-10 text-nord-6'; // Blue
    }
  }
  return getBackendColor(getBackendType(config));
}

// Get hex color for the full model type (for ReactFlow nodes)
export function getFullModelHexColor(config: InstanceConfig): string {
  if (isLlamaCppConfig(config)) {
    const llamaConfig = config as LlamaCppConfig;
    switch (llamaConfig.model_type) {
      case 'embedding':
        return '#B48EAD'; // Purple
      case 'reranker':
        return '#D08770'; // Orange
      default:
        return '#5E81AC'; // Blue
    }
  }
  switch (getBackendType(config)) {
    case 'huggingface_causal':
      return '#A3BE8C'; // Green
    case 'huggingface_classification':
      return '#EBCB8B'; // Yellow
    case 'huggingface_embedding':
      return '#B48EAD'; // Purple
    default:
      return '#4C566A';
  }
}

export interface Instance {
  id: string;
  config: InstanceConfig;
  status: InstanceStatus;
  port?: number;
  pid?: number;
  created_at: string;
  started_at?: string;
  error_message?: string;
  retry_count: number;
  supported_endpoints?: string[];
  // Ephemeral runtime fields (provided via separate state API/WS)
  busy?: boolean;
  prefill_progress?: number;
  active_slots?: number;
  // Ownership marker may also surface at the top level (payload position
  // depends on solar-control version — check both locations defensively).
  managed_by?: string | null;
  intent_id?: string | null;
}

export interface Host {
  id: string;
  name: string;
  url: string;
  api_key: string;
  status: HostStatus;
  last_seen?: string;
  memory?: MemoryInfo;
  gpu_type?: string;
  roles?: string[];
  disk_total_gb?: number;
  disk_used_gb?: number;
  disk_available_gb?: number;
  memory_available_gb?: number;
  version?: string;
  drain_state?: DrainState | null;
  drain_requested_at?: string | null;
  created_at: string;
}

export interface HostWithInstances extends Host {
  instances?: Instance[];
}

export interface LogMessage {
  seq: number;
  timestamp: string;
  line: string;
}

export interface ModelInfo {
  id: string;
  object: string;
  created: number;
  owned_by: string;
}

// Runtime state structures
export interface InstanceRuntimeState {
  instance_id: string;
  busy: boolean;
  phase: 'idle' | 'prefill' | 'generating';
  prefill_progress?: number | null;
  active_slots: number;
  slot_id?: number | null;
  task_id?: number | null;
  prefill_prompt_tokens?: number | null;
  generated_tokens?: number | null;
  decode_tps?: number | null;
  decode_ms_per_token?: number | null;
  checkpoint_index?: number | null;
  checkpoint_total?: number | null;
  timestamp: string;
}

export interface InstanceStateEvent {
  seq: number;
  timestamp: string;
  type: 'instance_state';
  data: InstanceRuntimeState;
}

export interface HostCreateRequest {
  name: string;
  url: string;
  api_key: string;
}

export interface PendingHost {
  pending_id: string;
  api_key_preview: string;
  host_name: string;
  instance_count?: number;
  connected_at: string;
}

export interface PendingHostApproveRequest {
  name: string;
  url: string;
}

export interface InstanceCreateRequest {
  config: InstanceConfig;
}

// Gateway monitoring
export interface GatewayStats {
  from: string;
  to: string;
  completed: number;
  missed: number;
  error: number;
  rerouted_requests: number;
  token_in_total: number;
  token_out_total: number;
  avg_tokens_in: number;
  avg_tokens_out: number;
  models?: Array<{ model: string; completed: number; token_in: number; token_out: number; avg_duration_s: number }>;
  hosts?: Array<{
    host_id: string;
    host_name?: string;
    completed: number;
    token_in: number;
    token_out: number;
    avg_duration_s: number;
  }>;
}

export interface GatewayRequestSummary {
  request_id: string;
  request_type?: string; // chat, completion, embedding, classification, etc.
  status: 'success' | 'error' | 'missed';
  model?: string;
  resolved_model?: string;
  endpoint?: string;
  endpoint_id?: string;
  client_ip?: string;
  stream?: boolean;
  attempts: number;
  start_timestamp?: string;
  end_timestamp: string;
  duration_s?: number;
  host_id?: string;
  host_name?: string;
  instance_id?: string;
  instance_url?: string;
  error_message?: string;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  decode_tps?: number;
  decode_ms_per_token?: number;
}

export interface GatewayRequestsResponse {
  from: string;
  to: string;
  page: number;
  limit: number;
  total: number;
  items: GatewayRequestSummary[];
}

export interface GatewayEventDTO {
  type: 'request_error' | 'request_reroute';
  data?: any;
  timestamp?: string;
}

// API Endpoint management (multi-tenant API keys)
export interface ApiEndpoint {
  id: string;
  name: string;
  api_key: string;
  description?: string | null;
  created_at: string;
  updated_at: string;
}

export interface EndpointCreateRequest {
  name: string;
  description?: string | null;
  api_key?: string | null;
}

export interface EndpointUpdateRequest {
  name?: string;
  description?: string | null;
  api_key?: string | null;
}

export interface EndpointUsageStats {
  total_requests: number;
  successful_requests: number;
  error_requests: number;
  missed_requests: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  avg_duration_s: number | null;
  avg_decode_tps: number | null;
}

export interface EndpointUsageResponse {
  endpoint: ApiEndpoint;
  hours: number;
  usage: EndpointUsageStats;
}

// Model catalog (D-018: GET /api/catalog/models)
export type CatalogEnrichmentStatus = 'ok' | 'partial' | 'unavailable';
export type CatalogSolarStatus = 'available' | 'deployed' | 'unavailable' | 'unknown';

export interface CatalogDeployedHost {
  host_id: string;
  host_name: string;
  size_bytes: number;
  path: string;
}

export interface CatalogRunningInstance {
  host_id: string;
  host_name: string;
  instance_id: string;
}

export interface CatalogSolarRuntime {
  status: CatalogSolarStatus;
  running_instances: number;
  deployed_hosts: CatalogDeployedHost[];
  instances: CatalogRunningInstance[];
}

export interface CatalogModelItem {
  name: string;
  category: string;
  description: string | null;
  versions_count: number;
  latest_version: string | null;
  created_at: string;
  solar: CatalogSolarRuntime;
}

export interface CatalogResponse {
  total: number;
  items: CatalogModelItem[];
  meta: { enrichment: CatalogEnrichmentStatus };
}

// ─── Catalog version listing and deletion (S-048 / U-008) ───

export interface CatalogModelVersion {
  version: string;
  harbor_ref: string;
  created_at: string;
  size_bytes: number | null;
  checksum: string | null;
  /** Per-version Solar runtime context — what blocks a version delete. */
  solar: {
    running_instances: number;
    deployed_hosts: CatalogDeployedHost[];
  };
}

export interface CatalogVersionsResponse {
  versions: CatalogModelVersion[];
}

export interface CatalogDeleteFailure {
  version: string;
  detail: string;
}

export interface CatalogDeleteResult {
  name: string;
  deleted: string[];
  failed: CatalogDeleteFailure[];
  artifact_removed: boolean;
  harbor_repository_removed: boolean;
}

// ─── Declarative deployment intents (U-003, spec deployment-intent.md §4/§10) ───

export type IntentPhase = 'pending' | 'reconciling' | 'ready' | 'degraded' | 'failed' | 'deleting' | 'deleted';
export type ReconcileState = 'idle' | 'in_progress' | 'succeeded' | 'failed';
export type IntentPriority = 'production' | 'staging' | 'ephemeral';
export type IntentStrategy = 'rolling' | 'immediate';

export interface IntentPlacement {
  roles: string[];
  gpu_type?: string | null;
  host_allow: string[];
  host_deny: string[];
}

export interface IntentResources {
  vram_gb?: number | null;
  ram_gb?: number | null;
}

export interface IntentCreateRequest {
  alias: string;
  model_source: string;
  replicas?: number;
  priority?: IntentPriority;
  strategy?: IntentStrategy;
  backend: Record<string, any>;
  placement?: Partial<IntentPlacement>;
  resources?: Partial<IntentResources>;
  metadata?: Record<string, string>;
}

/**
 * PUT /api/intents/{id} body (S-044). Same shape as create with
 * full-replace semantics: anything omitted is reset to its default, so the
 * form always sends a complete spec. `alias` must equal the stored alias.
 */
export type IntentUpdateRequest = IntentCreateRequest;

export interface ReplicaEntry {
  host_id?: string | null;
  host_name?: string | null;
  instance_id?: string | null;
  state?: string | null;
  model_source?: string | null;
  healthy: boolean;
  message?: string | null;
  updated_at?: string | null;
}

export interface IntentCondition {
  type: string;
  status: boolean;
  reason: string;
  message: string;
  last_transition: string;
}

export interface StrategyProgress {
  strategy: string;
  target_model_source?: string | null;
  /** Replicas this rollout is replacing, as identified by the reconciler. */
  drifted_instance_ids?: string[] | null;
  phase?: string | null;
  step?: string | null;
  updated: number;
  in_progress: number;
  failed: number;
  current_host_id?: string | null;
  current_instance_id?: string | null;
  pending_hosts?: string[];
  failed_hosts?: string[];
  started_at?: string | null;
  message?: string | null;
}

export interface IntentLastError {
  code: string;
  message: string;
  host_id?: string | null;
  source_uri?: string | null;
  at: string;
}

export interface IntentStatus {
  phase: IntentPhase;
  reconcile: ReconcileState;
  desired_replicas: number;
  observed_replicas: number;
  ready_replicas: number;
  updated_replicas: number;
  available: boolean;
  shortfall: number;
  replica_set: ReplicaEntry[];
  conditions: IntentCondition[];
  strategy_progress: StrategyProgress | null;
  last_error: IntentLastError | null;
  /** Set while an edited spec is still rolling out (S-044). */
  spec_changed_at?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  last_reconciled_at?: string | null;
  ready_at?: string | null;
}

export interface Intent {
  id: string;
  alias: string;
  model_source: string;
  replicas: number;
  priority: string;
  strategy: string;
  backend: Record<string, any>;
  placement: IntentPlacement;
  resources: IntentResources;
  metadata: Record<string, string>;
  status: IntentStatus;
}

export interface IntentDeletedResponse {
  id: string;
  alias: string;
  phase: IntentPhase;
  message: string;
}

// ─── Resource utilization (U-004, GET /api/resources — S-035 + PR #62) ───

export interface ActiveJobSummary {
  job_id: string;
  submission_id?: string | null;
  name?: string | null;
  status: string;
  current_step_name?: string | null;
  current_step_index?: number | null;
  last_step_name?: string | null;
  last_step_index?: number | null;
  pipeline?: string[];
  resource_hints?: Record<string, unknown>;
  started_at?: string | null;
  completed_at?: string | null;
  error_message?: string | null;
}

export interface HostInstanceSummary {
  id: string;
  alias?: string | null;
  status?: string | null;
  backend_type?: string | null;
  port?: number | null;
  supported_endpoints?: string[];
  /** 'intent' for reconciler-managed replicas, absent for manual instances (S-043) */
  managed_by?: string | null;
  intent_id?: string | null;
}

export interface HostReservationSummary {
  id: string;
  job_id: string; // owner — attribution for the reserved segment
  workload_type: string;
  status: string; // 'pending' | 'running'
  vram_gb?: number;
  ram_gb?: number;
  disk_gb?: number | null;
  actual_vram_gb?: number | null; // set only for running reservations
  actual_ram_gb?: number | null;
  actual_disk_gb?: number | null;
  expires_at?: string | null;
}

export interface HostResourceSnapshot {
  host_id: string;
  host_name: string;
  url: string;
  status: HostStatus;
  drain_state?: DrainState | null;
  roles: string[];
  gpu_type?: string | null;
  version?: string | null;
  reachable: boolean;
  error?: string | null;
  vram_total_gb?: number | null;
  vram_system_used_gb?: number | null;
  vram_reserved_headroom_gb?: number | null;
  vram_reported_used_gb?: number | null;
  vram_available_gb?: number | null;
  ram_total_gb?: number | null;
  ram_system_used_gb?: number | null;
  ram_reserved_headroom_gb?: number | null;
  ram_reported_used_gb?: number | null;
  ram_available_gb?: number | null;
  disk_total_gb?: number | null;
  disk_system_used_gb?: number | null;
  disk_reserved_headroom_gb?: number | null;
  disk_reported_used_gb?: number | null;
  disk_available_gb?: number | null;
  instance_count: number;
  running_instance_count: number;
  instances: HostInstanceSummary[]; // PR #62 — aliases included
  active_jobs: ActiveJobSummary[];
  reservation_count: number;
  reservation_vram_total_gb: number;
  reservation_ram_total_gb: number;
  reservation_disk_total_gb: number;
  reservations: HostReservationSummary[]; // PR #62 — per-reservation details
  vram_training_used_gb: number; // PR #62 — Σ actuals of running reservations
  ram_training_used_gb: number;
  disk_training_used_gb: number;
  snapshot_timestamp?: string | null;
}

export interface AggregatedResourceResponse {
  hosts: HostResourceSnapshot[];
  total_hosts: number;
  reachable_hosts: number;
  unreachable_hosts: number;
}

export interface ResourcesQueryParams {
  role?: string;
  gpu_type?: string;
  min_available_vram_gb?: number;
  min_available_ram_gb?: number;
}

// ─── Host draining (U-005, S-043 §5) ───

/** Something that prevents a drain from starting. */
export interface DrainBlocker {
  kind: 'manual_instance' | 'active_job' | string;
  id: string;
  name?: string | null;
  detail: string;
}

/** An intent-managed replica still on a draining host. */
export interface DrainReplica {
  instance_id: string;
  alias?: string | null;
  intent_id?: string | null;
  status?: string | null;
  /** Why this replica cannot be moved right now; null while it is movable. */
  blocked_reason?: string | null;
}

export interface HostDrainStatus {
  host_id: string;
  host_name: string;
  drain_state?: DrainState | null;
  drain_requested_at?: string | null;
  /** Draining, but every remaining replica is blocked — needs an operator. */
  stalled: boolean;
  managed_remaining: number;
  manual_running: number;
  replicas: DrainReplica[];
  blockers: DrainBlocker[];
}

// ─── Host storage management (GET /api/storage/hosts etc.) ───

export type StoredModelOrigin = 'repository' | 'huggingface' | 'local' | 'unknown';

/** An instance currently using a stored model. */
export interface StoredInstanceRef {
  instance_id: string;
  alias: string;
  status: string;
}

/** One model stored on one host (manifest entry + usage). */
export interface StoredModel {
  slug: string;
  model_name: string | null;
  version: string | null;
  category: string | null;
  source_uri: string | null;
  origin: StoredModelOrigin;
  harbor_ref: string | null;
  path: string;
  size_bytes: number;
  downloaded_at: string | null;
  in_use_by: StoredInstanceRef[];
}

/** Storage inventory for one host. */
export interface HostStorage {
  host_id: string;
  host_name: string;
  reachable: boolean;
  error: string | null;
  disk_total_gb: number | null;
  disk_used_gb: number | null;
  disk_available_gb: number | null;
  total_size_bytes: number;
  models: StoredModel[];
}

/** Cluster-wide storage inventory. */
export interface StorageResponse {
  hosts: HostStorage[];
  unreachable_hosts: string[];
  generated_at: string;
}

export interface StorageDeleteItem {
  host_id: string;
  slug: string;
}

export type StorageDeleteStatus = 'deleted' | 'in_use' | 'not_found' | 'unreachable' | 'error';

/** Per-item outcome of a bulk delete. */
export interface StorageDeleteResult {
  host_id: string;
  host_name: string;
  slug: string;
  status: StorageDeleteStatus;
  detail: string | null;
  freed_bytes: number;
}

// ─── Artifact upload (S-047 / U-007) ───

export type UploadCategory = 'model' | 'dataset';

export type UploadSessionState = 'pending' | 'uploading' | 'completing' | 'completed' | 'failed' | 'aborted';

export interface UploadFileDeclaration {
  path: string;
  size: number;
}

export interface CreateUploadRequest {
  category: UploadCategory;
  name: string;
  version?: string;
  files: UploadFileDeclaration[];
  metadata?: Record<string, any>;
}

export interface CreateUploadResponse {
  upload_id: string;
  harbor_ref: string;
  name: string;
  version: string;
  expires_at: string;
}

export interface UploadFileResult {
  path: string;
  digest: string;
  size: number;
}

export interface UploadFileStatus {
  path: string;
  size: number;
  digest: string | null;
  uploaded: boolean;
}

export interface UploadStatusResponse {
  upload_id: string;
  state: UploadSessionState;
  files: UploadFileStatus[];
  bytes_total: number;
  bytes_done: number;
}

export interface CompleteUploadResponse {
  name: string;
  version: string;
  category: UploadCategory;
  harbor_ref: string;
  size_bytes: number;
  registration: Record<string, any>;
}
