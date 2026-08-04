import { Link, useLocation } from 'react-router-dom';
import { ReactNode, useEffect, useRef, useState } from 'react';
import { Activity, Server, Target, Gauge, Key, Database, LogOut, Menu, X } from 'lucide-react';
import { useRoutingEventsContext } from '@/context/RoutingEventsContext';

type NavItemDef = {
  to: string;
  icon: ReactNode;
  label: string;
  isActive: (pathname: string) => boolean;
};

const NAV_ITEMS: NavItemDef[] = [
  { to: '/routing', icon: <Activity size={18} />, label: 'Routing', isActive: (p) => p === '/routing' },
  { to: '/gateway', icon: <Activity size={18} />, label: 'Gateway', isActive: (p) => p === '/gateway' },
  { to: '/hosts', icon: <Server size={18} />, label: 'Hosts & Instances', isActive: (p) => p === '/hosts' },
  { to: '/intents', icon: <Target size={18} />, label: 'Intents', isActive: (p) => p.startsWith('/intents') },
  { to: '/resources', icon: <Gauge size={18} />, label: 'Resources', isActive: (p) => p === '/resources' },
  { to: '/endpoints', icon: <Key size={18} />, label: 'Endpoints', isActive: (p) => p === '/endpoints' },
  { to: '/catalog', icon: <Database size={18} />, label: 'Catalog', isActive: (p) => p === '/catalog' },
];

function NavLink({ item, onNavigate, fullWidth }: { item: NavItemDef; onNavigate?: () => void; fullWidth?: boolean }) {
  const location = useLocation();
  const active = item.isActive(location.pathname);
  return (
    <Link
      to={item.to}
      onClick={onNavigate}
      className={`flex items-center gap-2 px-4 py-2 rounded-lg transition-colors whitespace-nowrap shrink-0 ${
        fullWidth ? 'w-full' : ''
      } ${active ? 'bg-nord-10 text-nord-6 font-medium' : 'text-nord-4 hover:bg-nord-2'}`}
    >
      {item.icon}
      {item.label}
    </Link>
  );
}

export function Navigation() {
  const [menuOpen, setMenuOpen] = useState(false);
  const navRef = useRef<HTMLElement>(null);
  let isConnected = false;
  try {
    const ctx = useRoutingEventsContext();
    isConnected = ctx.routingConnected;
  } catch {}

  // Close the collapsed menu on Escape or when clicking outside the nav bar.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setMenuOpen(false);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (navRef.current && !navRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onPointerDown);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onPointerDown);
    };
  }, [menuOpen]);

  return (
    <nav ref={navRef} className="relative bg-nord-1 border-b border-nord-3 px-4 sm:px-6 py-3">
      <div className="flex items-center gap-3 sm:gap-6 min-w-0">
        {/* Logo — pinned */}
        <div className="flex items-center gap-2 mr-4 sm:mr-8 shrink-0">
          <Activity className="text-nord-8" size={24} />
          <span className="font-bold text-xl text-nord-6">Solar</span>
        </div>

        {/* Link strip — desktop only; scrolls internally if space runs out */}
        <div className="hidden xl:flex flex-1 min-w-0 items-center gap-1 overflow-x-auto no-scrollbar">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} item={item} />
          ))}
        </div>

        {/* Status + logout + menu toggle — pinned */}
        <div className="ml-auto flex items-center gap-3 sm:gap-4 text-xs shrink-0">
          <div className="flex items-center gap-2">
            <span className={isConnected ? 'text-nord-14' : 'text-nord-11'}>●</span>
            <span className="text-nord-4 hidden sm:inline">Event Stream</span>
          </div>
          <form method="post" action="/auth/logout">
            <button
              type="submit"
              className="flex items-center gap-1 rounded-md px-2 py-1 text-nord-4 transition-colors hover:bg-nord-2 hover:text-nord-6"
            >
              <LogOut size={14} />
              <span className="hidden sm:inline">Logout</span>
            </button>
          </form>
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={menuOpen ? 'Close menu' : 'Open menu'}
            aria-expanded={menuOpen}
            className="xl:hidden p-2 rounded-lg text-nord-4 transition-colors hover:bg-nord-2 hover:text-nord-6"
          >
            {menuOpen ? <X size={20} /> : <Menu size={20} />}
          </button>
        </div>
      </div>

      {/* Collapsed menu — dropdown under the bar on tablet/mobile widths */}
      {menuOpen && (
        <div className="xl:hidden absolute right-4 sm:right-6 top-full mt-2 w-64 rounded-lg border border-nord-3 bg-nord-1 shadow-xl p-2 z-50 flex flex-col gap-0.5">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} item={item} fullWidth onNavigate={() => setMenuOpen(false)} />
          ))}
        </div>
      )}
    </nav>
  );
}
