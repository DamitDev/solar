import { useEffect, useRef, useState } from 'react';
import ReactFlow, {
  Node,
  Edge,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  MarkerType,
  Position,
} from 'reactflow';
import 'reactflow/dist/style.css';
import { X, Tags, Server, Binary, Search, MessageSquare, Zap } from 'lucide-react';
import { useRoutingEventsContext, RequestState } from '@/context/RoutingEventsContext';
import { useEventStreamContext } from '@/context/EventStreamContext';
import { useInstances } from '@/hooks/useInstances';
import {
  Instance,
  ApiEndpoint,
  getBackendType,
  getModelCategory,
  getFullModelLabel,
  getFullModelHexColor,
} from '@/api/types';
import solarClient from '@/api/client';

const SOLAR_CONTROL_NODE_ID = 'solar-control';
const ENDPOINT_PALETTE = ['#5E81AC', '#A3BE8C', '#EBCB8B', '#B48EAD', '#D08770', '#8FBCBB'];
const GROUP_GENERATION_ID = 'group-generation';
const GROUP_CLASSIFICATION_ID = 'group-classification';
const GROUP_EMBEDDING_ID = 'group-embedding';
const GROUP_RERANKER_ID = 'group-reranker';

function getStatusColor(status: RequestState['status']): string {
  switch (status) {
    case 'pending':
      return '#4C566A';
    case 'routed':
    case 'processing':
      return '#D08770';
    case 'success':
      return '#A3BE8C';
    case 'error':
      return '#BF616A';
    default:
      return '#4C566A';
  }
}

function getBrighterColor(color: string): string {
  const r = parseInt(color.slice(1, 3), 16);
  const g = parseInt(color.slice(3, 5), 16);
  const b = parseInt(color.slice(5, 7), 16);

  const brighterR = Math.min(255, Math.round(r * 1.3));
  const brighterG = Math.min(255, Math.round(g * 1.3));
  const brighterB = Math.min(255, Math.round(b * 1.3));

  return `#${brighterR.toString(16).padStart(2, '0')}${brighterG.toString(16).padStart(2, '0')}${brighterB.toString(16).padStart(2, '0')}`;
}

function getEndpointNodeId(endpointId: string): string {
  return `endpoint-${endpointId}`;
}

function getEndpointColor(endpoints: ApiEndpoint[], endpointId: string): string {
  const idx = endpoints.findIndex((ep) => ep.id === endpointId);
  return idx >= 0 ? ENDPOINT_PALETTE[idx % ENDPOINT_PALETTE.length] : '#4C566A';
}

function getInstanceCategory(instance: Instance): 'generation' | 'classification' | 'embedding' | 'reranker' {
  return getModelCategory(instance.config);
}

function getBackendDisplay(instance: Instance): { color: string; label: string } {
  return {
    color: getFullModelHexColor(instance.config),
    label: getFullModelLabel(instance.config),
  };
}

interface InstanceData {
  hostId: string;
  hostName: string;
  instance: Instance;
}

