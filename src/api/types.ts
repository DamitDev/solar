// Type definitions for Solar API

export type InstanceStatus = 'stopped' | 'starting' | 'running' | 'failed' | 'stopping';

export type HostStatus = 'online' | 'offline' | 'error';

export type BackendType = 'llamacpp' | 'huggingface_causal' | 'huggingface_classification' | 'huggingface_embedding';

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
}

// llama.cpp specific config
export interface LlamaCppConfig extends BaseInstanceConfig {
  backend_type: 'llamacpp';
  model: string;
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
  | LlamaCppConfig
  | HuggingFaceCausalConfig
  | HuggingFaceClassificationConfig
  | HuggingFaceEmbeddingConfig;

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
