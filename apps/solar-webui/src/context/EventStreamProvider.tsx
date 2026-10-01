import { ReactNode } from 'react';
import { useEventStream, EventHandlers } from '@/hooks/eventStream/useEventStream';
import { EventStreamContext } from './EventStreamContext';

interface EventStreamProviderProps {
  children: ReactNode;
  handlers?: EventHandlers;
}

export function EventStreamProvider({ children, handlers }: EventStreamProviderProps) {
  const eventStream = useEventStream(handlers);

  return <EventStreamContext.Provider value={eventStream}>{children}</EventStreamContext.Provider>;
}
