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

    def has_add_permission(self, request):
        # Every field is read-only (this is a computed/persisted record,
        # not something anyone should hand-author), which made the "Add"
        # page a confusing dead end: it rendered with zero editable fields
        # and no Save button. Removing the "Add" action entirely instead
        # of leaving a broken-looking link in the admin list view.
        return False
