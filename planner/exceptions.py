"""Domain-level exceptions for the route planner, plus a DRF exception
handler that turns them into clean, consistent 4xx JSON responses instead of
500s -- callers get a stable error contract regardless of which stage failed.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_default_exception_handler


class PlannerError(Exception):
    """Base class for expected, user-facing planning failures."""

    default_message = "Unable to plan the route."
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str | None = None):
        self.message = message or self.default_message
        super().__init__(self.message)


class GeocodingError(PlannerError):
    default_message = "Could not resolve the given location to coordinates."


class RoutingError(PlannerError):
    default_message = "Could not compute a driving route between the given locations."
    status_code = status.HTTP_502_BAD_GATEWAY


class InfeasibleTripError(PlannerError):
    """Raised when no sequence of available stations can keep the vehicle
    within its range for the whole trip (e.g. a >500mi gap with no station
    in it)."""

    default_message = "This trip cannot be completed within the vehicle's range using known stations."


class SameLocationError(PlannerError):
    """Raised when start and finish resolve to (essentially) the same point.

    A plain string comparison ("Chicago, IL" == "Chicago, Illinois"?) misses
    this the moment the two queries are worded differently but name the same
    place -- this check runs *after* geocoding, on actual coordinates, so it
    catches the semantic duplicate, not just the literal one."""

    default_message = "Start and finish resolve to the same location."


def api_exception_handler(exc, context):
    if isinstance(exc, PlannerError):
        return Response({"error": exc.message}, status=exc.status_code)
    return drf_default_exception_handler(exc, context)
