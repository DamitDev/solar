import { useState, useCallback } from 'react';
import { useInstances } from '@/hooks/useInstances';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';
import { HostCard } from './HostCard';
import { UnifiedTable } from './UnifiedTable';
import { AddHostModal } from './AddHostModal';
import { PendingHostBanner } from './PendingHostBanner';
import { Plus, RefreshCw, AlertCircle, Server, LayoutGrid, Table2 } from 'lucide-react';
import solarClient from '@/api/client';
import { cn, getIntentOwnership } from '@/lib/utils';
import { Instance } from '@/api/types';
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  DragEndEvent,
} from '@dnd-kit/core';
import { SortableContext, sortableKeyboardCoordinates, verticalListSortingStrategy } from '@dnd-kit/sortable';

type ViewMode = 'cards' | 'table';

const VIEW_MODE_KEY = 'solar_view_mode';

function readViewMode(): ViewMode {
  try {
    const raw = localStorage.getItem(VIEW_MODE_KEY);
    if (raw === 'cards' || raw === 'table') return raw;
  } catch {
    /* ignore */
  }
  return 'cards';
}

export function Dashboard() {
  const {
    hosts,
    loading,
    error,
    refresh,
    startInstance,
    stopInstance,
    restartInstance,
    reorderHost,
    reorderInstance,
    isHostReachable,
  } = useInstances();
  const { pendingHosts } = useRoutingEventsContext();

  const [showAddHost, setShowAddHost] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [viewMode, setViewMode] = useState<ViewMode>(readViewMode);

  const handleViewModeToggle = (mode: ViewMode) => {
    setViewMode(mode);
    localStorage.setItem(VIEW_MODE_KEY, mode);
  };

  const handleRefresh = async () => {
    setRefreshing(true);
    await refresh();
    setTimeout(() => setRefreshing(false), 500);
  };

  const handleDeleteHost = async (hostId: string) => {
    if (!confirm('Are you sure you want to remove this host?')) return;

    try {
      await solarClient.deleteHost(hostId);
      await refresh();
    } catch (error) {
      console.error('Failed to delete host:', error);
      alert('Failed to delete host');
    }
  };

  const handleCreateInstance = async (hostId: string, config: any) => {
    try {
      await solarClient.createInstance(hostId, config);
      await refresh();
    } catch (error) {
      console.error('Failed to create instance:', error);
      throw error;
    }
  };

  const handleUpdateInstance = async (hostId: string, instanceId: string, config: any) => {
    try {
      await solarClient.updateInstance(hostId, instanceId, config);
      await refresh();
    } catch (error) {
      console.error('Failed to update instance:', error);
      throw error;
    }
  };

  // Find the full instance record so ownership can be checked before actions
  const findInstance = useCallback(
    (hostId: string, instanceId: string): Instance | undefined =>
      hosts.find((h) => h.id === hostId)?.instances.find((i) => i.id === instanceId),
    [hosts],
  );

  // Managed instances are recreated by Solar Control — warn first
  const warnIfManaged = useCallback((instance?: Instance): boolean => {
    if (!instance) return true;
    const { managed } = getIntentOwnership(instance);
    if (!managed) return true;
    return window.confirm(
      `This instance is managed by Solar Control. Stopping it is temporary — it will be started again automatically. Delete it instead?`,
    );
  }, []);

  const handleStopInstance = async (hostId: string, instanceId: string) => {
    if (!warnIfManaged(findInstance(hostId, instanceId))) return;
    await stopInstance(hostId, instanceId);
  };

  const handleDeleteInstance = async (hostId: string, instanceId: string) => {
    if (!warnIfManaged(findInstance(hostId, instanceId))) return;
    if (!confirm('Are you sure you want to delete this instance?')) return;

    try {
      await solarClient.deleteInstance(hostId, instanceId);
      await refresh();
    } catch (error) {
      console.error('Failed to delete instance:', error);
      alert('Failed to delete instance');
    }
  };

  // DnD sensors with activation constraint to distinguish drag from click
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleHostDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || active.id === over.id) return;
      reorderHost(active.id as string, over.id as string);
    },
    [reorderHost],
  );

  if (loading && hosts.length === 0) {
    return (
      <div className="flex items-center justify-center" style={{ height: 'calc(100vh - 60px)' }}>
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-nord-9 mx-auto mb-4"></div>
          <p className="text-nord-4">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-nord-0">
      {/* Header */}
      <header className="bg-nord-1 shadow-lg">
        <div className="max-w-7xl mx-auto px-4 py-6 sm:px-6 lg:px-8">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-3xl font-bold text-nord-6">Solar Dashboard</h1>
              <p className="text-sm text-nord-4 mt-1">Multi-Host LLM Manager</p>
            </div>
            <div className="flex gap-2 items-center">
              {/* View mode toggle */}
              <div className="flex bg-nord-3 rounded-lg p-0.5">
                <button
                  onClick={() => handleViewModeToggle('cards')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    viewMode === 'cards' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="Card view"
                >
                  <LayoutGrid size={16} />
                </button>
                <button
                  onClick={() => handleViewModeToggle('table')}
                  className={cn(
                    'flex items-center gap-1 px-3 py-1.5 rounded-md text-sm transition-colors',
                    viewMode === 'table' ? 'bg-nord-10 text-nord-6' : 'text-nord-4 hover:text-nord-6',
                  )}
                  title="Table view"
                >
                  <Table2 size={16} />
                </button>
              </div>
              <button
                onClick={handleRefresh}
                disabled={refreshing}
                className="flex items-center gap-2 px-4 py-2 bg-nord-3 text-nord-6 rounded-lg hover:bg-nord-2 transition-colors disabled:opacity-50"
              >
                <RefreshCw size={18} className={refreshing ? 'animate-spin' : ''} />
                Refresh
              </button>
              <button
                onClick={() => setShowAddHost(true)}
                className="flex items-center gap-2 px-4 py-2 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
              >
                <Plus size={18} />
                Add Host
              </button>
            </div>
          </div>
        </div>
      </header>

      {/* Main Content */}
      <main className="max-w-7xl mx-auto px-4 py-8 sm:px-6 lg:px-8">
        {error && (
          <div className="mb-6 p-4 bg-nord-11 bg-opacity-20 border border-nord-11 rounded-lg flex items-start gap-3">
            <AlertCircle className="text-nord-11 flex-shrink-0" size={20} />
            <div>
              <h3 className="font-semibold text-nord-6">Error</h3>
              <p className="text-sm text-nord-4">{error}</p>
            </div>
          </div>
        )}

        {pendingHosts.size > 0 && <PendingHostBanner pendingHosts={pendingHosts} onApproved={refresh} />}

        {hosts.length === 0 ? (
          <div className="text-center py-16">
            <Server size={64} className="mx-auto text-nord-3 mb-4" />
            <h2 className="text-2xl font-semibold text-nord-6 mb-2">No hosts configured</h2>
            <p className="text-nord-4 mb-6">Add your first solar-host to get started</p>
            <button
              onClick={() => setShowAddHost(true)}
              className="inline-flex items-center gap-2 px-6 py-3 bg-nord-10 text-nord-6 rounded-lg hover:bg-nord-9 transition-colors"
            >
              <Plus size={20} />
              Add Host
            </button>
          </div>
        ) : viewMode === 'table' ? (
          /* Unified table view */
          <UnifiedTable
            hosts={hosts}
            isHostReachable={isHostReachable}
            onStartInstance={startInstance}
            onStopInstance={handleStopInstance}
            onRestartInstance={restartInstance}
            onUpdateInstance={handleUpdateInstance}
            onDeleteInstance={handleDeleteInstance}
          />
        ) : (
          /* Cards view with drag-and-drop host reordering */
          <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={handleHostDragEnd}>
            <SortableContext items={hosts.map((h) => h.id)} strategy={verticalListSortingStrategy}>
              <div className="space-y-6">
                {hosts.map((host) => (
                  <HostCard
                    key={host.id}
                    host={host}
                    hostReachable={isHostReachable(host.id)}
                    onReorderInstance={(hostId, activeId, overId) => reorderInstance(hostId, activeId, overId)}
                    onStartInstance={startInstance}
                    onStopInstance={handleStopInstance}
                    onRestartInstance={restartInstance}
                    onUpdateInstance={handleUpdateInstance}
                    onDeleteInstance={handleDeleteInstance}
                    onCreateInstance={handleCreateInstance}
                    onDeleteHost={handleDeleteHost}
                  />
                ))}
              </div>
            </SortableContext>
          </DndContext>
        )}
      </main>

      {/* Modals */}
      {showAddHost && <AddHostModal onClose={() => setShowAddHost(false)} onSuccess={() => refresh()} />}
    </div>
  );
}
