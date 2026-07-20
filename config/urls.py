"""URL configuration for the Fuel Route Planner API."""

from django.contrib import admin
from django.urls import include, path

from config.views import health_check

urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz/", health_check, name="health-check"),
    path("api/v1/", include("planner.urls", namespace="planner")),
]
