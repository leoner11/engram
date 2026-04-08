// React hook for fetching events from the API

import { useState, useEffect } from 'react';

interface Event {
  id: string;
  title: string;
  description: string;
  date: string;
  venue?: string;
  capacity: number;
}

export function useEvents() {
  const [events, setEvents] = useState<Event[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch('/api/events/')
      .then(res => res.json())
      .then(data => {
        setEvents(data);
        setLoading(false);
      });
  }, []);

  return { events, loading };
}

export function useCreateEvent() {
  const createEvent = async (eventData: Partial<Event>) => {
    const res = await fetch('/api/events/', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(eventData),
    });
    return res.json();
  };

  return { createEvent };
}
