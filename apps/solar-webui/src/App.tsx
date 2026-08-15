import { createBrowserRouter, RouterProvider, Navigate, Outlet } from 'react-router-dom';
import { Dashboard } from './components/Dashboard';
import { RoutingFlow } from './components/RoutingFlow';
import { GatewayDashboard } from './components/GatewayDashboard';
import { EndpointsDashboard } from './components/endpoints/EndpointsDashboard';
import { ModelCatalog } from './components/ModelCatalog';
import { IntentsPage } from './components/IntentsPage';
import { IntentDetail } from './components/IntentDetail';
import { StoragePage } from './components/StoragePage';
import { ArtifactUpload } from './components/ArtifactUpload';
import { Navigation } from './components/Navigation';
import { ResourcesPage } from './components/ResourcesPage';
import { RoutingEventsProvider } from './context/RoutingEventsContext';

function Layout() {
  return (
    <RoutingEventsProvider>
      <div className="min-h-screen bg-nord-0">
        <Navigation />
        <Outlet />
      </div>
    </RoutingEventsProvider>
  );
}

const router = createBrowserRouter([
  {
    element: <Layout />,
    children: [
      { path: '/', element: <Navigate to="/routing" replace /> },
      { path: '/routing', element: <RoutingFlow /> },
      { path: '/gateway', element: <GatewayDashboard /> },
      { path: '/hosts', element: <Dashboard /> },
      { path: '/intents', element: <IntentsPage /> },
      { path: '/intents/:id', element: <IntentDetail /> },
      { path: '/resources', element: <ResourcesPage /> },
      { path: '/endpoints', element: <EndpointsDashboard /> },
      { path: '/catalog', element: <ModelCatalog /> },
      { path: '/storage', element: <StoragePage /> },
      { path: '/upload', element: <ArtifactUpload /> },
    ],
  },
]);

function App() {
  return <RouterProvider router={router} />;
}

export default App;
