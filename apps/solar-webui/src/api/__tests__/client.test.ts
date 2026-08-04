import MockAdapter from 'axios-mock-adapter';
import { describe, expect, it, beforeEach } from 'vitest';
import { SolarClient } from '@/api/client';

describe('SolarClient', () => {
  let client: SolarClient;
  let mock: MockAdapter;

  beforeEach(() => {
    client = new SolarClient('http://control.test');
    mock = new MockAdapter((client as any).client);
  });

  it('lists hosts', async () => {
    const hosts = [{ id: 'host-1', name: 'host-a' }];
    mock.onGet('/api/hosts').reply(200, hosts);
    await expect(client.getHosts()).resolves.toEqual(hosts);
  });

  it('fetches a single host', async () => {
    mock.onGet('/api/hosts/host-1').reply(200, { id: 'host-1' });
    await expect(client.getHost('host-1')).resolves.toEqual({ id: 'host-1' });
  });

  it('creates a host via POST', async () => {
    const payload = { name: 'host-c', url: 'http://10.0.0.9:8000', api_key: 'k' };
    const reply = { host: { id: 'host-3' }, message: 'created' };
    mock.onPost('/api/hosts', payload).reply(201, reply);
    await expect(client.createHost(payload as any)).resolves.toEqual(reply);
  });

  it('deletes a host', async () => {
    mock.onDelete('/api/hosts/host-9').reply(200, { host: { id: 'host-9' }, message: 'deleted' });
    await expect(client.deleteHost('host-9')).resolves.toMatchObject({ message: 'deleted' });
  });

  it('lists pending hosts', async () => {
    mock.onGet('/api/hosts/pending').reply(200, [{ id: 'p1' }]);
    await expect(client.getPendingHosts()).resolves.toEqual([{ id: 'p1' }]);
  });

  it('approves a pending host', async () => {
    mock.onPost('/api/hosts/pending/p1/approve').reply(200, { host: { id: 'host-1' }, message: 'ok' });
    await expect(client.approveHost('p1', { approve: true } as any)).resolves.toMatchObject({ message: 'ok' });
  });

  it('rejects a pending host', async () => {
    mock.onPost('/api/hosts/pending/p1/reject').reply(200, { message: 'rejected' });
    await expect(client.rejectHost('p1')).resolves.toEqual({ message: 'rejected' });
  });

  it('lists instances for a host', async () => {
    mock.onGet('/api/hosts/host-1/instances').reply(200, [{ id: 'i1' }]);
    await expect(client.getHostInstances('host-1')).resolves.toEqual([{ id: 'i1' }]);
  });

  it('starts, stops and restarts instances', async () => {
    const reply = { instance: { id: 'i1' }, message: 'ok' };
    mock.onPost('/api/hosts/host-1/instances/i1/start').reply(200, reply);
    mock.onPost('/api/hosts/host-1/instances/i1/stop').reply(200, reply);
    mock.onPost('/api/hosts/host-1/instances/i1/restart').reply(200, reply);
    await expect(client.startInstance('host-1', 'i1')).resolves.toEqual(reply);
    await expect(client.stopInstance('host-1', 'i1')).resolves.toEqual(reply);
    await expect(client.restartInstance('host-1', 'i1')).resolves.toEqual(reply);
  });

  it('deletes an instance via DELETE', async () => {
    mock.onDelete('/api/hosts/host-1/instances/i1').reply(200, { instance: { id: 'i1' }, message: 'deleted' });
    await expect(client.deleteInstance('host-1', 'i1')).resolves.toMatchObject({ message: 'deleted' });
  });

  it('fetches instance runtime state', async () => {
    const state = { instance_id: 'i1', busy: true, phase: 'running' };
    mock.onGet('/api/hosts/host-1/instances/i1/state').reply(200, state);
    await expect(client.getInstanceState('host-1', 'i1')).resolves.toEqual(state);
  });

  it('updates an intent via PUT (S-044 full replace)', async () => {
    const spec = { alias: 'iris:v1', model_source: 'repo://iris:v2', backend: { backend_type: 'llamacpp' } };
    mock.onPut('/api/intents/intent-1', spec).reply(200, { id: 'intent-1', ...spec });
    await expect(client.updateIntent('intent-1', spec as any)).resolves.toMatchObject({
      model_source: 'repo://iris:v2',
    });
  });

  it('drains, resumes and reads drain status of a host', async () => {
    const status = { host_id: 'host-1', drain_state: 'draining', managed_remaining: 2 };
    mock.onPost('/api/hosts/host-1/drain').reply(202, status);
    mock.onDelete('/api/hosts/host-1/drain').reply(200, { ...status, drain_state: null });
    mock.onGet('/api/hosts/host-1/drain').reply(200, status);

    await expect(client.drainHost('host-1')).resolves.toEqual(status);
    await expect(client.resumeHost('host-1')).resolves.toMatchObject({ drain_state: null });
    await expect(client.getDrainStatus('host-1')).resolves.toEqual(status);
  });

  it('keeps the blockers payload on a rejected drain (409)', async () => {
    mock.onPost('/api/hosts/host-1/drain').reply(409, {
      detail: {
        detail: 'Host cannot be drained yet',
        blockers: [{ kind: 'manual_instance', id: 'i-2', detail: 'still running' }],
      },
    });
    await expect(client.drainHost('host-1')).rejects.toMatchObject({
      response: { status: 409, data: { detail: { blockers: [{ id: 'i-2' }] } } },
    });
  });

  it('rejects on 401 (interceptor keeps the rejection)', async () => {
    mock.onGet('/api/hosts').reply(401, { detail: 'Authentication required' });
    await expect(client.getHosts()).rejects.toMatchObject({ response: { status: 401 } });
  });
});
