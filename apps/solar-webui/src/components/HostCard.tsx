import { useState, useMemo, useCallback } from 'react';
import { Instance, InstanceConfig, MemoryInfo, getModelCategory, ModelCategory } from '@/api/types';
import {
  cn,
  getStatusColor,
  formatDate,
  getMemoryColor,
  formatMemoryUsage,
  formatDiskUsage,
  getGpuTypeLabel,
  getGpuTypeBadgeClass,
  getRoleBadgeClass,
} from '@/lib/utils';
import { InstanceCard } from './InstanceCard';
import { AddInstanceModal } from './AddInstanceModal';
import {
  Server,
  Trash2,
  Plus,
  MessageSquare,
  Brain,
  Tags,
  Binary,
  Search,
  GripVertical,
  Cpu,
  Microchip,
  HardDrive,
  MemoryStick,
} from 'lucide-react';
import { useSortable } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import {
  DndContext,
  closestCenter,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, rectSortingStrategy } from '@dnd-kit/sortable';

interface HostCardProps {
  host: {
    id: string;
    name: string;
    url: string;
    api_key: string;
    status: string;
    last_seen?: string;
    memory?: MemoryInfo;
    gpu_type?: string;
    roles?: string[];
    disk_total_gb?: number;
    disk_used_gb?: number;
    disk_available_gb?: number;
    version?: string;
    instances: Instance[];
  };
  hostReachable: boolean;
  onReorderInstance: (hostId: string, activeId: string, overId: string) => void;
  onStartInstance: (hostId: string, instanceId: string) => Promise<void>;
  onStopInstance: (hostId: string, instanceId: string) => Promise<void>;
  onRestartInstance: (hostId: string, instanceId: string) => Promise<void>;
  onUpdateInstance: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
  onDeleteInstance: (hostId: string, instanceId: string) => Promise<void>;
  onCreateInstance: (hostId: string, config: InstanceConfig) => Promise<void>;
  onDeleteHost: (hostId: string) => Promise<void>;
}

const CategoryIcon = ({ category, size = 14 }: { category: ModelCategory; size?: number }) => {
  switch (category) {
    case 'generation':
      return <MessageSquare size={size} />;
    case 'classification':
      return <Tags size={size} />;
    case 'embedding':
      return <Binary size={size} />;
    case 'reranker':
      return <Search size={size} />;
    default:
      return <Brain size={size} />;
  }
};

const getCategoryLabel = (category: ModelCategory): string => {
  switch (category) {
    case 'generation':
      return 'Text Gen';
    case 'classification':
      return 'Classifier';
    case 'embedding':
      return 'Embedding';
    case 'reranker':
      return 'Reranker';
    default:
      return category;
  }
};

/** Wrapper to make each InstanceCard sortable */
function SortableInstanceCard({
  instance,
  hostId,
  hostReachable,
  onStart,
  onStop,
  onRestart,
  onUpdate,
  onDelete,
}: {
  instance: Instance;
  hostId: string;
  hostReachable: boolean;
  onStart: (hostId: string, instanceId: string) => Promise<void>;
  onStop: (hostId: string, instanceId: string) => Promise<void>;
  onRestart: (hostId: string, instanceId: string) => Promise<void>;
  onUpdate: (hostId: string, instanceId: string, config: InstanceConfig) => Promise<void>;
  onDelete: (hostId: string, instanceId: string) => Promise<void>;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id: instance.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : 1,
    zIndex: isDragging ? 50 : undefined,
  };

  return (
    <div ref={setNodeRef} style={style} {...attributes}>
      <InstanceCard
        instance={instance}
        hostId={hostId}
        hostReachable={hostReachable}
        onStart={onStart}
        onStop={onStop}
        onRestart={onRestart}
        onUpdate={onUpdate}
        onDelete={onDelete}
        dragListeners={listeners}
      />
    </div>
  );
}

