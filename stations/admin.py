from django.contrib import admin

from stations.models import DataImportLog, Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price_per_gallon", "is_geocoded")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
    ordering = ("state", "city")


@admin.register(DataImportLog)
class DataImportLogAdmin(admin.ModelAdmin):
    """Read-only visibility into import history, when a reimport last
    happened, and how many stations it covered, without needing shell
    access. This is the table that drives route-plan cache invalidation
    (see stations/data_version.py), so being able to see it matters."""

    list_display = ("imported_at", "station_count")
    readonly_fields = ("imported_at", "station_count")
    ordering = ("-imported_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