export function RoutingGraph() {
  const { requests, removeRequest } = useRoutingEventsContext();
  const { getInstanceState } = useEventStreamContext();
  const { hosts, loading } = useInstances();
  const [endpoints, setEndpoints] = useState<ApiEndpoint[]>([]);

  const [nodes, setNodes, onNodesChange] = useNodesState([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState([]);
  const previousEdgeIdsRef = useRef<Set<string>>(new Set());

  // Fetch endpoints on mount and periodically
  useEffect(() => {
    const fetchEndpoints = async () => {
      try {
        const data = await solarClient.getEndpoints();
        setEndpoints(data);
      } catch (err) {
        console.error('Failed to fetch endpoints:', err);
      }
    };
    fetchEndpoints();
    const timer = setInterval(fetchEndpoints, 10000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const newNodes: Node[] = [];
    const newEdges: Edge[] = [];
    const currentEdgeIds = new Set<string>();

    const onlineHosts = hosts.filter((h) => h.status === 'online').length;
    const totalInstances = hosts.reduce((sum, h) => sum + h.instances.length, 0);
    const runningInstances = hosts.reduce(
      (sum, h) => sum + h.instances.filter((i) => i.status === 'running').length,
      0,
    );
    const activeRequests = Array.from(requests.values()).filter(
      (r) => r.status === 'processing' || r.status === 'routed',
    ).length;

    const processingInstances = new Set<string>();
    Array.from(requests.values()).forEach((req) => {
      if ((req.status === 'processing' || req.status === 'routed') && req.host_id && req.instance_id) {
        processingInstances.add(`${req.host_id}-${req.instance_id}`);
      }
    });

    // Group running instances by category and then by host
    const generationByHost = new Map<string, { hostName: string; hostStatus: string; instances: InstanceData[] }>();
    const classificationByHost = new Map<string, { hostName: string; hostStatus: string; instances: InstanceData[] }>();
    const embeddingByHost = new Map<string, { hostName: string; hostStatus: string; instances: InstanceData[] }>();
    const rerankerByHost = new Map<string, { hostName: string; hostStatus: string; instances: InstanceData[] }>();

    hosts.forEach((host) => {
      host.instances
        .filter((instance) => instance.status === 'running')
        .forEach((instance) => {
          const category = getInstanceCategory(instance);
          const item: InstanceData = { hostId: host.id, hostName: host.name, instance };

          if (category === 'classification') {
            if (!classificationByHost.has(host.id)) {
              classificationByHost.set(host.id, { hostName: host.name, hostStatus: host.status, instances: [] });
            }
            classificationByHost.get(host.id)!.instances.push(item);
          } else if (category === 'embedding') {
            if (!embeddingByHost.has(host.id)) {
              embeddingByHost.set(host.id, { hostName: host.name, hostStatus: host.status, instances: [] });
            }
            embeddingByHost.get(host.id)!.instances.push(item);
          } else if (category === 'reranker') {
            if (!rerankerByHost.has(host.id)) {
              rerankerByHost.set(host.id, { hostName: host.name, hostStatus: host.status, instances: [] });
            }
            rerankerByHost.get(host.id)!.instances.push(item);
          } else {
            if (!generationByHost.has(host.id)) {
              generationByHost.set(host.id, { hostName: host.name, hostStatus: host.status, instances: [] });
            }
            generationByHost.get(host.id)!.instances.push(item);
          }
        });
    });

    // Calculate max alias length for dynamic width
    const allInstances = [
      ...generationByHost.values(),
      ...classificationByHost.values(),
      ...embeddingByHost.values(),
      ...rerankerByHost.values(),
    ].flatMap((data) => data.instances);
    const maxAliasLength = allInstances.reduce((max, { instance }) => Math.max(max, instance.config.alias.length), 0);
    // Calculate width: base padding + estimated char width (roughly 7px per char for semibold text)
    const instanceBoxWidth = Math.max(130, Math.min(220, 30 + maxAliasLength * 7));

    // Build mappings for routing
    const instanceToHost = new Map<string, string>();
    const instanceToGroup = new Map<string, string>();
    const hostToGroup = new Map<string, string>();

    generationByHost.forEach((data, hostId) => {
      const hostNodeId = `host-gen-${hostId}`;
      hostToGroup.set(hostNodeId, GROUP_GENERATION_ID);
      data.instances.forEach(({ instance }) => {
        const instanceNodeId = `instance-${hostId}-${instance.id}`;
        instanceToHost.set(instanceNodeId, hostNodeId);
        instanceToGroup.set(instanceNodeId, GROUP_GENERATION_ID);
      });
    });

    classificationByHost.forEach((data, hostId) => {
      const hostNodeId = `host-class-${hostId}`;
      hostToGroup.set(hostNodeId, GROUP_CLASSIFICATION_ID);
      data.instances.forEach(({ instance }) => {
        const instanceNodeId = `instance-${hostId}-${instance.id}`;
        instanceToHost.set(instanceNodeId, hostNodeId);
        instanceToGroup.set(instanceNodeId, GROUP_CLASSIFICATION_ID);
      });
    });

    embeddingByHost.forEach((data, hostId) => {
      const hostNodeId = `host-embed-${hostId}`;
      hostToGroup.set(hostNodeId, GROUP_EMBEDDING_ID);
      data.instances.forEach(({ instance }) => {
        const instanceNodeId = `instance-${hostId}-${instance.id}`;
        instanceToHost.set(instanceNodeId, hostNodeId);
        instanceToGroup.set(instanceNodeId, GROUP_EMBEDDING_ID);
      });
    });

    rerankerByHost.forEach((data, hostId) => {
      const hostNodeId = `host-rerank-${hostId}`;
      hostToGroup.set(hostNodeId, GROUP_RERANKER_ID);
      data.instances.forEach(({ instance }) => {
        const instanceNodeId = `instance-${hostId}-${instance.id}`;
        instanceToHost.set(instanceNodeId, hostNodeId);
        instanceToGroup.set(instanceNodeId, GROUP_RERANKER_ID);
      });
    });

    const solarControlBg = '#5E81AC';

    // Layout positions: Request(50) -> Endpoint(200) -> Solar Control(520) -> Group(840) -> Host(1120) -> Instance(1420)
    const endpointX = 200;
    const controlX = 520;
    const groupX = 840;
    const hostX = 1120;
    const instanceX = 1420;

    const instanceHeight = 85;
    const hostGap = 20;
    const groupGap = 50;

    // Calculate total heights
    let genTotalHeight = 0;
    generationByHost.forEach((data) => {
      genTotalHeight += data.instances.length * instanceHeight + hostGap;
    });
    if (genTotalHeight > 0) genTotalHeight -= hostGap;

    let classTotalHeight = 0;
    classificationByHost.forEach((data) => {
      classTotalHeight += data.instances.length * instanceHeight + hostGap;
    });
    if (classTotalHeight > 0) classTotalHeight -= hostGap;

    let embedTotalHeight = 0;
    embeddingByHost.forEach((data) => {
      embedTotalHeight += data.instances.length * instanceHeight + hostGap;
    });
    if (embedTotalHeight > 0) embedTotalHeight -= hostGap;

    let rerankTotalHeight = 0;
    rerankerByHost.forEach((data) => {
      rerankTotalHeight += data.instances.length * instanceHeight + hostGap;
    });
    if (rerankTotalHeight > 0) rerankTotalHeight -= hostGap;

    const totalHeight =
      genTotalHeight +
      (genTotalHeight > 0 && classTotalHeight > 0 ? groupGap : 0) +
      classTotalHeight +
      ((genTotalHeight > 0 || classTotalHeight > 0) && embedTotalHeight > 0 ? groupGap : 0) +
      embedTotalHeight +
      ((genTotalHeight > 0 || classTotalHeight > 0 || embedTotalHeight > 0) && rerankTotalHeight > 0 ? groupGap : 0) +
      rerankTotalHeight;
    const startY = 100;
    const controlY = startY + totalHeight / 2 - 50;

    // 1. Endpoint nodes (leftmost infrastructure column)
    const endpointNodeHeight = 50;
    const endpointGap = 12;
    const endpointsTotalHeight =
      endpoints.length > 0 ? endpoints.length * endpointNodeHeight + (endpoints.length - 1) * endpointGap : 0;
    const endpointsStartY = controlY - endpointsTotalHeight / 2 + endpointNodeHeight / 2;

    endpoints.forEach((ep, idx) => {
      const endpointNodeId = getEndpointNodeId(ep.id);
      const epColor = ENDPOINT_PALETTE[idx % ENDPOINT_PALETTE.length];
      const epY = endpointsStartY + idx * (endpointNodeHeight + endpointGap);

      newNodes.push({
        id: endpointNodeId,
        type: 'default',
        position: { x: endpointX, y: epY },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2 px-2 py-1">
              <Zap size={12} style={{ opacity: 0.9 }} />
              <span className="font-semibold text-sm">{ep.name}</span>
            </div>
          ),
        },
        style: {
          background: epColor,
          color: '#ECEFF4',
          border: `1px solid ${getBrighterColor(epColor)}`,
          borderRadius: '6px',
          width: 120,
          boxShadow: '0 2px 4px rgba(0, 0, 0, 0.1)',
        },
      });
    });

    // 2. Solar Control node
    newNodes.push({
      id: SOLAR_CONTROL_NODE_ID,
      type: 'default',
      position: { x: controlX, y: controlY },
      sourcePosition: Position.Right,
      targetPosition: Position.Left,
      data: {
        label: (
          <div className="px-3 py-2">
            <div className="font-bold text-base mb-1 text-center">Solar Control</div>
            <div className="text-xs space-y-0.5 text-nord-6 opacity-90 text-left">
              <div>
                {onlineHosts} / {hosts.length} hosts online
              </div>
              <div>
                {runningInstances} / {totalInstances} instances
              </div>
              <div>{activeRequests} active requests</div>
            </div>
          </div>
        ),
      },
      style: {
        background: solarControlBg,
        color: '#ECEFF4',
        border: `1px solid ${getBrighterColor(solarControlBg)}`,
        borderRadius: '8px',
        width: 170,
        boxShadow: '0 4px 6px -1px rgba(0, 0, 0, 0.1)',
      },
    });

    // Static edges: Endpoint -> Solar Control (structure when we have endpoints)
    if (endpoints.length > 0) {
      endpoints.forEach((ep) => {
        newEdges.push({
          id: `static-${getEndpointNodeId(ep.id)}-to-control`,
          source: getEndpointNodeId(ep.id),
          target: SOLAR_CONTROL_NODE_ID,
          animated: false,
          style: { stroke: '#4C566A', strokeWidth: 1 },
        });
      });
    }

    // Helper to create instance node
    const createInstanceNode = (hostId: string, instance: Instance, x: number, y: number): string => {
      const instanceNodeId = `instance-${hostId}-${instance.id}`;
      const backendType = getBackendType(instance.config);
      const backendDisplay = getBackendDisplay(instance);
      const instanceBg = '#88C0D0';

      const isProcessing = processingInstances.has(`${hostId}-${instance.id}`);
      const runtime = getInstanceState(hostId, instance.id);
      const phase = runtime?.phase;
      const prefillPct =
        typeof runtime?.prefill_progress === 'number' ? Math.round(runtime.prefill_progress * 100) : null;

      const borderColor =
        phase === 'prefill'
          ? '#EBCB8B'
          : phase === 'generating'
            ? '#A3BE8C'
            : isProcessing
              ? '#D08770'
              : getBrighterColor(instanceBg);

      newNodes.push({
        id: instanceNodeId,
        type: 'default',
        position: { x, y },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="text-xs">
              <div className="flex items-center gap-1 mb-0.5">
                <span
                  className="px-1 rounded text-[6px] font-medium leading-tight"
                  style={{
                    backgroundColor: backendDisplay.color,
                    color: backendType === 'huggingface_classification' ? '#2E3440' : '#ECEFF4',
                  }}
                >
                  {backendDisplay.label}
                </span>
              </div>
              <div className="font-semibold">{instance.config.alias}</div>
              <div className="text-[10px] text-nord-0 opacity-70">
                {phase && phase !== 'idle' ? <span className="uppercase font-medium">{phase}</span> : 'idle'}
                {typeof runtime?.decode_tps === 'number' && phase === 'generating' && (
                  <span className="ml-1">{runtime.decode_tps.toFixed(1)} t/s</span>
                )}
              </div>
              {phase === 'prefill' && typeof prefillPct === 'number' && prefillPct < 100 && (
                <div className="w-full h-1 mt-1 bg-nord-3 rounded">
                  <div className="h-1 bg-nord-13 rounded" style={{ width: `${prefillPct}%` }} />
                </div>
              )}
            </div>
          ),
        },
        style: {
          background: instanceBg,
          color: '#2E3440',
          border: `1px solid ${borderColor}`,
          borderRadius: '6px',
          padding: '4px',
          width: instanceBoxWidth,
          boxShadow: '0 2px 4px rgba(0, 0, 0, 0.1)',
        },
      });

      return instanceNodeId;
    };

    let currentY = startY;

    // 2. Text Generation section
    if (generationByHost.size > 0) {
      const genStartY = currentY;
      const groupCenterY = genStartY + genTotalHeight / 2 - 20;

      // Group node
      newNodes.push({
        id: GROUP_GENERATION_ID,
        type: 'default',
        position: { x: groupX, y: groupCenterY },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2 px-3 py-1.5">
              <MessageSquare size={14} className="text-nord-14" />
              <span className="font-semibold text-sm whitespace-nowrap">Text Generation</span>
            </div>
          ),
        },
        style: {
          background: 'linear-gradient(135deg, #3B4252 0%, #434C5E 100%)',
          color: '#ECEFF4',
          border: '1px solid #A3BE8C',
          borderRadius: '8px',
          width: 175,
        },
      });

      // Edge: Solar Control → Generation Group
      newEdges.push({
        id: `control-to-gen-group`,
        source: SOLAR_CONTROL_NODE_ID,
        target: GROUP_GENERATION_ID,
        animated: false,
        style: { stroke: '#4C566A', strokeWidth: 1 },
      });

      // Hosts and instances
      let hostY = genStartY;
      generationByHost.forEach((data, hostId) => {
        const hostNodeId = `host-gen-${hostId}`;
        const hostInstanceCount = data.instances.length;
        const hostHeight = hostInstanceCount * instanceHeight;
        const hostCenterY = hostY + hostHeight / 2 - 15;

        // Host node - green when online, grey when offline
        const hostBg = data.hostStatus === 'online' ? '#A3BE8C' : '#434C5E';
        const hostTextColor = data.hostStatus === 'online' ? '#2E3440' : '#D8DEE9';
        const hostBorder = data.hostStatus === 'online' ? '#8CAA7E' : '#4C566A';

        newNodes.push({
          id: hostNodeId,
          type: 'default',
          position: { x: hostX, y: hostCenterY },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          data: {
            label: (
              <div className="flex items-center gap-1.5 px-2 py-1">
                <Server size={12} style={{ opacity: 0.8 }} />
                <span className="font-medium text-xs">{data.hostName}</span>
              </div>
            ),
          },
          style: {
            background: hostBg,
            color: hostTextColor,
            border: `1px solid ${hostBorder}`,
            borderRadius: '6px',
            width: 180,
          },
        });

        // Edge: Group → Host
        newEdges.push({
          id: `gen-group-to-${hostNodeId}`,
          source: GROUP_GENERATION_ID,
          target: hostNodeId,
          animated: false,
          style: { stroke: '#4C566A', strokeWidth: 1 },
        });

        // Instances
        let instanceY = hostY;
        data.instances.forEach(({ instance }) => {
          const instanceNodeId = createInstanceNode(hostId, instance, instanceX, instanceY);

          // Edge: Host → Instance
          newEdges.push({
            id: `${hostNodeId}-to-${instanceNodeId}`,
            source: hostNodeId,
            target: instanceNodeId,
            animated: false,
            style: { stroke: '#4C566A', strokeWidth: 1 },
          });

          instanceY += instanceHeight;
        });

        hostY += hostHeight + hostGap;
      });

      currentY = hostY + groupGap - hostGap;
    }

    // 3. Classification section
    if (classificationByHost.size > 0) {
      const classStartY = currentY;
      const groupCenterY = classStartY + classTotalHeight / 2 - 20;

      // Group node
      newNodes.push({
        id: GROUP_CLASSIFICATION_ID,
        type: 'default',
        position: { x: groupX, y: groupCenterY },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2 px-3 py-1.5">
              <Tags size={14} className="text-nord-13" />
              <span className="font-semibold text-sm whitespace-nowrap">Classification</span>
            </div>
          ),
        },
        style: {
          background: 'linear-gradient(135deg, #3B4252 0%, #434C5E 100%)',
          color: '#ECEFF4',
          border: '1px solid #EBCB8B',
          borderRadius: '8px',
          width: 175,
        },
      });

      // Edge: Solar Control → Classification Group
      newEdges.push({
        id: `control-to-class-group`,
        source: SOLAR_CONTROL_NODE_ID,
        target: GROUP_CLASSIFICATION_ID,
        animated: false,
        style: { stroke: '#4C566A', strokeWidth: 1 },
      });

      // Hosts and instances
      let hostY = classStartY;
      classificationByHost.forEach((data, hostId) => {
        const hostNodeId = `host-class-${hostId}`;
        const hostInstanceCount = data.instances.length;
        const hostHeight = hostInstanceCount * instanceHeight;
        const hostCenterY = hostY + hostHeight / 2 - 15;

        // Host node - green when online, grey when offline
        const hostBg = data.hostStatus === 'online' ? '#A3BE8C' : '#434C5E';
        const hostTextColor = data.hostStatus === 'online' ? '#2E3440' : '#D8DEE9';
        const hostBorder = data.hostStatus === 'online' ? '#8CAA7E' : '#4C566A';

        newNodes.push({
          id: hostNodeId,
          type: 'default',
          position: { x: hostX, y: hostCenterY },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          data: {
            label: (
              <div className="flex items-center gap-1.5 px-2 py-1">
                <Server size={12} style={{ opacity: 0.8 }} />
                <span className="font-medium text-xs">{data.hostName}</span>
              </div>
            ),
          },
          style: {
            background: hostBg,
            color: hostTextColor,
            border: `1px solid ${hostBorder}`,
            borderRadius: '6px',
            width: 180,
          },
        });

        // Edge: Group → Host
        newEdges.push({
          id: `class-group-to-${hostNodeId}`,
          source: GROUP_CLASSIFICATION_ID,
          target: hostNodeId,
          animated: false,
          style: { stroke: '#4C566A', strokeWidth: 1 },
        });

        // Instances
        let instanceY = hostY;
        data.instances.forEach(({ instance }) => {
          const instanceNodeId = createInstanceNode(hostId, instance, instanceX, instanceY);

          // Edge: Host → Instance
          newEdges.push({
            id: `${hostNodeId}-to-${instanceNodeId}`,
            source: hostNodeId,
            target: instanceNodeId,
            animated: false,
            style: { stroke: '#4C566A', strokeWidth: 1 },
          });

          instanceY += instanceHeight;
        });

        hostY += hostHeight + hostGap;
      });

      currentY = hostY + groupGap - hostGap;
    }

    // 4. Embedding section
    if (embeddingByHost.size > 0) {
      const embedStartY = currentY;
      const groupCenterY = embedStartY + embedTotalHeight / 2 - 20;

      // Group node
      newNodes.push({
        id: GROUP_EMBEDDING_ID,
        type: 'default',
        position: { x: groupX, y: groupCenterY },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2 px-3 py-1.5">
              <Binary size={14} className="text-nord-15" />
              <span className="font-semibold text-sm whitespace-nowrap">Embedding</span>
            </div>
          ),
        },
        style: {
          background: 'linear-gradient(135deg, #3B4252 0%, #434C5E 100%)',
          color: '#ECEFF4',
          border: '1px solid #B48EAD',
          borderRadius: '8px',
          width: 175,
        },
      });

      // Edge: Solar Control → Embedding Group
      newEdges.push({
        id: `control-to-embed-group`,
        source: SOLAR_CONTROL_NODE_ID,
        target: GROUP_EMBEDDING_ID,
        animated: false,
        style: { stroke: '#4C566A', strokeWidth: 1 },
      });

      // Hosts and instances
      let hostY = embedStartY;
      embeddingByHost.forEach((data, hostId) => {
        const hostNodeId = `host-embed-${hostId}`;
        const hostInstanceCount = data.instances.length;
        const hostHeight = hostInstanceCount * instanceHeight;
        const hostCenterY = hostY + hostHeight / 2 - 15;

        // Host node - green when online, grey when offline
        const hostBg = data.hostStatus === 'online' ? '#A3BE8C' : '#434C5E';
        const hostTextColor = data.hostStatus === 'online' ? '#2E3440' : '#D8DEE9';
        const hostBorder = data.hostStatus === 'online' ? '#8CAA7E' : '#4C566A';

        newNodes.push({
          id: hostNodeId,
          type: 'default',
          position: { x: hostX, y: hostCenterY },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          data: {
            label: (
              <div className="flex items-center gap-1.5 px-2 py-1">
                <Server size={12} style={{ opacity: 0.8 }} />
                <span className="font-medium text-xs">{data.hostName}</span>
              </div>
            ),
          },
          style: {
            background: hostBg,
            color: hostTextColor,
            border: `1px solid ${hostBorder}`,
            borderRadius: '6px',
            width: 180,
          },
        });

        // Edge: Group → Host
        newEdges.push({
          id: `embed-group-to-${hostNodeId}`,
          source: GROUP_EMBEDDING_ID,
          target: hostNodeId,
          animated: false,
          style: { stroke: '#4C566A', strokeWidth: 1 },
        });

        // Instances
        let instanceY = hostY;
        data.instances.forEach(({ instance }) => {
          const instanceNodeId = createInstanceNode(hostId, instance, instanceX, instanceY);

          // Edge: Host → Instance
          newEdges.push({
            id: `${hostNodeId}-to-${instanceNodeId}`,
            source: hostNodeId,
            target: instanceNodeId,
            animated: false,
            style: { stroke: '#4C566A', strokeWidth: 1 },
          });

          instanceY += instanceHeight;
        });

        hostY += hostHeight + hostGap;
      });

      currentY = hostY + groupGap - hostGap;
    }

    // 5. Reranker section
    if (rerankerByHost.size > 0) {
      const rerankStartY = currentY;
      const groupCenterY = rerankStartY + rerankTotalHeight / 2 - 20;

      // Group node
      newNodes.push({
        id: GROUP_RERANKER_ID,
        type: 'default',
        position: { x: groupX, y: groupCenterY },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2 px-3 py-1.5">
              <Search size={14} className="text-nord-12" />
              <span className="font-semibold text-sm whitespace-nowrap">Reranker</span>
            </div>
          ),
        },
        style: {
          background: 'linear-gradient(135deg, #3B4252 0%, #434C5E 100%)',
          color: '#ECEFF4',
          border: '1px solid #D08770',
          borderRadius: '8px',
          width: 175,
        },
      });

      // Edge: Solar Control → Reranker Group
      newEdges.push({
        id: `control-to-rerank-group`,
        source: SOLAR_CONTROL_NODE_ID,
        target: GROUP_RERANKER_ID,
        animated: false,
        style: { stroke: '#4C566A', strokeWidth: 1 },
      });

      // Hosts and instances
      let hostY = rerankStartY;
      rerankerByHost.forEach((data, hostId) => {
        const hostNodeId = `host-rerank-${hostId}`;
        const hostInstanceCount = data.instances.length;
        const hostHeight = hostInstanceCount * instanceHeight;
        const hostCenterY = hostY + hostHeight / 2 - 15;

        // Host node - green when online, grey when offline
        const hostBg = data.hostStatus === 'online' ? '#A3BE8C' : '#434C5E';
        const hostTextColor = data.hostStatus === 'online' ? '#2E3440' : '#D8DEE9';
        const hostBorder = data.hostStatus === 'online' ? '#8CAA7E' : '#4C566A';

        newNodes.push({
          id: hostNodeId,
          type: 'default',
          position: { x: hostX, y: hostCenterY },
          sourcePosition: Position.Right,
          targetPosition: Position.Left,
          data: {
            label: (
              <div className="flex items-center gap-1.5 px-2 py-1">
                <Server size={12} style={{ opacity: 0.8 }} />
                <span className="font-medium text-xs">{data.hostName}</span>
              </div>
            ),
          },
          style: {
            background: hostBg,
            color: hostTextColor,
            border: `1px solid ${hostBorder}`,
            borderRadius: '6px',
            width: 180,
          },
        });

        // Edge: Group → Host
        newEdges.push({
          id: `rerank-group-to-${hostNodeId}`,
          source: GROUP_RERANKER_ID,
          target: hostNodeId,
          animated: false,
          style: { stroke: '#4C566A', strokeWidth: 1 },
        });

        // Instances
        let instanceY = hostY;
        data.instances.forEach(({ instance }) => {
          const instanceNodeId = createInstanceNode(hostId, instance, instanceX, instanceY);

          // Edge: Host → Instance
          newEdges.push({
            id: `${hostNodeId}-to-${instanceNodeId}`,
            source: hostNodeId,
            target: instanceNodeId,
            animated: false,
            style: { stroke: '#4C566A', strokeWidth: 1 },
          });

          instanceY += instanceHeight;
        });

        hostY += hostHeight + hostGap;
      });
    }

    // 6. Request nodes (left side)
    const requestArray = Array.from(requests.values());
    requestArray.forEach((request, index) => {
      const yOffset = 80 + index * 100;
      const requestNodeId = `request-${request.request_id}`;

      newNodes.push({
        id: requestNodeId,
        type: 'default',
        position: { x: 50, y: yOffset },
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        data: {
          label: (
            <div className="flex items-center gap-2">
              <div className="flex flex-col text-left">
                <span
                  className="font-semibold text-sm truncate max-w-[140px]"
                  title={request.resolved_model || request.model}
                >
                  {request.resolved_model || request.model}
                </span>
                {request.host_name && <span className="text-xs text-nord-6 opacity-80">{request.host_name}</span>}
                <span className="text-xs text-nord-6 opacity-50">{request.request_id.substring(0, 8)}</span>
                {request.duration && (
                  <span className="text-xs text-nord-6 opacity-80 font-medium">{request.duration.toFixed(2)}s</span>
                )}
              </div>
              {request.status === 'error' && (
                <button
                  onClick={() => removeRequest(request.request_id)}
                  className="p-0.5 hover:bg-nord-0 hover:bg-opacity-20 rounded"
                  title="Dismiss"
                >
                  <X size={14} />
                </button>
              )}
            </div>
          ),
        },
        style: {
          background: getStatusColor(request.status),
          color: '#ECEFF4',
          border: `1px solid ${getBrighterColor(getStatusColor(request.status))}`,
          borderRadius: '6px',
          padding: '8px',
          width: 180,
          boxShadow: '0 2px 4px rgba(0, 0, 0, 0.1)',
        },
        className: request.removing ? 'request-node-animated removing' : 'request-node-animated',
      });

      const requestColor = getStatusColor(request.status);
      const hasKnownEndpoint = request.endpoint_id && endpoints.some((ep) => ep.id === request.endpoint_id);
      const endpointColor = hasKnownEndpoint ? getEndpointColor(endpoints, request.endpoint_id!) : requestColor;
      const edgeStrokeColor = hasKnownEndpoint ? endpointColor : requestColor;

      if (hasKnownEndpoint && request.endpoint_id) {
        const endpointNodeId = getEndpointNodeId(request.endpoint_id);
        // Edge: Request → Endpoint
        const edgeReqToEpId = `${requestNodeId}-to-${endpointNodeId}`;
        const isReqToEpNew = !previousEdgeIdsRef.current.has(edgeReqToEpId);
        currentEdgeIds.add(edgeReqToEpId);
        newEdges.push({
          id: edgeReqToEpId,
          source: requestNodeId,
          target: endpointNodeId,
          animated: request.status === 'pending' || request.status === 'processing' || request.status === 'routed',
          style: { stroke: edgeStrokeColor, strokeWidth: 3 },
          markerEnd: { type: MarkerType.ArrowClosed, color: edgeStrokeColor },
          className: request.removing
            ? 'request-edge removing-edge'
            : isReqToEpNew
              ? 'request-edge request-edge-new'
              : 'request-edge',
        });
        // Edge: Endpoint → Solar Control
        const edgeEpToCtrlId = `${endpointNodeId}-to-control-${request.request_id}`;
        const isEpToCtrlNew = !previousEdgeIdsRef.current.has(edgeEpToCtrlId);
        currentEdgeIds.add(edgeEpToCtrlId);
        newEdges.push({
          id: edgeEpToCtrlId,
          source: endpointNodeId,
          target: SOLAR_CONTROL_NODE_ID,
          animated: request.status === 'pending' || request.status === 'processing' || request.status === 'routed',
          style: { stroke: edgeStrokeColor, strokeWidth: 3 },
          markerEnd: { type: MarkerType.ArrowClosed, color: edgeStrokeColor },
          className: request.removing
            ? 'request-edge removing-edge'
            : isEpToCtrlNew
              ? 'request-edge request-edge-new'
              : 'request-edge',
        });
      } else {
        // Edge: Request → Solar Control (direct when no endpoint_id)
        const edge1Id = `${requestNodeId}-to-control`;
        const isEdge1New = !previousEdgeIdsRef.current.has(edge1Id);
        currentEdgeIds.add(edge1Id);
        newEdges.push({
          id: edge1Id,
          source: requestNodeId,
          target: SOLAR_CONTROL_NODE_ID,
          animated: request.status === 'pending' || request.status === 'processing' || request.status === 'routed',
          style: { stroke: requestColor, strokeWidth: 3 },
          markerEnd: { type: MarkerType.ArrowClosed, color: requestColor },
          className: request.removing
            ? 'request-edge removing-edge'
            : isEdge1New
              ? 'request-edge request-edge-new'
              : 'request-edge',
        });
      }

      // Routing path when request is routed
      if (request.instance_id && request.host_id) {
        const instanceNodeId = `instance-${request.host_id}-${request.instance_id}`;
        const hostNodeId = instanceToHost.get(instanceNodeId);
        const groupId = instanceToGroup.get(instanceNodeId);

        if (groupId && hostNodeId) {
          // Edge: Solar Control → Group
          const edge2Id = `control-to-${groupId}-${request.request_id}`;
          const isEdge2New = !previousEdgeIdsRef.current.has(edge2Id);
          currentEdgeIds.add(edge2Id);

          newEdges.push({
            id: edge2Id,
            source: SOLAR_CONTROL_NODE_ID,
            target: groupId,
            animated: request.status === 'processing' || request.status === 'routed',
            style: { stroke: getStatusColor(request.status), strokeWidth: 3 },
            markerEnd: { type: MarkerType.ArrowClosed, color: getStatusColor(request.status) },
            className: request.removing
              ? 'request-edge removing-edge'
              : isEdge2New
                ? 'request-edge request-edge-new'
                : 'request-edge',
          });

          // Edge: Group → Host
          const edge3Id = `${groupId}-to-${hostNodeId}-${request.request_id}`;
          const isEdge3New = !previousEdgeIdsRef.current.has(edge3Id);
          currentEdgeIds.add(edge3Id);

          newEdges.push({
            id: edge3Id,
            source: groupId,
            target: hostNodeId,
            animated: request.status === 'processing' || request.status === 'routed',
            style: { stroke: getStatusColor(request.status), strokeWidth: 3 },
            markerEnd: { type: MarkerType.ArrowClosed, color: getStatusColor(request.status) },
            className: request.removing
              ? 'request-edge removing-edge'
              : isEdge3New
                ? 'request-edge request-edge-new'
                : 'request-edge',
          });

          // Edge: Host → Instance
          const edge4Id = `${hostNodeId}-to-${instanceNodeId}-${request.request_id}`;
          const isEdge4New = !previousEdgeIdsRef.current.has(edge4Id);
          currentEdgeIds.add(edge4Id);

          newEdges.push({
            id: edge4Id,
            source: hostNodeId,
            target: instanceNodeId,
            animated: request.status === 'processing' || request.status === 'routed',
            style: { stroke: getStatusColor(request.status), strokeWidth: 3 },
            markerEnd: { type: MarkerType.ArrowClosed, color: getStatusColor(request.status) },
            className: request.removing
              ? 'request-edge removing-edge'
              : isEdge4New
                ? 'request-edge request-edge-new'
                : 'request-edge',
          });
        }
      }
    });

    setNodes(newNodes);
    setEdges(newEdges);
    previousEdgeIdsRef.current = currentEdgeIds;
  }, [requests, hosts, endpoints, removeRequest, setNodes, setEdges, getInstanceState]);

  if (loading) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-xl text-nord-4">Loading routing visualization...</div>
      </div>
    );
  }

  return (
    <div className="w-full bg-nord-0">
      <style>{`
        .react-flow__node.request-node-animated { animation: fadeIn 0.3s ease-out; }
        .react-flow__node.request-node-animated.removing { animation: fadeOut 0.3s ease-out forwards; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes fadeOut { from { opacity: 1; } to { opacity: 0; } }
        .react-flow__node { transition: transform 0.4s cubic-bezier(0.4, 0, 0.2, 1) !important; }
        .react-flow__handle { opacity: 0 !important; pointer-events: none !important; }
        .react-flow__edge.request-edge { transition: stroke 0.3s ease-out !important; }
        .react-flow__edge.request-edge-new { animation: edgeFadeIn 0.3s ease-out; }
        .react-flow__edge.removing-edge { animation: edgeFadeOut 0.3s ease-out forwards; }
        @keyframes edgeFadeIn { from { opacity: 0; } to { opacity: 1; } }
        @keyframes edgeFadeOut { from { opacity: 1; } to { opacity: 0; } }
      `}</style>
      <div className="p-4 bg-nord-1 border-b border-nord-3">
        <h1 className="text-2xl font-bold text-nord-6">Solar Routing Visualization</h1>
        <p className="text-sm text-nord-4 mt-1">Real-time view of API requests flowing through the system</p>
      </div>
      <div style={{ height: 'calc(100vh - 160px)' }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          fitView
          fitViewOptions={{ padding: 0.1, maxZoom: 1.2 }}
          attributionPosition="bottom-right"
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          minZoom={0.3}
          maxZoom={1.5}
        >
          <Controls />
          <MiniMap
            nodeColor={(node) => {
              if (node.id === SOLAR_CONTROL_NODE_ID) return '#5E81AC';
              if (node.id.startsWith('endpoint-')) return (node.style?.background as string) || '#4C566A';
              if (node.id === GROUP_GENERATION_ID) return '#A3BE8C';
              if (node.id === GROUP_CLASSIFICATION_ID) return '#EBCB8B';
              if (node.id === GROUP_EMBEDDING_ID) return '#B48EAD';
              if (node.id === GROUP_RERANKER_ID) return '#D08770';
              if (node.id.startsWith('host-')) return '#434C5E';
              if (node.id.startsWith('request-')) return (node.style?.background as string) || '#4C566A';
              return '#88C0D0';
            }}
            style={{ backgroundColor: '#3B4252' }}
            maskColor="rgba(46, 52, 64, 0.6)"
          />
        </ReactFlow>
      </div>
    </div>
  );
}
