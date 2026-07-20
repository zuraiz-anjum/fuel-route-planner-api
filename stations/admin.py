from django.contrib import admin

from stations.models import Station


@admin.register(Station)
class StationAdmin(admin.ModelAdmin):
    list_display = ("opis_id", "name", "city", "state", "price_per_gallon", "is_geocoded")
    list_filter = ("state",)
    search_fields = ("name", "city", "opis_id")
    ordering = ("state", "city")
