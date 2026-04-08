"""Django-style views for the event API."""

from models import Event, EventSerializer


def list_events(request):
    """List all events. GET /api/events/"""
    events = Event.objects.all()
    serializer = EventSerializer(events, many=True)
    return serializer.data


def create_event(request, event_data):
    """Create a new event. POST /api/events/"""
    serializer = EventSerializer(data=event_data)
    serializer.is_valid(raise_exception=True)
    event = serializer.save()
    return event


def get_event(request, event_id):
    """Get a single event. GET /api/events/{id}/"""
    event = Event.objects.get(id=event_id)
    serializer = EventSerializer(event)
    return serializer.data
