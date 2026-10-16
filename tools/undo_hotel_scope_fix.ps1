$ErrorActionPreference = "Stop"

function Restore-Exact {
    param(
        [string]$Path,
        [scriptblock]$Transform
    )
    $raw = Get-Content -LiteralPath $Path -Raw
    $next = & $Transform $raw
    if ($next -ne $raw) {
        Set-Content -LiteralPath $Path -Value $next -NoNewline
        Write-Host "reverted $Path"
    } else {
        Write-Host "no-op $Path"
    }
}

Restore-Exact "admin_ui/src/pages/ChatHarness.tsx" {
    param($raw)
    $raw = $raw -replace [regex]::Escape("    // A fresh conversation must not inherit the previously selected booking.`r`n    // If it does, every new turn keeps sending the old booking's property name`r`n    // and room context, which re-pins the backend to that hotel.`r`n    setSelectedBookingId("""");`r`n"), ""
    $raw = $raw -replace [regex]::Escape("      setSelectedBookingId((prev) => {`r`n        if (typeof prev === ""number"" && rows.some((row) => row.booking_id === prev)) return prev;`r`n        return """";`r`n      });"), "      setSelectedBookingId((prev) => {`r`n        if (typeof prev === ""number"" && rows.some((row) => row.booking_id === prev)) return prev;`r`n        if (rows.length === 1) return rows[0].booking_id;`r`n        return """";`r`n      });"
    return $raw
}

Restore-Exact "services/llm_orchestration_service.py" {
    param($raw)
    $raw = [regex]::Replace(
        $raw,
        '(?s)\r?\n    @classmethod\r?\n    def _is_compare_with_discussed_hotels_request\(.*?\r?\n    @classmethod\r?\n    def _should_preserve_pending_key_on_property_switch',
        "`r`n    @classmethod`r`n    def _should_preserve_pending_key_on_property_switch"
    )
    $raw = [regex]::Replace(
        $raw,
        '(?s)\r?\n        # Deterministic repair: when the guest explicitly compares the CURRENT.*?\r?\n        if not main_router_property_ids and main_router_book_ids:',
        "`r`n        if not main_router_property_ids and main_router_book_ids:"
    )
    $raw = $raw -replace [regex]::Escape("        _direct_recall_reply = self._direct_discussed_hotel_recall_reply(`r`n            user_message=user_message,`r`n            discussed_properties=_discussed_clean,`r`n        )`r`n        if _direct_recall_reply:`r`n            return _direct_recall_reply, [], []`r`n"), ""
    return $raw
}

Restore-Exact "tests/test_hotel_recall_repairs.py" {
    param($raw)
    return ""
}

if (Test-Path "tests/test_hotel_recall_repairs.py") {
    Remove-Item -LiteralPath "tests/test_hotel_recall_repairs.py" -Force
    Write-Host "removed tests/test_hotel_recall_repairs.py"
}
