"""
Set the Zone hotel chat-widget brand color to orange (#f8971d).

Scope: ONLY deployments whose hotel_code == "zone". Other tenants are untouched.
The widget theme is stored per-deployment in the DB table
`new_bot_widget_deployments` (the production data-widget-key embed reads this),
so this must run ON A MACHINE WITH DB ACCESS (VPN).

Usage:
    python tools/set_zone_widget_color.py            # DRY RUN — shows what would change
    python tools/set_zone_widget_color.py --apply    # actually write the change

It uses the app's own DB connection + the validated WidgetDeploymentService.update()
path, so normalization/validation is identical to the Admin UI Widget editor.
"""
from __future__ import annotations

import asyncio
import sys

from models.database import engine
from services.widget_deployment_service import widget_deployment_service

HOTEL_CODE = "zone"
NEW_COLOR = "#f8971d"  # orange — replaces the default pink #FF6816


async def main(apply: bool) -> int:
    print(f"DB: {engine.url.render_as_string(hide_password=True)}")
    print(f"Target hotel_code: {HOTEL_CODE!r}   New brand/accent: {NEW_COLOR}")
    print("Mode: " + ("APPLY (will write)" if apply else "DRY RUN (no writes)") + "\n")

    deployments = await widget_deployment_service.list(hotel_code=HOTEL_CODE)
    if not deployments:
        print(f"No widget deployments found for hotel_code={HOTEL_CODE!r}.")
        print(
            "Nothing to update. If zone is embedded via data-hotel-code (not a\n"
            "widget_key), create a deployment in Admin > Widget, or add a\n"
            "config/properties/zone.json with business.widget_theme instead."
        )
        return 1

    changed = 0
    for dep in deployments:
        key = dep["widget_key"]
        theme = dict(dep.get("theme") or {})
        before = {"brand_color": theme.get("brand_color"), "accent_color": theme.get("accent_color")}
        if before["brand_color"] == NEW_COLOR and before["accent_color"] == NEW_COLOR:
            print(f"  [skip] {key} ({dep.get('name')}) already {NEW_COLOR}")
            continue

        print(f"  [{'apply' if apply else 'would change'}] {key} ({dep.get('name')})")
        print(f"      brand_color: {before['brand_color']} -> {NEW_COLOR}")
        print(f"      accent_color: {before['accent_color']} -> {NEW_COLOR}")

        if apply:
            new_theme = {**theme, "brand_color": NEW_COLOR, "accent_color": NEW_COLOR}
            updated = await widget_deployment_service.update(key, {"theme": new_theme})
            if updated:
                changed += 1
            else:
                print(f"      !! update returned None for {key}")

    print()
    if apply:
        print(f"Done. Updated {changed} deployment(s) for hotel_code={HOTEL_CODE!r}.")
    else:
        print("Dry run complete. Re-run with --apply to write the change.")
    return 0


if __name__ == "__main__":
    apply = "--apply" in sys.argv[1:]
    raise SystemExit(asyncio.run(main(apply)))
