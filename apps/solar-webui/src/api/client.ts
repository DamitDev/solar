import axios, { AxiosInstance } from 'axios';
import {
  Host,
  HostCreateRequest,
  Instance,
  ModelInfo,
  InstanceRuntimeState,
  GatewayStats,
  GatewayRequestsResponse,
  GatewayEventDTO,
  GatewayTimeseries,
  GatewayBucket,
  GatewayGroupBy,
  ApiEndpoint,
  ApiKey,
  ApiKeyCreateRequest,
  ApiKeyUpdateRequest,
  EndpointModelsResponse,
  ModelPreviewRequest,
  ModelPreviewResponse,
  PullProgressEntry,
  EndpointCreateRequest,
  EndpointUpdateRequest,
  EndpointUsageResponse,
  PendingHost,
  PendingHostApproveRequest,
  CatalogResponse,
  CatalogVersionsResponse,
  CatalogDeleteResult,
  Intent,
  IntentCreateRequest,
  IntentUpdateRequest,
  IntentDeletedResponse,
  AggregatedResourceResponse,
  ResourcesQueryParams,
  HostDrainStatus,
  StorageResponse,
  HostStorage,
  StorageDeleteItem,
  StorageDeleteResult,
  CreateUploadRequest,
  CreateUploadResponse,
  UploadFileResult,
  UploadStatusResponse,
  CompleteUploadResponse,
} from './types';

const DEFAULT_RELATIVE_CONTROL_BASE = '/api/control';

const isAbsoluteUrl = (value: string) => /^https?:\/\//i.test(value);

const normalizeHttpBase = (value?: string | null): string => {
  let result = (value || '').trim();
  if (!result) {
    return DEFAULT_RELATIVE_CONTROL_BASE;
  }
  if (isAbsoluteUrl(result)) {
    return result.replace(/\/+$/, '');
  }
  if (!result.startsWith('/')) {
    result = `/${result}`;
  }
  return result.replace(/\/+$/, '');
};

class SolarClient {
  private client: AxiosInstance;
  private httpBase: string;
  private _managementApiKey: string | null = null;
  private _directApiKey: string = '';

  constructor(baseURL?: string) {
    const overrideBase =
      baseURL ||
      import.meta.env.VITE_SOLAR_WEBUI_API_BASE ||
      import.meta.env.VITE_SOLAR_CONTROL_URL ||
      DEFAULT_RELATIVE_CONTROL_BASE;

    this.httpBase = normalizeHttpBase(overrideBase);

    if (import.meta.env.DEV) {
      console.log('SolarClient initialized:', {
        httpBase: this.httpBase,
      });
    }

    this.client = axios.create({
      baseURL: this.httpBase,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    const directApiKey = import.meta.env.VITE_SOLAR_CONTROL_API_KEY;
    if (isAbsoluteUrl(this.httpBase) && directApiKey) {
      this._directApiKey = directApiKey;
      this.client.defaults.headers.common['X-API-Key'] = directApiKey;
      this.client.defaults.headers.common['Authorization'] = `Bearer ${directApiKey}`;
    }

    this.client.interceptors.response.use(
      (response) => response,
      (error) => {
        if (error.response?.status === 401) {
          console.error('❌ 401 Unauthorized - solar-webui proxy may be missing SOLAR_CONTROL_API_KEY');
        }
        return Promise.reject(error);
      },
    );
  }

  // Host Management
  async getHosts(): Promise<Host[]> {
    const response = await this.client.get('/api/hosts');
    return response.data;
  }

  async getHost(hostId: string): Promise<Host> {
    const response = await this.client.get(`/api/hosts/${hostId}`);
    return response.data;
  }

  async createHost(data: HostCreateRequest): Promise<{ host: Host; message: string }> {
    const response = await this.client.post('/api/hosts', data);
    return response.data;
  }

  async deleteHost(hostId: string): Promise<{ host: Host; message: string }> {
    const response = await this.client.delete(`/api/hosts/${hostId}`);
    return response.data;
  }

  async refreshAllHosts(): Promise<{
    message: string;
    results: Array<{ host_id: string; name: string; status: string; message: string }>;
  }> {
    const response = await this.client.post('/api/hosts/refresh-all');
    return response.data;
  }

  // Pending host approval
  async getPendingHosts(): Promise<PendingHost[]> {
    const response = await this.client.get('/api/hosts/pending');
    return response.data;
  }

  async approveHost(pendingId: string, data: PendingHostApproveRequest): Promise<{ host: Host; message: string }> {
    const response = await this.client.post(`/api/hosts/pending/${pendingId}/approve`, data);
    return response.data;
  }

  async rejectHost(pendingId: string): Promise<{ message: string }> {
    const response = await this.client.post(`/api/hosts/pending/${pendingId}/reject`);
    return response.data;
  }

  async getHostInstances(hostId: string): Promise<Instance[]> {
    const response = await this.client.get(`/api/hosts/${hostId}/instances`);
    return response.data;
  }

  // Instance Control (via solar-control proxy)
  async startInstance(hostId: string, instanceId: string): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.post(`/api/hosts/${hostId}/instances/${instanceId}/start`);
    return response.data;
  }

  async stopInstance(hostId: string, instanceId: string): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.post(`/api/hosts/${hostId}/instances/${instanceId}/stop`);
    return response.data;
  }

