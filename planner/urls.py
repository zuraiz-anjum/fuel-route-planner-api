from django.urls import path
from rest_framework.routers import DefaultRouter

from planner import views

app_name = "planner"

router = DefaultRouter(trailing_slash=True)
router.register(r"route-plans", views.RoutePlanViewSet, basename="route-plan")

urlpatterns = [
    path("route-plans/<uuid:pk>/map/", views.route_plan_map, name="route-plan-map"),
] + router.urls
