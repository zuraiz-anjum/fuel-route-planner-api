from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from planner.exceptions import RoutingError
from planner.services.geocoding import Coordinates
from planner.services.routing import get_route

ORIGIN = Coordinates(latitude=40.0, longitude=-95.0)
DESTINATION = Coordinates(latitude=40.0, longitude=-85.0)


class GetRouteErrorMessageTests(SimpleTestCase):
    def _mock_response(self, payload):
        response = MagicMock()
        response.json.return_value = payload
        return response

    def test_no_route_code_gets_the_no_route_message(self):
        payload = {"code": "NoRoute", "message": "no route found"}
        with patch("planner.services.routing.requests.get", return_value=self._mock_response(payload)):
            with self.assertRaises(RoutingError) as ctx:
                get_route(ORIGIN, DESTINATION)
        self.assertIn("No driving route exists", ctx.exception.message)

    def test_no_segment_code_also_gets_the_no_route_message(self):
        payload = {"code": "NoSegment", "message": "could not find a matching segment"}
        with patch("planner.services.routing.requests.get", return_value=self._mock_response(payload)):
            with self.assertRaises(RoutingError) as ctx:
                get_route(ORIGIN, DESTINATION)
        self.assertIn("No driving route exists", ctx.exception.message)

    def test_other_error_codes_do_not_get_the_no_route_message(self):
        # Any non-"Ok" OSRM code used to be reported as "no driving route
        # exists between these locations" --
        # true for NoRoute/NoSegment, but not for a request/service problem
        # like TooBig (route too complex for the server to handle). Lumping
        # them together hides real integration issues behind a message that
        # sounds like a permanent, nothing-to-be-done geographic fact.
        payload = {"code": "TooBig", "message": "Route is too big for server to handle"}
        with patch("planner.services.routing.requests.get", return_value=self._mock_response(payload)):
            with self.assertRaises(RoutingError) as ctx:
                get_route(ORIGIN, DESTINATION)
        self.assertNotIn("No driving route exists", ctx.exception.message)
        self.assertIn("could not process this request", ctx.exception.message)
