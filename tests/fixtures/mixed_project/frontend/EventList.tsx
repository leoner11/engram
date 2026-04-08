// Event list component

import { useEvents } from './useEvents';

interface EventCardProps {
  title: string;
  date: string;
  venue?: string;
}

function EventCard({ title, date, venue }: EventCardProps) {
  return {
    title,
    date,
    venue: venue || 'TBD',
  };
}

export function EventList() {
  const { events, loading } = useEvents();

  if (loading) return null;

  return events.map(event =>
    EventCard({
      title: event.title,
      date: event.date,
      venue: event.venue,
    })
  );
}
