from django.contrib import admin

from planner.models import FuelStop, RoutePlan


class FuelStopInline(admin.TabularInline):
    model = FuelStop
    extra = 0
    readonly_fields = [f.name for f in FuelStop._meta.fields if f.name != "id"]
    can_delete = False


@admin.register(RoutePlan)
class RoutePlanAdmin(admin.ModelAdmin):
    list_display = ("id", "start_query", "finish_query", "distance_miles", "total_cost", "created_at")
    search_fields = ("start_query", "finish_query")
    readonly_fields = [f.name for f in RoutePlan._meta.fields]
    inlines = [FuelStopInline]