export function HostCard({
  host,
  hostReachable,
  onReorderInstance,
  onStartInstance,
  onStopInstance,
  onRestartInstance,
  onUpdateInstance,
  onDeleteInstance,
  onCreateInstance,
  onDeleteHost,
}: HostCardProps) {
  const [showAddModal, setShowAddModal] = useState(false);
  const runningCount = host.instances.filter((i) => i.status === 'running').length;

  // Make the host card itself sortable (for host reordering)
  const {
    attributes: hostAttributes,
    listeners: hostListeners,
    setNodeRef: setHostNodeRef,
    transform: hostTransform,
    transition: hostTransition,
    isDragging: hostIsDragging,
  } = useSortable({ id: host.id });

  const hostStyle = {
    transform: CSS.Transform.toString(hostTransform),
    transition: hostTransition,
    opacity: hostIsDragging ? 0.5 : 1,
    zIndex: hostIsDragging ? 50 : undefined,
  };

  // Instance DnD
  const instanceSensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleInstanceDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      onReorderInstance(host.id, active.id as string, over.id as string);
    },
    [host.id, onReorderInstance],
  );

  // Compute model category counts
  const categoryCounts = useMemo(() => {
    const counts: Record<ModelCategory, { total: number; running: number }> = {
      generation: { total: 0, running: 0 },
      classification: { total: 0, running: 0 },
      embedding: { total: 0, running: 0 },
      reranker: { total: 0, running: 0 },
    };

    for (const instance of host.instances) {
      const category = getModelCategory(instance.config);
      counts[category].total++;
      if (instance.status === 'running') {
        counts[category].running++;
      }
    }

    return counts;
  }, [host.instances]);

  const activeCategories = (
    Object.entries(categoryCounts) as [ModelCategory, { total: number; running: number }][]
  ).filter(([, { total }]) => total > 0);

  return (
    <>
      <div
        ref={setHostNodeRef}
        style={hostStyle}
        {...hostAttributes}
        className="bg-nord-1 rounded-lg shadow-lg overflow-hidden"
      >
        {/* Host Header */}
        <div className="bg-gradient-to-r from-nord-10 to-nord-9 p-4">
          <div className="flex items-start justify-between">
            <div className="flex items-center gap-3">
              {/* Drag handle for host reordering */}
              <button
                {...hostListeners}
                className="p-1 rounded cursor-grab active:cursor-grabbing hover:bg-white hover:bg-opacity-20 transition-colors text-nord-6 touch-none"
                title="Drag to reorder host"
              >
                <GripVertical size={20} />
              </button>
              <Server size={24} />
              <div>
                <h2 className="text-xl font-bold text-nord-6" title={host.url}>
                  {host.name}
                </h2>
                {host.version && <p className="text-xs text-nord-4 opacity-70">v{host.version}</p>}
              </div>
            </div>
            <div className="flex items-center gap-2">
              {host.roles &&
                host.roles.length > 0 &&
                host.roles.map((role) => (
                  <span
                    key={role}
                    className={cn('px-2 py-1 rounded-full text-xs font-medium', getRoleBadgeClass(role))}
                  >
                    {role}
                  </span>
                ))}
              {host.gpu_type && (
                <span
                  className={cn(
                    'px-2 py-1 rounded-full text-xs font-medium flex items-center gap-1',
                    getGpuTypeBadgeClass(host.gpu_type),
                  )}
                  title={`Acceleration: ${getGpuTypeLabel(host.gpu_type)}`}
                >
                  {host.gpu_type === 'cpu' ? <Cpu size={12} /> : <Microchip size={12} />}
                  {getGpuTypeLabel(host.gpu_type)}
                </span>
              )}
              <span className={cn('px-3 py-1 rounded-full text-xs font-medium', getStatusColor(host.status))}>
                {host.status}
              </span>
              {!hostReachable && (
                <span className="px-2 py-1 rounded-full text-xs font-medium bg-nord-11 bg-opacity-30 text-nord-11">
                  disconnected
                </span>
              )}
              <button
                onClick={() => onDeleteHost(host.id)}
                className="p-2 hover:bg-nord-8 hover:bg-opacity-30 rounded transition-colors text-nord-6"
                title="Remove host"
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>

          <div className="mt-3 flex items-center justify-between text-sm text-nord-4">
            <div className="flex items-center gap-4">
              <span>
                {runningCount} / {host.instances.length} instances running
              </span>
              {activeCategories.length > 0 && (
                <div className="flex items-center gap-2">
                  {activeCategories.map(([category, { total, running }]) => (
                    <span
                      key={category}
                      className={cn(
                        'px-2 py-0.5 rounded-full text-xs font-medium flex items-center gap-1',
                        category === 'generation' && 'bg-nord-14 bg-opacity-30 text-nord-14',
                        category === 'classification' && 'bg-nord-13 bg-opacity-30 text-nord-13',
                        category === 'embedding' && 'bg-nord-15 bg-opacity-30 text-nord-15',
                        category === 'reranker' && 'bg-nord-12 bg-opacity-30 text-nord-12',
                      )}
                      title={`${getCategoryLabel(category)}: ${running}/${total}`}
                    >
                      <CategoryIcon category={category} />
                      <span>
                        {running}/{total}
                      </span>
                    </span>
                  ))}
                </div>
              )}
            </div>
            <div className="flex items-center gap-2">
              {host.last_seen && <span>Last seen: {formatDate(host.last_seen)}</span>}
              <button
                onClick={() => setShowAddModal(true)}
                disabled={!hostReachable}
                className={cn(
                  'flex items-center gap-1 px-3 py-1 rounded transition-colors text-sm font-medium shadow-md',
                  hostReachable
                    ? 'bg-nord-6 hover:bg-nord-5 text-nord-0'
                    : 'bg-nord-3 text-nord-4 cursor-not-allowed opacity-60',
                )}
                title={hostReachable ? 'Add instance' : 'Host is offline'}
              >
                <Plus size={16} />
                Add Instance
              </button>
            </div>
          </div>

          {/* Memory Usage */}
          {host.memory && (
            <div className="mt-3 space-y-1">
              <div className="flex items-center justify-between text-xs text-nord-4">
                <span className="font-medium flex items-center gap-1">
                  <MemoryStick size={12} />
                  {host.memory.memory_type} Usage:
                </span>
                <span>{formatMemoryUsage(host.memory.used_gb, host.memory.total_gb, host.memory.percent)}</span>
              </div>
              <div className="w-full h-2 bg-nord-2 rounded-full overflow-hidden">
                <div
                  className={cn('h-full transition-all duration-300 rounded-full', getMemoryColor(host.memory.percent))}
                  style={{ width: `${Math.min(host.memory.percent, 100)}%` }}
                />
              </div>
            </div>
          )}

          {/* Disk Usage */}
          {host.disk_total_gb != null && host.disk_used_gb != null && host.disk_total_gb > 0 && (
            <div className="mt-2 space-y-1">
              <div className="flex items-center justify-between text-xs text-nord-4">
                <span className="font-medium flex items-center gap-1">
                  <HardDrive size={12} />
                  Disk Usage:
                </span>
                <span>{formatDiskUsage(host.disk_used_gb, host.disk_total_gb)}</span>
              </div>
              <div className="w-full h-2 bg-nord-2 rounded-full overflow-hidden">
                <div
                  className={cn(
                    'h-full transition-all duration-300 rounded-full',
                    getMemoryColor((host.disk_used_gb / host.disk_total_gb) * 100),
                  )}
                  style={{ width: `${Math.min((host.disk_used_gb / host.disk_total_gb) * 100, 100)}%` }}
                />
              </div>
            </div>
          )}
        </div>

        {/* Instances - Cards with drag-and-drop */}
        <div className="p-4">
          {host.instances.length === 0 ? (
            <div className="text-center py-8 text-nord-4">
              <p>No instances configured</p>
              <button
                onClick={() => setShowAddModal(true)}
                disabled={!hostReachable}
                className={cn(
                  'mt-4 px-6 py-3 rounded-lg transition-colors font-medium shadow-md',
                  hostReachable
                    ? 'bg-nord-10 text-nord-6 hover:bg-nord-9'
                    : 'bg-nord-3 text-nord-4 cursor-not-allowed opacity-60',
                )}
                title={hostReachable ? undefined : 'Host is offline'}
              >
                <Plus size={20} className="inline-block mr-2" />
                Add First Instance
              </button>
            </div>
          ) : (
            <DndContext sensors={instanceSensors} collisionDetection={closestCenter} onDragEnd={handleInstanceDragEnd}>
              <SortableContext items={host.instances.map((i) => i.id)} strategy={rectSortingStrategy}>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                  {host.instances.map((instance) => (
                    <SortableInstanceCard
                      key={instance.id}
                      instance={instance}
                      hostId={host.id}
                      hostReachable={hostReachable}
                      onStart={onStartInstance}
                      onStop={onStopInstance}
                      onRestart={onRestartInstance}
                      onUpdate={onUpdateInstance}
                      onDelete={onDeleteInstance}
                    />
                  ))}
                </div>
              </SortableContext>
            </DndContext>
          )}
        </div>
      </div>

      {/* Add Instance Modal */}
      {showAddModal && (
        <AddInstanceModal
          hostId={host.id}
          hostName={host.name}
          onClose={() => setShowAddModal(false)}
          onCreate={onCreateInstance}
        />
      )}
    </>
  );
}