  async restartInstance(hostId: string, instanceId: string): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.post(`/api/hosts/${hostId}/instances/${instanceId}/restart`);
    return response.data;
  }

  async createInstance(hostId: string, config: any): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.post(`/api/hosts/${hostId}/instances`, { config });
    return response.data;
  }

  async updateInstance(
    hostId: string,
    instanceId: string,
    config: any,
  ): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.put(`/api/hosts/${hostId}/instances/${instanceId}`, { config });
    return response.data;
  }

  async deleteInstance(hostId: string, instanceId: string): Promise<{ instance: Instance; message: string }> {
    const response = await this.client.delete(`/api/hosts/${hostId}/instances/${instanceId}`);
    return response.data;
  }

  // Instance runtime state (via solar-control proxy)
  async getInstanceState(hostId: string, instanceId: string): Promise<InstanceRuntimeState> {
    const response = await this.client.get(`/api/hosts/${hostId}/instances/${instanceId}/state`);
    return response.data;
  }

  // Instance logs (via solar-control proxy)
  async getInstanceLogs(
    hostId: string,
    instanceId: string,
  ): Promise<Array<{ seq: number; timestamp: string; line: string }>> {
    const response = await this.client.get(`/api/hosts/${hostId}/instances/${instanceId}/logs`);
    return response.data;
  }

  /**
   * Get the base URL for Socket.IO connection.
   * For relative paths (e.g. /api/control), returns window.location.origin.
   * For absolute URLs, returns the control base URL.
   */
  getControlSocketIOUrl(): string {
    if (isAbsoluteUrl(this.httpBase)) {
      return this.httpBase;
    }
    if (typeof window !== 'undefined') {
      return window.location.origin;
    }
    return '';
  }

  /**
   * Get the Socket.IO path for the connection.
   * For relative paths: /api/control/socket.io (proxy rewrites to /socket.io)
   * For absolute URLs: /socket.io (control server root)
   */
  getSocketIOPath(): string {
    if (isAbsoluteUrl(this.httpBase)) {
      return '/socket.io';
    }
    return '/api/control/socket.io';
  }

  /**
   * Get the management API key.
   *
   * Resolution order:
   *  1. window.__SOLAR_CONFIG__ (injected by Express server at runtime)
   *  2. VITE_SOLAR_CONTROL_API_KEY (baked by Vite at build time, dev mode)
   */
  getManagementApiKey(): string {
    if (this._managementApiKey) return this._managementApiKey;

    const runtimeKey = (window as any).__SOLAR_CONFIG__?.SOLAR_CONTROL_API_KEY;
    if (runtimeKey) {
      this._managementApiKey = runtimeKey;
      return runtimeKey;
    }

    const envKey = import.meta.env.VITE_SOLAR_CONTROL_API_KEY;
    if (envKey) {
      this._managementApiKey = envKey;
      return envKey;
    }
    return '';
  }

  // Gateway monitoring
  async getGatewayStats(params: {
    from?: string;
    to?: string;
    request_type?: string;
    endpoint_id?: string;
  }): Promise<GatewayStats> {
    const response = await this.client.get('/api/gateway/stats', { params });
    return response.data as GatewayStats;
  }

  /** Bucketed gateway traffic for the dashboard charts. `bucket: 'auto'` lets
   *  the server pick a resolution that suits the range. */
  async getGatewayTimeseries(params: {
    from?: string;
    to?: string;
    bucket?: GatewayBucket | 'auto';
    group_by?: GatewayGroupBy;
    request_type?: string;
    model?: string;
    host_id?: string;
    endpoint_id?: string;
  }): Promise<GatewayTimeseries> {
    const response = await this.client.get('/api/gateway/timeseries', { params });
    return response.data as GatewayTimeseries;
  }

  async listGatewayRequests(params: {
    from?: string;
    to?: string;
    status?: 'all' | 'success' | 'error' | 'missed';
    request_type?: string;
    model?: string;
    host_id?: string;
    endpoint_id?: string;
    page?: number;
    limit?: number;
  }): Promise<GatewayRequestsResponse> {
    const response = await this.client.get('/api/gateway/requests', { params });
    return response.data as GatewayRequestsResponse;
  }

  async getRecentGatewayEvents(params: {
    from?: string;
    to?: string;
    types?: string; // comma separated
    limit?: number;
    endpoint_id?: string;
  }): Promise<{ from: string; to: string; types: string[]; items: GatewayEventDTO[] }> {
    const response = await this.client.get('/api/gateway/events/recent', { params });
    return response.data as { from: string; to: string; types: string[]; items: GatewayEventDTO[] };
  }

  // Model catalog (D-018)
  async getCatalogModels(params: { search?: string; limit?: number; offset?: number }): Promise<CatalogResponse> {
    const response = await this.client.get('/api/catalog/models', { params });
    return response.data as CatalogResponse;
  }

  // Catalog version listing and deletion (S-048 / U-008)

  /** Versions of one catalog model, each with its per-version runtime block. */
  async getCatalogModelVersions(name: string): Promise<CatalogVersionsResponse> {
    const response = await this.client.get(`/api/catalog/models/${encodeURIComponent(name)}/versions`);
    return response.data as CatalogVersionsResponse;
  }

  /** Delete one version — Harbor first, then unregister. 409 = running instance. */
  async deleteCatalogModelVersion(name: string, version: string): Promise<void> {
    await this.client.delete(`/api/catalog/models/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}`);
  }

  /** Delete a whole model repository; resolves with per-version results. */
  async deleteCatalogModel(name: string): Promise<CatalogDeleteResult> {
    const response = await this.client.delete(`/api/catalog/models/${encodeURIComponent(name)}`);
    return response.data as CatalogDeleteResult;
  }

  // Declarative deployment intents (U-003)
  async createIntent(data: IntentCreateRequest): Promise<Intent> {
    const response = await this.client.post('/api/intents', data);
    return response.data as Intent;
  }

  async getIntents(params?: { alias?: string; priority?: string; phase?: string }): Promise<Intent[]> {
    const response = await this.client.get('/api/intents', { params });
    return response.data as Intent[];
  }

  async getIntent(id: string): Promise<Intent> {
    const response = await this.client.get(`/api/intents/${id}`);
    return response.data as Intent;
  }

  /**
   * Replace an intent's spec (U-006, S-044). Full-replace semantics: send the
   * complete spec, since anything omitted is reset to its default server-side.
   */
  async updateIntent(id: string, data: IntentUpdateRequest): Promise<Intent> {
    const response = await this.client.put(`/api/intents/${id}`, data);
    return response.data as Intent;
  }

  async deleteIntent(id: string, orphan = false): Promise<IntentDeletedResponse> {
    const response = await this.client.delete(`/api/intents/${id}`, { params: { orphan } });
    return response.data as IntentDeletedResponse;
  }

  // Resource utilization (U-004, S-035)
  async getResources(params?: ResourcesQueryParams): Promise<AggregatedResourceResponse> {
    const response = await this.client.get('/api/resources', { params });
    return response.data as AggregatedResourceResponse;
  }

  // Host draining (U-005, S-043)

  /**
   * Start draining a host. Rejects with 409 and a `blockers` list while
   * manual instances are running or job steps are active.
   */
  async drainHost(hostId: string): Promise<HostDrainStatus> {
    const response = await this.client.post(`/api/hosts/${hostId}/drain`);
    return response.data as HostDrainStatus;
  }

  /** Cancel a drain, or return a drained host to service. */
  async resumeHost(hostId: string): Promise<HostDrainStatus> {
    const response = await this.client.delete(`/api/hosts/${hostId}/drain`);
    return response.data as HostDrainStatus;
  }

  /** Drain progress: what remains, what blocks it, and why a replica is stuck. */
  async getDrainStatus(hostId: string): Promise<HostDrainStatus> {
    const response = await this.client.get(`/api/hosts/${hostId}/drain`);
    return response.data as HostDrainStatus;
  }

  // Host storage management (per-host model inventory + guarded deletion)

  /** Cluster-wide storage view: per-host manifests joined with usage. */
  async getStorage(): Promise<StorageResponse> {
    const response = await this.client.get('/api/storage/hosts');
    return response.data as StorageResponse;
  }

  /** Fresh storage view for a single host. */
  async getHostStorage(hostId: string): Promise<HostStorage> {
    const response = await this.client.get(`/api/storage/hosts/${hostId}`);
    return response.data as HostStorage;
  }

  /** Delete one model on one host. 404 = already gone, 409 = in use. */
  async deleteStoredModel(hostId: string, slug: string): Promise<{ detail: string; name: string }> {
    const response = await this.client.delete(`/api/storage/hosts/${hostId}/models/${encodeURIComponent(slug)}`);
    return response.data as { detail: string; name: string };
  }

  /** Bulk delete across hosts; always resolves with per-item outcomes. */
  async deleteStoredModels(items: StorageDeleteItem[]): Promise<StorageDeleteResult[]> {
    const response = await this.client.post('/api/storage/delete', { items });
    return response.data as StorageDeleteResult[];
  }

  // OpenAI Gateway
  async getModels(): Promise<ModelInfo[]> {
    const response = await this.client.get('/v1/models');
    return response.data.data;
  }

  async chatCompletion(model: string, messages: Array<{ role: string; content: string }>) {
    const response = await this.client.post('/v1/chat/completions', {
      model,
      messages,
      stream: false,
    });
    return response.data;
  }

  // Health check
  async healthCheck(): Promise<{ status: string; service: string; version: string }> {
    const response = await this.client.get('/health');
    return response.data;
  }

  // API Endpoint management
  async getEndpoints(): Promise<ApiEndpoint[]> {
    const response = await this.client.get('/api/endpoints');
    return response.data;
  }

  // C4: latest model pull progress per (host, source_uri).
  async getPulls(): Promise<Record<string, PullProgressEntry>> {
    const response = await this.client.get('/api/pulls');
    return response.data;
  }

  async createEndpoint(data: EndpointCreateRequest): Promise<ApiEndpoint> {
    const response = await this.client.post('/api/endpoints', data);
    return response.data;
  }

  async getEndpoint(id: string): Promise<ApiEndpoint> {
    const response = await this.client.get(`/api/endpoints/${id}`);
    return response.data;
  }

  async updateEndpoint(id: string, data: EndpointUpdateRequest): Promise<ApiEndpoint> {
    const response = await this.client.put(`/api/endpoints/${id}`, data);
    return response.data;
  }

  async deleteEndpoint(id: string): Promise<{ message: string; id: string }> {
    const response = await this.client.delete(`/api/endpoints/${id}`);
    return response.data;
  }

  async getEndpointUsage(id: string, hours: number = 24): Promise<EndpointUsageResponse> {
    const response = await this.client.get(`/api/endpoints/${id}/usage`, { params: { hours } });
    return response.data;
  }

  async getEndpointModels(id: string): Promise<EndpointModelsResponse> {
    const response = await this.client.get(`/api/endpoints/${id}/models`);
    return response.data;
  }

  async previewEndpointModels(data: ModelPreviewRequest): Promise<ModelPreviewResponse> {
    const response = await this.client.post('/api/endpoints/preview-models', data);
    return response.data;
  }

  // API key management (keys are separate from endpoints)

  async getApiKeys(endpointId?: string): Promise<ApiKey[]> {
    const params = endpointId ? { params: { endpoint_id: endpointId } } : undefined;
    const response = await this.client.get('/api/api-keys', params);
    return response.data;
  }

  async createApiKey(data: ApiKeyCreateRequest): Promise<ApiKey> {
    const response = await this.client.post('/api/api-keys', data);
    return response.data;
  }

  async updateApiKey(id: string, data: ApiKeyUpdateRequest): Promise<ApiKey> {
    const response = await this.client.put(`/api/api-keys/${id}`, data);
    return response.data;
  }

  async rotateApiKey(id: string): Promise<ApiKey> {
    const response = await this.client.post(`/api/api-keys/${id}/rotate`);
    return response.data;
  }

  async deleteApiKey(id: string): Promise<{ message: string; id: string }> {
    const response = await this.client.delete(`/api/api-keys/${id}`);
    return response.data;
  }

  // Artifact upload (S-047 / U-007)

  async createUpload(data: CreateUploadRequest): Promise<CreateUploadResponse> {
    const response = await this.client.post('/api/uploads', data);
    return response.data as CreateUploadResponse;
  }

  /**
   * Stream one file into an upload session via XMLHttpRequest.
   *
   * XHR is used (not fetch) because `upload.onprogress` is the only
   * reliable source of upload progress events (spec §5.3).
   */
  uploadFile(
    uploadId: string,
    path: string,
    file: File,
    onProgress?: (sentBytes: number, totalBytes: number) => void,
  ): Promise<UploadFileResult> {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const url = `${this.httpBase}/api/uploads/${encodeURIComponent(uploadId)}/files?path=${encodeURIComponent(path)}`;
      xhr.open('PUT', url);
      xhr.setRequestHeader('Content-Type', 'application/octet-stream');
      // Direct-API mode mirrors the axios client's injected headers; in
      // proxied mode the Express proxy injects the management key.
      if (this._directApiKey) {
        xhr.setRequestHeader('X-API-Key', this._directApiKey);
        xhr.setRequestHeader('Authorization', `Bearer ${this._directApiKey}`);
      }
      xhr.upload.onprogress = (event) => {
        if (event.lengthComputable && onProgress) {
          onProgress(event.loaded, event.total);
        }
      };
      xhr.onload = () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as UploadFileResult);
          } catch {
            reject(new Error('Invalid upload response from server'));
          }
          return;
        }
        reject(new Error(extractUploadError(xhr)));
      };
      xhr.onerror = () => reject(new Error('Network error during upload'));
      xhr.send(file);
    });
  }

  async getUploadStatus(uploadId: string): Promise<UploadStatusResponse> {
    const response = await this.client.get(`/api/uploads/${uploadId}`);
    return response.data as UploadStatusResponse;
  }

  async completeUpload(uploadId: string): Promise<CompleteUploadResponse> {
    const response = await this.client.post(`/api/uploads/${uploadId}/complete`);
    return response.data as CompleteUploadResponse;
  }

  async abortUpload(uploadId: string): Promise<void> {
    await this.client.delete(`/api/uploads/${uploadId}`);
  }
}

function extractUploadError(xhr: XMLHttpRequest): string {
  try {
    const data = JSON.parse(xhr.responseText);
    if (typeof data?.detail === 'string' && data.detail) return data.detail;
    if (typeof data?.error?.message === 'string' && data.error.message) {
      return data.error.message;
    }
  } catch {
    /* non-JSON error body */
  }
  return `Upload failed with status ${xhr.status}`;
}

// Export a default instance
const solarClient = new SolarClient();
export default solarClient;
export { SolarClient };
