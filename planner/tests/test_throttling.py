from django.test import RequestFactory, SimpleTestCase
from rest_framework.throttling import AnonRateThrottle


class AnonThrottleIdentityTests(SimpleTestCase):
    """Regression coverage for a real bypass: DRF's throttle identifies a
    client by X-Forwarded-For as-is whenever NUM_PROXIES isn't configured --
    tested this live against a running server and a client could dodge the
    whole 60/min limit just by sending a different fake header on every
    request. NUM_PROXIES=0 (set in REST_FRAMEWORK settings) makes it ignore
    that header entirely and key off REMOTE_ADDR instead, which is correct
    for this app's default no-reverse-proxy deployments.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.throttle = AnonRateThrottle()

    def test_spoofed_x_forwarded_for_is_ignored(self):
        request_a = self.factory.get("/api/v1/route-plans/", HTTP_X_FORWARDED_FOR="1.2.3.4")
        request_b = self.factory.get("/api/v1/route-plans/", HTTP_X_FORWARDED_FOR="5.6.7.8")

        ident_a = self.throttle.get_ident(request_a)
        ident_b = self.throttle.get_ident(request_b)

        self.assertEqual(ident_a, ident_b, "the same client shouldn't get a fresh throttle bucket per request")
        self.assertEqual(ident_a, request_a.META["REMOTE_ADDR"])
