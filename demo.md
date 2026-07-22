
```powershell
$body = @{ start = "Atlanta, GA"; finish = "Salt Lake City, UT" } | ConvertTo-Json
$response = Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/route-plans/ -Method Post -Body $body -ContentType "application/json"
$response
$response.fuel_stops | Format-Table order, station_name, price_per_gallon, gallons_purchased, cost

```

```powershell
$r2 = Invoke-WebRequest -Uri http://127.0.0.1:8000/api/v1/route-plans/ -Method Post -Body $body -ContentType "application/json"
$r2.StatusCode
```


```powershell
Start-Process "http://127.0.0.1:8000/api/v1/route-plans/$($response.id)/map/"
```

---

```powershell
$badBody = @{ start = "Chicago, IL"; finish = "Chicago, IL" } | ConvertTo-Json
try {
    Invoke-RestMethod -Uri http://127.0.0.1:8000/api/v1/route-plans/ -Method Post -Body $badBody -ContentType "application/json"
} catch {
    $_.Exception.Response.StatusCode.value__
}
```
